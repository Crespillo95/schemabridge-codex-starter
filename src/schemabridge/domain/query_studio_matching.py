"""Pure deterministic scoring for already-governed Query Studio bindings."""

from __future__ import annotations

import re
import unicodedata

from schemabridge.domain.concepts import CanonicalType
from schemabridge.domain.query_studio import (
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
)
from schemabridge.domain.request_context import LogicalFieldRole

GOVERNED_DESCRIPTION_MATCHER_VERSION = "m27-deterministic-v9"


def score_governed_description(
    query: str | None,
    *,
    logical_field: str,
    model_description: str,
    field_definition: str,
    role: LogicalFieldRole,
    canonical_type: CanonicalType,
    allowed_values: tuple[str, ...] = (),
    physical_field: str | None = None,
    physical_definitions: tuple[str, ...] = (),
    native_types: tuple[str, ...] = (),
    taxonomy: tuple[str, ...] = (),
) -> SearchSignalBreakdown:
    """Score one approved binding; a zero score means insufficient evidence.

    This function ranks only candidates that a caller has already proven are
    governed. Its lexical signals are retrieval evidence and never establish
    semantic equivalence by themselves.
    """

    if query is None:
        return SearchSignalBreakdown.create(())
    query_tokens = _tokens(query)
    if not query_tokens:
        return SearchSignalBreakdown.create(())

    logical_tokens = _tokens(logical_field)
    physical_tokens = _tokens(physical_field or "")
    definition_tokens = _tokens(" ".join((model_description, field_definition)))
    taxonomy_tokens = _tokens(" ".join((*allowed_values, *physical_definitions, *taxonomy)))
    type_tokens = _tokens(" ".join((canonical_type.value, *native_types)))
    role_tokens = _tokens(role.value)
    candidate_tokens = (
        logical_tokens
        | physical_tokens
        | definition_tokens
        | taxonomy_tokens
        | type_tokens
        | role_tokens
    )
    exact_logical = _normalized_phrase(query) in {
        _normalized_phrase(logical_field),
        _normalized_phrase(logical_field.rsplit(".", 1)[-1]),
    }
    exact_physical = physical_field is not None and _normalized_phrase(query) in {
        _normalized_phrase(physical_field),
        _normalized_phrase(physical_field.rsplit(".", 1)[-1]),
    }
    overlap_count = len(query_tokens & candidate_tokens)
    generic_single_concept = len(query_tokens) == 1 and query_tokens.issubset(
        _GENERIC_AMBIGUOUS_CONCEPTS
    )
    if (
        not exact_logical
        and not exact_physical
        and overlap_count * 10 < len(query_tokens) * 7
        and not generic_single_concept
    ):
        return SearchSignalBreakdown.create(())

    signals: list[SearchSignal] = []
    if exact_logical:
        signals.append(SearchSignal(code=SearchSignalCode.EXACT_LOGICAL_FIELD, value=10_000))
    if exact_physical:
        signals.append(SearchSignal(code=SearchSignalCode.EXACT_PHYSICAL_FIELD, value=10_000))
    _append_overlap(
        signals,
        SearchSignalCode.LOGICAL_NAME_OVERLAP,
        query_tokens,
        logical_tokens,
        maximum=10_000,
    )
    _append_overlap(
        signals,
        SearchSignalCode.PHYSICAL_NAME_OVERLAP,
        query_tokens,
        physical_tokens,
        maximum=10_000,
    )
    _append_overlap(
        signals,
        SearchSignalCode.DEFINITION_OVERLAP,
        query_tokens,
        definition_tokens,
        maximum=10_000,
    )
    _append_overlap(
        signals,
        SearchSignalCode.TAXONOMY_OVERLAP,
        query_tokens,
        taxonomy_tokens,
        maximum=4_000,
    )
    _append_overlap(
        signals,
        SearchSignalCode.TYPE_MATCH,
        query_tokens,
        type_tokens,
        maximum=10_000,
    )
    _append_overlap(
        signals,
        SearchSignalCode.ROLE_MATCH,
        query_tokens,
        role_tokens,
        maximum=10_000,
    )
    return SearchSignalBreakdown.create(tuple(signals))


def governed_description_tokens(value: str) -> frozenset[str]:
    """Return the canonical multilingual terms used by the M27 matcher.

    Later governed retrieval lanes may reuse the reviewed Spanish/English
    vocabulary without copying its synonym table or treating lexical overlap
    as semantic approval.
    """

    return _tokens(value)


def _append_overlap(
    target: list[SearchSignal],
    code: SearchSignalCode,
    query: frozenset[str],
    candidate: frozenset[str],
    *,
    maximum: int,
) -> None:
    count = len(query & candidate)
    if count:
        target.append(
            SearchSignal(
                code=code,
                value=max(1, round(maximum * count / len(query))),
            )
        )


def _tokens(value: str) -> frozenset[str]:
    separated = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1 \2",
        value.replace("_", " ").replace(".", " "),
    )
    raw = _normalized_phrase(separated)
    raw_tokens = tuple(re.findall(r"[a-z0-9]+", raw))
    canonical_context = frozenset(
        _SYNONYMS.get(token, token)
        for token in raw_tokens
        if token not in _STOPWORDS and token != "registro"
    )
    result: set[str] = set()
    for token in raw_tokens:
        if token in _STOPWORDS:
            continue
        if token == "registro" and "identifier" in canonical_context:
            # Spanish "identificador del registro" uses registro in the generic
            # record sense. Keep that intentionally ambiguous while preserving
            # the registration concept in temporal phrases such as
            # "fecha de registro".
            continue
        canonical = _SYNONYMS.get(token, token)
        if not canonical or canonical in _STOPWORDS:
            continue
        if len(canonical) > 3 and canonical.endswith("s") and canonical not in _NON_PLURAL_TOKENS:
            canonical = canonical[:-1]
        result.add(canonical)
    return frozenset(result)


