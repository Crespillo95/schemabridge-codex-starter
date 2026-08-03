from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from schemabridge.config import Settings

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _oidc_payload(
    profile: str = "production",
    **overrides: object,
) -> dict[str, object]:
    connector_secrets: dict[str, object]
    if profile in {"staging", "production"}:
        connector_secrets = {
            "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
            "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": ("https://secrets.example.test"),
            "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE": "schemabridge-preflight",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE": "schemabridge-registry-reader",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF": ("registry.reader.primary"),
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION": 17,
            "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT": "tenant-connectors",
            "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY": "preflight",
            "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": (
                "/var/run/secrets/schemabridge/trust/ca.crt"
            ),
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": (
                "/var/run/secrets/schemabridge/identity/token"
            ),
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": ("/var/run/secrets/schemabridge/identity"),
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": ("schemabridge-secret-manager"),
        }
    else:
        connector_secrets = {
            "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": ("/run/secrets/schemabridge-connectors"),
        }
    payload: dict[str, object] = {
        "SCHEMABRIDGE_ENVIRONMENT": profile,
        "SCHEMABRIDGE_AUTH_MODE": "oidc",
        "SCHEMABRIDGE_OIDC_ISSUER": "https://identity.example.test",
        "SCHEMABRIDGE_OIDC_AUDIENCE": "schemabridge",
        "SCHEMABRIDGE_OIDC_PROVIDER": "corporate-oidc",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS": {
            "data-analysts": ("analyst",),
            "data-publishers": ("publisher", "auditor"),
        },
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS": ("tenant-a", "tenant-b"),
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("unit-test-pseudonymization-key-at-least-32-bytes"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "unit-test-query-studio-signing-key-with-diversity"
        ),
        **connector_secrets,
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://control_runtime:control_password@control.example.test/control"
            "?sslmode=verify-full"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
            "unit-test-control-audit-signing-key-with-diversity"
        ),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("unit-test-identity-migration-key-with-diversity"),
    }
    payload.update(overrides)
    return payload


def test_development_defaults_are_local_and_cannot_publish_live() -> None:
    settings = Settings.model_validate({})

    assert settings.runtime_profile == "development"
    assert settings.auth_mode == "local-demo"
    assert settings.catalog_mode == "recorded"
    assert settings.registry_mode == "recorded"
    assert settings.semantic_registry_selection == "fixed"
    assert settings.control_plane_kind == "local"
    assert settings.publication_mode == "fake"
    assert settings.execution_mode == "recorded"
    assert settings.local_workspace == "local-demo"
    assert settings.local_roles == ("analyst", "publisher")


def test_hosted_demo_resolves_all_integrations_to_safe_recorded_modes() -> None:
    settings = Settings.model_validate({"SCHEMABRIDGE_ENVIRONMENT": "hosted-demo"})

    assert settings.runtime_profile == "hosted-demo"
    assert settings.auth_mode == "local-demo"
    assert settings.catalog_kind == "recorded"
    assert settings.registry_kind == "recorded"
    assert settings.semantic_registry_selection == "fixed"
    assert settings.control_plane_kind == "local"
    assert settings.publication_kind == "fake"
    assert settings.judge_execution_kind == "recorded"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("SCHEMABRIDGE_AUTH_MODE", "oidc"),
        ("SCHEMABRIDGE_CATALOG_MODE", "live"),
        ("SCHEMABRIDGE_REGISTRY_MODE", "live"),
        ("SCHEMABRIDGE_PUBLICATION_MODE", "live"),
        ("SCHEMABRIDGE_JUDGE_EXECUTION", "live"),
    ),
)
def test_hosted_demo_rejects_explicit_mode_overrides(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="hosted-demo requires"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_ENVIRONMENT": "hosted-demo",
                field: value,
            }
        )


@pytest.mark.parametrize("profile", ("staging", "production"))
def test_managed_profiles_fail_closed_without_oidc(profile: str) -> None:
    with pytest.raises(ValidationError, match="requires SCHEMABRIDGE_AUTH_MODE=oidc"):
        Settings.model_validate({"SCHEMABRIDGE_ENVIRONMENT": profile})


@pytest.mark.parametrize(
    "missing_field",
    (
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_OIDC_AUDIENCE",
        "SCHEMABRIDGE_OIDC_PROVIDER",
    ),
)
def test_oidc_mode_requires_each_non_secret_metadata_field(missing_field: str) -> None:
    payload = _oidc_payload()
    del payload[missing_field]

    with pytest.raises(ValidationError, match=missing_field):
        Settings.model_validate(payload)


