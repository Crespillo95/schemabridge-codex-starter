"""Unit tests for one constant-memory catalog-indexer iteration."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from schemabridge.application.catalog_indexer import (
    CatalogIndexerIterationOutcome,
    CatalogIndexerUseCaseError,
    CatalogIndexerUseCaseErrorCode,
    KindCatalogSourceResolver,
    RunOneCatalogRefresh,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshCommand,
    CatalogRefreshFailureCode,
    CatalogRefreshId,
    CatalogRefreshLease,
    CatalogRefreshMode,
    CatalogRefreshRequestResult,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
CAPABILITY = "catalog-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CAPABILITY_DIGEST = hashlib.sha256(CAPABILITY.encode()).hexdigest()
SHA_A = "a" * 64
SHA_B = "b" * 64


def _replace(state: CatalogRefreshState, **updates: object) -> CatalogRefreshState:
    payload = state.model_dump(mode="python")
    payload.update(updates)
    return CatalogRefreshState.model_validate(payload)


def _requested_state(
    *,
    mode: CatalogRefreshMode = CatalogRefreshMode.FULL,
    source_page_count: int = 0,
    source_page_fingerprint: str | None = None,
    source_checkpoint: str | None = None,
    staged_asset_count: int = 0,
    staged_field_count: int = 0,
    source_complete: bool = False,
) -> CatalogRefreshState:
    return CatalogRefreshState(
        refresh_id=CatalogRefreshId("refresh_indexer_unit"),
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_primary"),
        mode=mode,
        status=CatalogRefreshStatus.REQUESTED,
        base_generation=0,
        target_generation=1,
        idempotency_digest=SHA_A,
        requested_by="actor_platform_admin",
        requested_at=NOW,
        updated_at=NOW,
        source_page_count=source_page_count,
        source_page_fingerprint=source_page_fingerprint,
        staged_asset_count=staged_asset_count,
        staged_field_count=staged_field_count,
        source_complete=source_complete,
        source_checkpoint=source_checkpoint,
    )


def _field(name: str = "contract_id") -> CatalogSourceField:
    return CatalogSourceField(
        field_path=(name,),
        native_type="varchar(20)",
        description="Synthetic identifier.",
        nullable=False,
        is_part_of_key=True,
        metadata_fingerprint=SHA_A,
    )


def _page(
    sequence: int,
    *,
    next_checkpoint: str | None,
    complete: bool,
) -> CatalogSourcePage:
    asset = CatalogSourceAsset(
        asset_id=CatalogAssetId(
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,scale.table_{sequence:05d},PROD)"
        ),
        qualified_name=f"scale.table_{sequence:05d}",
        display_name=f"table_{sequence:05d}",
        platform="postgres",
        environment="PROD",
        schema_name="scale",
        fields=(_field(),),
        metadata_fingerprint=SHA_B,
    )
    return CatalogSourcePage.create(
        mode=CatalogRefreshMode.FULL,
        sequence=sequence,
        changes=(
            CatalogSourceChange(
                kind=CatalogSourceChangeKind.UPSERT_ASSET,
                asset=asset,
            ),
        ),
        next_checkpoint=next_checkpoint,
        source_complete=complete,
    )


class _MemoryRefreshStore:
    def __init__(
        self,
        state: CatalogRefreshState | None,
        events: list[str],
    ) -> None:
        self.state = state
        self.events = events
        self.persist_error: CatalogInventoryError | None = None
        self.complete_error: CatalogInventoryError | None = None
        self.complete_kwargs: dict[str, object] | None = None
        self.reclaim_result: object = 0
        self.prune_result: object = 0
        self.reclaim_error: Exception | None = None
        self.prune_error: Exception | None = None
        self.heartbeat_calls = 0
        self.heartbeat_error_at: int | None = None

    def claim_next(
        self,
        *,
        indexer_id: str,
        lease_capability: str,
        lease_duration: timedelta,
    ) -> CatalogRefreshState | None:
        self.events.append("claim")
        if self.state is None:
            return None
        assert self.state.status is CatalogRefreshStatus.REQUESTED
        lease = CatalogRefreshLease(
            indexer_id=indexer_id,
            capability_digest=hashlib.sha256(lease_capability.encode()).hexdigest(),
            fencing_token=1,
            leased_at=NOW,
            expires_at=NOW + lease_duration,
        )
        self.state = _replace(
            self.state,
            status=CatalogRefreshStatus.LEASED,
            lease=lease,
        )
        return self.state

    def begin_staging(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> CatalogRefreshState:
        assert workspace_id == "workspace_alpha"
        del refresh_id, indexer_id, lease_capability, fencing_token
        self.events.append("begin")
        assert self.state is not None
        self.state = _replace(self.state, status=CatalogRefreshStatus.STAGING)
        return self.state

    def heartbeat(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        lease_duration: timedelta,
    ) -> CatalogRefreshState:
        assert workspace_id == "workspace_alpha"
        del (
            refresh_id,
            indexer_id,
            lease_capability,
            fencing_token,
            lease_duration,
        )
        self.events.append("heartbeat")
        self.heartbeat_calls += 1
        if self.heartbeat_calls == self.heartbeat_error_at:
            raise CatalogInventoryError(
                CatalogInventoryErrorCode.LEASE_CONFLICT,
                "sensitive stale lease detail",
            )
        assert self.state is not None
        return self.state

    def persist_page(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        page: CatalogSourcePage,
    ) -> CatalogRefreshState:
        assert workspace_id == "workspace_alpha"
        del refresh_id, indexer_id, lease_capability, fencing_token
        self.events.append(f"persist:{page.sequence}")
        if self.persist_error is not None:
            raise self.persist_error
        assert self.state is not None
        asset_delta = sum(
            1 for change in page.changes if change.kind is CatalogSourceChangeKind.UPSERT_ASSET
        )
        field_delta = sum(
            len(change.asset.fields) for change in page.changes if change.asset is not None
        )
        self.state = _replace(
            self.state,
            source_page_count=page.sequence,
            source_page_fingerprint=page.page_fingerprint,
            source_checkpoint=page.next_checkpoint,
            source_complete=page.source_complete,
            staged_asset_count=self.state.staged_asset_count + asset_delta,
            staged_field_count=self.state.staged_field_count + field_delta,
        )
        return self.state

    def complete(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        **kwargs: object,
    ) -> CatalogRefreshState:
        assert workspace_id == "workspace_alpha"
        del refresh_id
        self.events.append("complete")
        self.complete_kwargs = kwargs
        if self.complete_error is not None:
            raise self.complete_error
        assert self.state is not None
        self.state = _replace(
            self.state,
            status=CatalogRefreshStatus.COMPLETED,
            lease=None,
            catalog_fingerprint="f" * 64,
            completed_at=NOW + timedelta(minutes=1),
        )
        return self.state

    def fail(
        self,
        workspace_id: str,
        refresh_id: CatalogRefreshId,
        *,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
        code: CatalogRefreshFailureCode,
    ) -> CatalogRefreshState:
        assert workspace_id == "workspace_alpha"
        del refresh_id, indexer_id, lease_capability, fencing_token
        self.events.append(f"fail:{code.value}")
        assert self.state is not None
        self.state = _replace(
            self.state,
            status=CatalogRefreshStatus.FAILED,
            lease=None,
            failure_code=code,
            completed_at=NOW + timedelta(minutes=1),
        )
        return self.state

    def request(
        self,
        command: CatalogRefreshCommand,
    ) -> CatalogRefreshRequestResult:
        del command
        raise AssertionError("indexer cannot request refreshes")

    def load(self, workspace_id: str, refresh_id: CatalogRefreshId) -> CatalogRefreshState | None:
        del workspace_id, refresh_id
        return self.state

    def reclaim_expired(self, *, limit: int = 100) -> int:
        self.events.append(f"reclaim:{limit}")
        if self.reclaim_error is not None:
            raise self.reclaim_error
        return self.reclaim_result  # type: ignore[return-value]

    def prune_due_generations(self, *, limit: int = 100) -> int:
        self.events.append(f"prune:{limit}")
        if self.prune_error is not None:
            raise self.prune_error
        return self.prune_result  # type: ignore[return-value]

    def prune_generations(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        completed_before: datetime,
        limit: int = 100,
    ) -> int:
        del workspace_id, connection_id, completed_before, limit
        return 0


class _Routes:
    def __init__(
        self,
        route: CatalogConnectionRoute | None,
        events: list[str],
        *,
        subsequent: tuple[CatalogConnectionRoute | None, ...] = (),
    ) -> None:
        self.routes = [route, *subsequent]
        self.events = events

    def load_route(
        self,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        *,
        refresh_id: CatalogRefreshId,
        indexer_id: str,
        lease_capability: str,
        fencing_token: int,
    ) -> ManagedCatalogConnectorRoute | None:
        assert workspace_id == "workspace_alpha"
        assert connection_id == CatalogConnectionId("connection_primary")
        assert refresh_id == CatalogRefreshId("refresh_indexer_unit")
        assert indexer_id == "catalog_indexer_unit"
        assert lease_capability == CAPABILITY
        assert fencing_token == 1
        self.events.append("route")
        route = self.routes.pop(0) if len(self.routes) > 1 else self.routes[0]
        if route is None:
            return None
        return ManagedCatalogConnectorRoute(
            route=route,
            credential_binding_ref="binding_synthetic_alpha",
        )


class _Source:
    def __init__(
        self,
        pages: tuple[CatalogSourcePage, ...],
        events: list[str],
    ) -> None:
        self.pages = list(pages)
        self.events = events
        self.error: Exception | None = None
        self.checkpoints: list[str | None] = []
        self.stop_after_sequence: int | None = None
        self.stop_event: Event | None = None

    @property
    def source_label(self) -> str:
        return "synthetic:indexer-unit"

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        del route, mode
        sequence = self.pages[0].sequence if self.pages else 999
        self.events.append(f"source:{sequence}")
        self.checkpoints.append(checkpoint)
        assert page_size == 50
        if self.error is not None:
            raise self.error
        page = self.pages.pop(0)
        if self.stop_after_sequence == page.sequence:
            assert self.stop_event is not None
            self.stop_event.set()
        return page


class _TypedSourceError(CatalogInventoryError):
    def __init__(self, failure_code: CatalogRefreshFailureCode) -> None:
        self.failure_code = failure_code
        super().__init__(
            CatalogInventoryErrorCode.INVALID_RESPONSE,
            "sensitive vendor response must not escape",
        )


def _route(
    *,
    status: CatalogConnectionStatus = CatalogConnectionStatus.ENABLED,
    kind: CatalogConnectionKind = CatalogConnectionKind.SYNTHETIC,
) -> CatalogConnectionRoute:
    return CatalogConnectionRoute(
        workspace_id="workspace_alpha",
        connection_id=CatalogConnectionId("connection_primary"),
        kind=kind,
        environment="PROD",
        catalog_scope="synthetic-scale",
        status=status,
        catalog_identity_fingerprint=SHA_B,
        contract_version=1,
        route_revision=1,
        target_fingerprint=SHA_A,
    )


def _indexer(
    store: _MemoryRefreshStore,
    routes: _Routes,
    source: _Source,
    *,
    max_pages: int | None = None,
    kinds: tuple[CatalogConnectionKind, ...] = (CatalogConnectionKind.SYNTHETIC,),
    stop_event: Event | None = None,
) -> RunOneCatalogRefresh:
    stopping = stop_event or Event()
    return RunOneCatalogRefresh(
        refreshes=store,
        routes=routes,
        sources=KindCatalogSourceResolver({kind: source for kind in kinds}),
        capability_factory=lambda: CAPABILITY,
        indexer_id="catalog_indexer_unit",
        page_size=50,
        max_pages_per_refresh=max_pages,
        stop_requested=stopping.is_set,
    )


def test_indexer_persists_each_page_before_requesting_the_next() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source(
        (
            _page(1, next_checkpoint="checkpoint-1", complete=False),
            _page(2, next_checkpoint=None, complete=True),
        ),
        events,
    )
    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.COMPLETED
    assert result.pages_processed == 2
    assert result.total_pages == 2
    assert result.asset_count == 2
    assert result.field_count == 2
    assert source.checkpoints == [None, "checkpoint-1"]
    assert events == [
        "reclaim:100",
        "prune:100",
        "claim",
        "route",
        "heartbeat",
        "begin",
        "heartbeat",
        "route",
        "source:1",
        "heartbeat",
        "persist:1",
        "heartbeat",
        "route",
        "source:2",
        "heartbeat",
        "persist:2",
        "heartbeat",
        "route",
        "complete",
    ]
    assert store.complete_kwargs is not None
    assert "catalog_fingerprint" not in store.complete_kwargs
    assert store.complete_kwargs["expected_base_generation"] == 0
    assert store.complete_kwargs["expected_contract_version"] == 1
    assert store.complete_kwargs["expected_route_revision"] == 1
    assert store.complete_kwargs["expected_target_fingerprint"] == SHA_A


def test_indexer_resumes_from_durable_checkpoint_without_replaying_prior_page() -> None:
    events: list[str] = []
    requested = _requested_state(
        source_page_count=1,
        source_page_fingerprint="1" * 64,
        source_checkpoint="checkpoint-1",
        staged_asset_count=1,
        staged_field_count=1,
    )
    store = _MemoryRefreshStore(requested, events)
    source = _Source((_page(2, next_checkpoint=None, complete=True),), events)

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.pages_processed == 1
    assert result.total_pages == 2
    assert source.checkpoints == ["checkpoint-1"]
    assert "source:1" not in events


def test_indexer_completes_a_durable_final_page_without_reading_source_again() -> None:
    events: list[str] = []
    requested = _requested_state(
        source_page_count=1,
        source_page_fingerprint="1" * 64,
        staged_asset_count=1,
        staged_field_count=1,
        source_complete=True,
    )
    store = _MemoryRefreshStore(requested, events)
    source = _Source((), events)

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.COMPLETED
    assert result.pages_processed == 0
    assert result.total_pages == 1
    assert source.checkpoints == []
    assert events == [
        "reclaim:100",
        "prune:100",
        "claim",
        "route",
        "heartbeat",
        "begin",
        "heartbeat",
        "route",
        "complete",
    ]


def test_capacity_rejection_during_promotion_is_recorded_as_terminal_failure() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    store.complete_error = CatalogInventoryError(
        CatalogInventoryErrorCode.CAPACITY_EXCEEDED,
        "sensitive capacity policy detail",
    )
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.FAILED
    assert result.status is CatalogRefreshStatus.FAILED
    assert result.failure_code is CatalogRefreshFailureCode.CAPACITY_EXCEEDED
    assert result.pages_processed == 1
    assert result.total_pages == 1
    assert events[-3:] == [
        "complete",
        "heartbeat",
        "fail:catalog_capacity_exceeded",
    ]
    assert store.state is not None
    assert store.state.status is CatalogRefreshStatus.FAILED
    assert store.state.lease is None


def test_idle_iteration_does_not_resolve_route_or_source() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.IDLE
    assert events == ["reclaim:100", "prune:100", "claim"]


def test_stop_during_source_page_commits_that_page_and_leaves_resume_checkpoint() -> None:
    events: list[str] = []
    stopping = Event()
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source(
        (
            _page(1, next_checkpoint="checkpoint-1", complete=False),
            _page(2, next_checkpoint=None, complete=True),
        ),
        events,
    )
    source.stop_after_sequence = 1
    source.stop_event = stopping

    result = _indexer(
        store,
        _Routes(_route(), events),
        source,
        stop_event=stopping,
    ).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.STOPPED
    assert result.status is CatalogRefreshStatus.STAGING
    assert result.pages_processed == 1
    assert result.total_pages == 1
    assert store.state is not None
    assert store.state.status is CatalogRefreshStatus.STAGING
    assert store.state.source_checkpoint == "checkpoint-1"
    assert store.state.source_page_count == 1
    assert not store.state.source_complete
    assert source.checkpoints == [None]
    assert "persist:1" in events
    assert "source:2" not in events
    assert "complete" not in events
    assert not any(event.startswith("fail:") for event in events)

    store.state = _replace(store.state, status=CatalogRefreshStatus.REQUESTED, lease=None)
    stopping.clear()
    resumed = _indexer(
        store,
        _Routes(_route(), events),
        source,
        stop_event=stopping,
    ).execute()

    assert resumed.outcome is CatalogIndexerIterationOutcome.COMPLETED
    assert resumed.pages_processed == 1
    assert resumed.total_pages == 2
    assert source.checkpoints == [None, "checkpoint-1"]


def test_stop_before_first_source_read_leaves_empty_staging_generation() -> None:
    events: list[str] = []
    stopping = Event()
    stopping.set()
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

    result = _indexer(
        store,
        _Routes(_route(), events),
        source,
        stop_event=stopping,
    ).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.STOPPED
    assert result.pages_processed == 0
    assert result.total_pages == 0
    assert not any(event.startswith("source:") for event in events)
    assert not any(event.startswith("persist:") for event in events)


def test_operation_timeouts_require_a_full_renewal_margin() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)
    kwargs = {
        "refreshes": store,
        "routes": _Routes(_route(), events),
        "sources": KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        "capability_factory": lambda: CAPABILITY,
        "indexer_id": "catalog_indexer_unit",
        "source_operation_timeout": timedelta(seconds=15),
        "store_operation_timeout": timedelta(seconds=7),
    }

    with pytest.raises(ValueError, match="operation-renewal margin"):
        RunOneCatalogRefresh(
            **kwargs,  # type: ignore[arg-type]
            lease_duration=timedelta(seconds=26),
        )

    indexer = RunOneCatalogRefresh(
        **kwargs,  # type: ignore[arg-type]
        lease_duration=timedelta(seconds=27),
    )
    assert indexer.lease_duration == timedelta(seconds=27)


def test_indexer_identifier_matches_postgres_lease_owner_contract() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)
    kwargs = {
        "refreshes": store,
        "routes": _Routes(_route(), events),
        "sources": KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        "capability_factory": lambda: CAPABILITY,
    }

    indexer = RunOneCatalogRefresh(
        **kwargs,  # type: ignore[arg-type]
        indexer_id="catalog:indexer.prod",
    )
    assert indexer.indexer_id == "catalog:indexer.prod"
    with pytest.raises(ValueError, match="bounded inert identifier"):
        RunOneCatalogRefresh(
            **kwargs,  # type: ignore[arg-type]
            indexer_id="1indexer",
        )


@pytest.mark.parametrize(
    ("heartbeat_error_at", "page_was_persisted"),
    [(3, False), (4, True)],
)
def test_lease_loss_after_source_read_or_before_completion_fails_closed(
    heartbeat_error_at: int,
    page_was_persisted: bool,
) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    store.heartbeat_error_at = heartbeat_error_at
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        _indexer(store, _Routes(_route(), events), source).execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.LEASE_LOST
    assert "sensitive stale lease detail" not in str(captured.value)
    assert ("persist:1" in events) is page_was_persisted
    assert "complete" not in events
    assert not any(event.startswith("fail:") for event in events)


@pytest.mark.parametrize(
    "stop_requested",
    [
        lambda: "yes",
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive stop failure")),
    ],
)
def test_invalid_stop_signal_is_sanitized(
    stop_requested: object,
) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)
    indexer = RunOneCatalogRefresh(
        refreshes=store,
        routes=_Routes(_route(), events),
        sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        capability_factory=lambda: CAPABILITY,
        indexer_id="catalog_indexer_unit",
        stop_requested=stop_requested,  # type: ignore[arg-type]
    )

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        indexer.execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.INVALID_STOP_SIGNAL
    assert "sensitive stop failure" not in str(captured.value)
    assert not any(event.startswith("source:") for event in events)


def test_maintenance_reclaims_and_prunes_before_claim_with_a_bounded_batch() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    store.reclaim_result = 7
    store.prune_result = 5
    source = _Source((), events)
    indexer = RunOneCatalogRefresh(
        refreshes=store,
        routes=_Routes(_route(), events),
        sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        capability_factory=lambda: CAPABILITY,
        indexer_id="catalog_indexer_unit",
        maintenance_batch_size=17,
    )

    result = indexer.execute()

    assert result.outcome is CatalogIndexerIterationOutcome.IDLE
    assert events == ["reclaim:17", "prune:17", "claim"]


@pytest.mark.parametrize("operation", ["reclaim", "prune"])
def test_maintenance_failure_is_sanitized_and_stops_before_claim(operation: str) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    error = CatalogInventoryError(
        CatalogInventoryErrorCode.UNAVAILABLE,
        "sensitive maintenance failure",
    )
    if operation == "reclaim":
        store.reclaim_error = error
    else:
        store.prune_error = error
    source = _Source((), events)

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        _indexer(store, _Routes(_route(), events), source).execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.STORE_UNAVAILABLE
    assert "sensitive maintenance failure" not in str(captured.value)
    assert "claim" not in events


@pytest.mark.parametrize("invalid_count", [-1, 101, True, "1"])
def test_invalid_maintenance_count_fails_closed(invalid_count: object) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    store.reclaim_result = invalid_count
    source = _Source((), events)

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        _indexer(store, _Routes(_route(), events), source).execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.INVALID_STORE_RESPONSE
    assert events == ["reclaim:100"]


@pytest.mark.parametrize("batch_size", [0, 101])
def test_maintenance_batch_configuration_is_bounded(batch_size: int) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)

    with pytest.raises(ValueError, match="maintenance batch size"):
        RunOneCatalogRefresh(
            refreshes=store,
            routes=_Routes(_route(), events),
            sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
            capability_factory=lambda: CAPABILITY,
            indexer_id="catalog_indexer_unit",
            maintenance_batch_size=batch_size,
        )


def test_typed_source_failure_is_durably_sanitized() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)
    source.error = _TypedSourceError(CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE)

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.FAILED
    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_RESPONSE_TOO_LARGE
    assert events[-1] == "fail:source_response_too_large"
    assert "sensitive vendor" not in repr(result)


def test_missing_or_disabled_route_fails_without_source_resolution() -> None:
    for route in (None, _route(status=CatalogConnectionStatus.DISABLED)):
        events: list[str] = []
        store = _MemoryRefreshStore(_requested_state(), events)
        source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

        result = _indexer(store, _Routes(route, events), source).execute()

        assert result.failure_code is CatalogRefreshFailureCode.CONNECTION_DISABLED
        assert not any(event.startswith("source:") for event in events)


def test_governed_route_rotation_fails_before_the_next_source_page() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source(
        (
            _page(1, next_checkpoint="checkpoint-1", complete=False),
            _page(2, next_checkpoint=None, complete=True),
        ),
        events,
    )
    initial = _route().model_copy(
        update={
            "contract_version": 1,
            "route_revision": 1,
            "target_fingerprint": "a" * 64,
        }
    )
    rotated = initial.model_copy(
        update={
            "route_revision": 2,
            "target_fingerprint": "b" * 64,
        }
    )

    result = _indexer(
        store,
        _Routes(initial, events, subsequent=(initial, rotated)),
        source,
    ).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.FAILED
    assert result.failure_code is CatalogRefreshFailureCode.FINGERPRINT_MISMATCH
    assert result.pages_processed == 1
    assert source.checkpoints == [None]
    assert "source:2" not in events
    assert "complete" not in events


def test_governed_route_disable_fails_after_final_page_before_promotion() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)
    initial = _route().model_copy(
        update={
            "contract_version": 1,
            "route_revision": 1,
            "target_fingerprint": "a" * 64,
        }
    )

    result = _indexer(
        store,
        _Routes(initial, events, subsequent=(initial, None)),
        source,
    ).execute()

    assert result.outcome is CatalogIndexerIterationOutcome.FAILED
    assert result.failure_code is CatalogRefreshFailureCode.FINGERPRINT_MISMATCH
    assert result.pages_processed == 1
    assert "persist:1" in events
    assert "complete" not in events


def test_missing_source_kind_is_a_durable_source_unavailable_failure() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

    result = _indexer(
        store,
        _Routes(_route(kind=CatalogConnectionKind.DATAHUB_GRAPHQL), events),
        source,
    ).execute()

    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_UNAVAILABLE
    assert "begin" not in events


def test_changed_sequence_and_stalled_checkpoint_fail_before_persistence() -> None:
    events: list[str] = []
    requested = _requested_state(
        source_page_count=1,
        source_page_fingerprint="1" * 64,
        source_checkpoint="checkpoint-1",
        staged_asset_count=1,
        staged_field_count=1,
    )
    store = _MemoryRefreshStore(requested, events)
    source = _Source(
        (_page(2, next_checkpoint="checkpoint-1", complete=False),),
        events,
    )

    result = _indexer(store, _Routes(_route(), events), source).execute()

    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED
    assert "persist:2" not in events

    events = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(2, next_checkpoint=None, complete=True),), events)
    result = _indexer(store, _Routes(_route(), events), source).execute()
    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    assert "persist:2" not in events


def test_page_bound_fails_closed_without_requesting_another_page() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source(
        (
            _page(1, next_checkpoint="checkpoint-1", complete=False),
            _page(2, next_checkpoint=None, complete=True),
        ),
        events,
    )

    result = _indexer(
        store,
        _Routes(_route(), events),
        source,
        max_pages=1,
    ).execute()

    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED
    assert source.checkpoints == [None]
    assert "source:2" not in events


def test_page_bound_applies_to_the_whole_refresh_across_resume() -> None:
    events: list[str] = []
    requested = _requested_state(
        source_page_count=1,
        source_page_fingerprint="1" * 64,
        source_checkpoint="checkpoint-1",
        staged_asset_count=1,
        staged_field_count=1,
    )
    store = _MemoryRefreshStore(requested, events)
    source = _Source((_page(2, next_checkpoint=None, complete=True),), events)

    result = _indexer(
        store,
        _Routes(_route(), events),
        source,
        max_pages=1,
    ).execute()

    assert result.failure_code is CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED
    assert source.checkpoints == []
    assert not any(event.startswith("source:") for event in events)


def test_default_page_bound_covers_the_full_asset_policy_range() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)
    common = {
        "refreshes": store,
        "routes": _Routes(_route(), events),
        "sources": KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        "capability_factory": lambda: CAPABILITY,
        "indexer_id": "catalog_indexer_unit",
    }

    default = RunOneCatalogRefresh(**common)  # type: ignore[arg-type]
    smallest_pages = RunOneCatalogRefresh(
        **common,  # type: ignore[arg-type]
        page_size=1,
    )
    explicit = RunOneCatalogRefresh(
        **common,  # type: ignore[arg-type]
        max_pages_per_refresh=2_000_001,
    )

    assert default.effective_max_pages_per_refresh == 2_000_000
    assert smallest_pages.effective_max_pages_per_refresh == 100_000_000
    assert explicit.effective_max_pages_per_refresh == 2_000_001
    with pytest.raises(ValueError, match="page bound"):
        RunOneCatalogRefresh(
            **common,  # type: ignore[arg-type]
            max_pages_per_refresh=100_000_001,
        )


def test_ambiguous_persist_failure_is_not_misreported_as_durable_source_failure() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    store.persist_error = CatalogInventoryError(
        CatalogInventoryErrorCode.UNAVAILABLE,
        "sensitive database failure",
    )
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        _indexer(store, _Routes(_route(), events), source).execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.STORE_UNAVAILABLE
    assert not any(event.startswith("fail:") for event in events)
    assert "sensitive database" not in str(captured.value)


@pytest.mark.parametrize(
    "capability",
    [
        "short",
        "x" * 64,
        "contains whitespace and enough length 0123456789 ABCDEF",
        "\ninvalid-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    ],
)
def test_invalid_capability_stops_before_store_or_source(
    capability: str,
) -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(_requested_state(), events)
    source = _Source((_page(1, next_checkpoint=None, complete=True),), events)
    indexer = RunOneCatalogRefresh(
        refreshes=store,
        routes=_Routes(_route(), events),
        sources=KindCatalogSourceResolver({CatalogConnectionKind.SYNTHETIC: source}),
        capability_factory=lambda: capability,
        indexer_id="catalog_indexer_unit",
    )

    with pytest.raises(CatalogIndexerUseCaseError) as captured:
        indexer.execute()

    assert captured.value.code is CatalogIndexerUseCaseErrorCode.INVALID_CAPABILITY
    assert events == []


def test_indexer_repr_and_result_exclude_dependencies_and_capability() -> None:
    events: list[str] = []
    store = _MemoryRefreshStore(None, events)
    source = _Source((), events)
    indexer = _indexer(store, _Routes(_route(), events), source)

    rendered = repr(indexer)
    result = indexer.execute()

    assert CAPABILITY not in rendered
    assert "credential_binding_ref" not in rendered
    assert "sources=" not in rendered
    assert CAPABILITY not in repr(result)
