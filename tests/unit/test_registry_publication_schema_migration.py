"""Static least-privilege contract for control-plane migration 0014."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0014_registry_publication.sql"
ROLES = ROOT / "demo/control_plane/init/001_roles.sql"


def test_v14_has_bounded_reservation_queue_and_append_only_events() -> None:
    payload = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE schemabridge_control.registry_publication_jobs" in payload
    assert "CREATE TABLE schemabridge_control.registry_publication_events" in payload
    assert "UNIQUE (workspace_id, catalog_scope, registry_id, target_version)" in payload
    assert "UNIQUE (workspace_id, submitted_by, idempotency_digest)" in payload
    assert "UNIQUE (workspace_id, job_id, revision)" in payload
    assert "octet_length(payload::text) <= 16777216" in payload
    assert "octet_length(payload::text) <= 65536" in payload
    assert "lease_capability_digest char(64)" in payload
    assert "lease_capability varchar" not in payload
    assert "registry_publication_claim_idx" in payload
    assert "registry_publication_expired_lease_idx" in payload
    assert "FOR UPDATE SKIP LOCKED" not in payload
    assert "registry_publication_events_append_only" in payload
    assert "TG_OP <> 'INSERT'" in payload


def test_v14_enforces_exact_proposal_state_fencing_and_recovery() -> None:
    payload = MIGRATION.read_text(encoding="utf-8")

    assert "semantic_onboarding_proposals AS proposal" in payload
    assert "semantic_onboarding_drafts AS draft" in payload
    assert "draft_status <> 'ready_for_publication'" in payload
    assert "prepared_proposal_fingerprint <> NEW.proposal_fingerprint" in payload
    assert "NEW.attempt_count = OLD.attempt_count + 1" in payload
    assert "NEW.fencing_token = OLD.fencing_token + 1" in payload
    assert "OLD.cancel_requested_at IS NOT NULL" in payload
    assert "NEW.status = 'cancel_requested'" in payload
    assert "failure_code = 'readback_required'" in payload
    assert "authorization_expired" in payload
    assert "activation_ready" in payload


def test_v14_roles_separate_api_publisher_and_activation_capabilities() -> None:
    payload = MIGRATION.read_text(encoding="utf-8")
    roles = ROLES.read_text(encoding="utf-8")

    assert "CREATE ROLE schemabridge_publisher" in roles
    publisher_grants = payload.split("GRANT SELECT ON schemabridge_control.schema_migrations", 1)[1]
    assert "TO schemabridge_api, schemabridge_publisher" in publisher_grants
    assert "TO schemabridge_publisher" in publisher_grants
    assert (
        "schemabridge_control.registry_active_pointers\n    TO schemabridge_publisher"
        in publisher_grants
    )
    assert "GRANT UPDATE" in publisher_grants
    api_insert = payload.split("GRANT INSERT (", 1)[1].split(
        ") ON schemabridge_control.registry_publication_jobs", 1
    )[0]
    assert "lease_owner_id" not in api_insert
    assert "candidate_fingerprint" not in api_insert
    assert "observed_authorization_id" not in api_insert
    assert "database_url" not in payload.lower()


def test_v14_exposes_only_bounded_activation_handoff_and_locks_catalog_authority() -> None:
    payload = MIGRATION.read_text(encoding="utf-8")
    function = payload.split(
        "CREATE FUNCTION schemabridge_control.load_registry_activation_ready_handoff",
        1,
    )[1].split("CREATE TRIGGER registry_publication_jobs_lifecycle", 1)[0]

    assert "SECURITY DEFINER" in function
    assert "SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator')" in function
    assert "jobs.status = 'activation_ready'" in function
    assert "FOR SHARE OF jobs" in function
    assert "FOR SHARE OF catalog_connection" in function
    assert "candidate,registry,format_version" in function
    assert "receipt,observed_authorization_id" in function
    assert "catalog_connection.active_generation" in function
    assert "catalog_connection.active_generation_fingerprint" in function
    assert "asset.metadata_fingerprint" in function
    assert "field.metadata_fingerprint" in function
    assert "field.normalized_type" in function
    assert "observed_datahub_asset_urn" in function
    assert "lower(asset.platform) = 'postgres'" not in function
    assert "RETURNS TABLE" in function
    assert (
        "payload jsonb" not in function.split("RETURNS TABLE", 1)[1].split("LANGUAGE plpgsql", 1)[0]
    )

    grants = payload.split("GRANT EXECUTE ON FUNCTION", 2)[2]
    assert "load_registry_activation_ready_handoff" in grants
    assert "TO schemabridge_runtime, schemabridge_migrator" in grants
    assert "schemabridge_api" not in grants
    assert "schemabridge_publisher" not in grants


def test_v14_is_additive_and_has_no_source_or_pointer_mutation() -> None:
    payload = MIGRATION.read_text(encoding="utf-8")

    for forbidden in (
        "DROP TABLE",
        "TRUNCATE ",
        "DELETE FROM",
        "UPDATE schemabridge_control.registry_active_pointers",
        "CREATE INDEX CONCURRENTLY",
    ):
        assert forbidden.upper() not in payload.upper()
    assert MIGRATION.stat().st_size <= 1_048_576