def test_oidc_mode_requires_an_explicit_external_group_allowlist() -> None:
    with pytest.raises(ValidationError, match="non-empty SCHEMABRIDGE_OIDC_ALLOWED_GROUPS"):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_OIDC_ALLOWED_GROUPS={}))


def test_oidc_mode_requires_an_explicit_tenant_allowlist() -> None:
    with pytest.raises(ValidationError, match="non-empty SCHEMABRIDGE_OIDC_ALLOWED_TENANTS"):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_OIDC_ALLOWED_TENANTS=()))


@pytest.mark.parametrize(
    "tenants",
    (
        (" tenant-a",),
        ("tenant-a ",),
        ("tenant-a", "tenant-a"),
        ("x" * 121,),
    ),
)
def test_oidc_tenant_allowlist_rejects_ambiguous_or_unbounded_entries(
    tenants: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match=r"OIDC_ALLOWED_TENANTS|allowlisted tenants"):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_OIDC_ALLOWED_TENANTS=tenants))


def test_oidc_tenant_allowlist_has_a_bounded_number_of_entries() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_OIDC_ALLOWED_TENANTS=tuple(f"tenant-{index}" for index in range(257))
            )
        )


def test_production_accepts_complete_oidc_metadata_and_server_modes() -> None:
    settings = Settings.model_validate(
        _oidc_payload(
            SCHEMABRIDGE_CATALOG_MODE="live",
            SCHEMABRIDGE_PUBLICATION_MODE="disabled",
            SCHEMABRIDGE_JUDGE_EXECUTION="disabled",
            SCHEMABRIDGE_OIDC_ROLE_CLAIM="groups",
            SCHEMABRIDGE_OIDC_TENANT_CLAIM="workspace_id",
        )
    )

    assert settings.runtime_profile == "production"
    assert settings.auth_mode == "oidc"
    assert settings.catalog_mode == "live"
    assert settings.registry_mode == "live"
    assert settings.semantic_registry_selection == "active"
    assert settings.control_plane_kind == "postgres"
    assert settings.publication_mode == "disabled"
    assert settings.execution_mode == "disabled"
    assert settings.oidc_role_claim == "groups"
    assert settings.oidc_tenant_claim == "workspace_id"
    assert settings.oidc_allowed_groups["data-publishers"] == ("publisher", "auditor")
    assert settings.oidc_allowed_tenants == ("tenant-a", "tenant-b")
    assert settings.pseudonymization_key is not None
    assert "unit-test-pseudonymization-key" not in repr(settings)
    assert "unit-test-control-audit" not in repr(settings)
    assert "unit-test-identity-migration" not in repr(settings)


@pytest.mark.parametrize("profile", ("staging", "production"))
def test_managed_profiles_require_active_postgres_control_plane(profile: str) -> None:
    with pytest.raises(
        ValidationError,
        match="SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION=active",
    ):
        Settings.model_validate(
            _oidc_payload(
                profile,
                SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION="fixed",
            )
        )
    with pytest.raises(ValidationError, match="SCHEMABRIDGE_CONTROL_PLANE_MODE=postgres"):
        Settings.model_validate(
            _oidc_payload(
                profile,
                SCHEMABRIDGE_CONTROL_PLANE_MODE="local",
            )
        )


def test_postgres_control_plane_requires_a_separate_database_and_keys() -> None:
    same_database = "postgresql://control:password@source.example.test/source?sslmode=verify-full"
    with pytest.raises(ValidationError, match="separate from every source database"):
        Settings.model_validate(
            _oidc_payload(
                DATABASE_URL=same_database,
                SCHEMABRIDGE_CONTROL_DATABASE_URL=same_database,
            )
        )

    for missing in (
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY",
    ):
        payload = _oidc_payload()
        del payload[missing]
        with pytest.raises(ValidationError, match=missing):
            Settings.model_validate(payload)


@pytest.mark.parametrize(
    ("source_host", "control_host"),
    (
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "::1"),
        ("LOCALHOST.", "127.0.0.2"),
    ),
)
def test_postgres_control_plane_rejects_loopback_aliases_for_the_same_database(
    source_host: str,
    control_host: str,
) -> None:
    with pytest.raises(ValidationError, match="separate from every source database"):
        Settings.model_validate(
            _oidc_payload(
                DATABASE_URL=(
                    f"postgresql://source_reader:password@{source_host}:5432/shared"
                    "?sslmode=verify-full"
                ),
                SCHEMABRIDGE_CONTROL_DATABASE_URL=(
                    "postgresql://control_runtime:password@"
                    f"[{control_host}]:5432/shared?sslmode=verify-full"
                    if ":" in control_host
                    else (
                        "postgresql://control_runtime:password@"
                        f"{control_host}:5432/shared?sslmode=verify-full"
                    )
                ),
            )
        )


