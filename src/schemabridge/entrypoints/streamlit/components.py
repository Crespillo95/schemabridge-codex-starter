"""Reusable presentation components for the Streamlit entrypoint."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from enum import StrEnum
from html import escape

import streamlit as st

from schemabridge.application.ui_view_models import (
    JudgeUiView,
    UiHealthStatus,
    UiMode,
    UiQuery,
    UiRegistryProjectionStatus,
    UiResult,
)
from schemabridge.application.ui_workflow import UiCapabilities


class PageActionKind(StrEnum):
    CONFIRM_INTENT = "confirm_intent"
    APPROVE_EXECUTION = "approve_execution"
    PUBLISH = "publish"
    SKIP_PUBLICATION = "skip_publication"
    RETRY = "retry"
    RECOVER = "recover"


@dataclass(frozen=True, slots=True)
class PageAction:
    kind: PageActionKind
    alternative_id: str | None = None


def inject_styles() -> None:
    """Apply a compact product theme without introducing a design system."""

    st.markdown(
        """
        <style>
        :root { --sb-ink:#17233b; --sb-muted:#5f6b7a; --sb-line:#dfe5ec;
                --sb-teal:#087f75; --sb-blue:#2457c5; --sb-soft:#f5f8fb; }
        .stApp { background: linear-gradient(180deg, #fbfcfe 0%, #f7f9fc 100%); }
        [data-testid="stHeader"] { background: rgba(251,252,254,.82); }
        [data-testid="stSidebar"] { border-right: 1px solid var(--sb-line); }
        .sb-eyebrow { color:var(--sb-teal); font-weight:700; letter-spacing:.08em;
                      text-transform:uppercase; font-size:.74rem; }
        .sb-title { color:var(--sb-ink); font-size:2.35rem; line-height:1.06;
                    letter-spacing:-.035em; margin:.25rem 0 .45rem; font-weight:760;
                    overflow-wrap:anywhere; }
        .sb-subtitle { color:var(--sb-muted); max-width:760px; font-size:1.02rem; }
        .sb-card { background:#fff; border:1px solid var(--sb-line); border-radius:14px;
                   padding:1rem 1.1rem; box-shadow:0 6px 22px rgba(23,35,59,.045); }
        .sb-label { color:var(--sb-muted); font-size:.78rem; text-transform:uppercase;
                    letter-spacing:.055em; font-weight:700; }
        .sb-value { color:var(--sb-ink); font-size:1rem; font-weight:650; margin-top:.15rem; }
        .sb-mode-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
                        gap:.7rem; margin:.9rem 0 .35rem; }
        .sb-mode { background:#fff; border:1px solid var(--sb-line); border-radius:12px;
                   padding:.75rem .8rem; min-width:0; }
        .sb-mode strong { color:var(--sb-ink); overflow-wrap:anywhere; }
        .sb-mode small { color:var(--sb-muted); display:block; margin-top:.35rem;
                         line-height:1.35; }
        div[data-testid="stRadio"] > div { gap:.35rem; flex-wrap:wrap; }
        div[data-testid="stRadio"] label { background:#fff; border:1px solid var(--sb-line);
            border-radius:999px; padding:.25rem .7rem; }
        .stButton > button[kind="primary"] { background:var(--sb-blue); border-color:var(--sb-blue); }
        h1, h2, h3 { color:var(--sb-ink); letter-spacing:-.02em; }
        code { font-size:.82rem !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header(view: JudgeUiView) -> None:
    left, right = st.columns((4, 1.25), vertical_alignment="center")
    with left:
        st.markdown(
            '<div class="sb-eyebrow">Governed semantic workflow</div>', unsafe_allow_html=True
        )
        st.markdown(
            '<div class="sb-title">From fragmented schemas to trusted answers.</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="sb-subtitle">Inspect semantic evidence, confirm meaning, review the '
            "approved join path, and run one independently guarded read-only preview.</div>",
            unsafe_allow_html=True,
        )
    with right:
        label = (
            f"Workflow {view.workflow_id} · r{view.revision}"
            if view.workflow_id
            else "No scenario loaded"
        )
        st.markdown(
            f'<div class="sb-card"><div class="sb-label">Current state</div>'
            f'<div class="sb-value">{label}</div></div>',
            unsafe_allow_html=True,
        )


def render_modes(modes: tuple[UiMode, ...]) -> None:
    cards = []
    for mode in modes:
        icon = "●" if mode.kind == "live" else "◐" if mode.kind == "recorded" else "◇"
        cards.append(
            '<div class="sb-mode">'
            f'<div class="sb-label">{escape(mode.name)}</div>'
            f"<strong>{icon} {escape(mode.label)}</strong>"
            f"<small>{escape(mode.detail)}</small>"
            "</div>"
        )
    st.markdown(
        '<div class="sb-mode-grid">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


def render_failure(
    view: JudgeUiView,
    *,
    can_retry: bool = True,
    can_publish: bool = False,
) -> PageAction | None:
    session_error = st.session_state.get("ui_error")
    if isinstance(session_error, dict):
        code = str(session_error.get("code", "ui_action_failed"))
        message = str(session_error.get("message", "The action failed safely."))
        corrective = str(session_error.get("corrective_action", "Review the current state."))
        st.error(f"{code}: {message}")
        st.caption(f"Next action: {corrective}")
    if view.recovery_operation is not None:
        st.warning(
            "This durable workflow contains an interrupted transition. "
            "Opening it is read-only; recovery requires an explicit authorized action."
        )
        can_recover = can_publish if view.recovery_operation == "context_publication" else can_retry
        if can_recover and st.button("Recover interrupted state", key="recover-action"):
            return PageAction(PageActionKind.RECOVER)
        if not can_recover:
            st.caption("Your current roles do not allow this recovery.")
    if view.error_code is None:
        return None
    st.error(f"{view.error_code}: {view.error_message}")
    st.caption(f"Next action: {view.corrective_action}")
    if view.can_retry and can_retry and st.button("Retry failed step", key="retry-action"):
        return PageAction(PageActionKind.RETRY)
    if view.can_retry and not can_retry:
        st.caption("Your current roles do not allow this retry.")
    return None


def render_overview(view: JudgeUiView) -> None:
    st.subheader("Governance overview")
    st.caption(
        "A deterministic multi-domain synthetic corpus, explicit integration labels, "
        "and no hidden production claims."
    )
    health_columns = st.columns(len(view.health))
    for column, health in zip(health_columns, view.health, strict=True):
        with column:
            icon = {
                UiHealthStatus.READY: "✅",
                UiHealthStatus.DEGRADED: "⚠️",
                UiHealthStatus.BLOCKED: "⛔",
                UiHealthStatus.NOT_CHECKED: "○",
            }[health.status]
            st.markdown(f"### {icon} {health.name}")
            st.write(health.status.value.replace("_", " ").title())
            st.caption(health.detail)

    approved = sum(item.status == "approved" for item in view.reference.mappings)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Approved mappings", approved)
    c2.metric("Governed joins", len(view.reference.relationships))
    c3.metric("Logical models", view.reference.model_count)
    c4.metric("Registry version", view.reference.context_version)
    st.caption(
        f"Registry {view.reference.registry_id} · scope {view.reference.catalog_scope} · "
        f"adapter {_registry_adapter_label(view.reference.context_source)} · "
        f"source {view.reference.context_source} · "
        f"fingerprint {view.reference.registry_fingerprint}"
    )
    _render_registry_control_status(view)

    st.markdown("#### What the demo proves")
    p1, p2, p3 = st.columns(3)
    p1.info("Multiple physical identifier shapes resolve through explicit approved mappings.")
    p2.info("One-to-many fanout is visible and mitigated with COUNT DISTINCT.")
    p3.info("Final SQL is deterministic, parameterized, reparsed, and read-only.")
    if view.workflow_id is None:
        st.info("Use **Load demo scenario** in the sidebar to begin the three-step judge path.")


def _render_registry_control_status(view: JudgeUiView) -> None:
    reference = view.reference
    if reference.activation_generation is None:
        st.caption("Registry selection · fixed version (no managed activation pointer)")
        return
    assert reference.active_pointer_fingerprint is not None
    status = reference.projection_status
    message = (
        f"Authoritative registry · generation {reference.activation_generation} · "
        f"version {reference.context_version} · projection {status.value}"
    )
    if status is UiRegistryProjectionStatus.DELIVERED:
        st.success(message)
    elif status is UiRegistryProjectionStatus.PENDING:
        st.warning(message)
    elif status is UiRegistryProjectionStatus.BLOCKED:
        st.error(message)
    else:
        st.info(message)
    st.caption(f"Active pointer fingerprint {reference.active_pointer_fingerprint}")


def render_semantic_models(view: JudgeUiView) -> None:
    reference = view.reference
    st.subheader("Semantic Models")
    st.caption(
        f"Registry {reference.registry_id} v{reference.context_version} · "
        f"{reference.model_count} models · {len(reference.mappings)} mappings · "
        f"{len(reference.relationships)} joins · {reference.catalog_scope} · "
        f"adapter {_registry_adapter_label(reference.context_source)} · "
        f"source {reference.context_source} · "
        f"fingerprint {reference.registry_fingerprint}"
    )
    selected_model_id = st.selectbox(
        "Inspect logical model",
        tuple(model.id for model in reference.logical_models),
        key="semantic-model",
    )
    selected_model = next(
        model for model in reference.logical_models if model.id == selected_model_id
    )
    st.markdown(f"**{selected_model.id} · v{selected_model.version} · {selected_model.status}**")
    st.caption(selected_model.description)
    for field in selected_model.fields:
        values = f" · values: {', '.join(field.allowed_values)}" if field.allowed_values else ""
        st.write(
            f"**{field.id}** · {field.canonical_type} · {field.role} · v{field.version}{values}"
        )
        st.caption(field.definition)

    physical, canonical, evidence = st.columns((1.25, 1, 1.35))
    with physical:
        st.markdown("#### Physical candidates")
        for candidate in reference.candidates[:5]:
            with st.expander(
                f"{candidate.physical_field} · {candidate.native_type}",
                expanded=candidate.confidence >= 0.6,
            ):
                st.metric("Confidence", f"{candidate.confidence:.1%}")
                st.caption(f"Recommendation: {candidate.recommendation}")
                st.write(" → ".join(candidate.transformations))
                if candidate.risks:
                    st.warning("\n".join(f"• {risk}" for risk in candidate.risks))
    with canonical:
        st.markdown("#### Approved canonical fields")
        logical_fields = sorted({item.logical_field for item in reference.mappings})
        for logical_field in logical_fields:
            related = [item for item in reference.mappings if item.logical_field == logical_field]
            st.markdown(f"**{logical_field}**")
            st.caption(f"{len(related)} approved physical representation(s)")
            for mapping in related:
                st.write(f"✓ {mapping.physical_field} · v{mapping.version}")
    with evidence:
        st.markdown("#### Evidence and controls")
        selected_name = st.selectbox(
            "Inspect candidate",
            tuple(item.physical_field for item in reference.candidates),
            key="semantic-candidate",
        )
        selected = next(
            item for item in reference.candidates if item.physical_field == selected_name
        )
        st.dataframe(
            [
                {
                    "signal": signal.name,
                    "score": signal.score if signal.available else None,
                    "weight": signal.weight,
                    "detail": signal.detail,
                }
                for signal in selected.signals
            ],
            hide_index=True,
            width="stretch",
        )
        st.markdown("**Evidence**")
        for evidence_item in selected.evidence:
            st.write(f"• {evidence_item}")
        if selected.missing_evidence:
            st.markdown("**Missing evidence**")
            for missing_item in selected.missing_evidence:
                st.write(f"• {missing_item}")
        st.info("Confidence prioritizes review; it never grants approval.")

    st.markdown("#### Approved mapping registry")
    st.dataframe(
        [
            {
                "logical field": mapping.logical_field,
                "physical field": mapping.physical_field,
                "type": mapping.physical_type,
                "transformations": " → ".join(mapping.transformations),
                "status": mapping.status,
                "version": mapping.version,
                "decision": mapping.decision_id,
                "evidence": " · ".join(mapping.evidence),
                "risks": " · ".join(mapping.risks),
            }
            for mapping in reference.mappings
        ],
        hide_index=True,
        width="stretch",
    )


def render_relationships(view: JudgeUiView) -> None:
    st.subheader("Relationships")
    st.caption("Only versioned approved contracts can enter a query plan.")
    st.caption(
        f"{len(view.reference.relationships)} contracts from registry "
        f"{view.reference.registry_id} · fingerprint "
        f"{view.reference.registry_fingerprint[:12]}…"
    )
    st.dataframe(
        [
            {
                "contract": item.id,
                "left": item.left_logical_field,
                "right": item.right_logical_field,
                "cardinality": item.cardinality,
                "fanout policy": item.fanout_policy,
                "status": item.status,
                "version": item.version,
            }
            for item in view.reference.relationships
        ],
        hide_index=True,
        width="stretch",
    )
    for item in view.reference.relationships:
        with st.expander(f"{item.id} · {item.cardinality} · v{item.version}", expanded=True):
            left, right = st.columns(2)
            with left:
                st.markdown(f"**Left:** {item.left_logical_field}")
                st.code(item.left_physical_field)
                st.caption(" → ".join(item.left_transformations))
            with right:
                st.markdown(f"**Right:** {item.right_logical_field}")
                st.code(item.right_physical_field)
                st.caption(" → ".join(item.right_transformations))
            st.markdown(f"**Fanout policy:** `{item.fanout_policy}`")
            for evidence in item.evidence:
                st.success(evidence)
            for risk in item.risks:
                st.warning(risk)


def render_query_studio(
    view: JudgeUiView,
    capabilities: UiCapabilities,
) -> PageAction | None:
    st.subheader("Query Studio")
    query = view.query
    if query is None:
        st.info(
            "Crea una solicitud Natural o Guiada arriba, o carga el demo "
            "determinista desde la barra lateral."
        )
        return None

    status_1, status_2, status_3, status_4 = st.columns(4)
    status_1.metric("Stage", query.stage.replace("_", " ").title())
    status_2.metric("Assets", len(query.selected_assets))
    status_3.metric("Join contracts", len(query.join_path))
    status_4.metric("Validation", "Accepted" if query.policy_checks else "Pending")

    st.markdown("#### 1 · Interpretation")
    st.write(query.business_text)
    if query.interpretation_adapter:
        st.caption(f"Adapter: {query.interpretation_adapter}")
    if query.ambiguities:
        st.warning("Explicit ambiguity: " + ", ".join(query.ambiguities))
    for finding in query.findings:
        st.info(finding)
    action: PageAction | None = None
    if query.alternatives and query.can_confirm and capabilities.can_confirm:
        labels = {item.id: item.label for item in query.alternatives if item.available}
        selected = st.selectbox(
            "Choose the intended meaning",
            tuple(labels),
            format_func=lambda value: labels[value],
            key="intent-alternative",
        )
        chosen = next(item for item in query.alternatives if item.id == selected)
        st.caption(chosen.rationale)
        if st.button(
            "Confirm interpretation",
            type="primary",
            key="confirm-intent",
        ):
            action = PageAction(PageActionKind.CONFIRM_INTENT, selected)
    elif query.alternatives and query.can_confirm:
        st.caption("Your current roles do not allow interpretation confirmation.")
    elif query.checkpoint != "intent_confirmation":
        st.success("Interpretation confirmed and bound to the durable workflow.")

    st.markdown("#### 2 · Governed plan")
    if query.selected_assets:
        assets, joins = st.columns(2)
        with assets:
            st.markdown("**Selected assets**")
            for asset_name in query.selected_assets:
                st.write(f"✓ {asset_name}")
            st.markdown("**Mapping versions**")
            for mapping_label in query.mapping_versions:
                st.caption(mapping_label)
        with joins:
            st.markdown("**Approved join path**")
            for join_label in query.join_path or ("No join required",):
                st.write(join_label)
            st.markdown("**Fanout mitigation**")
            st.caption(query.fanout_summary)
            for mitigation in query.fanout_mitigations:
                st.warning(mitigation)
        with st.expander("Assumptions and plan fingerprint inputs"):
            for assumption in query.assumptions:
                st.write(f"• {assumption}")
            if query.validated_request_fingerprint:
                st.caption(
                    f"Validated request fingerprint: `{query.validated_request_fingerprint}`"
                )
            if query.resolved_plan_fingerprint:
                st.caption(f"Resolved plan fingerprint: `{query.resolved_plan_fingerprint}`")
            if query.semantic_gate is not None:
                st.success(
                    "M26 semantic context gate: "
                    f"{query.semantic_gate.status} · "
                    f"baseline revision {query.semantic_gate.baseline_revision}"
                )
                st.caption(
                    "Checked dependency fingerprint: "
                    f"`{query.semantic_gate.dependencies_fingerprint}`"
                )
            if query.plan_json and capabilities.can_export:
                st.download_button(
                    "Download sanitized plan inspection JSON",
                    query.plan_json,
                    file_name="schemabridge-plan-inspection.json",
                    mime="application/json",
                    key="download-plan",
                )
    else:
        st.caption("The physical plan appears only after the interpretation is confirmed.")

    st.markdown("#### 3 · Governed connector and cost preflight")
    _render_connector_cost_preflight(query)

    st.markdown("#### 4 · SQL safety")
    if query.sql:
        checks, sql = st.columns((1, 1.55))
        with checks:
            for policy_check in query.policy_checks:
                st.success(f"{policy_check.name}: {policy_check.detail}")
        with sql:
            st.code(query.sql, language="sql", line_numbers=True)
            parameter_types = ", ".join(query.parameter_types) or "none"
            st.caption(
                f"Bound parameters: {len(query.parameters)} "
                f"(values hidden; types: {parameter_types})"
            )
            if capabilities.can_export:
                st.download_button(
                    "Export validated SQL",
                    query.sql,
                    file_name="schemabridge-preview.sql",
                    mime="text/plain",
                    key="download-sql",
                )
    else:
        st.caption("No SQL is generated until an approved semantic plan exists.")

    if query.execution_blockers and query.result is None:
        for blocker in query.execution_blockers:
            st.caption(f"🔒 {blocker}")
    if query.can_execute:
        if capabilities.can_execute and st.button(
            "Approve & run bounded preview",
            type="primary",
            key="approve-execution",
        ):
            action = PageAction(PageActionKind.APPROVE_EXECUTION)
        elif not capabilities.can_execute:
            st.caption("Your current roles do not allow preview execution.")

    if query.result is not None:
        _render_result(
            query.result,
            query.selected_assets,
            query.join_path,
            query.fanout_summary,
            query.fanout_mitigations,
            can_export=capabilities.can_export,
        )
    if query.can_publish:
        st.markdown("#### 6 · Reusable context")
        st.info(
            "Publication is separate from execution and binds this exact validated result. "
            "The selected adapter is labeled above."
        )
        publish, skip = st.columns(2)
        if capabilities.can_publish:
            if publish.button("Approve context publication", key="publish-context"):
                action = PageAction(PageActionKind.PUBLISH)
        else:
            publish.caption("Publisher role required.")
        if capabilities.can_skip:
            if skip.button("Skip publication", key="skip-publication"):
                action = PageAction(PageActionKind.SKIP_PUBLICATION)
        else:
            skip.caption("Analyst role required to skip.")
    return action


def _render_connector_cost_preflight(query: UiQuery) -> None:
    """Render the public target and durable sanitized EXPLAIN trace facts."""

    target = query.execution_target
    if target is None:
        st.caption(
            "This explicit local/recorded path has no managed connector target or "
            "query-cost preflight."
        )
        return

    assessment = query.cost_assessment
    connection, dialect, route, decision = st.columns(4)
    connection.metric("Connection", target.connection_id)
    dialect.metric("Dialect", target.dialect)
    route.metric("Route revision", f"r{target.route_revision}")
    decision.metric(
        "Cost decision",
        assessment.decision.title() if assessment is not None else "Unavailable",
    )
    st.caption(
        f"Connector {target.connector_kind} · route {target.route_fingerprint} · "
        f"target {target.target_fingerprint} · type contract "
        f"{target.type_contract_fingerprint}"
    )

    budget = query.cost_budget
    if budget is not None:
        st.caption(
            "Budget "
            f"{budget.fingerprint} · timeout {budget.explain_timeout_ms} ms · "
            f"response {budget.max_response_bytes:,} B · total cost "
            f"{budget.max_total_cost} · rows {budget.max_estimated_rows:,} · "
            f"nodes {budget.max_plan_nodes:,} · depth {budget.max_plan_depth} · "
            f"width {budget.max_plan_width:,}"
        )

    if query.preflight_error_code is not None:
        st.error(f"Connector/cost preflight blocked: {query.preflight_error_code}.")
    if assessment is None:
        st.warning("No persisted sanitized cost assessment is available.")
        return

    reasons = ", ".join(assessment.rejection_codes)
    if assessment.decision == "accepted":
        st.success(
            f"Persisted bounded EXPLAIN accepted · ANALYZE disabled · "
            f"assessment {assessment.fingerprint}."
        )
    else:
        st.error(f"Cost preflight rejected: {reasons or 'unspecified_rejection'}.")
    st.caption(
        f"Persisted total cost {assessment.total_cost or 'unavailable'} · "
        f"estimated rows {_display_optional_count(assessment.estimated_root_rows)} · "
        f"nodes {_display_optional_count(assessment.plan_node_count)} · "
        f"configured preflight timeout {assessment.explain_timeout_ms} ms"
    )


def _display_optional_count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unavailable"


def render_decisions(view: JudgeUiView) -> None:
    st.subheader("Decisions")
    st.caption("Immutable mapping, relationship, workflow, and publication decisions.")
    if not view.decisions:
        st.info("No decisions are available.")
        return
    st.dataframe(
        [
            {
                "kind": item.kind,
                "subject": item.subject,
                "action": item.action,
                "actor": item.actor,
                "version": item.version,
                "status": item.status,
                "detail": item.detail,
            }
            for item in view.decisions
        ],
        hide_index=True,
        width="stretch",
    )
    st.markdown("#### Observable workflow trace")
    if not view.trace:
        st.caption("Load a scenario to record bounded action summaries.")
        return
    st.dataframe(
        [
            {
                "#": item.sequence,
                "stage": item.stage,
                "operation": item.operation,
                "status": item.status,
                "duration_ms": item.duration_ms,
                "summary": item.summary,
            }
            for item in view.trace
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("Trace summaries contain no prompts, credentials, or private reasoning.")


def _render_result(
    result: UiResult,
    assets: tuple[str, ...],
    join_path: tuple[str, ...],
    fanout_summary: str,
    fanout: tuple[str, ...],
    *,
    can_export: bool,
) -> None:
    st.markdown("#### 5 · Validated result")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", result.row_count)
    c2.metric("Rejected", len(result.rejections))
    c3.metric("Reader", result.database_user)
    c4.metric("Timeout", f"{result.statement_timeout_ms} ms")
    st.success(
        f"Validation accepted · read_only={result.transaction_read_only} · "
        f"truncated={result.truncated}"
    )
    summary, table = st.columns((1, 1.65))
    with summary:
        st.markdown("**Selected assets**")
        for item in assets:
            st.write(f"• {item}")
        st.markdown("**Join path**")
        for item in join_path or ("No join",):
            st.write(f"• {item}")
        st.markdown("**Fanout mitigation**")
        st.write(f"• {fanout_summary}")
        for item in fanout:
            st.caption(f"Recorded mitigation: {item}")
    with table:
        if result.rows:
            st.dataframe(
                [dict(zip(result.columns, row, strict=True)) for row in result.rows],
                hide_index=True,
                width="stretch",
            )
        elif result.row_count:
            st.info(
                "Validated row payload is intentionally transient and is not retained "
                "in durable workflow state."
            )

    st.markdown("**Rejected source records**")
    if result.rejections:
        rejection_rows = [
            {"record": item.record, "code": item.code, "reason": item.reason}
            for item in result.rejections
        ]
        st.dataframe(rejection_rows, hide_index=True, width="stretch")
        if can_export:
            st.download_button(
                "Download rejection report CSV",
                _csv(rejection_rows),
                file_name="schemabridge-rejections.csv",
                mime="text/csv",
                key="download-rejections",
            )
    else:
        st.info("No source records were rejected.")
    st.markdown("**Limitations**")
    for item in result.limitations:
        st.caption(f"• {item}")
    if can_export:
        st.download_button(
            "Download validation report JSON",
            _validation_report(result, assets, join_path, fanout),
            file_name="schemabridge-validation-report.json",
            mime="application/json",
            key="download-validation-report",
        )


def _registry_adapter_label(source: str) -> str:
    return "live:datahub" if source.startswith("datahub:") else "recorded:bundle"


def _csv(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _validation_report(
    result: UiResult,
    assets: tuple[str, ...],
    join_path: tuple[str, ...],
    fanout: tuple[str, ...],
) -> str:
    return json.dumps(
        {
            "validation_status": "accepted",
            "selected_assets": assets,
            "join_path": join_path,
            "fanout_mitigation": fanout,
            "execution": {
                "database_user": result.database_user,
                "transaction_read_only": result.transaction_read_only,
                "statement_timeout_ms": result.statement_timeout_ms,
                "truncated": result.truncated,
                "row_count": result.row_count,
            },
            "rejections": [
                {"record": item.record, "code": item.code, "reason": item.reason}
                for item in result.rejections
            ],
            "limitations": result.limitations,
        },
        indent=2,
        sort_keys=True,
    )
