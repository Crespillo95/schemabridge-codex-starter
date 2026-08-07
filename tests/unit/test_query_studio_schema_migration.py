"""Static security contract for additive Query Studio schema v6."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0006_dynamic_query_studio.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_v6_is_additive_bounded_and_checksumable() -> None:
    sql = _sql()

    assert MIGRATION.is_file()
    assert len(MIGRATION.read_bytes()) < 1_048_576
    assert hashlib.sha256(MIGRATION.read_bytes()).hexdigest() != "0" * 64
    assert "ALTER TABLE schemabridge_control.catalog_" not in sql
    assert "ALTER TABLE schemabridge_control.semantic_" not in sql
    assert "DROP TABLE" not in sql.upper()
    assert "CREATE ROLE" not in sql.upper()
    for table in (
        "tenant_ai_policies",
        "tenant_ai_policy_revisions",
        "ai_provider_request_windows",
        "ai_provider_daily_usage",
        "ai_provider_admission_state",
        "ai_provider_attempt_reservations",
        "ai_provider_usage_audit",
    ):
        assert f"CREATE TABLE schemabridge_control.{table} (" in sql


def test_governed_search_intersects_exact_current_m26_evidence() -> None:
    sql = _sql()
    search = sql.split(
        "CREATE FUNCTION schemabridge_control.search_governed_query_studio_fields(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.expire_ai_provider_attempts(",
        maxsplit=1,
    )[0]

    assert "SECURITY DEFINER" in search
    assert "SET search_path = pg_catalog, schemabridge_control" in search
    assert "semantic_context_gate_projection" in search
    assert "gate.gate_eligible" in search
    assert "semantic_resource_bindings" in search
    assert "binding.binding_state IN ('approved', 'revalidated')" in search
    assert "registry_active_pointers" not in search
    assert "catalog_connections" in search
    assert "catalog_assets" in search
    assert "catalog_fields" in search
    assert "field.search_document @@ query_value.document_query" in search
    assert "LIMIT p_page_size + 1" in search
    assert "p_page_size NOT BETWEEN 1 AND 50" in search
    assert "length(normalized_query) > 2000" in search
    assert "octet_length(normalized_query) > 8192" in search
    assert "eligible_mapping_count BETWEEN 0 AND 1000" in search
    assert "OFFSET" not in search.upper()
    assert "ORDER BY" in search
    assert "matched.deterministic_score DESC" in search
    assert (
        "(\n"
        "                matched.logical_field,\n"
        "                matched.binding_id\n"
        "            ) > ("
    ) in search
    assert "field.description AS field_description" in search
    assert "sample" not in search.lower()
    assert "source_row" not in search.lower()
    assert "credential" not in search.lower()


def test_governance_scope_binds_pointer_head_and_catalog_vector() -> None:
    sql = _sql()
    scope = sql.split(
        "CREATE FUNCTION schemabridge_control.load_query_studio_governance_scope(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.search_governed_query_studio_fields(",
        maxsplit=1,
    )[0]

    assert "registry_active_pointers" in scope
    assert "semantic_change_heads" in scope
    assert "semantic_context_gate_projection" in scope
    assert "pointer.transition_id" in scope
    assert "head.head_revision" in scope
    assert "head.baseline_revision" in scope
    assert "head.catalog_generation_vector_fingerprint" in scope
    assert "count(gate.evidence_id) FILTER (WHERE gate.gate_eligible)" in scope


def test_physical_discovery_is_a_separate_current_nonsemantic_keyset() -> None:
    sql = _sql()
    scope = sql.split(
        "CREATE FUNCTION schemabridge_control.load_physical_discovery_scope(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.discover_physical_fields(",
        maxsplit=1,
    )[0]
    discovery = sql.split(
        "CREATE FUNCTION schemabridge_control.discover_physical_fields(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_query_studio_governance_scope(",
        maxsplit=1,
    )[0]

    assert "SECURITY DEFINER" in scope
    assert "connection.active_generation" in scope
    assert "generation.status = 'completed'" in scope
    assert "connection.status = 'enabled'" in scope
    assert "asset_count bigint" in scope
    assert "field_count bigint" in scope
    assert "sum(current.asset_count)" in scope
    assert "sum(current.field_count)" in scope
    assert "jsonb_agg" in scope
    assert "sha256" in scope
    assert "SECURITY DEFINER" in discovery
    assert "catalog_assets" in discovery
    assert "catalog_fields" in discovery
    assert "semantic_resource_bindings" not in discovery
    assert "semantic_context_gate_projection" not in discovery
    assert "candidate_id" not in discovery
    assert "LIMIT p_page_size + 1" in discovery
    assert "p_page_size NOT BETWEEN 1 AND 50" in discovery
    assert "OFFSET" not in discovery.upper()
    assert "matched.deterministic_score DESC" in discovery
    assert "p_after_connection_id" in discovery
    assert "p_after_asset_key" in discovery
    assert "p_after_field_key" in discovery
    assert "catalog snapshot changed" in discovery
    assert "candidate_keys AS MATERIALIZED" in discovery
    assert "catalog_fields_physical_discovery_exact_idx" in sql
    assert "5434" not in discovery
    assert "10 tables" not in discovery.lower()


def test_governed_search_physical_type_prefers_approved_evidence_and_fails_closed() -> None:
    sql = _sql()
    resolver = sql.split(
        "CREATE FUNCTION schemabridge_control.resolve_query_studio_physical_type(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.discover_physical_fields(",
        maxsplit=1,
    )[0]
    search = sql.split(
        "CREATE FUNCTION schemabridge_control.search_governed_query_studio_fields(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.expire_ai_provider_attempts(",
        maxsplit=1,
    )[0]

    assert "p_approved_type IN" in resolver
    assert "p_native_type" in resolver
    assert "ELSE NULL" in resolver
    assert "'unknown'" not in resolver
    assert "binding.normalized_type" in search
    assert "field.native_type" in search
    assert "schemabridge_control.resolve_query_studio_physical_type(" in search
    assert "field.normalized_type" not in search
    assert ") IS NOT NULL" in search


def test_ai_policy_is_versioned_opt_in_and_migrator_only() -> None:
    sql = _sql()
    apply_function = sql.split(
        "CREATE FUNCTION schemabridge_control.apply_tenant_ai_policy(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_tenant_ai_policy(",
        maxsplit=1,
    )[0]

    assert "external_ai_enabled boolean NOT NULL DEFAULT false" in sql
    assert "provider_governance_accepted" in sql
    assert "provider_governance_fingerprint" in sql
    assert "provider_governance_accepted_at" in sql
    assert "tenant_ai_policy_revisions_immutable" in sql
    assert "p_expected_version" in apply_function
    assert "FOR UPDATE" in apply_function
    assert "SESSION_USER <> 'schemabridge_migrator'" in apply_function
    assert "p_confirmation <> 'APPLY TENANT AI POLICY'" in apply_function
    assert "gpt-5-nano-2025-08-07" in sql
    assert "gpt-5.4-nano-2026-03-17" in sql
    assert "gpt-5.6-luna" in sql
    assert "endpoint_region IN ('global', 'eu', 'us')" in sql
    assert "configuration_fingerprint" in sql
    inspector = sql.split(
        "CREATE FUNCTION schemabridge_control.inspect_tenant_ai_policy(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_tenant_ai_policy(",
        maxsplit=1,
    )[0]
    assert "SESSION_USER <> 'schemabridge_migrator'" in inspector
    assert "provider_governance_fingerprint" in inspector
    assert "endpoint_origin_fingerprint" in inspector
    assert "updated_by" in inspector


def test_attempt_admission_is_atomic_fenced_and_conservatively_accounted() -> None:
    sql = _sql()
    reserve = sql.split(
        "CREATE FUNCTION schemabridge_control.reserve_ai_provider_attempt(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.settle_ai_provider_attempt(",
        maxsplit=1,
    )[0]
    settle = sql.split(
        "CREATE FUNCTION schemabridge_control.settle_ai_provider_attempt(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.prune_ai_provider_temporary_state(",
        maxsplit=1,
    )[0]
    expire = sql.split(
        "CREATE FUNCTION schemabridge_control.expire_ai_provider_attempts(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.reserve_ai_provider_attempt(",
        maxsplit=1,
    )[0]

    assert "UNIQUE (workspace_id, request_id, stage, attempt_number)" in sql
    assert "UNIQUE (workspace_id, idempotency_digest)" in sql
    assert "attempt_number BETWEEN 1 AND 2" in sql
    assert "capability_digest" in sql
    assert "fencing_token" in sql
    assert "FOR UPDATE" in reserve
    assert "policy_record.requests_per_minute" in reserve
    assert "policy_record.daily_input_token_limit" in reserve
    assert "policy_record.daily_output_token_limit" in reserve
    assert "policy_record.concurrent_attempt_limit" in reserve
    assert "'rate_limited'::varchar(32)" in reserve
    assert "'quota_exhausted'::varchar(32)" in reserve
    assert "'concurrency_limited'::varchar(32)" in reserve
    assert "reserved_input_tokens" in settle
    assert "charged_input_tokens" in settle
    assert "charge_input := reservation_record.estimated_input_tokens" in settle
    assert "charge_output := reservation_record.estimated_output_tokens" in settle
    assert "reservation_record.estimated_input_tokens" in expire
    assert "'expired_crash'" in expire
    assert "active_attempt_count = active_attempt_count - 1" in settle
    assert "active_attempt_count = active_attempt_count - 1" in expire


def test_usage_audit_is_sanitized_append_only_and_retained() -> None:
    sql = _sql()
    audit_table = (
        sql.split(
            "CREATE TABLE schemabridge_control.ai_provider_usage_audit (",
            maxsplit=1,
        )[1]
        .split(");", maxsplit=1)[0]
        .lower()
    )

    for forbidden in (
        "prompt",
        "response",
        "definition",
        "candidate",
        "source_value",
        "source_row",
        "sql",
        "parameter",
        "credential",
        "password",
        "token_value",
        "raw_actor",
        "raw_workspace",
    ):
        assert forbidden not in audit_table
    for required in (
        "workspace_scope_digest",
        "actor_digest",
        "request_id",
        "model_snapshot",
        "configuration_fingerprint",
        "semantic_scope_fingerprint",
        "semantic_payload_fingerprint",
        "input_tokens",
        "output_tokens",
        "duration_ms",
        "outcome_code",
        "retain_until",
    ):
        assert required in audit_table
    assert "CREATE TRIGGER ai_provider_usage_audit_immutable" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "M29" not in sql


def test_v6_least_privilege_grants_only_runtime_provider_capability() -> None:
    sql = _sql()
    grants = sql.split("REVOKE ALL ON", maxsplit=1)[1]

    assert "FROM PUBLIC;" in grants
    assert "TO schemabridge_runtime;" in grants
    assert "TO schemabridge_migrator;" in grants
    runtime_grant = grants.split("GRANT EXECUTE ON FUNCTION", maxsplit=1)[1].split(
        "TO schemabridge_runtime;",
        maxsplit=1,
    )[0]
    for function in (
        "load_tenant_ai_policy",
        "load_physical_discovery_scope",
        "discover_physical_fields",
        "load_query_studio_governance_scope",
        "search_governed_query_studio_fields",
        "expire_ai_provider_attempts",
        "reserve_ai_provider_attempt",
        "settle_ai_provider_attempt",
    ):
        assert function in runtime_grant
    assert "apply_tenant_ai_policy" not in runtime_grant
    assert "prune_ai_provider_temporary_state" not in runtime_grant
    assert "resolve_query_studio_physical_type" not in runtime_grant
    for role in (
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_catalog",
        "schemabridge_reconciler",
    ):
        assert f"TO {role}" not in sql
    assert "GRANT SELECT ON schemabridge_control.tenant_ai_policies" not in sql
    assert "GRANT SELECT ON schemabridge_control.ai_provider_usage_audit" not in sql
