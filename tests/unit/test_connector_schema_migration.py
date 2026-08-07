"""Static security contract for additive connector-routing schema v9."""

from __future__ import annotations

from pathlib import Path

from schemabridge.domain.connectors import POSTGRES_TYPE_CONTRACT_FINGERPRINT

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0009_tenant_connector_routing.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function(sql: str, name: str, next_name: str) -> str:
    return sql.split(
        f"CREATE FUNCTION schemabridge_control.{name}",
        maxsplit=1,
    )[1].split(
        f"CREATE FUNCTION schemabridge_control.{next_name}",
        maxsplit=1,
    )[0]


def test_v9_is_additive_bounded_and_keeps_prior_migrations_immutable() -> None:
    sql = _sql()

    assert MIGRATION.is_file()
    assert MIGRATION.stat().st_size <= 1_048_576
    for table in (
        "connector_contract_revisions",
        "connector_route_revisions",
        "connector_private_route_revisions",
        "connector_route_heads",
        "connector_route_audit",
    ):
        assert f"CREATE TABLE schemabridge_control.{table} (" in sql
    for forbidden in (
        "CREATE ROLE",
        "DROP TABLE",
        "TRUNCATE ",
        "CREATE INDEX CONCURRENTLY",
    ):
        assert forbidden not in sql.upper()
    for version in range(1, 9):
        assert f"{version:04d}_" not in sql


def test_v9_drains_legacy_nonterminal_work_before_any_target_alter() -> None:
    sql = _sql()
    preflight = sql.split(
        "CREATE FUNCTION schemabridge_control.connector_canonical_cost",
        maxsplit=1,
    )[0]

    assert preflight.startswith("LOCK TABLE")
    assert "IN SHARE ROW EXCLUSIVE MODE" in preflight
    assert "schemabridge_control.execution_jobs" in preflight
    assert "schemabridge_control.semantic_join_profile_jobs" in preflight
    assert "schemabridge_control.catalog_refresh_runs" in preflight
    for status in ("'queued'", "'leased'", "'cancel_requested'", "'retry_wait'"):
        assert status in preflight
    for status in ("'requested'", "'staging'"):
        assert status in preflight
    assert preflight.index("LOCK TABLE") < preflight.index("DO $migration$")
    assert sql.index("DO $migration$") < sql.index(
        "ALTER TABLE schemabridge_control.execution_jobs"
    )


def test_v9_replaces_the_query_studio_type_resolver_with_the_m28_contract() -> None:
    sql = _sql()
    resolver = sql.split(
        "CREATE OR REPLACE FUNCTION\n    schemabridge_control.resolve_query_studio_physical_type(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.connector_canonical_cost",
        maxsplit=1,
    )[0]

    assert "RETURNS varchar(32)" in resolver
    assert "LANGUAGE sql" in resolver
    assert "IMMUTABLE" in resolver
    assert "PARALLEL SAFE" in resolver
    assert "SECURITY INVOKER" in resolver
    assert "SET search_path = pg_catalog" in resolver
    assert "length(p_native_type) BETWEEN 1 AND 200" in resolver
    assert "octet_length(p_native_type) <= 400" in resolver
    assert "btrim(p_native_type) = p_native_type" in resolver
    assert "p_native_type !~ '[[:cntrl:]]'" in resolver
    assert "native_type LIKE 'pg_catalog.%'" in resolver
    assert "native_type = 'array'" in resolver
    assert "left(native_type, 1) = '_'" in resolver
    for normalized_type in (
        "'string'",
        "'integer'",
        "'decimal'",
        "'float'",
        "'boolean'",
        "'date'",
        "'timestamp'",
        "'binary'",
        "'struct'",
        "'array'",
        "'unknown'",
    ):
        assert normalized_type in resolver
    for native_type in (
        "'bpchar'",
        "'smallserial'",
        "'money'",
        "'double precision'",
        "'timestamptz'",
        "'bit varying'",
        "'jsonb'",
    ):
        assert native_type in resolver
    assert "WHEN p_approved_type IN ('binary', 'struct', 'array', 'unknown')" in resolver
    assert "THEN NULL" in resolver
    for forbidden in ("credential", "password", "dsn", "EXECUTE "):
        assert forbidden not in resolver
    assert (
        "REVOKE ALL ON FUNCTION\n"
        "    schemabridge_control.resolve_query_studio_physical_type(\n"
        "        varchar,\n"
        "        varchar\n"
        "    ),"
    ) in sql


