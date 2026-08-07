"""Simple and advanced Spanish requests reach guarded copyable PostgreSQL."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

import pytest
from sqlglot import exp, parse_one

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_ACCOUNT_BALANCE_SUM_QUESTION_ES,
    M32_CUSTOMER_COUNT_BY_COUNTRY_QUESTION_ES,
    M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES,
    M32_SHORT_REVENUE_RANKING_QUESTION_ES,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.adapters.query_studio.advanced_security import (
    HmacAdvancedQueryPreviewTokens,
)
from schemabridge.adapters.query_studio.advanced_semantic_index import (
    RegistryWideAdvancedSemanticIndex,
)
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.export import PostgresCopyableSqlRenderer
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.natural_sql import (
    ConfirmNaturalSqlPreview,
    GenerateGovernedCopyableSql,
    PrepareNaturalSqlPreview,
)
from schemabridge.application.query_execution import CompiledQuery
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_plans import RestrictedQueryPlan
from schemabridge.domain.advanced_query_studio import (
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
    AdvancedQueryRoute,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    WindowOperation,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.requests import AnalyticalRequest, MetricOperation
from schemabridge.domain.resolution import ResolutionLimits

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SqlExpressionType: TypeAlias = type[exp.Count] | type[exp.Sum] | type[exp.Rank]


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass(frozen=True, slots=True)
class FixedNonce:
    def new_nonce(self) -> str:
        return "nonce_language_coverage"


@dataclass(slots=True)
class CountingCompiler:
    calls: int = 0

    def compile(
        self,
        plan: RestrictedQueryPlan,
        *,
        max_preview_rows: int,
        target: GovernedExecutionTarget | None = None,
    ) -> CompiledQuery:
        self.calls += 1
        return PostgresQueryCompiler().compile(
            plan,
            max_preview_rows=max_preview_rows,
            target=target,
        )


@pytest.mark.parametrize(
    (
        "question",
        "expected_route",
        "expected_dataset",
        "expected_expression",
    ),
    (
        (
            M32_CUSTOMER_COUNT_BY_COUNTRY_QUESTION_ES,
            AdvancedQueryRoute.V1,
            "crm.customers",
            exp.Count,
        ),
        (
            M32_ACCOUNT_BALANCE_SUM_QUESTION_ES,
            AdvancedQueryRoute.V1,
            "bank.accounts",
            exp.Sum,
        ),
        (
            M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES,
            AdvancedQueryRoute.V1,
            "crm.customers",
            exp.Count,
        ),
        (
            M32_SHORT_REVENUE_RANKING_QUESTION_ES,
            AdvancedQueryRoute.V2,
            "sales.orders",
            exp.Rank,
        ),
    ),
    ids=("count-by-country", "sum-balance", "long-simple-v1", "short-ranking-v2"),
)
def test_spanish_request_builds_the_expected_guarded_copy_artifact(
    question: str,
    expected_route: AdvancedQueryRoute,
    expected_dataset: str,
    expected_expression: SqlExpressionType,
) -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)
    language = DeterministicAdvancedLanguageAdapter()
    tokens = HmacAdvancedQueryPreviewTokens(bytes(range(32)))
    clock = FixedClock()
    compiler = CountingCompiler()

    preparation = PrepareNaturalSqlPreview(
        registry=registry,
        mentions=language,
        retrieval=RegistryWideAdvancedSemanticIndex(),
        interpreter=language,
        preview_tokens=tokens,
        clock=clock,
        nonces=FixedNonce(),
    ).execute(
        AdvancedNaturalLanguageInput(
            text=question,
            language=UserLanguage.SPANISH,
        )
    )

    assert preparation.preview is not None
    assert preparation.token is not None
    assert preparation.preview.route is expected_route
    assert preparation.as_dict()["sql"] is None
    assert question not in json.dumps(preparation.as_dict(), sort_keys=True)
    assert compiler.calls == 0

    routed = preparation.preview.routed_request
    if expected_route is AdvancedQueryRoute.V1:
        assert isinstance(routed, AnalyticalRequest)
        expected_operation = (
            MetricOperation.SUM if expected_expression is exp.Sum else MetricOperation.COUNT
        )
        assert tuple(item.operation for item in routed.metrics) == (expected_operation,)
    else:
        assert isinstance(routed, AdvancedAnalyticalRequest)
        assert tuple(item.operation for item in routed.windows) == (WindowOperation.RANK,)

    confirmation = AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preparation.request_digest,
        preview_fingerprint=preparation.preview.fingerprint,
        routed_request_fingerprint=preparation.preview.routed_request_fingerprint,
        token=preparation.token,
    )
    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=clock,
    ).execute(preparation, confirmation)

    assert compiler.calls == 0
    result = GenerateGovernedCopyableSql(
        registry=registry,
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
        renderer=PostgresCopyableSqlRenderer(),
        limits=ResolutionLimits(),
    ).execute(confirmed)

    assert compiler.calls == 1
    assert result.artifact.executed is False
    assert result.artifact.plan_version == int(expected_route.value.removeprefix("v"))
    assert result.as_dict()["datasets"] == [expected_dataset]
    assert result.as_dict()["join_contracts"] == []
    assert "%s" not in result.artifact.sql
    assert re.search(r"\$\d+", result.artifact.sql) is None
    statement = parse_one(result.artifact.sql, read="postgres")
    assert isinstance(statement, exp.Select)
    assert statement.find(expected_expression) is not None


def test_request_length_has_no_routing_authority() -> None:
    assert len(M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES) > (
        4 * len(M32_SHORT_REVENUE_RANKING_QUESTION_ES)
    )
