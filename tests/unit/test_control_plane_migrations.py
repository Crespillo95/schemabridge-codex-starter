from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPOSITORY_ROOT / "migrations" / "control_plane"


@dataclass
class _Cursor:
    rows: list[tuple[object, ...]]

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return tuple(self.rows)


@dataclass
class _FakeDatabase:
    schema_exists: bool = False
    history_table_exists: bool = False
    history: list[tuple[object, ...]] = field(default_factory=list)


@dataclass
class _FakeConnection:
    database: _FakeDatabase
    lock_available: bool = True
    ddl_failure: Exception | None = None
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)
    transaction_count: int = 0
    commits: int = 0
    rollbacks: int = 0

    def execute(
        self,
        query: str,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        self.statements.append((query, params))
        normalized = " ".join(query.split())
        if "pg_try_advisory_xact_lock" in normalized:
            return _Cursor([(self.lock_available,)])
        if normalized == "SELECT to_regclass(%s)":
            relation = (
                "schemabridge_control.schema_migrations"
                if self.database.history_table_exists
                else None
            )
            return _Cursor([(relation,)])
        if "FROM pg_catalog.pg_namespace" in normalized:
            return _Cursor([(self.database.schema_exists,)])
        if (
            normalized.startswith("SELECT version, name, checksum")
            and "schema_migrations" in normalized
        ):
            return _Cursor(list(self.database.history))
        if normalized.startswith("INSERT INTO schemabridge_control.schema_migrations"):
            assert params is not None
            self.database.history.append((params[0], params[1], params[2]))
            return _Cursor([])
        if "CREATE SCHEMA schemabridge_control" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            self.database.schema_exists = True
            self.database.history_table_exists = True
            return _Cursor([])
        if "CREATE TABLE schemabridge_control.execution_jobs" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "reject_expired_execution_job_success" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "CREATE TABLE schemabridge_control.tenant_capacity_policies" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "CREATE TABLE schemabridge_control.catalog_generation_changes" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "CREATE TABLE schemabridge_control.tenant_ai_policies" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "ai_attempt_terminal_usage_accounting_v7" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "historical AI settlement audit derivation is incompatible with schema v8" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        if "CREATE TABLE schemabridge_control.connector_contract_revisions" in query:
            if self.ddl_failure is not None:
                raise self.ddl_failure
            return _Cursor([])
        raise AssertionError(f"unexpected statement in fake connection: {normalized[:80]}")

    @contextmanager
    def transaction(self) -> Iterator[object]:
        self.transaction_count += 1
        snapshot = (
            self.database.schema_exists,
            self.database.history_table_exists,
            list(self.database.history),
        )
        try:
            yield object()
        except BaseException:
            (
                self.database.schema_exists,
                self.database.history_table_exists,
                history,
            ) = snapshot
            self.database.history = history
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


@dataclass
class _FakeConnectionFactory:
    connection: _FakeConnection
    calls: list[tuple[str, int]] = field(default_factory=list)

    @contextmanager
    def __call__(
        self,
        dsn: str,
        connect_timeout_seconds: int,
    ) -> Iterator[_FakeConnection]:
        self.calls.append((dsn, connect_timeout_seconds))
        yield self.connection


def _migrator(
    database: _FakeDatabase,
    *,
    lock_available: bool = True,
    ddl_failure: Exception | None = None,
    migrations_path: Path = _MIGRATIONS,
) -> tuple[PostgresControlPlaneMigrator, _FakeConnection, _FakeConnectionFactory]:
    connection = _FakeConnection(
        database,
        lock_available=lock_available,
        ddl_failure=ddl_failure,
    )
    factory = _FakeConnectionFactory(connection)
    migrator = PostgresControlPlaneMigrator(
        dsn="postgresql://not-logged.invalid/control",
        migrations_path=migrations_path,
        connect_timeout_seconds=4,
        connection_factory=factory,
    )
    return migrator, connection, factory


def _known_identity(version: int = 1) -> tuple[str, str]:
    migration_path = {
        1: _MIGRATIONS / "0001_initial_control_plane.sql",
        2: _MIGRATIONS / "0002_authenticated_api_jobs.sql",
        3: _MIGRATIONS / "0003_reject_expired_job_success.sql",
        4: _MIGRATIONS / "0004_dynamic_catalog_inventory.sql",
        5: _MIGRATIONS / "0005_semantic_change_management.sql",
        6: _MIGRATIONS / "0006_dynamic_query_studio.sql",
        7: _MIGRATIONS / "0007_harden_ai_usage_settlement.sql",
        8: _MIGRATIONS / "0008_serialize_ai_provider_accounting.sql",
        9: _MIGRATIONS / "0009_tenant_connector_routing.sql",
    }[version]
    return (
        migration_path.stem.split("_", maxsplit=1)[1],
        hashlib.sha256(migration_path.read_bytes()).hexdigest(),
    )


def _current_database() -> _FakeDatabase:
    name, checksum = _known_identity()
    return _FakeDatabase(
        schema_exists=True,
        history_table_exists=True,
        history=[
            (1, name, checksum),
            (2, *_known_identity(2)),
            (3, *_known_identity(3)),
            (4, *_known_identity(4)),
            (5, *_known_identity(5)),
            (6, *_known_identity(6)),
            (7, *_known_identity(7)),
            (8, *_known_identity(8)),
            (9, *_known_identity(9)),
        ],
    )


def _assert_no_ddl(connection: _FakeConnection) -> None:
    statements = "\n".join(query for query, _ in connection.statements).upper()
    for keyword in ("CREATE ", "ALTER ", "DROP ", "GRANT ", "REVOKE ", "INSERT "):
        assert keyword not in statements


def test_known_migrations_are_ordered_and_checksum_exact_file_bytes() -> None:
    migrator, connection, factory = _migrator(_FakeDatabase())

    known = migrator.known_migrations()

    assert [(item.version, item.name) for item in known] == [
        (1, "initial_control_plane"),
        (2, "authenticated_api_jobs"),
        (3, "reject_expired_job_success"),
        (4, "dynamic_catalog_inventory"),
        (5, "semantic_change_management"),
        (6, "dynamic_query_studio"),
        (7, "harden_ai_usage_settlement"),
        (8, "serialize_ai_provider_accounting"),
        (9, "tenant_connector_routing"),
    ]
    assert known[0].checksum == _known_identity()[1]
    assert known[1].checksum == _known_identity(2)[1]
    assert known[2].checksum == _known_identity(3)[1]
    assert known[3].checksum == _known_identity(4)[1]
    assert known[4].checksum == _known_identity(5)[1]
    assert known[5].checksum == _known_identity(6)[1]
    assert known[6].checksum == _known_identity(7)[1]
    assert known[7].checksum == _known_identity(8)[1]
    assert known[8].checksum == _known_identity(9)[1]
    assert factory.calls == []
    assert connection.statements == []


def test_inspect_pristine_database_reports_pending_without_ddl() -> None:
    migrator, connection, factory = _migrator(_FakeDatabase())

    inspection = migrator.inspect()

    assert inspection.current_version == 0
    assert inspection.expected_version == 9
    assert tuple(item.version for item in inspection.pending) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert not inspection.is_current
    assert factory.calls == [("postgresql://not-logged.invalid/control", 4)]
    _assert_no_ddl(connection)


def test_require_current_only_reads_and_rejects_database_behind() -> None:
    migrator, connection, _ = _migrator(_FakeDatabase())

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.require_current()

    assert raised.value.code is ControlPlaneMigrationErrorCode.SCHEMA_NOT_CURRENT
    assert "current=0, expected=9" in str(raised.value)
    _assert_no_ddl(connection)


def test_explicit_migrate_applies_all_pending_work_in_one_transaction() -> None:
    database = _FakeDatabase()
    migrator, connection, _ = _migrator(database)

    result = migrator.migrate()

    assert result.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert not result.already_current
    assert result.inspection.is_current
    assert connection.transaction_count == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert database.history == [
        (1, "initial_control_plane", _known_identity()[1]),
        (2, "authenticated_api_jobs", _known_identity(2)[1]),
        (3, "reject_expired_job_success", _known_identity(3)[1]),
        (4, "dynamic_catalog_inventory", _known_identity(4)[1]),
        (5, "semantic_change_management", _known_identity(5)[1]),
        (6, "dynamic_query_studio", _known_identity(6)[1]),
        (7, "harden_ai_usage_settlement", _known_identity(7)[1]),
        (8, "serialize_ai_provider_accounting", _known_identity(8)[1]),
        (9, "tenant_connector_routing", _known_identity(9)[1]),
    ]
    statements = [query for query, _ in connection.statements]
    assert "pg_try_advisory_xact_lock" in statements[0]
    assert sum("CREATE SCHEMA schemabridge_control" in query for query in statements) == 1
    assert (
        sum("INSERT INTO schemabridge_control.schema_migrations" in query for query in statements)
        == 9
    )


def test_exact_replay_obtains_lock_but_executes_no_ddl_or_history_insert() -> None:
    migrator, connection, _ = _migrator(_current_database())

    result = migrator.migrate()

    assert result.already_current
    assert result.inspection.is_current
    assert connection.commits == 1
    statements = "\n".join(query for query, _ in connection.statements)
    assert "pg_try_advisory_xact_lock" in statements
    assert "CREATE SCHEMA" not in statements
    assert "INSERT INTO schemabridge_control.schema_migrations" not in statements


def test_concurrent_migrator_fails_without_schema_mutation() -> None:
    database = _FakeDatabase()
    migrator, connection, _ = _migrator(database, lock_available=False)

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.migrate()

    assert raised.value.code is ControlPlaneMigrationErrorCode.LOCK_UNAVAILABLE
    assert not database.schema_exists
    assert database.history == []
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert all(
        "CREATE SCHEMA" not in query and "INSERT INTO" not in query
        for query, _ in connection.statements
    )


@pytest.mark.parametrize(
    ("history", "expected_code"),
    [
        (
            [(1, "initial_control_plane", "f" * 64)],
            ControlPlaneMigrationErrorCode.CHECKSUM_DRIFT,
        ),
        (
            [
                (1, *_known_identity()),
                (2, *_known_identity(2)),
                (3, *_known_identity(3)),
                (4, *_known_identity(4)),
                (5, *_known_identity(5)),
                (6, *_known_identity(6)),
                (7, *_known_identity(7)),
                (8, *_known_identity(8)),
                (9, *_known_identity(9)),
                (10, "future_release", "a" * 64),
            ],
            ControlPlaneMigrationErrorCode.SCHEMA_AHEAD,
        ),
        (
            [(1, "renamed_migration", _known_identity()[1])],
            ControlPlaneMigrationErrorCode.HISTORY_INVALID,
        ),
        (
            [(2, *_known_identity(2))],
            ControlPlaneMigrationErrorCode.HISTORY_INVALID,
        ),
    ],
)
def test_inspect_rejects_drift_future_and_malformed_history(
    history: list[tuple[object, ...]],
    expected_code: ControlPlaneMigrationErrorCode,
) -> None:
    database = _FakeDatabase(
        schema_exists=True,
        history_table_exists=True,
        history=history,
    )
    migrator, connection, _ = _migrator(database)

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.inspect()

    assert raised.value.code is expected_code
    _assert_no_ddl(connection)


def test_schema_without_history_is_rejected_as_incompatible() -> None:
    database = _FakeDatabase(schema_exists=True, history_table_exists=False)
    migrator, connection, _ = _migrator(database)

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.inspect()

    assert raised.value.code is ControlPlaneMigrationErrorCode.INCOMPATIBLE_SCHEMA
    _assert_no_ddl(connection)


def test_interrupted_migration_rolls_back_and_redacts_external_error() -> None:
    database = _FakeDatabase()
    raw_secret = "postgresql://admin:secret@private.invalid/control"
    migrator, connection, _ = _migrator(
        database,
        ddl_failure=RuntimeError(f"syntax failed near {raw_secret}"),
    )

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.migrate()

    assert raised.value.code is ControlPlaneMigrationErrorCode.APPLY_FAILED
    assert str(raised.value) == "control-plane migration failed and was rolled back"
    assert raw_secret not in str(raised.value)
    assert not database.schema_exists
    assert not database.history_table_exists
    assert database.history == []
    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("files", "expected_fragment"),
    [
        ({"0002_second.sql": "SELECT 1;"}, "contiguous"),
        ({"bad-name.sql": "SELECT 1;"}, "filename"),
        ({"0001_first.sql": ""}, "size"),
    ],
)
def test_invalid_local_migration_sets_fail_before_connecting(
    tmp_path: Path,
    files: dict[str, str],
    expected_fragment: str,
) -> None:
    for name, payload in files.items():
        (tmp_path / name).write_text(payload, encoding="utf-8")
    migrator, connection, factory = _migrator(
        _FakeDatabase(),
        migrations_path=tmp_path,
    )

    with pytest.raises(ControlPlaneMigrationError) as raised:
        migrator.inspect()

    assert raised.value.code is ControlPlaneMigrationErrorCode.MIGRATION_SET_INVALID
    assert expected_fragment in str(raised.value)
    assert factory.calls == []
    assert connection.statements == []


