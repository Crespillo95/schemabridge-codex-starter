from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.m32_natural_support import build_m32_simple_row_request

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_AMBIGUOUS_DATE_QUESTION_ES,
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
    M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES,
    AdvancedLanguageCase,
    AdvancedLanguageMentionFixture,
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
from schemabridge.application.natural_sql import (
    ConfirmNaturalSqlPreview,
    NaturalSqlError,
    NaturalSqlErrorCode,
    NaturalSqlPreparation,
    PrepareNaturalSqlPreview,
    route_advanced_request,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.application.query_execution import CompiledQuery
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_plans import RestrictedQueryPlan
from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationAmbiguity,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
    AdvancedQueryRoute,
    SignedAdvancedQueryPreviewToken,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    GroupingMode,
    LogicalBooleanPredicate,
    NumericBucket,
    OutputBooleanPredicate,
    OutputFilter,
    OutputOrder,
    WindowCalculation,
    WindowOperation,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.requests import (
    AnalyticalRequest,
    DateGrain,
    Filter,
    FilterOperator,
    SortDirection,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
KEY = bytes(range(32))


@dataclass(slots=True)
class MutableClock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current


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


def _registry() -> InMemoryGovernedSemanticRegistry:
    loaded = build_semantic_registry(repository_root=ROOT).load()
    return InMemoryGovernedSemanticRegistry(loaded.registry, loaded.scope)


def _query(text: str) -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(
        text=text,
        language=UserLanguage.SPANISH,
    )


def _prepare(
    text: str,
    *,
    registry: InMemoryGovernedSemanticRegistry | None = None,
    clock: MutableClock | None = None,
) -> tuple[
    NaturalSqlPreparation,
    InMemoryGovernedSemanticRegistry,
    HmacAdvancedQueryPreviewTokens,
    MutableClock,
]:
    selected_registry = registry or _registry()
    selected_clock = clock or MutableClock()
    language = DeterministicAdvancedLanguageAdapter()
    tokens = HmacAdvancedQueryPreviewTokens(KEY)
    preparation = PrepareNaturalSqlPreview(
        registry=selected_registry,
        mentions=language,
        retrieval=RegistryWideAdvancedSemanticIndex(),
        interpreter=language,
        preview_tokens=tokens,
        clock=selected_clock,
        nonces=FixedNonce(),
    ).execute(_query(text))
    return preparation, selected_registry, tokens, selected_clock


def _confirmation(
    preparation: NaturalSqlPreparation,
) -> AdvancedQueryConfirmation:
    assert preparation.preview is not None
    assert preparation.token is not None
    return AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preparation.request_digest,
        preview_fingerprint=preparation.preview.fingerprint,
        routed_request_fingerprint=(preparation.preview.routed_request_fingerprint),
        token=preparation.token,
    )


def _simple_aggregate() -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("Product"),
        fields=(AdvancedField(field=LogicalFieldRef("Product.category")),),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("Product.product_key"),
                alias="product_count",
            ),
        ),
        group_by=("category",),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("Product.is_active"),
                operator=FilterOperator.EQUALS,
                value=True,
            )
        ),
        result_order_by=(OutputOrder(alias="category"),),
        limit=100,
    )


def _rollup_request() -> AdvancedAnalyticalRequest:
    standard = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("Product"),
        fields=(
            AdvancedField(field=LogicalFieldRef("Product.category")),
            AdvancedField(
                field=LogicalFieldRef("Product.created_at"),
                grain=DateGrain.MONTH,
                alias="created_month",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("Product.product_key"),
                alias="product_count",
            ),
        ),
        group_by=("category", "created_month"),
    )
    return standard.model_copy(update={"grouping": GroupingMode.ROLLUP})


def test_simple_manual_aggregate_routes_to_the_unchanged_v1_contract() -> None:
    route, request = route_advanced_request(_simple_aggregate())

    assert route is AdvancedQueryRoute.V1
    assert isinstance(request, AnalyticalRequest)
    assert request.primary_entity == LogicalModelRef("Product")
    assert [item.field.root for item in request.dimensions] == ["Product.category"]
    assert [item.alias for item in request.metrics] == ["product_count"]