@pytest.mark.parametrize(
    "field",
    (
        "DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
    ),
)
def test_managed_postgres_urls_require_verified_tls(field: str) -> None:
    payload = _oidc_payload()
    if field == "DATABASE_URL":
        payload[field] = (
            "postgresql://source_reader:source_password@source.example.test/source"
            "?sslmode=verify-full"
        )
    payload[field] = str(payload[field]).split("?", maxsplit=1)[0]

    with pytest.raises(ValidationError, match="verified TLS"):
        Settings.model_validate(payload)


def test_control_plane_secrets_are_independent_and_strong() -> None:
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_IDENTITY_MIGRATION_KEY=(
                    "unit-test-control-audit-signing-key-with-diversity"
                )
            )
        )
    with pytest.raises(ValidationError, match="not acceptable"):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY="x" * 32))


def test_control_operator_actor_and_roles_are_an_atomic_typed_configuration() -> None:
    actor = "sb_actor_v2_" + "a" * 64
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID=actor,
                SCHEMABRIDGE_CONTROL_OPERATOR_ROLES=("platform_admin",),
            )
        )

    with pytest.raises(ValidationError, match="configured together"):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID=actor))
    with pytest.raises(ValidationError, match="configured together"):
        Settings.model_validate(
            _oidc_payload(SCHEMABRIDGE_CONTROL_OPERATOR_ROLES=("platform_admin",))
        )
    with pytest.raises(ValidationError, match="cannot contain duplicates"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID=actor,
                SCHEMABRIDGE_CONTROL_OPERATOR_ROLES=("platform_admin", "platform_admin"),
            )
        )


def test_optional_operator_credentials_are_distinct_and_bound_to_control_database() -> None:
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL=(
                    "postgresql://control_reconciler:password@control.example.test/control"
                    "?sslmode=verify-full"
                ),
                SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
                    "postgresql://control_migrator:password@control.example.test/control"
                    "?sslmode=verify-full"
                ),
            )
        )
    with pytest.raises(ValidationError, match="must identify the configured control database"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
                    "postgresql://control_migrator:password@other.example.test/control"
                    "?sslmode=verify-full"
                )
            )
        )
    with pytest.raises(ValidationError, match="must use distinct roles"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
                    "postgresql://control_runtime:another-password@control.example.test/control"
                    "?sslmode=verify-full"
                )
            )
        )


def test_restore_credential_targets_a_distinct_verified_database() -> None:
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL=(
                    "postgresql://control_restore:password@restore.example.test/control_restore"
                    "?sslmode=verify-full"
                )
            )
        )
    with pytest.raises(ValidationError, match="distinct from the active control database"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL=(
                    "postgresql://control_restore:password@control.example.test/control"
                    "?sslmode=verify-full"
                )
            )
        )
    with pytest.raises(ValidationError, match="verified TLS"):
        Settings.model_validate(
            _oidc_payload(
                SCHEMABRIDGE_CONTROL_RESTORE_DATABASE_URL=(
                    "postgresql://control_restore:password@restore.example.test/control_restore"
                )
            )
        )


def test_postgres_control_plane_rejects_an_unmigrated_schema_name() -> None:
    with pytest.raises(ValidationError, match="schemabridge_control"):
        Settings.model_validate(
            _oidc_payload(SCHEMABRIDGE_CONTROL_PLANE_SCHEMA="another_control_schema")
        )


@pytest.mark.parametrize("profile", ("staging", "production"))
def test_managed_oidc_profiles_require_https_issuer(profile: str) -> None:
    with pytest.raises(ValidationError, match="OIDC issuers must use HTTPS"):
        Settings.model_validate(
            _oidc_payload(
                profile,
                SCHEMABRIDGE_OIDC_ISSUER="http://identity.example.test",
            )
        )


@pytest.mark.parametrize("profile", ("staging", "production"))
def test_managed_profiles_reject_recorded_semantic_registry(profile: str) -> None:
    with pytest.raises(ValidationError, match="SCHEMABRIDGE_REGISTRY_MODE=live"):
        Settings.model_validate(
            _oidc_payload(
                profile,
                SCHEMABRIDGE_REGISTRY_MODE="recorded",
            )
        )


