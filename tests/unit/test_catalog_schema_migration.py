from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0004_dynamic_catalog_inventory.sql"
ROLES = ROOT / "demo/control_plane/init/001_roles.sql"


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_v4_is_additive_and_contains_the_dynamic_inventory_boundary() -> None:
    sql = _migration_sql()
    expected_tables = {
        "tenant_capacity_policies",
        "api_rate_limit_windows",
        "tenant_execution_capacity",
        "tenant_job_schedule",
        "catalog_connections",
        "catalog_connection_routes",
        "catalog_refresh_runs",
        "catalog_generations",
        "catalog_assets",
        "catalog_fields",
        "catalog_tombstones",
    }

    for table in expected_tables:
        assert f"CREATE TABLE schemabridge_control.{table}" in sql
    assert "CREATE ROLE" not in sql.upper()
    assert "CREATE INDEX CONCURRENTLY" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE " not in sql.upper()
    assert " OFFSET " not in sql.upper()
    assert "dsn" not in sql.lower()
    assert "password" not in sql.lower()
    assert "openai" not in sql.lower()
    assert "sample_value" not in sql.lower()
    assert "source_row" not in sql.lower()


def test_v4_has_compound_keyset_search_refresh_and_capacity_indexes() -> None:
    sql = _migration_sql()

    for index in (
        "api_rate_limit_windows_expiry_idx",
        "execution_jobs_fair_claim_idx",
        "catalog_connections_page_idx",
        "catalog_connections_all_page_idx",
        "catalog_refresh_one_active_idx",
        "catalog_refresh_claim_idx",
        "catalog_refresh_expired_lease_idx",
        "catalog_generations_retention_idx",
        "catalog_assets_keyset_idx",
        "catalog_assets_exact_lookup_idx",
        "catalog_assets_platform_filter_idx",
        "catalog_assets_schema_filter_idx",
        "catalog_assets_search_idx",
        "catalog_fields_keyset_idx",
        "catalog_fields_exact_lookup_idx",
        "catalog_fields_native_type_filter_idx",
        "catalog_fields_search_idx",
        "catalog_tombstones_resource_idx",
    ):
        assert f"CREATE INDEX {index}" in sql or f"CREATE UNIQUE INDEX {index}" in sql
    assert "USING gin (search_document)" in sql
    assert "catalog_terms_text(tags)" in sql
    assert "catalog_terms_text(glossary_terms)" in sql
    assert "CREATE FUNCTION schemabridge_control.catalog_terms_text(varchar[])" in sql
    assert "WHERE status IN ('requested', 'leased', 'staging')" in sql
    assert "WHERE status IN ('queued', 'retry_wait')" in sql
    assert "UNIQUE (workspace_id, connection_id, generation, asset_id)" in sql
    assert "field_path\n    )" in sql
    assert "target_generation bigint NOT NULL" in sql
    assert "UNIQUE (workspace_id, refresh_id)" in sql
    assert "NEW.base_generation := active_generation_value" in sql
    assert "NEW.target_generation := next_generation_value" in sql
    assert "NEW.refresh_mode = 'full'" in sql


def test_v4_job_capacity_is_transactional_and_idempotent_replay_is_rows_free() -> None:
    sql = _migration_sql()
    function = sql.split(
        "CREATE FUNCTION schemabridge_control.maintain_execution_job_capacity()",
        maxsplit=1,
    )[1].split("CREATE FUNCTION schemabridge_control.admit_api_request", maxsplit=1)[0]

    assert "AFTER INSERT OR UPDATE" in sql
    assert "ON schemabridge_control.execution_jobs" in sql
    assert "new_nonterminal AND NOT old_nonterminal" in function
    assert "old_nonterminal AND NOT new_nonterminal" in function
    assert "nonterminal_job_count = nonterminal_job_count + 1" in function
    assert "nonterminal_job_count = nonterminal_job_count - 1" in function
    assert "tenant capacity policy is unavailable" in function
    assert "tenant execution capacity is exhausted" in function
    # PostgreSQL AFTER INSERT does not fire for the existing job store's
    # INSERT ... ON CONFLICT DO NOTHING replay path.
    assert "ON CONFLICT (workspace_id) DO NOTHING" in function
    assert "INSERT INTO schemabridge_control.tenant_job_schedule" in function


