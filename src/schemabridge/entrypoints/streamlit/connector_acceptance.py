"""Secret-free M28 browser evidence projected through production UI values.

The production Streamlit app never imports this module.  The dedicated M28
acceptance script reads one owner-only, already sanitized evidence file and
passes these values to the same Query Studio presentation component used by
the product.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemabridge.application.connectors import ConnectorTargetErrorCode
from schemabridge.application.ui_view_models import (
    JudgeUiView,
    UiExecutionTarget,
    UiHealth,
    UiHealthStatus,
    UiMode,
    UiPolicyCheck,
    UiQuery,
    UiQueryCostAssessment,
    UiQueryCostBudget,
    UiReferenceData,
    UiRegistryProjectionStatus,
    UiResult,
    UiSemanticGateEvidence,
)
from schemabridge.domain.connectors import (
    QueryCostDecision,
    QueryCostRejectionCode,
)

M28_BROWSER_RELEASE_REF: Final = "m28-browser-acceptance"
M28_BROWSER_STATE_FILE_ENV: Final = "SCHEMABRIDGE_M28_BROWSER_STATE_FILE"
M28_BROWSER_SCENARIO_ENV: Final = "SCHEMABRIDGE_M28_BROWSER_SCENARIO"
M28_HOSTILE_METADATA: Final = (
    'Catalog label <script data-m28-hostile>window.__m28_xss = "executed"</script>'
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SAFE_CONNECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{2,99}$")
_PUBLIC_ERROR_CODES = frozenset(
    {
        ConnectorTargetErrorCode.UNAVAILABLE.value,
        ConnectorTargetErrorCode.ROUTE_STALE.value,
        ConnectorTargetErrorCode.ROUTE_DISABLED.value,
        ConnectorTargetErrorCode.DIALECT_UNSUPPORTED.value,
        QueryCostRejectionCode.TIMEOUT.value,
    }
)
_ROUTE_SCENARIO_ERRORS: Final = {
    "route_disabled": ConnectorTargetErrorCode.ROUTE_DISABLED.value,
    "route_stale": ConnectorTargetErrorCode.ROUTE_STALE.value,
    "route_unavailable": ConnectorTargetErrorCode.UNAVAILABLE.value,
    "rotated_after_confirmation": ConnectorTargetErrorCode.ROUTE_STALE.value,
}


class M28BrowserScenario(StrEnum):
    """Closed browser states required by the M28 manual matrix."""

    TENANT_A_ACCEPTED = "tenant_a_accepted"
    TENANT_B_ACCEPTED = "tenant_b_accepted"
    COST_REJECTED = "cost_rejected"
    ROUTE_DISABLED = "route_disabled"
    ROUTE_STALE = "route_stale"
    ROUTE_UNAVAILABLE = "route_unavailable"
    EXPLAIN_TIMEOUT = "explain_timeout"
    UNSUPPORTED_DIALECT = "unsupported_dialect"
    ROTATED_AFTER_CONFIRMATION = "rotated_after_confirmation"


M28_BROWSER_SCENARIO_LABELS: Final = {
    M28BrowserScenario.TENANT_A_ACCEPTED: "Workspace A · accepted",
    M28BrowserScenario.TENANT_B_ACCEPTED: "Workspace B · accepted",
    M28BrowserScenario.COST_REJECTED: "Cost rejected",
    M28BrowserScenario.ROUTE_DISABLED: "Route disabled",
    M28BrowserScenario.ROUTE_STALE: "Route stale",
    M28BrowserScenario.ROUTE_UNAVAILABLE: "Route unavailable",
    M28BrowserScenario.EXPLAIN_TIMEOUT: "EXPLAIN timeout",
    M28BrowserScenario.UNSUPPORTED_DIALECT: "Unsupported dialect",
    M28BrowserScenario.ROTATED_AFTER_CONFIRMATION: "Rotated after confirmation",
}


def is_m28_private_environment_key(name: str) -> bool:
    """Return whether an environment name could carry a private capability."""

    normalized = name.upper()
    return (
        normalized == "DATABASE_URL"
        or normalized.endswith("_DATABASE_URL")
        or any(
            marker in normalized
            for marker in (
                "PASSWORD",
                "TOKEN",
                "API_KEY",
                "SECRET",
                "DSN",
                "CREDENTIAL",
            )
        )
    )


class _PublicEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class M28CostBudgetEvidence(_PublicEvidenceModel):
    """Only the public numerical cost policy."""

    fingerprint: str
    explain_timeout_ms: int = Field(strict=True, ge=1, le=60_000)
    max_response_bytes: int = Field(strict=True, ge=1, le=4 * 1_024 * 1_024)
    max_total_cost: str = Field(min_length=1, max_length=32)
    max_estimated_rows: int = Field(strict=True, ge=0)
    max_plan_nodes: int = Field(strict=True, ge=1, le=100_000)
    max_plan_depth: int = Field(strict=True, ge=1, le=256)
    max_plan_width: int = Field(strict=True, ge=0)

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("max_total_cost")
    @classmethod
    def total_cost_must_be_finite(cls, value: str) -> str:
        return _require_non_negative_decimal(value)


class M28CostAssessmentEvidence(_PublicEvidenceModel):
    """Sanitized EXPLAIN scalars; the raw plan has no representable field."""

    decision: QueryCostDecision
    rejection_codes: tuple[QueryCostRejectionCode, ...] = ()
    total_cost: str | None = Field(default=None, max_length=32)
    estimated_root_rows: int | None = Field(default=None, strict=True, ge=0)
    plan_width: int | None = Field(default=None, strict=True, ge=0)
    plan_node_count: int | None = Field(default=None, strict=True, ge=0)
    plan_depth: int | None = Field(default=None, strict=True, ge=0)
    response_bytes: int | None = Field(default=None, strict=True, ge=0)
    read_only: bool | None = Field(default=None, strict=True)
    explain_timeout_ms: int = Field(strict=True, ge=1, le=60_000)
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("total_cost")
    @classmethod
    def total_cost_must_be_finite(cls, value: str | None) -> str | None:
        return None if value is None else _require_non_negative_decimal(value)

    @model_validator(mode="after")
    def decision_must_match_evidence(self) -> Self:
        if self.decision is QueryCostDecision.ACCEPTED:
            if self.rejection_codes or self.read_only is not True:
                raise ValueError("accepted cost evidence is inconsistent")
            if any(
                value is None
                for value in (
                    self.total_cost,
                    self.estimated_root_rows,
                    self.plan_width,
                    self.plan_node_count,
                    self.plan_depth,
                    self.response_bytes,
                )
            ):
                raise ValueError("accepted cost evidence is incomplete")
        elif not self.rejection_codes:
            raise ValueError("rejected cost evidence requires one stable reason")
        return self


class M28ResultEvidence(_PublicEvidenceModel):
    """One aggregate-only real preview result safe for browser replay."""

    columns: tuple[Literal["approved_rows"], ...]
    rows: tuple[tuple[int, ...], ...]
    row_count: int = Field(strict=True, ge=1, le=1)
    preview_fingerprint: str
    database_user: str = Field(min_length=1, max_length=63)
    transaction_read_only: Literal[True]
    statement_timeout_ms: int = Field(strict=True, ge=10, le=60_000)
    truncated: Literal[False]

    @field_validator("rows", mode="before")
    @classmethod
    def row_values_must_be_exact_integers(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, list | tuple) or any(
            not isinstance(row, list | tuple) or any(type(item) is not int for item in row)
            for row in value
        ):
            raise ValueError("browser result rows require exact integers")
        return value

    @field_validator("transaction_read_only", mode="before")
    @classmethod
    def transaction_must_be_exactly_read_only(cls, value: object) -> object:
        if value is not True:
            raise ValueError("browser result transaction must be read-only")
        return value

    @field_validator("truncated", mode="before")
    @classmethod
    def truncation_must_be_exactly_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("browser result cannot be truncated")
        return value

    @field_validator("preview_fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("database_user")
    @classmethod
    def reader_must_be_inert(cls, value: str) -> str:
        if _SAFE_READER.fullmatch(value) is None:
            raise ValueError("browser result reader is invalid")
        return value

    @model_validator(mode="after")
    def aggregate_result_must_be_exact(self) -> Self:
        if (
            self.columns != ("approved_rows",)
            or len(self.rows) != 1
            or len(self.rows[0]) != 1
            or type(self.rows[0][0]) is not int
            or self.rows[0][0] < 1
            or self.row_count != len(self.rows)
        ):
            raise ValueError("browser result must contain one positive aggregate count")
        expected = _fingerprint(
            {
                "columns": self.columns,
                "rows": self.rows,
                "database_user": self.database_user,
                "transaction_read_only": self.transaction_read_only,
                "statement_timeout_ms": self.statement_timeout_ms,
                "truncated": self.truncated,
            }
        )
        if self.preview_fingerprint != expected:
            raise ValueError("browser result fingerprint is invalid")
        return self


class M28ScenarioEvidence(_PublicEvidenceModel):
    """One closed presentation scenario with no private route surface."""

    scenario: M28BrowserScenario
    workspace_label: str = Field(min_length=3, max_length=80)
    connection_label: str = Field(min_length=3, max_length=100)
    connection_id: str = Field(min_length=3, max_length=100)
    connector_kind: str = Field(min_length=3, max_length=32)
    dialect: str = Field(min_length=3, max_length=32)
    route_revision: int = Field(strict=True, ge=1)
    current_route_revision: int | None = Field(default=None, strict=True, ge=1)
    route_fingerprint: str
    target_fingerprint: str
    type_contract_fingerprint: str
    cost_budget: M28CostBudgetEvidence
    cost_assessment: M28CostAssessmentEvidence | None = None
    preflight_error_code: str | None = Field(default=None, max_length=80)
    execution_allowed: bool = Field(strict=True)
    result: M28ResultEvidence | None = None
    blocker: str = Field(min_length=3, max_length=240)

    @field_validator(
        "route_fingerprint",
        "target_fingerprint",
        "type_contract_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("connection_label", "connection_id")
    @classmethod
    def connection_text_must_be_inert(cls, value: str) -> str:
        if _SAFE_CONNECTION.fullmatch(value) is None:
            raise ValueError("browser connection label is invalid")
        return value

    @field_validator("preflight_error_code")
    @classmethod
    def error_code_must_be_closed(cls, value: str | None) -> str | None:
        if value is not None and value not in _PUBLIC_ERROR_CODES:
            raise ValueError("browser preflight error code is unsupported")
        return value

    @model_validator(mode="after")
    def scenario_must_match_authority(self) -> Self:
        accepted = self.scenario in {
            M28BrowserScenario.TENANT_A_ACCEPTED,
            M28BrowserScenario.TENANT_B_ACCEPTED,
        }
        if accepted:
            if (
                self.dialect != "postgresql"
                or self.connector_kind != "postgresql"
                or self.preflight_error_code is not None
                or not self.execution_allowed
                or self.result is None
                or self.cost_assessment is None
                or self.cost_assessment.decision is not QueryCostDecision.ACCEPTED
            ):
                raise ValueError("accepted browser scenario is inconsistent")
        elif self.execution_allowed or self.result is not None:
            raise ValueError("blocked browser scenario cannot expose execution")

        if self.scenario is M28BrowserScenario.COST_REJECTED and (
            self.cost_assessment is None
            or self.cost_assessment.decision is not QueryCostDecision.REJECTED
            or QueryCostRejectionCode.TOTAL_COST_EXCEEDED
            not in self.cost_assessment.rejection_codes
            or self.preflight_error_code is not None
        ):
            raise ValueError("cost-rejected browser scenario is inconsistent")
        if self.scenario is M28BrowserScenario.EXPLAIN_TIMEOUT and (
            self.cost_assessment is None
            or QueryCostRejectionCode.TIMEOUT not in self.cost_assessment.rejection_codes
            or self.preflight_error_code != QueryCostRejectionCode.TIMEOUT.value
        ):
            raise ValueError("timeout browser scenario is inconsistent")
        expected_route_error = _ROUTE_SCENARIO_ERRORS.get(self.scenario.value)
        if expected_route_error is not None and self.preflight_error_code != expected_route_error:
            raise ValueError("route browser scenario has the wrong safe failure")
        if self.scenario is M28BrowserScenario.ROTATED_AFTER_CONFIRMATION:
            if (
                self.current_route_revision is None
                or self.current_route_revision <= self.route_revision
            ):
                raise ValueError("rotated browser scenario requires a newer current route")
        elif self.current_route_revision is not None:
            raise ValueError("only the rotated browser scenario has a current route revision")
        if self.scenario is M28BrowserScenario.UNSUPPORTED_DIALECT and (
            self.dialect == "postgresql"
            or self.preflight_error_code != ConnectorTargetErrorCode.DIALECT_UNSUPPORTED.value
            or self.cost_assessment is not None
        ):
            raise ValueError("unsupported-dialect browser scenario is inconsistent")
        return self


class M28BrowserAcceptanceState(_PublicEvidenceModel):
    """Complete bounded public handoff from real setup to the browser process."""

    schema_version: Literal[1] = 1
    source_pair_count: int = Field(strict=True, ge=2, le=2)
    real_preflight_calls: int = Field(strict=True, ge=4, le=32)
    real_preview_calls: int = Field(strict=True, ge=2, le=2)
    same_physical_shape: Literal[True]
    query_shape_fingerprint: str
    hostile_metadata: str = Field(min_length=1, max_length=512)
    scenarios: tuple[M28ScenarioEvidence, ...]
    evidence_fingerprint: str

    @field_validator("same_physical_shape", mode="before")
    @classmethod
    def shape_fact_must_be_exact_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("browser source-shape evidence is invalid")
        return value

    @field_validator("query_shape_fingerprint", "evidence_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def evidence_set_must_be_complete(self) -> Self:
        if self.hostile_metadata != M28_HOSTILE_METADATA:
            raise ValueError("browser hostile-metadata probe is not the reviewed fixture")
        if tuple(item.scenario for item in self.scenarios) != tuple(M28BrowserScenario):
            raise ValueError("browser scenarios are missing, duplicated, or reordered")
        alpha = self.for_scenario(M28BrowserScenario.TENANT_A_ACCEPTED)
        beta = self.for_scenario(M28BrowserScenario.TENANT_B_ACCEPTED)
        assert alpha.result is not None and beta.result is not None
        if (
            alpha.connection_id != beta.connection_id
            or alpha.target_fingerprint == beta.target_fingerprint
            or alpha.route_revision == beta.route_revision
            or alpha.cost_budget.fingerprint == beta.cost_budget.fingerprint
            or alpha.result.database_user == beta.result.database_user
            or alpha.result.rows == beta.result.rows
        ):
            raise ValueError("browser tenant-isolation evidence is incomplete")
        expected = _fingerprint(
            self.model_dump(
                mode="json",
                exclude={"evidence_fingerprint"},
            )
        )
        if self.evidence_fingerprint != expected:
            raise ValueError("browser evidence fingerprint is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        real_preflight_calls: int,
        query_shape_fingerprint: str,
        scenarios: tuple[M28ScenarioEvidence, ...],
    ) -> M28BrowserAcceptanceState:
        """Create one self-fingerprinted public state after real setup succeeds."""

        payload: dict[str, object] = {
            "schema_version": 1,
            "source_pair_count": 2,
            "real_preflight_calls": real_preflight_calls,
            "real_preview_calls": 2,
            "same_physical_shape": True,
            "query_shape_fingerprint": query_shape_fingerprint,
            "hostile_metadata": M28_HOSTILE_METADATA,
            "scenarios": [item.model_dump(mode="json") for item in scenarios],
        }
        return cls.model_validate(
            {
                **payload,
                "evidence_fingerprint": _fingerprint(payload),
            }
        )

    def for_scenario(self, scenario: M28BrowserScenario) -> M28ScenarioEvidence:
        return next(item for item in self.scenarios if item.scenario is scenario)

    def to_json(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )

    @classmethod
    def from_json(cls, raw: bytes) -> M28BrowserAcceptanceState:
        try:
            payload = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_unique_object,
            )
            return cls.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("M28 browser evidence is invalid") from error


def build_m28_browser_view(
    state: M28BrowserAcceptanceState,
    scenario: M28BrowserScenario,
    *,
    executed: bool,
) -> JudgeUiView:
    """Project one evidence state through the existing production view models."""

    evidence = state.for_scenario(scenario)
    target = UiExecutionTarget(
        connection_id=evidence.connection_id,
        connector_kind=evidence.connector_kind,
        dialect=evidence.dialect,
        route_revision=evidence.route_revision,
        route_fingerprint=_abbreviate(evidence.route_fingerprint),
        target_fingerprint=_abbreviate(evidence.target_fingerprint),
        type_contract_fingerprint=_abbreviate(evidence.type_contract_fingerprint),
    )
    budget = UiQueryCostBudget(
        fingerprint=_abbreviate(evidence.cost_budget.fingerprint),
        explain_timeout_ms=evidence.cost_budget.explain_timeout_ms,
        max_response_bytes=evidence.cost_budget.max_response_bytes,
        max_total_cost=evidence.cost_budget.max_total_cost,
        max_estimated_rows=evidence.cost_budget.max_estimated_rows,
        max_plan_nodes=evidence.cost_budget.max_plan_nodes,
        max_plan_depth=evidence.cost_budget.max_plan_depth,
        max_plan_width=evidence.cost_budget.max_plan_width,
    )
    assessment = (
        _ui_cost_assessment(evidence.cost_assessment)
        if evidence.cost_assessment is not None
        else None
    )
    result = (
        _ui_result(evidence.result, evidence.workspace_label)
        if executed and evidence.result is not None
        else None
    )
    policy_checks = (
        ()
        if scenario is M28BrowserScenario.UNSUPPORTED_DIALECT
        else (
            UiPolicyCheck(
                name="Typed deterministic compiler",
                status="accepted",
                detail="The restricted IR was compiled for the exact PostgreSQL target.",
            ),
            UiPolicyCheck(
                name="Independent AST guard",
                status="accepted",
                detail="One allowlisted read-only SELECT with a bounded result was reparsed.",
            ),
            UiPolicyCheck(
                name="Read-only cost preflight",
                status="accepted",
                detail="EXPLAIN used ANALYZE FALSE and rolled back its transaction.",
            ),
        )
    )
    query = UiQuery(
        business_text=("Cuenta las filas aprobadas de la relación gobernada para este workspace."),
        stage="execution_completed" if result is not None else "sql_validated",
        checkpoint="execution_approval",
        interpretation_adapter="deterministic typed M28 acceptance request",
        ambiguities=(),
        findings=_scenario_findings(evidence),
        alternatives=(),
        selected_assets=("public.routing_probe",),
        mapping_versions=("Approved row → public.routing_probe.approved_row (v1, approved)",),
        join_path=(),
        assumptions=(
            "One exact tenant-scoped connection; federation remains forbidden.",
            state.hostile_metadata,
        ),
        fanout_summary="No join; fanout is not applicable.",
        fanout_mitigations=(),
        sql=None,
        parameters=(),
        policy_checks=policy_checks,
        validated_request_fingerprint=state.query_shape_fingerprint,
        resolved_plan_fingerprint=evidence.target_fingerprint,
        semantic_gate=UiSemanticGateEvidence(
            status="eligible",
            baseline_revision=28,
            dependencies_fingerprint=_fingerprint(
                {
                    "query_shape": state.query_shape_fingerprint,
                    "target": evidence.target_fingerprint,
                }
            ),
        ),
        execution_target=target,
        cost_budget=budget,
        cost_assessment=assessment,
        preflight_error_code=evidence.preflight_error_code,
        plan_json=None,
        can_confirm=False,
        can_execute=evidence.execution_allowed and not executed,
        can_publish=False,
        execution_blockers=(() if evidence.execution_allowed else (evidence.blocker,)),
        result=result,
    )
    return JudgeUiView(
        workflow_id=f"m28-browser-{scenario.value}",
        revision=1,
        modes=_modes(evidence),
        health=(
            UiHealth(
                name="Connector route",
                status=(
                    UiHealthStatus.READY if evidence.execution_allowed else UiHealthStatus.BLOCKED
                ),
                detail=evidence.blocker,
            ),
        ),
        reference=_reference(state),
        query=query,
        decisions=(),
        trace=(),
    )


def _ui_cost_assessment(
    evidence: M28CostAssessmentEvidence,
) -> UiQueryCostAssessment:
    return UiQueryCostAssessment(
        decision=evidence.decision.value,
        rejection_codes=tuple(item.value for item in evidence.rejection_codes),
        total_cost=evidence.total_cost,
        estimated_root_rows=evidence.estimated_root_rows,
        plan_width=evidence.plan_width,
        plan_node_count=evidence.plan_node_count,
        plan_depth=evidence.plan_depth,
        response_bytes=evidence.response_bytes,
        read_only=evidence.read_only,
        explain_timeout_ms=evidence.explain_timeout_ms,
        fingerprint=_abbreviate(evidence.fingerprint),
    )


def _ui_result(
    evidence: M28ResultEvidence,
    workspace_label: str,
) -> UiResult:
    return UiResult(
        columns=evidence.columns,
        rows=tuple(tuple(value for value in row) for row in evidence.rows),
        row_count=evidence.row_count,
        preview_fingerprint=evidence.preview_fingerprint,
        database_user=evidence.database_user,
        transaction_read_only=evidence.transaction_read_only,
        statement_timeout_ms=evidence.statement_timeout_ms,
        truncated=evidence.truncated,
        rejections=(),
        limitations=(
            f"{workspace_label}: aggregate-only retained evidence from the real routed preview.",
            "The browser process receives no source credential and cannot reopen the source.",
        ),
    )


def _scenario_findings(evidence: M28ScenarioEvidence) -> tuple[str, ...]:
    findings = [
        f"Public connection label: {evidence.connection_label}.",
        f"Scenario: {M28_BROWSER_SCENARIO_LABELS[evidence.scenario]}.",
    ]
    if evidence.current_route_revision is not None:
        findings.append(
            "The confirmed plan remains bound to "
            f"r{evidence.route_revision}; current route r{evidence.current_route_revision} "
            "requires a new plan and approval."
        )
    return tuple(findings)


def _modes(evidence: M28ScenarioEvidence) -> tuple[UiMode, ...]:
    return (
        UiMode(
            name="Connector",
            label=f"{evidence.connection_label} · r{evidence.route_revision}",
            kind="live",
            detail="Real PostgreSQL setup evidence; private material removed before UI launch.",
        ),
        UiMode(
            name="Dialect",
            label=evidence.dialect,
            kind="live" if evidence.dialect == "postgresql" else "fake",
            detail="Only PostgreSQL has an executable compiler/guard/preflight capability.",
        ),
        UiMode(
            name="Cost",
            label=(
                evidence.cost_assessment.decision.value
                if evidence.cost_assessment is not None
                else evidence.preflight_error_code or "unavailable"
            ),
            kind="recorded",
            detail="Sanitized scalar assessment; raw EXPLAIN is not retained.",
        ),
    )


def _reference(state: M28BrowserAcceptanceState) -> UiReferenceData:
    return UiReferenceData(
        concept_name="Governed connector acceptance",
        concept_definition="Exact tenant route plus bounded cost decision.",
        candidate_source="synthetic M28 setup",
        context_source="recorded:m28-real-connector-evidence",
        context_version=28,
        registry_id="m28-browser-evidence",
        registry_fingerprint=state.evidence_fingerprint,
        catalog_scope="synthetic-browser-acceptance",
        model_count=0,
        logical_models=(),
        candidates=(),
        mappings=(),
        relationships=(),
        activation_generation=None,
        active_pointer_fingerprint=None,
        projection_status=UiRegistryProjectionStatus.FIXED,
    )


def _require_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("browser evidence fingerprint must be lowercase SHA-256")
    return value


def _require_non_negative_decimal(value: str) -> str:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("browser cost evidence is invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("browser cost evidence is invalid")
    return value


def _abbreviate(value: str) -> str:
    _require_sha256(value)
    return f"{value[:12]}…"


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate browser evidence key")
        result[key] = value
    return result


__all__ = [
    "M28_BROWSER_RELEASE_REF",
    "M28_BROWSER_SCENARIO_ENV",
    "M28_BROWSER_SCENARIO_LABELS",
    "M28_BROWSER_STATE_FILE_ENV",
    "M28_HOSTILE_METADATA",
    "M28BrowserAcceptanceState",
    "M28BrowserScenario",
    "M28CostAssessmentEvidence",
    "M28CostBudgetEvidence",
    "M28ResultEvidence",
    "M28ScenarioEvidence",
    "build_m28_browser_view",
    "is_m28_private_environment_key",
]
