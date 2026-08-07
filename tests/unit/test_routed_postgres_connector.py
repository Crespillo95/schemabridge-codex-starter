"""Tests for dynamic route/secret reload before every PostgreSQL source operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
    ResolvedPostgresSecret,
)
from schemabridge.adapters.connectors.routed_postgres import RoutedPostgresQueryConnector
from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.planning import ProtectedSourceOperationCancelled
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostBudget,
    QueryCostDecision,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.resolution import RejectedSourceReport, RejectionCheck

_SENSITIVE_DSN = "postgresql://reader:secret@private.invalid/source"
_SENSITIVE_BINDING = "vault:execution:sensitive"


def _target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost=Decimal("1000"),
        max_estimated_rows=10_000,
        max_plan_nodes=100,
        max_plan_depth=10,
        max_plan_width=1_024,
    )
    return GovernedExecutionTarget(
        workspace_id="tenant-a",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=7,
        route_fingerprint="a" * 64,
        expected_reader="schemabridge_reader",
        source_identity_fingerprint="d" * 64,
        catalog_identity_fingerprint="e" * 64,
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _query(target: GovernedExecutionTarget) -> ValidatedQuery:
    return ValidatedQuery(
        sql="SELECT c.customer_id FROM crm.customers AS c LIMIT 10",
        parameters=(),
        max_rows=10,
        statement_timeout_ms=5_000,
        dialect=SourceDialect.POSTGRESQL,
        target_fingerprint=target.fingerprint,
    )


def _check() -> RejectionCheck:
    key = build_north_star_join_proposals()[0].left_key
    return RejectionCheck(
        logical_field=key.logical_field,
        physical_field=key.physical_field,
        transformation_plan=key.transformation_plan,
    )


def _assessment(target: GovernedExecutionTarget) -> QueryCostAssessment:
    return QueryCostAssessment(
        decision=QueryCostDecision.ACCEPTED,
        total_cost=Decimal("10"),
        estimated_root_rows=100,
        plan_width=16,
        plan_node_count=2,
        plan_depth=2,
        response_bytes=512,
        observed_reader=target.expected_reader,
        read_only=True,
        explain_timeout_ms=target.cost_budget.explain_timeout_ms,
        target_fingerprint=target.fingerprint,
        budget_fingerprint=target.cost_budget_fingerprint,
        cost_budget=target.cost_budget,
    )


@dataclass
class _ReferenceLoader:
    calls: list[str] = field(default_factory=list)

    def __call__(self, target: GovernedExecutionTarget) -> OpaqueConnectorSecretRef:
        self.calls.append(target.fingerprint)
        return OpaqueConnectorSecretRef(_SENSITIVE_BINDING)


@dataclass
class _SecretResolver:
    calls: list[str] = field(default_factory=list)

    def resolve(
        self,
        reference: OpaqueConnectorSecretRef,
        target: GovernedExecutionTarget,
    ) -> ResolvedPostgresSecret:
        self.calls.append(reference.value)
        return ResolvedPostgresSecret(
            dsn=_SENSITIVE_DSN,
            expected_reader=target.expected_reader,
            dialect=target.dialect,
        )


def test_route_and_secret_are_reloaded_before_cost_preview_and_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    query = _query(target)
    check = _check()
    loader = _ReferenceLoader()
    resolver = _SecretResolver()
    source_calls: list[str] = []

    class _Cost:
        def __init__(self, dsn: str, *, connect_timeout_seconds: int) -> None:
            assert dsn == _SENSITIVE_DSN
            assert connect_timeout_seconds == 3

        def assess(
            self,
            received_query: ValidatedQuery,
            received_target: GovernedExecutionTarget,
        ) -> QueryCostAssessment:
            assert received_query == query
            source_calls.append("cost")
            return _assessment(received_target)

    class _Preview:
        def __init__(self, dsn: str, **kwargs: object) -> None:
            assert dsn == _SENSITIVE_DSN
            assert kwargs["bound_target_fingerprint"] == target.fingerprint

        def execute(
            self,
            received_query: ValidatedQuery,
            *,
            target: GovernedExecutionTarget,
        ) -> QueryPreviewResult:
            assert received_query == query
            source_calls.append("preview")
            return QueryPreviewResult(
                columns=("customer_id",),
                rows=(("1",),),
                database_user=target.expected_reader,
                transaction_read_only=True,
                statement_timeout_ms=5_000,
                truncated=False,
            )

    class _Reporter:
        def __init__(self, dsn: str, **kwargs: object) -> None:
            assert dsn == _SENSITIVE_DSN
            assert kwargs["bound_target_fingerprint"] == target.fingerprint

        def inspect(
            self,
            checks: tuple[RejectionCheck, ...],
            **kwargs: object,
        ) -> RejectedSourceReport:
            assert checks == (check,)
            source_calls.append("rejections")
            timeout = kwargs["statement_timeout_ms"]
            assert isinstance(timeout, int)
            return RejectedSourceReport(
                inspected_fields=(check.physical_field,),
                database_user=target.expected_reader,
                transaction_read_only=True,
                statement_timeout_ms=timeout,
            )

    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgQueryCostPreflight",
        _Cost,
    )
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgQueryPreview",
        _Preview,
    )
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgRejectedSourceReporter",
        _Reporter,
    )
    connector = RoutedPostgresQueryConnector(
        loader,
        resolver,  # type: ignore[arg-type]
        allowed_fields=frozenset({check.physical_field.root}),
    )

    assert connector.assess(query, target).decision is QueryCostDecision.ACCEPTED
    assert connector.execute(query, target=target).rows == (("1",),)
    assert connector.inspect(
        (check,),
        statement_timeout_ms=5_000,
        target=target,
    ).inspected_fields == (check.physical_field,)

    assert loader.calls == [target.fingerprint] * 3
    assert resolver.calls == [_SENSITIVE_BINDING] * 3
    assert source_calls == ["cost", "preview", "rejections"]
    assert _SENSITIVE_DSN not in repr(connector)
    assert _SENSITIVE_BINDING not in repr(connector)


def test_secret_failure_is_sanitized_without_exception_chaining() -> None:
    target = _target()

    class _UnavailableResolver:
        def resolve(self, *_args: object) -> ResolvedPostgresSecret:
            raise ConnectorSecretResolutionError(
                ConnectorSecretErrorCode.UNAVAILABLE,
                f"sensitive failure {_SENSITIVE_DSN}",
            )

    connector = RoutedPostgresQueryConnector(
        _ReferenceLoader(),
        _UnavailableResolver(),  # type: ignore[arg-type]
        allowed_fields=frozenset(),
    )

    with pytest.raises(ConnectorTargetError) as captured:
        connector.execute(_query(target), target=target)

    assert captured.value.code is ConnectorTargetErrorCode.SECRET_UNAVAILABLE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert _SENSITIVE_DSN not in str(captured.value)
    assert _SENSITIVE_BINDING not in str(captured.value)


def test_cancellation_prevents_route_secret_and_source_access() -> None:
    target = _target()
    loader = _ReferenceLoader()
    resolver = _SecretResolver()
    connector = RoutedPostgresQueryConnector(
        loader,
        resolver,  # type: ignore[arg-type]
        allowed_fields=frozenset({_check().physical_field.root}),
    )

    with pytest.raises(ProtectedSourceOperationCancelled):
        connector.inspect(
            (_check(),),
            statement_timeout_ms=5_000,
            should_continue=lambda: False,
            target=target,
        )

    assert loader.calls == []
    assert resolver.calls == []


@pytest.mark.parametrize("operation", ("cost", "preview"))
def test_query_target_mismatch_prevents_route_and_secret_access(operation: str) -> None:
    target = _target()
    other_target = target.model_copy(
        update={
            "workspace_id": "tenant-b",
        }
    )
    loader = _ReferenceLoader()
    resolver = _SecretResolver()
    connector = RoutedPostgresQueryConnector(
        loader,
        resolver,  # type: ignore[arg-type]
        allowed_fields=frozenset(),
    )

    with pytest.raises(QueryPreviewRejectedError, match="governed target"):
        if operation == "cost":
            connector.assess(_query(target), other_target)
        else:
            connector.execute(_query(target), target=other_target)

    assert loader.calls == []
    assert resolver.calls == []


@pytest.mark.parametrize("operation", ("cost", "preview", "rejections"))
def test_observed_source_identity_mismatch_is_one_sanitized_stale_route(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    target = _target()
    query = _query(target)
    check = _check()

    class _MismatchedCost:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def assess(self, *_args: object, **_kwargs: object) -> QueryCostAssessment:
            raise PostgresSourceIdentityMismatchError("sanitized identity mismatch")

    class _MismatchedPreview:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def execute(self, *_args: object, **_kwargs: object) -> QueryPreviewResult:
            raise PostgresSourceIdentityMismatchError("sanitized identity mismatch")

    class _MismatchedReporter:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def inspect(self, *_args: object, **_kwargs: object) -> RejectedSourceReport:
            raise PostgresSourceIdentityMismatchError("sanitized identity mismatch")

    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgQueryCostPreflight",
        _MismatchedCost,
    )
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgQueryPreview",
        _MismatchedPreview,
    )
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.routed_postgres.PsycopgRejectedSourceReporter",
        _MismatchedReporter,
    )
    connector = RoutedPostgresQueryConnector(
        _ReferenceLoader(),
        _SecretResolver(),  # type: ignore[arg-type]
        allowed_fields=frozenset({check.physical_field.root}),
    )

    with pytest.raises(ConnectorTargetError) as raised:
        if operation == "cost":
            connector.assess(query, target)
        elif operation == "preview":
            connector.execute(query, target=target)
        else:
            connector.inspect(
                (check,),
                statement_timeout_ms=5_000,
                target=target,
            )

    assert raised.value.code is ConnectorTargetErrorCode.ROUTE_STALE
    assert str(raised.value) == "connector route is stale"
    assert raised.value.__cause__ is None
    for forbidden in (_SENSITIVE_DSN, _SENSITIVE_BINDING, "sanitized identity mismatch"):
        assert forbidden not in str(raised.value)
