"""Adversarial unit tests for bounded catalog source pages and refresh lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogConnectionId,
    CatalogRefreshCommand,
    CatalogRefreshConfirmation,
    CatalogRefreshFailureCode,
    CatalogRefreshId,
    CatalogRefreshLease,
    CatalogRefreshMode,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogRefreshSummary,
    CatalogRefreshTransitionError,
    CatalogRefreshTransitionErrorCode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
    ensure_catalog_refresh_transition,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
CAPABILITY = "c" * 64


def _field(name: str = "contract_id") -> CatalogSourceField:
    return CatalogSourceField(
        field_path=(name,),
        native_type="VARCHAR(12)",
        normalized_type=PhysicalValueType.STRING,
        description="Synthetic identifier metadata.",
        nullable=False,
        is_part_of_key=True,
        tags=("identifier",),
        glossary_terms=("Contract",),
        metadata_fingerprint=SHA_A,
    )


def _asset(name: str = "contracts") -> CatalogSourceAsset:
    return CatalogSourceAsset(
        asset_id=CatalogAssetId(f"urn:li:dataset:(urn:li:dataPlatform:postgres,core.{name},PROD)"),
        qualified_name=f"core.{name}",
        display_name=name,
        platform="postgres",
        environment="PROD",
        database_name="schemabridge",
        schema_name="core",
        description="Synthetic catalog asset.",
        fields=(_field(),),
        metadata_fingerprint=SHA_B,
    )


def _upsert_asset(name: str = "contracts") -> CatalogSourceChange:
    return CatalogSourceChange(
        kind=CatalogSourceChangeKind.UPSERT_ASSET,
        asset=_asset(name),
    )


def _lease(
    *,
    leased_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=2),
) -> CatalogRefreshLease:
    return CatalogRefreshLease(
        indexer_id="indexer_primary",
        capability_digest=CAPABILITY,
        fencing_token=1,
        leased_at=leased_at,
        expires_at=expires_at,
    )


def _state(
    *,
    status: CatalogRefreshStatus = CatalogRefreshStatus.REQUESTED,
    lease: CatalogRefreshLease | None = None,
    updated_at: datetime = NOW,
    **updates: object,
) -> CatalogRefreshState:
    payload: dict[str, object] = {
        "refresh_id": CatalogRefreshId("refresh_unit_one"),
        "workspace_id": "workspace_alpha",
        "connection_id": CatalogConnectionId("connection_primary"),
        "mode": CatalogRefreshMode.FULL,
        "status": status,
        "base_generation": 0,
        "target_generation": 1,
        "idempotency_digest": SHA_A,
        "requested_by": "actor_platform_admin",
        "requested_at": NOW,
        "updated_at": updated_at,
        "lease": lease,
    }
    payload.update(updates)
    return CatalogRefreshState.model_validate(payload)


def test_full_source_page_carries_asset_and_fields_in_one_bounded_unit() -> None:
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert_asset(),),
        next_checkpoint="scroll-next-1",
        source_complete=False,
    )

    assert page.changes[0].asset is not None
    assert page.changes[0].asset.fields[0].field_path == ("contract_id",)
    assert len(page.changes) == 1
    assert page.next_checkpoint == "scroll-next-1"


def test_source_page_fingerprint_rejects_tampering_and_duplicate_identity() -> None:
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert_asset(),),
        next_checkpoint=None,
        source_complete=True,
    )
    tampered = page.model_dump(mode="python")
    tampered["sequence"] = 2

    with pytest.raises(ValidationError, match="fingerprint"):
        CatalogSourcePage.model_validate(tampered)
    with pytest.raises(ValidationError, match="unique identities"):
        CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(_upsert_asset(), _upsert_asset()),
            next_checkpoint=None,
            source_complete=True,
        )


def test_source_page_fingerprint_binds_database_and_normalized_type_evidence() -> None:
    baseline = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(_upsert_asset(),),
        next_checkpoint=None,
        source_complete=True,
    )
    assert baseline.changes[0].asset is not None
    changed_asset = baseline.changes[0].asset.model_copy(
        update={
            "database_name": "schemabridge_archive",
            "fields": (
                baseline.changes[0]
                .asset.fields[0]
                .model_copy(update={"normalized_type": PhysicalValueType.INTEGER}),
            ),
        }
    )
    changed = CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=1,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=changed_asset,
            ),
        ),
        next_checkpoint=None,
        source_complete=True,
    )

    assert changed.page_fingerprint != baseline.page_fingerprint


def test_nonterminal_source_page_requires_progress_and_data() -> None:
    with pytest.raises(ValidationError, match="next checkpoint"):
        CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(_upsert_asset(),),
            next_checkpoint=None,
            source_complete=False,
        )
    with pytest.raises(ValidationError, match="empty"):
        CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(),
            next_checkpoint="scroll-next-1",
            source_complete=False,
        )


def test_full_scan_cannot_be_relabelled_as_delta_or_emit_deletes() -> None:
    delete = CatalogSourceChange(
        kind=CatalogSourceChangeKind.DELETE_ASSET,
        asset_id=_asset().asset_id,
    )
    with pytest.raises(ValidationError, match="full discovery"):
        CatalogSourcePage.create(
            mode=CatalogRefreshMode.FULL,
            sequence=1,
            changes=(delete,),
            next_checkpoint=None,
            source_complete=True,
        )

    delta = CatalogSourcePage.create(
        mode=CatalogRefreshMode.DELTA,
        sequence=1,
        changes=(delete,),
        next_checkpoint=None,
        source_complete=True,
    )
    assert delta.changes[0].kind is CatalogSourceChangeKind.DELETE_ASSET


@pytest.mark.parametrize(
    "change",
    [
        CatalogSourceChange(
            kind=CatalogSourceChangeKind.UPSERT_FIELD,
            asset_id=_asset().asset_id,
            field=_field("status"),
        ),
        CatalogSourceChange(
            kind=CatalogSourceChangeKind.DELETE_FIELD,
            asset_id=_asset().asset_id,
            field_path=("legacy_status",),
        ),
    ],
)
def test_delta_field_changes_are_typed_without_source_values(
    change: CatalogSourceChange,
) -> None:
    page = CatalogSourcePage.create(
        mode=CatalogRefreshMode.DELTA,
        sequence=1,
        changes=(change,),
        next_checkpoint=None,
        source_complete=True,
    )
    encoded = page.model_dump_json()

    assert page.source_complete
    assert "sample" not in encoded
    assert "rows" not in encoded


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": CatalogSourceChangeKind.UPSERT_ASSET,
            "asset_id": _asset().asset_id,
        },
        {
            "kind": CatalogSourceChangeKind.DELETE_ASSET,
            "asset": _asset(),
        },
        {
            "kind": CatalogSourceChangeKind.UPSERT_FIELD,
            "asset_id": _asset().asset_id,
        },
        {
            "kind": CatalogSourceChangeKind.DELETE_FIELD,
            "asset_id": _asset().asset_id,
        },
    ],
)
def test_source_changes_reject_missing_or_mismatched_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CatalogSourceChange.model_validate(payload)


def test_refresh_request_requires_exact_confirmation_and_opaque_idempotency() -> None:
    command = CatalogRefreshCommand(
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_primary"),
        mode=CatalogRefreshMode.FULL,
        requested_by="actor_platform_admin",
        requested_at=NOW,
        idempotency_digest=SHA_A,
    )
    payload = command.model_dump(mode="python")
    payload["confirmation"] = "anything-else"

    assert command.confirmation is CatalogRefreshConfirmation.REQUEST
    with pytest.raises(ValidationError):
        CatalogRefreshCommand.model_validate(payload)


def test_refresh_lifecycle_accepts_only_closed_forward_transitions() -> None:
    requested = _state()
    ensure_catalog_refresh_transition(
        requested,
        CatalogRefreshStatus.LEASED,
        at=NOW,
    )
    leased = _state(
        status=CatalogRefreshStatus.LEASED,
        lease=_lease(),
        updated_at=NOW + timedelta(seconds=1),
    )
    ensure_catalog_refresh_transition(
        leased,
        CatalogRefreshStatus.STAGING,
        at=NOW + timedelta(seconds=1),
    )
    staging = _state(
        status=CatalogRefreshStatus.STAGING,
        lease=_lease(),
        updated_at=NOW + timedelta(seconds=2),
    )
    ensure_catalog_refresh_transition(
        staging,
        CatalogRefreshStatus.COMPLETED,
        at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(CatalogRefreshTransitionError) as captured:
        ensure_catalog_refresh_transition(
            requested,
            CatalogRefreshStatus.COMPLETED,
            at=NOW,
        )
    assert captured.value.code is CatalogRefreshTransitionErrorCode.INVALID_STATE


def test_lease_reclaim_requires_expiry_and_terminal_state_is_immutable() -> None:
    leased = _state(
        status=CatalogRefreshStatus.LEASED,
        lease=_lease(),
        updated_at=NOW,
    )
    with pytest.raises(CatalogRefreshTransitionError) as captured:
        ensure_catalog_refresh_transition(
            leased,
            CatalogRefreshStatus.REQUESTED,
            at=NOW + timedelta(minutes=1),
        )
    assert captured.value.code is CatalogRefreshTransitionErrorCode.LEASE_ACTIVE

    ensure_catalog_refresh_transition(
        leased,
        CatalogRefreshStatus.REQUESTED,
        at=NOW + timedelta(minutes=2),
    )

    completed = _state(
        status=CatalogRefreshStatus.COMPLETED,
        updated_at=NOW + timedelta(minutes=3),
        source_page_count=1,
        source_page_fingerprint=SHA_A,
        source_complete=True,
        catalog_fingerprint=SHA_B,
        completed_at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(CatalogRefreshTransitionError) as terminal:
        ensure_catalog_refresh_transition(
            completed,
            CatalogRefreshStatus.FAILED,
            at=NOW + timedelta(minutes=4),
        )
    assert terminal.value.code is CatalogRefreshTransitionErrorCode.TERMINAL_IMMUTABLE


def test_refresh_state_rejects_partial_completion_and_stale_generation() -> None:
    with pytest.raises(ValidationError, match="fingerprint"):
        _state(
            status=CatalogRefreshStatus.COMPLETED,
            completed_at=NOW,
        )
    with pytest.raises(ValidationError, match="advance"):
        _state(base_generation=1, target_generation=1)
    with pytest.raises(ValidationError, match="lease ownership"):
        _state(status=CatalogRefreshStatus.STAGING)
    with pytest.raises(ValidationError, match="persisted source page"):
        _state(staged_asset_count=1)


def test_completed_and_failed_state_have_mutually_exclusive_safe_metadata() -> None:
    completed = _state(
        status=CatalogRefreshStatus.COMPLETED,
        source_page_count=1,
        source_page_fingerprint=SHA_A,
        staged_asset_count=5_434,
        staged_field_count=21_736,
        source_complete=True,
        source_checkpoint="scroll-final",
        catalog_fingerprint=SHA_B,
        completed_at=NOW,
    )
    failed = _state(
        status=CatalogRefreshStatus.FAILED,
        failure_code=CatalogRefreshFailureCode.SOURCE_UNAVAILABLE,
        completed_at=NOW,
    )

    assert completed.staged_asset_count == 5_434
    assert failed.failure_code is CatalogRefreshFailureCode.SOURCE_UNAVAILABLE

    payload = completed.model_dump(mode="python")
    payload["failure_code"] = CatalogRefreshFailureCode.STORE_UNAVAILABLE
    with pytest.raises(ValidationError):
        CatalogRefreshState.model_validate(payload)


def test_public_refresh_summary_omits_lease_checkpoint_actor_and_idempotency() -> None:
    state = _state(
        status=CatalogRefreshStatus.COMPLETED,
        source_page_count=109,
        source_page_fingerprint=SHA_A,
        staged_asset_count=5_434,
        staged_field_count=21_736,
        source_complete=True,
        source_checkpoint="scroll-final",
        catalog_fingerprint=SHA_B,
        completed_at=NOW,
    )
    summary = CatalogRefreshSummary.from_state(state)
    encoded = summary.model_dump_json()

    assert summary.asset_count == 5_434
    assert summary.source_page_count == 109
    assert "source_checkpoint" not in encoded
    assert "idempotency" not in encoded
    assert "requested_by" not in encoded
    assert "capability" not in encoded


def test_refresh_lease_is_bounded_and_uses_digest_not_raw_capability() -> None:
    lease = _lease()
    assert lease.is_current(NOW + timedelta(seconds=30))
    assert not lease.is_current(lease.expires_at)

    with pytest.raises(ValidationError, match="five minutes"):
        _lease(expires_at=NOW + timedelta(minutes=6))
    payload = lease.model_dump(mode="python")
    payload["lease_capability"] = "raw-secret-capability"
    with pytest.raises(ValidationError):
        CatalogRefreshLease.model_validate(payload)
