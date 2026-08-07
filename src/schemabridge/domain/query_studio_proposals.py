"""Pure canonicalization of aligned Query Studio model proposals.

Provider output and the deterministic fake are evidence-producing boundaries. This
module owns the small set of presentation-independent defaults that must converge
before a proposal is fingerprinted and signed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from schemabridge.domain.concepts import CanonicalType
from schemabridge.domain.query_studio import (
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    QueryFieldPurpose,
    QueryStudioInterpretationInput,
    QueryStudioModelProposal,
    QueryStudioPromptCandidate,
    SemanticMatchState,
)
from schemabridge.domain.requests import DateGrain, SortDirection

QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION = "m27-proposal-defaults-v3"

_T = TypeVar("_T", ProposedDimension, ProposedMetric, ProposedFilter)
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_WORD = re.compile(r"[a-z0-9]+")
_SORT_MARKER = re.compile(
    r"\b(?:order(?:ed)?|sort(?:ed)?|orden(?:a|ado|ados|ar)?|"
    r"clasifica(?:r|do|dos)?)\s+(?:by|por)\b"
)
_SORT_TERMINATOR = re.compile(r"[,;.]|\b(?:where|donde|limit|limite|top)\b")
_LIMIT = re.compile(
    r"\b(?:top|limit|limite|first|primeros?|primeras?)"
    r"[\s:=-]*(?:(?:de|to)\s+)?([0-9]{1,4})\b"
)

_GRAIN_BY_TOKEN = {
    "day": DateGrain.DAY,
    "days": DateGrain.DAY,
    "dia": DateGrain.DAY,
    "dias": DateGrain.DAY,
    "diaria": DateGrain.DAY,
    "diarias": DateGrain.DAY,
    "diario": DateGrain.DAY,
    "diarios": DateGrain.DAY,
    "daily": DateGrain.DAY,
    "week": DateGrain.WEEK,
    "weeks": DateGrain.WEEK,
    "semana": DateGrain.WEEK,
    "semanas": DateGrain.WEEK,
    "semanal": DateGrain.WEEK,
    "weekly": DateGrain.WEEK,
    "month": DateGrain.MONTH,
    "months": DateGrain.MONTH,
    "mes": DateGrain.MONTH,
    "meses": DateGrain.MONTH,
    "mensual": DateGrain.MONTH,
    "monthly": DateGrain.MONTH,
    "year": DateGrain.YEAR,
    "years": DateGrain.YEAR,
    "ano": DateGrain.YEAR,
    "anos": DateGrain.YEAR,
    "anual": DateGrain.YEAR,
    "yearly": DateGrain.YEAR,
}
_DIRECTION_BY_TOKEN = {
    "asc": SortDirection.ASC,
    "ascending": SortDirection.ASC,
    "ascendente": SortDirection.ASC,
    "creciente": SortDirection.ASC,
    "desc": SortDirection.DESC,
    "descending": SortDirection.DESC,
    "descendente": SortDirection.DESC,
    "decreciente": SortDirection.DESC,
}
_TOKEN_ALIASES = {
    "activa": "active",
    "activas": "active",
    "activo": "active",
    "activos": "active",
    "alta": "registration",
    "amount": "amount",
    "ano": "year",
    "anos": "year",
    "categoria": "category",
    "category": "category",
    "clave": "identifier",
    "client": "customer",
    "cliente": "customer",
    "clientes": "customer",
    "codigo": "code",
    "country": "country",
    "cuenta": "account",
    "cuentas": "account",
    "customer": "customer",
    "customers": "customer",
    "date": "temporal",
    "day": "temporal",
    "days": "temporal",
    "dia": "temporal",
    "dias": "temporal",
    "delivered": "delivery",
    "delivery": "delivery",
    "entrega": "delivery",
    "entregado": "delivery",
    "entregados": "delivery",
    "estado": "status",
    "fecha": "temporal",
    "holder": "holder",
    "hora": "temporal",
    "id": "identifier",
    "identificador": "identifier",
    "identifier": "identifier",
    "importe": "amount",
    "instant": "temporal",
    "instante": "temporal",
    "key": "identifier",
    "linea": "line",
    "mes": "month",
    "meses": "month",
    "month": "month",
    "months": "month",
    "neto": "net",
    "ordered": "order",
    "orders": "order",
    "pais": "country",
    "pedido": "order",
    "pedidos": "order",
    "producto": "product",
    "productos": "product",
    "registration": "registration",
    "registro": "registration",
    "revenue": "amount",
    "role": "role",
    "sales": "sale",
    "second": "role",
    "secondary": "role",
    "segunda": "role",
    "segundo": "role",
    "shipment": "shipment",
    "status": "status",
    "time": "temporal",
    "timestamp": "temporal",
    "tipo": "role",
    "titular": "holder",
    "titulares": "holder",
    "week": "week",
    "weeks": "week",
    "year": "year",
    "years": "year",
}
_STOPWORDS = frozenset(
    {
        "a",
        "al",
        "and",
        "as",
        "by",
        "de",
        "del",
        "el",
        "en",
        "for",
        "in",
        "la",
        "las",
        "los",
        "of",
        "on",
        "or",
        "por",
        "the",
        "to",
        "un",
        "una",
        "y",
    }
)


@dataclass(frozen=True, slots=True)
class _SourceText:
    normalized: str
    raw_tokens: tuple[str, ...]
    governed_tokens: tuple[str, ...]


def canonicalize_query_studio_proposal(
    proposal: QueryStudioModelProposal,
    value: QueryStudioInterpretationInput,
) -> QueryStudioModelProposal:
    """Return the one server-owned representation of an aligned proposal.

    Candidate selection, metric/filter operations, and filter values remain exactly
    those supplied by the caller. Only order/default choices that have an objective
    representation in the original business text are canonicalized.
    """

    if proposal.semantic_state is not SemanticMatchState.ALIGNED:
        return proposal

    source = _source_text(value.text.root)
    candidates = {
        candidate.candidate_id.root: candidate for candidate in value.vocabulary.candidates
    }
    dimensions = _source_ordered(proposal.dimensions, source, candidates)
    metrics = _source_ordered(proposal.metrics, source, candidates)
    filters = _source_ordered(proposal.filters, source, candidates)
    canonical_dimensions = tuple(
        ProposedDimension(
            candidate_id=dimension.candidate_id,
            grain=_dimension_grain(
                dimension,
                source=source,
                candidates=candidates,
            ),
        )
        for dimension in dimensions
    )
    canonical_metrics = tuple(
        ProposedMetric(
            candidate_id=metric.candidate_id,
            operation=metric.operation,
            alias=None,
        )
        for metric in metrics
    )
    primary_candidate_id = canonical_metrics[0].candidate_id
    order_by = _canonical_order(
        proposal.order_by,
        dimensions=canonical_dimensions,
        metrics=canonical_metrics,
        source=source,
        candidates=candidates,
    )
    return QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=primary_candidate_id,
        dimensions=canonical_dimensions,
        metrics=canonical_metrics,
        filters=filters,
        order_by=order_by,
        limit=_canonical_limit(source.normalized),
    )


def _source_ordered(
    values: Sequence[_T],
    source: _SourceText,
    candidates: dict[str, QueryStudioPromptCandidate],
) -> tuple[_T, ...]:
    return tuple(
        sorted(
            values,
            key=lambda item: _candidate_source_key(
                item.candidate_id.root,
                source=source,
                candidates=candidates,
            ),
        )
    )


def _candidate_source_key(
    candidate_id: str,
    *,
    source: _SourceText,
    candidates: dict[str, QueryStudioPromptCandidate],
) -> tuple[int, str]:
    candidate = candidates.get(candidate_id)
    if candidate is None:
        return (len(source.governed_tokens) + 1, candidate_id)
    return (
        _candidate_source_position(candidate, source),
        candidate.logical_field.root.casefold(),
    )


def _candidate_source_position(
    candidate: QueryStudioPromptCandidate,
    source: _SourceText,
    *,
    start: int = 0,
) -> int:
    field_name = candidate.logical_field.root.rsplit(".", 1)[-1]
    field_tokens = _governed_token_set(field_name)
    position = _first_token_position(source.governed_tokens, field_tokens, start=start)
    if position is not None:
        return position

    model_name = candidate.logical_field.root.split(".", 1)[0]
    model_tokens = _governed_token_set(model_name)
    position = _first_token_position(source.governed_tokens, model_tokens, start=start)
    if position is not None:
        return position

    definition_tokens = _governed_token_set(candidate.definition)
    position = _first_token_position(source.governed_tokens, definition_tokens, start=start)
    if position is not None:
        return position
    return len(source.governed_tokens) + 1


def _first_token_position(
    source: Sequence[str],
    candidates: set[str],
    *,
    start: int,
) -> int | None:
    if not candidates:
        return None
    return next(
        (
            index
            for index, token in enumerate(source)
            if index >= start and token and token in candidates
        ),
        None,
    )


def _dimension_grain(
    dimension: ProposedDimension,
    *,
    source: _SourceText,
    candidates: dict[str, QueryStudioPromptCandidate],
) -> DateGrain | None:
    candidate = candidates.get(dimension.candidate_id.root)
    if candidate is None or candidate.canonical_type not in {
        CanonicalType.DATE,
        CanonicalType.TIMESTAMP,
    }:
        return None
    explicit = tuple(
        (index, grain)
        for index, token in enumerate(source.raw_tokens)
        if (grain := _GRAIN_BY_TOKEN.get(token)) is not None
    )
    if not explicit:
        return DateGrain.DAY
    candidate_position = _candidate_source_position(candidate, source)
    return min(
        explicit,
        key=lambda item: (abs(item[0] - candidate_position), item[0], item[1].value),
    )[1]


def _canonical_order(
    proposed: Sequence[ProposedOrder],
    *,
    dimensions: tuple[ProposedDimension, ...],
    metrics: tuple[ProposedMetric, ...],
    source: _SourceText,
    candidates: dict[str, QueryStudioPromptCandidate],
) -> tuple[ProposedOrder, ...]:
    marker = _SORT_MARKER.search(source.normalized)
    if marker is None:
        return tuple(
            ProposedOrder(
                candidate_id=dimension.candidate_id,
                direction=SortDirection.ASC,
            )
            for dimension in dimensions
        )

    original = {item.candidate_id.root: item for item in proposed}
    eligible_ids = tuple(
        dict.fromkeys(
            (
                *(item.candidate_id.root for item in proposed),
                *(item.candidate_id.root for item in dimensions),
                *(
                    item.candidate_id.root
                    for item in metrics
                    if item.candidate_id.root in candidates
                    if QueryFieldPurpose.ORDER in candidates[item.candidate_id.root].intended_uses
                ),
            )
        )
    )
    marker_token_index = len(_raw_tokens(source.normalized[: marker.end()]))
    remainder = source.normalized[marker.end() :]
    terminator = _SORT_TERMINATOR.search(remainder)
    clause_end = (
        len(source.raw_tokens)
        if terminator is None
        else len(_raw_tokens(source.normalized[: marker.end() + terminator.start()]))
    )
    mentioned = tuple(
        sorted(
            (
                (
                    _candidate_source_position(
                        candidate,
                        source,
                        start=marker_token_index,
                    ),
                    candidate.logical_field.root.casefold(),
                    candidate_id,
                )
                for candidate_id in eligible_ids
                if (candidate := candidates.get(candidate_id)) is not None
            ),
            key=lambda item: (item[0], item[1]),
        )
    )
    selected = tuple(item for item in mentioned if item[0] < clause_end)
    ordered_ids = (
        tuple(item[2] for item in selected)
        if selected
        else tuple(
            item.candidate_id.root
            for item in (
                proposed
                if proposed
                else tuple(
                    ProposedOrder(candidate_id=dimension.candidate_id) for dimension in dimensions
                )
            )
        )
    )
    positions = {item[2]: item[0] for item in selected}
    return tuple(
        ProposedOrder(
            candidate_id=(
                candidates[candidate_id].candidate_id
                if candidate_id in candidates
                else original[candidate_id].candidate_id
            ),
            direction=_explicit_direction(
                source,
                start=positions.get(candidate_id, marker_token_index),
                clause_start=marker_token_index,
                end=clause_end,
            ),
        )
        for candidate_id in ordered_ids
    )


def _explicit_direction(
    source: _SourceText,
    *,
    start: int,
    clause_start: int,
    end: int,
) -> SortDirection:
    direction = next(
        (
            value
            for token in source.raw_tokens[start : min(start + 6, end)]
            if (value := _DIRECTION_BY_TOKEN.get(token)) is not None
        ),
        None,
    )
    if direction is not None:
        return direction
    global_direction = next(
        (
            value
            for token in source.raw_tokens[clause_start:end]
            if (value := _DIRECTION_BY_TOKEN.get(token)) is not None
        ),
        None,
    )
    return SortDirection.ASC if global_direction is None else global_direction


def _canonical_limit(normalized_text: str) -> int:
    match = _LIMIT.search(normalized_text)
    if match is None:
        return 500
    value = int(match.group(1))
    if not 1 <= value <= 1_000:
        raise ValueError("explicit Query Studio limit must be between 1 and 1,000")
    return value


def _source_text(value: str) -> _SourceText:
    normalized = _normalized_phrase(value)
    raw = _raw_tokens(normalized)
    return _SourceText(
        normalized=normalized,
        raw_tokens=raw,
        governed_tokens=tuple(_governed_token(token) for token in raw),
    )


def _governed_token_set(value: str) -> set[str]:
    return {
        governed
        for token in _raw_tokens(_normalized_phrase(value))
        if (governed := _governed_token(token))
    }


def _governed_token(value: str) -> str:
    if value in _STOPWORDS:
        return ""
    canonical = _TOKEN_ALIASES.get(value, value)
    if (
        len(canonical) > 3
        and canonical.endswith("s")
        and canonical
        not in {
            "business",
            "status",
        }
    ):
        canonical = canonical[:-1]
    return canonical


def _raw_tokens(value: str) -> tuple[str, ...]:
    separated = _CAMEL_BOUNDARY.sub(r"\1 \2", value.replace("_", " ").replace(".", " "))
    return tuple(_WORD.findall(separated.casefold()))


def _normalized_phrase(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(plain.casefold().split())


__all__ = [
    "QUERY_STUDIO_PROPOSAL_NORMALIZER_VERSION",
    "canonicalize_query_studio_proposal",
]
