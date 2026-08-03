"""Real PostgreSQL proof for Query Studio v8 policy, admission, and audit."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
    PostgresQueryStudioAiControl,
)
from schemabridge.adapters.control_plane.postgres_query_studio_policy import (
    PostgresTenantAiPolicyOperator,
)
from schemabridge.application.ports.query_studio_ai_control import (
    AiAdmissionOutcome,
    AiAttemptReservationRequest,
    AiAttemptSettlementRequest,
    AiSettlementOutcome,
)
from schemabridge.application.query_studio_ai_policy import (
    TenantAiPolicyDesired,
    TenantAiPolicyError,
    TenantAiPolicyErrorCode,
    TenantAiPolicyOperator,
)
from schemabridge.domain.query_studio import ProviderStage

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
EU_ENDPOINT_ORIGIN_FINGERPRINT = "83203af93d5b1b9a2b7ab344441b99f7e19d980c881ba78de44e16073bd199c7"


@dataclass(frozen=True)
class _Urls:
    database: str
    migrator: str
    runtime: str
    api: str
    worker: str
    catalog: str
    reconciler: str


def _admin_dsn() -> str:
    return os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


@pytest.fixture(scope="module")
def query_studio_database() -> Iterator[_Urls]:
    database = f"schemabridge_query_studio_{uuid4().hex[:12]}"
    urls = _Urls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        runtime=_role_dsn("schemabridge_runtime", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        catalog=_role_dsn("schemabridge_catalog", database),
        reconciler=_role_dsn("schemabridge_reconciler", database),
    )
    with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
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
                    schemabridge_runtime,
                    schemabridge_api,
                    schemabridge_worker,
                    schemabridge_catalog,
                    schemabridge_reconciler,
                    schemabridge_backup
                """
            ).format(sql.Identifier(database))
        )
    try:
        migrated = PostgresControlPlaneMigrator(urls.migrator, MIGRATIONS).migrate()
        assert migrated.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
        assert migrated.inspection.current_version == 14
        yield urls
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


def _apply_policy(
    dsn: str,
    workspace_id: str,
    *,
    expected_version: int = 0,
    enabled: bool = True,
    requests_per_minute: int = 20,
    daily_input_tokens: int = 10_000,
    daily_output_tokens: int = 2_000,
    concurrency: int = 2,
    lease_seconds: int = 60,
) -> tuple[object, ...]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.apply_tenant_ai_policy(
                %s, %s, %s, %s, %s, 'gpt-5-nano-2025-08-07',
                'eu', %s, %s, %s, %s, %s, %s, %s, 2592000,
                'sb_platform_admin_v1', 'APPLY TENANT AI POLICY'
            )
            """,
            (
                workspace_id,
                expected_version,
                enabled,
                enabled,
                SHA_A if enabled else None,
                EU_ENDPOINT_ORIGIN_FINGERPRINT,
                SHA_C,
                requests_per_minute,
                daily_input_tokens,
                daily_output_tokens,
                concurrency,
                lease_seconds,
            ),
        ).fetchone()
    assert row is not None
    return row


def _reserve(
    dsn: str,
    workspace_id: str,
    *,
    request_id: str,
    idempotency_digest: str,
    actor_digest: str = SHA_A,
    estimated_input: int = 10,
    estimated_output: int = 5,
    capability: str = "opaque-capability-0123456789abcdef",
) -> tuple[object, ...]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.reserve_ai_provider_attempt(
                %s, %s, %s, 'expansion', 1::smallint, %s, %s, %s, %s, %s,
                %s::integer, %s::integer, %s
            )
            """,
            (
                workspace_id,
                request_id,
                actor_digest,
                idempotency_digest,
                SHA_A,
                SHA_B,
                SHA_C,
                SHA_C,
                estimated_input,
                estimated_output,
                capability,
            ),
        ).fetchone()
    assert row is not None
    return row


