"""PostgreSQL integration proof for the authoritative M23 control plane."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_identity_rotation import (
    PostgresIdentityRotationStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_operations import (
    PostgresControlPlaneBackup,
    PostgresControlPlaneRestore,
)
from schemabridge.adapters.control_plane.postgres_registry_control import (
    PostgresRegistryControlStore,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.identity_rotation import (
    ApproveIdentityRotation,
    CompleteIdentityRotation,
    InitializeVerifiedIdentityState,
    PrepareIdentityRotation,
)
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
    ControlPlaneMigrationErrorCode,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
    ControlPlaneOperationErrorCode,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.application.registry_control import (
    CommitRegistryActivation,
    InspectRegistryReconciliation,
    PrepareRegistryActivation,
    PrepareRegistryActivationApproval,
    PrepareRegistryReconciliationApproval,
    PrepareRegistryRollback,
    ReconcileRegistryProjection,
)
from schemabridge.domain.control_plane_operations import ControlPlaneBackupManifest
from schemabridge.domain.identity import AuthenticationMethod
from schemabridge.domain.identity_rotation import (
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    VerifiedDualKeyOidcDerivation,
    VerifiedOidcKeyDerivation,
)
from schemabridge.domain.registry_control import (
    GovernedRegistryVersion,
    RegistryActivationConfirmation,
    RegistryActivationTransition,
    RegistryControlCommit,
    RegistryProjectionOutboxItem,
    RegistryProjectionOutboxStatus,
    RegistryProjectionOutcome,
    RegistryProjectionState,
    RegistryReconciliationApproval,
    RegistryReconciliationConfirmation,
    RegistryVersionTrust,
    build_registry_activation_transition,
    build_registry_projection_outbox,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    prepare_datahub_registry_version,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
RUNTIME_DSN = (
    "postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/schemabridge_control"
)
RECONCILER_DSN = (
    "postgresql://schemabridge_reconciler:schemabridge_reconciler"
    "@127.0.0.1:55434/schemabridge_control"
)
MIGRATOR_DSN = (
    "postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/schemabridge_control"
)
AUDIT_KEYS = {"v1": b"control-audit-key-0123456789-abcdef"}
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
ADMIN_DSN = os.environ.get(
    "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:55434/postgres",
)


class _VersionReader:
    def __init__(self, versions: dict[int, GovernedRegistryVersion]) -> None:
        self._versions = versions

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        loaded = self._versions.get(version)
        if loaded is None or loaded.snapshot.scope != scope:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_UNAVAILABLE,
                "strict test registry version is unavailable",
            )
        return loaded


class _StaticProjection:
    def __init__(self, state: RegistryProjectionState) -> None:
        self.state = state
        self.writes: list[RegistryProjectionState] = []

    def read(self, scope: SemanticRegistryScope) -> RegistryProjectionState | None:
        return self.state if self.state.pointer.scope == scope else None

    def project(
        self,
        desired: RegistryProjectionState,
        approval: RegistryReconciliationApproval,
    ) -> RegistryProjectionState:
        del approval
        self.writes.append(desired)
        self.state = desired
        return desired


def _dsn(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


@pytest.fixture(scope="module", autouse=True)
def _require_current_control_schema() -> None:
    PostgresControlPlaneMigrator(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL", MIGRATOR_DSN),
        MIGRATIONS,
    ).require_current()


def _scope() -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id=f"integration-{uuid4().hex}",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )


def _stores() -> tuple[PostgresRegistryControlStore, PostgresRegistryControlStore]:
    runtime = PostgresRegistryControlStore(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN),
        AUDIT_KEYS,
        "v1",
    )
    reconciler = PostgresRegistryControlStore(
        _dsn("SCHEMABRIDGE_TEST_CONTROL_RECONCILER_DATABASE_URL", RECONCILER_DSN),
        AUDIT_KEYS,
        "v1",
    )
    return runtime, reconciler


class _DockerPostgresTools:
    """Exercise the pinned container tools without exposing DSNs in argv."""

    def __call__(
        self,
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> int:
        tool = Path(command[0]).name
        postgres_environment = {
            name: value for name, value in environment.items() if name.startswith("PG")
        }
        postgres_environment["PGHOST"] = "127.0.0.1"
        postgres_environment["PGPORT"] = "5432"
        docker_environment = [
            item
            for name, value in sorted(postgres_environment.items())
            for item in ("-e", f"{name}={value}")
        ]
        if tool == "pg_dump":
            output_argument = next(item for item in command if item.startswith("--file="))
            output = Path(output_argument.removeprefix("--file="))
            arguments = [item for item in command[1:] if not item.startswith("--file=")]
            result = subprocess.run(
                (
                    "docker",
                    "exec",
                    *docker_environment,
                    "schemabridge-control-postgres",
                    "pg_dump",
                    *arguments,
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            if result.returncode == 0:
                output.write_bytes(result.stdout)
            return result.returncode
        if tool == "pg_restore":
            archive = Path(command[-1])
            result = subprocess.run(
                (
                    "docker",
                    "exec",
                    "-i",
                    *docker_environment,
                    "schemabridge-control-postgres",
                    "pg_restore",
                    *command[1:-1],
                ),
                input=archive.read_bytes(),
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            return result.returncode
        raise AssertionError("unexpected PostgreSQL operator tool")


@contextmanager
def _fresh_restore_database() -> Iterator[str]:
    database = f"schemabridge_restore_{uuid4().hex[:20]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
    try:
        yield (
            f"postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/{database}"
        )
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


@pytest.fixture
def fresh_restore_dsn() -> Iterator[str]:
    with _fresh_restore_database() as dsn:
        yield dsn


@pytest.fixture
def fresh_backup_source_dsns() -> Iterator[tuple[str, str, str]]:
    """Provide an isolated migrated source so backup tests never capture operator state."""

    database = f"schemabridge_backup_{uuid4().hex[:20]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
    migrator_dsn = (
        f"postgresql://schemabridge_migrator:schemabridge_migrator@127.0.0.1:55434/{database}"
    )
    runtime_dsn = (
        f"postgresql://schemabridge_runtime:schemabridge_runtime@127.0.0.1:55434/{database}"
    )
    reconciler_dsn = (
        f"postgresql://schemabridge_reconciler:schemabridge_reconciler@127.0.0.1:55434/{database}"
    )
    try:
        migrated = PostgresControlPlaneMigrator(migrator_dsn, MIGRATIONS).migrate()
        assert migrated.inspection.is_current
        yield migrator_dsn, runtime_dsn, reconciler_dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _versions(scope: SemanticRegistryScope) -> _VersionReader:
    recorded = RecordedGovernedSemanticRegistry(MANIFEST, scope).load().registry
    return _VersionReader(
        {version: _governed_version(recorded, scope, version) for version in (1, 2, 3)}
    )


def _governed_version(
    recorded: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    version: int,
) -> GovernedRegistryVersion:
    numbered = GovernedSemanticRegistrySnapshot.model_validate(
        {**recorded.model_dump(mode="python"), "version": version}
    )
    published = prepare_datahub_registry_version(numbered, scope)
    return GovernedRegistryVersion(
        snapshot=ScopedSemanticRegistrySnapshot(scope=scope, registry=published),
        publication_approval_id=f"integration-publication-v{version}",
        trust=RegistryVersionTrust.STRICT,
    )


def _confirmation(action: str) -> RegistryActivationConfirmation:
    return {
        "activate": RegistryActivationConfirmation.ACTIVATE_APPROVED_REGISTRY_VERSION,
        "rollback": RegistryActivationConfirmation.ROLLBACK_TO_APPROVED_REGISTRY_VERSION,
    }[action]


def _activate(
    store: PostgresRegistryControlStore,
    versions: _VersionReader,
    scope: SemanticRegistryScope,
    version: int,
    *,
    approved_at: datetime,
) -> RegistryControlCommit:
    proposal = PrepareRegistryActivation(store, versions, scope).execute(version)
    approval = PrepareRegistryActivationApproval().execute(
        proposal,
        actor="integration-registry-operator",
        approved_at=approved_at,
        confirmation=_confirmation(proposal.action.value),
    )
    return CommitRegistryActivation(store, versions).execute(
        proposal,
        approval,
        committed_at=approved_at + timedelta(seconds=1),
    )


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _opaque_id(kind: str, key_version: str, label: str) -> str:
    return f"sb_{kind}_{key_version}_{_digest(f'{key_version}:{label}')}"


def _identity_pair(namespace: str) -> VerifiedDualKeyOidcDerivation:
    workspace_reference = _digest(f"{namespace}:workspace-reference")
    actor_reference = _digest(f"{namespace}:actor-reference")
    verification = _digest(f"{namespace}:verification")
    provenance = _digest(f"{namespace}:provenance")

    def derivation(key_version: str) -> VerifiedOidcKeyDerivation:
        return VerifiedOidcKeyDerivation(
            verification_id=verification,
            workspace_reference_digest=workspace_reference,
            actor_reference_digest=actor_reference,
            workspace_id=_opaque_id("workspace", key_version, namespace),
            actor_id=_opaque_id("actor", key_version, namespace),
            key_version=key_version,
            provenance_fingerprint=provenance,
            provenance_version=1,
            policy_version=1,
            verified_at=NOW + timedelta(hours=1),
            authentication_method=AuthenticationMethod.OIDC,
        )

    return VerifiedDualKeyOidcDerivation(
        previous=derivation("v1"),
        current=derivation("v2"),
    )


def _seed_completed_identity_rotation(
    runtime_dsn: str,
) -> tuple[VerifiedDualKeyOidcDerivation, str]:
    pair = _identity_pair(f"backup-identity-{uuid4().hex}")
    store = PostgresIdentityRotationStore(runtime_dsn, AUDIT_KEYS, "v1")
    initialized = InitializeVerifiedIdentityState(store).execute(
        (pair,),
        evidence_fingerprint=_digest(f"backup-evidence:{pair.previous.workspace_id}"),
        actor=pair.previous.actor_id,
        approved_at=NOW + timedelta(hours=1),
        confirmation=IdentityInitializationConfirmation.INITIALIZE_VERIFIED_OIDC_BINDINGS,
    )
    assert initialized.active_key_version == "v1"
    plan = PrepareIdentityRotation(store).execute(pair.previous.workspace_id, (pair,))
    approved = ApproveIdentityRotation(store).execute(
        plan,
        actor=pair.previous.actor_id,
        approved_at=NOW + timedelta(hours=1, minutes=1),
        confirmation=IdentityRotationConfirmation.ROTATE_VERIFIED_OIDC_BINDINGS,
    )
    completed = CompleteIdentityRotation(store).execute(
        plan,
        approved.approval,
        completed_at=NOW + timedelta(hours=1, minutes=2),
    )
    assert completed.completion.approved.plan.new_workspace_id == pair.current.workspace_id
    return pair, plan.id


def _seed_quarantine(runtime_dsn: str) -> tuple[str, str]:
    suffix = uuid4().hex
    import_id = f"backup-import-{suffix}"
    quarantine_id = f"backup-quarantine-{suffix}"
    source_digest = _digest(f"backup-source:{suffix}")
    schema_digest = _digest(f"backup-schema:{suffix}")
    plan_digest = _digest(f"backup-plan:{suffix}")
    resource_digest = _digest(f"backup-resource:{suffix}")
    payload_digest = _digest(f"backup-payload:{suffix}")
    with psycopg.connect(runtime_dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.legacy_control_imports (
                import_id, source_kind, source_fingerprint,
                source_schema_fingerprint, plan_fingerprint,
                approval_id, actor, approved_at, status,
                counts_json, created_at, completed_at
            ) VALUES (
                %s, 'sqlite', %s, %s, %s,
                NULL, NULL, NULL, 'dry_run',
                %s::jsonb, %s, NULL
            )
            """,
            (
                import_id,
                source_digest,
                schema_digest,
                plan_digest,
                json.dumps(
                    {
                        "total": 1,
                        "imported": 0,
                        "quarantined": 1,
                        "skipped": 0,
                        "preview_rows_stripped": 0,
                    }
                ),
                NOW + timedelta(hours=2),
            ),
        )
        connection.execute(
            """
            UPDATE schemabridge_control.legacy_control_imports
            SET approval_id = %s,
                actor = %s,
                approved_at = %s,
                status = 'completed',
                completed_at = %s
            WHERE import_id = %s
            """,
            (
                f"backup-import-approval-{suffix}",
                "integration-platform-admin",
                NOW + timedelta(hours=2, minutes=1),
                NOW + timedelta(hours=2, minutes=2),
                import_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.legacy_control_import_items (
                import_id, source_table, source_id_digest, outcome,
                target_type, target_id, reason_code,
                payload_fingerprint, created_at
            ) VALUES (
                %s, 'agent_workflow_drafts', %s, 'quarantined',
                NULL, NULL, 'orphan_workflow', %s, %s
            )
            """,
            (
                import_id,
                resource_digest,
                payload_digest,
                NOW + timedelta(hours=2, minutes=2),
            ),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.control_quarantine_items (
                quarantine_id, import_id, workspace_id, resource_type,
                resource_id_digest, reason_code, payload_fingerprint,
                details_json, detected_at, resolved_at, resolution_event_id
            ) VALUES (
                %s, %s, NULL, 'workflow_draft',
                %s, 'orphan_workflow', %s,
                %s::jsonb, %s, NULL, NULL
            )
            """,
            (
                quarantine_id,
                import_id,
                resource_digest,
                payload_digest,
                json.dumps(
                    {
                        "resource_kind": "workflow_draft",
                        "source_id_digest": resource_digest,
                        "source_table": "agent_workflow_drafts",
                    }
                ),
                NOW + timedelta(hours=2, minutes=2),
            ),
        )
    return import_id, quarantine_id


def _restore_coverage_state(
    dsn: str,
    *,
    identity_workspace_id: str,
    rotation_plan_id: str,
    import_id: str,
    quarantine_id: str,
) -> dict[str, tuple[str, ...]]:
    with psycopg.connect(dsn) as connection:

        def json_rows(query: str, parameters: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(str(row[0]) for row in connection.execute(query, parameters).fetchall())

        return {
            "identity_bindings": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.identity_bindings
                    WHERE workspace_id = %s
                    ORDER BY binding_id
                ) AS row_data
                """,
                (identity_workspace_id,),
            ),
            "identity_rotation_plans": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.identity_rotation_plans
                    WHERE plan_id = %s
                ) AS row_data
                """,
                (rotation_plan_id,),
            ),
            "identity_rotation_bindings": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.identity_rotation_bindings
                    WHERE plan_id = %s
                    ORDER BY binding_kind, stable_reference_digest
                ) AS row_data
                """,
                (rotation_plan_id,),
            ),
            "legacy_control_imports": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.legacy_control_imports
                    WHERE import_id = %s
                ) AS row_data
                """,
                (import_id,),
            ),
            "legacy_control_import_items": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.legacy_control_import_items
                    WHERE import_id = %s
                    ORDER BY sequence
                ) AS row_data
                """,
                (import_id,),
            ),
            "control_quarantine_items": json_rows(
                """
                SELECT to_jsonb(row_data)::text
                FROM (
                    SELECT *
                    FROM schemabridge_control.control_quarantine_items
                    WHERE quarantine_id = %s
                ) AS row_data
                """,
                (quarantine_id,),
            ),
        }


@dataclass(frozen=True, slots=True)
class _PreparedControlBackup:
    archive: Path
    manifest_path: Path
    manifest: ControlPlaneBackupManifest
    source_state: dict[str, tuple[str, ...]]
    identity_pair: VerifiedDualKeyOidcDerivation
    rotation_plan_id: str
    import_id: str
    quarantine_id: str
    tools: _DockerPostgresTools


@pytest.fixture
def prepared_control_backup(
    tmp_path: Path,
    fresh_backup_source_dsns: tuple[str, str, str],
) -> _PreparedControlBackup:
    operator_dsn, runtime_dsn, _ = fresh_backup_source_dsns
    scope = _scope()
    runtime = PostgresRegistryControlStore(runtime_dsn, AUDIT_KEYS, "v1")
    versions = _versions(scope)
    for offset, version in enumerate((1, 2, 3)):
        _activate(
            runtime,
            versions,
            scope,
            version,
            approved_at=NOW + timedelta(minutes=offset),
        )
    identity_pair, rotation_plan_id = _seed_completed_identity_rotation(runtime_dsn)
    import_id, quarantine_id = _seed_quarantine(runtime_dsn)
    source_state = _restore_coverage_state(
        operator_dsn,
        identity_workspace_id=identity_pair.previous.workspace_id,
        rotation_plan_id=rotation_plan_id,
        import_id=import_id,
        quarantine_id=quarantine_id,
    )
    tools = _DockerPostgresTools()
    archive, manifest_path, manifest = PostgresControlPlaneBackup(
        operator_dsn,
        MIGRATIONS,
        AUDIT_KEYS,
        "v1",
        runner=tools,
        clock=lambda: NOW + timedelta(hours=4),
    ).create_backup(tmp_path / "owner-only")
    return _PreparedControlBackup(
        archive=archive,
        manifest_path=manifest_path,
        manifest=manifest,
        source_state=source_state,
        identity_pair=identity_pair,
        rotation_plan_id=rotation_plan_id,
        import_id=import_id,
        quarantine_id=quarantine_id,
        tools=tools,
    )


def test_control_roles_are_separated_and_runtime_cannot_create_schema_objects() -> None:
    runtime_dsn = _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN)
    reconciler_dsn = _dsn(
        "SCHEMABRIDGE_TEST_CONTROL_RECONCILER_DATABASE_URL",
        RECONCILER_DSN,
    )
    migrator_dsn = _dsn(
        "SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL",
        MIGRATOR_DSN,
    )

    observed: dict[str, tuple[str, bool]] = {}
    for label, dsn in (
        ("runtime", runtime_dsn),
        ("reconciler", reconciler_dsn),
        ("migrator", migrator_dsn),
    ):
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                """
                SELECT current_user,
                       has_schema_privilege(
                           current_user,
                           'schemabridge_control',
                           'CREATE'
                       )
                """
            ).fetchone()
        assert row is not None
        observed[label] = (str(row[0]), bool(row[1]))

    assert observed == {
        "runtime": ("schemabridge_runtime", False),
        "reconciler": ("schemabridge_reconciler", False),
        "migrator": ("schemabridge_migrator", True),
    }

    with (
        psycopg.connect(runtime_dsn) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        connection.transaction(),
    ):
        connection.execute("CREATE TABLE schemabridge_control.forbidden_runtime_ddl (id integer)")
    for role in (
        "schemabridge_runtime",
        "schemabridge_reconciler",
        "schemabridge_migrator",
    ):
        with (
            pytest.raises(psycopg.OperationalError),
            psycopg.connect(
                f"postgresql://{role}:{role}@127.0.0.1:55433/schemabridge",
                connect_timeout=2,
            ),
        ):
            pass


def test_control_roles_have_exact_outbox_and_immutable_ledger_capabilities() -> None:
    runtime_dsn = _dsn("SCHEMABRIDGE_TEST_CONTROL_DATABASE_URL", RUNTIME_DSN)
    reconciler_dsn = _dsn(
        "SCHEMABRIDGE_TEST_CONTROL_RECONCILER_DATABASE_URL",
        RECONCILER_DSN,
    )
    with psycopg.connect(runtime_dsn) as connection:
        runtime = connection.execute(
            """
            SELECT
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'SELECT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_import_items',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_import_items',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_import_items',
                    'DELETE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'UPDATE'
                ),
                has_any_column_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'UPDATE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'status',
                    'UPDATE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'source_fingerprint',
                    'UPDATE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'counts_json',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.legacy_control_imports',
                    'DELETE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.control_quarantine_items',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.control_quarantine_items',
                    'UPDATE'
                ),
                has_any_column_privilege(
                    current_user,
                    'schemabridge_control.control_quarantine_items',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.control_quarantine_items',
                    'DELETE'
                )
            """
        ).fetchone()
    assert runtime == (
        True,
        True,
        False,
        True,
        False,
        False,
        True,
        False,
        True,
        True,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
    )

    with psycopg.connect(reconciler_dsn) as connection:
        reconciler = connection.execute(
            """
            SELECT
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'SELECT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'DELETE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'status',
                    'UPDATE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'desired_projection_json',
                    'UPDATE'
                ),
                has_column_privilege(
                    current_user,
                    'schemabridge_control.registry_reconciliation_outbox',
                    'workspace_id',
                    'UPDATE'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.control_audit_events',
                    'INSERT'
                ),
                has_table_privilege(
                    current_user,
                    'schemabridge_control.control_audit_events',
                    'UPDATE'
                )
            """
        ).fetchone()
        assert reconciler == (True, False, False, True, False, False, True, False)
        connection.execute(
            """
            UPDATE schemabridge_control.registry_reconciliation_outbox
            SET status = status
            WHERE false
            """
        )
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            connection.transaction(),
        ):
            connection.execute(
                """
                UPDATE schemabridge_control.registry_reconciliation_outbox
                SET desired_projection_json = desired_projection_json
                WHERE false
                """
            )


def test_two_live_migrators_have_one_lock_winner(
    tmp_path: Path,
    fresh_restore_dsn: str,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    original = (MIGRATIONS / "0001_initial_control_plane.sql").read_text(encoding="utf-8")
    (migrations / "0001_initial_control_plane.sql").write_text(
        "SELECT pg_sleep(0.5);\n" + original,
        encoding="utf-8",
    )
    barrier = Barrier(2)

    def migrate() -> object:
        barrier.wait()
        return PostgresControlPlaneMigrator(
            fresh_restore_dsn,
            migrations,
        ).migrate()

    results: list[object] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(migrate) for _ in range(2))
        for future in futures:
            try:
                results.append(future.result())
            except ControlPlaneMigrationError as error:
                results.append(error)

    successes = [result for result in results if not isinstance(result, ControlPlaneMigrationError)]
    failures = [result for result in results if isinstance(result, ControlPlaneMigrationError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ControlPlaneMigrationErrorCode.LOCK_UNAVAILABLE
    inspection = PostgresControlPlaneMigrator(
        fresh_restore_dsn,
        migrations,
    ).require_current()
    assert inspection.current_version == 1


def test_concurrent_initial_activations_have_one_atomic_cas_winner() -> None:
    scope = _scope()
    store, _ = _stores()
    versions = _versions(scope)
    proposals = (
        PrepareRegistryActivation(store, versions, scope).execute(2),
        PrepareRegistryActivation(store, versions, scope).execute(3),
    )
    transitions: list[tuple[RegistryActivationTransition, RegistryProjectionOutboxItem]] = []
    for offset, proposal in enumerate(proposals):
        approved_at = NOW + timedelta(seconds=offset)
        approval = PrepareRegistryActivationApproval().execute(
            proposal,
            actor=f"integration-operator-{offset}",
            approved_at=approved_at,
            confirmation=_confirmation(proposal.action.value),
        )
        transition = build_registry_activation_transition(
            proposal,
            approval,
            previous_pointer=None,
            committed_at=approved_at + timedelta(seconds=2),
        )
        transitions.append((transition, build_registry_projection_outbox(transition)))

    def commit(index: int) -> RegistryControlCommit:
        transition, outbox = transitions[index]
        return store.commit_transition(transition, outbox)

    successes: list[RegistryControlCommit] = []
    failures: list[RegistryControlError] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(commit, index) for index in range(2))
        for future in futures:
            try:
                successes.append(future.result())
            except RegistryControlError as error:
                failures.append(error)

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is RegistryControlErrorCode.CAS_CONFLICT
    winner = successes[0]
    assert store.load_active(scope) == winner.transition.active_pointer
    assert store.list_transitions(scope) == (winner.transition,)
    assert store.load_pending_outbox(scope) == winner.outbox
    assert store.load_transition_outbox(scope, winner.transition.id) == winner.outbox
    replay = store.commit_transition(winner.transition, winner.outbox)
    assert replay.replayed is True
    assert replay.transition == winner.transition
    assert replay.outbox == winner.outbox
    verification = store.verify_audit_chain(scope.workspace_id)
    assert verification.valid is True
    assert verification.event_count == 1
    with (
        psycopg.connect(
            _dsn(
                "SCHEMABRIDGE_TEST_CONTROL_MIGRATOR_DATABASE_URL",
                MIGRATOR_DSN,
            )
        ) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
        connection.transaction(),
    ):
        connection.execute(
            """
            UPDATE schemabridge_control.control_audit_events
            SET operation = 'registry_rollback'
            WHERE transition_id = %s
            """,
            (winner.transition.id,),
        )


def test_rollback_and_reconciliation_are_durable_idempotent_and_audited() -> None:
    scope = _scope()
    runtime, reconciler = _stores()
    versions = _versions(scope)
    first = _activate(runtime, versions, scope, 2, approved_at=NOW)
    second = _activate(
        runtime,
        versions,
        scope,
        3,
        approved_at=NOW + timedelta(minutes=1),
    )
    proposal = PrepareRegistryRollback(runtime, versions, scope).execute(first.transition.id)
    approval = PrepareRegistryActivationApproval().execute(
        proposal,
        actor="integration-rollback-operator",
        approved_at=NOW + timedelta(minutes=2),
        confirmation=_confirmation(proposal.action.value),
    )
    rollback = CommitRegistryActivation(runtime, versions).execute(
        proposal,
        approval,
        committed_at=NOW + timedelta(minutes=2, seconds=1),
    )

    assert second.transition.active_pointer.generation == 2
    assert rollback.transition.active_pointer.generation == 3
    assert rollback.transition.active_pointer.registry_version == 2
    assert tuple(
        item.active_pointer.registry_version for item in runtime.list_transitions(scope)
    ) == (2, 3, 2)
    pending = reconciler.load_pending_outbox(scope)
    assert pending == rollback.outbox
    outcome = RegistryProjectionOutcome(
        outbox_id=pending.id,
        transition_id=pending.transition_id,
        scope=scope,
        generation=pending.desired.pointer.generation,
        status=RegistryProjectionOutboxStatus.DELIVERED,
        observed_projection=pending.desired,
        occurred_at=NOW + timedelta(minutes=3),
    )
    reconciler.record_projection_outcome(outcome)
    reconciler.record_projection_outcome(outcome)

    delivered = reconciler.load_transition_outbox(scope, rollback.transition.id)
    assert delivered is not None
    assert delivered.status is RegistryProjectionOutboxStatus.DELIVERED
    assert delivered.attempts == 1
    newest_pending = reconciler.load_pending_outbox(scope)
    assert newest_pending is not None
    assert newest_pending.desired.pointer.generation == 2
    verification = reconciler.verify_audit_chain(scope.workspace_id)
    assert verification.valid is True
    assert verification.event_count == 4

    projection = _StaticProjection(rollback.outbox.desired)
    report = InspectRegistryReconciliation(
        reconciler,
        versions,
        projection,
        scope,
    ).execute(inspected_at=NOW + timedelta(minutes=4))
    reconciliation_approval = PrepareRegistryReconciliationApproval().execute(
        report,
        actor="integration-reconciliation-operator",
        approved_at=NOW + timedelta(minutes=5),
        confirmation=RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION,
    )
    superseded = ReconcileRegistryProjection(
        reconciler,
        versions,
        projection,
    ).execute(
        report,
        reconciliation_approval,
        occurred_at=NOW + timedelta(minutes=6),
    )

    assert projection.writes == []
    assert superseded.status is RegistryProjectionOutboxStatus.SUPERSEDED
    assert superseded.generation == second.transition.active_pointer.generation
    superseded_outbox = reconciler.load_transition_outbox(scope, second.transition.id)
    assert superseded_outbox is not None
    assert superseded_outbox.status is RegistryProjectionOutboxStatus.SUPERSEDED
    assert reconciler.verify_audit_chain(scope.workspace_id).event_count == 5


def test_signed_backup_restores_exact_state_into_a_fresh_database(
    fresh_restore_dsn: str,
    prepared_control_backup: _PreparedControlBackup,
) -> None:
    evidence = prepared_control_backup
    assert len(evidence.source_state["identity_bindings"]) == 4
    assert len(evidence.source_state["identity_rotation_plans"]) == 1
    assert len(evidence.source_state["identity_rotation_bindings"]) == 2
    assert len(evidence.source_state["control_quarantine_items"]) == 1
    assert evidence.archive.stat().st_mode & 0o077 == 0
    assert evidence.manifest_path.stat().st_mode & 0o077 == 0
    manifest = evidence.manifest
    assert manifest.table_counts["schema_migrations"] == 9
    assert manifest.table_counts["execution_jobs"] == 0
    assert manifest.table_counts["execution_job_events"] == 0
    assert manifest.table_counts["registry_active_pointers"] == 1
    assert manifest.table_counts["registry_activation_transitions"] == 3
    assert manifest.table_counts["control_audit_events"] == 6
    assert manifest.table_counts["identity_bindings"] == 4
    assert manifest.table_counts["identity_rotation_plans"] == 1
    assert manifest.table_counts["identity_rotation_bindings"] == 2
    assert manifest.table_counts["legacy_control_imports"] == 1
    assert manifest.table_counts["legacy_control_import_items"] == 1
    assert manifest.table_counts["control_quarantine_items"] == 1
    restore = PostgresControlPlaneRestore(
        fresh_restore_dsn,
        MIGRATIONS,
        AUDIT_KEYS,
        runner=evidence.tools,
        clock=lambda: NOW + timedelta(hours=5),
    )

    verification = restore.restore_backup(evidence.archive, evidence.manifest_path)

    assert verification.schema_version == 9
    assert verification.schema_checksum == manifest.schema_checksum
    assert verification.state_sha256 == manifest.state_sha256
    assert verification.table_counts == manifest.table_counts
    assert verification.active_pointers == 1
    assert verification.transition_records == 3
    assert verification.audited_workspaces == 2
    assert verification.audit_events == 6
    assert verification.pending_outbox_records == 3
    assert verification.quarantine_records == 1
    restored_state = _restore_coverage_state(
        fresh_restore_dsn,
        identity_workspace_id=evidence.identity_pair.previous.workspace_id,
        rotation_plan_id=evidence.rotation_plan_id,
        import_id=evidence.import_id,
        quarantine_id=evidence.quarantine_id,
    )
    assert restored_state == evidence.source_state
    restored_identities = PostgresIdentityRotationStore(
        fresh_restore_dsn,
        AUDIT_KEYS,
        "v1",
    )
    restored_identity_state = restored_identities.load_state(
        evidence.identity_pair.current.workspace_id
    )
    assert restored_identity_state.active_key_version == "v2"
    assert restored_identity_state.previous_key_versions == ("v1",)
    assert restored_identities.load_completion(evidence.rotation_plan_id) is not None


def test_restore_rejects_nonfresh_target_before_runner_and_preserves_relation(
    fresh_restore_dsn: str,
    prepared_control_backup: _PreparedControlBackup,
) -> None:
    with psycopg.connect(fresh_restore_dsn) as connection:
        connection.execute(
            """
            CREATE TABLE public.restore_sentinel (
                id integer PRIMARY KEY,
                marker text NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO public.restore_sentinel (id, marker) VALUES (1, 'unchanged')"
        )
    with psycopg.connect(fresh_restore_dsn) as connection:
        before = connection.execute(
            "SELECT id, marker FROM public.restore_sentinel ORDER BY id"
        ).fetchall()
    runner_calls = 0

    def forbidden_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> int:
        nonlocal runner_calls
        del command, environment, timeout_seconds
        runner_calls += 1
        raise AssertionError("restore runner must not execute for a non-fresh target")

    restore = PostgresControlPlaneRestore(
        fresh_restore_dsn,
        MIGRATIONS,
        AUDIT_KEYS,
        runner=forbidden_runner,
    )
    with pytest.raises(ControlPlaneOperationError) as non_fresh:
        restore.restore_backup(
            prepared_control_backup.archive,
            prepared_control_backup.manifest_path,
        )
    assert non_fresh.value.code is ControlPlaneOperationErrorCode.TARGET_NOT_FRESH
    assert runner_calls == 0
    with psycopg.connect(fresh_restore_dsn) as connection:
        after = connection.execute(
            "SELECT id, marker FROM public.restore_sentinel ORDER BY id"
        ).fetchall()
        schema = connection.execute("SELECT to_regnamespace('schemabridge_control')").fetchone()
    assert after == before == [(1, "unchanged")]
    assert schema == (None,)


