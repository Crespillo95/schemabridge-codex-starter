from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemabridge.config import Settings

_BACKUP_DSN = (
    "postgresql://schemabridge_backup:synthetic-backup-password@control.example.test/control"
)
_AUDIT_KEY = "unit-test-control-audit-signing-key-with-diversity"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "OPENAI_API_KEY": None,
        "SCHEMABRIDGE_COMPONENT": "backup",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL": _BACKUP_DSN,
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": _AUDIT_KEY,
    }
    payload.update(overrides)
    return payload


def test_backup_component_accepts_only_its_dedicated_read_identity() -> None:
    settings = Settings.model_validate(_payload())

    assert settings.runtime_component == "backup"
    assert settings.control_backup_database_url is not None
    assert _BACKUP_DSN not in repr(settings)


def test_backup_component_rejects_a_migrator_identity() -> None:
    with pytest.raises(ValidationError, match="schemabridge_backup role"):
        Settings.model_validate(
            _payload(
                SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL=(
                    "postgresql://schemabridge_migrator:synthetic-password"
                    "@control.example.test/control"
                )
            )
        )


def test_backup_component_rejects_cross_component_credentials() -> None:
    with pytest.raises(ValidationError, match="forbidden cross-component credential"):
        Settings.model_validate(
            _payload(
                SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL=(
                    "postgresql://schemabridge_migrator:synthetic-password"
                    "@control.example.test/control"
                )
            )
        )


def test_backup_component_requires_its_audit_key() -> None:
    with pytest.raises(ValidationError, match="SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY"):
        Settings.model_validate(_payload(SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY=None))


def test_production_backup_is_non_interactive_and_requires_verified_tls() -> None:
    settings = Settings.model_validate(
        _payload(
            SCHEMABRIDGE_ENVIRONMENT="production",
            SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL=(
                f"{_BACKUP_DSN}?sslmode=verify-full"
                "&sslrootcert=/var/run/secrets/schemabridge/trust/ca.crt"
            ),
        )
    )

    assert settings.auth_mode == "local-demo"
    assert settings.oidc_issuer is None

    with pytest.raises(ValidationError, match="verified TLS"):
        Settings.model_validate(
            _payload(
                SCHEMABRIDGE_ENVIRONMENT="production",
            )
        )
