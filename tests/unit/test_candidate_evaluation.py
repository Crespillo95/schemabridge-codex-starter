"""Honest labeled-fixture candidate metrics."""

from pathlib import Path

from schemabridge.adapters.matching.evaluation import YamlCandidateEvaluationAdapter
from schemabridge.application.candidate_engine import EvaluateSemanticCandidates
from schemabridge.domain.candidates import EvaluationCaseKind

ROOT = Path(__file__).parents[2]


def test_labeled_fixture_contains_negatives_homonym_and_hidden_synonym() -> None:
    dataset = YamlCandidateEvaluationAdapter(
        ROOT / "demo/ground_truth/semantic_mappings.yml"
    ).load()

    kinds = {case.kind for case in dataset.cases}
    assert EvaluationCaseKind.NEGATIVE in kinds
    assert EvaluationCaseKind.HOMONYM in kinds
    assert EvaluationCaseKind.HIDDEN_SYNONYM in kinds
    assert "not production evidence" in dataset.fixture_notice


def test_evaluation_reports_every_false_positive_and_false_negative() -> None:
    report = EvaluateSemanticCandidates(
        YamlCandidateEvaluationAdapter(ROOT / "demo/ground_truth/semantic_mappings.yml")
    ).execute()

    assert report.case_count == 7
    assert report.false_positives == ("support_customer_key_homonym",)
    assert report.false_negatives == ("archive_subject_ref_hidden_synonym",)
    assert report.precision == 0.75
    assert report.recall == 0.75
    assert report.f1 == 0.75
    assert set(report.top_k_recall) == {1, 3, 5}
    assert (
        sum(
            len(group)
            for group in (
                report.true_positives,
                report.true_negatives,
                report.false_positives,
                report.false_negatives,
            )
        )
        == report.case_count
    )