def test_public_contract_has_exact_postgresql_target_and_seven_budget_bounds() -> None:
    sql = _sql()
    contract = sql.split(
        "CREATE TABLE schemabridge_control.connector_contract_revisions (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    for column in (
        "connector_kind varchar(32)",
        "sql_dialect varchar(32)",
        "expected_reader varchar(63)",
        "type_contract_version integer",
        "type_contract_fingerprint char(64)",
        "explain_timeout_ms integer",
        "max_response_bytes integer",
        "max_total_cost numeric(24, 6)",
        "max_estimated_rows bigint",
        "max_plan_nodes integer",
        "max_plan_depth integer",
        "max_plan_width integer",
        "cost_budget_fingerprint char(64)",
        "contract_fingerprint char(64)",
        "approval_fingerprint char(64)",
    ):
        assert column in contract
    assert "CHECK (connector_kind = 'postgresql')" in contract
    assert "CHECK (sql_dialect = 'postgresql')" in contract
    assert "connector_cost_budget_fingerprint(" in contract
    assert "PRIMARY KEY (workspace_id, connection_id, contract_version)" in contract


def test_only_the_reviewed_postgresql_type_contract_v1_can_persist_or_route() -> None:
    sql = _sql()
    contract = sql.split(
        "CREATE TABLE schemabridge_control.connector_contract_revisions (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    generation = sql.split(
        "ALTER TABLE schemabridge_control.catalog_generations",
        maxsplit=1,
    )[1].split(
        "CREATE TABLE schemabridge_control.catalog_refresh_semantic_bindings",
        maxsplit=1,
    )[0]
    binding = sql.split(
        "CREATE TABLE schemabridge_control.catalog_refresh_semantic_bindings (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    apply_change = _function(
        sql,
        "apply_connector_route_change(",
        "load_current_connector_target(",
    )
    catalog_loader = _function(
        sql,
        "load_owned_catalog_connector_route(",
        "activate_catalog_generation_for_target(",
    )
    activation = _function(
        sql,
        "activate_catalog_generation_for_target(",
        "load_owned_execution_connector_route(",
    )
    execution_guard = _function(
        sql,
        "guard_execution_job_connector_target()",
        "guard_profile_job_connector_target()",
    )
    profile_guard = _function(
        sql,
        "guard_profile_job_connector_target()",
        "CREATE TRIGGER connector_contract_revisions_immutable",
    )
    current_target = _function(
        sql,
        "load_current_connector_target(",
        "load_current_preflight_connector_route(",
    )
    preflight = _function(
        sql,
        "load_current_preflight_connector_route(",
        "load_owned_catalog_connector_route(",
    )
    execution = _function(
        sql,
        "load_owned_execution_connector_route(",
        "load_owned_profile_connector_route(",
    )
    profile = sql.split(
        "CREATE FUNCTION schemabridge_control.load_owned_profile_connector_route(",
        maxsplit=1,
    )[1].split("REVOKE INSERT ON", maxsplit=1)[0]

    assert "CHECK (type_contract_version = 1)" in contract
    assert POSTGRES_TYPE_CONTRACT_FINGERPRINT in contract
    assert POSTGRES_TYPE_CONTRACT_FINGERPRINT in generation
    assert POSTGRES_TYPE_CONTRACT_FINGERPRINT in binding
    assert "p_type_contract_version IS DISTINCT FROM 1" in apply_change
    assert POSTGRES_TYPE_CONTRACT_FINGERPRINT in apply_change
    for guarded_path in (
        execution_guard,
        profile_guard,
        current_target,
        preflight,
        catalog_loader,
        activation,
        execution,
        profile,
    ):
        assert "contract.type_contract_version = 1" in guarded_path
        assert POSTGRES_TYPE_CONTRACT_FINGERPRINT in guarded_path
    assert catalog_loader.index(POSTGRES_TYPE_CONTRACT_FINGERPRINT) < catalog_loader.index(
        "INSERT INTO schemabridge_control.catalog_refresh_semantic_bindings"
    )


