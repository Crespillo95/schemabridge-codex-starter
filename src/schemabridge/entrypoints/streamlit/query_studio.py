"""Transient Streamlit surface for the dynamic governed Query Studio."""

from __future__ import annotations

import math
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal, TypeAlias, cast

import streamlit as st
from pydantic import ValidationError

from schemabridge.application.ports.query_studio import QueryStudioPortError
from schemabridge.application.query_studio import (
    QueryStudioError,
    natural_language_edit_candidates,
)
from schemabridge.bootstrap import QueryStudioRuntimeServices
from schemabridge.domain.concepts import CanonicalType
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    ConfirmedQueryStudioRequest,
    DescriptionQuery,
    GovernedFieldSearchFilters,
    GovernedFieldSearchRequest,
    GovernedSearchKey,
    GuidedGovernedCandidate,
    GuidedGovernedFieldPage,
    PhysicalDiscoveryCandidate,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioEditableCandidate,
    QueryStudioInterpretationRevision,
    QueryStudioModelProposal,
    QueryStudioOperationalState,
    QueryStudioPreview,
    QueryStudioRevisionAction,
    QueryStudioScopeSnapshot,
    SemanticMatchState,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    FilterScalar,
    MetricOperation,
    SortDirection,
)

_NATURAL_STATE_KEY = "_query_studio_natural_state"
_NATURAL_TEXT_KEY = "query-studio-natural-text"
_NATURAL_TEXT_CLEAR_KEY = "_query_studio_natural_text_clear"
_NATURAL_EDIT_PREFIX = "natural-edit-"
_NATURAL_EDIT_RESET_KEY = "_query_studio_natural_edit_reset"
_GUIDED_BROWSE_KEY = "_query_studio_guided_browse"
_GUIDED_SELECTION_KEY = "_query_studio_guided_selection"
_GUIDED_PREVIEW_KEY = "_query_studio_guided_preview"
_PHYSICAL_DISCOVERY_KEY = "_query_studio_physical_discovery"
_QUERY_STUDIO_KEYS = (
    _NATURAL_STATE_KEY,
    _NATURAL_TEXT_CLEAR_KEY,
    _GUIDED_BROWSE_KEY,
    _GUIDED_SELECTION_KEY,
    _GUIDED_PREVIEW_KEY,
    _PHYSICAL_DISCOVERY_KEY,
)
_NULL_OPERATORS = {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}


@dataclass(frozen=True, slots=True)
class _NaturalState:
    text: str
    language: UserLanguage
    preview: QueryStudioPreview


@dataclass(frozen=True, slots=True)
class _GuidedBrowseState:
    raw_query: str
    roles: tuple[LogicalFieldRole, ...]
    canonical_types: tuple[CanonicalType, ...]
    page_size: int
    page_number: int
    page: GuidedGovernedFieldPage


@dataclass(frozen=True, slots=True)
class _GuidedPreviewState:
    proposal: QueryStudioModelProposal
    preview: QueryStudioPreview


@dataclass(frozen=True, slots=True)
class _PhysicalDiscoveryState:
    raw_query: str
    page_number: int
    page: PhysicalFieldDiscoveryPage


FilterWidgetValue: TypeAlias = FilterScalar | tuple[FilterScalar, ...]


def clear_query_studio_state() -> None:
    """Drop every unconfirmed Query Studio value from this browser session."""

    for key in _QUERY_STUDIO_KEYS:
        st.session_state.pop(key, None)
    _clear_natural_edit_widgets()


def render_dynamic_query_studio(
    runtime: QueryStudioRuntimeServices,
    *,
    can_create: bool,
) -> ConfirmedQueryStudioRequest | None:
    """Render one bounded builder and return only a server-confirmed request."""

    st.markdown("### Nueva solicitud")
    st.caption(
        "Busca únicamente contexto gobernado y paginado. El tamaño del inventario es "
        "dinámico —10, 5.434 o más tablas—, mientras cada consulta conserva el límite "
        "independiente de tres tablas y dos joins."
    )
    _render_catalog_cardinality(runtime)
    st.caption(
        _query_studio_ai_mode_copy(
            runtime.ai_mode,
            model_snapshot=runtime.configuration.model_snapshot,
            endpoint_region=runtime.configuration.endpoint_region,
        )
    )
    mode = st.radio(
        "Modo de creación",
        ("Natural", "Guiado"),
        horizontal=True,
        key="query-studio-mode",
        disabled=not can_create,
    )
    if not can_create:
        st.info("Tu rol actual permite consultar workflows, pero no crear uno nuevo.")
        return None
    if mode == "Guiado":
        return _render_guided(runtime)
    return _render_natural(runtime)


def _render_catalog_cardinality(runtime: QueryStudioRuntimeServices) -> None:
    physical = runtime.catalog_cardinality
    connections, assets, fields, governed = st.columns(4)
    connections.metric(
        "Conexiones físicas",
        f"{physical.connection_count:,}",
    )
    assets.metric(
        "Assets físicos",
        f"{physical.asset_count:,}",
    )
    fields.metric(
        "Campos físicos",
        f"{physical.field_count:,}",
    )
    governed.metric(
        "Mappings gobernados",
        f"{runtime.governed_mapping_count:,}",
    )
    st.caption(
        "Cardinalidad actual observada por el servidor. El inventario físico es "
        "descubrimiento; solo los mappings gobernados pueden participar en una consulta."
    )


def _query_studio_ai_mode_copy(
    ai_mode: Literal["disabled", "fake", "live"],
    *,
    model_snapshot: str,
    endpoint_region: str,
) -> str:
    """Return inert presentation copy selected only by the composed runtime mode."""

    if ai_mode == "live":
        return (
            f"Query Studio AI · live · modelo {model_snapshot} · región {endpoint_region} · "
            "El texto de negocio normalizado y el cierre público gobernado y acotado salen "
            "del sistema; el modo Guiado sigue disponible."
        )
    if ai_mode == "fake":
        return (
            "Query Studio AI · fake · Interpretación local determinista · "
            "no se usa una API externa."
        )
    if ai_mode == "disabled":
        return (
            "Query Studio AI · disabled · Interpretación automática desactivada · "
            "no se usa una API externa."
        )
    raise ValueError("unsupported Query Studio AI mode")


