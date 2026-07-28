from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.config import Settings, get_settings


def test_api_defaults_are_bounded_and_do_not_enable_a_secret_transport() -> None:
    settings = Settings.model_validate({})

    assert settings.api_local_bearer_token is None
    assert settings.api_oidc_jwks_url is None
    assert settings.api_oidc_algorithms == ("RS256",)
    assert settings.api_max_request_bytes == 65_536
    assert settings.api_bind_host == "127.0.0.1"
    assert settings.api_port == 8520
    assert settings.api_limit_concurrency == 100
    assert settings.api_docs_enabled is False
    assert settings.control_pool_min_size == 1
    assert settings.control_pool_max_size == 8
    assert settings.control_pool_max_waiting == 32
    assert settings.control_pool_acquisition_timeout_seconds == 2
    assert settings.control_pool_startup_timeout_seconds == 10
    assert settings.control_pool_close_timeout_seconds == 5
    assert settings.worker_heartbeat_seconds * 2 < settings.worker_lease_seconds
    assert settings.worker_identity_lineage_mode == "exact-local"
    assert settings.api_local_bearer_token is None


def test_control_pool_limits_are_bounded_and_ordered() -> None:
    with pytest.raises(ValidationError, match="minimum size"):
        _component_settings(
            {
                "SCHEMABRIDGE_COMPONENT": "api",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                    "postgresql://schemabridge_api:password@control.example.test/control"
                ),
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": (
                    "local-api-bearer-token-with-enough-byte-diversity-123"
                ),
                "SCHEMABRIDGE_CONTROL_POOL_MIN_SIZE": 5,
                "SCHEMABRIDGE_CONTROL_POOL_MAX_SIZE": 4,
            }
        )


def test_inventory_cursor_key_is_strong_api_only_and_distinct() -> None:
    token = "local-api-bearer-token-with-enough-byte-diversity-123"
    cursor_key = "inventory-cursor-signing-key-with-diversity-456789"
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "api",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                "postgresql://schemabridge_api:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": token,
            "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": cursor_key,
        }
    )

    assert settings.inventory_cursor_signing_key is not None
    assert cursor_key not in repr(settings)

    with pytest.raises(ValidationError, match="must be distinct"):
        _component_settings(
            {
                "SCHEMABRIDGE_COMPONENT": "api",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                    "postgresql://schemabridge_api:password@control.example.test/control"
                ),
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": token,
                "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": token,
            }
        )

    with pytest.raises(ValidationError, match="only to the API"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": cursor_key,
            }
        )


def test_source_and_datahub_credentials_are_hidden_from_settings_repr() -> None:
    source_dsn = "postgresql://reader:source-secret@source.example/test"
    datahub_token = "sensitive-datahub-token"
    settings = Settings.model_validate(
        {
            "DATABASE_URL": source_dsn,
            "DATAHUB_GMS_TOKEN": datahub_token,
        }
    )

    assert source_dsn not in repr(settings)
    assert datahub_token not in repr(settings)


def test_local_api_token_is_secret_and_development_only() -> None:
    value = "local-api-bearer-token-with-enough-byte-diversity-123"
    settings = Settings.model_validate({"SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": value})

    assert settings.api_local_bearer_token is not None
    assert settings.api_local_bearer_token.get_secret_value() == value
    assert value not in repr(settings)

    with pytest.raises(ValidationError, match="only in development"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_ENVIRONMENT": "hosted-demo",
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": value,
            }
        )


@pytest.mark.parametrize(
    "value",
    (
        "short",
        "replace-with-a-real-local-api-bearer-token",
        "a" * 64,
    ),
)
def test_local_api_token_rejects_weak_or_placeholder_values(value: str) -> None:
    with pytest.raises(ValidationError, match="API_LOCAL_BEARER_TOKEN is not acceptable"):
        Settings.model_validate({"SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": value})


def test_worker_heartbeat_must_leave_reclaim_margin() -> None:
    with pytest.raises(ValidationError, match="less than half"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_WORKER_LEASE_SECONDS": 20,
                "SCHEMABRIDGE_WORKER_HEARTBEAT_SECONDS": 10,
            }
        )