def test_database_fingerprints_match_the_domain_canonical_contract_surface() -> None:
    sql = _sql()
    budget = _function(
        sql,
        "connector_cost_budget_fingerprint(",
        "connector_target_fingerprint(",
    )
    target = _function(
        sql,
        "connector_target_fingerprint(",
        "reject_connector_immutable_mutation()",
    ).split(
        "CREATE TABLE schemabridge_control.connector_contract_revisions",
        maxsplit=1,
    )[0]

    assert '"m28-query-cost-budget-v1"' in budget
    for key in (
        "explain_timeout_ms",
        "max_estimated_rows",
        "max_plan_depth",
        "max_plan_nodes",
        "max_plan_width",
        "max_response_bytes",
        "max_total_cost",
    ):
        assert key in budget
    assert '"m28-governed-execution-target-v1"' in target
    assert target.index('"connection_id"') < target.index('"connector_kind"')
    assert target.index('"connector_kind"') < target.index('"cost_budget"')
    for public_fact in (
        "workspace_id",
        "connection_id",
        "route_revision",
        "route_fingerprint",
        "expected_reader",
        "type_contract_fingerprint",
        "cost_budget_fingerprint",
    ):
        assert public_fact in target
    assert "credential_binding_ref" not in budget
    assert "credential_binding_ref" not in target


def test_private_revisions_store_only_separate_opaque_capability_references() -> None:
    sql = _sql()
    private = sql.split(
        "CREATE TABLE schemabridge_control.connector_private_route_revisions (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    apply_change = _function(
        sql,
        "apply_connector_route_change(",
        "load_current_connector_target(",
    )

    assert "capability IN ('preflight', 'catalog', 'execution', 'profile')" in private
    assert "credential_binding_ref" in private
    assert "credential_binding_ref !~ '://'" in private
    assert "credential_binding_ref !~ '@'" in private
    assert "route_fingerprint" not in private
    assert "target_fingerprint" not in private
    for forbidden in (
        "password",
        "access_token",
        "secret_path",
        "credential_value",
    ):
        assert forbidden not in private.lower()
    assert "p_catalog_binding_ref = p_execution_binding_ref" in apply_change
    assert "p_catalog_binding_ref = p_profile_binding_ref" in apply_change
    assert "p_execution_binding_ref = p_profile_binding_ref" in apply_change
    assert "p_preflight_binding_ref = p_catalog_binding_ref" in apply_change
    assert "p_preflight_binding_ref = p_execution_binding_ref" in apply_change
    assert "p_preflight_binding_ref = p_profile_binding_ref" in apply_change


def test_route_changes_are_exact_confirmed_cas_idempotent_and_audited() -> None:
    sql = _sql()
    apply_change = _function(
        sql,
        "apply_connector_route_change(",
        "load_current_connector_target(",
    )

    assert "SECURITY DEFINER" in apply_change
    assert "SET search_path = pg_catalog, schemabridge_control" in apply_change
    assert "SESSION_USER <> 'schemabridge_migrator'" in apply_change
    for confirmation in (
        "CREATE CONNECTOR ROUTE",
        "ROTATE CONNECTOR ROUTE",
        "DISABLE CONNECTOR ROUTE",
    ):
        assert confirmation in apply_change
    assert "pg_advisory_xact_lock" in apply_change
    assert "FOR UPDATE" in apply_change
    assert "current_head.head_revision <> p_expected_head_revision" in apply_change
    assert "p_route_revision <> current_head.route_revision + 1" in apply_change
    assert "connector route idempotency conflict" in apply_change
    assert "contract.type_contract_version = p_type_contract_version" in apply_change
    assert "contract.contract_fingerprint = p_contract_fingerprint" in apply_change
    assert "connector_cost_budget_fingerprint(" in apply_change
    assert "connector_target_fingerprint(" in apply_change
    assert "INSERT INTO schemabridge_control.connector_route_audit" in apply_change
    assert (
        "credential_binding_ref"
        not in sql.split(
            "CREATE TABLE schemabridge_control.connector_route_audit (",
            maxsplit=1,
        )[1].split(");", maxsplit=1)[0]
    )


