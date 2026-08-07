"""Registry-wide logical retrieval for the bounded M32 interpretation closure."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from itertools import combinations

from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
    AdvancedSemanticRetrievalResult,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedMentionExtraction,
    AdvancedMentionPurpose,
    AdvancedNaturalLanguageInput,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.query_studio_matching import governed_description_tokens
from schemabridge.domain.request_context import (
    ApprovedLogicalJoin,
    ApprovedRequestField,
    ApprovedRequestModel,
    LogicalFieldRole,
)
from schemabridge.domain.semantic_registry import GovernedSemanticRegistrySnapshot

_QUALIFIED_FIELD = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*\b")
_MAX_PER_MENTION = 3

# M32 extends the reviewed M27 multilingual matcher only with analytical
# vocabulary needed to retrieve existing governed fields. These terms are
# search evidence, never mappings or semantic authority.
_TERM_EXPANSIONS: dict[str, frozenset[str]] = {
    "acumulada": frozenset({"amount", "quantity", "total"}),
    "acumulado": frozenset({"amount", "quantity", "total"}),
    "activa": frozenset({"active"}),
    "activo": frozenset({"active"}),
    "ano": frozenset({"temporal"}),
    "anual": frozenset({"temporal"}),
    "canal": frozenset({"channel"}),
    "categoria": frozenset({"category"}),
    "completada": frozenset({"completed", "status"}),
    "completado": frozenset({"completed", "status"}),
    "descuento": frozenset({"discount"}),
    "dia": frozenset({"temporal"}),
    "diaria": frozenset({"temporal"}),
    "diario": frozenset({"temporal"}),
    "distinta": frozenset({"distinct", "identifier", "key"}),
    "distinto": frozenset({"distinct", "identifier", "key"}),
    "entregada": frozenset({"delivery"}),
    "entregado": frozenset({"delivery"}),
    "estado": frozenset({"status"}),
    "facturacion": frozenset({"amount", "sales", "total"}),
    "facturada": frozenset({"amount", "sales", "total"}),
    "facturado": frozenset({"amount", "sales", "total"}),
    "facturar": frozenset({"amount", "sales", "total"}),
    "ingreso": frozenset({"amount", "sales"}),
    "mes": frozenset({"ordered", "temporal"}),
    "mensual": frozenset({"ordered", "temporal"}),
    "neta": frozenset({"net"}),
    "neto": frozenset({"net"}),
    "precio": frozenset({"price"}),
    "revenue": frozenset({"amount", "sales"}),
    "semana": frozenset({"temporal"}),
    "semanal": frozenset({"temporal"}),
    "vendida": frozenset({"sales"}),
    "vendido": frozenset({"sales"}),
    "venta": frozenset({"sale", "sales"}),
}

# Request mechanics are deliberately removed before lexical scoring. Keeping
# them would let words such as "ranking" or "porcentaje" select an unrelated
# attribute even though they describe an operation rather than a governed
# field.
_ANALYTICAL_SYNTAX_TOKENS = frozenset(
    {
        "alfabeticamente",
        "calcula",
        "calcular",
        "conserva",
        "consulta",
        "devuelve",
        "elegible",
        "filas",
        "grupo",
        "muestra",
        "mostrar",
        "obtener",
        "ordena",
        "ordenar",
        "porcentaje",
        "posicion",
        "primer",
        "primera",
        "ranking",
        "rango",
        "resultado",
        "suma",
        "sumar",
        "totaliza",
        "ventana",
    }
)


@dataclass(frozen=True, slots=True)
class RegistryWideAdvancedSemanticIndex:
    """Score all approved logical fields, then return a small verified hit set."""

    max_per_mention: int = _MAX_PER_MENTION

    def __post_init__(self) -> None:
        if not 1 <= self.max_per_mention <= _MAX_PER_MENTION:
            raise ValueError("advanced semantic retrieval per-mention limit is invalid")

    def retrieve(
        self,
        *,
        query: AdvancedNaturalLanguageInput,
        extraction: AdvancedMentionExtraction,
        registry: GovernedSemanticRegistrySnapshot,
    ) -> AdvancedSemanticRetrievalResult:
        if extraction.request_digest != query.digest:
            raise _retrieval_error(
                AdvancedQueryStudioPortErrorCode.RETRIEVAL_INVALID,
                "advanced semantic retrieval input is not grounded",
            )
        models = registry.logical_context.model_index()
        fields = registry.logical_context.field_index()
        branch = _select_model_branch(extraction, registry)
        branch_fields = tuple(
            field for field in fields.values() if field.id.root.split(".", 1)[0] in branch
        )
        selected: dict[str, LogicalFieldRef] = {}

        explicit = tuple(_QUALIFIED_FIELD.findall(query.text))
        for reference in explicit:
            matched = fields.get(reference)
            if matched is None:
                raise _retrieval_error(
                    AdvancedQueryStudioPortErrorCode.RETRIEVAL_INVALID,
                    "an explicit logical field is absent from the governed registry",
                )
            selected[matched.id.root] = matched.id

        for mention in extraction.mentions:
            if mention.purpose is AdvancedMentionPurpose.LIMIT:
                continue
            ranked = sorted(
                (
                    (
                        _score_field(
                            mention.value,
                            mention.purpose,
                            field,
                            models[field.id.root.split(".", 1)[0]],
                        ),
                        field.id.root,
                        field.id,
                    )
                    for field in branch_fields
                ),
                key=lambda item: (-item[0], item[1]),
            )
            positive = tuple(item for item in ranked if item[0] > 0)
            if not positive:
                continue
            best = positive[0][0]
            threshold = max(1, best * 2 // 3)
            best_by_model: dict[str, tuple[int, str, LogicalFieldRef]] = {}
            for candidate in positive:
                model_id = candidate[1].split(".", 1)[0]
                best_by_model.setdefault(model_id, candidate)
            retained = sorted(
                best_by_model.values(),
                key=lambda item: (-item[0], item[1]),
            )
            for added, (_score, field_id, logical_ref) in enumerate(retained, start=1):
                if _score < threshold:
                    break
                selected.setdefault(field_id, logical_ref)
                if added >= self.max_per_mention:
                    break
            if len(selected) > 12:
                raise _retrieval_error(
                    AdvancedQueryStudioPortErrorCode.RETRIEVAL_INVALID,
                    "advanced semantic retrieval exceeds the twelve-field closure",
                )

        if not selected:
            raise _retrieval_error(
                AdvancedQueryStudioPortErrorCode.RETRIEVAL_UNAVAILABLE,
                "no approved logical context matches the advanced request",
            )
        return AdvancedSemanticRetrievalResult(
            registry_fingerprint=registry.fingerprint,
            logical_fields=tuple(selected.values()),
        )


def _select_model_branch(
    extraction: AdvancedMentionExtraction,
    registry: GovernedSemanticRegistrySnapshot,
) -> frozenset[str]:
    logical = registry.logical_context
    models = logical.model_index()
    fields = logical.field_index()
    branches = {frozenset((model_id,)) for model_id in models}
    for size in (1, 2):
        for subset in combinations(logical.joins, size):
            vertices = frozenset(
                endpoint
                for join in subset
                for endpoint in (join.left_model.root, join.right_model.root)
            )
            if len(vertices) <= 3 and _connected_join_subset(subset, vertices):
                branches.add(vertices)

    def branch_score(branch: frozenset[str]) -> tuple[int, int, int]:
        branch_fields = tuple(
            field for field in fields.values() if field.id.root.split(".", 1)[0] in branch
        )
        scores = tuple(
            max(
                (
                    _score_field(
                        mention.value,
                        mention.purpose,
                        field,
                        models[field.id.root.split(".", 1)[0]],
                    )
                    for field in branch_fields
                ),
                default=0,
            )
            for mention in extraction.mentions
            if mention.purpose is not AdvancedMentionPurpose.LIMIT
        )
        return sum(score > 0 for score in scores), sum(scores), -len(branch)

    scored = tuple((branch_score(branch), branch) for branch in branches)
    best_score = max(score for score, _branch in scored)
    best = tuple(branch for score, branch in scored if score == best_score)
    if len(best) != 1:
        raise _retrieval_error(
            AdvancedQueryStudioPortErrorCode.RETRIEVAL_AMBIGUOUS,
            "multiple governed model branches match the request equally",
        )
    return best[0]


def _connected_join_subset(
    joins: tuple[ApprovedLogicalJoin, ...],
    vertices: frozenset[str],
) -> bool:
    start = next(iter(vertices))
    reached = {start}
    pending = [start]
    while pending:
        current = pending.pop()
        for join in joins:
            left = join.left_model.root
            right = join.right_model.root
            if current == left and right not in reached:
                reached.add(right)
                pending.append(right)
            elif current == right and left not in reached:
                reached.add(left)
                pending.append(left)
    return reached == set(vertices)


def _score_field(
    text: str,
    purpose: AdvancedMentionPurpose,
    field: ApprovedRequestField,
    model: ApprovedRequestModel,
) -> int:
    query_tokens = _expanded_tokens(text)
    model_tokens = _tokens(f"{model.id.root} {model.description}")
    leaf = field.id.root.split(".", 1)[1]
    field_tokens = _tokens(f"{leaf} {field.definition}")
    value_tokens = _tokens(" ".join(field.allowed_values))
    score = (
        5 * len(query_tokens & field_tokens)
        + 3 * len(query_tokens & model_tokens)
        + 6 * len(query_tokens & value_tokens)
    )
    normalized_text = _normalized(text)
    if field.id.root.casefold() in normalized_text.casefold():
        score += 100
    if leaf.casefold() in normalized_text.casefold():
        score += 20
    if score:
        score += _purpose_role_bonus(purpose, field.role)
    return score


def _purpose_role_bonus(
    purpose: AdvancedMentionPurpose,
    role: LogicalFieldRole,
) -> int:
    if purpose is AdvancedMentionPurpose.METRIC:
        return 5 if role in {LogicalFieldRole.MEASURE, LogicalFieldRole.IDENTIFIER} else 0
    if purpose in {
        AdvancedMentionPurpose.DIMENSION,
        AdvancedMentionPurpose.GROUPING,
        AdvancedMentionPurpose.ORDERING,
        AdvancedMentionPurpose.WINDOW,
    }:
        return (
            4
            if role
            in {
                LogicalFieldRole.ATTRIBUTE,
                LogicalFieldRole.TEMPORAL,
                LogicalFieldRole.MEASURE,
            }
            else 1
        )
    if purpose is AdvancedMentionPurpose.FILTER:
        return 4 if role in {LogicalFieldRole.ATTRIBUTE, LogicalFieldRole.TEMPORAL} else 1
    if purpose is AdvancedMentionPurpose.PRIMARY_ENTITY:
        return 2
    return 0


def _expanded_tokens(value: str) -> set[str]:
    result = _tokens(value)
    for token in tuple(result):
        result.update(_TERM_EXPANSIONS.get(token, ()))
    result.difference_update(_ANALYTICAL_SYNTAX_TOKENS)
    return result


def _tokens(value: str) -> set[str]:
    return set(governed_description_tokens(value))


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", plain)
    return re.sub(r"[_./-]+", " ", separated).casefold()


def _retrieval_error(
    code: AdvancedQueryStudioPortErrorCode,
    message: str,
) -> AdvancedQueryStudioPortError:
    return AdvancedQueryStudioPortError(code, message)


__all__ = ["RegistryWideAdvancedSemanticIndex"]
