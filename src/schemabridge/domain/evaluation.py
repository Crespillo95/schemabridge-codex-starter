"""Pure contracts and normalization for reproducible synthetic evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.joins import Cardinality
from schemabridge.domain.recipes import QueryRecipe
from schemabridge.domain.requests import AnalyticalRequest


class EvaluationMode(StrEnum):
    DETERMINISTIC = "deterministic"
    LIVE_LLM = "live_llm"


class EvaluationRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class EvaluationSectionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvaluationCaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    OBSERVED = "observed"


class EvaluationGuidedCase(StrEnum):
    NORTH_STAR = "north_star"
    NO_JOIN = "no_join"


class SourceFixtureVersion(FrozenDomainModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: int = Field(ge=1)


class NormalizedCell(FrozenDomainModel):
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str


class NormalizedRow(FrozenDomainModel):
    cells: tuple[NormalizedCell, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def columns_are_sorted_and_unique(self) -> NormalizedRow:
        columns = tuple(cell.column for cell in self.cells)
        if columns != tuple(sorted(columns)) or len(columns) != len(set(columns)):
            raise ValueError("normalized row columns must be sorted and unique")
        return self


class EvaluationQueryGroundTruth(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    guided_case: EvaluationGuidedCase
    question: str = Field(min_length=1, max_length=2_000)
    expected_request: AnalyticalRequest
    expected_join_contracts: tuple[str, ...] = Field(max_length=2)
    expected_rows: tuple[NormalizedRow, ...]
    expected_rejection_codes: tuple[str, ...] = ()
    intent_alternative: IntentAlternativeId | None = None
    intent_skip_reason: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def intent_case_is_evaluated_or_skipped(self) -> EvaluationQueryGroundTruth:
        if (self.intent_alternative is None) == (self.intent_skip_reason is None):
            raise ValueError("intent case requires exactly one alternative or skip reason")
        return self


class EvaluationJoinGroundTruth(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    cardinality: Cardinality


class EvaluationSafetyGroundTruth(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    sql: str = Field(min_length=1)
    expected_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    effective_limit: int = Field(default=1, ge=1, le=10_000)


class EvaluationRecipeReuseGroundTruth(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    mutation: str = Field(pattern=r"^(none|increment_first_mapping_version)$")
    expected_status: str = Field(pattern=r"^(reusable|stale)$")
    expected_reasons: tuple[str, ...] = ()


class EvaluationGroundTruth(FrozenDomainModel):
    version: int = Field(ge=1)
    fixture_notice: str = Field(min_length=1)
    sources: tuple[SourceFixtureVersion, ...] = Field(min_length=5)
    queries: tuple[EvaluationQueryGroundTruth, ...] = Field(min_length=1)
    joins: tuple[EvaluationJoinGroundTruth, ...] = Field(min_length=1)
    safety_cases: tuple[EvaluationSafetyGroundTruth, ...] = Field(min_length=1)
    recipe_reuse_cases: tuple[EvaluationRecipeReuseGroundTruth, ...] = Field(min_length=1)
    recipe: QueryRecipe
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identities_are_unique(self) -> EvaluationGroundTruth:
        for values in (
            tuple(item.name for item in self.sources),
            tuple(item.id for item in self.queries),
            tuple(item.id for item in self.joins),
            tuple(item.id for item in self.safety_cases),
            tuple(item.id for item in self.recipe_reuse_cases),
        ):
            if len(values) != len(set(values)):
                raise ValueError("evaluation ground-truth identities must be unique")
        return self


class EvaluationReleaseIdentity(FrozenDomainModel):
    revision: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dirty: bool
    package_version: str = Field(min_length=1, max_length=40)


class EvaluationMetric(FrozenDomainModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    numerator: float = Field(ge=0.0)
    denominator: float = Field(gt=0.0)
    value: float = Field(ge=0.0, le=1.0)
    evaluated_cases: int = Field(ge=0)
    skipped_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ratio_is_consistent(self) -> EvaluationMetric:
        if not math.isclose(self.value, self.numerator / self.denominator, abs_tol=1e-9):
            raise ValueError("evaluation metric value must match its visible ratio")
        return self


class EvaluationCaseResult(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,99}$")
    status: EvaluationCaseStatus
    expected: str = Field(min_length=1, max_length=4_000)
    actual: str = Field(min_length=1, max_length=4_000)
    detail: str = Field(min_length=1, max_length=1_000)
    blocking: bool


class EvaluationSection(FrozenDomainModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    label: str = Field(min_length=1, max_length=120)
    status: EvaluationSectionStatus
    metrics: tuple[EvaluationMetric, ...] = ()
    cases: tuple[EvaluationCaseResult, ...] = ()
    failures: tuple[str, ...] = ()

    @model_validator(mode="after")
    def status_matches_blocking_cases(self) -> EvaluationSection:
        blocking_failure = any(
            case.blocking and case.status is EvaluationCaseStatus.FAILED for case in self.cases
        ) or bool(self.failures)
        if (self.status is EvaluationSectionStatus.FAILED) != blocking_failure:
            raise ValueError("evaluation section status must match blocking failures")
        return self


class EvaluationRun(FrozenDomainModel):
    mode: EvaluationMode
    adapter: str = Field(min_length=1, max_length=120)
    required: bool
    status: EvaluationRunStatus
    sections: tuple[EvaluationSection, ...] = ()
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def status_payload_is_consistent(self) -> EvaluationRun:
        if self.status is EvaluationRunStatus.NOT_RUN:
            if self.sections or self.reason is None:
                raise ValueError("not-run evaluation requires only a reason")
        elif self.reason is not None:
            raise ValueError("executed evaluation cannot carry a not-run reason")
        if self.status is EvaluationRunStatus.COMPLETED and any(
            section.status is EvaluationSectionStatus.FAILED for section in self.sections
        ):
            raise ValueError("completed evaluation cannot contain a failed section")
        if self.status is EvaluationRunStatus.FAILED and not any(
            section.status is EvaluationSectionStatus.FAILED for section in self.sections
        ):
            raise ValueError("failed evaluation requires a failed section")
        return self


class EvaluationReport(FrozenDomainModel):
    schema_version: int = 1
    ground_truth_version: int = Field(ge=1)
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_notice: str = Field(min_length=1)
    release: EvaluationReleaseIdentity
    runs: tuple[EvaluationRun, ...] = Field(min_length=2)
    regression_thresholds: tuple[str, ...] = ()
    statistical_note: str = Field(min_length=1)
    successful: bool

    @model_validator(mode="after")
    def success_matches_required_runs(self) -> EvaluationReport:
        expected = all(
            not run.required or run.status is EvaluationRunStatus.COMPLETED for run in self.runs
        )
        if self.successful != expected:
            raise ValueError("report success must match every required evaluation run")
        if self.regression_thresholds:
            raise ValueError("M15 has no justified regression thresholds")
        modes = tuple(run.mode for run in self.runs)
        if len(modes) != len(set(modes)):
            raise ValueError("deterministic and live-LLM runs must remain separate")
        return self


def rate_metric(
    name: str,
    numerator: float,
    denominator: float,
    *,
    evaluated_cases: int,
    skipped_cases: int = 0,
    failed_cases: int = 0,
    note: str,
) -> EvaluationMetric:
    """Build a visible bounded ratio without hiding its raw counts."""

    return EvaluationMetric(
        name=name,
        numerator=numerator,
        denominator=denominator,
        value=round(numerator / denominator, 9),
        evaluated_cases=evaluated_cases,
        skipped_cases=skipped_cases,
        failed_cases=failed_cases,
        note=note,
    )


def normalize_mapping_rows(rows: tuple[dict[str, object], ...]) -> tuple[NormalizedRow, ...]:
    return _sort_rows(
        tuple(
            NormalizedRow(
                cells=tuple(
                    NormalizedCell(column=column, value=normalize_scalar(value))
                    for column, value in sorted(row.items())
                )
            )
            for row in rows
        )
    )


def normalize_preview_rows(
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
) -> tuple[NormalizedRow, ...]:
    if any(len(row) != len(columns) for row in rows):
        raise ValueError("preview row width does not match its columns")
    return normalize_mapping_rows(tuple(dict(zip(columns, row, strict=True)) for row in rows))


def normalized_rows_json(rows: tuple[NormalizedRow, ...]) -> str:
    return json.dumps(
        [[cell.model_dump(mode="json") for cell in row.cells] for row in rows],
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool:{str(value).lower()}"
    if isinstance(value, int):
        return f"int:{value}"
    if isinstance(value, Decimal):
        return f"decimal:{format(value.normalize(), 'f')}"
    if isinstance(value, float):
        if math.isnan(value):
            return "float:nan"
        if math.isinf(value):
            return "float:+inf" if value > 0 else "float:-inf"
        return f"float:{format(value, '.17g')}"
    if isinstance(value, datetime):
        return f"datetime:{value.isoformat()}"
    if isinstance(value, date):
        return f"date:{value.isoformat()}"
    if isinstance(value, str):
        return f"str:{value}"
    raise TypeError(f"unsupported evaluation scalar type: {type(value).__name__}")


def fingerprint_fixture(parts: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(parts):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _sort_rows(rows: tuple[NormalizedRow, ...]) -> tuple[NormalizedRow, ...]:
    return tuple(
        sorted(
            rows,
            key=lambda row: tuple((cell.column, cell.value) for cell in row.cells),
        )
    )
