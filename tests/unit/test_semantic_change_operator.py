"""Operator read-model tests for semantic-change heads and shared audit verification."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from tests.unit.test_semantic_change import SCOPE, _baseline_flow

from schemabridge.application.semantic_change import (
    SemanticChangeError,
    SemanticChangeErrorCode,
)
from schemabridge.application.semantic_change_operator import (
    LoadSemanticChangeHead,
    VerifySemanticChangeAudit,
)
from schemabridge.domain.registry_control import ControlAuditChainVerification
from schemabridge.domain.semantic_change import (
    SemanticChangeCommit,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass
class _Store:
    head: SemanticChangeCommit | None

    def load_head(self, scope: SemanticRegistryScope) -> SemanticChangeCommit | None:
        del scope
        return self.head


@dataclass
class _Audit:
    value: ControlAuditChainVerification

    def verify_audit_chain(self, workspace_id: str) -> ControlAuditChainVerification:
        del workspace_id
        return self.value


def _commit() -> SemanticChangeCommit:
    _, _, _, decision = _baseline_flow()
    return SemanticChangeCommit(
        scope=SCOPE,
        head_revision=1,
        decision=decision,
        baseline=decision.baseline,
        audit_event_hash=semantic_change_fingerprint({"decision": decision.fingerprint}),
    )


def test_shared_audit_chain_may_contain_more_events_than_semantic_head() -> None:
    commit = _commit()
    chain = ControlAuditChainVerification(
        workspace_id=SCOPE.workspace_id,
        event_count=3,
        head_hash="a" * 64,
        valid=True,
    )

    result = VerifySemanticChangeAudit(
        heads=LoadSemanticChangeHead(_Store(commit)),
        audit=_Audit(chain),
    ).execute(SCOPE)

    assert result.valid is True
    assert result.event_count == 3
    assert result.head_revision == commit.head_revision


def test_audit_fails_closed_when_chain_is_shorter_than_head() -> None:
    commit = _commit()
    chain = ControlAuditChainVerification(
        workspace_id=SCOPE.workspace_id,
        event_count=0,
        head_hash=None,
        valid=True,
    )

    result = VerifySemanticChangeAudit(
        heads=LoadSemanticChangeHead(_Store(commit)),
        audit=_Audit(chain),
    ).execute(SCOPE)

    assert result.valid is False


def test_head_and_audit_reject_cross_scope_responses() -> None:
    commit = _commit()
    other_scope = SCOPE.model_copy(update={"workspace_id": "workspace-b"})
    with pytest.raises(SemanticChangeError) as head_error:
        LoadSemanticChangeHead(_Store(commit)).execute(other_scope)
    assert head_error.value.code is SemanticChangeErrorCode.INVALID_RESPONSE

    chain = ControlAuditChainVerification(
        workspace_id="workspace-b",
        event_count=1,
        head_hash="a" * 64,
        valid=True,
    )
    with pytest.raises(SemanticChangeError) as audit_error:
        VerifySemanticChangeAudit(
            heads=LoadSemanticChangeHead(_Store(commit)),
            audit=_Audit(chain),
        ).execute(SCOPE)
    assert audit_error.value.code is SemanticChangeErrorCode.INVALID_RESPONSE
