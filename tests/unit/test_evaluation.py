"""Raw-count metric, versioned-fixture, normalization, and report regressions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from schemabridge.adapters.evaluation.ground_truth import (
    RecordedEvaluationGroundTruthAdapter,
)
from schemabridge.adapters.evaluation.reporting import FileEvaluationReportWriter
from schemabridge.bootstrap import build_evaluation_runner
from schemabridge.config import Settings
from schemabridge.domain.evaluation import (
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationMode,
    EvaluationReleaseIdentity,
    EvaluationReport,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSection,
    EvaluationSectionStatus,
    normalize_mapping_rows,
    normalize_preview_rows,
    rate_metric,
)

ROOT = Path(__file__).parents[2]


def test_evaluation_composition_uses_the_fixed_demo_reader_when_url_is_absent() -> None:
    runner = build_evaluation_runner(
        repository_root=ROOT,
        settings=Settings.model_validate({"DATABASE_URL": None}),
    )

    assert runner.ground_truth.load().version == 1


def test_versioned_ground_truth_loads_every_small_synthetic_denominator() -> None:
    truth = RecordedEvaluationGroundTruthAdapter(ROOT).load()

    assert truth.version == 1
    assert {item.name: item.version for item in truth.sources} == {
        "approved_logical_context": 1,
        "join_contracts": 1,
        "planning_mappings": 1,
        "query_cases": 1,
        "query_recipe": 1,
        "semantic_mappings": 1,
        "sql_guard_cases": 1,
    }
    assert len(truth.queries) == 2
    assert len(truth.joins) == 2
    assert len(truth.safety_cases) == 38
    assert len(truth.recipe_reuse_cases) == 2
    assert "Raw counts only" in truth.fixture_notice
    assert "sql" not in truth.recipe.__class__.model_fields


def test_rate_metric_exposes_ratio_skips_and_failures_without_rounding_drift() -> None:
    metric = rate_metric(
        "intent_equivalence",
        3,
        4,
        evaluated_cases=4,
        skipped_cases=2,
        failed_cases=1,
        note="Small fixture raw counts.",
    )

    assert metric.value == 0.75
    assert metric.numerator == 3
    assert metric.denominator == 4
    assert metric.skipped_cases == 2
    assert metric.failed_cases == 1


def test_normalized_result_rows_are_typed_column_keyed_and_order_independent() -> None:
    expected = normalize_mapping_rows(
        (
            {"registration_date": date(2026, 1, 1), "customers": 2},
            {"registration_date": date(2026, 1, 2), "customers": 1},
        )
    )
    actual = normalize_preview_rows(
        ("customers", "registration_date"),
        (
            (1, date(2026, 1, 2)),
            (2, date(2026, 1, 1)),
        ),
    )
    wrong_type = normalize_mapping_rows(
        ({"registration_date": date(2026, 1, 1), "customers": "2"},)
    )

    assert actual == expected
    assert wrong_type[0] != expected[0]


def test_report_writer_is_deterministic_and_labels_live_llm_as_separate(tmp_path: Path) -> None:
    metric = rate_metric(
        "safety_rejection_rate",
        1,
        1,
        evaluated_cases=1,
        note="One explicit guard case.",
    )
    section = EvaluationSection(
        id="sql_safety",
        label="SQL safety",
        status=EvaluationSectionStatus.COMPLETED,
        metrics=(metric,),
        cases=(
            EvaluationCaseResult(
                id="safety_statement_smuggling",
                status=EvaluationCaseStatus.PASSED,
                expected="multiple_statements",
                actual="multiple_statements",
                detail="Guard only; never executed.",
                blocking=True,
            ),
        ),
    )
    report = EvaluationReport(
        ground_truth_version=1,
        fixture_fingerprint="1" * 64,
        fixture_notice="Small synthetic fixture.",
        release=EvaluationReleaseIdentity(
            revision="working-tree-uncommitted",
            source_fingerprint="2" * 64,
            dirty=True,
            package_version="0.1.0",
        ),
        runs=(
            EvaluationRun(
                mode=EvaluationMode.DETERMINISTIC,
                adapter="deterministic fake",
                required=True,
                status=EvaluationRunStatus.COMPLETED,
                sections=(section,),
            ),
            EvaluationRun(
                mode=EvaluationMode.LIVE_LLM,
                adapter="live parser",
                required=False,
                status=EvaluationRunStatus.NOT_RUN,
                reason="Not requested.",
            ),
        ),
        statistical_note="Raw counts only; no confidence interval.",
        successful=True,
    )
    writer = FileEvaluationReportWriter(tmp_path)

    writer.write(report, Path("reports/evaluation.json"), Path("examples/evaluation.md"))
    first_json = (tmp_path / "reports/evaluation.json").read_text(encoding="utf-8")
    first_markdown = (tmp_path / "examples/evaluation.md").read_text(encoding="utf-8")
    writer.write(report, Path("reports/evaluation.json"), Path("examples/evaluation.md"))

    assert (tmp_path / "reports/evaluation.json").read_text() == first_json
    assert (tmp_path / "examples/evaluation.md").read_text() == first_markdown
    assert "## deterministic" in first_markdown
    assert "## live_llm" in first_markdown
    assert "not run" in first_markdown
    assert "not a release-commit claim" in first_markdown