def test_public_target_loader_preserves_disabled_route_and_connection_state() -> None:
    loader = _function(
        _sql(),
        "load_current_connector_target(",
        "load_current_preflight_connector_route(",
    )

    assert "route_status varchar(16)" in loader
    assert "connection_status varchar(16)" in loader
    assert "head.status," in loader
    assert "connection.status," in loader
    assert "head.status = 'enabled'" not in loader
    assert "connection.status = 'enabled'" not in loader
    assert (
        "TO\n"
        "        schemabridge_runtime,\n"
        "        schemabridge_reconciler,\n"
        "        schemabridge_catalog,\n"
        "        schemabridge_migrator;" in _sql()
    )


def test_new_execution_and_profile_jobs_require_an_immutable_exact_target() -> None:
    sql = _sql()
    execution_guard = _function(
        sql,
        "guard_execution_job_connector_target()",
        "guard_profile_job_connector_target()",
    )
    profile_guard = _function(
        sql,
        "guard_profile_job_connector_target()",
        "CREATE TRIGGER connector_contract_revisions_immutable",
    )

    for column in (
        "connector_contract_version",
        "connector_route_revision",
        "connector_route_fingerprint",
        "connector_target_fingerprint",
    ):
        assert sql.count(f"ADD COLUMN {column}") == 2
    assert sql.count("ADD COLUMN connector_workspace_id varchar(200)") == 1
    assert "ADD COLUMN connector_connection_id varchar(200)" in sql
    assert "connector_workspace_id = workflow_workspace_id" in sql
    assert "execution_jobs_connector_target_shape" in sql
    assert "semantic_profile_jobs_connector_target_shape" in sql
    assert sql.count("connector_contract_version IS NOT NULL") >= 2
    assert sql.count("connector_target_fingerprint IS NOT NULL") >= 2
    assert sql.count("MATCH SIMPLE") == 2
    assert "new execution job connector target is required" in sql
    assert "execution job connector target is immutable" in sql
    assert "new profile job connector target is required" in sql
    assert "profile job connector target is immutable" in sql
    assert "execution_jobs_connector_target_guard" in sql
    assert "semantic_profile_jobs_connector_target_guard" in sql
    for guard in (execution_guard, profile_guard):
        assert "SECURITY DEFINER" in guard
        assert "head.status = 'enabled'" in guard
        assert "connection.status = 'enabled'" in guard
        assert "NEW.connector_contract_version := resolved_contract_version" in guard
        assert "NEW.connector_route_revision" in guard
        assert "NEW.connector_route_fingerprint" in guard
        assert "NEW.connector_target_fingerprint" in guard
    assert "head.workspace_id = NEW.connector_workspace_id" in execution_guard
    assert (
        "NEW.connector_workspace_id\n            IS DISTINCT FROM OLD.connector_workspace_id"
    ) in execution_guard
    api_target_grant = sql.split(
        "GRANT INSERT (\n    connector_workspace_id,",
        maxsplit=1,
    )[1].split(
        ") ON schemabridge_control.execution_jobs",
        maxsplit=1,
    )[0]
    assert "connector_contract_version" not in api_target_grant
    assert (
        "connector_target_fingerprint\n"
        ") ON schemabridge_control.execution_jobs\n"
        "    TO schemabridge_api;"
    ) in sql


