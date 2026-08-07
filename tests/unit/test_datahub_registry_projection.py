"""Bounded DataHub active-pointer projection tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryIdentity,
    DataHubRegistryWriteConfig,
)
from schemabridge.adapters.semantic_registry.datahub_projection import (
    DataHubRegistryProjectionAdapter,
    datahub_active_registry_projection_urn,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryProjectionState,
    RegistryReconciliationApproval,
    RegistryReconciliationConfirmation,
    registry_projection_fingerprint,
    registry_reconciliation_approval_id_from_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

NOW = datetime(2026, 7, 23, 14, 0, tzinfo=UTC)
ACTOR = "urn:li:corpuser:schemabridge-registry-writer"
SCOPE = SemanticRegistryScope(
    workspace_id="projection-workspace",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)


@dataclass(slots=True)
class ProjectionClient:
    document: DataHubRegistryDocument | None = None
    identity_value: DataHubRegistryIdentity = field(
        default_factory=lambda: DataHubRegistryIdentity(
            actor_urn=ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments"}),
            granted_target_edit_privileges=frozenset(),
        )
    )
    identity_calls: list[str] = field(default_factory=list)
    document_calls: list[str] = field(default_factory=list)
    writes: list[DataHubRegistryDocumentWrite] = field(default_factory=list)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        self.identity_calls.append(target_urn)
        return self.identity_value

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        self.document_calls.append(urn)
        return self.document

    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        self.writes.append(document)
        self.document = DataHubRegistryDocument(
            urn=f"urn:li:document:{document.document_id}",
            title=document.title,
            text=document.text,
            custom_properties=document.custom_properties,
            related_asset_urns=document.related_asset_urns,
            removed=False,
        )


def _projection(generation: int, *, version: int | None = None) -> RegistryProjectionState:
    resolved_version = version or generation + 1
    pointer = ActiveRegistryPointer(
        scope=SCOPE,
        generation=generation,
        registry_version=resolved_version,
        registry_fingerprint=f"{resolved_version:064x}",
        registry_target=datahub_registry_document_urn(SCOPE, resolved_version),
        transition_id=f"registry-transition-v1-{generation:064x}",
        activated_by="registry-operator",
        activated_at=NOW,
        decision_ids=("decision-a",),
    )
    return RegistryProjectionState(
        pointer=pointer,
        projection_fingerprint=registry_projection_fingerprint(pointer),
    )


def _approval(desired: RegistryProjectionState) -> RegistryReconciliationApproval:
    report_fingerprint = "a" * 64
    actor = "reconciliation-operator"
    confirmation = RegistryReconciliationConfirmation.REPAIR_ACTIVE_REGISTRY_PROJECTION
    return RegistryReconciliationApproval(
        id=registry_reconciliation_approval_id_from_fingerprint(
            report_fingerprint,
            actor,
            NOW,
            confirmation,
        ),
        report_fingerprint=report_fingerprint,
        scope=desired.pointer.scope,
        generation=desired.pointer.generation,
        registry_fingerprint=desired.pointer.registry_fingerprint,
        actor=actor,
        approved_at=NOW,
        confirmation=confirmation,
    )


def _adapter(client: ProjectionClient) -> DataHubRegistryProjectionAdapter:
    return DataHubRegistryProjectionAdapter(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-unit-token",
            actor_urn=ACTOR,
        ),
        client=client,
    )


def test_projection_is_exact_read_back_and_idempotent() -> None:
    client = ProjectionClient()
    adapter = _adapter(client)
    desired = _projection(1)
    approval = _approval(desired)

    assert adapter.read(SCOPE) is None
    first = adapter.project(desired, approval)
    replay = adapter.project(desired, approval)

    assert first == desired
    assert replay == desired
    assert adapter.read(SCOPE) == desired
    assert len(client.writes) == 1
    assert client.identity_calls == [
        datahub_active_registry_projection_urn(SCOPE),
        datahub_active_registry_projection_urn(SCOPE),
    ]


def test_older_generation_never_overwrites_a_newer_projection() -> None:
    client = ProjectionClient()
    adapter = _adapter(client)
    newer = _projection(3)
    adapter.project(newer, _approval(newer))
    writes_before = tuple(client.writes)
    older = _projection(2)

    with pytest.raises(RegistryControlError) as raised:
        adapter.project(older, _approval(older))

    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
    assert tuple(client.writes) == writes_before
    assert adapter.read(SCOPE) == newer


def test_projection_rejects_mismatched_approval_before_datahub_io() -> None:
    client = ProjectionClient()
    adapter = _adapter(client)
    desired = _projection(1)
    approval = _approval(desired).model_copy(update={"generation": 2})

    with pytest.raises(RegistryControlError) as raised:
        adapter.project(desired, approval)

    assert raised.value.code is RegistryControlErrorCode.APPROVAL_MISMATCH
    assert client.identity_calls == []
    assert client.document_calls == []
    assert client.writes == []


def test_reconciliation_approval_id_must_match_its_immutable_facts() -> None:
    approval = _approval(_projection(1))
    payload = approval.model_dump(mode="python")
    payload["actor"] = "another-reconciliation-operator"

    with pytest.raises(ValueError, match="approval id does not match its facts"):
        RegistryReconciliationApproval.model_validate(payload)


def test_projection_rejects_tampered_pointer_properties() -> None:
    client = ProjectionClient()
    adapter = _adapter(client)
    desired = _projection(1)
    adapter.project(desired, _approval(desired))
    assert client.document is not None
    client.document = DataHubRegistryDocument(
        urn=client.document.urn,
        title=client.document.title,
        text=client.document.text,
        custom_properties={
            **client.document.custom_properties,
            "schemabridge.registryActivationGeneration": "999",
        },
        related_asset_urns=(),
        removed=False,
    )

    with pytest.raises(RegistryControlError) as raised:
        adapter.read(SCOPE)

    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT


def test_projection_rejects_persisted_approval_for_another_pointer() -> None:
    client = ProjectionClient()
    adapter = _adapter(client)
    desired = _projection(1)
    adapter.project(desired, _approval(desired))
    assert client.document is not None
    approval = json.loads(
        client.document.custom_properties["schemabridge.registryReconciliationApproval"]
    )
    approval["generation"] = desired.pointer.generation + 1
    client.document = DataHubRegistryDocument(
        urn=client.document.urn,
        title=client.document.title,
        text=client.document.text,
        custom_properties={
            **client.document.custom_properties,
            "schemabridge.registryReconciliationApproval": json.dumps(
                approval,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        related_asset_urns=(),
        removed=False,
    )

    with pytest.raises(RegistryControlError) as raised:
        adapter.read(SCOPE)

    assert raised.value.code is RegistryControlErrorCode.RECONCILIATION_CONFLICT
