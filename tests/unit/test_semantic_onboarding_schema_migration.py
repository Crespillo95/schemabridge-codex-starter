"""Static security contract for control-plane migration 0013."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "migrations/control_plane/0013_semantic_onboarding.sql"


def test_m33_migration_has_bounded_tenant_scoped_state() -> None:
    payload = _MIGRATION.read_text(encoding="utf-8")

    for table in (
        "semantic_onboarding_drafts",
        "semantic_onboarding_decisions",
        "semantic_onboarding_proposals",
        "semantic_onboarding_audit",
        "semantic_onboarding_operations",
    ):
        assert f"CREATE TABLE schemabridge_control.{table}" in payload
        assert f"schemabridge_control.{table}" in payload.split("REVOKE ALL ON", 1)[1]
    assert payload.count("PRIMARY KEY (workspace_id") == 5
    assert "octet_length(payload::text) <= 2097152" in payload
    assert "octet_length(payload::text) <= 65536" in payload
    assert "response_revision bigint NOT NULL" in payload
    assert "response_fingerprint char(64) NOT NULL" in payload
    assert "response_draft IS NULL" in payload
    assert "operation = 'record_decision'" in payload
    assert "semantic_onboarding_operations_create_draft_idx" in payload
    assert "UNIQUE (workspace_id, draft_id, target_kind, target_id)" in payload
    assert "octet_length(response_proposal::text) <= 2097152" in payload
    assert "external_writes_performed' = 'false'" in payload
    assert "status IN ('ready_for_publication', 'superseded')" in payload


def test_m33_decisions_proposals_audit_and_replays_are_immutable() -> None:
    payload = _MIGRATION.read_text(encoding="utf-8")

    assert "reject_semantic_onboarding_immutable_mutation" in payload
    for trigger in (
        "semantic_onboarding_decisions_immutable",
        "semantic_onboarding_proposals_immutable",
        "semantic_onboarding_audit_immutable",
        "semantic_onboarding_operations_immutable",
    ):
        assert f"CREATE TRIGGER {trigger}" in payload
    assert "BEFORE UPDATE OR DELETE" in payload
    assert "GRANT DELETE" not in payload
    assert "GRANT UPDATE ON" not in payload


def test_m33_api_grants_are_exact_and_do_not_include_source_or_writer_roles() -> None:
    payload = _MIGRATION.read_text(encoding="utf-8")
    grants = payload.split("REVOKE EXECUTE ON FUNCTION", 1)[1]

    assert "GRANT SELECT, INSERT ON" in grants
    assert "TO schemabridge_api" in grants
    assert (
        "GRANT SELECT ON schemabridge_control.registry_active_pointers\n    TO schemabridge_api;"
    ) in grants
    assert "registry_activation_transitions" not in grants
    assert "registry_reconciliation_outbox" not in grants
    assert "GRANT UPDATE (" in grants
    assert "prepared_proposal_fingerprint" in grants
    assert "TO schemabridge_worker" not in grants
    assert "TO schemabridge_runtime" not in grants
    assert "TO schemabridge_catalog" not in grants
    assert "DataHub" not in payload
    assert "DATABASE_URL" not in payload