def test_dynamic_profile_claim_and_reclaim_have_global_bounded_indexes() -> None:
    sql = _sql()

    assert (
        "CREATE INDEX semantic_join_profile_job_global_claim_idx\n"
        "    ON schemabridge_control.semantic_join_profile_jobs (\n"
        "        available_at,\n"
        "        requested_at,\n"
        "        workspace_id,\n"
        "        connection_id,\n"
        "        job_id\n"
        "    )\n"
        "    WHERE status IN ('requested', 'retry_wait')\n"
        "      AND connector_target_fingerprint IS NOT NULL;"
    ) in sql
    assert (
        "CREATE INDEX semantic_join_profile_job_global_expired_lease_idx\n"
        "    ON schemabridge_control.semantic_join_profile_jobs (\n"
        "        lease_expires_at,\n"
        "        workspace_id,\n"
        "        connection_id,\n"
        "        job_id\n"
        "    )\n"
        "    WHERE status = 'leased'\n"
        "      AND connector_target_fingerprint IS NOT NULL;"
    ) in sql


def test_private_route_loaders_are_capability_and_live_lease_bound() -> None:
    sql = _sql()
    catalog = _function(
        sql,
        "load_owned_catalog_connector_route(",
        "activate_catalog_generation_for_target(",
    )
    execution = _function(
        sql,
        "load_owned_execution_connector_route(",
        "load_owned_profile_connector_route(",
    )
    profile = sql.split(
        "CREATE FUNCTION schemabridge_control.load_owned_profile_connector_route(",
        maxsplit=1,
    )[1].split("REVOKE INSERT ON", maxsplit=1)[0]

    for loader in (catalog, execution, profile):
        assert "SECURITY DEFINER" in loader
        assert "SET search_path = pg_catalog, schemabridge_control" in loader
        assert "sha256(convert_to(p_lease_capability, 'UTF8'))" in loader
        assert "p_fencing_token" in loader
        assert "lease_expires_at > clock_timestamp()" in loader
        assert "head.status = 'enabled'" in loader
        assert "connection.status = 'enabled'" in loader
        assert "p_route_revision" in loader
        assert "p_target_fingerprint" in loader
    assert "private.capability = 'catalog'" in catalog
    assert "platform_instance varchar(200)" in catalog
    assert "connection.platform_instance" in catalog
    assert "refresh.lease_owner_id = p_indexer_id" in catalog
    assert "private.capability = 'execution'" in execution
    assert "p_job_workspace_id varchar" in execution
    assert "p_connector_workspace_id varchar" in execution
    assert "job.workspace_id = p_job_workspace_id" in execution
    assert "job.connector_workspace_id = p_connector_workspace_id" in execution
    assert "connection.workspace_id = job.connector_workspace_id" in execution
    assert "job.lease_owner_id = p_worker_id" in execution
    assert "job.authorization_expires_at > clock_timestamp()" in execution
    assert "private.capability = 'profile'" in profile
    assert "job.lease_owner_id = p_worker_id" in profile


def test_catalog_promotion_holds_exact_route_and_raw_capability_atomically() -> None:
    sql = _sql()
    activation = _function(
        sql,
        "activate_catalog_generation_for_target(",
        "load_owned_execution_connector_route(",
    )

    assert "SECURITY DEFINER" in activation
    assert "SET search_path = pg_catalog, schemabridge_control" in activation
    assert "p_indexer_id varchar" in activation
    assert "p_lease_capability varchar" in activation
    assert "sha256(convert_to(p_lease_capability, 'UTF8'))" in activation
    assert "refresh.lease_owner_id" in activation
    assert "IS DISTINCT FROM lease_capability_digest" in activation
    assert "refresh_record.fencing_token IS DISTINCT FROM p_fencing_token" in activation
    assert "refresh_record.lease_expires_at <= clock_timestamp()" in activation
    assert "head.contract_version <> p_contract_version" in activation
    assert "head.route_revision <> p_route_revision" in activation
    assert "head.target_fingerprint <> p_target_fingerprint" in activation
    connection_lock = activation.index(
        "FROM schemabridge_control.catalog_connections AS connection"
    )
    head_lock = activation.index("FROM schemabridge_control.connector_route_heads AS head")
    refresh_lock = activation.index("FROM schemabridge_control.catalog_refresh_runs AS refresh")
    generation_lock = activation.index(
        "FROM schemabridge_control.catalog_generations AS generation"
    )
    policy_lock = activation.index("FROM schemabridge_control.tenant_capacity_policies AS policy")
    final_lease_check = activation.index("refresh_record.lease_expires_at <= clock_timestamp()")
    promotion = activation.index("FROM schemabridge_control.activate_catalog_generation(")
    assert (
        connection_lock
        < head_lock
        < refresh_lock
        < generation_lock
        < policy_lock
        < final_lease_check
        < promotion
    )
    assert "FOR UPDATE;" in activation[connection_lock:head_lock]
    assert "FOR SHARE;" in activation[head_lock:refresh_lock]
    assert activation[refresh_lock:promotion].count("FOR UPDATE;") == 3