def test_v4_rate_admission_returns_atomic_limit_and_usage_from_policy_lock() -> None:
    sql = _migration_sql()
    function = sql.split(
        "CREATE FUNCTION schemabridge_control.admit_api_request(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.lock_catalog_completion_scope(",
        maxsplit=1,
    )[0]

    assert "remaining integer,\n    request_limit integer,\n    used integer" in function
    assert "INTO configured_request_limit" in function
    assert "FOR SHARE" in function
    assert "configured_request_limit - observed_count" in function
    assert function.count("configured_request_limit") >= 8
    assert "configured_request_limit,\n        observed_count;" in function
    assert "WHERE rate_window.window_expires_at <= observed_at" in function
    assert "FOR UPDATE SKIP LOCKED\n        LIMIT 100" in function


def test_v4_policy_can_be_lowered_below_usage_while_new_admission_stays_closed() -> None:
    sql = _migration_sql()
    guard = sql.split(
        "CREATE FUNCTION schemabridge_control.guard_tenant_capacity_policy()",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.admit_catalog_connection(",
        maxsplit=1,
    )[0]

    assert "NEW.version <= OLD.version" in guard
    assert "NEW.updated_at <= OLD.updated_at" in guard
    assert "current_connections" not in guard
    assert "current_assets" not in guard
    assert "current_jobs" not in guard


def test_v4_inventory_shape_matches_public_domain_without_raw_values() -> None:
    sql = _migration_sql()
    assets = sql.split(
        "CREATE TABLE schemabridge_control.catalog_assets (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    fields = sql.split(
        "CREATE TABLE schemabridge_control.catalog_fields (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    for column in (
        "asset_key char(64)",
        "asset_id varchar(500)",
        "qualified_name varchar(500)",
        "platform varchar(100)",
        "environment varchar(100)",
        "field_count bigint",
        "metadata_fingerprint char(64)",
        "observed_at timestamptz",
    ):
        assert column in assets
    for column in (
        "asset_key char(64)",
        "field_key char(64)",
        "field_path varchar(200)[]",
        "native_type varchar(200)",
        "nullable boolean",
        "is_part_of_key boolean",
        "tags varchar(200)[]",
        "glossary_terms varchar(200)[]",
        "metadata_fingerprint char(64)",
        "observed_at timestamptz",
    ):
        assert column in fields
    for forbidden in ("sample", "source_row", "raw_value"):
        assert forbidden not in assets.lower()
        assert forbidden not in fields.lower()


def test_v4_activation_is_fenced_capacity_checked_and_cas_only() -> None:
    sql = _migration_sql()
    function = sql.split(
        "CREATE FUNCTION schemabridge_control.activate_catalog_generation(",
        maxsplit=1,
    )[1].split("CREATE FUNCTION schemabridge_control.prune_catalog_generations", maxsplit=1)[0]
    catalog_grants = sql.split(
        "GRANT SELECT ON\n"
        "    schemabridge_control.tenant_capacity_policies,\n"
        "    schemabridge_control.catalog_connections,",
        maxsplit=1,
    )[1]

    assert "SECURITY DEFINER" in function
    assert "SET search_path = pg_catalog, schemabridge_control" in function
    assert "lease_capability_digest" in function
    assert "fencing_token" in function
    assert "source_complete" in function
    assert "active_generation IS NOT DISTINCT FROM p_expected_base_generation" in function
    assert "policy_record.asset_limit" in function
    assert "policy_record.field_limit" in function
    assert "'full_reconciliation_absence'" in function
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "UPDATE ON\n    schemabridge_control.catalog_connections" not in catalog_grants


def test_v4_completion_lock_is_exact_ordered_and_catalog_execute_only() -> None:
    sql = _migration_sql()
    function = sql.split(
        "CREATE FUNCTION schemabridge_control.lock_catalog_completion_scope(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.activate_catalog_generation(",
        maxsplit=1,
    )[0]

    assert "RETURNS void" in function
    assert "SECURITY DEFINER" in function
    assert "SET search_path = pg_catalog, schemabridge_control" in function
    assert "SESSION_USER <> 'schemabridge_catalog'" in function
    assert "refresh_record.target_generation <> p_target_generation" in function
    assert "refresh_record.fencing_token <> p_fencing_token" in function
    assert "refresh_record.lease_capability_digest" in function
    assert "refresh_record.lease_expires_at <= observed_at" in function
    assert "NOT refresh_record.source_complete" in function
    connection_lock = function.index("FROM schemabridge_control.catalog_connections")
    refresh_lock = function.index("FROM schemabridge_control.catalog_refresh_runs")
    generation_lock = function.index("FROM schemabridge_control.catalog_generations")
    assert connection_lock < refresh_lock < generation_lock
    assert function.count("FOR UPDATE") == 3
    assert (
        "schemabridge_control.lock_catalog_completion_scope(\n"
        "        varchar,\n"
        "        varchar,\n"
        "        varchar,\n"
        "        bigint,\n"
        "        bigint,\n"
        "        char\n"
        "    )\n"
        "    TO schemabridge_catalog;"
    ) in sql


def test_v4_uses_only_an_opaque_route_binding_and_hides_it_from_api_reads() -> None:
    sql = _migration_sql()
    route_table = sql.split(
        "CREATE TABLE schemabridge_control.catalog_connection_routes (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    assert "credential_binding_ref" in route_table
    assert "credential_binding_ref !~ '://'" in route_table
    assert "credential_binding_ref !~ '@'" in route_table
    for forbidden in ("dsn", "password", "access_token", "secret_path", "credential_value"):
        assert forbidden not in route_table.lower()
    assert (
        "GRANT INSERT ON\n"
        "    schemabridge_control.catalog_connection_routes\n"
        "    TO schemabridge_api;"
    ) in sql
    api_route_grants = [
        statement
        for statement in sql.split(";")
        if "catalog_connection_routes" in statement and "TO schemabridge_api" in statement
    ]
    assert len(api_route_grants) == 1
    assert "GRANT INSERT ON" in api_route_grants[0]
    assert "GRANT SELECT" not in api_route_grants[0]
    catalog_route_grants = [
        statement
        for statement in sql.split(";")
        if "catalog_connection_routes" in statement and "TO schemabridge_catalog" in statement
    ]
    assert catalog_route_grants == []
    route_loader = sql.split(
        "CREATE FUNCTION schemabridge_control.load_owned_catalog_connection_route(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.guard_catalog_connection()",
        maxsplit=1,
    )[0]
    assert "SECURITY DEFINER" in route_loader
    assert "SET search_path = pg_catalog, schemabridge_control" in route_loader
    assert "SESSION_USER NOT IN ('schemabridge_catalog', 'schemabridge_migrator')" in route_loader
    assert "refresh.lease_owner_id = p_indexer_id" in route_loader
    assert "sha256(convert_to(p_lease_capability, 'UTF8'))" in route_loader
    assert "refresh.fencing_token = p_fencing_token" in route_loader
    assert "refresh.lease_expires_at > clock_timestamp()" in route_loader


def test_v4_api_refresh_access_is_public_projection_only() -> None:
    sql = _migration_sql()
    request = sql.split(
        "CREATE FUNCTION schemabridge_control.request_catalog_refresh(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_catalog_refresh_public(",
        maxsplit=1,
    )[0]
    public_load = sql.split(
        "CREATE FUNCTION schemabridge_control.load_catalog_refresh_public(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.load_owned_catalog_connection_route(",
        maxsplit=1,
    )[0]

    for function in (request, public_load):
        assert "SECURITY DEFINER" in function
        assert "SET search_path = pg_catalog, schemabridge_control" in function
        assert "SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator')" in function
    for protected in (
        "source_checkpoint",
        "source_page_fingerprint",
        "lease_owner_id",
        "lease_capability_digest",
        "fencing_token",
        "idempotency_digest",
        "requested_by_actor_id",
    ):
        assert protected not in public_load
    api_refresh_table_grants = [
        statement
        for statement in sql.split(";")
        if "catalog_refresh_runs" in statement and "TO schemabridge_api" in statement
    ]
    assert api_refresh_table_grants == []
    assert (
        "schemabridge_control.load_catalog_refresh_public(varchar, varchar)\n"
        "    TO schemabridge_api, schemabridge_migrator;"
    ) in sql


def test_v4_cursor_ttl_is_contract_fixed_and_source_page_size_is_runtime_owned() -> None:
    sql = _migration_sql()
    policy = sql.split(
        "CREATE TABLE schemabridge_control.tenant_capacity_policies (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]

    assert "source_page_size" not in policy
    assert "CHECK (catalog_cursor_ttl_seconds = 900)" in policy


def test_v4_api_reads_capacity_policy_but_cannot_provision_or_change_it() -> None:
    sql = _migration_sql()
    api_policy_grants = [
        statement
        for statement in sql.split(";")
        if "tenant_capacity_policies" in statement and "TO schemabridge_api" in statement
    ]

    assert len(api_policy_grants) == 1
    assert "GRANT SELECT ON" in api_policy_grants[0]
    assert "INSERT" not in api_policy_grants[0]
    assert "UPDATE" not in api_policy_grants[0]
    assert (
        "schemabridge_control.guard_tenant_capacity_policy()\n    TO schemabridge_migrator;"
    ) in sql
    admission = sql.split(
        "CREATE FUNCTION schemabridge_control.admit_catalog_connection(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.guard_catalog_connection()",
        maxsplit=1,
    )[0]
    assert "SECURITY DEFINER" in admission
    assert "SET search_path = pg_catalog, schemabridge_control" in admission
    assert "SESSION_USER NOT IN ('schemabridge_api', 'schemabridge_migrator')" in admission
    assert "FOR UPDATE" in admission
    assert "current_connections >= connection_capacity_limit" in admission
    assert (
        "schemabridge_control.admit_catalog_connection(varchar)\n"
        "    TO schemabridge_api, schemabridge_migrator;"
    ) in sql


def test_v4_disable_persists_one_terminal_idempotency_digest() -> None:
    sql = _migration_sql()
    connections = sql.split(
        "CREATE TABLE schemabridge_control.catalog_connections (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    guard = sql.split(
        "CREATE FUNCTION schemabridge_control.guard_catalog_connection()",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.reject_catalog_immutable_mutation()",
        maxsplit=1,
    )[0]

    assert "disabled_idempotency_digest char(64)" in connections
    assert "AND disabled_idempotency_digest IS NULL" in connections
    assert "AND disabled_idempotency_digest IS NOT NULL" in connections
    assert "NEW.disabled_idempotency_digest IS NULL" in guard
    assert (
        "NEW.disabled_idempotency_digest\n"
        "                IS DISTINCT FROM OLD.disabled_idempotency_digest"
    ) in guard
    assert (
        "status,\n    disabled_by_actor_id,\n    disabled_idempotency_digest,\n    disabled_at,"
    ) in sql


def test_nine_control_roles_are_non_privileged_non_inheriting_and_connected() -> None:
    roles = ROLES.read_text(encoding="utf-8")
    expected_roles = (
        "schemabridge_migrator",
        "schemabridge_runtime",
        "schemabridge_reconciler",
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_publisher",
        "schemabridge_catalog",
        "schemabridge_observer",
        "schemabridge_backup",
    )

    assert roles.count("CREATE ROLE schemabridge_") == len(expected_roles)
    assert roles.count("NOSUPERUSER") == len(expected_roles)
    assert roles.count("NOCREATEDB") == len(expected_roles)
    assert roles.count("NOCREATEROLE") == len(expected_roles)
    assert roles.count("NOINHERIT") == len(expected_roles)
    for role in expected_roles:
        assert f"CREATE ROLE {role}" in roles
        assert role in roles.split("GRANT CONNECT ON DATABASE schemabridge_control", maxsplit=1)[1]
    assert "ALTER ROLE schemabridge_publisher\n  SET statement_timeout = '30s';" in roles