@pytest.mark.parametrize("profile", ("staging", "production"))
def test_managed_profiles_reject_recorded_catalog(profile: str) -> None:
    with pytest.raises(ValidationError, match="SCHEMABRIDGE_CATALOG_MODE=live"):
        Settings.model_validate(
            _oidc_payload(
                profile,
                SCHEMABRIDGE_CATALOG_MODE="recorded",
            )
        )


def test_development_oidc_allows_local_provider_testing() -> None:
    settings = Settings.model_validate(
        _oidc_payload(
            "development",
            SCHEMABRIDGE_OIDC_ISSUER="http://localhost:9999",
        )
    )

    assert settings.auth_mode == "oidc"
    assert settings.oidc_issuer == "http://localhost:9999"


@pytest.mark.parametrize(
    "live_field",
    (
        "SCHEMABRIDGE_CATALOG_MODE",
        "SCHEMABRIDGE_REGISTRY_MODE",
        "SCHEMABRIDGE_JUDGE_EXECUTION",
    ),
)
def test_development_oidc_live_reads_require_explicit_local_opt_in(
    live_field: str,
) -> None:
    payload = _oidc_payload("development", **{live_field: "live"})

    with pytest.raises(ValidationError, match="SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS"):
        Settings.model_validate(payload)

    payload["SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS"] = True
    assert Settings.model_validate(payload).allow_local_live_reads is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("SCHEMABRIDGE_OIDC_PROVIDER", "corporate_oidc"),
        ("SCHEMABRIDGE_OIDC_ISSUER", "https://user:secret@identity.example.test"),
        ("SCHEMABRIDGE_OIDC_ISSUER", "https://identity.example.test?tenant=unsafe"),
    ),
)
def test_oidc_metadata_rejects_unsupported_or_credential_bearing_values(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(_oidc_payload(**{field: value}))


def test_live_publication_rejects_local_demo_identity_in_development() -> None:
    with pytest.raises(ValidationError, match="live publication requires"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_ENVIRONMENT": "development",
                "SCHEMABRIDGE_AUTH_MODE": "local-demo",
                "SCHEMABRIDGE_PUBLICATION_MODE": "live",
            }
        )


@pytest.mark.parametrize(
    ("catalog", "registry", "execution"),
    (
        ("live", "recorded", "recorded"),
        ("recorded", "live", "recorded"),
        ("recorded", "recorded", "live"),
        ("live", "live", "live"),
    ),
)
def test_local_live_reads_require_explicit_development_enablement(
    catalog: str,
    registry: str,
    execution: str,
) -> None:
    with pytest.raises(ValidationError, match="ALLOW_LOCAL_LIVE_READS"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_CATALOG_MODE": catalog,
                "SCHEMABRIDGE_REGISTRY_MODE": registry,
                "SCHEMABRIDGE_JUDGE_EXECUTION": execution,
            }
        )

    settings = Settings.model_validate(
        {
            "SCHEMABRIDGE_CATALOG_MODE": catalog,
            "SCHEMABRIDGE_REGISTRY_MODE": registry,
            "SCHEMABRIDGE_JUDGE_EXECUTION": execution,
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
        }
    )
    assert settings.catalog_mode == catalog
    assert settings.registry_mode == registry
    assert settings.execution_mode == execution


@pytest.mark.parametrize(
    "key",
    (
        None,
        "short",
        "replace-with-production-pseudonymization-key",
        "x" * 32,
    ),
)
def test_oidc_requires_a_strong_non_placeholder_pseudonymization_key(
    key: str | None,
) -> None:
    payload = _oidc_payload()
    if key is None:
        del payload["SCHEMABRIDGE_PSEUDONYMIZATION_KEY"]
    else:
        payload["SCHEMABRIDGE_PSEUDONYMIZATION_KEY"] = key
    with pytest.raises(ValidationError, match="PSEUDONYMIZATION_KEY"):
        Settings.model_validate(payload)


def test_rejected_low_diversity_key_is_not_rendered_in_validation_errors() -> None:
    weak_key = "Z" * 32

    with pytest.raises(ValidationError) as failure:
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_PSEUDONYMIZATION_KEY=weak_key))

    assert weak_key not in str(failure.value)
    assert weak_key not in repr(failure.value)
    assert "input_value" not in str(failure.value)