def test_catalog_change_capture_avoids_cold_statistics_nested_loop_plan() -> None:
    sql = _sql()

    assert (
        "ALTER FUNCTION schemabridge_control.capture_catalog_generation_change()\n"
        "    SET enable_nestloop = off;"
    ) in sql
    assert sql.count("ALTER FUNCTION schemabridge_control.capture_catalog_generation_change()") == 1


def test_runtime_preflight_route_is_current_target_bound_and_separate() -> None:
    sql = _sql()
    preflight = _function(
        sql,
        "load_current_preflight_connector_route(",
        "load_owned_catalog_connector_route(",
    )

    assert "SECURITY DEFINER" in preflight
    assert "SET search_path = pg_catalog, schemabridge_control" in preflight
    assert "SESSION_USER NOT IN ('schemabridge_runtime', 'schemabridge_migrator')" in preflight
    assert "private.capability = 'preflight'" in preflight
    assert "head.route_revision = p_route_revision" in preflight
    assert "head.target_fingerprint = p_target_fingerprint" in preflight
    assert "head.status = 'enabled'" in preflight
    assert "connection.status = 'enabled'" in preflight
    assert (
        "load_current_preflight_connector_route(" in sql
        and "TO schemabridge_runtime, schemabridge_migrator;" in sql
    )


def test_acl_retires_v4_bypass_and_exposes_no_private_table_reads() -> None:
    sql = _sql()

    assert (
        "REVOKE INSERT ON\n"
        "    schemabridge_control.catalog_connection_routes\n"
        "    FROM schemabridge_api;"
    ) in sql
    assert (
        "schemabridge_control.load_owned_catalog_connection_route(\n"
        "        varchar,\n"
        "        varchar,\n"
        "        varchar,\n"
        "        varchar,\n"
        "        varchar,\n"
        "        bigint\n"
        "    )\n"
        "    FROM schemabridge_catalog, schemabridge_migrator;"
    ) in sql
    assert (
        "schemabridge_control.activate_catalog_generation(\n"
        "        varchar,\n"
        "        varchar,\n"
        "        varchar,\n"
        "        bigint,\n"
        "        bigint,\n"
        "        bigint,\n"
        "        char,\n"
        "        char\n"
        "    )\n"
        "    FROM schemabridge_catalog;"
    ) in sql
    assert "FROM PUBLIC;" in sql
    private_grants = [
        statement
        for statement in sql.split(";")
        if "connector_private_route_revisions" in statement
        and statement.lstrip().upper().startswith("GRANT")
    ]
    assert private_grants == []
    assert (
        "load_owned_catalog_connector_route(" in sql
        and "TO schemabridge_catalog, schemabridge_migrator;" in sql
    )
    assert (
        "load_owned_execution_connector_route(" in sql
        and "load_owned_profile_connector_route(" in sql
        and "TO schemabridge_worker, schemabridge_migrator;" in sql
    )
    for forbidden_role in (
        "schemabridge_api",
        "schemabridge_runtime",
        "schemabridge_reconciler",
    ):
        private_execute_grants = [
            statement
            for statement in sql.split(";")
            if "load_owned_" in statement
            and statement.lstrip().upper().startswith("GRANT EXECUTE")
            and f"TO {forbidden_role}" in statement
        ]
        assert private_execute_grants == []
