"""Copy-first Streamlit surface for governed M32 natural-language SQL."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

import streamlit as st
from pydantic import ValidationError

from schemabridge.application.natural_sql import (
    GovernedCopyableSqlResult,
    NaturalSqlError,
    NaturalSqlPreparation,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
)
from schemabridge.application.query_execution import (
    QueryCompilationError,
    SqlPolicyViolation,
)
from schemabridge.bootstrap import NaturalSqlRuntimeServices
from schemabridge.domain.advanced_query_studio import (
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    AdvancedField,
    LogicalBooleanPredicate,
    OutputBooleanPredicate,
    WindowCalculation,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.requests import AnalyticalRequest, Filter, FilterScalar
from schemabridge.domain.resolution import SemanticResolutionError

_PREPARATION_KEY = "_m32_natural_sql_preparation"
_ARTIFACT_KEY = "_m32_natural_sql_artifact"
_TEXT_KEY = "m32-natural-sql-text"
_LANGUAGE_KEY = "m32-natural-sql-language"
_REVIEW_KEY = "m32-natural-sql-reviewed"
_SESSION_KEYS = (
    _PREPARATION_KEY,
    _ARTIFACT_KEY,
    _TEXT_KEY,
    _LANGUAGE_KEY,
    _REVIEW_KEY,
)


@dataclass(frozen=True, slots=True)
class _PreparationState:
    text: str
    language: UserLanguage
    preparation: NaturalSqlPreparation


@dataclass(frozen=True, slots=True)
class _ArtifactState:
    request_digest: str
    preview_fingerprint: str
    result: GovernedCopyableSqlResult


def clear_copyable_natural_sql_state() -> None:
    """Remove every transient M32 input, token, and copy artifact."""

    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)


def render_copyable_natural_sql(runtime: NaturalSqlRuntimeServices) -> None:
    """Render prepare/confirm/copy without exposing an execution capability."""

    st.markdown("### SQL PostgreSQL para copiar")
    st.caption(
        "Resultado principal · PostgreSQL standalone · no ejecuta consultas. "
        "La validación contra una base de datos es opcional, separada y está "
        "deshabilitada por defecto."
    )
    language_label = st.radio(
        "Idioma de la petición SQL",
        ("Español", "English"),
        horizontal=True,
        key=_LANGUAGE_KEY,
    )
    language = UserLanguage.SPANISH if language_label == "Español" else UserLanguage.ENGLISH
    text = st.text_area(
        "Petición para generar SQL",
        placeholder=(
            "Para cada mes, calcula los ingresos por categoría, ordénalos dentro "
            "del mes y devuelve las tres primeras categorías."
        ),
        max_chars=2_000,
        key=_TEXT_KEY,
    )
    if runtime.ai_mode == "live":
        st.warning(
            "IA externa configurada. Solo salen el texto de negocio y un cierre "
            "público gobernado de hasta 3 modelos, 12 campos y 2 joins. No se "
            "envían filas, SQL, parámetros ni credenciales."
        )
    else:
        st.caption("Interpretación local determinista · no se usa una API externa.")

    state = st.session_state.get(_PREPARATION_KEY)
    if isinstance(state, _PreparationState) and (
        state.text != text or state.language is not language
    ):
        _clear_prepared_output()
        state = None

    if st.button(
        "Preparar interpretación sin generar SQL",
        type="primary",
        key="m32-prepare-natural-sql",
        disabled=not text.strip(),
    ):
        _clear_prepared_output()
        try:
            with st.spinner("Buscando contexto gobernado e interpretando la petición…"):
                preparation = runtime.prepare.execute(
                    AdvancedNaturalLanguageInput(text=text, language=language)
                )
            state = _PreparationState(
                text=text,
                language=language,
                preparation=preparation,
            )
            st.session_state[_PREPARATION_KEY] = state
        except _EXPECTED_ERRORS as error:
            _render_safe_error(error, stage="preparación")
            state = None

    if not isinstance(state, _PreparationState):
        st.info("Todavía no existe SQL. Primero prepara y revisa la interpretación tipada.")
        return

    preparation = state.preparation
    if preparation.preview is None:
        st.warning("La petición necesita aclaración y no puede confirmarse. No se ha generado SQL.")
        for ambiguity in preparation.ambiguities:
            st.write(f"• `{ambiguity.value}`")
        return

    _render_typed_preview(preparation)
    reviewed = st.checkbox(
        "He revisado esta interpretación y confirmo su fingerprint exacto.",
        key=_REVIEW_KEY,
    )
    if st.button(
        "Confirmar y generar SQL standalone",
        type="primary",
        key="m32-confirm-generate-natural-sql",
        disabled=not reviewed,
    ):
        assert preparation.token is not None
        preview = preparation.preview
        try:
            confirmation = AdvancedQueryConfirmation(
                action=AdvancedQueryConfirmationAction.CONFIRM,
                request_digest=preparation.request_digest,
                preview_fingerprint=preview.fingerprint,
                routed_request_fingerprint=preview.routed_request_fingerprint,
                token=preparation.token,
            )
            with st.spinner("Compilando, validando y literalizando PostgreSQL…"):
                confirmed = runtime.confirm.execute(preparation, confirmation)
                generated = runtime.generate.execute(confirmed)
            artifact_state = _ArtifactState(
                request_digest=preparation.request_digest,
                preview_fingerprint=preview.fingerprint,
                result=generated,
            )
            st.session_state[_ARTIFACT_KEY] = artifact_state
        except _EXPECTED_ERRORS as error:
            st.session_state.pop(_ARTIFACT_KEY, None)
            _render_safe_error(error, stage="generación")

    artifact_state = st.session_state.get(_ARTIFACT_KEY)
    if not isinstance(artifact_state, _ArtifactState):
        return
    if (
        artifact_state.request_digest != preparation.request_digest
        or artifact_state.preview_fingerprint != preparation.preview.fingerprint
    ):
        st.session_state.pop(_ARTIFACT_KEY, None)
        return
    _render_copy_artifact(artifact_state.result)


def _render_typed_preview(preparation: NaturalSqlPreparation) -> None:
    preview = preparation.preview
    assert preview is not None
    request = preview.routed_request
    context = preparation.semantic_context

    st.markdown("#### Interpretación tipada para confirmar")
    route, models, fields, joins = st.columns(4)
    route.metric("Ruta", preview.route.value)
    models.metric("Modelos", len(context.models))
    fields.metric("Campos de contexto", len(context.fields))
    joins.metric("Joins aprobados", len(context.joins))
    st.warning(
        "No existe SQL todavía. Confirmar este preview es la única acción que "
        "habilita la compilación determinista."
    )
    st.markdown(f"**Entidad principal:** `{request.primary_entity.root}`")

    if isinstance(request, AnalyticalRequest):
        _render_v1_request(request)
    else:
        _render_v2_request(request)

    context_models, context_joins = st.columns(2)
    with context_models:
        _render_items(
            "Modelos seleccionados",
            tuple(item.id.root for item in context.models),
        )
        _render_items(
            "Datasets resueltos",
            tuple(item.root for item in preview.datasets),
        )
        with st.expander("Campos del cierre gobernado"):
            for item in context.fields:
                st.write(f"• `{item.id.root}` · {item.role.value} · {item.canonical_type.value}")
                st.caption(item.definition)
    with context_joins:
        _render_items(
            "Contratos de join aprobados",
            tuple(
                f"{item.id}: {item.left_model.root} → {item.right_model.root} "
                f"({item.cardinality.value}; {item.fanout_policy.value})"
                for item in context.joins
                if item.id in preview.join_contract_ids
            ),
            empty="No se requiere join.",
        )
        _render_items(
            "Mitigaciones de fanout",
            tuple(
                f"{item.contract_id}: {item.metric_alias} · "
                f"{item.requested_operation.value} → {item.applied_operation.value} · "
                f"{'automática' if item.automatic else 'explícita'}"
                for item in preview.fanout_mitigations
            ),
            empty="No se requiere mitigación.",
        )

    with st.expander("Supuestos, riesgos y fingerprints"):
        for assumption in preview.assumptions:
            st.write(f"• {assumption.code}: {assumption.message}")
        for mapping_review in preview.mapping_reviews:
            st.write(
                f"• Mapping `{mapping_review.logical_field.root}` → "
                f"`{mapping_review.physical_field.root}` · "
                f"confianza={mapping_review.confidence:.2f}"
            )
            for risk in mapping_review.risks:
                st.write(f"  - Riesgo: {risk}")
            for evidence in mapping_review.evidence:
                st.write(f"  - Evidencia: {evidence}")
        for join_review in preview.join_reviews:
            st.write(f"• Contrato `{join_review.contract_id}`")
            for risk in join_review.risks:
                st.write(f"  - Riesgo: {risk}")
            for evidence in join_review.evidence:
                st.write(f"  - Evidencia: {evidence}")
        st.write("• Solo participan mappings y contratos aprobados del registro gobernado actual.")
        st.write(
            "• El artefacto será PostgreSQL; no se promete portabilidad automática "
            "a otros dialectos."
        )
        st.write(
            "• Un cambio de registro, mapping, contrato o cierre invalida esta "
            "confirmación antes de generar SQL."
        )
        st.write(
            "• Las políticas de cardinalidad y fanout de los joins seleccionados "
            "siguen siendo obligatorias."
        )
        st.caption(f"Preview fingerprint: `{preview.fingerprint}`")
        st.caption(f"Typed request fingerprint: `{preview.routed_request_fingerprint}`")
        st.caption(f"Resolved plan fingerprint: `{preview.resolved_plan_fingerprint}`")
        st.caption(f"Governed registry fingerprint: `{preview.governed_registry_fingerprint}`")


def _render_v1_request(request: AnalyticalRequest) -> None:
    _render_items(
        "Campos / dimensiones",
        tuple(
            f"{item.field.root}"
            + (f" · grain={item.grain.value}" if item.grain is not None else "")
            for item in request.dimensions
        ),
    )
    _render_items(
        "Métricas",
        tuple(
            f"{item.operation.value}({item.field.root})"
            + (f" AS {item.alias}" if item.alias is not None else "")
            for item in request.metrics
        ),
    )
    _render_items(
        "WHERE tipado",
        tuple(_filter_summary(item) for item in request.filters),
        empty="Sin filtro.",
    )
    _render_items(
        "GROUP BY",
        tuple(item.field.root for item in request.dimensions),
        empty="Sin agrupación.",
    )
    _render_items(
        "Orden final",
        tuple(f"{item.field.root} {item.direction.value}" for item in request.order_by),
        empty="Sin orden explícito.",
    )
    st.write(f"**Límite:** `{request.limit}`")
    st.caption("HAVING, ventanas y filtro post-ventana: no aplican en la ruta v1.")


def _render_v2_request(request: AdvancedAnalyticalRequest) -> None:
    _render_items(
        "Campos / dimensiones",
        tuple(_advanced_field_summary(item) for item in request.fields),
        empty="Sin campos no agregados.",
    )
    _render_items(
        "Métricas",
        tuple(
            f"{item.operation.value}"
            f"({item.field.root if item.field is not None else 'rows'})"
            + (f" AS {item.alias}" if item.alias is not None else "")
            + (
                f" · condición={_logical_predicate_summary(item.condition)}"
                if item.condition is not None
                else ""
            )
            for item in request.metrics
        ),
        empty="Sin métricas.",
    )
    _render_items(
        "WHERE tipado",
        ((_logical_predicate_summary(request.where),) if request.where is not None else ()),
        empty="Sin filtro.",
    )
    _render_items("GROUP BY", request.group_by, empty="Sin agrupación.")
    _render_items(
        "HAVING tipado",
        ((_output_predicate_summary(request.having),) if request.having is not None else ()),
        empty="Sin HAVING.",
    )
    _render_items(
        "Cálculos de ventana",
        tuple(_window_summary(item) for item in request.windows),
        empty="Sin ventanas.",
    )
    _render_items(
        "Filtro post-ventana",
        (
            (_output_predicate_summary(request.post_filter),)
            if request.post_filter is not None
            else ()
        ),
        empty="Sin filtro post-ventana.",
    )
    _render_items(
        "Orden final",
        tuple(f"{item.alias} {item.direction.value}" for item in request.result_order_by),
        empty="Sin orden explícito.",
    )
    st.write(f"**Modo:** `{request.mode.value}` · **Límite:** `{request.limit}`")


def _render_copy_artifact(result: GovernedCopyableSqlResult) -> None:
    artifact = result.artifact
    st.markdown("#### PostgreSQL standalone listo para copiar")
    dialect, plan, execution = st.columns(3)
    dialect.metric("Dialecto", artifact.dialect.value)
    plan.metric("Plan", f"v{artifact.plan_version}")
    execution.metric("Ejecutado", "No")
    st.success(
        "La sentencia pasó dos validaciones AST independientes y contiene sus "
        "valores tipados sin placeholders de driver."
    )
    st.code(artifact.sql, language="sql", line_numbers=True)
    st.caption(
        "Usa el control de copia del bloque o descarga el archivo. "
        "El artefacto no se ha enviado a ningún ejecutor."
    )
    st.download_button(
        "Descargar SQL PostgreSQL standalone",
        artifact.sql,
        file_name=f"schemabridge-{artifact.sha256[:12]}.sql",
        mime="text/plain",
        key="m32-download-copyable-sql",
    )
    datasets = (
        result.resolved_plan.query_plan.root_scan.dataset.root,
        *(item.right_scan.dataset.root for item in result.resolved_plan.query_plan.joins),
    )
    _render_items("Datasets resueltos", datasets)
    _render_items(
        "Contratos resueltos",
        tuple(item.id for item in result.resolved_plan.selected_contracts),
        empty="No se requiere join.",
    )
    with st.expander("Integridad del artefacto"):
        st.caption(f"SQL SHA-256: `{artifact.sha256}`")
        st.caption(f"Request fingerprint: `{artifact.request_fingerprint}`")
        st.caption(f"Plan fingerprint: `{artifact.plan_fingerprint}`")
        st.caption("executed=false")
    st.button(
        "Validar/ejecutar por canal de solo lectura (opcional)",
        key="m32-optional-validation-disabled",
        disabled=True,
        help=(
            "La validación contra PostgreSQL es una operación separada y no "
            "recibe este artefacto standalone."
        ),
    )
    st.caption(
        "Validación/ejecución opcional deshabilitada por defecto. "
        "Solo un SQL parametrizado y guardado puede entrar en ese canal separado."
    )


def _advanced_field_summary(value: AdvancedField) -> str:
    summary = value.field.root
    if value.grain is not None:
        summary += f" · grain={value.grain.value}"
    if value.buckets:
        rendered = ", ".join(
            f"{item.label}[{item.lower!r}, {item.upper!r})" for item in value.buckets
        )
        summary += f" · buckets={rendered}"
    if value.alias is not None:
        summary += f" AS {value.alias}"
    return summary


def _logical_predicate_summary(value: LogicalBooleanPredicate) -> str:
    if value.comparison is not None:
        return _filter_summary(value.comparison)
    joined = f" {value.kind.value.upper()} ".join(
        _logical_predicate_summary(item) for item in value.operands
    )
    return f"{value.kind.value.upper()} ({joined})" if value.kind.value == "not" else f"({joined})"


def _output_predicate_summary(value: OutputBooleanPredicate) -> str:
    if value.comparison is not None:
        comparison = value.comparison
        operand = (
            f"alias:{comparison.compare_to_alias}"
            if comparison.compare_to_alias is not None
            else _format_value(comparison.value)
        )
        return f"{comparison.alias} {comparison.operator.value} {operand}".strip()
    joined = f" {value.kind.value.upper()} ".join(
        _output_predicate_summary(item) for item in value.operands
    )
    return f"{value.kind.value.upper()} ({joined})" if value.kind.value == "not" else f"({joined})"


def _filter_summary(value: Filter) -> str:
    return f"{value.field.root} {value.operator.value} {_format_value(value.value)}".strip()


def _window_summary(value: WindowCalculation) -> str:
    parts = [value.operation.value]
    if value.source is not None:
        parts.append(f"source={value.source}")
    if value.partition_by:
        parts.append("partition=" + ",".join(value.partition_by))
    if value.order_by:
        parts.append(
            "order=" + ",".join(f"{item.alias}:{item.direction.value}" for item in value.order_by)
        )
    if value.buckets is not None:
        parts.append(f"tiles={value.buckets}")
    if value.offset is not None:
        parts.append(f"offset={value.offset}")
    if value.preceding_rows is not None:
        parts.append(f"preceding_rows={value.preceding_rows}")
    return f"{value.alias}: " + " · ".join(parts)


def _format_value(value: FilterScalar | tuple[FilterScalar, ...]) -> str:
    if isinstance(value, tuple):
        return json.dumps(
            [_json_scalar(item) for item in value],
            ensure_ascii=False,
        )
    return json.dumps(_json_scalar(value), ensure_ascii=False)


def _json_scalar(value: FilterScalar) -> str | int | float | bool | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _render_items(
    label: str,
    values: tuple[str, ...],
    *,
    empty: str = "Ninguno.",
) -> None:
    st.markdown(f"**{label}**")
    if not values:
        st.caption(empty)
        return
    for value in values:
        st.write(f"• `{value}`")


def _clear_prepared_output() -> None:
    for key in (_PREPARATION_KEY, _ARTIFACT_KEY, _REVIEW_KEY):
        st.session_state.pop(key, None)


def _render_safe_error(error: Exception, *, stage: str) -> None:
    code = getattr(error, "code", "natural_sql_failed")
    code_value = code.value if isinstance(code, StrEnum) else str(code)
    st.error(
        f"{code_value}: La {stage} se rechazó de forma segura. No se ha generado ni ejecutado SQL."
    )


_EXPECTED_ERRORS = (
    AdvancedQueryStudioPortError,
    NaturalSqlError,
    QueryCompilationError,
    SemanticResolutionError,
    SqlPolicyViolation,
    ValidationError,
    TypeError,
    ValueError,
)


__all__ = [
    "clear_copyable_natural_sql_state",
    "render_copyable_natural_sql",
]