def test_restore_reports_verification_failed_after_post_restore_state_tampering(
    fresh_restore_dsn: str,
    prepared_control_backup: _PreparedControlBackup,
) -> None:
    runner_calls = 0

    def tampering_runner(
        command: Sequence[str],
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> int:
        nonlocal runner_calls
        runner_calls += 1
        return_code = prepared_control_backup.tools(
            command,
            environment,
            timeout_seconds,
        )
        if return_code == 0:
            with psycopg.connect(fresh_restore_dsn) as connection:
                updated = connection.execute(
                    """
                    UPDATE schemabridge_control.identity_rotation_plans
                    SET updated_at = updated_at + interval '1 second'
                    WHERE plan_id = %s
                    """,
                    (prepared_control_backup.rotation_plan_id,),
                )
                assert updated.rowcount == 1
        return return_code

    restore = PostgresControlPlaneRestore(
        fresh_restore_dsn,
        MIGRATIONS,
        AUDIT_KEYS,
        runner=tampering_runner,
        clock=lambda: NOW + timedelta(hours=6),
    )
    with pytest.raises(ControlPlaneOperationError) as verification_failed:
        restore.restore_backup(
            prepared_control_backup.archive,
            prepared_control_backup.manifest_path,
        )
    assert verification_failed.value.code is ControlPlaneOperationErrorCode.VERIFICATION_FAILED
    assert runner_calls == 1
