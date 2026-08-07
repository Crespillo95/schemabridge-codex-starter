from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.config import Settings, get_settings

_OBSERVER_DSN = "postgresql://schemabridge_observer:synthetic-password@control.example.test/control"


def _settings(payload: dict[str, object]) -> Settings:
    isolated = {
        "OPENAI_API_KEY": None,
        "DATAHUB_GMS_TOKEN": None,
        **payload,
    }
    return Settings(_env_file=None, **isolated)  # type: ignore[arg-type]


def test_observer_accepts_only_its_read_only_control_credential() -> None:
    settings = _settings(
        {
            "SCHEMABRIDGE_COMPONENT": "observer",
            "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": _OBSERVER_DSN,
        }
    )

    assert settings.runtime_component == "observer"
    assert settings.control_observer_database_url is not None
    assert settings.control_database_url is None
    assert settings.control_api_database_url is None
    assert settings.control_worker_database_url is None
    assert settings.control_catalog_database_url is None
    assert settings.control_reconciler_database_url is None
    assert settings.control_migrator_database_url is None
    assert settings.control_restore_database_url is None
    assert settings.database_url is None
    assert settings.openai_api_key is None
    assert settings.datahub_gms_token is None
    assert _OBSERVER_DSN not in repr(settings)
    assert settings.observer_bind_host == "127.0.0.1"
    assert settings.observer_port == 9464
    assert settings.observer_limit_concurrency == 20
    assert settings.observer_graceful_shutdown_seconds == 20
    assert settings.observer_snapshot_timeout_ms == 2_000
    assert settings.observer_max_metrics_response_bytes == 65_536


def test_observer_requires_postgres_and_its_dedicated_credential() -> None:
    with pytest.raises(ValidationError, match="requires PostgreSQL control plane"):
        _settings({"SCHEMABRIDGE_COMPONENT": "observer"})

    with pytest.raises(
        ValidationError,
        match="SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL",
    ):
        _settings(
            {
                "SCHEMABRIDGE_COMPONENT": "observer",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
            }
        )


@pytest.mark.parametrize("role", ("postgres", "schemabridge_migrator", "observer"))
def test_observer_rejects_every_non_observer_database_role(role: str) -> None:
    with pytest.raises(ValidationError, match="must use the schemabridge_observer role"):
        _settings(
            {
                "SCHEMABRIDGE_COMPONENT": "observer",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": (
                    f"postgresql://{role}:password@control.example.test/control"
                ),
            }
        )


def test_managed_observer_is_source_and_identity_secret_free() -> None:
    settings = _settings(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "production",
            "SCHEMABRIDGE_COMPONENT": "observer",
            "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": (f"{_OBSERVER_DSN}?sslmode=verify-full"),
        }
    )

    assert settings.auth_mode == "local-demo"
    assert settings.control_plane_kind == "postgres"
    assert settings.query_studio_ai_mode == "disabled"
    assert settings.connector_secret_mode == "local"
    assert settings.connector_secret_directory is None
    assert settings.workload_identity_token_file is None

    forbidden: tuple[tuple[str, object], ...] = (
        ("OPENAI_API_KEY", "must-not-enter-observer"),
        (
            "DATABASE_URL",
            "postgresql://reader:password@source.example.test/source?sslmode=verify-full",
        ),
        ("DATAHUB_GMS_TOKEN", "must-not-enter-observer"),
        (
            "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
            "postgresql://schemabridge_api:password@control.example.test/control"
            "?sslmode=verify-full",
        ),
        ("SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY", "/run/secrets/connectors"),
    )
    base = settings.model_dump(by_alias=True)
    for name, value in forbidden:
        with pytest.raises(ValidationError, match="forbidden cross-component credential"):
            _settings({**base, name: value})


def test_observer_rejects_oidc_and_remote_connector_identity() -> None:
    base: dict[str, object] = {
        "SCHEMABRIDGE_COMPONENT": "observer",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": _OBSERVER_DSN,
    }
    with pytest.raises(ValidationError, match="cannot receive OIDC"):
        _settings({**base, "SCHEMABRIDGE_AUTH_MODE": "oidc"})

    with pytest.raises(ValidationError, match="forbidden for this component"):
        _settings(
            {
                **base,
                "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
                "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": ("https://secrets.example.test"),
            }
        )


def test_observer_bind_host_must_be_an_explicit_ip_address() -> None:
    with pytest.raises(ValidationError, match="observer bind host"):
        _settings(
            {
                "SCHEMABRIDGE_COMPONENT": "observer",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": _OBSERVER_DSN,
                "SCHEMABRIDGE_OBSERVER_BIND_HOST": "all-interfaces",
            }
        )


def test_other_dedicated_components_reject_observer_credential() -> None:
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        _settings(
            {
                "SCHEMABRIDGE_COMPONENT": "api",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": (
                    "postgresql://schemabridge_api:password@control.example.test/control"
                ),
                "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": _OBSERVER_DSN,
                "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": (
                    "local-api-bearer-token-with-enough-byte-diversity-123"
                ),
            }
        )


def test_observer_does_not_load_shared_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=must-not-enter-observer\n"
        "DATABASE_URL=postgresql://reader:secret@source.example.test/source\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCHEMABRIDGE_COMPONENT", "observer")
    monkeypatch.setenv("SCHEMABRIDGE_CONTROL_PLANE_MODE", "postgres")
    monkeypatch.setenv("SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL", _OBSERVER_DSN)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = get_settings()

    assert settings.runtime_component == "observer"
    assert settings.openai_api_key is None
    assert settings.database_url is None
