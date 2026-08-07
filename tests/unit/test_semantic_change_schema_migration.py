from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0005_semantic_change_management.sql"
EVIDENCE_ADAPTER = ROOT / "src/schemabridge/adapters/semantic_change/postgres_evidence.py"


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_v5_is_additive_bounded_and_contains_the_semantic_change_boundary() -> None:
    sql = _migration_sql()
    expected_tables = {
        "catalog_generation_changes",
        "semantic_resource_bindings",
        "semantic_join_profiles",
        "semantic_change_reports",
        "semantic_change_findings",
        "semantic_change_impacts",
        "semantic_change_resolutions",
        "semantic_artifact_dependencies",
        "semantic_dependency_index_states",
        "semantic_change_heads",
        "semantic_change_scan_requests",
        "semantic_join_profile_jobs",
    }

    assert MIGRATION.stat().st_size <= 1_048_576
    for table in expected_tables:
        assert f"CREATE TABLE schemabridge_control.{table}" in sql
    for forbidden in (
        "CREATE ROLE",
        "DROP TABLE",
        "TRUNCATE ",
        "CREATE INDEX CONCURRENTLY",
    ):
        assert forbidden not in sql.upper()
    for forbidden_column in (
        "sample_value varchar",
        "source_row jsonb",
        "raw_value varchar",
        "password varchar",
        "openai_",
    ):
        assert forbidden_column not in sql.lower()
    assert "semantic_json_has_protected_keys" in sql
    assert "resulting_baseline_revision\n                    = resulting_head_revision" in sql
    assert (
        "resulting_baseline_revision\n"
        "                    = expected_baseline_revision + 1" not in sql
    )


def test_v5_generation_capture_is_post_promotion_append_only_and_durable() -> None:
    sql = _migration_sql()
    capture = sql.split(
        "CREATE FUNCTION schemabridge_control.capture_catalog_generation_change()",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.enqueue_registry_semantic_scan()",
        maxsplit=1,
    )[0]

    assert "AFTER UPDATE OF active_generation" in sql
    assert "NEW.active_generation IS DISTINCT FROM OLD.active_generation" in sql
    assert "IF OLD.active_generation IS NOT NULL THEN" in capture
    enqueue = capture.split("END IF;", maxsplit=2)[-1]
    assert "FOR target_scope IN" in enqueue
    assert "registry_active_pointers AS pointer" in enqueue
    assert "pointer.catalog_scope = NEW.catalog_scope" in enqueue
    assert "target_scope.catalog_scope" in enqueue
    assert "target_scope.registry_id" in enqueue
    assert "'catalog_generation_scan_v2'" in enqueue
    assert capture.count("INSERT INTO schemabridge_control.catalog_generation_changes") == 2
    assert "FULL OUTER JOIN current_asset" in capture
    assert "FULL OUTER JOIN current_field" in capture
    assert "'added'" in capture
    assert "'removed'" in capture
    assert "'metadata_changed'" in capture
    assert "ON CONFLICT DO NOTHING" in capture
    assert ("CREATE TRIGGER catalog_generation_changes_immutable\nBEFORE UPDATE OR DELETE") in sql
    assert "ON DELETE CASCADE" not in sql


