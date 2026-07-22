"""Deterministic key-free natural-language parser for CI and offline demonstrations."""

from __future__ import annotations

import re

from schemabridge.application.ports.intents import (
    IntentParserError,
    IntentParserErrorCode,
)
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import (
    IntentAmbiguityKind,
    IntentModelOutput,
    IntentParseInput,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    DateGrain,
    Dimension,
    Filter,
    FilterOperator,
    Metric,
    MetricOperation,
    OrderBy,
)


class FakeIntentParser:
    """Recognize only explicit synthetic cases; unknown language fails visibly."""

    def __init__(self) -> None:
        self.inputs: list[IntentParseInput] = []

    def parse(self, parse_input: IntentParseInput) -> IntentModelOutput:
        self.inputs.append(parse_input)
        text = " ".join(parse_input.text.casefold().split())
        words = set(re.findall(r"[a-záéíóúñ]+", text))
        if _looks_like_control_text(text):
            return IntentModelOutput(
                language=parse_input.language,
                request=None,
                ambiguities=(IntentAmbiguityKind.UNRESOLVED_BUSINESS_REQUEST,),
            )
        if text in {"agrupa clientes", "group customers"}:
            return IntentModelOutput(
                language=parse_input.language,
                request=None,
                ambiguities=(IntentAmbiguityKind.COUNT_OR_LIST,),
            )
        if "titular" in text or "holder" in text:
            role = "VIP" if "vip" in text else "SECONDARY"
            ambiguities: tuple[IntentAmbiguityKind, ...] = (
                (IntentAmbiguityKind.UNKNOWN_ROLE_VALUE,)
                if role == "VIP"
                else (IntentAmbiguityKind.DISTINCT_OR_RELATIONSHIP_COUNT,)
            )
            if ("fecha" in words and "registro" not in words) or (
                "date" in words and "registration" not in words
            ):
                ambiguities = (*ambiguities, IntentAmbiguityKind.DATE_MEANING)
            return IntentModelOutput(
                language=parse_input.language,
                request=_north_star_request(role),
                ambiguities=ambiguities,
            )
        raise IntentParserError(
            IntentParserErrorCode.INVALID_MODEL_OUTPUT,
            "deterministic parser has no fixture for this business request",
        )


def _north_star_request(role: str) -> AnalyticalRequest:
    return AnalyticalRequest(
        primary_entity=LogicalModelRef("Customer"),
        dimensions=(
            Dimension(
                field=LogicalFieldRef("Customer.registration_date"),
                grain=DateGrain.DAY,
            ),
        ),
        metrics=(
            Metric(
                operation=MetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("Customer.customer_key"),
                alias="secondary_holder_customers",
            ),
        ),
        filters=(
            Filter(
                field=LogicalFieldRef("AccountHolder.holder_role"),
                operator=FilterOperator.EQUALS,
                value=role,
            ),
        ),
        order_by=(OrderBy(field=LogicalFieldRef("Customer.registration_date")),),
        limit=500,
    )


def _looks_like_control_text(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "ignore previous",
            "ignore rules",
            "ignora las reglas",
            "ignora instrucciones",
            "drop table",
            "elimina la tabla",
            "--",
            "/*",
            ";",
        )
    )
