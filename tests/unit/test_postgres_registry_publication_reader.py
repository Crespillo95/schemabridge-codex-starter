"""Exact proposal-reader contracts for M35 Phase-B publication."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol, cast

import pytest
from tests.unit.test_registry_change_authoring import _principal
from tests.unit.test_registry_model_change_authoring import (
    CREATE_KEY,
    DECIDE_KEY,
    PREPARE_KEY,
    _Harness,
)

from schemabridge.adapters.control_plane.postgres_registry_publication import (
    PostgresRegistryPublicationProposalReader,
)
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationStoreError,
    RegistryPublicationStoreErrorCode,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.registry_model_change_authoring import (
    RegistryModelChangeDraft,
)
from schemabridge.domain.registry_model_changes import (
    PreparedRegistryModelReplacementProposal,
)


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass
class _Cursor:
    row: Sequence[object] | None

    def fetchone(self) -> Sequence[object] | None:
        return self.row


@dataclass
class _Connection:
    rows: list[Sequence[object] | None]
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        text = query if isinstance(query, str) else cast(_Composable, query).as_string()
        self.statements.append((" ".join(text.split()), params))
        return _Cursor(self.rows.pop(0))


@dataclass
class _Provider:
    value: _Connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.value


def _prepared() -> tuple[RegistryModelChangeDraft, PreparedRegistryModelReplacementProposal]:
    harness = _Harness.create()
    owner = _principal(
        "analyst-outer",
        IdentityRole.ANALYST,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    created = harness.create_use_case().execute(
        owner,
        harness.request,
        idempotency_key=CREATE_KEY,
    )
    harness.clock.current += timedelta(minutes=1)
    steward = _principal(
        "steward-outer",
        IdentityRole.STEWARD,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    decided = harness.decide_use_case().execute(
        steward,
        created.draft.id,
        action=DecisionAction.APPROVE,
        expected_revision=created.draft.revision,
        confirmed_draft_fingerprint=created.draft.fingerprint,
        rationale="The complete replacement and every incident join were reviewed.",
        idempotency_key=DECIDE_KEY,
    )
    harness.clock.current += timedelta(minutes=1)
    publisher = _principal(
        "publisher-outer",
        IdentityRole.PUBLISHER,
        authenticated_at=harness.clock.current - timedelta(minutes=1),
    )
    prepared = harness.prepare_use_case().execute(
        publisher,
        decided.draft.id,
        expected_revision=decided.draft.revision,
        confirmed_draft_fingerprint=decided.draft.fingerprint,
        idempotency_key=PREPARE_KEY,
    )
    assert prepared.proposal is not None
    return prepared.draft, prepared.proposal


def _rows(
    draft: RegistryModelChangeDraft,
    proposal: PreparedRegistryModelReplacementProposal,
) -> list[Sequence[object]]:
    source = proposal.replacement
    return [
        (proposal.proposal_kind, proposal.fingerprint, proposal.target_registry_version),
        (
            proposal.model_dump(mode="json"),
            proposal.fingerprint,
            proposal.target_registry_version,
            draft.status.value,
            draft.prepared_proposal_id,
            draft.prepared_proposal_fingerprint,
            draft.model_dump(mode="json"),
            proposal.draft_fingerprint,
            proposal.source_replacement_proposal_id,
            proposal.source_replacement_proposal_fingerprint,
            source.model_dump(mode="json"),
            source.fingerprint,
            source.target_registry_version,
            "ready_for_publication",
            source.id,
            source.fingerprint,
        ),
    ]


def test_reader_loads_only_the_exact_outer_and_inner_ready_proposals() -> None:
    draft, proposal = _prepared()
    connection = _Connection(_rows(draft, proposal))
    reader = PostgresRegistryPublicationProposalReader(
        "postgresql://private-control.invalid/control",
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    assert reader.load_exact(proposal.workspace_id, proposal.id) == proposal
    assert len(connection.statements) == 2
    assert "registry_publication_sources" in connection.statements[0][0]
    assert "semantic_registry_model_change_proposals" in connection.statements[1][0]
    assert "semantic_onboarding_proposals" in connection.statements[1][0]


@pytest.mark.parametrize("index", [3, 5, 10, 14, 15])
def test_reader_rejects_any_draft_or_inner_source_drift(index: int) -> None:
    draft, proposal = _prepared()
    rows = _rows(draft, proposal)
    outer = list(rows[1])
    outer[index] = "tampered"
    connection = _Connection([rows[0], tuple(outer)])
    reader = PostgresRegistryPublicationProposalReader(
        "postgresql://private-control.invalid/control",
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(RegistryPublicationStoreError) as rejected:
        reader.load_exact(proposal.workspace_id, proposal.id)

    assert rejected.value.code is RegistryPublicationStoreErrorCode.INVALID_RESPONSE