def test_initial_sql_contains_governed_state_and_existing_role_grants_only() -> None:
    sql = (_MIGRATIONS / "0001_initial_control_plane.sql").read_text(encoding="utf-8")

    expected_tables = {
        "schema_migrations",
        "registry_active_pointers",
        "registry_activation_transitions",
        "registry_reconciliation_outbox",
        "control_audit_events",
        "agent_workflow_drafts",
        "workflow_access_grants",
        "analytical_request_drafts",
        "review_drafts",
        "review_decisions",
        "review_publications",
        "join_review_drafts",
        "join_review_decisions",
        "join_publications",
        "publication_approval_identity",
        "publication_target_audit",
        "identity_bindings",
        "identity_rotation_plans",
        "identity_rotation_bindings",
        "legacy_control_imports",
        "legacy_control_import_items",
        "control_quarantine_items",
    }
    for table in expected_tables:
        assert f"CREATE TABLE schemabridge_control.{table}" in sql
    assert "CREATE ROLE" not in sql.upper()
    assert "schemabridge_control_runtime" not in sql
    assert "schemabridge_control_reconciler" not in sql
    assert "schemabridge_control_migrator" not in sql
    assert "TO schemabridge_runtime" in sql
    assert "TO schemabridge_reconciler" in sql
    assert "AUTHORIZATION schemabridge_migrator" in sql
    assert "preview_rows" not in sql
    assert "execution_row_count" in sql
    assert "execution_preview_fingerprint" in sql
    identity_bindings_sql = sql.split(
        "CREATE TABLE schemabridge_control.identity_bindings (",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "UNIQUE (binding_kind, opaque_id, key_version)" not in identity_bindings_sql
    assert (
        "UNIQUE (\n"
        "        workspace_id,\n"
        "        binding_kind,\n"
        "        opaque_id,\n"
        "        key_version\n"
        "    )"
    ) in identity_bindings_sql


def test_initial_sql_hardens_legacy_import_and_quarantine_lifecycles() -> None:
    sql = (_MIGRATIONS / "0001_initial_control_plane.sql").read_text(encoding="utf-8")

    assert "CREATE FUNCTION schemabridge_control.enforce_legacy_control_import_lifecycle()" in sql
    assert (
        "CREATE TRIGGER legacy_control_imports_lifecycle\n"
        "    BEFORE INSERT OR UPDATE OR DELETE\n"
        "    ON schemabridge_control.legacy_control_imports"
    ) in sql
    assert "OLD.status <> 'dry_run' OR NEW.status <> 'completed'" in sql
    assert "NEW.counts_json IS DISTINCT FROM OLD.counts_json" in sql
    assert "NEW.completed_at < NEW.approved_at" in sql

    assert (
        "CREATE TRIGGER control_quarantine_items_lifecycle\n"
        "    BEFORE INSERT OR UPDATE OR DELETE\n"
        "    ON schemabridge_control.control_quarantine_items"
    ) in sql
    assert "new quarantine records must be unresolved" in sql
    assert "quarantine records cannot be changed" in sql

    assert (
        "GRANT SELECT, INSERT ON\n"
        "    schemabridge_control.legacy_control_imports,\n"
        "    schemabridge_control.control_quarantine_items\n"
        "    TO schemabridge_runtime;"
    ) in sql
    assert (
        "GRANT UPDATE (\n"
        "    approval_id,\n"
        "    actor,\n"
        "    approved_at,\n"
        "    status,\n"
        "    completed_at\n"
        ") ON schemabridge_control.legacy_control_imports"
    ) in sql
    broad_runtime_grant = sql.split(
        "GRANT SELECT, INSERT, UPDATE ON",
        maxsplit=1,
    )[1].split("TO schemabridge_runtime;", maxsplit=1)[0]
    assert "legacy_control_imports" not in broad_runtime_grant
    assert "control_quarantine_items" not in broad_runtime_grant
