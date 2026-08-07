"""Bounded catalog retrieval, ranking, and explanation-only LLM behavior."""

from pathlib import Path

from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.matching.evidence import RecordedCandidateEvidenceAdapter
from schemabridge.adapters.matching.fake_llm import FakeDescriptionInterpreter
from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.application.candidate_engine import (
    CandidateRetrievalLimits,
    GenerateSemanticCandidates,
)
from schemabridge.domain.candidates import (
    DescriptionInterpretation,
    DescriptionVerdict,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef

ROOT = Path(__file__).parents[2]


def _engine(
    interpreter: FakeDescriptionInterpreter | None = None,
) -> GenerateSemanticCandidates:
    return GenerateSemanticCandidates(
        catalog=RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        evidence=RecordedCandidateEvidenceAdapter(
            ROOT / "demo/datahub/semantic_evidence_snapshot.json"
        ),
        evidence_source="recorded:synthetic-bounded-signals",
        description_interpreter=interpreter,
    )


def test_recorded_candidate_engine_preserves_top_three_with_reporting_variation() -> None:
    report = _engine().execute(build_customer_key_concept())

    assert report.asset_query == "Customer"
    assert report.assets_considered == 4
    assert report.fields_scanned == 18
    assert report.retrieval_truncated is False
    assert [candidate.physical_field.root for candidate in report.candidates[:3]] == [
        "crm.customers.customer_id",
        "legacy.client_master.client_no",
        "bank.account_holders.gf_customer_id",
    ]
    assert (
        report.candidates[3].physical_field.root == "reporting.customer_accounts.customer_key_text"
    )
    assert all(candidate.evidence for candidate in report.candidates[:3])
    assert all(candidate.missing_evidence for candidate in report.candidates[:3])
    assert all(candidate.status is ApprovalStatus.NEEDS_REVIEW for candidate in report.candidates)


def test_candidate_retrieval_stops_at_explicit_asset_and_field_caps() -> None:
    engine = GenerateSemanticCandidates(
        catalog=RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        evidence=RecordedCandidateEvidenceAdapter(
            ROOT / "demo/datahub/semantic_evidence_snapshot.json"
        ),
        evidence_source="recorded:synthetic-bounded-signals",
        limits=CandidateRetrievalLimits(max_assets=1, max_fields=2, page_size=1),
    )

    report = engine.execute(build_customer_key_concept())

    assert report.asset_query == "Customer"
    assert report.assets_considered == 1
    assert report.fields_scanned == 2
    assert report.retrieval_truncated is True


def test_fake_llm_can_only_attach_typed_explanation() -> None:
    baseline = _engine().execute(build_customer_key_concept())
    interpreted = _engine(
        FakeDescriptionInterpreter(
            (
                DescriptionInterpretation(
                    physical_field=PhysicalFieldRef("crm.customers.customer_id"),
                    verdict=DescriptionVerdict.ALIGNED,
                    explanation="The bounded description uses customer identifier terminology.",
                ),
            )
        )
    ).execute(build_customer_key_concept())

    baseline_customer = baseline.candidates[0]
    interpreted_customer = interpreted.candidates[0]
    assert interpreted_customer.semantic_explanation is not None
    assert interpreted_customer.confidence == baseline_customer.confidence
    assert (
        interpreted_customer.suggested_transformation_plan
        == baseline_customer.suggested_transformation_plan
    )
    assert interpreted_customer.status is ApprovalStatus.NEEDS_REVIEW