def test_v6_role_boundary_and_default_off_policy(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    with psycopg.connect(query_studio_database.runtime) as connection:
        assert (
            connection.execute(
                "SELECT * FROM schemabridge_control.load_tenant_ai_policy(%s)",
                (workspace_id,),
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT count(*) FROM schemabridge_control.tenant_ai_policies")
    with psycopg.connect(query_studio_database.runtime) as connection:
        discovery_scope = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.load_physical_discovery_scope(
                %s, 'catalog.prod', 'registry_alpha'
            )
            """,
            (workspace_id,),
        ).fetchone()
        assert discovery_scope is not None
        assert discovery_scope[1:] == (0, 0, 0)
        assert (
            connection.execute(
                """
            SELECT *
            FROM schemabridge_control.discover_physical_fields(
                %s, 'catalog.prod', 'registry_alpha', %s, '', 20,
                NULL, NULL, NULL, NULL
            )
            """,
                (workspace_id, discovery_scope[0]),
            ).fetchall()
            == []
        )

    for dsn in (
        query_studio_database.api,
        query_studio_database.worker,
        query_studio_database.catalog,
        query_studio_database.reconciler,
    ):
        with (
            psycopg.connect(dsn) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute(
                "SELECT * FROM schemabridge_control.load_tenant_ai_policy(%s)",
                (workspace_id,),
            ).fetchone()
    for dsn in (
        query_studio_database.api,
        query_studio_database.worker,
        query_studio_database.catalog,
        query_studio_database.reconciler,
    ):
        with (
            psycopg.connect(dsn) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute(
                """
                SELECT *
                FROM schemabridge_control.load_physical_discovery_scope(
                    %s, 'catalog.prod', 'registry_alpha'
                )
                """,
                (workspace_id,),
            ).fetchone()

    disabled = _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        enabled=False,
    )
    assert disabled[1:4] == (1, False, False)
    denied = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-disabled",
        idempotency_digest=SHA_A,
    )
    assert denied[0] == "policy_disabled"
    assert denied[2:6] == (None, None, None, None)


def test_exact_tenant_ai_policy_operator_records_immutable_revision(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    operator = TenantAiPolicyOperator(
        PostgresTenantAiPolicyOperator(dsn=query_studio_database.migrator)
    )
    desired = TenantAiPolicyDesired(
        external_ai_enabled=True,
        provider_governance_accepted=True,
        provider_governance_fingerprint=SHA_A,
        model_snapshot="gpt-5-nano-2025-08-07",
        endpoint_region="eu",
        endpoint_origin_fingerprint=EU_ENDPOINT_ORIGIN_FINGERPRINT,
        configuration_fingerprint=SHA_C,
        requests_per_minute=20,
        daily_input_token_limit=10_000,
        daily_output_token_limit=2_000,
        concurrent_attempt_limit=2,
        reservation_lease_seconds=60,
        audit_retention_seconds=2_592_000,
    )

    assert operator.inspect(workspace_id) is None
    proposal = operator.prepare(
        workspace_id=workspace_id,
        expected_version=0,
        desired=desired,
        updated_by="sb_platform_admin_v1",
    )
    with pytest.raises(TenantAiPolicyError) as mismatch:
        operator.apply(
            proposal,
            expected_proposal_fingerprint=proposal.fingerprint,
            confirmation="apply tenant ai policy",
        )
    assert mismatch.value.code is TenantAiPolicyErrorCode.CONFIRMATION_MISMATCH

    applied = operator.apply(
        proposal,
        expected_proposal_fingerprint=proposal.fingerprint,
        confirmation="APPLY TENANT AI POLICY",
    )
    assert applied.version == 1
    assert applied.external_ai_enabled is True
    assert applied.endpoint_origin_fingerprint == EU_ENDPOINT_ORIGIN_FINGERPRINT

    with psycopg.connect(query_studio_database.migrator) as connection:
        revisions = connection.execute(
            """
            SELECT version, external_ai_enabled, updated_by
            FROM schemabridge_control.tenant_ai_policy_revisions
            WHERE workspace_id = %s
            ORDER BY version
            """,
            (workspace_id,),
        ).fetchall()
        with (
            pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
            connection.transaction(),
        ):
            connection.execute(
                """
                UPDATE schemabridge_control.tenant_ai_policy_revisions
                SET requests_per_minute = 21
                WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
    assert revisions == [(1, True, "sb_platform_admin_v1")]


def test_atomic_reserve_settle_replay_quota_and_append_only_audit(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=20,
        daily_output_tokens=10,
        concurrency=1,
    )
    control = PostgresQueryStudioAiControl(dsn=query_studio_database.runtime)
    policy = control.load_policy(workspace_id)
    assert policy is not None
    first = control.reserve(
        AiAttemptReservationRequest(
            workspace_id=workspace_id,
            request_id="request-first",
            actor_digest=SHA_A,
            stage=ProviderStage.EXPANSION,
            attempt_number=1,
            idempotency_digest=SHA_A,
            request_fingerprint=SHA_A,
            semantic_scope_fingerprint=SHA_B,
            semantic_payload_fingerprint=SHA_C,
            configuration_fingerprint=SHA_C,
            estimated_input_tokens=10,
            estimated_output_tokens=5,
            lease_capability="opaque-capability-0123456789abcdef",
        )
    )
    assert first.outcome is AiAdmissionOutcome.RESERVED
    assert first.replayed is False
    assert first.reservation_id is not None
    assert first.fencing_token is not None
    reservation_id = first.reservation_id
    fencing_token = first.fencing_token

    concurrency_denied = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-concurrent",
        idempotency_digest=SHA_B,
        actor_digest=SHA_B,
    )
    assert concurrency_denied[0] == "concurrency_limited"

    replay = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-first",
        idempotency_digest=SHA_A,
    )
    assert replay[0:5] == (
        "replayed",
        True,
        reservation_id,
        "reserved",
        fencing_token,
    )
    with (
        psycopg.connect(query_studio_database.runtime) as connection,
        pytest.raises(psycopg.errors.UniqueViolation),
    ):
        connection.execute(
            """
            SELECT *
            FROM schemabridge_control.reserve_ai_provider_attempt(
                %s, 'request-conflict', %s, 'expansion', 1::smallint, %s,
                %s, %s, %s, %s, 10, 5, %s
            )
            """,
            (
                workspace_id,
                SHA_A,
                SHA_A,
                SHA_A,
                SHA_B,
                SHA_C,
                SHA_C,
                "opaque-capability-0123456789abcdef",
            ),
        ).fetchone()

    settled = control.settle(
        AiAttemptSettlementRequest(
            workspace_id=workspace_id,
            reservation_id=reservation_id,
            lease_capability="opaque-capability-0123456789abcdef",
            fencing_token=fencing_token,
            outcome=AiSettlementOutcome.SUCCEEDED,
            reserved_input_tokens=10,
            reserved_output_tokens=5,
            observed_input_tokens=8,
            observed_output_tokens=4,
            duration_ms=120,
        )
    )
    with psycopg.connect(query_studio_database.runtime) as connection:
        replayed_settlement = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, 'succeeded', 8, 4, 120
            )
            """,
            (
                workspace_id,
                reservation_id,
                "opaque-capability-0123456789abcdef",
                fencing_token,
            ),
        ).fetchone()
    assert settled.replayed is False
    assert settled.outcome is AiSettlementOutcome.SUCCEEDED
    assert (settled.charged_input_tokens, settled.charged_output_tokens) == (8, 4)
    assert replayed_settlement is not None
    assert replayed_settlement[1] is True
    assert replayed_settlement[7] == settled.audit_id

    failed = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-failed",
        idempotency_digest=SHA_D,
        estimated_input=5,
        estimated_output=3,
    )
    assert failed[0] == "reserved"
    with psycopg.connect(query_studio_database.runtime) as connection:
        failed_settlement = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, 'timeout', 1, 0, 900
            )
            """,
            (
                workspace_id,
                failed[2],
                "opaque-capability-0123456789abcdef",
                failed[4],
            ),
        ).fetchone()
    assert failed_settlement is not None
    assert failed_settlement[3:6] == ("timeout", 5, 3)

    quota_denied = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-over-quota",
        idempotency_digest=SHA_C,
        estimated_input=13,
        estimated_output=7,
    )
    assert quota_denied[0] == "quota_exhausted"

    with psycopg.connect(query_studio_database.migrator) as connection:
        accounting = connection.execute(
            """
            SELECT reserved_input_tokens, reserved_output_tokens,
                   charged_input_tokens, charged_output_tokens
            FROM schemabridge_control.ai_provider_daily_usage
            WHERE workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT input_tokens, output_tokens, duration_ms, outcome_code,
                   retain_until > occurred_at
            FROM schemabridge_control.ai_provider_usage_audit
            WHERE reservation_id = %s
            """,
            (reservation_id,),
        ).fetchone()
        with (
            pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
            connection.transaction(),
        ):
            connection.execute(
                """
                UPDATE schemabridge_control.ai_provider_usage_audit
                SET duration_ms = 0
                WHERE reservation_id = %s
                """,
                (reservation_id,),
            )
    assert accounting == (0, 0, 13, 7)
    assert audit == (8, 4, 120, "succeeded", True)


