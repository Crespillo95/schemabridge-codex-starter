"""Static least-privilege contract for the additive M35 control-plane migration."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0015_registry_v2_changes.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_m35_uses_generic_profile_sources_without_faking_m26_scans() -> None:
    sql = _sql()

    assert "CREATE TABLE schemabridge_control.semantic_profile_sources" in sql
    assert "semantic_change_scan_v1" in sql
    assert "registry_join_profile_v1" in sql
    assert "registry_join_profile_requests" in sql
    assert "registry_join_profile_request_fk" in sql
    assert "DROP CONSTRAINT semantic_join_profile_job_scan_fk" in sql
    assert "REFERENCES schemabridge_control.semantic_profile_sources" in sql
    assert "INSERT INTO schemabridge_control.semantic_profile_sources" in sql
    assert "FROM schemabridge_control.semantic_change_scan_requests" in sql


def test_m35_generalizes_publication_sources_without_rewriting_m33_payloads() -> None:
    sql = _sql()

    assert "CREATE TABLE schemabridge_control.registry_publication_sources" in sql
    assert "onboarding_additive_v1" in sql
    assert "add_join_v1" in sql
    assert "replace_model_v1" in sql
    assert "FROM schemabridge_control.semantic_onboarding_proposals" in sql
    assert "DROP CONSTRAINT registry_publication_proposal_fk" in sql
    assert "REFERENCES schemabridge_control.registry_publication_sources" in sql
    assert "proposal_kind" in sql
    assert "coalesce(payload#>>'{proposal,proposal_kind}', 'onboarding_additive_v1')" in sql
    assert "CREATE TRIGGER semantic_registry_model_change_publication_source" in sql
    assert "ON schemabridge_control.semantic_registry_model_change_proposals" in sql
    assert "RENAME TO enforce_registry_publication_job_transition" in sql
    assert "CREATE TRIGGER registry_publication_jobs_lifecycle\nBEFORE INSERT" in sql
    assert "CREATE TRIGGER registry_publication_jobs_transition\nBEFORE UPDATE OR DELETE" in sql
    insert_guard = sql.split(
        "CREATE FUNCTION schemabridge_control.enforce_registry_publication_job_lifecycle()",
        1,
    )[1].split("CREATE TRIGGER registry_publication_jobs_lifecycle", 1)[0]
    assert "semantic_onboarding_proposals" in insert_guard
    assert "semantic_registry_change_proposals" in insert_guard
    assert "semantic_registry_model_change_proposals" in insert_guard
    assert "proposal_record.payload IS DISTINCT FROM NEW.payload->'proposal'" in insert_guard


def test_m35_authoring_history_is_tenant_bound_and_bounded() -> None:
    sql = _sql()

    for table in (
        "semantic_registry_change_drafts",
        "semantic_registry_change_decisions",
        "semantic_registry_change_proposals",
        "semantic_registry_change_audit",
        "semantic_registry_change_operations",
    ):
        assert f"CREATE TABLE schemabridge_control.{table}" in sql
        assert f"schemabridge_control.{table}" in sql
    assert "semantic registry change history is append-only" in sql
    assert "guard_semantic_registry_change_operation" in sql
    assert "new semantic registry change operation cannot pre-bind a job" in sql
    assert "semantic registry change operation witness is invalid" in sql
    assert "request.payload = NEW.response_authoring" in sql
    assert "draft.payload = NEW.response_draft" in sql
    assert "proposal.payload = NEW.response_proposal" in sql
    assert "OLD.response_job_id IS NOT NULL" in sql
    assert "OLD.response_job IS NOT NULL" in sql
    assert "octet_length(payload::text) <= 524288" in sql
    assert "PRIMARY KEY (workspace_id, idempotency_digest)" in sql
    assert "READY_FOR_PUBLICATION" not in sql
    assert "ready_for_publication" in sql


def test_m35_profile_job_binding_is_crash_safe_and_blocks_early_claims() -> None:
    sql = _sql()

    assert "response_job jsonb" in sql
    assert "CHECK ((response_job_id IS NULL) = (response_job IS NULL))" in sql
    assert "audit.event = 'profile_job_bound'" in sql
    assert "operation.response_job_id = NEW.job_id" in sql
    assert "registry join profile job is not durably bound" in sql
    assert "operation.response_authoring->>'fingerprint'" in sql
    assert "operation.request_fingerprint = request.request_fingerprint" not in sql
    assert "GRANT UPDATE (response_job_id, response_job)" in sql
    assert "schemabridge_control.semantic_json_has_protected_keys(jsonb)" in sql
    join_enqueue = sql.split(
        "CREATE FUNCTION schemabridge_control.enqueue_registry_join_profile(", 1
    )[1].split("CREATE FUNCTION schemabridge_control.load_registry_join_profile_job(", 1)[0]
    model_enqueue = sql.split(
        "CREATE FUNCTION schemabridge_control.enqueue_registry_model_join_profile(", 1
    )[1].split("CREATE FUNCTION schemabridge_control.load_registry_model_join_profile_job(", 1)[0]
    assert join_enqueue.count("request_record.requested_at") == 3
    assert model_enqueue.count("request_record.requested_at") == 3


def test_m35_activation_handoff_uses_a_typed_witness() -> None:
    sql = _sql()

    assert (
        "CREATE OR REPLACE FUNCTION schemabridge_control.load_registry_activation_ready_handoff"
        in sql
    )
    assert "publication.proposal_kind = 'onboarding_additive_v1'" in sql
    assert "publication.proposal_kind = 'add_join_v1'" in sql
    assert "publication.proposal_kind = 'replace_model_v1'" in sql
    assert "registry_model_publication_witness_valid" in sql
    assert "{proposal,replacement,model,definition,fields}" in sql
    assert "{proposal,incident_join_changes}" in sql
    assert "{proposal,decision,id}" in sql
    assert "{proposal,contract,approval_decision_id}" in sql
    assert "{candidate,registry,join_contracts,contracts}" in sql
    assert "{candidate,registry,logical_context,joins}" in sql
    assert "join_contracts" in sql


def test_m35_role_grants_preserve_writer_and_source_separation() -> None:
    sql = _sql()

    assert "GRANT EXECUTE ON FUNCTION schemabridge_control.enqueue_registry_join_profile" in sql
    assert "TO schemabridge_api" in sql
    assert "GRANT SELECT ON" in sql
    assert "TO schemabridge_api, schemabridge_publisher" in sql
    assert "GRANT UPDATE ON schemabridge_control.registry_active_pointers" not in sql
    assert (
        "GRANT INSERT ON schemabridge_control.semantic_join_profile_jobs\n    TO schemabridge_api"
        not in sql
    )
    assert "GRANT UPDATE ON schemabridge_control.semantic_registry_change_proposals" not in sql
    assert "schemabridge_control.registry_model_profile_job_claimable(" in sql
    assert "schemabridge_control.load_semantic_dependency_index_state(" in sql
    assert "TO schemabridge_api, schemabridge_publisher, schemabridge_reconciler" in sql
    assert "schemabridge_control.registry_model_profile_proposal_authorized(jsonb)," in sql
    assert "GRANT SELECT ON schemabridge_control.semantic_dependency_index_states" not in sql
    assert "FROM PUBLIC" in sql
