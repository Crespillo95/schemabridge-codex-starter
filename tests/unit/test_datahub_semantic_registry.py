"""Adversarial unit contract for the immutable DataHub semantic registry."""

from __future__ import annotations

import hashlib
import json
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import schemabridge.adapters.semantic_registry.datahub as datahub_module
from schemabridge.adapters.semantic_registry.datahub import (
    DataHubGovernedSemanticRegistry,
    DataHubHttpRegistryReadClient,
    DataHubRegistryDocument,
    DataHubRegistryDocumentWrite,
    DataHubRegistryIdentity,
    DataHubRegistryReadConfig,
    DataHubRegistryWriteConfig,
    DataHubSemanticRegistryPublisher,
)
from schemabridge.adapters.semantic_registry.datahub_control import (
    DataHubRegistryVersionReader,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import RegistryVersionTrust
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    RegistryPublicationApproval,
    RegistryPublicationConfirmation,
    RegistryPublicationStatus,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    registry_publication_approval_id,
    semantic_registry_decision_ids,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "demo/ground_truth/registries/manifest.yml"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
READER_ACTOR = "urn:li:corpuser:schemabridge-registry-reader"
WRITER_ACTOR = "urn:li:corpuser:schemabridge-registry-writer"


@dataclass(slots=True)
class FakeRegistryReadClient:
    """A mutation-free fake for the exact read-client protocol."""

    document: DataHubRegistryDocument | None
    identity_value: DataHubRegistryIdentity = field(
        default_factory=lambda: DataHubRegistryIdentity(
            actor_urn=READER_ACTOR,
            granted_platform_mutation_privileges=frozenset(),
            granted_target_edit_privileges=frozenset(),
        )
    )
    identity_error: Exception | None = None
    document_error: Exception | None = None
    identity_calls: list[str] = field(default_factory=list)
    document_calls: list[str] = field(default_factory=list)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        self.identity_calls.append(target_urn)
        if self.identity_error is not None:
            raise self.identity_error
        return self.identity_value

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        self.document_calls.append(urn)
        if self.document_error is not None:
            raise self.document_error
        return self.document


@dataclass(slots=True)
class FakeRegistryWriteClient:
    """An independently typed fake for the write-client protocol."""

    document: DataHubRegistryDocument | None = None
    identity_value: DataHubRegistryIdentity = field(
        default_factory=lambda: DataHubRegistryIdentity(
            actor_urn=WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments"}),
            granted_target_edit_privileges=frozenset(),
        )
    )
    identity_error: Exception | None = None
    document_error: Exception | None = None
    upsert_error: Exception | None = None
    after_upsert: Callable[[DataHubRegistryDocument], DataHubRegistryDocument] | None = None
    identity_calls: list[str] = field(default_factory=list)
    document_calls: list[str] = field(default_factory=list)
    upserts: list[DataHubRegistryDocumentWrite] = field(default_factory=list)

    def identity(self, target_urn: str) -> DataHubRegistryIdentity:
        self.identity_calls.append(target_urn)
        if self.identity_error is not None:
            raise self.identity_error
        return self.identity_value

    def get_document(self, urn: str) -> DataHubRegistryDocument | None:
        self.document_calls.append(urn)
        if self.document_error is not None:
            raise self.document_error
        return self.document

    def upsert_document(self, document: DataHubRegistryDocumentWrite) -> None:
        self.upserts.append(document)
        if self.upsert_error is not None:
            raise self.upsert_error
        stored = DataHubRegistryDocument(
            urn=f"urn:li:document:{document.document_id}",
            title=document.title,
            text=document.text,
            custom_properties=dict(document.custom_properties),
            related_asset_urns=document.related_asset_urns,
            removed=False,
        )
        self.document = self.after_upsert(stored) if self.after_upsert is not None else stored


@pytest.fixture(scope="module")
def scope() -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id="ws_unit_tenant_a_7db801",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )


@pytest.fixture(scope="module")
def live_registry(scope: SemanticRegistryScope) -> GovernedSemanticRegistrySnapshot:
    recorded = RecordedGovernedSemanticRegistry(MANIFEST_PATH, scope).load().registry
    return prepare_datahub_registry_version(recorded, scope)