@pytest.mark.parametrize(
    ("feature", "advanced_request"),
    (
        ("row mode", build_m32_simple_row_request()),
        (
            "field alias",
            AdvancedAnalyticalRequest(
                version=2,
                mode=AdvancedQueryMode.AGGREGATE,
                primary_entity=LogicalModelRef("Product"),
                fields=(
                    AdvancedField(
                        field=LogicalFieldRef("Product.category"),
                        alias="category_label",
                    ),
                ),
                metrics=(
                    AdvancedMetric(
                        operation=AdvancedMetricOperation.COUNT_DISTINCT,
                        field=LogicalFieldRef("Product.product_key"),
                        alias="product_count",
                    ),
                ),
                group_by=("category_label",),
            ),
        ),
        (
            "HAVING",
            _simple_aggregate().model_copy(
                update={
                    "having": OutputBooleanPredicate.leaf(
                        OutputFilter(
                            alias="product_count",
                            operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                            value=2,
                        )
                    )
                }
            ),
        ),
        (
            "window",
            _simple_aggregate().model_copy(
                update={
                    "windows": (
                        WindowCalculation(
                            operation=WindowOperation.ROW_NUMBER,
                            alias="category_rank",
                            order_by=(
                                OutputOrder(
                                    alias="product_count",
                                    direction=SortDirection.DESC,
                                ),
                                OutputOrder(alias="category"),
                            ),
                        ),
                    )
                }
            ),
        ),
        (
            "post-filter",
            _simple_aggregate().model_copy(
                update={
                    "post_filter": OutputBooleanPredicate.leaf(
                        OutputFilter(
                            alias="product_count",
                            operator=FilterOperator.GREATER_THAN,
                            value=0,
                        )
                    )
                }
            ),
        ),
        (
            "numeric bucket",
            AdvancedAnalyticalRequest(
                version=2,
                mode=AdvancedQueryMode.AGGREGATE,
                primary_entity=LogicalModelRef("Product"),
                fields=(
                    AdvancedField(
                        field=LogicalFieldRef("Product.unit_price"),
                        buckets=(
                            NumericBucket(label="low", upper=50),
                            NumericBucket(label="high", lower=50),
                        ),
                        else_label="unknown",
                        alias="price_band",
                    ),
                ),
                metrics=(
                    AdvancedMetric(
                        operation=AdvancedMetricOperation.COUNT_DISTINCT,
                        field=LogicalFieldRef("Product.product_key"),
                        alias="product_count",
                    ),
                ),
                group_by=("price_band",),
            ),
        ),
        (
            "conditional aggregate",
            _simple_aggregate().model_copy(
                update={
                    "metrics": (
                        AdvancedMetric(
                            operation=AdvancedMetricOperation.COUNT_DISTINCT,
                            field=LogicalFieldRef("Product.product_key"),
                            alias="active_product_count",
                            condition=LogicalBooleanPredicate.leaf(
                                Filter(
                                    field=LogicalFieldRef("Product.is_active"),
                                    operator=FilterOperator.EQUALS,
                                    value=True,
                                )
                            ),
                        ),
                    )
                }
            ),
        ),
        (
            "row count",
            _simple_aggregate().model_copy(
                update={
                    "metrics": (
                        AdvancedMetric(
                            operation=AdvancedMetricOperation.COUNT_ROWS,
                            alias="row_count",
                        ),
                    )
                }
            ),
        ),
        (
            "OR predicate",
            _simple_aggregate().model_copy(
                update={
                    "where": LogicalBooleanPredicate.any_of(
                        LogicalBooleanPredicate.leaf(
                            Filter(
                                field=LogicalFieldRef("Product.category"),
                                operator=FilterOperator.EQUALS,
                                value="BOOKS",
                            )
                        ),
                        LogicalBooleanPredicate.leaf(
                            Filter(
                                field=LogicalFieldRef("Product.category"),
                                operator=FilterOperator.EQUALS,
                                value="HOME",
                            )
                        ),
                    )
                }
            ),
        ),
        (
            "NOT predicate",
            _simple_aggregate().model_copy(
                update={
                    "where": LogicalBooleanPredicate.negate(
                        LogicalBooleanPredicate.leaf(
                            Filter(
                                field=LogicalFieldRef("Product.is_active"),
                                operator=FilterOperator.EQUALS,
                                value=False,
                            )
                        )
                    )
                }
            ),
        ),
        (
            "metric output order",
            _simple_aggregate().model_copy(
                update={
                    "result_order_by": (
                        OutputOrder(
                            alias="product_count",
                            direction=SortDirection.DESC,
                        ),
                    )
                }
            ),
        ),
    ),
)
def test_each_representative_advanced_shape_routes_to_v2(
    feature: str,
    advanced_request: AdvancedAnalyticalRequest,
) -> None:
    route, routed = route_advanced_request(advanced_request)

    assert feature
    assert route is AdvancedQueryRoute.V2
    assert routed is advanced_request


