"""Real PostgreSQL proof for the M26 semantic-change scan queue."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.semantic_change.postgres_scans import (
    PostgresSemanticChangeScanStore,
)
from schemabridge.adapters.semantic_change.postgres_store import (
    PostgresSemanticChangeStore,
)
from schemabridge.application.ports.semantic_change_scans import (
    SemanticChangeScanInspection,
    SemanticChangeScanStoreError,
    SemanticChangeScanStoreErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.semantic_change import (
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticDependencyIndexState,
    SemanticEvidenceObservation,
    SemanticImpactKind,
    SemanticImpactSet,
    build_semantic_change_report,
    classify_semantic_change_findings,
)
from schemabridge.domain.semantic_change_scans import (
    SemanticChangeScanFailureCode,
    SemanticChangeScanRequest,
    SemanticChangeScanSourceKind,
    SemanticChangeScanStatus,
    digest_semantic_change_scan_capability,
    semantic_change_scan_retry_delay,
)
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
DEFAULT_ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
AUDIT_KEY = b"semantic-change-scan-audit-key-0123456789abcdef"
CAPABILITY = "semantic-scan-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OTHER_CAPABILITY = "other-scan-capability-9876543210-zyxwvutsrqponmlkjihgfedcba"


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    reconciler: str


def _database_clock(dsn: str) -> datetime:
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, datetime)
    return value


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture
def scan_database() -> Iterator[_DatabaseUrls]:
    """Create a pristine schema-v5 database for each isolated lifecycle proof."""

    database = f"schemabridge_scans_{uuid4().hex[:12]}"
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL",
        DEFAULT_ADMIN_DSN,
    )
    urls = _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
    )
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                sql.Identifier(database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL(
                """
                GRANT CONNECT ON DATABASE {} TO
                    schemabridge_migrator,
                    schemabridge_reconciler
                """
            ).format(sql.Identifier(database))
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.inspection.current_version == 13
        yield urls
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


@dataclass(frozen=True)
class _FixedDependencies:
    impacts: SemanticImpactSet

    def load_state(self, scope: SemanticRegistryScope) -> SemanticDependencyIndexState:
        return SemanticDependencyIndexState(
            scope=scope,
            watermark=self.impacts.watermark,
            fingerprint=self.impacts.dependency_index_fingerprint,
            complete=self.impacts.complete,
        )

    def resolve_impacts(
        self,
        context: SemanticChangeInspectionContext,
        findings: tuple[SemanticChangeFinding, ...],
    ) -> SemanticImpactSet:
        del context
        assert tuple(item.id for item in findings) == tuple(
            item.finding_ids[0] for item in self.impacts.impacts
        )
        return self.impacts


def _inspection(
    scope: SemanticRegistryScope,
    *,
    transition_id: str,
    pointer_generation: int = 7,
    catalog_generation: int = 12,
) -> SemanticChangeScanInspection:
    mapping = GovernedMappingRef(
        logical_field=LogicalFieldRef("Customer.customer_id"),
        physical_field=PhysicalFieldRef("crm.customers.customer_id"),
        version=1,
        approval_decision_id="mapping-decision-1",
        physical_type=PhysicalValueType.STRING,
    )
    dependency = SemanticDependencyIndexState(
        scope=scope,
        watermark=3,
        fingerprint="d" * 64,
        complete=True,
    )
    context = SemanticChangeInspectionContext.create(
        scope=scope,
        pointer_generation=pointer_generation,
        pointer_fingerprint="b" * 64,
        pointer_transition_id=transition_id,
        registry_version=4,
        registry_fingerprint="c" * 64,
        mappings=(mapping,),
        joins=(),
        dependency_index=dependency,
    )
    generations = CatalogGenerationVector.create(
        (
            CatalogGenerationObservation(
                connection_id=CatalogConnectionId("warehouse-primary"),
                generation=catalog_generation,
                inventory_fingerprint="e" * 64,
            ),
        )
    )
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=scope.workspace_id,
            connection_id=CatalogConnectionId("warehouse-primary"),
            asset_id=CatalogAssetId("crm.customers"),
        ),
        field_path=("customer_id",),
    )
    binding = GovernedResourceBinding.create(
        mapping=mapping,
        locator=locator,
        catalog_generation=catalog_generation,
        catalog_generation_fingerprint=generations.fingerprint,
        asset_metadata_fingerprint="1" * 64,
        field_metadata_fingerprint="2" * 64,
        field_definition_fingerprint="3" * 64,
        field_terms_fingerprint="4" * 64,
    )
    evidence = ObservedFieldEvidence.create(
        mapping=mapping,
        binding=binding,
        present=True,
        candidate_count=1,
        normalized_type=PhysicalValueType.STRING,
        nullable=False,
        is_part_of_key=True,
        asset_metadata_fingerprint="1" * 64,
        field_metadata_fingerprint="2" * 64,
        field_definition_fingerprint="3" * 64,
        field_terms_fingerprint="4" * 64,
        reason_code=None,
    )
    observation = SemanticEvidenceObservation.create(
        context=context,
        catalog_generations=generations,
        fields=(evidence,),
        joins=(),
        observed_at=NOW,
        complete=True,
    )
    findings = classify_semantic_change_findings(observation, None)
    impacts = SemanticImpactSet.create(
        impacts=(
            SemanticChangeImpact.create(
                kind=SemanticImpactKind.MAPPING,
                artifact_id=mapping.logical_field.root,
                artifact_version=mapping.version,
                finding_ids=tuple(item.id for item in findings),
            ),
        ),
        complete=True,
        watermark=dependency.watermark,
        dependency_index_fingerprint=dependency.fingerprint,
    )
    return SemanticChangeScanInspection(
        report=build_semantic_change_report(observation, None, impacts),
        observation=observation,
    )


def _stores(
    urls: _DatabaseUrls,
    inspection: SemanticChangeScanInspection,
) -> PostgresSemanticChangeScanStore:
    dependencies = _FixedDependencies(
        SemanticImpactSet.create(
            impacts=(
                SemanticChangeImpact.create(
                    kind=SemanticImpactKind.MAPPING,
                    artifact_id=inspection.report.context.mappings[0].logical_field.root,
                    artifact_version=inspection.report.context.mappings[0].version,
                    finding_ids=tuple(item.id for item in inspection.report.findings),
                ),
            ),
            complete=inspection.report.impacts.complete,
            watermark=inspection.report.impacts.watermark,
            dependency_index_fingerprint=(inspection.report.impacts.dependency_index_fingerprint),
        )
    )
    report_store = PostgresSemanticChangeStore(
        urls.reconciler,
        dependencies,
        {"v1": AUDIT_KEY},
        "v1",
    )
    return PostgresSemanticChangeScanStore(urls.reconciler, report_store)


def _scope() -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id=f"workspace-scan-{uuid4().hex[:12]}",
        catalog_scope="synthetic-demo",
        registry_id="enterprise_registry",
    )


def _registry_request(
    scope: SemanticRegistryScope,
    *,
    generation: int = 7,
    fingerprint_seed: str | None = None,
    max_attempts: int = 5,
    requested_at: datetime | None = None,
) -> SemanticChangeScanRequest:
    seed = fingerprint_seed or f"{scope.workspace_id}:{generation}"
    return SemanticChangeScanRequest.requested(
        workspace_id=scope.workspace_id,
        source_kind=SemanticChangeScanSourceKind.REGISTRY_POINTER,
        source_event_key=f"registry-transition-{generation}-{_digest(seed)[:12]}",
        source_fingerprint=_digest(f"scan:{seed}"),
        catalog_scope=scope.catalog_scope,
        registry_id=scope.registry_id,
        registry_generation=generation,
        requested_at=requested_at or datetime.now(UTC) - timedelta(seconds=30),
        max_attempts=max_attempts,
    )


def _catalog_request(
    scope: SemanticRegistryScope,
    *,
    generation: int,
    requested_at: datetime,
) -> SemanticChangeScanRequest:
    fingerprint = _digest(f"catalog:{scope.workspace_id}:{generation}")
    return SemanticChangeScanRequest.requested(
        workspace_id=scope.workspace_id,
        source_kind=SemanticChangeScanSourceKind.CATALOG_GENERATION,
        source_event_key=f"warehouse-primary:{generation}",
        source_fingerprint=fingerprint,
        catalog_scope=scope.catalog_scope,
        registry_id=scope.registry_id,
        connection_id="warehouse-primary",
        base_catalog_generation=generation - 1,
        observed_catalog_generation=generation,
        requested_at=requested_at,
    )


def _seed_request(dsn: str, request: SemanticChangeScanRequest) -> None:
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.semantic_change_scan_requests (
                scan_id, workspace_id, source_kind, source_event_key,
                source_fingerprint, catalog_scope, registry_id,
                registry_generation, connection_id, base_catalog_generation,
                observed_catalog_generation, status, attempts, max_attempts,
                available_at, fencing_token, requested_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                request.scan_id,
                request.workspace_id,
                request.source_kind.value,
                request.source_event_key,
                request.source_fingerprint,
                request.catalog_scope,
                request.registry_id,
                request.registry_generation,
                request.connection_id,
                request.base_catalog_generation,
                request.observed_catalog_generation,
                request.status.value,
                request.attempts,
                request.max_attempts,
                request.available_at,
                request.fencing_token,
                request.requested_at,
                request.updated_at,
            ),
        )


def _seed_transition(
    dsn: str,
    inspection: SemanticChangeScanInspection,
) -> None:
    context = inspection.report.context
    target = datahub_registry_document_urn(
        context.scope,
        context.registry_version,
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.registry_activation_transitions (
                transition_id, workspace_id, catalog_scope, registry_id,
                generation, action, expected_generation,
                expected_registry_version, expected_registry_fingerprint,
                expected_transition_id, target_registry_version,
                target_registry_fingerprint, target_registry_urn,
                target_publication_approval_id, rollback_transition_id,
                proposal_fingerprint, approval_id, actor, approved_at,
                decision_ids_json, committed_at, payload_json
            ) VALUES (
                %s, %s, %s, %s, %s, 'activate', 0, NULL, NULL, NULL,
                %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s::jsonb, %s,
                '{}'::jsonb
            )
            """,
            (
                context.pointer_transition_id,
                context.scope.workspace_id,
                context.scope.catalog_scope,
                context.scope.registry_id,
                context.pointer_generation,
                context.registry_version,
                context.registry_fingerprint,
                target,
                f"publication-{uuid4().hex}",
                _digest(f"proposal:{context.pointer_transition_id}"),
                f"approval-scan-{uuid4().hex}",
                "sb_registry_publisher_v1",
                NOW,
                json.dumps(["mapping-decision-1"]),
                NOW,
            ),
        )


