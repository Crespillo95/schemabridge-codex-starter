"""Deterministic, zero-I/O M32 language adapter for synthetic tests and demos."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.domain.advanced_query_studio import (
    MAX_ADVANCED_MENTIONS,
    AdvancedInterpretationAmbiguity,
    AdvancedInterpretationEnvelope,
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtraction,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
    AdvancedQueryMention,
    AdvancedSourceSpan,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    AdvancedMetric,
    AdvancedMetricOperation,
    AdvancedQueryMode,
    LogicalBooleanPredicate,
    OutputBooleanPredicate,
    OutputFilter,
    OutputOrder,
    WindowCalculation,
    WindowOperation,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.requests import (
    DateGrain,
    Filter,
    FilterOperator,
    SortDirection,
)

M32_REFERENCE_QUESTION_ES = (
    "Para cada mes, en pedidos completados, calcula por categoría de producto "
    "los ingresos netos, unidades y pedidos distintos. Conserva solo las categorías "
    "con al menos 4 pedidos distintos; ordénalas por ingresos dentro de cada mes, "
    "desempatando alfabéticamente por categoría; asigna una posición única, calcula "
    "su porcentaje sobre los ingresos de las categorías elegibles del mes y el ingreso "
    "acumulado, y devuelve como máximo las tres primeras categorías de cada mes."
)
M32_SIMPLE_PRODUCTS_QUESTION_ES = (
    "Muestra los productos activos con identificador, categoría y precio unitario; "
    "ordénalos por categoría y después por identificador; devuelve como máximo 50 filas."
)
M32_CUSTOMER_COUNT_BY_COUNTRY_QUESTION_ES = "Cuenta clientes por país."
M32_ACCOUNT_BALANCE_SUM_QUESTION_ES = "Suma el saldo de las cuentas."
M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES = (
    "Para preparar un informe comercial detallado para el equipo de análisis y mantener "
    "una salida clara y sencilla, necesito que cuentes los clientes, agrupes el resultado "
    "únicamente por país y devuelvas como máximo 100 filas."
)
M32_SHORT_REVENUE_RANKING_QUESTION_ES = "Ranking de facturación por región."
M32_AMBIGUOUS_DATE_QUESTION_ES = "Muestra las ventas por fecha."
M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES = (
    "Cruza todos los productos con todos los pedidos, incluso sin relación."
)

_MAX_FAKE_CASES = 32


@dataclass(frozen=True, slots=True)
class AdvancedLanguageMentionFixture:
    """One exact phrase occurrence used to construct a grounded mention."""

    phrase: str
    purpose: AdvancedMentionPurpose
    occurrence: int = 1

    def __post_init__(self) -> None:
        if not self.phrase or not self.phrase.strip():
            raise ValueError("advanced language mention phrase must not be blank")
        if not isinstance(self.purpose, AdvancedMentionPurpose):
            raise TypeError("advanced language mention purpose is invalid")
        if self.occurrence < 1:
            raise ValueError("advanced language mention occurrence must be positive")


@dataclass(frozen=True, slots=True)
class AdvancedLanguageCase:
    """Injectable deterministic language case with exactly one typed outcome."""

    query: AdvancedNaturalLanguageInput
    mentions: tuple[AdvancedLanguageMentionFixture, ...]
    request: AdvancedAnalyticalRequest | None = None
    ambiguities: tuple[AdvancedInterpretationAmbiguity, ...] = ()

    def __post_init__(self) -> None:
        if not self.mentions or len(self.mentions) > MAX_ADVANCED_MENTIONS:
            raise ValueError("advanced language case requires between one and twelve mentions")
        if (self.request is None) == (not self.ambiguities):
            raise ValueError("advanced language case requires one request or ambiguities")
        if len(self.ambiguities) != len(set(self.ambiguities)):
            raise ValueError("advanced language case ambiguities must be unique")
        _build_extraction(self)


@dataclass(frozen=True, slots=True)
class DeterministicAdvancedLanguageAdapter:
    """Implement both M32 language ports over an exact injectable case set."""

    cases: tuple[AdvancedLanguageCase, ...] = field(
        default_factory=lambda: default_advanced_language_cases()
    )

    def __post_init__(self) -> None:
        if not self.cases or len(self.cases) > _MAX_FAKE_CASES:
            raise ValueError("advanced language adapter requires one to thirty-two cases")
        keys = tuple((item.query.language, item.query.text) for item in self.cases)
        if len(keys) != len(set(keys)):
            raise ValueError("advanced language adapter cases must be unique")

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        case = self._case_for(value.query)
        return AdvancedMentionExtractionResult(
            input=value,
            extraction=_build_extraction(case),
            usage=None,
        )

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        case = self._case_for(value.query)
        expected_extraction = _build_extraction(case)
        if value.extraction != expected_extraction:
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INPUT_MISMATCH,
                "advanced language mentions do not match the deterministic extraction",
            )
        try:
            envelope = AdvancedInterpretationEnvelope(
                request_digest=value.query.digest,
                mention_fingerprint=value.extraction.fingerprint,
                semantic_context_fingerprint=value.context.fingerprint,
                request=case.request,
                ambiguities=case.ambiguities,
            )
        except ValidationError as error:
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.INVALID_ADAPTER_OUTPUT,
                "advanced language adapter produced an invalid typed outcome",
            ) from error
        try:
            return AdvancedInterpretationResult(
                input=value,
                envelope=envelope,
                usage=None,
            )
        except ValidationError as error:
            raise AdvancedQueryStudioPortError(
                AdvancedQueryStudioPortErrorCode.GOVERNED_CONTEXT_MISMATCH,
                "advanced language outcome is incompatible with the governed context",
            ) from error

    def _case_for(self, query: AdvancedNaturalLanguageInput) -> AdvancedLanguageCase:
        for case in self.cases:
            if case.query == query:
                return case
        raise AdvancedQueryStudioPortError(
            AdvancedQueryStudioPortErrorCode.UNSUPPORTED_INPUT,
            "advanced language input is not supported by this deterministic adapter",
        )


def default_advanced_language_cases() -> tuple[AdvancedLanguageCase, ...]:
    """Return fresh immutable fixtures for accepted and closed synthetic outcomes."""

    return (
        AdvancedLanguageCase(
            query=_spanish_query(M32_REFERENCE_QUESTION_ES),
            mentions=(
                _mention("cada mes", AdvancedMentionPurpose.GROUPING),
                _mention("pedidos completados", AdvancedMentionPurpose.FILTER),
                _mention("categoría de producto", AdvancedMentionPurpose.DIMENSION),
                _mention("ingresos netos", AdvancedMentionPurpose.METRIC),
                _mention("unidades", AdvancedMentionPurpose.METRIC),
                _mention("pedidos distintos", AdvancedMentionPurpose.METRIC),
                _mention(
                    "al menos 4 pedidos distintos",
                    AdvancedMentionPurpose.FILTER,
                ),
                _mention(
                    "ordénalas por ingresos dentro de cada mes, "
                    "desempatando alfabéticamente por categoría",
                    AdvancedMentionPurpose.ORDERING,
                ),
                _mention("posición única", AdvancedMentionPurpose.WINDOW),
                _mention(
                    "porcentaje sobre los ingresos de las categorías elegibles del mes",
                    AdvancedMentionPurpose.WINDOW,
                ),
                _mention("ingreso acumulado", AdvancedMentionPurpose.WINDOW),
                _mention(
                    "tres primeras categorías de cada mes",
                    AdvancedMentionPurpose.LIMIT,
                ),
            ),
            request=_build_reference_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_SIMPLE_PRODUCTS_QUESTION_ES),
            mentions=(
                _mention("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                _mention("activos", AdvancedMentionPurpose.FILTER),
                _mention("identificador", AdvancedMentionPurpose.DIMENSION),
                _mention("categoría", AdvancedMentionPurpose.DIMENSION),
                _mention("precio unitario", AdvancedMentionPurpose.DIMENSION),
                _mention(
                    "categoría y después por identificador",
                    AdvancedMentionPurpose.ORDERING,
                ),
                _mention("máximo 50 filas", AdvancedMentionPurpose.LIMIT),
            ),
            request=_build_simple_products_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_CUSTOMER_COUNT_BY_COUNTRY_QUESTION_ES),
            mentions=(
                _mention("clientes", AdvancedMentionPurpose.METRIC),
                _mention("país", AdvancedMentionPurpose.DIMENSION),
            ),
            request=_build_customer_count_by_country_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_ACCOUNT_BALANCE_SUM_QUESTION_ES),
            mentions=(
                _mention("saldo", AdvancedMentionPurpose.METRIC),
                _mention("cuentas", AdvancedMentionPurpose.PRIMARY_ENTITY),
            ),
            request=_build_account_balance_sum_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES),
            mentions=(
                _mention("clientes", AdvancedMentionPurpose.METRIC),
                _mention("país", AdvancedMentionPurpose.DIMENSION),
                _mention("máximo 100 filas", AdvancedMentionPurpose.LIMIT),
            ),
            request=_build_customer_count_by_country_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_SHORT_REVENUE_RANKING_QUESTION_ES),
            mentions=(
                _mention("Ranking", AdvancedMentionPurpose.WINDOW),
                _mention("facturación", AdvancedMentionPurpose.METRIC),
                _mention("región", AdvancedMentionPurpose.DIMENSION),
            ),
            request=_build_short_revenue_ranking_request(),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_AMBIGUOUS_DATE_QUESTION_ES),
            mentions=(
                _mention("ventas", AdvancedMentionPurpose.PRIMARY_ENTITY),
                _mention("fecha", AdvancedMentionPurpose.DIMENSION),
            ),
            ambiguities=(AdvancedInterpretationAmbiguity.DATE_MEANING,),
        ),
        AdvancedLanguageCase(
            query=_spanish_query(M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES),
            mentions=(
                _mention("productos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                _mention("pedidos", AdvancedMentionPurpose.PRIMARY_ENTITY),
                _mention("sin relación", AdvancedMentionPurpose.GROUPING),
            ),
            ambiguities=(AdvancedInterpretationAmbiguity.UNSUPPORTED_REQUEST,),
        ),
    )


def _spanish_query(text: str) -> AdvancedNaturalLanguageInput:
    return AdvancedNaturalLanguageInput(text=text, language=UserLanguage.SPANISH)


def _mention(
    phrase: str,
    purpose: AdvancedMentionPurpose,
    *,
    occurrence: int = 1,
) -> AdvancedLanguageMentionFixture:
    return AdvancedLanguageMentionFixture(
        phrase=phrase,
        purpose=purpose,
        occurrence=occurrence,
    )


def _build_extraction(case: AdvancedLanguageCase) -> AdvancedMentionExtraction:
    mentions = tuple(
        sorted(
            (_grounded_mention(case.query.text, fixture) for fixture in case.mentions),
            key=lambda item: (
                item.source_span.start,
                item.source_span.end,
                item.purpose.value,
            ),
        )
    )
    return AdvancedMentionExtraction(
        request_digest=case.query.digest,
        mentions=mentions,
    )


def _grounded_mention(
    text: str,
    fixture: AdvancedLanguageMentionFixture,
) -> AdvancedQueryMention:
    start = _occurrence_start(text, fixture.phrase, fixture.occurrence)
    return AdvancedQueryMention(
        value=fixture.phrase,
        source_span=AdvancedSourceSpan(
            start=start,
            end=start + len(fixture.phrase),
        ),
        purpose=fixture.purpose,
    )


def _occurrence_start(text: str, phrase: str, occurrence: int) -> int:
    cursor = 0
    start = -1
    for _ in range(occurrence):
        start = text.find(phrase, cursor)
        if start < 0:
            raise ValueError("advanced language mention is not grounded in its configured query")
        cursor = start + len(phrase)
    return start


def _build_reference_request() -> AdvancedAnalyticalRequest:
    revenue_order = (
        OutputOrder(alias="net_revenue", direction=SortDirection.DESC),
        OutputOrder(alias="category", direction=SortDirection.ASC),
    )
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SaleLine"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("SalesOrder.ordered_at"),
                grain=DateGrain.MONTH,
                alias="month",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.category"),
                alias="category",
            ),
        ),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.net_amount"),
                alias="net_revenue",
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SaleLine.quantity"),
                alias="units",
            ),
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("SalesOrder.order_key"),
                alias="distinct_orders",
            ),
        ),
        group_by=("month", "category"),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("SalesOrder.order_status"),
                operator=FilterOperator.EQUALS,
                value="COMPLETED",
            )
        ),
        having=OutputBooleanPredicate.leaf(
            OutputFilter(
                alias="distinct_orders",
                operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                value=4,
            )
        ),
        windows=(
            WindowCalculation(
                operation=WindowOperation.ROW_NUMBER,
                alias="revenue_rank",
                partition_by=("month",),
                order_by=revenue_order,
            ),
            WindowCalculation(
                operation=WindowOperation.PERCENT_OF_TOTAL,
                alias="revenue_percent",
                source="net_revenue",
                partition_by=("month",),
            ),
            WindowCalculation(
                operation=WindowOperation.RUNNING_SUM,
                alias="cumulative_revenue",
                source="net_revenue",
                partition_by=("month",),
                order_by=revenue_order,
            ),
        ),
        post_filter=OutputBooleanPredicate.leaf(
            OutputFilter(
                alias="revenue_rank",
                operator=FilterOperator.LESS_THAN_OR_EQUAL,
                value=3,
            )
        ),
        result_order_by=(
            OutputOrder(alias="month"),
            OutputOrder(alias="revenue_rank"),
            OutputOrder(alias="category"),
        ),
        limit=100,
    )


def _build_simple_products_request() -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.ROWS,
        primary_entity=LogicalModelRef("Product"),
        fields=(
            AdvancedField(
                field=LogicalFieldRef("Product.product_key"),
                alias="product_key",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.category"),
                alias="category",
            ),
            AdvancedField(
                field=LogicalFieldRef("Product.unit_price"),
                alias="unit_price",
            ),
        ),
        where=LogicalBooleanPredicate.leaf(
            Filter(
                field=LogicalFieldRef("Product.is_active"),
                operator=FilterOperator.EQUALS,
                value=True,
            )
        ),
        result_order_by=(
            OutputOrder(alias="category"),
            OutputOrder(alias="product_key"),
        ),
        limit=50,
    )


def _build_customer_count_by_country_request() -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("Customer"),
        fields=(AdvancedField(field=LogicalFieldRef("Customer.country_code")),),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.COUNT,
                field=LogicalFieldRef("Customer.customer_key"),
                alias="customer_count",
            ),
        ),
        group_by=("country_code",),
        limit=100,
    )


def _build_account_balance_sum_request() -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("Account"),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("Account.current_balance"),
                alias="total_balance",
            ),
        ),
        limit=100,
    )


def _build_short_revenue_ranking_request() -> AdvancedAnalyticalRequest:
    return AdvancedAnalyticalRequest(
        version=2,
        mode=AdvancedQueryMode.AGGREGATE,
        primary_entity=LogicalModelRef("SalesOrder"),
        fields=(AdvancedField(field=LogicalFieldRef("SalesOrder.region")),),
        metrics=(
            AdvancedMetric(
                operation=AdvancedMetricOperation.SUM,
                field=LogicalFieldRef("SalesOrder.order_total"),
                alias="gross_revenue",
            ),
        ),
        group_by=("region",),
        windows=(
            WindowCalculation(
                operation=WindowOperation.RANK,
                alias="revenue_rank",
                order_by=(
                    OutputOrder(
                        alias="gross_revenue",
                        direction=SortDirection.DESC,
                    ),
                ),
            ),
        ),
        result_order_by=(
            OutputOrder(alias="revenue_rank"),
            OutputOrder(alias="region"),
        ),
        limit=100,
    )


__all__ = [
    "M32_ACCOUNT_BALANCE_SUM_QUESTION_ES",
    "M32_AMBIGUOUS_DATE_QUESTION_ES",
    "M32_CUSTOMER_COUNT_BY_COUNTRY_QUESTION_ES",
    "M32_LONG_SIMPLE_CUSTOMER_COUNT_QUESTION_ES",
    "M32_REFERENCE_QUESTION_ES",
    "M32_SHORT_REVENUE_RANKING_QUESTION_ES",
    "M32_SIMPLE_PRODUCTS_QUESTION_ES",
    "M32_UNSUPPORTED_CROSS_JOIN_QUESTION_ES",
    "AdvancedLanguageCase",
    "AdvancedLanguageMentionFixture",
    "DeterministicAdvancedLanguageAdapter",
    "default_advanced_language_cases",
]