def _normalized_phrase(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(plain.casefold().split())


_STOPWORDS = frozenset(
    {
        "a",
        "al",
        "an",
        "and",
        "actual",
        "as",
        "asignada",
        "asignado",
        "assigned",
        "asociada",
        "asociado",
        "associated",
        "being",
        "como",
        "containing",
        "contiene",
        "current",
        "de",
        "del",
        "dentro",
        "disponible",
        "dispatch",
        "dispatched",
        "effective",
        "efectiva",
        "el",
        "en",
        "esta",
        "figura",
        "for",
        "held",
        "in",
        "is",
        "la",
        "las",
        "los",
        "o",
        "of",
        "on",
        "or",
        "por",
        "que",
        "recorded",
        "se",
        "shipped",
        "showing",
        "si",
        "the",
        "to",
        "un",
        "una",
        "vigente",
        "was",
        "when",
        "whether",
        "with",
        "within",
        "y",
    }
)

_SYNONYMS = {
    "account": "account",
    "accounts": "account",
    "activa": "active",
    "active": "active",
    "activo": "active",
    "alta": "registration",
    "amount": "amount",
    "aplicado": "applied",
    "attribute": "attribute",
    "atributo": "attribute",
    "balance": "balance",
    "boolean": "boolean",
    "booleano": "boolean",
    "bruto": "gross",
    "business": "business",
    "cadena": "string",
    "canal": "channel",
    "cantidad": "quantity",
    "catalog": "catalog",
    "catalogo": "catalog",
    "categoria": "category",
    "category": "category",
    "clave": "identifier",
    "client": "customer",
    "cliente": "customer",
    "clientes": "customer",
    "ciclo": "status",
    "code": "code",
    "codigo": "code",
    "comercial": "sales",
    "count": "count",
    "created": "created",
    "creado": "created",
    "cuenta": "account",
    "cuentas": "account",
    "customer": "customer",
    "current": "current",
    "date": "temporal",
    "day": "temporal",
    "decimal": "decimal",
    "delivered": "delivery",
    "delivery": "delivery",
    "descuento": "discount",
    "discount": "discount",
    "entera": "integer",
    "entero": "integer",
    "entrega": "delivery",
    "entregado": "delivery",
    "entro": "entered",
    "enviando": "shipped",
    "envio": "shipment",
    "envios": "shipment",
    "estado": "status",
    "estable": "stable",
    "fecha": "temporal",
    "financial": "financial",
    "financiera": "financial",
    "flag": "boolean",
    "gross": "gross",
    "holder": "holder",
    "hora": "temporal",
    "id": "identifier",
    "identifica": "identifier",
    "identificador": "identifier",
    "identifier": "identifier",
    "identifying": "identifier",
    "importe": "amount",
    "indicador": "boolean",
    "individual": "line",
    "instant": "temporal",
    "instante": "temporal",
    "int": "integer",
    "integer": "integer",
    "key": "identifier",
    "lifecycle": "status",
    "line": "line",
    "linea": "line",
    "maestro": "master",
    "measure": "measure",
    "medida": "measure",
    "metric": "measure",
    "metrica": "measure",
    "momento": "temporal",
    "negocio": "business",
    "net": "net",
    "neto": "net",
    "normalized": "normalized",
    "normalizado": "normalized",
    "number": "quantity",
    "numero": "quantity",
    "numeric": "decimal",
    "order": "order",
    "orden": "order",
    "pais": "country",
    "pedido": "order",
    "pedidos": "order",
    "placed": "registration",
    "position": "role",
    "posicion": "role",
    "precio": "price",
    "price": "price",
    "product": "product",
    "producto": "product",
    "productos": "product",
    "quantity": "quantity",
    "ref": "identifier",
    "reference": "identifier",
    "referencia": "identifier",
    "region": "region",
    "registro": "registration",
    "registered": "registration",
    "relacion": "relationship",
    "realizo": "registration",
    "role": "role",
    "sale": "sales",
    "sales": "sales",
    "saldo": "balance",
    "salio": "shipped",
    "second": "role",
    "secondary": "role",
    "segunda": "role",
    "segundo": "role",
    "shipment": "shipment",
    "shipped": "shipped",
    "stable": "stable",
    "status": "status",
    "string": "string",
    "texto": "string",
    "time": "temporal",
    "timestamp": "temporal",
    "tipo": "role",
    "titular": "holder",
    "titulares": "holder",
    "total": "total",
    "unit": "unit",
    "unitario": "unit",
    "units": "quantity",
    "unidades": "quantity",
    "varchar": "string",
    "venta": "sales",
    "vendidas": "sales",
    "vendido": "sales",
    "vida": "status",
}

_GENERIC_AMBIGUOUS_CONCEPTS = frozenset({"identifier", "status", "temporal"})
_NON_PLURAL_TOKENS = frozenset({"business", "gross", "status"})


__all__ = [
    "GOVERNED_DESCRIPTION_MATCHER_VERSION",
    "governed_description_tokens",
    "score_governed_description",
]