def test_runtime_settlement_rejects_invalid_success_usage_at_the_database_boundary(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    capability = "opaque-capability-v7-0123456789abcdef"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=100,
        daily_output_tokens=100,
        concurrency=2,
    )
    succeeded = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-v7-success",
        idempotency_digest=hashlib.sha256(b"request-v7-success").hexdigest(),
        estimated_input=10,
        estimated_output=5,
        capability=capability,
    )
    assert succeeded[0] == "reserved"

    with psycopg.connect(query_studio_database.runtime) as connection:
        for observed_input, observed_output in (
            (0, 0),
            (0, 5),
            (10, 0),
            (11, 5),
            (10, 6),
        ):
            with (
                pytest.raises(psycopg.errors.InvalidParameterValue),
                connection.transaction(),
            ):
                connection.execute(
                    """
                    SELECT *
                    FROM schemabridge_control.settle_ai_provider_attempt(
                        %s, %s, %s, %s, 'succeeded', %s, %s, 25
                    )
                    """,
                    (
                        workspace_id,
                        succeeded[2],
                        capability,
                        succeeded[4],
                        observed_input,
                        observed_output,
                    ),
                ).fetchone()

        exact = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, 'succeeded', 10, 5, 25
            )
            """,
            (workspace_id, succeeded[2], capability, succeeded[4]),
        ).fetchone()
        replay = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, 'succeeded', 10, 5, 25
            )
            """,
            (workspace_id, succeeded[2], capability, succeeded[4]),
        ).fetchone()

    assert exact is not None
    assert exact[1:6] == (False, "settled", "succeeded", 10, 5)
    assert replay is not None
    assert replay[0] == exact[0]
    assert replay[1] is True
    assert replay[2:6] == exact[2:6]
    assert replay[6:] == exact[6:]

    failed = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-v7-failure",
        idempotency_digest=hashlib.sha256(b"request-v7-failure").hexdigest(),
        estimated_input=7,
        estimated_output=3,
        capability=capability,
    )
    assert failed[0] == "reserved"
    with psycopg.connect(query_studio_database.runtime) as connection:
        conservative = connection.execute(
            """
            SELECT *
            FROM schemabridge_control.settle_ai_provider_attempt(
                %s, %s, %s, %s, 'timeout', 0, 0, 50
            )
            """,
            (workspace_id, failed[2], capability, failed[4]),
        ).fetchone()
    assert conservative is not None
    assert conservative[1:6] == (False, "settled", "timeout", 7, 3)

    with psycopg.connect(query_studio_database.migrator) as connection:
        constraints = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT conname
                FROM pg_catalog.pg_constraint
                WHERE connamespace = (
                    SELECT oid
                    FROM pg_catalog.pg_namespace
                    WHERE nspname = 'schemabridge_control'
                )
                  AND conname IN (
                    'ai_attempt_terminal_usage_accounting_v7',
                    'ai_provider_usage_audit_positive_charges_v7'
                  )
                """
            )
        }
        audits = connection.execute(
            """
            SELECT request_id, input_tokens, output_tokens, outcome_code
            FROM schemabridge_control.ai_provider_usage_audit
            WHERE request_id IN ('request-v7-success', 'request-v7-failure')
            ORDER BY request_id
            """
        ).fetchall()
    assert constraints == {
        "ai_attempt_terminal_usage_accounting_v7",
        "ai_provider_usage_audit_positive_charges_v7",
    }
    assert audits == [
        ("request-v7-failure", 7, 3, "timeout"),
        ("request-v7-success", 10, 5, "succeeded"),
    ]


def test_settlement_rechecks_lease_after_waiting_for_the_reservation_lock(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    capability = "opaque-capability-v8-expiry-0123456789"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=100,
        daily_output_tokens=100,
        concurrency=2,
    )
    reserved = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-v8-wait-expiry",
        idempotency_digest=hashlib.sha256(b"request-v8-wait-expiry").hexdigest(),
        estimated_input=10,
        estimated_output=5,
        capability=capability,
    )

    with psycopg.connect(query_studio_database.migrator) as connection:
        connection.execute(
            """
            ALTER TABLE schemabridge_control.ai_provider_attempt_reservations
            DISABLE TRIGGER ai_provider_attempt_reservations_guard
            """
        )
        connection.execute(
            """
            UPDATE schemabridge_control.ai_provider_attempt_reservations
            SET lease_expires_at = clock_timestamp() + interval '1 second'
            WHERE reservation_id = %s
            """,
            (reserved[2],),
        )
        connection.execute(
            """
            ALTER TABLE schemabridge_control.ai_provider_attempt_reservations
            ENABLE TRIGGER ai_provider_attempt_reservations_guard
            """
        )

    def settle_after_wait() -> str | None:
        try:
            with psycopg.connect(query_studio_database.runtime) as connection:
                connection.execute("SET statement_timeout = '5s'")
                connection.execute(
                    """
                    SELECT *
                    FROM schemabridge_control.settle_ai_provider_attempt(
                        %s, %s, %s, %s, 'succeeded', 8, 4, 25
                    )
                    """,
                    (workspace_id, reserved[2], capability, reserved[4]),
                ).fetchone()
        except psycopg.errors.ObjectNotInPrerequisiteState as error:
            return error.sqlstate
        return None

    with (
        psycopg.connect(query_studio_database.migrator) as blocker,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        blocker.execute(
            """
            SELECT reservation_id
            FROM schemabridge_control.ai_provider_attempt_reservations
            WHERE reservation_id = %s
            FOR UPDATE
            """,
            (reserved[2],),
        ).fetchone()
        waiting = executor.submit(settle_after_wait)
        time.sleep(1.25)
        blocker.commit()
        assert waiting.result(timeout=5) == "55000"

    with psycopg.connect(query_studio_database.migrator) as connection:
        state = connection.execute(
            """
            SELECT status, outcome_code, settled_at
            FROM schemabridge_control.ai_provider_attempt_reservations
            WHERE reservation_id = %s
            """,
            (reserved[2],),
        ).fetchone()
        audit_count = connection.execute(
            """
            SELECT count(*)
            FROM schemabridge_control.ai_provider_usage_audit
            WHERE reservation_id = %s
            """,
            (reserved[2],),
        ).fetchone()
    assert state == ("reserved", None, None)
    assert audit_count == (0,)


@pytest.mark.parametrize("stale_ownership", ("capability", "fence"))
def test_stale_settlement_ownership_does_not_lock_accounting_rows(
    query_studio_database: _Urls,
    stale_ownership: str,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    capability = "opaque-capability-v8-ownership-012345"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=100,
        daily_output_tokens=100,
        concurrency=2,
    )
    reserved = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id=f"request-v8-stale-{stale_ownership}",
        idempotency_digest=hashlib.sha256(
            f"request-v8-stale-{stale_ownership}".encode()
        ).hexdigest(),
        capability=capability,
    )
    provided_capability = (
        "wrong-capability-v8-ownership-012345" if stale_ownership == "capability" else capability
    )
    provided_fence = int(reserved[4]) + 1 if stale_ownership == "fence" else int(reserved[4])

    attacker = psycopg.connect(query_studio_database.runtime)
    try:
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="AI reservation ownership is stale",
        ) as rejected:
            attacker.execute(
                """
                SELECT *
                FROM schemabridge_control.settle_ai_provider_attempt(
                    %s, %s, %s, %s, 'succeeded', 8, 4, 25
                )
                """,
                (
                    workspace_id,
                    reserved[2],
                    provided_capability,
                    provided_fence,
                ),
            ).fetchone()
        assert rejected.value.sqlstate == "55000"

        with psycopg.connect(query_studio_database.migrator) as contender:
            state = contender.execute(
                """
                SELECT workspace_id
                FROM schemabridge_control.ai_provider_admission_state
                WHERE workspace_id = %s
                FOR UPDATE NOWAIT
                """,
                (workspace_id,),
            ).fetchone()
            usage = contender.execute(
                """
                SELECT workspace_id
                FROM schemabridge_control.ai_provider_daily_usage
                WHERE workspace_id = %s
                FOR UPDATE NOWAIT
                """,
                (workspace_id,),
            ).fetchone()
        assert state == (workspace_id,)
        assert usage == (workspace_id,)
    finally:
        attacker.rollback()
        attacker.close()


def test_expiry_locks_only_daily_dates_referenced_by_active_reservations(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=100,
        daily_output_tokens=100,
        concurrency=2,
    )
    _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-v8-bounded-expiry-lock",
        idempotency_digest=hashlib.sha256(b"request-v8-bounded-expiry-lock").hexdigest(),
    )
    with psycopg.connect(query_studio_database.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.ai_provider_daily_usage (
                workspace_id, usage_date,
                reserved_input_tokens, reserved_output_tokens,
                charged_input_tokens, charged_output_tokens, updated_at
            )
            SELECT %s,
                   (current_date - historical.day_offset::integer),
                   0, 0, 0, 0, clock_timestamp()
            FROM generate_series(1, 120) AS historical(day_offset)
            ON CONFLICT (workspace_id, usage_date) DO NOTHING
            """,
            (workspace_id,),
        )

    holder = psycopg.connect(query_studio_database.runtime)
    try:
        expired = holder.execute(
            """
            SELECT schemabridge_control.expire_ai_provider_attempts(%s, 10)
            """,
            (workspace_id,),
        ).fetchone()
        assert expired == (0,)

        with psycopg.connect(query_studio_database.migrator) as contender:
            historical = contender.execute(
                """
                SELECT usage_date
                FROM schemabridge_control.ai_provider_daily_usage
                WHERE workspace_id = %s
                  AND usage_date = current_date - 120
                FOR UPDATE NOWAIT
                """,
                (workspace_id,),
            ).fetchone()
            assert historical is not None
            with (
                pytest.raises(psycopg.errors.LockNotAvailable),
                contender.transaction(),
            ):
                contender.execute(
                    """
                    SELECT usage_date
                    FROM schemabridge_control.ai_provider_daily_usage
                    WHERE workspace_id = %s
                      AND usage_date = current_date
                    FOR UPDATE NOWAIT
                    """,
                    (workspace_id,),
                ).fetchone()
    finally:
        holder.rollback()
        holder.close()