def _query_studio_live_disclosure_copy() -> str:
    """Describe the exact bounded live egress without exposing configuration secrets."""

    return (
        "IA externa configurada. Al pulsar «Interpretar», saldrán del sistema el texto "
        "de negocio normalizado y un cierre público gobernado y acotado de hasta 3 "
        "modelos, 12 campos y 2 joins. El modo Guiado sigue disponible sin esta petición "
        "externa. No se envían filas, credenciales, SQL ni valores de parámetros."
    )


def _render_natural(
    runtime: QueryStudioRuntimeServices,
) -> ConfirmedQueryStudioRequest | None:
    st.markdown("#### Describe el resultado que necesitas")
    language_label = st.radio(
        "Idioma",
        ("Español", "English"),
        horizontal=True,
        key="query-studio-language",
    )
    language = UserLanguage.SPANISH if language_label == "Español" else UserLanguage.ENGLISH
    if st.session_state.pop(_NATURAL_TEXT_CLEAR_KEY, False):
        st.session_state[_NATURAL_TEXT_KEY] = ""
    text = st.text_area(
        "Petición de negocio",
        placeholder=(
            "Agrupa por fecha de registro todos los clientes que sean segundo "
            "titular de una cuenta."
        ),
        max_chars=2_000,
        key=_NATURAL_TEXT_KEY,
    )
    if runtime.ai_mode == "live":
        st.warning(_query_studio_live_disclosure_copy())
    elif runtime.ai_mode == "disabled":
        st.info(
            "La interpretación automática está desactivada. Usa el modo Guiado; "
            "no se realizará ninguna petición externa."
        )

    altered = _visible_text(text)
    if text and altered != text:
        st.warning(
            "La petición contiene caracteres de control o direccionales. "
            "Se muestran de forma visible para revisión:"
        )
        st.code(altered, language=None)

    state = st.session_state.get(_NATURAL_STATE_KEY)
    if isinstance(state, _NaturalState) and (state.text != text or state.language is not language):
        st.session_state.pop(_NATURAL_STATE_KEY, None)
        _clear_natural_edit_widgets()
        state = None
    prepare_disabled = runtime.prepare_natural is None or not text.strip()
    if st.button(
        "Interpretar con contexto gobernado",
        type="primary",
        key="prepare-natural-query-studio",
        disabled=prepare_disabled,
    ):
        assert runtime.prepare_natural is not None
        try:
            with st.spinner("Recuperando un cierre gobernado y acotado…"):
                preview = runtime.prepare_natural.execute(text, language)
            _clear_natural_edit_widgets()
            sensitive = (
                preview.operational_state is QueryStudioOperationalState.SENSITIVE_INPUT_BLOCKED
            )
            state = _NaturalState(
                text="" if sensitive else text,
                language=language,
                preview=preview,
            )
            st.session_state[_NATURAL_STATE_KEY] = state
            if sensitive:
                st.session_state[_NATURAL_TEXT_CLEAR_KEY] = True
                st.rerun()
        except (
            QueryStudioError,
            QueryStudioPortError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            st.error(
                "query_studio_request_invalid: La petición no pudo interpretarse de "
                "forma segura. Revisa el texto y vuelve a intentarlo."
            )
            state = None

    if runtime.prepare_natural is None:
        st.caption("Interpretación natural no disponible en esta configuración.")
    if not isinstance(state, _NaturalState):
        return None
    _render_preview(state.preview, submitted_text=state.text)
    if not _is_confirmable(state.preview):
        return None
    if not _render_natural_editor(runtime, state):
        return None
    if not st.button(
        "Confirmar interpretación exacta",
        type="primary",
        key="confirm-natural-query-studio",
    ):
        return None
    assert runtime.confirm_natural is not None
    assert state.preview.expansion is not None
    assert state.preview.proposal is not None
    assert state.preview.token is not None
    try:
        confirmed = runtime.confirm_natural.execute(
            QueryStudioConfirmation(
                original_text=DescriptionQuery(state.text),
                language=state.language,
                expansion=state.preview.expansion,
                proposal=state.preview.proposal,
                token=state.preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
    except (
        QueryStudioError,
        QueryStudioPortError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        st.session_state.pop(_NATURAL_STATE_KEY, None)
        st.error(
            "query_studio_stale_preview: La evidencia cambió o el preview caducó. "
            "Interpreta de nuevo antes de confirmar."
        )
        return None
    clear_query_studio_state()
    return confirmed


def _render_natural_editor(
    runtime: QueryStudioRuntimeServices,
    state: _NaturalState,
) -> bool:
    preview = state.preview
    assert preview.vocabulary is not None
    assert preview.proposal is not None
    assert preview.token is not None
    assert preview.expansion is not None
    _apply_pending_natural_edit_reset()

    controls = natural_language_edit_candidates(preview.vocabulary)
    index = {item.candidate_id.root: item for item in controls}
    options = tuple(index)
    labels = {key: index[key].logical_field.root for key in options}
    signed = preview.proposal
    assert signed.primary_candidate_id is not None

    st.markdown("**Editar interpretación tipada**")
    st.caption(
        "Los controles proceden únicamente del vocabulario gobernado firmado. "
        "Cualquier cambio retira la confirmación hasta que el servidor recompute "
        "la evidencia y emita una firma nueva con el vencimiento original."
    )
    primary_id = st.selectbox(
        "Entidad principal interpretada",
        options,
        index=options.index(signed.primary_candidate_id.root),
        format_func=labels.__getitem__,
        key=f"{_NATURAL_EDIT_PREFIX}primary",
    )

    signed_dimensions = {item.candidate_id.root: item for item in signed.dimensions}
    dimension_ids = tuple(
        st.multiselect(
            "Dimensiones interpretadas",
            options,
            default=tuple(signed_dimensions),
            format_func=labels.__getitem__,
            key=f"{_NATURAL_EDIT_PREFIX}dimensions",
        )
    )
    dimensions = tuple(
        ProposedDimension(
            candidate_id=index[candidate_id].candidate_id,
            grain=_natural_dimension_grain(
                index[candidate_id],
                signed_dimensions.get(candidate_id),
                candidate_id,
            ),
        )
        for candidate_id in dimension_ids
    )

    signed_metrics = {item.candidate_id.root: item for item in signed.metrics}
    metric_options = tuple(key for key, item in index.items() if item.metric_operations)
    metric_ids = tuple(
        st.multiselect(
            "Métricas interpretadas",
            metric_options,
            default=tuple(signed_metrics),
            format_func=labels.__getitem__,
            key=f"{_NATURAL_EDIT_PREFIX}metrics",
        )
    )
    metrics = tuple(
        ProposedMetric(
            candidate_id=index[candidate_id].candidate_id,
            operation=_natural_metric_operation(
                index[candidate_id],
                signed_metrics.get(candidate_id),
                candidate_id,
            ),
            alias=(signed_metrics[candidate_id].alias if candidate_id in signed_metrics else None),
        )
        for candidate_id in metric_ids
    )

    signed_filters = {item.candidate_id.root: item for item in signed.filters}
    filter_options = tuple(key for key, item in index.items() if item.filter_operators)
    filter_ids = tuple(
        st.multiselect(
            "Filtros interpretados",
            filter_options,
            default=tuple(signed_filters),
            format_func=labels.__getitem__,
            key=f"{_NATURAL_EDIT_PREFIX}filters",
        )
    )
    filters: list[ProposedFilter] = []
    filter_error: str | None = None
    for position, candidate_id in enumerate(filter_ids):
        candidate = index[candidate_id]
        existing = signed_filters.get(candidate_id)
        operator = _natural_filter_operator(
            candidate,
            existing,
            candidate_id,
        )
        replace_value = (
            st.checkbox(
                f"Sustituir valor oculto · {labels[candidate_id]}",
                value=False,
                key=f"{_NATURAL_EDIT_PREFIX}filter-replace-{candidate_id[-16:]}",
            )
            if existing is not None
            else True
        )
        if existing is not None:
            st.caption(f"{labels[candidate_id]} · valor actual: valor oculto")
        operator_changed = existing is not None and operator is not existing.operator
        if existing is not None and not operator_changed and not replace_value:
            filters.append(existing)
            continue
        if existing is not None and operator_changed and not replace_value:
            filter_error = (
                "query_studio_filter_value_required: Modificar un filtro exige "
                "marcar «Sustituir valor oculto» y aportar un valor nuevo explícito."
            )
            continue
        try:
            filters.append(
                ProposedFilter(
                    candidate_id=candidate.candidate_id,
                    operator=operator,
                    value=_natural_filter_value_widget(
                        candidate,
                        operator,
                        position=position,
                    ),
                )
            )
        except (TypeError, ValueError):
            filter_error = (
                "query_studio_filter_value_required: Un filtro nuevo o modificado "
                "requiere un valor tipado explícito."
            )

    signed_orders = {item.candidate_id.root: item for item in signed.order_by}
    output_ids = tuple(dict.fromkeys((*dimension_ids, *metric_ids)))
    order_ids = tuple(
        st.multiselect(
            "Orden interpretado",
            output_ids,
            default=tuple(key for key in signed_orders if key in output_ids),
            format_func=labels.__getitem__,
            key=f"{_NATURAL_EDIT_PREFIX}order",
        )
    )
    orders = tuple(
        ProposedOrder(
            candidate_id=index[candidate_id].candidate_id,
            direction=_natural_sort_direction(
                index[candidate_id],
                signed_orders.get(candidate_id),
                candidate_id,
            ),
        )
        for candidate_id in order_ids
    )
    limit = int(
        st.number_input(
            "Límite interpretado",
            min_value=1,
            max_value=1_000,
            value=signed.limit,
            step=1,
            key=f"{_NATURAL_EDIT_PREFIX}limit",
        )
    )
    if filter_error:
        st.error(filter_error)

    revised: QueryStudioModelProposal | None = None
    if filter_error is None:
        with suppress(ValidationError, TypeError, ValueError):
            revised = QueryStudioModelProposal(
                semantic_state=SemanticMatchState.ALIGNED,
                primary_candidate_id=index[primary_id].candidate_id,
                dimensions=dimensions,
                metrics=metrics,
                filters=tuple(filters),
                order_by=orders,
                limit=limit,
            )
    if revised is None:
        st.warning(
            "query_studio_edit_invalid: La edición aún no forma una solicitud tipada válida."
        )
        return False
    if revised.fingerprint == signed.fingerprint:
        st.success("La interpretación visible coincide exactamente con la firma actual.")
        return True

    st.warning(
        "Firma invalidada por la edición. Revalida los cambios antes de confirmar; "
        "no se hará una nueva petición a la IA."
    )
    if runtime.recompute_natural is None:
        st.error("query_studio_resign_unavailable: La revalidación no está disponible.")
        return False
    if not st.button(
        "Revalidar edición en el servidor",
        type="primary",
        key="resign-natural-query-studio",
    ):
        return False
    try:
        resigned = runtime.recompute_natural.execute(
            QueryStudioInterpretationRevision(
                original_text=DescriptionQuery(state.text),
                language=state.language,
                expansion=preview.expansion,
                signed_proposal=signed,
                revised_proposal=revised,
                token=preview.token,
                action=QueryStudioRevisionAction.RECOMPUTE_INTERPRETATION,
            )
        )
    except (
        QueryStudioError,
        QueryStudioPortError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        st.session_state.pop(_NATURAL_STATE_KEY, None)
        _clear_natural_edit_widgets()
        st.error(
            "query_studio_stale_preview: La evidencia, el ámbito o la firma ya no "
            "coinciden. Interpreta de nuevo."
        )
        return False
    st.session_state[_NATURAL_STATE_KEY] = _NaturalState(
        text=state.text,
        language=state.language,
        preview=resigned,
    )
    st.session_state[_NATURAL_EDIT_RESET_KEY] = True
    st.rerun()
    return False


def _natural_dimension_grain(
    candidate: QueryStudioEditableCandidate,
    existing: ProposedDimension | None,
    candidate_id: str,
) -> DateGrain | None:
    if not candidate.date_grains:
        return None
    options: tuple[DateGrain | None, ...] = (None, *candidate.date_grains)
    selected = existing.grain if existing is not None else None
    return cast(
        DateGrain | None,
        st.selectbox(
            f"Granularidad interpretada · {candidate.logical_field.root}",
            options,
            index=options.index(selected),
            format_func=lambda value: "Sin granularidad" if value is None else value.value,
            key=f"{_NATURAL_EDIT_PREFIX}grain-{candidate_id[-16:]}",
        ),
    )


def _natural_metric_operation(
    candidate: QueryStudioEditableCandidate,
    existing: ProposedMetric | None,
    candidate_id: str,
) -> MetricOperation:
    values = candidate.metric_operations
    selected = existing.operation if existing is not None else values[0]
    return MetricOperation(
        st.selectbox(
            f"Operación interpretada · {candidate.logical_field.root}",
            tuple(value.value for value in values),
            index=values.index(selected),
            key=f"{_NATURAL_EDIT_PREFIX}metric-{candidate_id[-16:]}",
        )
    )


def _natural_filter_operator(
    candidate: QueryStudioEditableCandidate,
    existing: ProposedFilter | None,
    candidate_id: str,
) -> FilterOperator:
    values = candidate.filter_operators
    selected = existing.operator if existing is not None else values[0]
    return FilterOperator(
        st.selectbox(
            f"Operador interpretado · {candidate.logical_field.root}",
            tuple(value.value for value in values),
            index=values.index(selected),
            key=f"{_NATURAL_EDIT_PREFIX}filter-operator-{candidate_id[-16:]}",
        )
    )


def _natural_sort_direction(
    candidate: QueryStudioEditableCandidate,
    existing: ProposedOrder | None,
    candidate_id: str,
) -> SortDirection:
    values = candidate.sort_directions
    selected = existing.direction if existing is not None else values[0]
    return SortDirection(
        st.selectbox(
            f"Dirección interpretada · {candidate.logical_field.root}",
            tuple(value.value for value in values),
            index=values.index(selected),
            key=f"{_NATURAL_EDIT_PREFIX}order-{candidate_id[-16:]}",
        )
    )


def _natural_filter_value_widget(
    candidate: QueryStudioEditableCandidate,
    operator: FilterOperator,
    *,
    position: int,
) -> FilterWidgetValue:
    key = f"{_NATURAL_EDIT_PREFIX}filter-value-{position}"
    label = candidate.logical_field.root
    if operator in _NULL_OPERATORS:
        st.caption(f"Nuevo valor explícito · {label} · sin valor ({operator.value})")
        return None
    if candidate.allowed_values:
        if operator is FilterOperator.IN:
            selected = tuple(
                st.multiselect(
                    f"Nuevo valor explícito · {label}",
                    candidate.allowed_values,
                    default=(),
                    key=key,
                )
            )
            if not selected:
                raise ValueError("IN requires an explicit governed value")
            return selected
        selected = st.selectbox(
            f"Nuevo valor explícito · {label}",
            candidate.allowed_values,
            index=None,
            placeholder="Selecciona un valor nuevo",
            key=key,
        )
        if selected is None:
            raise ValueError("filter replacement requires an explicit governed value")
        return cast(str, selected)
    if candidate.canonical_type is CanonicalType.BOOLEAN:
        selected = st.selectbox(
            f"Nuevo valor explícito · {label}",
            (True, False),
            index=None,
            placeholder="Selecciona true o false",
            key=key,
        )
        if selected is None:
            raise ValueError("boolean replacement requires an explicit value")
        return bool(selected)
    raw = st.text_input(
        f"Nuevo valor explícito · {label}",
        value="",
        type="password",
        autocomplete="off",
        key=key,
        help="El valor se mantiene oculto y no se vuelve a mostrar tras revalidar.",
    )
    if operator is FilterOperator.IN:
        parts = tuple(value.strip() for value in raw.split(",") if value.strip())
        if not parts:
            raise ValueError("IN requires at least one explicit value")
        return tuple(
            _parse_natural_filter_scalar(value, candidate.canonical_type) for value in parts
        )
    if not raw.strip():
        raise ValueError("filter replacement requires an explicit value")
    return _parse_natural_filter_scalar(raw.strip(), candidate.canonical_type)


def _parse_natural_filter_scalar(value: str, canonical_type: CanonicalType) -> FilterScalar:
    if canonical_type is CanonicalType.INTEGER:
        return int(value)
    if canonical_type is CanonicalType.DECIMAL:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("decimal filter must be finite")
        return parsed
    if canonical_type is CanonicalType.DATE:
        return date.fromisoformat(value)
    if canonical_type is CanonicalType.TIMESTAMP:
        return datetime.fromisoformat(value)
    return value


def _clear_natural_edit_widgets() -> None:
    for key in tuple(st.session_state):
        if str(key).startswith(_NATURAL_EDIT_PREFIX):
            st.session_state.pop(key, None)
    st.session_state.pop(_NATURAL_EDIT_RESET_KEY, None)


def _apply_pending_natural_edit_reset() -> None:
    if not st.session_state.pop(_NATURAL_EDIT_RESET_KEY, False):
        return
    for key in tuple(st.session_state):
        if str(key).startswith(_NATURAL_EDIT_PREFIX):
            st.session_state.pop(key, None)


def _render_guided(
    runtime: QueryStudioRuntimeServices,
) -> ConfirmedQueryStudioRequest | None:
    st.markdown("#### Busca y selecciona campos gobernados")
    st.caption(
        "Cada página recupera como máximo 20 opciones mediante keyset; nunca se carga "
        "una lista global del catálogo en el navegador."
    )
    raw_query = st.text_input(
        "Buscar por nombre o definición",
        max_chars=500,
        key="query-studio-guided-query",
    )
    left, right, size = st.columns((1, 1, 0.55))
    role_values = left.multiselect(
        "Rol lógico",
        tuple(role.value for role in LogicalFieldRole),
        key="query-studio-guided-roles",
    )
    type_values = right.multiselect(
        "Tipo canónico",
        tuple(value.value for value in CanonicalType),
        key="query-studio-guided-types",
    )
    page_size = size.selectbox(
        "Por página",
        (5, 10, 20),
        index=1,
        key="query-studio-guided-page-size",
    )
    roles = tuple(LogicalFieldRole(value) for value in role_values)
    canonical_types = tuple(CanonicalType(value) for value in type_values)
    browse_state = st.session_state.get(_GUIDED_BROWSE_KEY)
    if isinstance(browse_state, _GuidedBrowseState) and (
        browse_state.raw_query != raw_query
        or browse_state.roles != roles
        or browse_state.canonical_types != canonical_types
        or browse_state.page_size != page_size
    ):
        _clear_guided_results()
        browse_state = None

    search, restart = st.columns(2)
    if search.button(
        "Buscar campos gobernados",
        type="primary",
        key="search-guided-query-studio",
    ):
        _clear_guided_results()
        try:
            page = runtime.browse_guided.execute(
                _guided_search_request(
                    runtime,
                    raw_query=raw_query,
                    roles=roles,
                    canonical_types=canonical_types,
                    page_size=page_size,
                )
            )
            browse_state = _GuidedBrowseState(
                raw_query=raw_query,
                roles=roles,
                canonical_types=canonical_types,
                page_size=page_size,
                page_number=1,
                page=page,
            )
            st.session_state[_GUIDED_BROWSE_KEY] = browse_state
        except (
            QueryStudioError,
            QueryStudioPortError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            st.error(
                "query_studio_search_invalid: La búsqueda gobernada se rechazó de forma segura."
            )
            browse_state = None
    if restart.button("Limpiar selección", key="reset-guided-query-studio"):
        _clear_guided_results()
        browse_state = None

    if isinstance(browse_state, _GuidedBrowseState):
        _render_guided_page(browse_state)
        if browse_state.page.next_key is not None and st.button(
            "Página siguiente",
            key="next-guided-query-studio",
        ):
            try:
                page = runtime.browse_guided.execute(
                    _guided_search_request(
                        runtime,
                        raw_query=browse_state.raw_query,
                        roles=browse_state.roles,
                        canonical_types=browse_state.canonical_types,
                        page_size=browse_state.page_size,
                        after=browse_state.page.next_key,
                        expected_scope=browse_state.page.context.scope,
                    ),
                    context=browse_state.page.context,
                )
                browse_state = replace(
                    browse_state,
                    page_number=browse_state.page_number + 1,
                    page=page,
                )
                st.session_state[_GUIDED_BROWSE_KEY] = browse_state
                st.rerun()
            except (
                QueryStudioError,
                QueryStudioPortError,
                ValidationError,
                TypeError,
                ValueError,
            ):
                _clear_guided_results()
                st.error(
                    "query_studio_stale_page: La página ya no coincide con el "
                    "catálogo gobernado actual. Inicia una búsqueda nueva."
                )

    confirmed = (
        _render_guided_request(runtime, browse_state)
        if isinstance(browse_state, _GuidedBrowseState)
        else None
    )
    _render_physical_discovery(runtime, raw_query)
    return confirmed


def _guided_search_request(
    runtime: QueryStudioRuntimeServices,
    *,
    raw_query: str,
    roles: tuple[LogicalFieldRole, ...],
    canonical_types: tuple[CanonicalType, ...],
    page_size: int,
    after: GovernedSearchKey | None = None,
    expected_scope: QueryStudioScopeSnapshot | None = None,
) -> GovernedFieldSearchRequest:
    return GovernedFieldSearchRequest(
        scope=runtime.scope,
        query=DescriptionQuery(raw_query) if raw_query.strip() else None,
        filters=GovernedFieldSearchFilters(
            roles=roles,
            canonical_types=canonical_types,
        ),
        page_size=page_size,
        after=after,
        expected_scope=expected_scope,
    )


def _render_guided_page(state: _GuidedBrowseState) -> None:
    page = state.page
    st.markdown(f"##### Resultados gobernados · página {state.page_number}")
    st.caption(
        f"{len(page.items)} opciones visibles · {page.rows_read} filas leídas "
        "incluyendo como máximo el sentinel de continuación."
    )
    if not page.items:
        st.info("No hay campos gobernados que coincidan con estos filtros.")
        return
    option_ids = tuple(item.candidate_id.root for item in page.items)
    page_index = {item.candidate_id.root: item for item in page.items}
    picks = st.multiselect(
        "Añadir a la selección acotada",
        option_ids,
        format_func=lambda value: page_index[value].binding.logical_field.root,
        key=f"guided-page-picks-{state.page_number}",
        max_selections=12,
    )
    for item in page.items:
        _render_guided_candidate(item)
    if st.button(
        "Añadir campos seleccionados",
        key=f"add-guided-page-{state.page_number}",
        disabled=not picks,
    ):
        selected = _guided_selection()
        by_id = {item.candidate_id.root: item for item in selected}
        for candidate_id in picks:
            by_id.setdefault(candidate_id, page_index[candidate_id])
        if len(by_id) > 12:
            st.error("query_studio_selection_overflow: Selecciona como máximo 12 campos.")
        else:
            st.session_state[_GUIDED_SELECTION_KEY] = tuple(by_id.values())
            st.session_state.pop(_GUIDED_PREVIEW_KEY, None)


def _render_guided_candidate(item: GuidedGovernedCandidate) -> None:
    binding = item.binding
    label = f"{binding.logical_field.root} · {item.canonical_type.value} · {item.role.value}"
    with st.expander(label):
        st.text(_visible_text(item.field_definition))
        st.caption(f"Modelo · {_visible_text(item.model_definition)}")
        st.caption(
            "Binding físico aprobado · "
            f"{_visible_text(binding.physical_field.root)} · conexión "
            f"{binding.locator.asset.connection_id.root}"
        )
        st.caption(
            f"Frescura · {binding.evidence_status.value} · generación "
            f"{binding.catalog_generation} · versión de mapping {binding.mapping_version}"
        )
        st.caption(
            "Evidencia · "
            + ", ".join(f"{signal.code.value}:{signal.value}" for signal in binding.signals.signals)
        )
        if item.risks:
            st.warning("Riesgos · " + ", ".join(risk.value for risk in item.risks))
        else:
            st.success("Sin riesgos adicionales detectados en la evidencia actual.")


def _render_guided_request(
    runtime: QueryStudioRuntimeServices,
    browse_state: _GuidedBrowseState,
) -> ConfirmedQueryStudioRequest | None:
    selected = _guided_selection()
    if not selected:
        return None
    st.markdown("##### Solicitud tipada")
    st.caption(
        f"{len(selected)}/12 campos en la selección. Los identificadores del navegador "
        "son opacos y se vuelven a resolver en el servidor antes de confirmar."
    )
    index = {item.candidate_id.root: item for item in selected}
    options = tuple(index)
    labels = {key: index[key].binding.logical_field.root for key in options}
    for candidate_id in index:
        remove_key = f"remove-guided-{candidate_id[-16:]}"
        if st.button(f"Quitar {labels[candidate_id]}", key=remove_key):
            st.session_state[_GUIDED_SELECTION_KEY] = tuple(
                item for item in selected if item.candidate_id.root != candidate_id
            )
            st.session_state.pop(_GUIDED_PREVIEW_KEY, None)
            st.rerun()

    primary_id = st.selectbox(
        "Entidad principal",
        options,
        format_func=labels.__getitem__,
        key="guided-primary",
    )
    dimension_ids = tuple(
        st.multiselect(
            "Dimensiones / agrupaciones",
            options,
            format_func=labels.__getitem__,
            key="guided-dimensions",
        )
    )
    metric_options = tuple(key for key, candidate in index.items() if candidate.metric_operations)
    metric_ids = tuple(
        st.multiselect(
            "Métricas",
            metric_options,
            format_func=labels.__getitem__,
            key="guided-metrics",
        )
    )
    dimensions = tuple(
        ProposedDimension(
            candidate_id=index[candidate_id].candidate_id,
            grain=_dimension_grain(index[candidate_id], candidate_id),
        )
        for candidate_id in dimension_ids
    )
    metrics = tuple(
        ProposedMetric(
            candidate_id=index[candidate_id].candidate_id,
            operation=MetricOperation(
                st.selectbox(
                    f"Operación · {labels[candidate_id]}",
                    tuple(value.value for value in index[candidate_id].metric_operations),
                    key=f"guided-metric-operation-{candidate_id[-16:]}",
                )
            ),
        )
        for candidate_id in metric_ids
    )
    filter_options = tuple(key for key, candidate in index.items() if candidate.filter_operators)
    filter_ids = tuple(
        st.multiselect(
            "Filtros",
            filter_options,
            format_func=labels.__getitem__,
            key="guided-filters",
        )
    )
    filters: list[ProposedFilter] = []
    filter_error: str | None = None
    for candidate_id in filter_ids:
        candidate = index[candidate_id]
        operator = FilterOperator(
            st.selectbox(
                f"Operador · {labels[candidate_id]}",
                tuple(value.value for value in candidate.filter_operators),
                key=f"guided-filter-operator-{candidate_id[-16:]}",
            )
        )
        try:
            value = _filter_value_widget(candidate, operator)
            filters.append(
                ProposedFilter(
                    candidate_id=candidate.candidate_id,
                    operator=operator,
                    value=value,
                )
            )
        except (TypeError, ValueError):
            filter_error = (
                "query_studio_filter_invalid: Revisa el valor tipado del filtro "
                f"{labels[candidate_id]}."
            )
    selected_output = tuple(dict.fromkeys((*dimension_ids, *metric_ids)))
    order_ids = tuple(
        st.multiselect(
            "Orden",
            selected_output,
            format_func=labels.__getitem__,
            key="guided-order",
        )
    )
    orders = tuple(
        ProposedOrder(
            candidate_id=index[candidate_id].candidate_id,
            direction=SortDirection(
                st.selectbox(
                    f"Dirección · {labels[candidate_id]}",
                    tuple(value.value for value in index[candidate_id].sort_directions),
                    key=f"guided-order-direction-{candidate_id[-16:]}",
                )
            ),
        )
        for candidate_id in order_ids
    )
    limit = int(
        st.number_input(
            "Límite máximo de filas",
            min_value=1,
            max_value=1_000,
            value=100,
            step=1,
            key="guided-limit",
        )
    )
    if filter_error:
        st.error(filter_error)
    proposal: QueryStudioModelProposal | None = None
    with suppress(ValidationError, TypeError, ValueError):
        proposal = QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=index[primary_id].candidate_id,
            dimensions=dimensions,
            metrics=metrics,
            filters=tuple(filters),
            order_by=orders,
            limit=limit,
        )

    preview_state = st.session_state.get(_GUIDED_PREVIEW_KEY)
    if isinstance(preview_state, _GuidedPreviewState) and (
        proposal is None or preview_state.proposal.fingerprint != proposal.fingerprint
    ):
        st.session_state.pop(_GUIDED_PREVIEW_KEY, None)
        preview_state = None
    if st.button(
        "Preparar preview tipado",
        type="primary",
        key="prepare-guided-query-studio",
        disabled=proposal is None or filter_error is not None,
    ):
        assert proposal is not None
        try:
            preview = runtime.prepare_guided.execute(
                proposal,
                context=browse_state.page.context,
            )
            preview_state = _GuidedPreviewState(proposal=proposal, preview=preview)
            st.session_state[_GUIDED_PREVIEW_KEY] = preview_state
        except (
            QueryStudioError,
            QueryStudioPortError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            st.error(
                "query_studio_guided_invalid: La selección no forma una solicitud gobernada válida."
            )
            preview_state = None
    if proposal is None:
        st.caption("Selecciona una entidad principal y al menos una métrica compatible.")
    if not isinstance(preview_state, _GuidedPreviewState):
        return None
    _render_preview(preview_state.preview)
    if not _is_confirmable(preview_state.preview):
        return None
    if not st.button(
        "Confirmar selección guiada exacta",
        type="primary",
        key="confirm-guided-query-studio",
    ):
        return None
    preview = preview_state.preview
    assert preview.token is not None
    try:
        confirmed = runtime.confirm_guided.execute(
            QueryStudioConfirmation(
                original_text=None,
                language=None,
                expansion=None,
                proposal=preview_state.proposal,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
    except (
        QueryStudioError,
        QueryStudioPortError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        _clear_guided_results()
        st.error(
            "query_studio_stale_preview: La evidencia guiada cambió o caducó. "
            "Busca y prepara de nuevo."
        )
        return None
    clear_query_studio_state()
    return confirmed


def _dimension_grain(
    candidate: GuidedGovernedCandidate,
    candidate_id: str,
) -> DateGrain | None:
    if not candidate.date_grains:
        return None
    return DateGrain(
        st.selectbox(
            f"Granularidad · {candidate.binding.logical_field.root}",
            tuple(value.value for value in candidate.date_grains),
            key=f"guided-grain-{candidate_id[-16:]}",
        )
    )


def _filter_value_widget(
    candidate: GuidedGovernedCandidate,
    operator: FilterOperator,
) -> FilterWidgetValue:
    key = candidate.candidate_id.root[-16:]
    label = candidate.binding.logical_field.root
    if operator in _NULL_OPERATORS:
        return None
    if candidate.allowed_values:
        if operator is FilterOperator.IN:
            return tuple(
                st.multiselect(
                    f"Valores · {label}",
                    candidate.allowed_values,
                    key=f"guided-filter-value-{key}",
                )
            )
        return cast(
            str,
            st.selectbox(
                f"Valor · {label}",
                candidate.allowed_values,
                key=f"guided-filter-value-{key}",
            ),
        )
    if candidate.canonical_type is CanonicalType.BOOLEAN:
        boolean_value = bool(
            st.selectbox(
                f"Valor · {label}",
                (True, False),
                key=f"guided-filter-value-{key}",
            )
        )
        return (boolean_value,) if operator is FilterOperator.IN else boolean_value
    if candidate.canonical_type is CanonicalType.INTEGER:
        integer_value = int(
            st.number_input(
                f"Valor · {label}",
                step=1,
                key=f"guided-filter-value-{key}",
            )
        )
        return (integer_value,) if operator is FilterOperator.IN else integer_value
    if candidate.canonical_type is CanonicalType.DECIMAL:
        decimal_value = float(
            st.number_input(
                f"Valor · {label}",
                key=f"guided-filter-value-{key}",
            )
        )
        return (decimal_value,) if operator is FilterOperator.IN else decimal_value
    if candidate.canonical_type is CanonicalType.DATE:
        date_value = cast(
            date,
            st.date_input(
                f"Valor · {label}",
                key=f"guided-filter-value-{key}",
            ),
        )
        return (date_value,) if operator is FilterOperator.IN else date_value
    raw = cast(
        str,
        st.text_input(
            f"Valor · {label}",
            max_chars=500,
            help="Para IN, separa los valores con comas.",
            key=f"guided-filter-value-{key}",
        ),
    )
    if candidate.canonical_type is CanonicalType.TIMESTAMP:
        if operator is FilterOperator.IN:
            timestamps = tuple(
                datetime.fromisoformat(value.strip()) for value in raw.split(",") if value.strip()
            )
            if not timestamps:
                raise ValueError("IN requires at least one timestamp")
            return timestamps
        return datetime.fromisoformat(raw)
    if operator is FilterOperator.IN:
        values = tuple(value.strip() for value in raw.split(",") if value.strip())
        if not values:
            raise ValueError("IN requires at least one value")
        return values
    if not raw:
        raise ValueError("comparison requires one value")
    return raw


def _render_preview(preview: QueryStudioPreview, *, submitted_text: str | None = None) -> None:
    st.markdown("##### Preview no ejecutable")
    st.caption(
        "Todavía no hay SQL ni acceso a datos. La confirmación recomputará esta misma "
        "evidencia en el servidor."
    )
    sensitive = preview.operational_state is QueryStudioOperationalState.SENSITIVE_INPUT_BLOCKED
    if submitted_text is not None and not sensitive:
        st.markdown("**Petición revisada**")
        st.text(_visible_text(submitted_text))
    elif sensitive:
        st.caption("La petición bloqueada no se vuelve a mostrar.")
    if preview.operational_state is not None:
        _render_operational_state(preview.operational_state)
        if preview.shortlist is not None:
            _render_ranked_candidates(preview)
        return
    assert preview.semantic_state is not None
    if preview.semantic_state is SemanticMatchState.AMBIGUOUS:
        st.warning(
            "Resultado ambiguo: falta una decisión semántica explícita. Ajusta la "
            "descripción o usa el modo Guiado."
        )
    elif preview.semantic_state is SemanticMatchState.NO_MATCH:
        st.info("Sin match gobernado: no se ha convertido ningún campo físico en ejecutable.")
    elif preview.semantic_state is SemanticMatchState.CONFLICTING:
        st.error("Conflicto semántico: la evidencia actual no permite una interpretación segura.")
    else:
        st.success("Interpretación alineada y lista para confirmación humana explícita.")
    _render_ranked_candidates(preview)
    if preview.guided_preview is not None:
        draft = preview.guided_preview.draft
        st.markdown("**Solicitud tipada interpretada**")
        st.write(f"Entidad principal · {draft.primary_entity.root}")
        for dimension in draft.dimensions:
            grain = f" · {dimension.grain.value}" if dimension.grain else ""
            st.write(f"Dimensión · {dimension.field.root}{grain}")
        for metric in draft.metrics:
            st.write(f"Métrica · {metric.operation.value}({metric.field.root})")
        for request_filter in draft.filters:
            st.write(
                f"Filtro · {request_filter.field.root} · "
                f"{request_filter.operator.value} · valor oculto"
            )
        for order in draft.order_by:
            st.write(f"Orden · {order.field.root} · {order.direction.value}")
        st.caption(f"Límite · {draft.limit}")
    if preview.provider_usage:
        st.caption(
            "Uso de interpretación · "
            f"{len(preview.provider_usage)} etapa(s) · "
            f"{sum(item.input_tokens for item in preview.provider_usage)} tokens de entrada · "
            f"{sum(item.output_tokens for item in preview.provider_usage)} tokens de salida"
        )


def _render_ranked_candidates(preview: QueryStudioPreview) -> None:
    if preview.shortlist is None or not preview.shortlist.candidates:
        return
    st.markdown("**Evidencia gobernada recuperada**")
    interpretable_candidates = (
        {candidate.candidate_id.root: candidate for candidate in preview.vocabulary.candidates}
        if preview.vocabulary is not None
        else {}
    )
    for candidate in preview.shortlist.candidates:
        binding = candidate.binding
        with st.expander(
            f"#{candidate.rank} · {binding.logical_field.root} · "
            f"{candidate.canonical_type.value}/{candidate.role.value}"
        ):
            st.caption(
                "Cierre interpretable · "
                + (
                    "incluido"
                    if candidate.candidate_id.root in interpretable_candidates
                    else "alternativa excluida"
                )
            )
            prompt_candidate = interpretable_candidates.get(candidate.candidate_id.root)
            if prompt_candidate is not None:
                st.caption(
                    "Usos recuperados · "
                    + ", ".join(item.value for item in prompt_candidate.intended_uses)
                )
            st.text(_visible_text(candidate.field_definition))
            st.caption(f"Modelo · {_visible_text(candidate.model_definition)}")
            st.caption(
                "Binding físico aprobado · "
                f"{_visible_text(binding.physical_field.root)} · conexión "
                f"{binding.locator.asset.connection_id.root}"
            )
            st.caption(
                f"Frescura · {binding.evidence_status.value} · generación "
                f"{binding.catalog_generation} · mapping v{binding.mapping_version} · "
                f"campo lógico v{candidate.field_version}"
            )
            st.caption(
                "Evidencia · "
                + ", ".join(
                    f"{signal.code.value}:{signal.value}" for signal in binding.signals.signals
                )
            )
            if candidate.risks:
                st.warning("Riesgos · " + ", ".join(risk.value for risk in candidate.risks))
            else:
                st.success("Sin riesgos adicionales detectados en la evidencia actual.")


def _render_operational_state(state: QueryStudioOperationalState) -> None:
    copy = {
        QueryStudioOperationalState.STALE: (
            "warning",
            "Evidencia obsoleta: inicia una interpretación nueva.",
        ),
        QueryStudioOperationalState.CLOSURE_OVERFLOW: (
            "warning",
            "La solicitud supera el cierre seguro. Redúcela o divídela.",
        ),
        QueryStudioOperationalState.PROVIDER_UNAVAILABLE: (
            "error",
            "El servicio de interpretación no está disponible; no se ha confirmado nada.",
        ),
        QueryStudioOperationalState.RATE_LIMITED: (
            "warning",
            "Límite temporal de peticiones alcanzado. Inténtalo más tarde.",
        ),
        QueryStudioOperationalState.QUOTA_EXHAUSTED: (
            "error",
            "Cuota de IA agotada para este workspace.",
        ),
        QueryStudioOperationalState.SENSITIVE_INPUT_BLOCKED: (
            "error",
            "La petición se bloqueó antes de enviarla por contener material sensible o inseguro.",
        ),
    }
    level, message = copy[state]
    getattr(st, level)(message)


def _is_confirmable(preview: QueryStudioPreview) -> bool:
    return (
        preview.semantic_state is SemanticMatchState.ALIGNED
        and preview.proposal is not None
        and preview.guided_preview is not None
        and preview.token is not None
    )


def _render_physical_discovery(
    runtime: QueryStudioRuntimeServices,
    raw_query: str,
) -> None:
    if runtime.discover_physical is None:
        return
    st.divider()
    st.markdown("##### Inventario físico no gobernado")
    st.warning(
        "Carril separado: estos resultados siempre requieren revisión de mapping, "
        "no son seleccionables y nunca entran en el planner."
    )
    if st.button(
        "Buscar también en inventario físico",
        key="search-physical-query-studio",
    ):
        try:
            page = runtime.discover_physical.execute(
                PhysicalFieldDiscoveryRequest(
                    scope=runtime.scope,
                    query=DescriptionQuery(raw_query) if raw_query.strip() else None,
                    page_size=10,
                )
            )
            state = _PhysicalDiscoveryState(
                raw_query=raw_query,
                page_number=1,
                page=page,
            )
            st.session_state[_PHYSICAL_DISCOVERY_KEY] = state
        except (
            QueryStudioError,
            QueryStudioPortError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            st.error(
                "physical_discovery_unavailable: El inventario físico no pudo "
                "consultarse de forma segura."
            )
    state = st.session_state.get(_PHYSICAL_DISCOVERY_KEY)
    if not isinstance(state, _PhysicalDiscoveryState) or state.raw_query != raw_query:
        return
    st.caption(f"Página física {state.page_number} · resultados no ejecutables")
    for item in state.page.items:
        with st.expander(_physical_discovery_label(item)):
            st.caption(f"Conexión · {item.locator.asset.connection_id.root}")
            st.caption(f"Tipo nativo · {_visible_text(item.native_type or 'desconocido')}")
            st.caption(f"Frescura · generación {item.generation}")
            if item.definition:
                st.text(_visible_text(item.definition))
            st.warning("needs_mapping_review · no seleccionable")
    if state.page.next_cursor is not None and st.button(
        "Página física siguiente",
        key="next-physical-query-studio",
    ):
        assert runtime.discover_physical is not None
        try:
            page = runtime.discover_physical.execute(
                PhysicalFieldDiscoveryRequest(
                    scope=runtime.scope,
                    query=DescriptionQuery(raw_query) if raw_query.strip() else None,
                    page_size=10,
                    cursor=state.page.next_cursor,
                )
            )
            st.session_state[_PHYSICAL_DISCOVERY_KEY] = replace(
                state,
                page_number=state.page_number + 1,
                page=page,
            )
            st.rerun()
        except (
            QueryStudioError,
            QueryStudioPortError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            st.session_state.pop(_PHYSICAL_DISCOVERY_KEY, None)
            st.error("physical_discovery_stale_page: Reinicia la búsqueda del inventario físico.")


def _physical_discovery_label(item: PhysicalDiscoveryCandidate) -> str:
    """Keep equal physical names visibly distinct by their connection identity."""

    field_path = ".".join(item.locator.field_path)
    return (
        f"[{_visible_text(item.locator.asset.connection_id.root)}] "
        f"{_visible_text(item.asset_qualified_name)}.{_visible_text(field_path)} · "
        f"{item.status.value}"
    )


def _guided_selection() -> tuple[GuidedGovernedCandidate, ...]:
    value = st.session_state.get(_GUIDED_SELECTION_KEY)
    if isinstance(value, tuple) and all(
        isinstance(item, GuidedGovernedCandidate) for item in value
    ):
        return value
    return ()


def _clear_guided_results() -> None:
    for key in (
        _GUIDED_BROWSE_KEY,
        _GUIDED_SELECTION_KEY,
        _GUIDED_PREVIEW_KEY,
        _PHYSICAL_DISCOVERY_KEY,
    ):
        st.session_state.pop(key, None)


def _visible_text(value: str) -> str:
    """Make bidi/control characters visible before rendering user/catalog text."""

    pieces: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"} and character not in {"\n", "\t"}:
            pieces.append(f"<U+{ord(character):04X}>")
        else:
            pieces.append(character)
    return "".join(pieces)


__all__ = [
    "clear_query_studio_state",
    "render_dynamic_query_studio",
]