def test_worker_lease_must_outlive_the_source_statement_timeout() -> None:
    with pytest.raises(ValidationError, match="exceed the maximum source statement timeout"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 60_000,
                "SCHEMABRIDGE_WORKER_LEASE_SECONDS": 60,
                "SCHEMABRIDGE_WORKER_HEARTBEAT_SECONDS": 20,
            }
        )


@pytest.mark.parametrize("worker_id", ("Worker-1", "worker.1", "worker:1"))
def test_worker_id_matches_the_closed_job_lease_identifier(worker_id: str) -> None:
    with pytest.raises(ValidationError, match="SCHEMABRIDGE_WORKER_ID"):
        Settings.model_validate({"SCHEMABRIDGE_WORKER_ID": worker_id})


@pytest.mark.parametrize(
    "hosts",
    (
        ("localhost", "localhost"),
        ("https://api.example.test",),
        ("api.example.test/path",),
        (" api.example.test",),
    ),
)
def test_api_allowed_hosts_are_exact_and_unique(hosts: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match=r"allowed hosts|API_ALLOWED_HOSTS"):
        Settings.model_validate({"SCHEMABRIDGE_API_ALLOWED_HOSTS": hosts})


def test_managed_profile_rejects_interactive_api_docs() -> None:
    payload = _managed_oidc_payload()
    payload["SCHEMABRIDGE_API_DOCS_ENABLED"] = True

    with pytest.raises(ValidationError, match="documentation is forbidden"):
        Settings.model_validate(payload)


def test_api_bind_host_must_be_an_explicit_ip_address() -> None:
    with pytest.raises(ValidationError, match="explicit IP"):
        Settings.model_validate({"SCHEMABRIDGE_API_BIND_HOST": "all-interfaces"})


def test_managed_api_jwks_must_be_https_and_same_issuer_origin() -> None:
    payload = _managed_oidc_payload()
    payload["SCHEMABRIDGE_API_OIDC_JWKS_URL"] = "http://identity.example.test/jwks"
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings.model_validate(payload)

    payload["SCHEMABRIDGE_API_OIDC_JWKS_URL"] = "https://keys.example.test/jwks"
    with pytest.raises(ValidationError, match="issuer origin"):
        Settings.model_validate(payload)

    payload["SCHEMABRIDGE_API_OIDC_JWKS_URL"] = "https://identity.example.test/jwks"
    settings = Settings.model_validate(payload)
    assert settings.api_oidc_jwks_url == "https://identity.example.test/jwks"


def test_api_and_worker_control_credentials_must_target_control_with_distinct_roles() -> None:
    payload = _managed_oidc_payload()
    payload.update(
        {
            "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                "postgresql://schemabridge_api:password@control.example.test/control"
                "?sslmode=verify-full"
            ),
            "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                "postgresql://schemabridge_worker:password@control.example.test/control"
                "?sslmode=verify-full"
            ),
        }
    )
    settings = Settings.model_validate(payload)
    assert settings.control_api_database_url is not None
    assert settings.control_worker_database_url is not None

    payload["SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL"] = payload[
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL"
    ]
    with pytest.raises(ValidationError, match="distinct roles"):
        Settings.model_validate(payload)


def test_api_component_accepts_only_its_control_credential() -> None:
    token = "local-api-bearer-token-with-enough-byte-diversity-123"
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "api",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                "postgresql://schemabridge_api:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": token,
        }
    )

    assert settings.runtime_component == "api"
    assert settings.control_database_url is None
    assert settings.database_url is None
    assert settings.control_audit_signing_key is None

    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                "SCHEMABRIDGE_COMPONENT": "api",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                    "postgresql://schemabridge_api:password@control.example.test/control"
                ),
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": token,
                "DATABASE_URL": (
                    "postgresql://schemabridge_reader:password@source.example.test/source"
                ),
            }
        )
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                "SCHEMABRIDGE_COMPONENT": "api",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                    "postgresql://schemabridge_api:password@control.example.test/control"
                ),
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": token,
                "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-connectors"),
            }
        )


