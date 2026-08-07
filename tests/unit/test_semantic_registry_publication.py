"""Exact approval and audit binding for immutable semantic-registry publication."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.planning import (
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.application.ports.publication_audit import PublicationAuditStoreError
from schemabridge.application.semantic_registry import (
    PrepareGovernedSemanticRegistryPublicationApproval,
    PublishGovernedSemanticRegistryVersion,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    RegistryPublicationConfirmation,
    RegistryPublicationResult,
    RegistryPublicationStatus,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    registry_publication_approval_id,
    semantic_registry_decision_ids,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "demo/ground_truth/registries/manifest.yml"
NOW = datetime(2026, 7, 23, 9, 30, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-unit-live",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)


class StubRegistryPublisher:
    def __init__(self, results: tuple[RegistryPublicationResult, ...] = ()) -> None:
        self._results = results
        self.calls: list[tuple[GovernedSemanticRegistrySnapshot, RegistryPublicationApproval]] = []

    def publish(
        self,
        registry: GovernedSemanticRegistrySnapshot,
        approval: RegistryPublicationApproval,
    ) -> RegistryPublicationResult:
        self.calls.append((registry, approval))
        try:
            return self._results[len(self.calls) - 1]
        except IndexError as error:
            raise AssertionError("registry publisher must not have been called") from error


class SpyAuditStore:
    def __init__(
        self,
        *,
        fail_append: bool = False,
        fail_append_calls: frozenset[int] = frozenset(),
    ) -> None:
        self.fail_append = fail_append
        self.fail_append_calls = fail_append_calls
        self.append_calls = 0
        self.records: list[PublicationTargetAuditRecord] = []

    def append(self, records: tuple[PublicationTargetAuditRecord, ...]) -> None:
        self.append_calls += 1
        if self.fail_append or self.append_calls in self.fail_append_calls:
            raise PublicationAuditStoreError("sensitive storage detail")
        self.records.extend(records)

    def list_for_approval(
        self,
        approval_id: str,
    ) -> tuple[PublicationTargetAuditRecord, ...]:
        return tuple(record for record in self.records if record.approval_id == approval_id)


def test_publication_requires_explicit_approval_before_any_external_effect() -> None:
    registry = _registry()
    publisher = StubRegistryPublisher()
    audit_store = SpyAuditStore()

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            None,  # type: ignore[arg-type]
        )

    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_REQUIRED
    assert str(raised.value) == "explicit registry publication approval is required"
    assert publisher.calls == []
    assert audit_store.append_calls == 0


def test_approval_preparation_reuses_the_durable_identity_on_exact_retry() -> None:
    registry = _registry()
    audit_store = SpyAuditStore()
    preparer = PrepareGovernedSemanticRegistryPublicationApproval(audit_store)

    first = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW,
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    audit_store.records.append(_audit(registry, first, RegistryPublicationStatus.PUBLISHED))
    replay = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW + timedelta(minutes=5),
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )

    assert replay.id == first.id
    assert replay.approved_at == first.approved_at == NOW
    assert replay.payload_fingerprint == first.payload_fingerprint


@pytest.mark.parametrize(
    ("scope", "confirmed_fingerprint"),
    (
        pytest.param(
            SCOPE.model_copy(update={"catalog_scope": "another-catalog"}),
            None,
            id="catalog-scope",
        ),
        pytest.param(
            SCOPE.model_copy(update={"registry_id": "another_registry"}),
            None,
            id="registry-id",
        ),
        pytest.param(SCOPE, "0" * 64, id="confirmed-fingerprint"),
    ),
)
def test_approval_preparation_rejects_scope_or_fingerprint_mismatch_without_audit_io(
    scope: SemanticRegistryScope,
    confirmed_fingerprint: str | None,
) -> None:
    registry = _registry()
    audit_store = SpyAuditStore()

    with pytest.raises(RegistryPublicationError) as raised:
        PrepareGovernedSemanticRegistryPublicationApproval(audit_store).execute(
            registry,
            scope,
            actor="publisher-unit",
            approved_at=NOW,
            confirmed_fingerprint=confirmed_fingerprint or registry.fingerprint,
            confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
        )

    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH
    assert audit_store.records == []
    assert audit_store.append_calls == 0


def test_publication_fails_closed_before_publisher_if_reservation_is_not_durable() -> None:
    registry = _registry()
    audit_store = SpyAuditStore(fail_append=True)
    preparer = PrepareGovernedSemanticRegistryPublicationApproval(audit_store)
    approval = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW,
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    publisher = StubRegistryPublisher()

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            approval,
        )

    assert raised.value.code is RegistryPublicationErrorCode.AUDIT_UNAVAILABLE
    assert str(raised.value) == ("registry publication approval could not be durably reserved")
    assert publisher.calls == []


def test_approval_preparation_rejects_inconsistent_durable_history() -> None:
    registry = _registry()
    audit_store = SpyAuditStore()
    preparer = PrepareGovernedSemanticRegistryPublicationApproval(audit_store)
    approval = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW,
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    audit_store.records.append(
        _audit(registry, approval, RegistryPublicationStatus.PUBLISHED).model_copy(
            update={"family": PublicationFamily.RECIPE}
        )
    )

    with pytest.raises(RegistryPublicationError) as raised:
        preparer.execute(
            registry,
            SCOPE,
            actor="publisher-unit",
            approved_at=NOW + timedelta(minutes=5),
            confirmed_fingerprint=registry.fingerprint,
            confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
        )

    assert raised.value.code is RegistryPublicationErrorCode.AUDIT_UNAVAILABLE
    assert str(raised.value) == "registry publication approval history is invalid"


def test_reserved_approval_survives_post_write_audit_failure_for_exact_retry() -> None:
    registry = _registry()
    audit_store = SpyAuditStore(fail_append_calls=frozenset({2}))
    preparer = PrepareGovernedSemanticRegistryPublicationApproval(audit_store)
    approval = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW,
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    first = _result(registry, approval, RegistryPublicationStatus.PUBLISHED)
    replay = _result(registry, approval, RegistryPublicationStatus.ALREADY_CURRENT)
    publisher = StubRegistryPublisher((first, replay))
    use_case = PublishGovernedSemanticRegistryVersion(publisher, audit_store)

    with pytest.raises(RegistryPublicationError) as raised:
        use_case.execute(registry, approval)
    assert raised.value.code is RegistryPublicationErrorCode.AUDIT_UNAVAILABLE

    recovered = preparer.execute(
        registry,
        SCOPE,
        actor="publisher-unit",
        approved_at=NOW + timedelta(minutes=5),
        confirmed_fingerprint=registry.fingerprint,
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )
    assert recovered == approval
    assert use_case.execute(registry, recovered) == replay
    assert [record.outcome for record in audit_store.records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED,
        PublicationAuditOutcome.ALREADY_CURRENT,
    ]


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(
            lambda approval, _registry: approval.model_copy(
                update={"payload_fingerprint": "0" * 64}
            ),
            id="payload-fingerprint",
        ),
        pytest.param(
            lambda approval, _registry: approval.model_copy(
                update={"target": "urn:li:document:schemabridge-semantic-registry-forged"}
            ),
            id="target",
        ),
        pytest.param(
            lambda approval, _registry: _approval_for_workspace(
                _registry,
                approval,
                "workspace-another-live",
            ),
            id="workspace-with-matching-forged-target",
        ),
        pytest.param(
            lambda approval, _registry: approval.model_copy(
                update={"decision_ids": approval.decision_ids[:-1]}
            ),
            id="decision-closure",
        ),
        pytest.param(
            lambda approval, _registry: approval.model_copy(
                update={"confirmation": "not-approved"}
            ),
            id="forged-confirmation",
        ),
        pytest.param(
            lambda approval, _registry: approval.model_copy(update={"id": "arbitrary-approval-id"}),
            id="approval-id",
        ),
    ),
)
def test_approval_mismatch_stops_before_publisher_and_audit(
    mutate: Callable[
        [RegistryPublicationApproval, GovernedSemanticRegistrySnapshot],
        RegistryPublicationApproval,
    ],
) -> None:
    registry = _registry()
    approval = mutate(_approval(registry), registry)
    publisher = StubRegistryPublisher()
    audit_store = SpyAuditStore()

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            approval,
        )

    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH
    assert str(raised.value) == ("registry publication approval does not match the exact payload")
    assert publisher.calls == []
    assert audit_store.append_calls == 0


def test_exact_workspace_target_and_successful_audit_are_preserved() -> None:
    registry = _registry()
    approval = _approval(registry)
    result = _result(registry, approval, RegistryPublicationStatus.PUBLISHED)
    publisher = StubRegistryPublisher((result,))
    audit_store = SpyAuditStore()

    published = PublishGovernedSemanticRegistryVersion(
        publisher,
        audit_store,
    ).execute(registry, approval)

    assert approval.workspace_id == SCOPE.workspace_id
    assert approval.target == datahub_registry_document_urn(SCOPE, registry.version)
    assert published == result
    assert publisher.calls == [(registry, approval)]
    assert audit_store.append_calls == 2
    assert [record.outcome for record in audit_store.records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED,
        PublicationAuditOutcome.SUCCEEDED,
    ]
    assert audit_store.records[1] == result.audit_record
    assert result.audit_record.family is PublicationFamily.REGISTRY
    assert result.audit_record.decision_ids == semantic_registry_decision_ids(registry)


@pytest.mark.parametrize(
    ("override", "value"),
    (
        pytest.param("workspace_id", "workspace-forged", id="workspace"),
        pytest.param(
            "target",
            "urn:li:document:schemabridge-semantic-registry-forged",
            id="target",
        ),
        pytest.param("registry_id", "forged_registry", id="registry"),
        pytest.param("registry_version", 2, id="version"),
        pytest.param("fingerprint", "f" * 64, id="fingerprint"),
        pytest.param("approval_id", "approval-forged", id="approval"),
    ),
)
def test_publisher_result_for_another_payload_is_rejected_before_audit(
    override: str,
    value: str | int,
) -> None:
    registry = _registry()
    approval = _approval(registry)
    forged = _result(
        registry,
        approval,
        RegistryPublicationStatus.PUBLISHED,
        **{override: value},
    )
    publisher = StubRegistryPublisher((forged,))
    audit_store = SpyAuditStore()

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            approval,
        )

    assert raised.value.code is RegistryPublicationErrorCode.INVALID_RESPONSE
    assert str(raised.value) == ("registry publisher returned a result for another payload")
    assert publisher.calls == [(registry, approval)]
    assert audit_store.append_calls == 1
    assert audit_store.records[0].outcome is PublicationAuditOutcome.NOT_ATTEMPTED


@pytest.mark.parametrize(
    "audit_mutation",
    (
        pytest.param({"actor": "actor-forged"}, id="actor"),
        pytest.param({"approved_at": NOW + timedelta(seconds=1)}, id="approval-time"),
        pytest.param({"decision_ids": ("decision-forged",)}, id="decision-closure"),
    ),
)
def test_forged_audit_binding_is_rejected_without_ledger_append(
    audit_mutation: dict[str, object],
) -> None:
    registry = _registry()
    approval = _approval(registry)
    forged_audit = _audit(
        registry,
        approval,
        RegistryPublicationStatus.PUBLISHED,
    ).model_copy(update=audit_mutation)
    result = RegistryPublicationResult(
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        registry_id=registry.registry_id,
        registry_version=registry.version,
        fingerprint=registry.fingerprint,
        target=approval.target,
        status=RegistryPublicationStatus.PUBLISHED,
        audit_record=forged_audit,
    )
    publisher = StubRegistryPublisher((result,))
    audit_store = SpyAuditStore()

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            approval,
        )

    assert raised.value.code is RegistryPublicationErrorCode.INVALID_RESPONSE
    assert str(raised.value) == ("registry publisher returned an audit for another approval")
    assert publisher.calls == [(registry, approval)]
    assert audit_store.append_calls == 1
    assert audit_store.records[0].outcome is PublicationAuditOutcome.NOT_ATTEMPTED


def test_ledger_failure_is_sanitized_after_one_publication_attempt() -> None:
    registry = _registry()
    approval = _approval(registry)
    result = _result(registry, approval, RegistryPublicationStatus.PUBLISHED)
    publisher = StubRegistryPublisher((result,))
    audit_store = SpyAuditStore(fail_append_calls=frozenset({2}))

    with pytest.raises(RegistryPublicationError) as raised:
        PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
            registry,
            approval,
        )

    assert raised.value.code is RegistryPublicationErrorCode.AUDIT_UNAVAILABLE
    assert str(raised.value) == ("registry publication audit could not be durably recorded")
    assert "sensitive" not in str(raised.value)
    assert publisher.calls == [(registry, approval)]
    assert audit_store.append_calls == 2
    assert [record.outcome for record in audit_store.records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED
    ]


def test_idempotent_replay_appends_both_bound_audit_facts() -> None:
    registry = _registry()
    approval = _approval(registry)
    first = _result(registry, approval, RegistryPublicationStatus.PUBLISHED)
    replay = _result(registry, approval, RegistryPublicationStatus.ALREADY_CURRENT)
    publisher = StubRegistryPublisher((first, replay))
    audit_store = SpyAuditStore()
    use_case = PublishGovernedSemanticRegistryVersion(publisher, audit_store)

    assert use_case.execute(registry, approval) == first
    assert use_case.execute(registry, approval) == replay

    assert publisher.calls == [(registry, approval), (registry, approval)]
    assert audit_store.append_calls == 3
    assert [record.outcome for record in audit_store.records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED,
        PublicationAuditOutcome.SUCCEEDED,
        PublicationAuditOutcome.ALREADY_CURRENT,
    ]
    assert audit_store.records[2].previous_fingerprint == registry.fingerprint


def test_failed_result_is_bound_and_durably_recorded() -> None:
    registry = _registry()
    approval = _approval(registry)
    failed = _result(
        registry,
        approval,
        RegistryPublicationStatus.FAILED,
        reason_code="post_write_verification_failed",
    )
    publisher = StubRegistryPublisher((failed,))
    audit_store = SpyAuditStore()

    result = PublishGovernedSemanticRegistryVersion(publisher, audit_store).execute(
        registry,
        approval,
    )

    assert result == failed
    assert result.status is RegistryPublicationStatus.FAILED
    assert result.reason_code == "post_write_verification_failed"
    assert result.audit_record.outcome is PublicationAuditOutcome.FAILED
    assert result.audit_record.reason_code == result.reason_code
    assert [record.outcome for record in audit_store.records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED,
        PublicationAuditOutcome.FAILED,
    ]
    assert audit_store.records[1] == result.audit_record


def _registry() -> GovernedSemanticRegistrySnapshot:
    recorded = RecordedGovernedSemanticRegistry(MANIFEST_PATH, SCOPE).load().registry
    assert len(recorded.logical_context.models) == 7
    assert len(recorded.mapping_set.mappings) == 31
    assert len(recorded.join_contracts.contracts) == 5
    return prepare_datahub_registry_version(recorded, SCOPE)


def _approval(
    registry: GovernedSemanticRegistrySnapshot,
) -> RegistryPublicationApproval:
    return RegistryPublicationApproval(
        id=registry_publication_approval_id(registry, SCOPE, "publisher-unit"),
        workspace_id=SCOPE.workspace_id,
        registry_id=registry.registry_id,
        registry_version=registry.version,
        catalog_scope=registry.catalog_scope,
        payload_fingerprint=registry.fingerprint,
        target=datahub_registry_document_urn(SCOPE, registry.version),
        actor="publisher-unit",
        approved_at=NOW,
        decision_ids=semantic_registry_decision_ids(registry),
        confirmation=(RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION),
    )


def _approval_for_workspace(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    workspace_id: str,
) -> RegistryPublicationApproval:
    scope = SCOPE.model_copy(update={"workspace_id": workspace_id})
    return approval.model_copy(
        update={
            "workspace_id": workspace_id,
            "target": datahub_registry_document_urn(scope, registry.version),
        }
    )


def _result(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    status: RegistryPublicationStatus,
    *,
    reason_code: str | None = None,
    **overrides: object,
) -> RegistryPublicationResult:
    approval_id = str(overrides.get("approval_id", approval.id))
    target = str(overrides.get("target", approval.target))
    fingerprint = str(overrides.get("fingerprint", registry.fingerprint))
    return RegistryPublicationResult(
        approval_id=approval_id,
        workspace_id=str(overrides.get("workspace_id", approval.workspace_id)),
        registry_id=str(overrides.get("registry_id", registry.registry_id)),
        registry_version=int(overrides.get("registry_version", registry.version)),
        fingerprint=fingerprint,
        target=target,
        status=status,
        reason_code=reason_code,
        audit_record=_audit(
            registry,
            approval,
            status,
            approval_id=approval_id,
            target=target,
            fingerprint=fingerprint,
            reason_code=reason_code,
        ),
    )


def _audit(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
    status: RegistryPublicationStatus,
    *,
    approval_id: str | None = None,
    target: str | None = None,
    fingerprint: str | None = None,
    reason_code: str | None = None,
) -> PublicationTargetAuditRecord:
    resolved_fingerprint = fingerprint or registry.fingerprint
    return PublicationTargetAuditRecord(
        family=PublicationFamily.REGISTRY,
        operation="versioned_document",
        target=target or approval.target,
        approval_id=approval_id or approval.id,
        actor=approval.actor,
        approved_at=approval.approved_at,
        previous_fingerprint=(
            resolved_fingerprint if status is RegistryPublicationStatus.ALREADY_CURRENT else None
        ),
        new_fingerprint=resolved_fingerprint,
        outcome={
            RegistryPublicationStatus.PUBLISHED: PublicationAuditOutcome.SUCCEEDED,
            RegistryPublicationStatus.ALREADY_CURRENT: (PublicationAuditOutcome.ALREADY_CURRENT),
            RegistryPublicationStatus.FAILED: PublicationAuditOutcome.FAILED,
        }[status],
        decision_ids=semantic_registry_decision_ids(registry),
        reason_code=reason_code,
    )