def test_v5_has_closed_shapes_compound_indexes_and_exact_identity_bindings() -> None:
    sql = _migration_sql()
    binding = sql.split(
        "CREATE TABLE schemabridge_control.semantic_resource_bindings (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    reports = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_reports (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    scans = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_scan_requests (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    assert "workspace_id, catalog_scope, connection_id" in binding
    assert "asset_key char(64)" in binding
    assert "field_key char(64)" in binding
    assert "field_path varchar(200)[]" in binding
    assert "catalog_generation bigint" in binding
    assert "pointer_transition_id varchar(200)" in binding
    assert "mapping_decision_id varchar(200)" in binding
    assert "evidence_fingerprint char(64)" in binding
    assert "catalog_generation_vector_json jsonb" in reports
    assert "dependency_index_complete boolean" in reports
    assert "finding_set_fingerprint char(64)" in reports
    assert "impact_set_fingerprint char(64)" in reports
    assert "source_kind = 'catalog_generation'" in scans
    assert "catalog_scope IS NOT NULL" in scans
    assert "registry_id IS NOT NULL" in scans
    assert "registry_generation IS NULL" in scans
    for index in (
        "catalog_assets_semantic_lookup_idx",
        "catalog_generation_changes_generation_idx",
        "catalog_generation_changes_resource_idx",
        "semantic_resource_bindings_scope_idx",
        "semantic_resource_bindings_resource_idx",
        "semantic_resource_bindings_gate_idx",
        "semantic_join_profiles_scope_idx",
        "semantic_join_profiles_gate_idx",
        "semantic_change_reports_scope_page_idx",
        "semantic_change_reports_status_page_idx",
        "semantic_change_resolutions_report_state_idx",
        "semantic_change_findings_page_idx",
        "semantic_change_impacts_page_idx",
        "semantic_artifact_dependencies_mapping_idx",
        "semantic_artifact_dependencies_join_idx",
        "semantic_change_scan_claim_idx",
        "semantic_change_scan_expired_lease_idx",
    ):
        assert f"CREATE INDEX {index}" in sql
    assert "CREATE UNIQUE INDEX catalog_fields_semantic_lookup_idx" in sql


def test_v5_join_profiles_are_aggregate_only_and_reports_are_immutable() -> None:
    sql = _migration_sql()
    profiles = sql.split(
        "CREATE TABLE schemabridge_control.semantic_join_profiles (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    for column in (
        "left_row_count bigint",
        "right_row_count bigint",
        "left_null_count bigint",
        "right_null_count bigint",
        "left_invalid_count bigint",
        "right_invalid_count bigint",
        "left_distinct_count bigint",
        "right_distinct_count bigint",
        "matching_distinct_count bigint",
        "max_left_multiplicity bigint",
        "max_right_multiplicity bigint",
        "foreign_key_evidence boolean",
        "approved_baseline_revision bigint",
        "approval_id varchar(200)",
        "actor_id varchar(200)",
        "decided_at timestamptz",
        "profile_fingerprint char(64)",
    ):
        assert column in profiles
    for forbidden in ("key_value", "source_row", "sample", "sql_text", "parameter"):
        assert forbidden not in profiles.lower()
    for trigger in (
        "semantic_resource_bindings_immutable",
        "semantic_join_profiles_immutable",
        "semantic_change_reports_immutable",
        "semantic_change_findings_immutable",
        "semantic_change_impacts_immutable",
        "semantic_change_resolutions_immutable",
        "semantic_artifact_dependencies_immutable",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql


def test_v5_reports_preserve_exact_bounded_canonical_domain_payloads() -> None:
    sql = _migration_sql()
    reports = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_reports (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    findings = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_findings (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    impacts = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_impacts (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    for column in (
        "pointer_fingerprint char(64)",
        "context_fingerprint char(64)",
        "observation_fingerprint char(64)",
        "baseline_fingerprint char(64)",
        "context_json jsonb",
        "observation_json jsonb",
        "report_json jsonb",
    ):
        assert column in reports
    assert "octet_length(context_json::text) <= 4000000" in reports
    assert "octet_length(observation_json::text) <= 16000000" in reports
    assert "octet_length(report_json::text) <= 16000000" in reports
    assert "semantic_json_has_protected_keys" in reports
    assert "semantic_change_report_payload_identity_shape" in reports
    assert "finding_json jsonb" in findings
    assert "semantic_change_finding_payload_identity_shape" in findings
    assert "finding_ids_json jsonb" in impacts
    assert "impact_json jsonb" in impacts
    assert "semantic_change_impact_payload_identity_shape" in impacts
    assert "UNIQUE NULLS NOT DISTINCT" in impacts


def test_v5_evidence_and_gate_views_compare_active_catalog_to_baseline() -> None:
    sql = _migration_sql()
    locator_key = sql.split(
        "CREATE FUNCTION schemabridge_control.semantic_catalog_asset_locator_key(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.semantic_catalog_field_locator_key(",
        maxsplit=1,
    )[0]
    field_locator_key = sql.split(
        "CREATE FUNCTION schemabridge_control.semantic_catalog_field_locator_key(",
        maxsplit=1,
    )[1].split(
        "CREATE INDEX catalog_assets_semantic_lookup_idx",
        maxsplit=1,
    )[0]
    semantic_index = sql.split(
        "CREATE INDEX catalog_assets_semantic_lookup_idx",
        maxsplit=1,
    )[1].split(
        "CREATE UNIQUE INDEX catalog_fields_semantic_lookup_idx",
        maxsplit=1,
    )[0]
    field_semantic_index = sql.split(
        "CREATE UNIQUE INDEX catalog_fields_semantic_lookup_idx",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_catalog_evidence_projection",
        maxsplit=1,
    )[0]
    evidence_view = sql.split(
        "CREATE VIEW schemabridge_control.semantic_catalog_evidence_projection",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_context_gate_projection",
        maxsplit=1,
    )[0]
    gate_view = sql.split(
        "CREATE VIEW schemabridge_control.semantic_context_gate_projection",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_change_report_public",
        maxsplit=1,
    )[0]

    assert "field_definition_fingerprint" in evidence_view
    assert "field_terms_fingerprint" in evidence_view
    assert "asset.qualified_name" in evidence_view
    assert "field.field_name" in evidence_view
    assert "field.description" in evidence_view
    assert "field.tags" in evidence_view
    assert "field.glossary_terms" in evidence_view
    assert "semantic_catalog_connection_evidence_projection" in sql
    assert "load_semantic_initial_catalog_candidates" in sql
    assert "load_semantic_bound_catalog_evidence" in sql
    assert "RETURNS text" in locator_key
    assert "IMMUTABLE" in locator_key
    assert "STRICT" in locator_key
    assert "PARALLEL SAFE" in locator_key
    assert "SECURITY INVOKER" in locator_key
    assert "SET search_path = pg_catalog" in locator_key
    assert "octet_length(workspace_id)::text || ':' || workspace_id" in locator_key
    assert "octet_length(connection_id)::text || ':' || connection_id" in locator_key
    assert "octet_length(generation::text)::text || ':' || generation::text" in locator_key
    assert "octet_length(qualified_name)::text || ':' || qualified_name" in locator_key
    assert "semantic_catalog_asset_locator_key(" in semantic_index
    assert "asset_key" not in semantic_index
    assert "RETURNS text" in field_locator_key
    assert "IMMUTABLE" in field_locator_key
    assert "STRICT" in field_locator_key
    assert "PARALLEL SAFE" in field_locator_key
    assert "SECURITY INVOKER" in field_locator_key
    assert "SET search_path = pg_catalog" in field_locator_key
    assert "octet_length(asset_key::text)::text || ':' || asset_key::text" in field_locator_key
    assert "octet_length(field_key::text)::text || ':' || field_key::text" in field_locator_key
    assert "semantic_catalog_field_locator_key(" in field_semantic_index
    assert "qualified_name" not in field_semantic_index
    initial_lookup = sql.split(
        "CREATE FUNCTION schemabridge_control.load_semantic_initial_catalog_candidates(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_semantic_bound_catalog_evidence(",
        maxsplit=1,
    )[0]
    assert "jsonb_array_length(requested_fields)" in initial_lookup
    assert "BETWEEN 1 AND 2000" in initial_lookup
    assert "octet_length(requested_fields::text) <= 2000000" in initial_lookup
    assert "count(DISTINCT ordinal)" in initial_lookup
    assert "coalesce(min(ordinal), 0) = 0" in initial_lookup
    assert "coalesce(max(ordinal), -1) = count(*) - 1" in initial_lookup
    assert "item.selected_field_path = item.field_path" in initial_lookup
    assert "LEFT JOIN LATERAL" in initial_lookup
    assert "JOIN LATERAL (" in initial_lookup
    assert "SELECT inner_asset.*" in initial_lookup
    assert "SELECT inner_field.*" in initial_lookup
    assert "semantic_catalog_asset_locator_key(" in initial_lookup
    assert "replace(to_jsonb(parsed.field_path)::text, ', ', ',')" in initial_lookup
    assert "semantic_catalog_field_locator_key(" in initial_lookup
    assert "field.field_key = requested.field_key" in initial_lookup
    assert "OFFSET 0" in initial_lookup
    assert "count(*) OVER () AS candidate_count" in initial_lookup
    assert "LIMIT 2" in initial_lookup
    assert "asset.workspace_id = requested_workspace_id" in initial_lookup
    assert "asset.connection_id = source_connection.connection_id" in initial_lookup
    assert "asset.generation = source_connection.active_generation" in initial_lookup
    assert "asset.qualified_name = requested.dataset_ref" in initial_lookup
    assert "inner_field.field_path = requested.field_path" not in initial_lookup
    assert "field.field_path = requested.field_path" in initial_lookup
    assert "SECURITY DEFINER" in initial_lookup
    assert "SET search_path = pg_catalog, schemabridge_control" in initial_lookup
    assert "semantic_catalog_evidence_projection" not in initial_lookup
    exact_lookup = sql.split(
        "CREATE FUNCTION schemabridge_control.load_semantic_bound_catalog_evidence(",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_context_gate_projection",
        maxsplit=1,
    )[0]
    assert "jsonb_array_length(requested_bindings)" in exact_lookup
    assert "BETWEEN 1 AND 2000" in exact_lookup
    assert "octet_length(requested_bindings::text) <= 2000000" in exact_lookup
    assert "asset.asset_id = requested.asset_id" in exact_lookup
    assert "field.field_path = requested.field_path" in exact_lookup
    assert "SECURITY DEFINER" in exact_lookup
    assert "LEFT JOIN schemabridge_control.catalog_connections" in gate_view
    assert "current_connection.active_generation" in gate_view
    assert "baseline_field_metadata_fingerprint" in gate_view
    assert "current_field_metadata_fingerprint" in gate_view
    assert "baseline_field_definition_fingerprint" in gate_view
    assert "current_field_definition_fingerprint" in gate_view
    assert "baseline_field_terms_fingerprint" in gate_view
    assert "current_field_terms_fingerprint" in gate_view
    assert "catalog_evidence_available" in gate_view
    assert "catalog_evidence_matches" in gate_view
    assert "catalog_generation_covered" in gate_view
    assert "gate_eligible" in gate_view
    assert "latest_context_report AS" in gate_view
    assert "live_dependency_status AS" in gate_view
    assert "semantic_dependency_index_states AS live" in gate_view
    assert "live.pointer_transition_id = head.pointer_transition_id" in gate_view
    assert "live.watermark = report.dependency_index_watermark" in gate_view
    assert "live.index_fingerprint" in gate_view
    assert "dependency_index_current" in gate_view
    assert "latest_dependency_impacts AS" in gate_view
    assert "latest_join_profiles AS" in gate_view
    assert "candidate.baseline_revision = head.baseline_revision" in gate_view
    assert "impact.artifact_id = evidence.logical_field" in gate_view
    assert "latest_profile.safety_fingerprint = profile.policy_fingerprint" in gate_view
    assert "left_binding.connection_id = right_binding.connection_id" in gate_view
    assert "mapping.context_state IN ('current', 'revalidated', 'rejected')" in gate_view
    assert "mapping.dependency_index_complete" in gate_view


def test_initial_evidence_adapter_uses_only_the_bounded_candidate_function() -> None:
    source = EVIDENCE_ADAPTER.read_text(encoding="utf-8")
    initial_loader = source.split(
        "    def _load_initial_candidates(",
        maxsplit=1,
    )[1].split(
        "    def _load_bound_rows(",
        maxsplit=1,
    )[0]

    assert "load_semantic_initial_catalog_candidates" in initial_loader
    assert "semantic_catalog_evidence_projection" not in initial_loader


def test_v5_public_report_status_uses_only_audited_committed_state() -> None:
    sql = _migration_sql()
    public_view = sql.split(
        "CREATE VIEW schemabridge_control.semantic_change_report_public",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_change_finding_public",
        maxsplit=1,
    )[0]

    assert "semantic_change_resolutions AS resolution" in public_view
    assert "control_audit_events AS audit" in public_view
    assert "audit.operation = 'semantic_change_decision'" in public_view
    assert "head.head_revision" in public_view
    assert "audited_resolution.resulting_head_revision" in public_view
    assert "registry_active_pointers AS pointer" in public_view
    assert "THEN 'superseded'" in public_view
    assert "ELSE report.outcome" in public_view


def test_v5_scan_queue_is_idempotent_fenced_and_enqueued_by_both_pointers() -> None:
    sql = _migration_sql()
    scans = sql.split(
        "CREATE TABLE schemabridge_control.semantic_change_scan_requests (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    guard = sql.split(
        "CREATE FUNCTION schemabridge_control.guard_semantic_change_scan()",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.capture_catalog_generation_change()",
        maxsplit=1,
    )[0]

    assert "UNIQUE (workspace_id, source_kind, source_event_key)" in scans
    assert "lease_capability_digest char(64)" in scans
    assert "fencing_token bigint" in scans
    assert "max_attempts integer" in scans
    assert "lease_acquired_at timestamptz" in scans
    assert "lease_heartbeat_at timestamptz" in scans
    assert "lease_expires_at timestamptz" in scans
    assert "completed_report_id varchar(200)" in scans
    assert "completed_report_fingerprint char(64)" in scans
    assert "superseded_by_scan_id varchar(80)" in scans
    assert "superseded_by_scan_fingerprint char(64)" in scans
    assert "status IN (" in scans
    assert "NEW.fencing_token <= OLD.fencing_token" in guard
    assert "NEW.attempts <> OLD.attempts + 1" in guard
    assert "NEW.fencing_token <> OLD.fencing_token" in guard
    assert "NEW.lease_heartbeat_at <= OLD.lease_heartbeat_at" in guard
    assert "NEW.lease_expires_at <= OLD.lease_expires_at" in guard
    assert "catalog_generation_semantic_change_capture" in sql
    assert "registry_pointer_semantic_scan_enqueue" in sql
    assert sql.count("ON CONFLICT (workspace_id, source_kind, source_event_key) DO NOTHING") >= 4


def test_v5_join_profile_queue_separates_reconciler_from_source_worker() -> None:
    sql = _migration_sql()
    jobs = sql.split(
        "CREATE TABLE schemabridge_control.semantic_join_profile_jobs (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    guard = sql.split(
        "CREATE FUNCTION schemabridge_control.guard_semantic_join_profile_job()",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.capture_catalog_generation_change()",
        maxsplit=1,
    )[0]

    assert "UNIQUE (workspace_id, scan_id, proposal_fingerprint)" in jobs
    assert "connection_id varchar(200) NOT NULL" in jobs
    assert "connection_id ~ '^[a-z][a-z0-9_-]{2,199}$'" in jobs
    assert "'source_connection_mismatch'" in jobs
    assert "proposal_json jsonb" in jobs
    assert "octet_length(proposal_json::text) <= 65536" in jobs
    assert "proposal_json - ARRAY[" in jobs
    assert "jsonb_path_exists" in jobs
    assert '"map_values"' in jobs
    assert "semantic_json_has_protected_keys" in jobs
    assert "lease_capability_digest char(64)" in jobs
    assert "fencing_token bigint" in jobs
    assert "result_profile_json jsonb" in jobs
    assert "octet_length(result_profile_json::text) <= 16384" in jobs
    assert "result_profile_json - ARRAY[" in jobs
    assert "source_row" not in jobs.lower()
    assert "sample_value" not in jobs.lower()
    assert "sql_text" not in jobs.lower()
    assert "FOR UPDATE SKIP LOCKED" not in jobs
    assert "semantic_join_profile_job_claim_idx" in sql
    claim_index = sql.split(
        "CREATE INDEX semantic_join_profile_job_claim_idx",
        maxsplit=1,
    )[1].split(
        "WHERE status IN ('requested', 'retry_wait');",
        maxsplit=1,
    )[0]
    assert claim_index.index("workspace_id") < claim_index.index("connection_id")
    assert claim_index.index("connection_id") < claim_index.index("available_at")
    expired_index = sql.split(
        "CREATE INDEX semantic_join_profile_job_expired_lease_idx",
        maxsplit=1,
    )[1].split(
        "WHERE status = 'leased';",
        maxsplit=1,
    )[0]
    assert expired_index.index("workspace_id") < expired_index.index("connection_id")
    assert expired_index.index("connection_id") < expired_index.index("lease_expires_at")
    assert "semantic_join_profile_job_expired_lease_idx" in sql
    assert "semantic_join_profile_jobs_guard" in sql
    assert "pg_advisory_xact_lock" in guard
    assert ") >= 500 THEN" in guard
    assert "ERRCODE = '53300'" in guard
    assert "NEW.fencing_token <> OLD.fencing_token + 1" in guard
    assert "NEW.lease_capability_digest" in guard
    assert "NEW.connection_id IS DISTINCT FROM OLD.connection_id" in guard
    assert ("schemabridge_control.semantic_join_profile_jobs\n    TO schemabridge_worker;") in sql
    worker_update = sql.split(
        "GRANT UPDATE (",
    )[-1].split(
        ") ON schemabridge_control.semantic_join_profile_jobs",
        maxsplit=1,
    )[0]
    assert "proposal_json" not in worker_update
    assert "workspace_id" not in worker_update
    assert "scan_id" not in worker_update
    assert "connection_id" not in worker_update
    assert "result_profile_json" in worker_update


def test_v5_least_privilege_exposes_only_sanitized_api_and_gate_views() -> None:
    sql = _migration_sql()
    report_public = sql.split(
        "CREATE VIEW schemabridge_control.semantic_change_report_public",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_change_finding_public",
        maxsplit=1,
    )[0]
    finding_public = sql.split(
        "CREATE VIEW schemabridge_control.semantic_change_finding_public",
        maxsplit=1,
    )[1].split(
        "CREATE VIEW schemabridge_control.semantic_change_impact_public",
        maxsplit=1,
    )[0]
    impact_public = sql.split(
        "CREATE VIEW schemabridge_control.semantic_change_impact_public",
        maxsplit=1,
    )[1].split(
        "INSERT INTO schemabridge_control.semantic_change_scan_requests",
        maxsplit=1,
    )[0]

    for view in (
        "semantic_catalog_evidence_projection",
        "semantic_catalog_connection_evidence_projection",
        "semantic_context_gate_projection",
        "semantic_change_report_public",
        "semantic_change_finding_public",
        "semantic_change_impact_public",
    ):
        assert f"CREATE VIEW schemabridge_control.{view}" in sql
    assert "WITH (security_barrier = true)" in sql
    assert "FROM PUBLIC;" in sql
    assert (
        "schemabridge_control.semantic_context_gate_projection\n"
        "    TO schemabridge_runtime, schemabridge_worker;"
    ) in sql
    assert (
        "schemabridge_control.semantic_change_report_public,\n"
        "    schemabridge_control.semantic_change_finding_public,\n"
        "    schemabridge_control.semantic_change_impact_public\n"
        "    TO schemabridge_api;"
    ) in sql
    assert sql.count("TO schemabridge_catalog") == 1
    catalog_execute = sql.split(
        "GRANT EXECUTE ON FUNCTION\n    schemabridge_control.semantic_catalog_asset_locator_key(",
        maxsplit=1,
    )[1].split("    TO schemabridge_catalog;", maxsplit=1)[0]
    assert "semantic_catalog_field_locator_key(" in catalog_execute
    for field in (
        "status",
        "pointer_generation",
        "pointer_fingerprint",
        "catalog_generation_count",
        "observation_fingerprint",
        "mapping_impact_count",
        "join_impact_count",
        "workflow_impact_count",
        "recipe_impact_count",
        "impacts_complete",
        "dependency_watermark",
        "fingerprint",
    ):
        assert field in report_public
    for field in (
        "target_kind",
        "target_id",
        "target_version",
        "previous_fingerprint",
        "current_fingerprint",
        "risks",
    ):
        assert field in finding_public
    assert "finding_ids" in impact_public
    for public_view in (report_public, finding_public, impact_public):
        assert "context_json AS" not in public_view
        assert "observation_json AS" not in public_view
        assert "finding_json AS" not in public_view
        assert "impact_json AS" not in public_view
    api_grants = [statement for statement in sql.split(";") if "TO schemabridge_api" in statement]
    assert len(api_grants) == 1
    assert "_public" in api_grants[0]
    assert "semantic_resource_bindings" not in api_grants[0]
    assert "semantic_change_resolutions" not in api_grants[0]


def test_v5_reconciler_reads_only_workflow_columns_needed_for_dependency_indexing() -> None:
    sql = _migration_sql()
    reconciler_workflow_grant = (
        "GRANT SELECT (\n"
        "    workspace_id,\n"
        "    id,\n"
        "    revision,\n"
        "    payload,\n"
        "    updated_at\n"
        ") ON schemabridge_control.agent_workflow_drafts\n"
        "    TO schemabridge_reconciler;"
    )

    assert reconciler_workflow_grant in sql
    assert (
        "GRANT SELECT ON schemabridge_control.agent_workflow_drafts\n"
        "    TO schemabridge_reconciler;"
    ) not in sql