def test_worker_component_requires_only_worker_control_and_connector_secret_directory() -> None:
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "worker",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                "postgresql://schemabridge_worker:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": (
                "/run/secrets/schemabridge-worker-connectors"
            ),
        }
    )

    assert settings.runtime_component == "worker"
    assert settings.worker_identity_lineage_mode == "exact-local"
    assert settings.control_database_url is None
    assert settings.control_audit_signing_key is None
    assert settings.openai_api_key is None
    assert settings.database_url is None

    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                **settings.model_dump(by_alias=True),
                "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                    "worker-must-not-receive-this-signing-key-123"
                ),
            }
        )
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                **settings.model_dump(by_alias=True),
                "DATABASE_URL": (
                    "postgresql://schemabridge_reader:password@source.example.test/source"
                ),
            }
        )


def test_catalog_component_accepts_only_catalog_control_and_routed_secret_directory() -> None:
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "catalog",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": (
                "postgresql://schemabridge_catalog:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_CATALOG_MODE": "live",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": (
                "/run/secrets/schemabridge-catalog-connectors"
            ),
        }
    )

    assert settings.runtime_component == "catalog"
    assert settings.auth_mode == "local-demo"
    assert settings.control_catalog_database_url is not None
    assert settings.control_api_database_url is None
    assert settings.database_url is None
    assert settings.openai_api_key is None
    assert "schemabridge-catalog-connectors" not in repr(settings)

    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                **settings.model_dump(by_alias=True),
                "OPENAI_API_KEY": "must-not-enter-the-catalog-indexer",
            }
        )


def test_reconciler_component_accepts_only_reconciler_control_and_audit_credentials() -> None:
    audit_key = "semantic-reconciler-audit-key-with-diversity-123456"
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "reconciler",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
                "postgresql://schemabridge_reconciler:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": audit_key,
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
        }
    )

    assert settings.runtime_component == "reconciler"
    assert settings.control_reconciler_database_url is not None
    assert settings.control_api_database_url is None
    assert settings.database_url is None
    assert settings.openai_api_key is None
    assert audit_key not in repr(settings)

    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _component_settings(
            {
                **settings.model_dump(by_alias=True),
                "OPENAI_API_KEY": "must-not-enter-the-semantic-reconciler",
            }
        )


def test_reconciler_component_requires_strong_audit_key_and_no_oidc() -> None:
    base: dict[str, object] = {
        "SCHEMABRIDGE_COMPONENT": "reconciler",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
            "postgresql://schemabridge_reconciler:password@control.example.test/control"
        ),
        "SCHEMABRIDGE_REGISTRY_MODE": "live",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
        "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
    }
    with pytest.raises(ValidationError, match="CONTROL_AUDIT_SIGNING_KEY"):
        _component_settings(base)

    with pytest.raises(ValidationError, match="cannot receive OIDC"):
        _component_settings(
            {
                **base,
                "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                    "semantic-reconciler-audit-key-with-diversity-123456"
                ),
                "SCHEMABRIDGE_AUTH_MODE": "oidc",
            }
        )


def test_managed_catalog_forbids_deployment_wide_datahub_credentials() -> None:
    payload: dict[str, object] = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_COMPONENT": "catalog",
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": (
            "postgresql://schemabridge_catalog:password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-catalog-connectors"),
        "DATAHUB_GMS_TOKEN": "synthetic-datahub-reader-token",
    }

    with pytest.raises(ValidationError, match="deployment-wide DATAHUB_GMS_TOKEN"):
        _component_settings(payload)

    del payload["DATAHUB_GMS_TOKEN"]
    payload["DATAHUB_GMS_URL"] = "https://datahub.example.test"
    with pytest.raises(ValidationError, match="deployment-wide DATAHUB_GMS_URL"):
        _component_settings(payload)

    del payload["DATAHUB_GMS_URL"]
    payload["SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS"] = {
        "synthetic-managed": 10,
    }
    with pytest.raises(ValidationError, match="synthetic catalog source"):
        _component_settings(payload)

    del payload["SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS"]
    settings = _component_settings(payload)
    assert settings.runtime_component == "catalog"
    assert settings.auth_mode == "local-demo"

    del payload["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"]
    with pytest.raises(ValidationError, match="CONNECTOR_SECRET_DIRECTORY"):
        _component_settings(payload)


