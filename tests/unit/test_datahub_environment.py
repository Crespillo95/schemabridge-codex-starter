from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import scripts.provision_datahub_mcp as provision
import scripts.provision_datahub_writer as writer_provision
import yaml
from scripts.ensure_datahub_secrets import REQUIRED_KEYS, ensure_secrets
from scripts.prepare_datahub_quickstart import QuickstartPreparationError, prepare_compose
from scripts.sanitize_datahub_log import redact

ROOT = Path(__file__).parents[2]


def _compose_source() -> bytes:
    return b"""services:
  datahub-gms-quickstart:
    environment:
      EXISTING: unchanged
    ports:
      - target: 8080
        published: 8080
  datahub-frontend-react:
    ports:
      - target: 9002
        published: 9002
"""


def test_quickstart_preparation_checks_hash_enables_features_and_binds_loopback() -> None:
    source = _compose_source()
    rendered = prepare_compose(source, expected_sha256=hashlib.sha256(source).hexdigest())
    payload = yaml.safe_load(rendered)

    gms = payload["services"]["datahub-gms-quickstart"]
    assert gms["environment"]["LOGICAL_MODELS_ENABLED"] == "true"
    assert gms["environment"]["METADATA_SERVICE_AUTH_ENABLED"] == "true"
    for service in payload["services"].values():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1"


def test_quickstart_preparation_rejects_unreviewed_bytes() -> None:
    with pytest.raises(QuickstartPreparationError, match="checksum mismatch"):
        prepare_compose(_compose_source(), expected_sha256="0" * 64)


def test_quickstart_signing_material_is_local_persistent_and_private(tmp_path: Path) -> None:
    path = tmp_path / "quickstart.env"

    assert ensure_secrets(path) is True
    original = path.read_text()
    assert ensure_secrets(path) is False

    assert path.read_text() == original
    assert set(line.partition("=")[0] for line in original.splitlines()) == set(REQUIRED_KEYS)
    assert path.stat().st_mode & 0o777 == 0o600


def test_ingestion_recipe_uses_exact_synthetic_schemas_and_environment_secrets() -> None:
    recipe = yaml.safe_load((ROOT / "infra/datahub/ingestion/postgres.yml").read_text())
    source = recipe["source"]["config"]

    assert source["host_port"] == "127.0.0.1:55433"
    assert source["username"] == "schemabridge_reader"
    assert source["password"] == "${POSTGRES_READER_PASSWORD}"
    assert source["schema_pattern"]["allow"] == ["crm", "legacy", "bank", "reporting"]
    assert source["profiling"]["enabled"] is True
    assert recipe["sink"]["config"] == {
        "server": "${DATAHUB_GMS_URL}",
        "token": "${DATAHUB_GMS_TOKEN}",
    }


def test_pins_and_codex_mcp_configuration_are_read_only() -> None:
    versions = (ROOT / "infra/datahub/versions.env").read_text()
    codex = (ROOT / ".codex/config.toml").read_text()
    wrapper = (ROOT / "scripts/datahub-mcp.sh").read_text()

    assert "DATAHUB_CLI_VERSION=1.6.0.15" in versions
    assert "DATAHUB_CORE_VERSION=v1.6.0" in versions
    assert "DATAHUB_MCP_VERSION=0.6.0" in versions
    assert "latest" not in versions.lower()
    assert "enabled = true" in codex
    assert 'args = ["scripts/datahub-mcp.sh"]' in codex
    assert "TOOLS_IS_MUTATION_ENABLED=false" in wrapper
    assert "SAVE_DOCUMENT_TOOL_ENABLED=false" in wrapper
    assert "DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED=true" in wrapper


def test_datahub_admin_initialization_retries_fresh_gms_graphql_readiness() -> None:
    wrapper = (ROOT / "scripts/datahub.sh").read_text()

    assert "SCHEMABRIDGE_DATAHUB_INIT_ATTEMPTS=31" in wrapper
    assert "SCHEMABRIDGE_DATAHUB_INIT_DELAY_SECONDS=5" in wrapper
    assert "DataHub admin initialization did not become ready." in wrapper
    assert "if datahub_cli init" in wrapper


