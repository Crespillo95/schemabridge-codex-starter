"""Focused adapter contracts for M35 PostgreSQL registry changes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.test_registry_change_authoring import (
    REQUEST_KEY,
    _Harness,
    _principal,
)

from schemabridge.adapters.storage.postgres_registry_changes import (
    PostgresRegistryChangeStore,
    PostgresRegistryJoinProfileRequestQueue,
    _authoring_from_row,
    _draft_from_row,
)
from schemabridge.domain.identity import IdentityRole
from schemabridge.domain.semantic_profile_jobs import (
    semantic_join_profile_proposal_fingerprint,
)


def test_registry_change_adapters_redact_their_dsn() -> None:
    dsn = "postgresql://schemabridge_api:do-not-render@localhost/control"

    assert dsn not in repr(PostgresRegistryChangeStore(dsn))
    assert dsn not in repr(PostgresRegistryJoinProfileRequestQueue(dsn))


def test_authoring_row_requires_denormalized_columns_to_match_payload() -> None:
    harness = _Harness.create()
    authoring = (
        harness.request_use_case()
        .execute(
            _principal("analyst-owner", IdentityRole.ANALYST),
            harness.request_input,
            idempotency_key=REQUEST_KEY,
        )
        .authoring
    )
    request = authoring.request
    base = request.base_evidence.base_registry
    row = (
        authoring.workspace_id,
        authoring.id,
        request.scan_id,
        request.scope.catalog_scope,
        request.scope.registry_id,
        base.registry_version,
        base.registry_fingerprint,
        request.proposal.connection_id.root,
        semantic_join_profile_proposal_fingerprint(request.proposal),
        request.execution_target.target_fingerprint,
        request.fingerprint,
        authoring.fingerprint,
        authoring.owner_actor_id,
        authoring.model_dump(mode="json"),
        authoring.created_at,
    )

    assert _authoring_from_row(row) == authoring
    tampered = (*row[:3], "foreign.catalog", *row[4:])
    with pytest.raises(ValueError, match="disagrees"):
        _authoring_from_row(tampered)


def test_draft_row_requires_denormalized_columns_to_match_payload() -> None:
    harness = _Harness.create()
    owner = _principal("analyst-owner", IdentityRole.ANALYST)
    authoring = (
        harness.request_use_case()
        .execute(
            owner,
            harness.request_input,
            idempotency_key=REQUEST_KEY,
        )
        .authoring
    )
    harness.queue.complete_current()
    harness.clock.current = authoring.created_at + timedelta(minutes=2)
    draft = (
        harness.finalize_use_case()
        .execute(
            owner,
            authoring.id,
            confirmed_authoring_fingerprint=authoring.fingerprint,
            idempotency_key="registry-change-finalize-row-test",
        )
        .draft
    )
    row = (
        draft.workspace_id,
        draft.id,
        "add_join_v1",
        draft.scope.catalog_scope,
        draft.scope.registry_id,
        draft.owner_actor_id,
        draft.status.value,
        draft.revision,
        draft.fingerprint,
        draft.prepared_proposal_id,
        draft.prepared_proposal_fingerprint,
        draft.model_dump(mode="json"),
        draft.created_at,
        draft.updated_at,
    )

    assert _draft_from_row(row) == draft
    tampered = (*row[:7], draft.revision + 1, *row[8:])
    with pytest.raises(ValueError, match="disagrees"):
        _draft_from_row(tampered)