def test_dedicated_component_does_not_load_the_shared_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=must-not-enter-the-api-process\n"
        "DATABASE_URL=postgresql://reader:secret@source.example.test/source\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHEMABRIDGE_COMPONENT", "api")
    monkeypatch.setenv("SCHEMABRIDGE_CONTROL_PLANE_MODE", "postgres")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "postgresql://schemabridge_api:password@control.example.test/control",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "local-api-bearer-token-with-enough-byte-diversity-123",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = get_settings()

    assert settings.runtime_component == "api"
    assert settings.openai_api_key is None
    assert settings.database_url is None


def test_production_worker_needs_no_browser_identity_or_signing_secret() -> None:
    payload: dict[str, object] = {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_COMPONENT": "worker",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
            "postgresql://schemabridge_worker:password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-worker-connectors"),
        "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE": "verified-oidc",
    }

    connector_directory = payload.pop("SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY")
    with pytest.raises(ValidationError, match="CONNECTOR_SECRET_DIRECTORY"):
        _component_settings(payload)
    payload["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] = connector_directory

    settings = _component_settings(payload)

    assert settings.auth_mode == "local-demo"
    assert settings.worker_identity_lineage_mode == "verified-oidc"
    assert settings.pseudonymization_key is None
    assert settings.control_audit_signing_key is None

    payload.update(
        {
            "SCHEMABRIDGE_AUTH_MODE": "oidc",
            "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
            "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": (
                "worker-must-not-receive-pseudonymization-secret"
            ),
        }
    )
    with pytest.raises(ValidationError, match="worker component cannot receive OIDC"):
        _component_settings(payload)


@pytest.mark.parametrize("environment", ("staging", "production"))
def test_managed_worker_requires_verified_identity_lineage_mode(
    environment: str,
) -> None:
    payload: dict[str, object] = {
        "SCHEMABRIDGE_ENVIRONMENT": environment,
        "SCHEMABRIDGE_COMPONENT": "worker",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
            "postgresql://schemabridge_worker:password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-worker-connectors"),
    }

    with pytest.raises(
        ValidationError,
        match="WORKER_IDENTITY_LINEAGE_MODE=verified-oidc",
    ):
        _component_settings(payload)

    payload["SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE"] = "verified-oidc"
    settings = _component_settings(payload)
    assert settings.worker_identity_lineage_mode == "verified-oidc"
    assert settings.auth_mode == "local-demo"
    assert settings.oidc_issuer is None
    assert settings.pseudonymization_key is None


@pytest.mark.parametrize("mode", ("exact-local", "verified-oidc"))
def test_development_worker_allows_explicit_lineage_modes(mode: str) -> None:
    settings = _component_settings(
        {
            "SCHEMABRIDGE_COMPONENT": "worker",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                "postgresql://schemabridge_worker:password@control.example.test/control"
            ),
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
            "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE": mode,
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": (
                "/run/secrets/schemabridge-worker-connectors"
            ),
        }
    )

    assert settings.worker_identity_lineage_mode == mode
    assert settings.oidc_issuer is None
    assert settings.pseudonymization_key is None


def _managed_oidc_payload() -> dict[str, object]:
    return {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": {"analysts": ("analyst",)},
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": ("tenant-a",),
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("pseudonymization-key-for-api-config-test-12345"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "query-studio-signing-key-for-api-config-test-67890"
        ),
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-connectors"),
        "DATABASE_URL": (
            "postgresql://reader:password@source.example.test/source?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://runtime:password@control.example.test/control?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": ("audit-signing-key-for-api-config-test-12345"),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("identity-migration-key-for-api-config-test-123"),
    }


def _component_settings(payload: dict[str, object]) -> Settings:
    isolated = {
        "OPENAI_API_KEY": None,
        "DATAHUB_GMS_TOKEN": None,
        **payload,
    }
    return Settings(_env_file=None, **isolated)  # type: ignore[arg-type]
