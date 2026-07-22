"""Validated YAML loader for synthetic candidate evaluation cases."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from schemabridge.application.candidate_engine import CandidateGenerationError
from schemabridge.domain.candidates import (
    CandidateEvaluationCase,
    CandidateEvaluationDataset,
    CandidateFieldMetadata,
    EvaluationCaseKind,
    EvaluationLabel,
    ExternalCandidateEvidence,
)
from schemabridge.domain.concepts import CanonicalField


class YamlCandidateEvaluationAdapter:
    """Load only the labeled evaluation section of semantic_mappings.yml."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> CandidateEvaluationDataset:
        try:
            decoded: object = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise CandidateGenerationError(
                "candidate evaluation fixture could not be loaded"
            ) from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("evaluation"), dict):
            raise CandidateGenerationError("semantic mappings lack candidate evaluation labels")
        evaluation = decoded["evaluation"]
        assert isinstance(evaluation, dict)
        concepts = evaluation.get("concepts")
        cases = evaluation.get("cases")
        notice = evaluation.get("fixture_notice")
        if (
            not isinstance(concepts, dict)
            or not isinstance(cases, list)
            or not isinstance(notice, str)
        ):
            raise CandidateGenerationError("candidate evaluation fixture structure is invalid")
        try:
            typed_concepts = {
                str(key): CanonicalField.model_validate(value) for key, value in concepts.items()
            }
            typed_cases = tuple(
                _case(raw_case, typed_concepts) for raw_case in cases if isinstance(raw_case, dict)
            )
            if len(typed_cases) != len(cases):
                raise CandidateGenerationError("candidate evaluation contains a non-object case")
            return CandidateEvaluationDataset(fixture_notice=notice, cases=typed_cases)
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise CandidateGenerationError("candidate evaluation fixture is invalid") from error


def _case(
    raw: dict[object, object], concepts: dict[str, CanonicalField]
) -> CandidateEvaluationCase:
    concept_id = str(raw["concept"])
    concept = concepts[concept_id]
    field = CandidateFieldMetadata.model_validate(
        {
            "id": raw["physical_field"],
            "native_type": raw.get("native_type"),
            "description": raw.get("description"),
            "glossary_terms": raw.get("glossary_terms", []),
            "is_part_of_key": raw.get("is_part_of_key"),
        }
    )
    evidence_payload = raw.get("evidence", {})
    if not isinstance(evidence_payload, dict):
        raise CandidateGenerationError("evaluation evidence must be an object")
    evidence = ExternalCandidateEvidence.model_validate(
        {
            "logical_field": concept.id,
            "physical_field": field.id,
            **evidence_payload,
        }
    )
    return CandidateEvaluationCase(
        id=str(raw["id"]),
        concept=concept,
        field=field,
        label=EvaluationLabel(str(raw["label"])),
        kind=EvaluationCaseKind(str(raw["kind"])),
        evidence=evidence,
        rationale=str(raw["rationale"]),
    )
