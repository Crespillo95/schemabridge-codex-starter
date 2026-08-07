"""Dynamic PostgreSQL query connector resolved from one exact governed route."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.adapters.postgres.cost_preflight import PsycopgQueryCostPreflight
from schemabridge.adapters.postgres.preview import PsycopgQueryPreview
from schemabridge.adapters.postgres.rejections import PsycopgRejectedSourceReporter
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    ConnectorSecretResolver,
    OpaqueConnectorSecretRef,
    ResolvedPostgresSecret,
)
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    ProtectedSourceOperationCancelled,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    SourceConnectorKind,
    SourceDialect,
)
from schemabridge.domain.resolution import (
    RejectedSourceReport,
    RejectionCheck,
)

ConnectorSecretReferenceLoader = Callable[
    [GovernedExecutionTarget],
    OpaqueConnectorSecretRef,
]


@dataclass(frozen=True, slots=True)
class RoutedPostgresQueryConnector:
    """Reload a private route and secret immediately before each source operation."""

    reference_loader: ConnectorSecretReferenceLoader = field(repr=False)
    secret_resolver: ConnectorSecretResolver = field(repr=False)
    allowed_fields: frozenset[str]
    connect_timeout_seconds: int = 3
    max_rows_limit: int = 500
    max_timeout_ms: int = 5_000
    max_rejection_records: int = 500

    def assess(
        self,
        query: ValidatedQuery,
        target: GovernedExecutionTarget,
    ) -> QueryCostAssessment:
        """Resolve the current private binding and run EXPLAIN without ANALYZE."""

        managed_target = _managed_target(target)
        _require_query_target(query, managed_target)
        secret = self._resolve_secret(managed_target)
        try:
            return PsycopgQueryCostPreflight(
                secret.dsn,
                connect_timeout_seconds=self.connect_timeout_seconds,
            ).assess(query, managed_target)
        except PostgresSourceIdentityMismatchError:
            raise _source_identity_mismatch() from None

    def execute(
        self,
        query: ValidatedQuery,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> QueryPreviewResult:
        """Resolve the private binding again for the exact guarded preview."""

        managed_target = _managed_target(target)
        _require_query_target(query, managed_target)
        secret = self._resolve_secret(managed_target)
        try:
            return PsycopgQueryPreview(
                secret.dsn,
                expected_user=secret.expected_reader,
                bound_target_fingerprint=managed_target.fingerprint,
                connect_timeout_seconds=self.connect_timeout_seconds,
                max_rows_limit=self.max_rows_limit,
                max_timeout_ms=self.max_timeout_ms,
            ).execute(query, target=managed_target)
        except PostgresSourceIdentityMismatchError:
            raise _source_identity_mismatch() from None

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
        target: GovernedExecutionTarget | None = None,
    ) -> RejectedSourceReport:
        """Resolve the private binding once more before bounded rejection inspection."""

        managed_target = _managed_target(target)
        if not checks:
            return RejectedSourceReport()
        if should_continue is not None and not should_continue():
            raise ProtectedSourceOperationCancelled(
                "protected source operation was cooperatively cancelled"
            )
        secret = self._resolve_secret(managed_target)
        try:
            return PsycopgRejectedSourceReporter(
                secret.dsn,
                allowed_fields=self.allowed_fields,
                expected_user=secret.expected_reader,
                bound_target_fingerprint=managed_target.fingerprint,
                connect_timeout_seconds=self.connect_timeout_seconds,
                max_records=self.max_rejection_records,
            ).inspect(
                checks,
                statement_timeout_ms=statement_timeout_ms,
                should_continue=should_continue,
                target=managed_target,
            )
        except PostgresSourceIdentityMismatchError:
            raise _source_identity_mismatch() from None

    def _resolve_secret(
        self,
        target: GovernedExecutionTarget,
    ) -> ResolvedPostgresSecret:
        failure_code: ConnectorTargetErrorCode | None = None
        try:
            reference = self.reference_loader(target)
            if type(reference) is not OpaqueConnectorSecretRef:
                raise ConnectorTargetError(
                    ConnectorTargetErrorCode.INVALID_RESPONSE,
                    "connector route response is invalid",
                )
            return self.secret_resolver.resolve(reference, target)
        except ConnectorTargetError:
            raise
        except ConnectorSecretResolutionError as error:
            failure_code = (
                ConnectorTargetErrorCode.ROUTE_STALE
                if error.code is ConnectorSecretErrorCode.TARGET_MISMATCH
                else ConnectorTargetErrorCode.SECRET_UNAVAILABLE
            )
        except Exception:
            failure_code = ConnectorTargetErrorCode.UNAVAILABLE
        assert failure_code is not None
        message = {
            ConnectorTargetErrorCode.ROUTE_STALE: "connector route is stale",
            ConnectorTargetErrorCode.SECRET_UNAVAILABLE: "connector secret is unavailable",
        }.get(failure_code, "connector route is unavailable")
        raise ConnectorTargetError(failure_code, message)


def _managed_target(
    target: GovernedExecutionTarget | None,
) -> GovernedExecutionTarget:
    if (
        type(target) is not GovernedExecutionTarget
        or target.connector_kind is not SourceConnectorKind.POSTGRESQL
        or target.dialect is not SourceDialect.POSTGRESQL
    ):
        raise QueryPreviewRejectedError(
            "a dynamic PostgreSQL connector requires one exact governed target"
        )
    return target


def _require_query_target(
    query: ValidatedQuery,
    target: GovernedExecutionTarget,
) -> None:
    if (
        type(query) is not ValidatedQuery
        or query.dialect is not SourceDialect.POSTGRESQL
        or query.target_fingerprint != target.fingerprint
    ):
        raise QueryPreviewRejectedError(
            "the dynamic PostgreSQL query does not match its governed target"
        )


def _source_identity_mismatch() -> ConnectorTargetError:
    return ConnectorTargetError(
        ConnectorTargetErrorCode.ROUTE_STALE,
        "connector route is stale",
    )


@dataclass(frozen=True, slots=True)
class WorkerOnlyManagedQueryConnector:
    """Reject direct runtime reads; managed previews must cross the job worker boundary."""

    def execute(
        self,
        query: ValidatedQuery,
        *,
        target: GovernedExecutionTarget | None = None,
    ) -> QueryPreviewResult:
        del query, target
        raise QueryPreviewRejectedError(
            "managed query execution requires an authorized background worker"
        )

    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
        target: GovernedExecutionTarget | None = None,
    ) -> RejectedSourceReport:
        del checks, statement_timeout_ms, should_continue, target
        raise PlanningPortError(
            PlanningPortErrorCode.REJECTION_INSPECTION_FORBIDDEN,
            "managed source inspection requires an authorized background worker",
        )