def test_settlement_and_reservation_serialize_without_accounting_deadlock(
    query_studio_database: _Urls,
) -> None:
    workspace_id = f"workspace-{uuid4().hex[:12]}"
    capability = "opaque-capability-v8-lock-order-0123456"
    _apply_policy(
        query_studio_database.migrator,
        workspace_id,
        daily_input_tokens=100,
        daily_output_tokens=100,
        concurrency=2,
        lease_seconds=10,
    )
    first = _reserve(
        query_studio_database.runtime,
        workspace_id,
        request_id="request-v8-race-first",
        idempotency_digest=hashlib.sha256(b"request-v8-race-first").hexdigest(),
        estimated_input=10,
        estimated_output=5,
        capability=capability,
    )

    with psycopg.connect(query_studio_database.migrator) as connection:
        connection.execute(
            """
            CREATE FUNCTION schemabridge_control.pause_v8_daily_usage_update()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY INVOKER
            SET search_path = pg_catalog, schemabridge_control
            AS $pause$
            BEGIN
                IF current_setting('application_name')
                    = 'm27-v8-settlement-racer'
                THEN
                    PERFORM pg_sleep(1.5);
                END IF;
                RETURN NEW;
            END;
            $pause$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER pause_v8_daily_usage_update
            BEFORE UPDATE
            ON schemabridge_control.ai_provider_daily_usage
            FOR EACH ROW
            EXECUTE FUNCTION schemabridge_control.pause_v8_daily_usage_update()
            """
        )

    def settle_first() -> tuple[object, ...]:
        with psycopg.connect(query_studio_database.runtime) as connection:
            connection.execute("SET application_name = 'm27-v8-settlement-racer'")
            connection.execute("SET statement_timeout = '5s'")
            row = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.settle_ai_provider_attempt(
                    %s, %s, %s, %s, 'succeeded', 8, 4, 25
                )
                """,
                (workspace_id, first[2], capability, first[4]),
            ).fetchone()
        assert row is not None
        return row

    def reserve_second() -> tuple[object, ...]:
        with psycopg.connect(query_studio_database.runtime) as connection:
            connection.execute("SET statement_timeout = '5s'")
            row = connection.execute(
                """
                SELECT *
                FROM schemabridge_control.reserve_ai_provider_attempt(
                    %s, 'request-v8-race-second', %s, 'expansion',
                    1::smallint, %s, %s, %s, %s, %s, 6, 2, %s
                )
                """,
                (
                    workspace_id,
                    SHA_A,
                    hashlib.sha256(b"request-v8-race-second").hexdigest(),
                    SHA_A,
                    SHA_B,
                    SHA_C,
                    SHA_C,
                    capability,
                ),
            ).fetchone()
        assert row is not None
        return row

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            settlement = executor.submit(settle_first)
            deadline = time.monotonic() + 3
            observed_sleep = False
            with psycopg.connect(_admin_dsn()) as observer:
                while time.monotonic() < deadline:
                    wait_event = observer.execute(
                        """
                        SELECT wait_event
                        FROM pg_catalog.pg_stat_activity
                        WHERE application_name = 'm27-v8-settlement-racer'
                        """
                    ).fetchone()
                    if wait_event == ("PgSleep",):
                        observed_sleep = True
                        break
                    time.sleep(0.02)
            assert observed_sleep
            reservation = executor.submit(reserve_second)
            settled = settlement.result(timeout=6)
            reserved = reservation.result(timeout=6)
    finally:
        with psycopg.connect(query_studio_database.migrator) as connection:
            connection.execute(
                """
                DROP TRIGGER IF EXISTS pause_v8_daily_usage_update
                ON schemabridge_control.ai_provider_daily_usage
                """
            )
            connection.execute(
                """
                DROP FUNCTION IF EXISTS
                    schemabridge_control.pause_v8_daily_usage_update()
                """
            )

    assert settled[1:6] == (False, "settled", "succeeded", 8, 4)
    assert reserved[0:4] == ("reserved", False, reserved[2], "reserved")
    with psycopg.connect(query_studio_database.migrator) as connection:
        accounting = connection.execute(
            """
            SELECT state.active_attempt_count,
                   usage.reserved_input_tokens,
                   usage.reserved_output_tokens,
                   usage.charged_input_tokens,
                   usage.charged_output_tokens
            FROM schemabridge_control.ai_provider_admission_state AS state
            JOIN schemabridge_control.ai_provider_daily_usage AS usage
              ON usage.workspace_id = state.workspace_id
            WHERE state.workspace_id = %s
            """,
            (workspace_id,),
        ).fetchone()
        lease_is_fresh = connection.execute(
            """
            SELECT lease_expires_at > clock_timestamp() + interval '9 seconds'
            FROM schemabridge_control.ai_provider_attempt_reservations
            WHERE reservation_id = %s
            """,
            (reserved[2],),
        ).fetchone()
    assert accounting == (1, 6, 2, 8, 4)
    assert lease_is_fresh == (True,)


def test_v8_provider_function_acl_security_and_owner_are_exact(
    query_studio_database: _Urls,
) -> None:
    signatures = (
        "expire_ai_provider_attempts(character varying,integer)",
        (
            "reserve_ai_provider_attempt(character varying,character varying,"
            "character,character varying,smallint,character,character,character,"
            "character,character,integer,integer,character varying)"
        ),
        (
            "settle_ai_provider_attempt(character varying,character varying,"
            "character varying,bigint,character varying,integer,integer,integer)"
        ),
    )
    core_signatures = tuple(
        signature.replace("attempts(", "attempts_v7_core(").replace(
            "attempt(",
            "attempt_v7_core(",
        )
        for signature in signatures
    )

    with psycopg.connect(query_studio_database.migrator) as connection:
        for signature in signatures:
            metadata = connection.execute(
                """
                SELECT pg_get_userbyid(proowner), prosecdef, proconfig
                FROM pg_catalog.pg_proc
                WHERE oid = to_regprocedure(%s)
                """,
                (f"schemabridge_control.{signature}",),
            ).fetchone()
            acl = connection.execute(
                """
                SELECT coalesce(role.rolname, 'PUBLIC'), privilege.privilege_type
                FROM pg_catalog.pg_proc AS procedure
                CROSS JOIN LATERAL aclexplode(
                    coalesce(
                        procedure.proacl,
                        acldefault('f', procedure.proowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS role
                  ON role.oid = privilege.grantee
                WHERE procedure.oid = to_regprocedure(%s)
                ORDER BY 1, 2
                """,
                (f"schemabridge_control.{signature}",),
            ).fetchall()
            assert metadata == (
                "schemabridge_migrator",
                True,
                ["search_path=pg_catalog, schemabridge_control"],
            )
            assert acl == [
                ("schemabridge_migrator", "EXECUTE"),
                ("schemabridge_runtime", "EXECUTE"),
            ]

        for signature in core_signatures:
            metadata = connection.execute(
                """
                SELECT pg_get_userbyid(proowner), prosecdef, proconfig
                FROM pg_catalog.pg_proc
                WHERE oid = to_regprocedure(%s)
                """,
                (f"schemabridge_control.{signature}",),
            ).fetchone()
            acl = connection.execute(
                """
                SELECT coalesce(role.rolname, 'PUBLIC'), privilege.privilege_type
                FROM pg_catalog.pg_proc AS procedure
                CROSS JOIN LATERAL aclexplode(
                    coalesce(
                        procedure.proacl,
                        acldefault('f', procedure.proowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS role
                  ON role.oid = privilege.grantee
                WHERE procedure.oid = to_regprocedure(%s)
                ORDER BY 1, 2
                """,
                (f"schemabridge_control.{signature}",),
            ).fetchall()
            assert metadata == (
                "schemabridge_migrator",
                False,
                ["search_path=pg_catalog, schemabridge_control"],
            )
            assert acl == [("schemabridge_migrator", "EXECUTE")]


def test_rate_limit_and_crash_expiry_charge_conservatively(
    query_studio_database: _Urls,
) -> None:
    rate_workspace = f"workspace-{uuid4().hex[:12]}"
    _apply_policy(
        query_studio_database.migrator,
        rate_workspace,
        requests_per_minute=1,
        concurrency=5,
    )
    assert (
        _reserve(
            query_studio_database.runtime,
            rate_workspace,
            request_id="request-rate-one",
            idempotency_digest=SHA_A,
        )[0]
        == "reserved"
    )
    assert (
        _reserve(
            query_studio_database.runtime,
            rate_workspace,
            request_id="request-rate-two",
            idempotency_digest=SHA_B,
            actor_digest=SHA_A,
        )[0]
        == "rate_limited"
    )

    expiry_workspace = f"workspace-{uuid4().hex[:12]}"
    _apply_policy(query_studio_database.migrator, expiry_workspace)
    reservation_id = "air_" + hashlib.sha256(expiry_workspace.encode()).hexdigest()
    with psycopg.connect(query_studio_database.migrator) as connection:
        connection.execute(
            """
            UPDATE schemabridge_control.ai_provider_admission_state
            SET active_attempt_count = 1, next_fencing_token = 1
            WHERE workspace_id = %s
            """,
            (expiry_workspace,),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.ai_provider_daily_usage (
                workspace_id, usage_date, reserved_input_tokens,
                reserved_output_tokens, charged_input_tokens,
                charged_output_tokens, updated_at
            ) VALUES (%s, CURRENT_DATE, 11, 6, 0, 0, clock_timestamp())
            """,
            (expiry_workspace,),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.ai_provider_attempt_reservations (
                reservation_id, workspace_id, request_id, actor_digest,
                stage, attempt_number, idempotency_digest,
                request_fingerprint, semantic_scope_fingerprint,
                semantic_payload_fingerprint, configuration_fingerprint,
                policy_version, model_snapshot, endpoint_region,
                estimated_input_tokens, estimated_output_tokens,
                audit_retention_seconds, status, capability_digest,
                fencing_token, lease_acquired_at, lease_expires_at,
                created_at, retain_until
            ) VALUES (
                %s, %s, 'request-expired', %s, 'interpretation', 1, %s,
                %s, %s, %s, %s, 1, 'gpt-5-nano-2025-08-07', 'eu',
                11, 6, 2592000, 'reserved', %s, 1,
                clock_timestamp() - interval '20 seconds',
                clock_timestamp() - interval '10 seconds',
                clock_timestamp() - interval '20 seconds',
                clock_timestamp() + interval '30 days'
            )
            """,
            (
                reservation_id,
                expiry_workspace,
                SHA_A,
                SHA_B,
                SHA_A,
                SHA_B,
                SHA_C,
                SHA_C,
                hashlib.sha256(b"opaque-capability-expired-0123456789").hexdigest(),
            ),
        )

    with psycopg.connect(query_studio_database.runtime) as connection:
        assert connection.execute(
            "SELECT schemabridge_control.expire_ai_provider_attempts(%s, 100)",
            (expiry_workspace,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT schemabridge_control.expire_ai_provider_attempts(%s, 100)",
            (expiry_workspace,),
        ).fetchone() == (0,)

    with psycopg.connect(query_studio_database.migrator) as connection:
        state = connection.execute(
            """
            SELECT status, outcome_code, charged_input_tokens,
                   charged_output_tokens, lease_expires_at
            FROM schemabridge_control.ai_provider_attempt_reservations
            WHERE reservation_id = %s
            """,
            (reservation_id,),
        ).fetchone()
        accounting = connection.execute(
            """
            SELECT reserved_input_tokens, reserved_output_tokens,
                   charged_input_tokens, charged_output_tokens
            FROM schemabridge_control.ai_provider_daily_usage
            WHERE workspace_id = %s
            """,
            (expiry_workspace,),
        ).fetchone()
        active = connection.execute(
            """
            SELECT active_attempt_count
            FROM schemabridge_control.ai_provider_admission_state
            WHERE workspace_id = %s
            """,
            (expiry_workspace,),
        ).fetchone()
    assert state == ("expired", "expired_crash", 11, 6, None)
    assert accounting == (0, 0, 11, 6)
    assert active == (0,)
