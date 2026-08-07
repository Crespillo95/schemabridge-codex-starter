from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import scripts.check_datahub_catalog as catalog_check
import scripts.provision_datahub_mcp as provision
import scripts.provision_datahub_writer as writer_provision
import yaml
from scripts.ensure_datahub_secrets import REQUIRED_KEYS, ensure_secrets
from scripts.prepare_datahub_quickstart import QuickstartPreparationError, prepare_compose
from scripts.sanitize_datahub_log import redact

ROOT = Path(__file__).parents[2]
EXPECTED_SYNTHETIC_ROW_COUNTS = {
    "bank.account_holders": 9,
    "bank.accounts": 9,
    "commerce.products": 40,
    "crm.customers": 7,
    "fulfillment.shipments": 75,
    "legacy.client_master": 7,
    "legacy.item_master": 42,
    "reporting.customer_accounts": 6,
    "sales.order_lines": 180,
    "sales.orders": 60,
    "support.order_cases": 30,
}
EXPECTED_SYNTHETIC_SCHEMAS = [
    "crm",
    "legacy",
    "bank",
    "reporting",
    "commerce",
    "sales",
    "fulfillment",
    "support",
]


def _dataset_name(urn: str) -> str:
    prefix = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge."
    assert urn.startswith(prefix)
    assert urn.endswith(",PROD)")
    return urn.removeprefix(prefix).removesuffix(",PROD)")


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
    assert source["schema_pattern"]["allow"] == EXPECTED_SYNTHETIC_SCHEMAS
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
    datasets = {_dataset_name(item["urn"]): item for item in fixture["datasets"]}

    assert fixture["fixture_kind"] == "sanitized_mcp_expectation"
    assert "customers" in fixture["dataset_urn"]
    assert {name: item["row_count"] for name, item in datasets.items()} == (
        EXPECTED_SYNTHETIC_ROW_COUNTS
    )
    assert sum(item["row_count"] for item in datasets.values()) == 465
    assert all(item["fields"] for item in datasets.values())
    assert all(set(item["described_fields"]) <= set(item["fields"]) for item in datasets.values())
    assert "token" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized


def test_catalog_check_loads_exact_full_synthetic_expectations() -> None:
    expected = catalog_check._load_expectations(ROOT / "tests/fixtures/datahub/mcp_catalog.json")

    assert len(expected) == 11
    assert {_dataset_name(item["urn"]) for item in expected} == set(EXPECTED_SYNTHETIC_ROW_COUNTS)


def test_catalog_check_rejects_malformed_or_incomplete_expectations(tmp_path: Path) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/datahub/mcp_catalog.json").read_text())
    malformed_path = tmp_path / "malformed.json"
    fixture["datasets"][0]["described_fields"].append("unknown_field")
    malformed_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(SystemExit, match="invalid dataset contract"):
        catalog_check._load_expectations(malformed_path)

    fixture = json.loads((ROOT / "tests/fixtures/datahub/mcp_catalog.json").read_text())
    incomplete_path = tmp_path / "incomplete.json"
    incomplete_path.write_text(
        json.dumps({"fixture_kind": fixture["fixture_kind"], "datasets": []})
    )

    with pytest.raises(SystemExit, match="exact synthetic corpus"):
        catalog_check._load_expectations(incomplete_path)


def test_recorded_catalog_snapshot_covers_the_same_eleven_datasets() -> None:
    snapshot = json.loads((ROOT / "demo/datahub/catalog_snapshot.json").read_text())
    assets = {item["dataset"]: item for item in snapshot["assets"]}
    fixture = json.loads((ROOT / "tests/fixtures/datahub/mcp_catalog.json").read_text())
    expected_fields = {
        _dataset_name(item["urn"]): set(item["fields"]) for item in fixture["datasets"]
    }

    assert snapshot["fixture_kind"] == "sanitized_catalog_recording"
    assert set(assets) == set(EXPECTED_SYNTHETIC_ROW_COUNTS)
    assert all(asset["description"] for asset in assets.values())
    assert {
        name: {field["field_path"] for field in asset["fields"]} for name, asset in assets.items()
    } == expected_fields
    described_fields = {
        _dataset_name(item["urn"]): set(item["described_fields"]) for item in fixture["datasets"]
    }
    assert all(
        field["description"]
        for name, asset in assets.items()
        for field in asset["fields"]
        if field["field_path"] in described_fields[name]
    )
    native_types = {
        f"{name}.{field['field_path']}": field["native_type"]
        for name, asset in assets.items()
        for field in asset["fields"]
    }
    assert native_types["commerce.products.product_code"] == "VARCHAR(8)"
    assert native_types["legacy.item_master.item_no"] == "BIGINT"
    assert native_types["sales.order_lines.order_ref"] == "VARCHAR(24)"
    assert native_types["fulfillment.shipments.order_ref"] == "VARCHAR(24)"
    assert native_types["support.order_cases.order_id"] == "VARCHAR(24)"


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
