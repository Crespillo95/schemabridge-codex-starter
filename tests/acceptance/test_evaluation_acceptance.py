"""Observable release-candidate evaluation against the real synthetic reader."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from schemabridge.adapters.evaluation.reporting import FileEvaluationReportWriter
from schemabridge.bootstrap import build_evaluation_runner
from schemabridge.config import Settings
from schemabridge.domain.evaluation import (
    EvaluationGroundTruth,
    EvaluationRunStatus,
    EvaluationSectionStatus,
    NormalizedCell,
    NormalizedRow,
)

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True, slots=True)
class _StaticTruth:
    value: EvaluationGroundTruth

    def load(self) -> EvaluationGroundTruth:
        return self.value


@pytest.mark.acceptance
def test_release_evaluation_measures_every_boundary_and_detects_wrong_rows(
    tmp_path: Path,
) -> None:
    database_url = os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SCHEMABRIDGE_TEST_DATABASE_URL is required for evaluation acceptance")
    settings = Settings.model_validate(
        {
            "DATABASE_URL": database_url,
            "SCHEMABRIDGE_DRAFT_STORE_PATH": tmp_path / "evaluation.db",
        }
    )
    runner = build_evaluation_runner(repository_root=ROOT, settings=settings)

    report = runner.execute_evaluation()

    assert report.successful is True
    deterministic, live = report.runs
    assert deterministic.status is EvaluationRunStatus.COMPLETED
    assert live.status is EvaluationRunStatus.NOT_RUN
    sections = {section.id: section for section in deterministic.sections}
    assert set(sections) == {
        "semantic_matching",
        "join_reasoning",
        "intent_equivalence",
        "query_execution",
        "sql_safety",
        "recipe_reuse",
    }
    assert all(section.status is EvaluationSectionStatus.COMPLETED for section in sections.values())
    metrics = {
        metric.name: metric for section in deterministic.sections for metric in section.metrics
    }
    assert metrics["candidate_precision"].value == 0.75
    assert metrics["candidate_recall"].value == 0.75
    assert metrics["join_path_accuracy"].value == 1.0
    assert metrics["join_cardinality_accuracy"].value == 1.0
    assert metrics["intent_equivalence"].value == 1.0
    assert metrics["compile_success"].value == 1.0
    assert metrics["execution_success"].value == 1.0
    assert metrics["result_correctness"].value == 1.0
    assert metrics["safety_rejection_rate"].value == 1.0
    assert metrics["recipe_reuse_accuracy"].value == 1.0
    candidate_cases = {case.detail for case in sections["semantic_matching"].cases}
    assert "false_positive" in candidate_cases
    assert "false_negative" in candidate_cases

    FileEvaluationReportWriter(tmp_path).write(
        report,
        Path("reports/evaluation.json"),
        Path("examples/evaluation-report.md"),
    )
    assert (tmp_path / "reports/evaluation.json").is_file()
    assert "candidate_precision" in (tmp_path / "examples/evaluation-report.md").read_text()

    truth = runner.ground_truth.load()
    payload = truth.model_dump(mode="python")
    queries = list(payload["queries"])
    first_query = dict(queries[0])
    expected_rows = list(first_query["expected_rows"])
    expected_rows[0] = NormalizedRow(
        cells=(
            NormalizedCell(column="registration_date", value="date:2026-01-01"),
            NormalizedCell(column="secondary_holder_customers", value="int:999"),
        )
    )
    first_query["expected_rows"] = expected_rows
    queries[0] = first_query
    payload["queries"] = queries
    changed = EvaluationGroundTruth.model_validate(payload)
    failed_report = replace(runner, ground_truth=_StaticTruth(changed)).execute_evaluation()
    failed_sections = {section.id: section for section in failed_report.runs[0].sections}

    assert failed_report.successful is False
    assert failed_sections["query_execution"].status is EvaluationSectionStatus.FAILED
    mismatched = next(
        case
        for case in failed_sections["query_execution"].cases
        if case.id == "result_secondary_holders_by_registration_date"
    )
    assert mismatched.status.value == "failed"
    assert "int:999" in mismatched.expected
    assert "int:2" in mismatched.actual