@pytest.fixture
def approval(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
) -> RegistryPublicationApproval:
    return _approval(live_registry, scope)


def _approval(
    registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    *,
    actor: str = "publisher_unit_actor",
) -> RegistryPublicationApproval:
    return RegistryPublicationApproval(
        id=registry_publication_approval_id(registry, scope, actor),
        workspace_id=scope.workspace_id,
        registry_id=registry.registry_id,
        registry_version=registry.version,
        catalog_scope=registry.catalog_scope,
        payload_fingerprint=registry.fingerprint,
        target=datahub_registry_document_urn(scope, registry.version),
        actor=actor,
        approved_at=NOW,
        decision_ids=semantic_registry_decision_ids(registry),
        confirmation=RegistryPublicationConfirmation.PUBLISH_APPROVED_REGISTRY_VERSION,
    )


def _publisher(client: FakeRegistryWriteClient) -> DataHubSemanticRegistryPublisher:
    return DataHubSemanticRegistryPublisher(
        config=DataHubRegistryWriteConfig(
            server="http://127.0.0.1:1",
            token="synthetic-unit-token",
            actor_urn=WRITER_ACTOR,
        ),
        client=client,
    )


def _reader(
    client: FakeRegistryReadClient,
    scope: SemanticRegistryScope,
    *,
    version: int = 1,
) -> DataHubGovernedSemanticRegistry:
    return DataHubGovernedSemanticRegistry(
        config=DataHubRegistryReadConfig(
            server="http://127.0.0.1:1",
            token="synthetic-unit-token",
        ),
        _scope=scope,
        version=version,
        client=client,
    )