def test_claim_skips_locked_row_hashes_capability_and_hides_other_tenant(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    inspection = _inspection(scope, transition_id="registry-transition-7")
    store = _stores(scan_database, inspection)
    first = _registry_request(scope, generation=7, fingerprint_seed="first")
    second = _registry_request(
        scope,
        generation=8,
        fingerprint_seed="second",
        requested_at=first.requested_at + timedelta(microseconds=1),
    )
    _seed_request(scan_database.migrator, first)
    _seed_request(scan_database.migrator, second)

    with psycopg.connect(scan_database.migrator) as blocker:
        blocker.execute(
            """
            SELECT 1
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s AND scan_id = %s
            FOR UPDATE
            """,
            (scope.workspace_id, first.scan_id),
        )
        claimed = store.claim_next(
            reconciler_id="semantic-reconciler-a",
            lease_capability=CAPABILITY,
            lease_duration=timedelta(seconds=30),
        )
        assert claimed is not None
    assert claimed.scan_id == second.scan_id

    assert store.load("workspace-other", second.scan_id) is None
    with pytest.raises(SemanticChangeScanStoreError) as cross_tenant:
        store.heartbeat(
            "workspace-other",
            second.scan_id,
            reconciler_id="semantic-reconciler-a",
            lease_capability=CAPABILITY,
            fencing_token=claimed.fencing_token,
            lease_duration=timedelta(seconds=30),
        )
    assert cross_tenant.value.code is SemanticChangeScanStoreErrorCode.NOT_FOUND
    with pytest.raises(SemanticChangeScanStoreError) as stale:
        store.heartbeat(
            scope.workspace_id,
            second.scan_id,
            reconciler_id="semantic-reconciler-a",
            lease_capability=OTHER_CAPABILITY,
            fencing_token=claimed.fencing_token,
            lease_duration=timedelta(seconds=30),
        )
    assert stale.value.code is SemanticChangeScanStoreErrorCode.LEASE_LOST

    with psycopg.connect(scan_database.migrator) as connection:
        row = connection.execute(
            """
            SELECT lease_capability_digest
            FROM schemabridge_control.semantic_change_scan_requests
            WHERE workspace_id = %s AND scan_id = %s
            """,
            (scope.workspace_id, second.scan_id),
        ).fetchone()
    assert row == (digest_semantic_change_scan_capability(CAPABILITY),)
    assert CAPABILITY not in json.dumps(claimed.model_dump(mode="json"))


def test_crashed_lease_retries_then_reclaims_with_a_higher_fence(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    inspection = _inspection(scope, transition_id="registry-transition-7")
    store = _stores(scan_database, inspection)
    request = _registry_request(scope)
    _seed_request(scan_database.migrator, request)
    with psycopg.connect(scan_database.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.semantic_change_scan_requests
            SET status = 'leased',
                attempts = 1,
                lease_owner_id = 'crashed-reconciler',
                lease_capability_digest = %s,
                fencing_token = 1,
                lease_acquired_at = statement_timestamp() - interval '3 seconds',
                lease_heartbeat_at = statement_timestamp() - interval '3 seconds',
                lease_expires_at = statement_timestamp() - interval '1 second',
                updated_at = statement_timestamp() - interval '3 seconds'
            WHERE workspace_id = %s AND scan_id = %s
            """,
            (
                digest_semantic_change_scan_capability(CAPABILITY),
                scope.workspace_id,
                request.scan_id,
            ),
        )

    assert store.reclaim_expired(limit=10, retention=timedelta(days=30)) == 1
    retry = store.load(scope.workspace_id, request.scan_id)
    assert retry is not None
    assert retry.status is SemanticChangeScanStatus.RETRY_WAIT
    assert retry.failure is not None
    assert retry.failure.code is SemanticChangeScanFailureCode.LEASE_EXPIRED
    with pytest.raises(SemanticChangeScanStoreError) as stale:
        store.heartbeat(
            scope.workspace_id,
            request.scan_id,
            reconciler_id="crashed-reconciler",
            lease_capability=CAPABILITY,
            fencing_token=1,
            lease_duration=timedelta(seconds=30),
        )
    assert stale.value.code is SemanticChangeScanStoreErrorCode.LEASE_LOST

    wait_seconds = max(
        0.0,
        (retry.available_at - datetime.now(UTC)).total_seconds(),
    )
    time.sleep(wait_seconds + 0.05)
    replacement = store.claim_next(
        reconciler_id="semantic-reconciler-b",
        lease_capability=OTHER_CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert replacement is not None
    assert replacement.scan_id == request.scan_id
    assert replacement.attempts == replacement.fencing_token == 2
    with pytest.raises(SemanticChangeScanStoreError) as stale_fence:
        store.heartbeat(
            scope.workspace_id,
            request.scan_id,
            reconciler_id="semantic-reconciler-b",
            lease_capability=OTHER_CAPABILITY,
            fencing_token=1,
            lease_duration=timedelta(seconds=30),
        )
    assert stale_fence.value.code is SemanticChangeScanStoreErrorCode.LEASE_LOST
    current = store.heartbeat(
        scope.workspace_id,
        request.scan_id,
        reconciler_id="semantic-reconciler-b",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=2,
        lease_duration=timedelta(seconds=30),
    )
    assert current.fencing_token == 2


def test_retry_and_terminal_failure_are_closed_and_database_timed(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    inspection = _inspection(scope, transition_id="registry-transition-7")
    store = _stores(scan_database, inspection)
    retry_request = _registry_request(scope, fingerprint_seed="retry")
    _seed_request(scan_database.migrator, retry_request)
    claimed = store.claim_next(
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None
    caller_failed_at = NOW
    before_failure = _database_clock(scan_database.reconciler)
    retry = store.fail(
        scope.workspace_id,
        claimed.scan_id,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        code=SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
        failed_at=caller_failed_at,
        retry_at=caller_failed_at + semantic_change_scan_retry_delay(1),
        retain_until=None,
    )
    after_failure = _database_clock(scan_database.reconciler)
    assert retry.status is SemanticChangeScanStatus.RETRY_WAIT
    assert before_failure <= retry.updated_at <= after_failure
    assert retry.failure is not None
    assert retry.failure.occurred_at == retry.updated_at
    assert retry.available_at == retry.updated_at + semantic_change_scan_retry_delay(1)

    terminal_scope = _scope()
    terminal_request = _registry_request(
        terminal_scope,
        fingerprint_seed="terminal",
        max_attempts=1,
    )
    _seed_request(scan_database.migrator, terminal_request)
    terminal_claim = store.claim_next(
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert terminal_claim is not None
    assert terminal_claim.scan_id == terminal_request.scan_id
    before_terminal_failure = _database_clock(scan_database.reconciler)
    terminal = store.fail(
        terminal_scope.workspace_id,
        terminal_claim.scan_id,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=terminal_claim.fencing_token,
        code=SemanticChangeScanFailureCode.EVIDENCE_UNAVAILABLE,
        failed_at=caller_failed_at,
        retry_at=None,
        retain_until=caller_failed_at + timedelta(days=30),
    )
    after_terminal_failure = _database_clock(scan_database.reconciler)
    assert terminal.status is SemanticChangeScanStatus.FAILED
    assert before_terminal_failure <= terminal.updated_at <= after_terminal_failure
    assert terminal.completed_at is not None
    assert terminal.completed_at == terminal.updated_at
    assert terminal.failure is not None
    assert terminal.failure.occurred_at == terminal.updated_at
    assert terminal.retain_until == terminal.completed_at + timedelta(days=30)


def test_waiting_and_leased_obsolete_requests_bind_exact_successor(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    inspection = _inspection(scope, transition_id="registry-transition-7")
    store = _stores(scan_database, inspection)
    first = _registry_request(scope, generation=7, fingerprint_seed="gen-7")
    second = _registry_request(scope, generation=8, fingerprint_seed="gen-8")
    _seed_request(scan_database.migrator, first)
    _seed_request(scan_database.migrator, second)

    assert store.supersede_obsolete(limit=10, retention=timedelta(days=30)) == 1
    superseded = store.load(scope.workspace_id, first.scan_id)
    assert superseded is not None
    assert superseded.status is SemanticChangeScanStatus.SUPERSEDED
    assert superseded.superseded_by is not None
    assert superseded.superseded_by.scan_id == second.scan_id
    assert store.load_latest_for_scope(scope) == second

    claimed = store.claim_next(
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None
    assert claimed.scan_id == second.scan_id
    third = _registry_request(scope, generation=9, fingerprint_seed="gen-9")
    _seed_request(scan_database.migrator, third)
    leased_superseded = store.supersede_if_obsolete(
        scope.workspace_id,
        second.scan_id,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        retention=timedelta(days=30),
    )
    assert leased_superseded.status is SemanticChangeScanStatus.SUPERSEDED
    assert leased_superseded.superseded_by is not None
    assert leased_superseded.superseded_by.scan_id == third.scan_id
    assert store.load_latest_for_scope(scope) == third


def test_completion_is_atomic_database_timed_and_exactly_replayable(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    transition_id = f"registry-transition-{uuid4().hex}"
    inspection = _inspection(
        scope,
        transition_id=transition_id,
        pointer_generation=1,
    )
    store = _stores(scan_database, inspection)
    _seed_transition(scan_database.migrator, inspection)
    request = _registry_request(scope, generation=1)
    _seed_request(scan_database.migrator, request)
    claimed = store.claim_next(
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None

    before_completion = datetime.now(UTC)
    completed = store.complete(
        scope.workspace_id,
        request.scan_id,
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        fencing_token=claimed.fencing_token,
        inspection=inspection,
        completed_at=NOW,
        retain_until=NOW + timedelta(days=30),
    )
    assert completed.status is SemanticChangeScanStatus.COMPLETED
    assert completed.completed_at is not None
    assert before_completion <= completed.completed_at <= datetime.now(UTC)
    assert completed.completion is not None
    assert completed.completion.report_id == inspection.report.id
    assert store.load_latest_for_scope(scope) == completed

    catalog_request = _catalog_request(
        scope,
        generation=999,
        requested_at=datetime.now(UTC),
    )
    _seed_request(scan_database.migrator, catalog_request)
    assert store.load_latest_for_scope(scope) == completed
    other_registry = scope.model_copy(update={"registry_id": "other_registry"})
    assert store.load_latest_for_scope(other_registry) is None

    replay = store.complete(
        scope.workspace_id,
        request.scan_id,
        reconciler_id="semantic-reconciler-replay",
        lease_capability=OTHER_CAPABILITY,
        fencing_token=claimed.fencing_token,
        inspection=inspection,
        completed_at=NOW,
        retain_until=NOW + timedelta(days=30),
    )
    assert replay == completed

    different = _inspection(
        scope,
        transition_id=transition_id,
        pointer_generation=1,
        catalog_generation=13,
    )
    with pytest.raises(SemanticChangeScanStoreError) as mismatch:
        store.complete(
            scope.workspace_id,
            request.scan_id,
            reconciler_id="semantic-reconciler-replay",
            lease_capability=OTHER_CAPABILITY,
            fencing_token=claimed.fencing_token,
            inspection=different,
            completed_at=NOW,
            retain_until=NOW + timedelta(days=30),
        )
    assert mismatch.value.code is SemanticChangeScanStoreErrorCode.STATE_CONFLICT

    with psycopg.connect(scan_database.migrator) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.semantic_change_reports
                 WHERE workspace_id = %s),
                (SELECT count(*) FROM schemabridge_control.semantic_change_findings
                 WHERE workspace_id = %s),
                (SELECT count(*) FROM schemabridge_control.semantic_change_impacts
                 WHERE workspace_id = %s)
            """,
            (scope.workspace_id, scope.workspace_id, scope.workspace_id),
        ).fetchone()
    assert counts == (1, 1, 1)


def test_report_insert_failure_rolls_back_every_evidence_row_and_scan_state(
    scan_database: _DatabaseUrls,
) -> None:
    scope = _scope()
    transition_id = f"registry-transition-{uuid4().hex}"
    inspection = _inspection(
        scope,
        transition_id=transition_id,
        pointer_generation=1,
    )
    store = _stores(scan_database, inspection)
    _seed_transition(scan_database.migrator, inspection)
    request = _registry_request(scope, generation=1)
    _seed_request(scan_database.migrator, request)
    claimed = store.claim_next(
        reconciler_id="semantic-reconciler-a",
        lease_capability=CAPABILITY,
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None

    with psycopg.connect(scan_database.migrator) as connection:
        connection.execute(
            """
            CREATE FUNCTION schemabridge_control.fail_scan_finding_test()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'synthetic finding failure';
            END;
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER fail_scan_finding_test
            BEFORE INSERT ON schemabridge_control.semantic_change_findings
            FOR EACH ROW
            EXECUTE FUNCTION schemabridge_control.fail_scan_finding_test()
            """
        )
    try:
        with pytest.raises(SemanticChangeScanStoreError):
            store.complete(
                scope.workspace_id,
                request.scan_id,
                reconciler_id="semantic-reconciler-a",
                lease_capability=CAPABILITY,
                fencing_token=claimed.fencing_token,
                inspection=inspection,
                completed_at=NOW,
                retain_until=NOW + timedelta(days=30),
            )
    finally:
        with psycopg.connect(scan_database.migrator) as connection:
            connection.execute(
                """
                DROP TRIGGER fail_scan_finding_test
                ON schemabridge_control.semantic_change_findings
                """
            )
            connection.execute(
                """
                DROP FUNCTION schemabridge_control.fail_scan_finding_test()
                """
            )

    loaded = store.load(scope.workspace_id, request.scan_id)
    assert loaded is not None
    assert loaded.status is SemanticChangeScanStatus.LEASED
    with psycopg.connect(scan_database.migrator) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM schemabridge_control.semantic_change_reports
                 WHERE workspace_id = %s),
                (SELECT count(*) FROM schemabridge_control.semantic_change_findings
                 WHERE workspace_id = %s),
                (SELECT count(*) FROM schemabridge_control.semantic_change_impacts
                 WHERE workspace_id = %s)
            """,
            (scope.workspace_id, scope.workspace_id, scope.workspace_id),
        ).fetchone()
    assert counts == (0, 0, 0)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
