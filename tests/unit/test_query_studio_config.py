from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemabridge.config import Settings


def _live_payload() -> dict[str, object]:
    return {
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "live",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL": "gpt-5-nano-2025-08-07",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_REGION": "global",
        "OPENAI_API_KEY": "synthetic-test-key-never-used",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("query-studio-pseudonym-key-with-diversity-123"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "query-studio-preview-signing-key-with-diversity-456"
        ),
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
            "postgresql://runtime:password@control.example.test/control"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": ("control-audit-signing-key-with-diversity-789"),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": ("identity-migration-key-with-diversity-012"),
    }


def test_query_studio_defaults_to_key_free_fake_and_cheapest_nano_candidate() -> None:
    settings = Settings(_env_file=None)

    assert settings.query_studio_ai_mode == "fake"
    assert settings.query_studio_ai_model == "gpt-5-nano-2025-08-07"
    assert settings.query_studio_ai_region == "global"
    assert settings.query_studio_signing_key is None


def test_live_query_studio_requires_durable_control_and_distinct_strong_keys() -> None:
    payload = _live_payload()
    settings = Settings.model_validate(payload)

    assert settings.query_studio_ai_mode == "live"
    assert settings.openai_api_key is not None
    assert "synthetic-test-key" not in repr(settings)
    assert "preview-signing-key" not in repr(settings)

    without_control = {**payload, "SCHEMABRIDGE_CONTROL_PLANE_MODE": "local"}
    with pytest.raises(ValidationError, match="durable PostgreSQL"):
        Settings.model_validate(without_control)

    same_key = payload["SCHEMABRIDGE_PSEUDONYMIZATION_KEY"]
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings.model_validate(
            {
                **payload,
                "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": same_key,
            }
        )


def test_query_studio_rejects_unreviewed_model_and_non_web_live_capability() -> None:
    with pytest.raises(ValidationError, match="evaluated pinned snapshot"):
        Settings.model_validate({"SCHEMABRIDGE_QUERY_STUDIO_AI_MODEL": "caller-model"})

    with pytest.raises(ValidationError, match="only to the web runtime"):
        Settings.model_validate(
            {
                **_live_payload(),
                "SCHEMABRIDGE_COMPONENT": "worker",
            }
        )

    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                **_live_payload(),
                "SCHEMABRIDGE_QUERY_STUDIO_AI_REGION": ("https://attacker.invalid/v1"),
            }
        )


def test_hosted_demo_forces_the_visible_key_free_fake_mode() -> None:
    settings = Settings.model_validate({"SCHEMABRIDGE_ENVIRONMENT": "hosted-demo"})

    assert settings.query_studio_ai_mode == "fake"

    with pytest.raises(ValidationError, match="hosted-demo requires"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_ENVIRONMENT": "hosted-demo",
                "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "live",
            }
        )