def test_natural_rollup_request_is_rejected_before_preview_or_compilation() -> None:
    query = _query(
        "Agrupa Product.category y Product.created_at, cuenta Product.product_key "
        "y añade subtotales."
    )
    language = DeterministicAdvancedLanguageAdapter(
        cases=(
            AdvancedLanguageCase(
                query=query,
                mentions=(
                    AdvancedLanguageMentionFixture(
                        phrase="Product.category",
                        purpose=AdvancedMentionPurpose.DIMENSION,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="Product.created_at",
                        purpose=AdvancedMentionPurpose.DIMENSION,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="Product.product_key",
                        purpose=AdvancedMentionPurpose.METRIC,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="subtotales",
                        purpose=AdvancedMentionPurpose.GROUPING,
                    ),
                ),
                request=_rollup_request(),
            ),
        )
    )
    registry = _registry()
    compiler = CountingCompiler()

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        PrepareNaturalSqlPreview(
            registry=registry,
            mentions=language,
            retrieval=RegistryWideAdvancedSemanticIndex(),
            interpreter=language,
            preview_tokens=HmacAdvancedQueryPreviewTokens(KEY),
            clock=MutableClock(),
            nonces=FixedNonce(),
        ).execute(query)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT
    assert compiler.calls == 0


def test_simple_active_products_row_request_remains_v2() -> None:
    preparation, _registry_port, _tokens, _clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)

    assert preparation.preview is not None
    assert preparation.preview.route is AdvancedQueryRoute.V2
    assert preparation.preview.routed_request == build_m32_simple_row_request()
    payload = preparation.as_dict()
    assert payload["sql"] is None
    assert payload["compiled"] is False
    assert payload["executed"] is False


@pytest.mark.parametrize(
    ("question", "ambiguity"),
    (
        (
            M32_AMBIGUOUS_DATE_QUESTION_ES,
            AdvancedInterpretationAmbiguity.DATE_MEANING,
        ),
        (
            M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES,
            AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,
        ),
    ),
)
def test_ambiguous_or_unsupported_language_returns_no_sql(
    question: str,
    ambiguity: AdvancedInterpretationAmbiguity,
) -> None:
    compiler = CountingCompiler()

    preparation, _registry_port, _tokens, _clock = _prepare(question)

    assert preparation.preview is None
    assert preparation.token is None
    assert preparation.ambiguities == (ambiguity,)
    assert preparation.as_dict()["sql"] is None
    assert compiler.calls == 0


def test_tampered_qsp3_fails_before_compilation() -> None:
    preparation, registry, tokens, clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    assert preparation.token is not None
    position = len("qsp3.") + 8
    replacement = "A" if preparation.token.root[position] != "A" else "B"
    tampered_token = SignedAdvancedQueryPreviewToken(
        preparation.token.root[:position] + replacement + preparation.token.root[position + 1 :]
    )
    tampered_preparation = replace(preparation, token=tampered_token)
    tampered_confirmation = _confirmation(tampered_preparation)
    compiler = CountingCompiler()

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        ConfirmNaturalSqlPreview(
            registry=registry,
            preview_tokens=tokens,
            clock=clock,
        ).execute(tampered_preparation, tampered_confirmation)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_INVALID
    assert compiler.calls == 0


