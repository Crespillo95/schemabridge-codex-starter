"""M32 Spanish natural language to confirmed standalone PostgreSQL."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlglot import exp, parse_one
from tests.m32_natural_support import M32_REFERENCE_QUESTION

from schemabridge.adapters.query_studio.advanced_fake_language import (
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
from schemabridge.domain.advanced_plans import AdvancedQueryPlan, RestrictedQueryPlan
from schemabridge.domain.advanced_query_studio import (
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
    AdvancedQueryRoute,
)
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.resolution import ResolutionLimits

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
M32_REFERENCE_SQL_SHA256 = "ec589a1527d0d641f4f7f7eb7e052ca1ace5b092013b437da72542ce4145d4f3"


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass(frozen=True, slots=True)
class FixedNonce:
    def new_nonce(self) -> str:
        return "nonce_ABCDEFGHIJKLMNOP"


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


def test_reference_spanish_request_requires_confirmation_then_builds_copyable_sql() -> None:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    registry = InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)
    language = DeterministicAdvancedLanguageAdapter()
    tokens = HmacAdvancedQueryPreviewTokens(bytes(range(32)))
    clock = FixedClock()
    compiler = CountingCompiler()
    query = AdvancedNaturalLanguageInput(
        text=M32_REFERENCE_QUESTION,
        language=UserLanguage.SPANISH,
    )

    preparation = PrepareNaturalSqlPreview(
        registry=registry,
        mentions=language,
        retrieval=RegistryWideAdvancedSemanticIndex(),
        interpreter=language,
        preview_tokens=tokens,
        clock=clock,
        nonces=FixedNonce(),
    ).execute(query)

    assert preparation.preview is not None
    assert preparation.token is not None
    assert preparation.token.root.startswith("qsp2.")
    assert preparation.preview.route is AdvancedQueryRoute.V2
    assert len(preparation.preview.mapping_reviews) == 9
    assert all(item.evidence for item in preparation.preview.mapping_reviews)
    assert len(preparation.preview.join_reviews) == 2
    assert all(item.evidence for item in preparation.preview.join_reviews)
    assert tuple(item.root for item in preparation.preview.validated_request.required_models) == (
        "SaleLine",
        "SalesOrder",
        "Product",
    )
    preview_payload = preparation.as_dict()
    assert preview_payload["sql"] is None
    assert preview_payload["compiled"] is False
    assert preview_payload["executed"] is False
    assert M32_REFERENCE_QUESTION not in json.dumps(
        preview_payload,
        sort_keys=True,
    )
    assert compiler.calls == 0

    confirmation = AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preparation.request_digest,
        preview_fingerprint=preparation.preview.fingerprint,
        routed_request_fingerprint=(preparation.preview.routed_request_fingerprint),
        token=preparation.token,
    )
    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=clock,
    ).execute(preparation, confirmation)

    assert confirmed.validated_request == preparation.preview.validated_request
    assert compiler.calls == 0

    result = GenerateGovernedCopyableSql(
        registry=registry,
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
        renderer=PostgresCopyableSqlRenderer(),
        limits=ResolutionLimits(),
    ).execute(confirmed)

    assert compiler.calls == 1
    assert result.artifact.plan_version == 2
    assert result.artifact.executed is False
    assert result.artifact.sha256 == M32_REFERENCE_SQL_SHA256
    assert isinstance(result.resolved_plan.query_plan, AdvancedQueryPlan)
    assert result.resolved_plan.query_plan.version == 2
    assert len(result.resolved_plan.query_plan.joins) == 2
    assert {asset.dataset.root for asset in result.resolved_plan.query_policy.assets} == {
        "sales.order_lines",
        "sales.orders",
        "commerce.products",
    }
    assert [item.id for item in result.resolved_plan.selected_contracts] == [
        "sales_order_to_sale_line",
        "product_to_sale_line",
    ]

    sql = result.artifact.sql
    assert "%s" not in sql
    assert re.search(r"\$\d+", sql) is None
    statement = parse_one(sql, read="postgres")
    assert isinstance(statement, exp.Select)
    with_clause = statement.args.get("with_")
    assert isinstance(with_clause, exp.With)
    assert tuple(item.alias_or_name for item in with_clause.expressions) == (
        "aggregated",
        "windowed",
    )
    assert len(tuple(statement.find_all(exp.Select))) == 3
    assert result.as_dict()["executed"] is False