def test_sanitized_fixture_contains_no_credentials() -> None:
    fixture = json.loads((ROOT / "tests/fixtures/datahub/mcp_catalog.json").read_text())
    serialized = json.dumps(fixture).lower()

    assert fixture["fixture_kind"] == "sanitized_mcp_expectation"
    assert "customers" in fixture["dataset_urn"]
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_datahub_log_redaction_removes_full_and_partially_masked_tokens() -> None:
    token = "secret-token-value"
    text = f"Authorization: {token}\nconfigured with token: secr**********alue\n"

    result = redact(text, token)

    assert token not in result
    assert "secr" not in result
    assert "alue" not in result
    assert result.count("***REDACTED***") == 2


def test_mcp_identity_check_accepts_matching_actor_without_write_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_urn = "urn:li:corpuser:service_demo"
    response = {
        "me": {
            "corpUser": {"urn": actor_urn},
            "platformPrivileges": {"managePolicies": False, "manageTokens": False},
        }
    }
    monkeypatch.setattr(provision, "_graphql", lambda *_args: response)

    provision._verify_read_only_identity("not-a-real-token", actor_urn)


def test_mcp_identity_check_rejects_write_privilege(monkeypatch: pytest.MonkeyPatch) -> None:
    actor_urn = "urn:li:corpuser:service_demo"
    response = {
        "me": {
            "corpUser": {"urn": actor_urn},
            "platformPrivileges": {"managePolicies": True, "manageTokens": False},
        }
    }
    monkeypatch.setattr(provision, "_graphql", lambda *_args: response)

    with pytest.raises(SystemExit, match="unexpected platform privileges"):
        provision._verify_read_only_identity("not-a-real-token", actor_urn)


def test_writer_identity_waits_for_fresh_policy_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_urn = "urn:li:corpuser:schemabridge_writer"
    responses = iter(
        (
            {
                "me": {
                    "corpUser": {"urn": actor_urn},
                    "platformPrivileges": {"generatePersonalAccessTokens": True},
                }
            },
            {
                "me": {
                    "corpUser": {"urn": actor_urn},
                    "platformPrivileges": {
                        "generatePersonalAccessTokens": True,
                        "manageGlossaries": True,
                        "manageDocuments": True,
                        "manageStructuredProperties": True,
                        "viewStructuredPropertiesPage": True,
                    },
                }
            },
        )
    )
    sleeps: list[float] = []
    monkeypatch.setattr(writer_provision, "_graphql", lambda *_args: next(responses))
    monkeypatch.setattr(writer_provision.time, "sleep", sleeps.append)

    writer_provision._verify_identity("not-a-real-token", actor_urn, attempts=2, delay_seconds=0.25)

    assert sleeps == [0.25]


def test_writer_identity_fails_immediately_on_unexpected_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_urn = "urn:li:corpuser:schemabridge_writer"
    response = {
        "me": {
            "corpUser": {"urn": actor_urn},
            "platformPrivileges": {
                "generatePersonalAccessTokens": True,
                "managePolicies": True,
            },
        }
    }
    monkeypatch.setattr(writer_provision, "_graphql", lambda *_args: response)
    monkeypatch.setattr(
        writer_provision.time,
        "sleep",
        lambda _delay: pytest.fail("unexpected privileges must not be retried"),
    )

    with pytest.raises(SystemExit, match="unexpected platform privileges"):
        writer_provision._verify_identity(
            "not-a-real-token", actor_urn, attempts=2, delay_seconds=0.25
        )


def test_writer_identity_fails_closed_when_policy_never_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_urn = "urn:li:corpuser:schemabridge_writer"
    response = {
        "me": {
            "corpUser": {"urn": actor_urn},
            "platformPrivileges": {"generatePersonalAccessTokens": True},
        }
    }
    sleeps: list[float] = []
    monkeypatch.setattr(writer_provision, "_graphql", lambda *_args: response)
    monkeypatch.setattr(writer_provision.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit, match="did not converge"):
        writer_provision._verify_identity(
            "not-a-real-token", actor_urn, attempts=2, delay_seconds=0.25
        )

    assert sleeps == [0.25]