def test_expired_qsp3_fails_before_compilation() -> None:
    preparation, registry, tokens, clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    confirmation = _confirmation(preparation)
    compiler = CountingCompiler()
    clock.current = NOW + timedelta(minutes=10, microseconds=1)

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        ConfirmNaturalSqlPreview(
            registry=registry,
            preview_tokens=tokens,
            clock=clock,
        ).execute(preparation, confirmation)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_EXPIRED
    assert compiler.calls == 0


def test_registry_drift_fails_before_compilation() -> None:
    preparation, registry, tokens, clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    confirmation = _confirmation(preparation)
    compiler = CountingCompiler()
    registry.registry = registry.registry.model_copy(
        update={"version": registry.registry.version + 1}
    )

    with pytest.raises(NaturalSqlError) as captured:
        ConfirmNaturalSqlPreview(
            registry=registry,
            preview_tokens=tokens,
            clock=clock,
        ).execute(preparation, confirmation)

    assert captured.value.code is NaturalSqlErrorCode.STALE_CONTEXT
    assert compiler.calls == 0


def test_active_registry_pointer_drift_fails_before_compilation() -> None:
    registry = _registry()
    registry.activation_generation = 1
    registry.active_pointer_fingerprint = "a" * 64
    preparation, registry, tokens, clock = _prepare(
        M32_SIMPLE_PRODUCTS_QUESTION_ES,
        registry=registry,
    )
    confirmation = _confirmation(preparation)
    compiler = CountingCompiler()
    registry.activation_generation = 2
    registry.active_pointer_fingerprint = "b" * 64

    with pytest.raises(NaturalSqlError) as captured:
        ConfirmNaturalSqlPreview(
            registry=registry,
            preview_tokens=tokens,
            clock=clock,
        ).execute(preparation, confirmation)

    assert captured.value.code is NaturalSqlErrorCode.STALE_CONTEXT
    assert compiler.calls == 0


def test_preparation_rejects_a_semantic_context_swapped_after_signing() -> None:
    preparation, _registry_port, _tokens, _clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)
    changed_context = preparation.semantic_context.model_copy(
        update={"context_version": preparation.semantic_context.context_version + 1}
    )

    with pytest.raises(ValueError, match="differs from the signed preview"):
        replace(preparation, semantic_context=changed_context)


def test_natural_count_that_requires_automatic_distinct_is_not_confirmable() -> None:
    query = _query("Agrupa por SaleLine.product_key y cuenta SalesOrder.order_key.")
    request = AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SalesOrder"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("SaleLine.product_key"),
                alias="product_key",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT,
                field=LogicalFieldRef("SalesOrder.order_key"),
                alias="order_count",
            ),
        ),
        group_by=("product_key",),
    )
    language = DeterministicAdvancedLanguageAdapter(
        cases=(
            AdvancedLanguageCase(
                query=query,
                mentions=(
                    AdvancedLanguageMentionFixture(
                        phrase="SaleLine.product_key",
                        purpose=AdvancedMentionPurpose.GROUPING,
                    ),
                    AdvancedLanguageMentionFixture(
                        phrase="SalesOrder.order_key",
                        purpose=AdvancedMentionPurpose.METRIC,
                    ),
                ),
                request=request,
            ),
        )
    )

    with pytest.raises(NaturalSqlError) as captured:
        PrepareNaturalSqlPreview(
            registry=_registry(),
            mentions=language,
            retrieval=RegistryWideAdvancedSemanticIndex(),
            interpreter=language,
            preview_tokens=HmacAdvancedQueryPreviewTokens(KEY),
            clock=MutableClock(),
            nonces=FixedNonce(),
        ).execute(query)

    assert captured.value.code is NaturalSqlErrorCode.FANOUT_MITIGATION_REQUIRED


def test_confirmable_preview_does_not_retain_raw_text_or_sql() -> None:
    preparation, _registry_port, _tokens, _clock = _prepare(M32_SIMPLE_PRODUCTS_QUESTION_ES)

    payload = preparation.as_dict()
    encoded = json.dumps(payload, sort_keys=True)
    assert preparation.can_confirm
    assert payload["sql"] is None
    assert payload["compiled"] is False
    assert payload["executed"] is False
    assert M32_SIMPLE_PRODUCTS_QUESTION_ES not in encoded
