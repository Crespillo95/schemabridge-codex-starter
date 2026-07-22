"""Synthetic recorded and in-memory candidate-evidence adapters."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from schemabridge.application.candidate_engine import CandidateGenerationError
from schemabridge.domain.candidates import ExternalCandidateEvidence
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef

_EVIDENCE_LIST = TypeAdapter(tuple[ExternalCandidateEvidence, ...])
_FIXTURE_KIND = "synthetic_candidate_evidence"
_FORBIDDEN_KEYS = frozenset({"password", "secret", "token"})


class FakeCandidateEvidenceAdapter:
    """Return only configured evidence requested by the use case."""

    def __init__(self, entries: tuple[ExternalCandidateEvidence, ...]) -> None:
        self._entries = entries

    def evidence_for(
        self,
        logical_field: LogicalFieldRef,
        physical_fields: tuple[PhysicalFieldRef, ...],
    ) -> tuple[ExternalCandidateEvidence, ...]:
        requested = {field.root for field in physical_fields}
        return tuple(
            entry
            for entry in self._entries
            if entry.logical_field == logical_field and entry.physical_field.root in requested
        )


class EmptyCandidateEvidenceAdapter(FakeCandidateEvidenceAdapter):
    """Explicitly supply no external signals for live catalogs without profiling."""

    def __init__(self) -> None:
        super().__init__(())


class RecordedCandidateEvidenceAdapter(FakeCandidateEvidenceAdapter):
    """Load bounded synthetic signals that are visibly distinct from production evidence."""

    def __init__(self, path: Path) -> None:
        try:
            decoded: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CandidateGenerationError(
                "candidate evidence fixture could not be loaded"
            ) from error
        if not isinstance(decoded, dict) or decoded.get("fixture_kind") != _FIXTURE_KIND:
            raise CandidateGenerationError("candidate evidence fixture kind is unsupported")
        if _contains_forbidden_key(decoded):
            raise CandidateGenerationError(
                "candidate evidence fixture contains a forbidden secret-shaped key"
            )
        try:
            entries = _EVIDENCE_LIST.validate_python(decoded.get("entries"))
        except ValidationError as error:
            raise CandidateGenerationError("candidate evidence fixture is invalid") from error
        super().__init__(entries)


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _FORBIDDEN_KEYS or _contains_forbidden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False
