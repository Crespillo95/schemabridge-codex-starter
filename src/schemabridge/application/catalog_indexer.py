"""One bounded catalog-indexer iteration over durable refresh generations."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from schemabridge.application.ports.catalog_inventory import (
    CatalogConnectionRoutePort,
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    CatalogRefreshStorePort,
    CatalogSourcePort,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CATALOG_INDEXER_ID_PATTERN,
    MAX_INVENTORY_PAGE_SIZE,
    MAX_TENANT_ASSET_LIMIT,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshFailureCode,
    CatalogRefreshState,
    CatalogRefreshStatus,
    CatalogSourcePage,
)

_SAFE_INDEXER_ID = re.compile(CATALOG_INDEXER_ID_PATTERN)
_MINIMUM_CAPABILITY_BYTES = 32
_MINIMUM_CAPABILITY_DISTINCT_BYTES = 8
_MAXIMUM_CAPABILITY_BYTES = 1_024
_LEASE_RENEWAL_MARGIN = timedelta(seconds=5)


def _never_stop() -> bool:
    return False


class CatalogLeaseCapabilityFactoryPort(Protocol):
    """Generate a transient capability that is never returned or logged."""

    def __call__(self) -> str:
        """Return fresh high-entropy text."""


class CatalogStopRequestedPort(Protocol):
    """Read one process-local cooperative shutdown signal."""

    def __call__(self) -> bool:
        """Return true after the process has requested a bounded stop."""


class CatalogSourceResolverPort(Protocol):
    """Resolve a mutation-free source for one exact lease-bound route."""

    def resolve(self, route: ManagedCatalogConnectorRoute) -> CatalogSourcePort:
        """Return the exact source composed for this route."""


@dataclass(frozen=True, slots=True)
class KindCatalogSourceResolver:
    """Small immutable source registry; concrete credentials remain inside adapters."""

    sources: Mapping[CatalogConnectionKind, CatalogSourcePort] = field(repr=False)

    def __post_init__(self) -> None:
        normalized = dict(self.sources)
        if not normalized or any(
            not isinstance(kind, CatalogConnectionKind) for kind in normalized
        ):
            raise ValueError("catalog source registry is invalid")
        object.__setattr__(self, "sources", MappingProxyType(normalized))

    def resolve(self, route: ManagedCatalogConnectorRoute) -> CatalogSourcePort:
        try:
            return self.sources[route.route.kind]
        except KeyError:
            raise CatalogInventoryError(
                CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
                "catalog source is unavailable",
            ) from None


class CatalogIndexerIterationOutcome(StrEnum):
    """Bounded process outcomes with no route, checkpoint, or capability material."""

    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CatalogIndexerIterationResult:
    """Sanitized observable result for one claimed refresh."""

    outcome: CatalogIndexerIterationOutcome
    status: CatalogRefreshStatus | None = None
    failure_code: CatalogRefreshFailureCode | None = None
    pages_processed: int = 0
    total_pages: int = 0
    asset_count: int = 0
    field_count: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.pages_processed,
                self.total_pages,
                self.asset_count,
                self.field_count,
            )
            < 0
        ):
            raise ValueError("catalog indexer result counts cannot be negative")
        if self.pages_processed > self.total_pages:
            raise ValueError("processed pages cannot exceed total refresh pages")
        if self.outcome is CatalogIndexerIterationOutcome.IDLE:
            if (
                self.status is not None
                or self.failure_code is not None
                or self.pages_processed
                or self.total_pages
                or self.asset_count
                or self.field_count
            ):
                raise ValueError("idle catalog result cannot expose refresh state")
        elif self.outcome is CatalogIndexerIterationOutcome.COMPLETED:
            if self.status is not CatalogRefreshStatus.COMPLETED or self.failure_code is not None:
                raise ValueError("completed catalog result has invalid status")
        elif self.outcome is CatalogIndexerIterationOutcome.STOPPED:
            if self.status is not CatalogRefreshStatus.STAGING or self.failure_code is not None:
                raise ValueError("stopped catalog result must retain staging state")
        elif self.status is not CatalogRefreshStatus.FAILED or self.failure_code is None:
            raise ValueError("failed catalog result requires one safe failure code")


class CatalogIndexerUseCaseErrorCode(StrEnum):
    """Failures where no trustworthy terminal transition can be claimed."""

    STORE_UNAVAILABLE = "catalog_indexer_store_unavailable"
    INVALID_CAPABILITY = "catalog_indexer_invalid_capability"
    INVALID_CLAIM = "catalog_indexer_invalid_claim"
    LEASE_LOST = "catalog_indexer_lease_lost"
    INVALID_STORE_RESPONSE = "catalog_indexer_invalid_store_response"
    INVALID_STOP_SIGNAL = "catalog_indexer_invalid_stop_signal"


class CatalogIndexerUseCaseError(RuntimeError):
    """Sanitized iteration failure that never contains an infrastructure payload."""

    def __init__(self, code: CatalogIndexerUseCaseErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RunOneCatalogRefresh:
    """Claim and stream one refresh with constant application-memory behavior."""

    refreshes: CatalogRefreshStorePort = field(repr=False)
    routes: CatalogConnectionRoutePort = field(repr=False)
    sources: CatalogSourceResolverPort = field(repr=False)
    capability_factory: CatalogLeaseCapabilityFactoryPort = field(repr=False)
    indexer_id: str
    lease_duration: timedelta = timedelta(minutes=2)
    source_operation_timeout: timedelta = timedelta(seconds=15)
    store_operation_timeout: timedelta = timedelta(seconds=7)
    page_size: int = MAX_INVENTORY_PAGE_SIZE
    max_pages_per_refresh: int | None = None
    maintenance_batch_size: int = 100
    stop_requested: CatalogStopRequestedPort = field(
        default=_never_stop,
        repr=False,
    )

    def __post_init__(self) -> None:
        if _SAFE_INDEXER_ID.fullmatch(self.indexer_id) is None:
            raise ValueError("catalog indexer id must be a bounded inert identifier")
        if not timedelta(seconds=10) <= self.lease_duration <= timedelta(minutes=5):
            raise ValueError("catalog indexer lease duration is outside the supported bound")
        if (
            not timedelta(milliseconds=100)
            <= self.source_operation_timeout
            <= timedelta(seconds=30)
        ):
            raise ValueError("catalog source operation timeout is outside the supported bound")
        if not timedelta(milliseconds=150) <= self.store_operation_timeout <= timedelta(seconds=90):
            raise ValueError("catalog store operation timeout is outside the supported bound")
        minimum_lease = max(
            self.source_operation_timeout + self.store_operation_timeout + _LEASE_RENEWAL_MARGIN,
            self.store_operation_timeout * 2 + _LEASE_RENEWAL_MARGIN,
        )
        if self.lease_duration < minimum_lease:
            raise ValueError("catalog lease does not leave a safe operation-renewal margin")
        if not 1 <= self.page_size <= MAX_INVENTORY_PAGE_SIZE:
            raise ValueError("catalog source page size is outside the supported bound")
        if self.max_pages_per_refresh is not None and not (
            1 <= self.max_pages_per_refresh <= MAX_TENANT_ASSET_LIMIT
        ):
            raise ValueError("catalog refresh page bound is outside the supported range")
        if not 1 <= self.maintenance_batch_size <= 100:
            raise ValueError("catalog maintenance batch size is outside the supported range")

    @property
    def effective_max_pages_per_refresh(self) -> int:
        """Cover the full tenant asset-policy range unless an operator narrows it."""

        if self.max_pages_per_refresh is not None:
            return self.max_pages_per_refresh
        return (MAX_TENANT_ASSET_LIMIT + self.page_size - 1) // self.page_size

    def execute(self) -> CatalogIndexerIterationResult:
        """Process at most one refresh and persist every page before requesting another."""

        capability, capability_digest = _new_capability(self.capability_factory)
        self._maintain()
        claimed = self._claim(capability)
        if claimed is None:
            return CatalogIndexerIterationResult(outcome=CatalogIndexerIterationOutcome.IDLE)
        try:
            _require_owned_state(
                claimed,
                expected_status=CatalogRefreshStatus.LEASED,
                indexer_id=self.indexer_id,
                capability_digest=capability_digest,
            )
        except CatalogIndexerUseCaseError:
            raise CatalogIndexerUseCaseError(
                CatalogIndexerUseCaseErrorCode.INVALID_CLAIM,
                "catalog refresh claim is invalid",
            ) from None
        starting_page_count = claimed.source_page_count
        managed_route = self._load_route(claimed, capability=capability)
        claimed = self._heartbeat(
            claimed,
            capability=capability,
            capability_digest=capability_digest,
            expected_status=CatalogRefreshStatus.LEASED,
        )
        if managed_route is None:
            return self._record_source_failure(
                claimed,
                capability=capability,
                capability_digest=capability_digest,
                code=CatalogRefreshFailureCode.CONNECTION_DISABLED,
                pages_processed=0,
            )
        try:
            source = self.sources.resolve(managed_route)
        except Exception as error:
            return self._record_source_failure(
                claimed,
                capability=capability,
                capability_digest=capability_digest,
                code=_source_failure_code(error),
                pages_processed=0,
            )
        route = managed_route.route
        staged = self._begin_staging(
            claimed,
            capability=capability,
            capability_digest=capability_digest,
        )
        current = staged
        stopped = self._stopped_result(
            current,
            starting_page_count=starting_page_count,
        )
        if stopped is not None:
            return stopped
        if current.source_complete:
            current = self._heartbeat(
                current,
                capability=capability,
                capability_digest=capability_digest,
            )
            if not self._route_remains_current(
                current,
                managed_route,
                capability=capability,
            ):
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                    pages_processed=current.source_page_count - starting_page_count,
                )
            return self._complete_result(
                current,
                route=route,
                capability=capability,
                capability_digest=capability_digest,
                starting_page_count=starting_page_count,
            )
        while True:
            pages_processed = current.source_page_count - starting_page_count
            stopped = self._stopped_result(
                current,
                starting_page_count=starting_page_count,
            )
            if stopped is not None:
                return stopped
            if current.source_page_count >= self.effective_max_pages_per_refresh:
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED,
                    pages_processed=pages_processed,
                )
            current = self._heartbeat(
                current,
                capability=capability,
                capability_digest=capability_digest,
            )
            if not self._route_remains_current(
                current,
                managed_route,
                capability=capability,
            ):
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                    pages_processed=pages_processed,
                )
            try:
                page = source.read_page(
                    route,
                    mode=current.mode,
                    checkpoint=current.source_checkpoint,
                    page_size=self.page_size,
                )
            except Exception as error:
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=_source_failure_code(error),
                    pages_processed=pages_processed,
                )
            failure = _validate_source_progress(current, page)
            if failure is not None:
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=failure,
                    pages_processed=pages_processed,
                )
            current = self._heartbeat(
                current,
                capability=capability,
                capability_digest=capability_digest,
            )
            current = self._persist_page(
                current,
                page,
                capability=capability,
                capability_digest=capability_digest,
            )
            pages_processed = current.source_page_count - starting_page_count
            stopped = self._stopped_result(
                current,
                starting_page_count=starting_page_count,
            )
            if stopped is not None:
                return stopped
            if not page.source_complete:
                continue
            current = self._heartbeat(
                current,
                capability=capability,
                capability_digest=capability_digest,
            )
            if not self._route_remains_current(
                current,
                managed_route,
                capability=capability,
            ):
                return self._record_source_failure(
                    current,
                    capability=capability,
                    capability_digest=capability_digest,
                    code=CatalogRefreshFailureCode.FINGERPRINT_MISMATCH,
                    pages_processed=pages_processed,
                )
            return self._complete_result(
                current,
                route=route,
                capability=capability,
                capability_digest=capability_digest,
                starting_page_count=starting_page_count,
            )

    def _maintain(self) -> None:
        """Recover expired ownership and enforce retention before every claim."""

        try:
            reclaimed = self.refreshes.reclaim_expired(
                limit=self.maintenance_batch_size,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        _require_maintenance_count(reclaimed, limit=self.maintenance_batch_size)

        try:
            pruned = self.refreshes.prune_due_generations(
                limit=self.maintenance_batch_size,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        _require_maintenance_count(pruned, limit=self.maintenance_batch_size)

    def _claim(self, capability: str) -> CatalogRefreshState | None:
        try:
            return self.refreshes.claim_next(
                indexer_id=self.indexer_id,
                lease_capability=capability,
                lease_duration=self.lease_duration,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None

    def _load_route(
        self,
        state: CatalogRefreshState,
        *,
        capability: str,
    ) -> ManagedCatalogConnectorRoute | None:
        if state.lease is None:
            raise _invalid_store_response()
        try:
            route = self.routes.load_route(
                state.workspace_id,
                state.connection_id,
                refresh_id=state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        if (
            route is None
            or route.route.workspace_id != state.workspace_id
            or route.route.connection_id != state.connection_id
            or route.route.status is not CatalogConnectionStatus.ENABLED
        ):
            return None
        return route

    def _route_remains_current(
        self,
        state: CatalogRefreshState,
        expected: ManagedCatalogConnectorRoute,
        *,
        capability: str,
    ) -> bool:
        """Revalidate governed v9 target identity immediately around source I/O."""

        if expected.route.target_fingerprint is None:
            return True
        current = self._load_route(state, capability=capability)
        return current == expected

    def _begin_staging(
        self,
        state: CatalogRefreshState,
        *,
        capability: str,
        capability_digest: str,
    ) -> CatalogRefreshState:
        assert state.lease is not None
        try:
            staged = self.refreshes.begin_staging(
                state.workspace_id,
                state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        _require_owned_state(
            staged,
            expected_status=CatalogRefreshStatus.STAGING,
            indexer_id=self.indexer_id,
            capability_digest=capability_digest,
            expected=state,
        )
        return staged

    def _heartbeat(
        self,
        state: CatalogRefreshState,
        *,
        capability: str,
        capability_digest: str,
        expected_status: CatalogRefreshStatus = CatalogRefreshStatus.STAGING,
    ) -> CatalogRefreshState:
        assert state.lease is not None
        try:
            renewed = self.refreshes.heartbeat(
                state.workspace_id,
                state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
                lease_duration=self.lease_duration,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        _require_owned_state(
            renewed,
            expected_status=expected_status,
            indexer_id=self.indexer_id,
            capability_digest=capability_digest,
            expected=state,
        )
        return renewed

    def _persist_page(
        self,
        state: CatalogRefreshState,
        page: CatalogSourcePage,
        *,
        capability: str,
        capability_digest: str,
    ) -> CatalogRefreshState:
        assert state.lease is not None
        try:
            persisted = self.refreshes.persist_page(
                state.workspace_id,
                state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
                page=page,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        _require_owned_state(
            persisted,
            expected_status=CatalogRefreshStatus.STAGING,
            indexer_id=self.indexer_id,
            capability_digest=capability_digest,
            expected=state,
            preserve_progress=False,
        )
        if (
            persisted.source_page_count != state.source_page_count + 1
            or persisted.source_page_fingerprint != page.page_fingerprint
            or persisted.source_checkpoint != page.next_checkpoint
            or persisted.source_complete is not page.source_complete
        ):
            raise _invalid_store_response()
        return persisted

    def _complete(
        self,
        state: CatalogRefreshState,
        *,
        route: CatalogConnectionRoute,
        capability: str,
    ) -> CatalogRefreshState:
        assert state.lease is not None
        assert route.contract_version is not None
        assert route.route_revision is not None
        assert route.target_fingerprint is not None
        try:
            completed = self.refreshes.complete(
                state.workspace_id,
                state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
                expected_base_generation=state.base_generation,
                expected_contract_version=route.contract_version,
                expected_route_revision=route.route_revision,
                expected_target_fingerprint=route.target_fingerprint,
            )
        except CatalogInventoryError as error:
            if error.code is CatalogInventoryErrorCode.CAPACITY_EXCEEDED:
                raise
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        if (
            completed.refresh_id != state.refresh_id
            or completed.workspace_id != state.workspace_id
            or completed.connection_id != state.connection_id
            or completed.status is not CatalogRefreshStatus.COMPLETED
            or completed.target_generation != state.target_generation
            or completed.source_page_count != state.source_page_count
            or not completed.source_complete
            or completed.catalog_fingerprint is None
            or completed.lease is not None
        ):
            raise _invalid_store_response()
        return completed

    def _complete_result(
        self,
        state: CatalogRefreshState,
        *,
        route: CatalogConnectionRoute,
        capability: str,
        capability_digest: str,
        starting_page_count: int,
    ) -> CatalogIndexerIterationResult:
        try:
            completed = self._complete(
                state,
                route=route,
                capability=capability,
            )
        except CatalogInventoryError as error:
            if error.code is not CatalogInventoryErrorCode.CAPACITY_EXCEEDED:
                raise _store_error(error) from None
            return self._record_source_failure(
                state,
                capability=capability,
                capability_digest=capability_digest,
                code=CatalogRefreshFailureCode.CAPACITY_EXCEEDED,
                pages_processed=state.source_page_count - starting_page_count,
            )
        return CatalogIndexerIterationResult(
            outcome=CatalogIndexerIterationOutcome.COMPLETED,
            status=completed.status,
            pages_processed=completed.source_page_count - starting_page_count,
            total_pages=completed.source_page_count,
            asset_count=completed.staged_asset_count,
            field_count=completed.staged_field_count,
        )

    def _stopped_result(
        self,
        state: CatalogRefreshState,
        *,
        starting_page_count: int,
    ) -> CatalogIndexerIterationResult | None:
        try:
            requested = self.stop_requested()
        except Exception:
            raise CatalogIndexerUseCaseError(
                CatalogIndexerUseCaseErrorCode.INVALID_STOP_SIGNAL,
                "catalog stop signal is unavailable",
            ) from None
        if type(requested) is not bool:
            raise CatalogIndexerUseCaseError(
                CatalogIndexerUseCaseErrorCode.INVALID_STOP_SIGNAL,
                "catalog stop signal is invalid",
            )
        if not requested:
            return None
        if state.status is not CatalogRefreshStatus.STAGING or state.lease is None:
            raise _invalid_store_response()
        return CatalogIndexerIterationResult(
            outcome=CatalogIndexerIterationOutcome.STOPPED,
            status=state.status,
            pages_processed=state.source_page_count - starting_page_count,
            total_pages=state.source_page_count,
            asset_count=state.staged_asset_count,
            field_count=state.staged_field_count,
        )

    def _record_source_failure(
        self,
        state: CatalogRefreshState,
        *,
        capability: str,
        capability_digest: str,
        code: CatalogRefreshFailureCode,
        pages_processed: int,
    ) -> CatalogIndexerIterationResult:
        if state.lease is None:
            raise _invalid_store_response()
        if state.lease.indexer_id != self.indexer_id or not hmac.compare_digest(
            state.lease.capability_digest,
            capability_digest,
        ):
            raise _invalid_store_response()
        if state.status not in {
            CatalogRefreshStatus.LEASED,
            CatalogRefreshStatus.STAGING,
        }:
            raise _invalid_store_response()
        state = self._heartbeat(
            state,
            capability=capability,
            capability_digest=capability_digest,
            expected_status=state.status,
        )
        assert state.lease is not None
        try:
            failed = self.refreshes.fail(
                state.workspace_id,
                state.refresh_id,
                indexer_id=self.indexer_id,
                lease_capability=capability,
                fencing_token=state.lease.fencing_token,
                code=code,
            )
        except CatalogInventoryError as error:
            raise _store_error(error) from None
        except Exception:
            raise _store_unavailable() from None
        if (
            failed.refresh_id != state.refresh_id
            or failed.workspace_id != state.workspace_id
            or failed.connection_id != state.connection_id
            or failed.status is not CatalogRefreshStatus.FAILED
            or failed.failure_code is not code
            or failed.lease is not None
        ):
            raise _invalid_store_response()
        return CatalogIndexerIterationResult(
            outcome=CatalogIndexerIterationOutcome.FAILED,
            status=failed.status,
            failure_code=failed.failure_code,
            pages_processed=pages_processed,
            total_pages=failed.source_page_count,
            asset_count=failed.staged_asset_count,
            field_count=failed.staged_field_count,
        )


def _new_capability(
    factory: CatalogLeaseCapabilityFactoryPort,
) -> tuple[str, str]:
    try:
        value = factory()
    except Exception:
        raise CatalogIndexerUseCaseError(
            CatalogIndexerUseCaseErrorCode.INVALID_CAPABILITY,
            "catalog lease capability could not be created",
        ) from None
    if not isinstance(value, str):
        raise _invalid_capability()
    encoded = value.encode("utf-8")
    if (
        not value
        or value.strip() != value
        or not _MINIMUM_CAPABILITY_BYTES <= len(encoded) <= _MAXIMUM_CAPABILITY_BYTES
        or len(set(encoded)) < _MINIMUM_CAPABILITY_DISTINCT_BYTES
        or any(byte < 0x21 or byte == 0x7F for byte in encoded)
    ):
        raise _invalid_capability()
    return value, hashlib.sha256(encoded).hexdigest()


def _require_owned_state(
    state: CatalogRefreshState,
    *,
    expected_status: CatalogRefreshStatus,
    indexer_id: str,
    capability_digest: str,
    expected: CatalogRefreshState | None = None,
    preserve_progress: bool = True,
) -> None:
    lease = state.lease
    if (
        state.status is not expected_status
        or lease is None
        or lease.indexer_id != indexer_id
        or not hmac.compare_digest(lease.capability_digest, capability_digest)
    ):
        raise _invalid_store_response()
    if expected is None:
        return
    expected_lease = expected.lease
    if (
        expected_lease is None
        or state.refresh_id != expected.refresh_id
        or state.workspace_id != expected.workspace_id
        or state.connection_id != expected.connection_id
        or state.mode is not expected.mode
        or state.base_generation != expected.base_generation
        or state.target_generation != expected.target_generation
        or lease.fencing_token != expected_lease.fencing_token
    ):
        raise _invalid_store_response()
    if preserve_progress and (
        state.source_page_count != expected.source_page_count
        or state.source_page_fingerprint != expected.source_page_fingerprint
        or state.source_checkpoint != expected.source_checkpoint
        or state.source_complete is not expected.source_complete
    ):
        raise _invalid_store_response()


def _validate_source_progress(
    state: CatalogRefreshState,
    page: CatalogSourcePage,
) -> CatalogRefreshFailureCode | None:
    if page.mode is not state.mode or page.sequence != state.source_page_count + 1:
        return CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
    if (
        state.source_page_fingerprint is not None
        and page.page_fingerprint == state.source_page_fingerprint
    ):
        return CatalogRefreshFailureCode.SOURCE_PAGE_REPEATED
    if not page.source_complete and page.next_checkpoint == state.source_checkpoint:
        return CatalogRefreshFailureCode.SOURCE_CURSOR_STALLED
    return None


def _require_maintenance_count(value: int, *, limit: int) -> None:
    if type(value) is not int or not 0 <= value <= limit:
        raise _invalid_store_response()


def _source_failure_code(error: Exception) -> CatalogRefreshFailureCode:
    observed = getattr(error, "failure_code", None)
    if isinstance(observed, CatalogRefreshFailureCode):
        return observed
    if isinstance(error, CatalogInventoryError):
        return {
            CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE: (
                CatalogRefreshFailureCode.SOURCE_UNAVAILABLE
            ),
            CatalogInventoryErrorCode.CONNECTION_DISABLED: (
                CatalogRefreshFailureCode.CONNECTION_DISABLED
            ),
            CatalogInventoryErrorCode.DELTA_UNSUPPORTED: (
                CatalogRefreshFailureCode.DELTA_UNSUPPORTED
            ),
            CatalogInventoryErrorCode.CAPACITY_EXCEEDED: (
                CatalogRefreshFailureCode.CAPACITY_EXCEEDED
            ),
            CatalogInventoryErrorCode.INVALID_RESPONSE: (
                CatalogRefreshFailureCode.SOURCE_INVALID_RESPONSE
            ),
        }.get(
            error.code,
            CatalogRefreshFailureCode.UNEXPECTED_INDEXER_FAILURE,
        )
    return CatalogRefreshFailureCode.UNEXPECTED_INDEXER_FAILURE


def _store_error(error: CatalogInventoryError) -> CatalogIndexerUseCaseError:
    if error.code is CatalogInventoryErrorCode.LEASE_CONFLICT:
        return CatalogIndexerUseCaseError(
            CatalogIndexerUseCaseErrorCode.LEASE_LOST,
            "catalog refresh lease is unavailable",
        )
    if error.code is CatalogInventoryErrorCode.INVALID_RESPONSE:
        return _invalid_store_response()
    return _store_unavailable()


def _invalid_capability() -> CatalogIndexerUseCaseError:
    return CatalogIndexerUseCaseError(
        CatalogIndexerUseCaseErrorCode.INVALID_CAPABILITY,
        "catalog lease capability is invalid",
    )


def _store_unavailable() -> CatalogIndexerUseCaseError:
    return CatalogIndexerUseCaseError(
        CatalogIndexerUseCaseErrorCode.STORE_UNAVAILABLE,
        "catalog refresh store is unavailable",
    )


def _invalid_store_response() -> CatalogIndexerUseCaseError:
    return CatalogIndexerUseCaseError(
        CatalogIndexerUseCaseErrorCode.INVALID_STORE_RESPONSE,
        "catalog refresh store returned invalid state",
    )
