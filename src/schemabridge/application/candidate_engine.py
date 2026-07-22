"""Bounded semantic candidate retrieval, ranking, and fixture evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemabridge.application.ports.candidates import (
    CandidateEvaluationDatasetPort,
    CandidateEvidencePort,
    DescriptionInterpreterPort,
)
from schemabridge.application.ports.catalog import CatalogAsset, CatalogReadPort, PageRequest
from schemabridge.domain.candidates import (
    CandidateEvaluationMetrics,
    CandidateFieldMetadata,
    CandidateScoringConfig,
    DescriptionInterpretation,
    ExternalCandidateEvidence,
    SemanticCandidate,
    candidate_is_blocked_in,
    evaluate_candidate_fixture,
    rank_semantic_candidates,
    score_semantic_candidate,
)
from schemabridge.domain.concepts import CanonicalField


class CandidateGenerationError(RuntimeError):
    """Typed invariant failure at an injected candidate-evidence boundary."""


@dataclass(frozen=True, slots=True)
class CandidateRetrievalLimits:
    """Hard bounds that prevent catalog-wide all-pairs comparison."""

    max_assets: int = 10
    max_fields: int = 200
    page_size: int = 25

    def __post_init__(self) -> None:
        if not 1 <= self.max_assets <= 50:
            raise ValueError("max_assets must be between 1 and 50")
        if not 1 <= self.max_fields <= 1_000:
            raise ValueError("max_fields must be between 1 and 1000")
        if not 1 <= self.page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")


@dataclass(frozen=True, slots=True)
class CandidateGenerationReport:
    """Ranked candidates plus transparent retrieval bounds."""

    concept: CanonicalField
    catalog_source: str
    evidence_source: str
    asset_query: str
    assets_considered: int
    fields_scanned: int
    fields_blocked_in: int
    retrieval_truncated: bool
    candidates: tuple[SemanticCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept.model_dump(mode="json"),
            "catalog_source": self.catalog_source,
            "evidence_source": self.evidence_source,
            "retrieval": {
                "asset_query": self.asset_query,
                "assets_considered": self.assets_considered,
                "fields_scanned": self.fields_scanned,
                "fields_blocked_in": self.fields_blocked_in,
                "truncated": self.retrieval_truncated,
            },
            "candidates": [candidate.model_dump(mode="json") for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class GenerateSemanticCandidates:
    """Retrieve a bounded field set and rank it without granting approval."""

    catalog: CatalogReadPort
    evidence: CandidateEvidencePort
    evidence_source: str
    scoring: CandidateScoringConfig = field(default_factory=CandidateScoringConfig)
    limits: CandidateRetrievalLimits = field(default_factory=CandidateRetrievalLimits)
    description_interpreter: DescriptionInterpreterPort | None = None

    def execute(self, concept: CanonicalField) -> CandidateGenerationReport:
        asset_query = concept.id.root.partition(".")[0]
        assets, asset_truncated = self._retrieve_assets(asset_query)
        fields, field_truncated, fields_scanned = self._retrieve_fields(assets)
        blocked = tuple(field for field in fields if candidate_is_blocked_in(concept, field))
        evidence_by_field = self._evidence_by_field(concept, blocked)
        candidates = tuple(
            score_semantic_candidate(
                concept,
                field,
                evidence_by_field.get(field.id.root),
                self.scoring,
            )
            for field in blocked
        )
        ranked = rank_semantic_candidates(candidates)
        if self.description_interpreter is not None and ranked:
            ranked = self._attach_interpretations(concept, blocked, ranked)
        return CandidateGenerationReport(
            concept=concept,
            catalog_source=self.catalog.source_label,
            evidence_source=self.evidence_source,
            asset_query=asset_query,
            assets_considered=len(assets),
            fields_scanned=fields_scanned,
            fields_blocked_in=len(blocked),
            retrieval_truncated=asset_truncated or field_truncated,
            candidates=ranked,
        )

    def _retrieve_assets(self, query: str) -> tuple[tuple[CatalogAsset, ...], bool]:
        assets: list[CatalogAsset] = []
        cursor: str | None = None
        truncated = False
        while len(assets) < self.limits.max_assets:
            size = min(self.limits.page_size, self.limits.max_assets - len(assets))
            page = self.catalog.search_assets(query, PageRequest(size=size, cursor=cursor))
            assets.extend(page.items)
            cursor = page.next_cursor
            truncated = truncated or page.partial
            if cursor is None:
                break
        if cursor is not None:
            truncated = True
        return tuple(assets), truncated

    def _retrieve_fields(
        self,
        assets: tuple[CatalogAsset, ...],
    ) -> tuple[tuple[CandidateFieldMetadata, ...], bool, int]:
        fields: list[CandidateFieldMetadata] = []
        truncated = False
        scanned = 0
        for asset in assets:
            cursor: str | None = None
            while len(fields) < self.limits.max_fields:
                size = min(self.limits.page_size, self.limits.max_fields - len(fields))
                page = self.catalog.list_schema_fields(
                    asset.dataset,
                    PageRequest(size=size, cursor=cursor),
                )
                scanned += len(page.items)
                fields.extend(
                    CandidateFieldMetadata(
                        id=field.id,
                        native_type=field.native_type,
                        description=field.description,
                        glossary_terms=field.glossary_terms,
                        is_part_of_key=field.is_part_of_key,
                    )
                    for field in page.items
                )
                cursor = page.next_cursor
                truncated = truncated or page.partial
                if cursor is None:
                    break
            if len(fields) >= self.limits.max_fields:
                if cursor is not None or asset != assets[-1]:
                    truncated = True
                break
        return tuple(fields), truncated, scanned

    def _evidence_by_field(
        self,
        concept: CanonicalField,
        fields: tuple[CandidateFieldMetadata, ...],
    ) -> dict[str, ExternalCandidateEvidence]:
        requested = tuple(field.id for field in fields)
        returned = self.evidence.evidence_for(concept.id, requested)
        requested_roots = {field.root for field in requested}
        result: dict[str, ExternalCandidateEvidence] = {}
        for item in returned:
            root = item.physical_field.root
            if item.logical_field != concept.id or root not in requested_roots:
                raise CandidateGenerationError(
                    "candidate evidence returned an unrequested identity"
                )
            if root in result:
                raise CandidateGenerationError("candidate evidence returned a duplicate field")
            result[root] = item
        return result

    def _attach_interpretations(
        self,
        concept: CanonicalField,
        fields: tuple[CandidateFieldMetadata, ...],
        ranked: tuple[SemanticCandidate, ...],
    ) -> tuple[SemanticCandidate, ...]:
        assert self.description_interpreter is not None
        returned = self.description_interpreter.interpret(concept, fields)
        allowed = {candidate.physical_field.root for candidate in ranked}
        interpretations: dict[str, DescriptionInterpretation] = {}
        for item in returned:
            root = item.physical_field.root
            if root not in allowed:
                raise CandidateGenerationError(
                    "description interpreter returned an unrequested field"
                )
            if root in interpretations:
                raise CandidateGenerationError("description interpreter returned a duplicate field")
            interpretations[root] = item
        return tuple(
            SemanticCandidate.model_validate(
                {
                    **candidate.model_dump(mode="json"),
                    "semantic_explanation": (
                        interpretations[candidate.physical_field.root].model_dump(mode="json")
                        if candidate.physical_field.root in interpretations
                        else None
                    ),
                }
            )
            for candidate in ranked
        )


@dataclass(frozen=True, slots=True)
class EvaluateSemanticCandidates:
    """Evaluate the same pure scorer against a plainly labeled synthetic fixture."""

    dataset: CandidateEvaluationDatasetPort
    scoring: CandidateScoringConfig = field(default_factory=CandidateScoringConfig)

    def execute(self) -> CandidateEvaluationMetrics:
        return evaluate_candidate_fixture(self.dataset.load(), self.scoring)