@pytest.mark.parametrize(
    "field_value",
    (
        {" untrimmed": ("analyst",)},
        {"empty": ()},
        {"duplicate": ("analyst", "analyst")},
    ),
)
def test_oidc_group_mapping_rejects_ambiguous_entries(
    field_value: dict[str, tuple[str, ...]],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(_oidc_payload(SCHEMABRIDGE_OIDC_ALLOWED_GROUPS=field_value))


def test_unknown_oidc_role_grants_nothing_and_fails_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            _oidc_payload(SCHEMABRIDGE_OIDC_ALLOWED_GROUPS={"untrusted-group": ("owner",)})
        )


def test_duplicate_local_roles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot contain duplicates"):
        Settings.model_validate({"SCHEMABRIDGE_LOCAL_ROLES": ("analyst", "analyst")})


def test_environment_json_decodes_role_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "production")
    monkeypatch.setenv("SCHEMABRIDGE_AUTH_MODE", "oidc")
    monkeypatch.setenv("SCHEMABRIDGE_PUBLICATION_MODE", "disabled")
    monkeypatch.setenv("SCHEMABRIDGE_JUDGE_EXECUTION", "disabled")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_AUDIENCE", "schemabridge")
    monkeypatch.setenv("SCHEMABRIDGE_OIDC_PROVIDER", "corporate-oidc")
    monkeypatch.setenv(
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
        json.dumps({"analytics": ["analyst"], "publishers": ["publisher"]}),
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS",
        json.dumps(["tenant-a", "tenant-b"]),
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "unit-test-pseudonymization-key-at-least-32-bytes",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY",
        "unit-test-query-studio-signing-key-with-diversity",
    )
    monkeypatch.setenv("SCHEMABRIDGE_CONNECTOR_SECRET_MODE", "remote")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL",
        "https://secrets.example.test",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE",
        "schemabridge-preflight",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE",
        "schemabridge-registry-reader",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF",
        "registry.reader.primary",
    )
    monkeypatch.setenv("SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION", "17")
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT",
        "tenant-connectors",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY",
        "preflight",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE",
        "/var/run/secrets/schemabridge/trust/ca.crt",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE",
        "/var/run/secrets/schemabridge/identity/token",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT",
        "/var/run/secrets/schemabridge/identity",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE",
        "schemabridge-secret-manager",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_DATABASE_URL",
        "postgresql://control_runtime:control_password@control.example.test/control"
        "?sslmode=verify-full",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "unit-test-control-audit-signing-key-with-diversity",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY",
        "unit-test-identity-migration-key-with-diversity",
    )
    monkeypatch.setenv(
        "SCHEMABRIDGE_LOCAL_ROLES",
        json.dumps(["analyst", "publisher"]),
    )

    settings = Settings(_env_file=None)

    assert settings.oidc_allowed_groups == {
        "analytics": ("analyst",),
        "publishers": ("publisher",),
    }
    assert settings.oidc_allowed_tenants == ("tenant-a", "tenant-b")
    assert settings.local_roles == ("analyst", "publisher")


def test_oidc_secrets_are_not_part_of_typed_application_settings() -> None:
    secret_field_names = {
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_access_token",
        "oidc_refresh_token",
        "oidc_id_token",
    }

    assert secret_field_names.isdisjoint(Settings.model_fields)


def test_unknown_runtime_profile_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"SCHEMABRIDGE_ENVIRONMENT": "preview"})


def test_checked_in_environment_example_is_a_valid_secret_free_development_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for field in Settings.model_fields.values():
        if isinstance(field.alias, str):
            monkeypatch.delenv(field.alias, raising=False)
    settings = Settings(_env_file=_REPOSITORY_ROOT / ".env.example")

    assert settings.runtime_profile == "development"
    assert settings.runtime_component == "web"
    assert settings.control_plane_schema_version == 14
    assert settings.connector_secret_mode == "local"
    assert settings.openai_api_key is None
    assert settings.datahub_gms_token is None
    assert settings.oidc_issuer is None


def test_settings_accept_field_names_for_composition_tests() -> None:
    payload: dict[str, Any] = {
        "environment": "development",
        "auth_mode": "local-demo",
        "catalog_kind": "recorded",
        "publication_kind": "fake",
        "judge_execution_kind": "recorded",
    }

    settings = Settings.model_validate(payload)

    assert settings.runtime_profile == "development"
    assert settings.execution_mode == "recorded"