def _published_document(
    registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> DataHubRegistryDocument:
    client = FakeRegistryWriteClient()
    result = _publisher(client).publish(registry, approval)
    assert result.status is RegistryPublicationStatus.PUBLISHED
    assert client.document is not None
    return client.document


def _properties(
    document: DataHubRegistryDocument,
    **changes: object,
) -> dict[str, object]:
    properties: dict[str, object] = dict(document.custom_properties)
    properties.update(changes)
    return properties


def _document_with_properties(
    document: DataHubRegistryDocument,
    properties: Mapping[str, object],
) -> DataHubRegistryDocument:
    return replace(
        document,
        custom_properties=cast(Mapping[str, str], properties),
    )


def _assert_read_error(
    document: DataHubRegistryDocument | None,
    scope: SemanticRegistryScope,
    code: PlanningPortErrorCode,
) -> PlanningPortError:
    client = FakeRegistryReadClient(document=document)
    with pytest.raises(PlanningPortError) as raised:
        _reader(client, scope).load()
    assert raised.value.code is code
    assert client.identity_calls == [datahub_registry_document_urn(scope, 1)]
    assert client.document_calls == [datahub_registry_document_urn(scope, 1)]
    return raised.value


def test_live_reader_reconstructs_exact_7_31_5_registry_and_workspace_binding(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    client = FakeRegistryReadClient(document=document)

    scoped = _reader(client, scope).load()

    assert scoped.scope == scope
    assert scoped.registry == live_registry
    assert scoped.registry.source.startswith("datahub:")
    assert len(scoped.registry.logical_context.models) == 7
    assert len(scoped.registry.logical_context.field_index()) == 31
    assert len(scoped.registry.mapping_set.mappings) == 31
    assert len(scoped.registry.join_contracts.contracts) == 5
    assert len(semantic_registry_decision_ids(scoped.registry)) == 37
    assert client.identity_calls == [approval.target]
    assert client.document_calls == [approval.target]
    assert not hasattr(client, "upsert_document")

    other_scope = scope.model_copy(update={"workspace_id": "ws_unit_tenant_b_8ca900"})
    other_target = datahub_registry_document_urn(other_scope, live_registry.version)
    rebound = replace(
        document,
        urn=other_target,
        custom_properties={
            **document.custom_properties,
            "schemabridge.registryWorkspaceDigest": hashlib.sha256(
                other_scope.workspace_id.encode()
            ).hexdigest()[:24],
        },
    )
    with pytest.raises(PlanningPortError) as raised:
        _reader(FakeRegistryReadClient(document=rebound), other_scope).load()
    assert raised.value.code is PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH


def test_control_plane_version_reader_returns_strict_exact_publication(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryReadClient(
        document=_published_document(live_registry, approval),
    )
    reader = DataHubRegistryVersionReader(
        config=DataHubRegistryReadConfig(
            server="http://127.0.0.1:1",
            token="synthetic-unit-token",
        ),
        client=client,
    )

    loaded = reader.load_version(scope, live_registry.version)

    assert loaded.snapshot.scope == scope
    assert loaded.snapshot.registry == live_registry
    assert loaded.publication_approval_id == approval.id
    assert loaded.trust is RegistryVersionTrust.STRICT
    assert client.identity_calls == [approval.target]
    assert client.document_calls == [approval.target]


def test_control_plane_version_reader_sanitizes_missing_and_overprivileged_reads(
    scope: SemanticRegistryScope,
) -> None:
    config = DataHubRegistryReadConfig(
        server="http://127.0.0.1:1",
        token="synthetic-unit-token",
    )
    missing = DataHubRegistryVersionReader(
        config=config,
        client=FakeRegistryReadClient(document=None),
    )
    overprivileged = DataHubRegistryVersionReader(
        config=config,
        client=FakeRegistryReadClient(
            document=None,
            identity_value=DataHubRegistryIdentity(
                actor_urn=READER_ACTOR,
                granted_platform_mutation_privileges=frozenset(),
                granted_target_edit_privileges=frozenset({"canEditProperties"}),
            ),
        ),
    )

    with pytest.raises(RegistryControlError) as missing_error:
        missing.load_version(scope, 1)
    with pytest.raises(RegistryControlError) as privilege_error:
        overprivileged.load_version(scope, 1)

    assert missing_error.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE
    assert privilege_error.value.code is RegistryControlErrorCode.VERSION_INVALID


def test_live_reader_rejects_a_credential_with_document_mutation_privilege(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryReadClient(
        document=_published_document(live_registry, approval),
        identity_value=DataHubRegistryIdentity(
            actor_urn=READER_ACTOR,
            granted_platform_mutation_privileges=frozenset(),
            granted_target_edit_privileges=frozenset({"canEditProperties"}),
        ),
    )

    with pytest.raises(PlanningPortError) as raised:
        _reader(client, scope).load()

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_FORBIDDEN
    assert str(raised.value) == "DataHub registry reader has mutation privileges"
    assert client.document_calls == []


def test_live_reader_allows_only_the_known_stock_personal_token_privilege(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryReadClient(
        document=_published_document(live_registry, approval),
        identity_value=DataHubRegistryIdentity(
            actor_urn=READER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"generatePersonalAccessTokens"}),
            granted_target_edit_privileges=frozenset(),
        ),
    )

    assert _reader(client, scope).load().registry == live_registry


def test_live_reader_maps_missing_and_removed_versions_without_fallback(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    missing = _assert_read_error(None, scope, PlanningPortErrorCode.REGISTRY_NOT_FOUND)
    assert str(missing) == "configured DataHub semantic registry version was not found"

    removed = replace(_published_document(live_registry, approval), removed=True)
    raised = _assert_read_error(
        removed,
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )
    assert "identity or active status changed" in str(raised)


def test_live_reader_rejects_partial_extra_and_non_string_properties(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    missing = dict(document.custom_properties)
    missing.pop("schemabridge.registryPublicationAudit")
    _assert_read_error(
        _document_with_properties(document, missing),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    extra = _properties(document, **{"schemabridge.registryUnexpected": "forged"})
    _assert_read_error(
        _document_with_properties(document, extra),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    unrelated_extra = _properties(document, **{"unrelated.extra": "forged"})
    _assert_read_error(
        _document_with_properties(document, unrelated_extra),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    non_string = _properties(
        document,
        **{"schemabridge.registrySnapshot": 7},
    )
    _assert_read_error(
        _document_with_properties(document, non_string),
        scope,
        PlanningPortErrorCode.CONTEXT_INVALID,
    )


@pytest.mark.parametrize(
    "change",
    (
        {"title": "Forged registry title"},
        {"text": "# Forged registry text"},
    ),
)
def test_live_reader_rejects_mutated_document_title_or_text(
    change: dict[str, str],
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = replace(_published_document(live_registry, approval), **change)

    _assert_read_error(
        document,
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )


def test_live_reader_rejects_wrong_scope_and_fingerprint(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    wrong_catalog_scope = scope.model_copy(update={"catalog_scope": "other-demo"})
    _assert_read_error(
        document,
        wrong_catalog_scope,
        PlanningPortErrorCode.REGISTRY_SCOPE_MISMATCH,
    )

    wrong_fingerprint = _properties(
        document,
        **{"schemabridge.registryFingerprint": "0" * 64},
    )
    _assert_read_error(
        _document_with_properties(document, wrong_fingerprint),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    wrong_version = _properties(
        document,
        **{"schemabridge.registryVersion": "2"},
    )
    _assert_read_error(
        _document_with_properties(document, wrong_version),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )


def test_live_reader_rejects_corrupt_approval_and_audit(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    approval_payload = json.loads(
        document.custom_properties["schemabridge.registryPublicationApproval"]
    )
    approval_payload["payload_fingerprint"] = "0" * 64
    bad_approval = _properties(
        document,
        **{
            "schemabridge.registryPublicationApproval": json.dumps(
                approval_payload,
                separators=(",", ":"),
            )
        },
    )
    _assert_read_error(
        _document_with_properties(document, bad_approval),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    audit_payload = json.loads(document.custom_properties["schemabridge.registryPublicationAudit"])
    audit_payload["actor"] = "forged_audit_actor"
    bad_audit = _properties(
        document,
        **{
            "schemabridge.registryPublicationAudit": json.dumps(
                audit_payload,
                separators=(",", ":"),
            )
        },
    )
    _assert_read_error(
        _document_with_properties(document, bad_audit),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )


def test_live_reader_rejects_stale_decision_closure_and_related_assets(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    decisions = list(json.loads(document.custom_properties["schemabridge.registryDecisionIds"]))
    stale_decisions = _properties(
        document,
        **{
            "schemabridge.registryDecisionIds": json.dumps(
                decisions[:-1],
                separators=(",", ":"),
            )
        },
    )
    _assert_read_error(
        _document_with_properties(document, stale_decisions),
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )

    wrong_assets = replace(
        document,
        related_asset_urns=(
            *document.related_asset_urns[:-1],
            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "schemabridge.support.customer_cases,PROD)",
        ),
    )
    _assert_read_error(
        wrong_assets,
        scope,
        PlanningPortErrorCode.REGISTRY_INTEGRITY_FAILED,
    )


def test_live_reader_rejects_malformed_and_duplicate_json(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    malformed = _properties(
        document,
        **{"schemabridge.registrySnapshot": '{"format_version":'},
    )
    _assert_read_error(
        _document_with_properties(document, malformed),
        scope,
        PlanningPortErrorCode.CONTEXT_INVALID,
    )

    snapshot = document.custom_properties["schemabridge.registrySnapshot"]
    assert snapshot.startswith("{")
    duplicated = _properties(
        document,
        **{"schemabridge.registrySnapshot": ('{"registry_id":"forged_registry",' + snapshot[1:])},
    )
    _assert_read_error(
        _document_with_properties(document, duplicated),
        scope,
        PlanningPortErrorCode.CONTEXT_INVALID,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PermissionError("secret detail"), PlanningPortErrorCode.CONTEXT_FORBIDDEN),
        (ConnectionError("secret detail"), PlanningPortErrorCode.CONTEXT_UNAVAILABLE),
        (
            urllib.error.HTTPError(
                "http://datahub.invalid",
                503,
                "secret detail",
                {},
                None,
            ),
            PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
        ),
        (ValueError("secret detail"), PlanningPortErrorCode.CONTEXT_INVALID),
    ],
)
def test_live_reader_maps_client_failures_to_sanitized_codes(
    error: Exception,
    expected: PlanningPortErrorCode,
    scope: SemanticRegistryScope,
) -> None:
    client = FakeRegistryReadClient(document=None, identity_error=error)

    with pytest.raises(PlanningPortError) as raised:
        _reader(client, scope).load()

    assert raised.value.code is expected
    assert str(raised.value) == "DataHub semantic registry read failed"
    assert "secret detail" not in str(raised.value)
    assert client.document_calls == []


def test_publisher_rejects_missing_or_mismatched_approval_with_zero_client_io(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient()
    publisher = _publisher(client)

    with pytest.raises(RegistryPublicationError) as missing:
        publisher.publish(live_registry, None)  # type: ignore[arg-type]
    assert missing.value.code is RegistryPublicationErrorCode.APPROVAL_REQUIRED

    wrong = approval.model_copy(update={"payload_fingerprint": "0" * 64})
    with pytest.raises(RegistryPublicationError) as mismatch:
        publisher.publish(live_registry, wrong)
    assert mismatch.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH

    forged_confirmation = approval.model_copy(update={"confirmation": "not-approved"})
    with pytest.raises(RegistryPublicationError) as forged:
        publisher.publish(live_registry, forged_confirmation)
    assert forged.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH

    legacy_id = approval.model_copy(
        update={"id": f"{approval.actor}-{approval.registry_id}-v{approval.registry_version}"}
    )
    with pytest.raises(RegistryPublicationError) as legacy:
        publisher.publish(live_registry, legacy_id)
    assert legacy.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH

    assert client.identity_calls == []
    assert client.document_calls == []
    assert client.upserts == []


def test_publisher_publishes_post_write_verifies_and_replays_idempotently(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient()
    publisher = _publisher(client)

    first = publisher.publish(live_registry, approval)
    second = publisher.publish(live_registry, approval)
    distinct_approval = _approval(
        live_registry,
        scope,
        actor="publisher_unit_actor_2",
    ).model_copy(update={"approved_at": NOW + timedelta(seconds=10)})
    third = publisher.publish(live_registry, distinct_approval)

    assert first.status is RegistryPublicationStatus.PUBLISHED
    assert first.audit_record.previous_fingerprint is None
    assert second.status is RegistryPublicationStatus.ALREADY_CURRENT
    assert second.audit_record.previous_fingerprint == live_registry.fingerprint
    assert third.status is RegistryPublicationStatus.ALREADY_CURRENT
    assert third.audit_record.approved_at == distinct_approval.approved_at
    assert first.workspace_id == second.workspace_id == approval.workspace_id
    assert first.target == second.target == third.target == approval.target
    assert len(client.upserts) == 1
    assert client.document_calls == [
        approval.target,
        approval.target,
        approval.target,
        approval.target,
    ]


def test_legacy_publisher_tolerates_the_historical_target_grant(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient(
        identity_value=DataHubRegistryIdentity(
            actor_urn=WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset(
                {
                    "generatePersonalAccessTokens",
                    "manageDocuments",
                    "manageGlossaries",
                    "manageStructuredProperties",
                }
            ),
            granted_target_edit_privileges=frozenset({"EDIT_ENTITY", "MANAGE_DOCUMENTS"}),
        )
    )

    result = _publisher(client).publish(live_registry, approval)

    assert result.status is RegistryPublicationStatus.PUBLISHED
    assert len(client.upserts) == 1

    client.identity_value = replace(
        client.identity_value,
        granted_target_edit_privileges=frozenset(
            {"EDIT_ENTITY", "MANAGE_DOCUMENTS", "canEditProperties"}
        ),
    )
    with pytest.raises(RegistryPublicationError) as raised:
        _publisher(client).publish(live_registry, approval)

    assert raised.value.code is RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED


def test_publisher_rejects_reissued_approval_id_with_changed_immutable_facts(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient()
    publisher = _publisher(client)

    publisher.publish(live_registry, approval)
    changed_time = approval.model_copy(update={"approved_at": NOW + timedelta(seconds=10)})

    with pytest.raises(RegistryPublicationError) as raised:
        publisher.publish(live_registry, changed_time)

    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH
    assert str(raised.value) == (
        "registry publication approval id identifies different immutable facts"
    )
    assert len(client.upserts) == 1


def test_publisher_rejects_an_immutable_target_conflict_before_upsert(
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    existing = _published_document(live_registry, approval)
    client = FakeRegistryWriteClient(document=existing)
    conflicting_payload = live_registry.model_dump(mode="python")
    conflicting_payload["logical_context"]["models"][0]["description"] = (
        "A materially different approved model description."
    )
    conflicting_registry = GovernedSemanticRegistrySnapshot.model_validate(conflicting_payload)
    another_approval = _approval(
        conflicting_registry,
        scope,
        actor="publisher_unit_actor_2",
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _publisher(client).publish(conflicting_registry, another_approval)

    assert raised.value.code is RegistryPublicationErrorCode.CONFLICT
    assert client.upserts == []


def test_publisher_rejects_unbounded_writer_identity_before_target_read(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient(
        identity_value=DataHubRegistryIdentity(
            actor_urn=WRITER_ACTOR,
            granted_platform_mutation_privileges=frozenset({"manageDocuments", "managePolicies"}),
            granted_target_edit_privileges=frozenset(),
        )
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _publisher(client).publish(live_registry, approval)

    assert raised.value.code is RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED
    assert client.document_calls == []
    assert client.upserts == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            PermissionError("secret writer detail"),
            RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED,
        ),
        (ConnectionError("secret writer detail"), RegistryPublicationErrorCode.CATALOG_UNAVAILABLE),
        (
            urllib.error.HTTPError(
                "http://datahub.invalid",
                503,
                "secret writer detail",
                {},
                None,
            ),
            RegistryPublicationErrorCode.CATALOG_UNAVAILABLE,
        ),
        (ValueError("secret writer detail"), RegistryPublicationErrorCode.INVALID_RESPONSE),
    ],
)
def test_publisher_maps_target_read_errors_without_upsert(
    error: Exception,
    expected: RegistryPublicationErrorCode,
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient(document_error=error)

    with pytest.raises(RegistryPublicationError) as raised:
        _publisher(client).publish(live_registry, approval)

    assert raised.value.code is expected
    assert "secret writer detail" not in str(raised.value)
    assert client.upserts == []


def test_publisher_returns_typed_failed_result_for_post_write_outage(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    client = FakeRegistryWriteClient(upsert_error=TimeoutError("secret post-write detail"))

    result = _publisher(client).publish(live_registry, approval)

    assert result.status is RegistryPublicationStatus.FAILED
    assert result.reason_code == "catalog_unavailable"
    assert result.audit_record.reason_code == "catalog_unavailable"
    assert result.audit_record.previous_fingerprint is None
    assert len(client.upserts) == 1


def test_publisher_returns_typed_failed_result_for_mismatched_post_write_state(
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    def corrupt(document: DataHubRegistryDocument) -> DataHubRegistryDocument:
        return _document_with_properties(
            document,
            _properties(
                document,
                **{"schemabridge.registryFingerprint": "0" * 64},
            ),
        )

    client = FakeRegistryWriteClient(after_upsert=corrupt)

    result = _publisher(client).publish(live_registry, approval)

    assert result.status is RegistryPublicationStatus.FAILED
    assert result.reason_code == "post_write_verification_failed"
    assert len(client.upserts) == 1


def test_registry_json_limit_accepts_exact_size_and_rejects_one_byte_over_before_io(
    monkeypatch: pytest.MonkeyPatch,
    live_registry: GovernedSemanticRegistrySnapshot,
    approval: RegistryPublicationApproval,
) -> None:
    exact_size = len(live_registry.model_dump_json().encode())
    accepted_client = FakeRegistryWriteClient()
    monkeypatch.setattr(datahub_module, "_MAX_REGISTRY_JSON_BYTES", exact_size)

    accepted = _publisher(accepted_client).publish(live_registry, approval)

    assert accepted.status is RegistryPublicationStatus.PUBLISHED
    assert len(accepted_client.upserts) == 1

    rejected_client = FakeRegistryWriteClient()
    monkeypatch.setattr(datahub_module, "_MAX_REGISTRY_JSON_BYTES", exact_size - 1)
    with pytest.raises(RegistryPublicationError) as raised:
        _publisher(rejected_client).publish(live_registry, approval)

    assert raised.value.code is RegistryPublicationErrorCode.PAYLOAD_INVALID
    assert rejected_client.identity_calls == []
    assert rejected_client.document_calls == []
    assert rejected_client.upserts == []


def test_reader_property_limit_accepts_exact_size_and_rejects_one_byte_over(
    monkeypatch: pytest.MonkeyPatch,
    live_registry: GovernedSemanticRegistrySnapshot,
    scope: SemanticRegistryScope,
    approval: RegistryPublicationApproval,
) -> None:
    document = _published_document(live_registry, approval)
    exact_size = len(document.custom_properties["schemabridge.registrySnapshot"].encode())
    monkeypatch.setattr(datahub_module, "_MAX_REGISTRY_JSON_BYTES", exact_size)
    assert _reader(FakeRegistryReadClient(document), scope).load().registry == live_registry

    monkeypatch.setattr(datahub_module, "_MAX_REGISTRY_JSON_BYTES", exact_size - 1)
    with pytest.raises(PlanningPortError) as raised:
        _reader(FakeRegistryReadClient(document), scope).load()

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_INVALID


@pytest.mark.parametrize(
    "server",
    (
        "http://datahub.example.test",
        "http://user:password@127.0.0.1:8080",
        "https://datahub.example.test?token=unsafe",
        "https://datahub.example.test/#fragment",
    ),
)
def test_reader_env_rejects_unsafe_transport(
    tmp_path: Path,
    server: str,
) -> None:
    env_path = tmp_path / "reader.env"
    env_path.write_text(f"DATAHUB_GMS_URL={server}\nDATAHUB_GMS_TOKEN=synthetic-token\n")
    env_path.chmod(0o600)

    with pytest.raises(PlanningPortError) as raised:
        DataHubRegistryReadConfig.from_env_file(env_path)

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE
    assert "synthetic-token" not in str(raised.value)


def test_reader_env_rejects_duplicate_keys(tmp_path: Path) -> None:
    env_path = tmp_path / "reader.env"
    env_path.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=synthetic-token\n"
        "DATAHUB_GMS_TOKEN=duplicate-token\n"
    )
    env_path.chmod(0o600)

    with pytest.raises(PlanningPortError) as raised:
        DataHubRegistryReadConfig.from_env_file(env_path)

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE


def test_reader_env_rejects_unrelated_secret_keys(tmp_path: Path) -> None:
    env_path = tmp_path / "reader.env"
    unrelated_secret = "must-not-be-loaded"
    env_path.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=synthetic-token\n"
        f"OPENAI_API_KEY={unrelated_secret}\n"
    )
    env_path.chmod(0o600)

    with pytest.raises(PlanningPortError) as raised:
        DataHubRegistryReadConfig.from_env_file(env_path)

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE
    assert unrelated_secret not in str(raised.value)


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("TOOLS_IS_MUTATION_ENABLED", "true"),
        ("SAVE_DOCUMENT_TOOL_ENABLED", "true"),
        ("DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED", "false"),
    ),
)
def test_reader_env_rejects_unsafe_exact_mcp_flag_set(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    flags = {
        "TOOLS_IS_MUTATION_ENABLED": "false",
        "SAVE_DOCUMENT_TOOL_ENABLED": "false",
        "DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED": "true",
    }
    flags[flag] = value
    env_path = tmp_path / "mcp.env"
    env_path.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=synthetic-token\n"
        + "".join(f"{key}={item}\n" for key, item in flags.items())
    )
    env_path.chmod(0o600)

    with pytest.raises(PlanningPortError) as raised:
        DataHubRegistryReadConfig.from_env_file(env_path)

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE


def test_writer_env_rejects_unrelated_secret_keys(tmp_path: Path) -> None:
    env_path = tmp_path / "writer.env"
    unrelated_secret = "must-not-be-loaded"
    env_path.write_text(
        "DATAHUB_GMS_URL=http://127.0.0.1:8080\n"
        "DATAHUB_GMS_TOKEN=synthetic-token\n"
        f"DATAHUB_WRITER_ACTOR_URN={WRITER_ACTOR}\n"
        f"OPENAI_API_KEY={unrelated_secret}\n"
    )
    env_path.chmod(0o600)

    with pytest.raises(RegistryPublicationError) as raised:
        DataHubRegistryWriteConfig.from_env_file(env_path)

    assert raised.value.code is RegistryPublicationErrorCode.CATALOG_UNAVAILABLE
    assert unrelated_secret not in str(raised.value)


def test_http_identity_rejects_partial_privilege_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DataHubHttpRegistryReadClient(
        server="http://127.0.0.1:8080",
        token="synthetic-unit-token",
    )
    monkeypatch.setattr(
        DataHubHttpRegistryReadClient,
        "_graphql",
        lambda *_args, **_kwargs: {
            "me": {
                "corpUser": {"urn": READER_ACTOR},
                "platformPrivileges": {"manageDocuments": False},
            }
        },
    )

    with pytest.raises(ValueError, match="privilege response is incomplete"):
        client.identity("urn:li:document:synthetic")


@pytest.mark.parametrize("related_assets", ("absent", []))
def test_http_document_reader_accepts_no_related_assets(
    monkeypatch: pytest.MonkeyPatch,
    related_assets: str | list[object],
) -> None:
    client = DataHubHttpRegistryReadClient(
        server="http://127.0.0.1:8080",
        token="synthetic-unit-token",
    )
    document_info: dict[str, object] = {
        "status": {"state": "PUBLISHED"},
        "title": "Synthetic active-registry projection",
        "contents": {"text": "Synthetic projection payload."},
        "customProperties": {"schemabridge.synthetic": "true"},
    }
    if related_assets != "absent":
        document_info["relatedAssets"] = related_assets
    payload = {
        "aspect": {
            "com.linkedin.knowledge.DocumentInfo": document_info,
        }
    }

    def aspect(
        _client: DataHubHttpRegistryReadClient,
        _urn: str,
        aspect_name: str,
        _maximum: int,
    ) -> dict[str, object] | None:
        return payload if aspect_name == "documentInfo" else None

    monkeypatch.setattr(DataHubHttpRegistryReadClient, "_aspect", aspect)

    document = client.get_document("urn:li:document:synthetic-active-registry")

    assert document is not None
    assert document.related_asset_urns == ()


@pytest.mark.parametrize(
    "related_assets",
    (
        None,
        (),
        [{"asset": []}],
        [{"asset": "urn:li:dataset:duplicate"}, {"asset": "urn:li:dataset:duplicate"}],
        [{"asset": f"urn:li:dataset:{index}"} for index in range(257)],
    ),
)
def test_http_document_reader_rejects_malformed_or_unbounded_related_assets(
    monkeypatch: pytest.MonkeyPatch,
    related_assets: object,
) -> None:
    client = DataHubHttpRegistryReadClient(
        server="http://127.0.0.1:8080",
        token="synthetic-unit-token",
    )
    payload = {
        "aspect": {
            "com.linkedin.knowledge.DocumentInfo": {
                "status": {"state": "PUBLISHED"},
                "title": "Synthetic active-registry projection",
                "contents": {"text": "Synthetic projection payload."},
                "customProperties": {"schemabridge.synthetic": "true"},
                "relatedAssets": related_assets,
            }
        }
    }

    def aspect(
        _client: DataHubHttpRegistryReadClient,
        _urn: str,
        aspect_name: str,
        _maximum: int,
    ) -> dict[str, object] | None:
        return payload if aspect_name == "documentInfo" else None

    monkeypatch.setattr(DataHubHttpRegistryReadClient, "_aspect", aspect)

    with pytest.raises(ValueError, match="related assets are invalid"):
        client.get_document("urn:li:document:synthetic-active-registry")
