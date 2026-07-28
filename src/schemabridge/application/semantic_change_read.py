"""Authenticated, tenant-scoped semantic-change read use cases."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from schemabridge.application.ports.semantic_change_read import (
    MAX_SEMANTIC_CHANGE_CURSOR_BYTES,
    MAX_SEMANTIC_CHANGE_PAGE_SIZE,
    SemanticChangeCursorBinding,
    SemanticChangeCursorError,
    SemanticChangeCursorPort,
    SemanticChangeCursorPosition,
    SemanticChangeFindingFilter,
    SemanticChangeFindingPublic,
    SemanticChangeImpactFilter,
    SemanticChangeImpactPublic,
    SemanticChangePage,
    SemanticChangePageKey,
    SemanticChangePageRequest,
    SemanticChangeReadClockPort,
    SemanticChangeReadPortError,
    SemanticChangeReadResource,
    SemanticChangeReadStorePort,
    SemanticChangeReportFilter,
    SemanticChangeReportPublic,
    SemanticChangeStorePage,
    semantic_change_read_fingerprint,
)
from schemabridge.domain.identity import AuthenticatedPrincipal

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_ID = re.compile(r"^report_[0-9a-f]{64}$")
_FINDING_ID = re.compile(r"^finding_[0-9a-f]{64}$")
_NO_CONTROL = re.compile(r"^[^\x00-\x1f\x7f]+$")
_REPORT_SORT_FINGERPRINT = semantic_change_read_fingerprint(
    {"keys": ["inspected_at_desc", "report_id"], "version": 1}
)
_FINDING_SORT_FINGERPRINT = semantic_change_read_fingerprint({"keys": ["finding_id"], "version": 1})
_IMPACT_SORT_FINGERPRINT = semantic_change_read_fingerprint(
    {"keys": ["kind", "artifact_id", "artifact_version"], "version": 1}
)
_ReadableT = TypeVar("_ReadableT")


class SemanticChangeReadErrorCode(StrEnum):
    INVALID_REQUEST = "semantic_change_invalid_request"
    UNAVAILABLE = "semantic_change_resource_unavailable"
    SERVICE_UNAVAILABLE = "semantic_change_service_unavailable"


class SemanticChangeReadError(RuntimeError):
    """Stable, non-disclosing failure for the authenticated HTTP boundary."""

    def __init__(self, code: SemanticChangeReadErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ListSemanticChangeReports:
    store: SemanticChangeReadStorePort
    cursors: SemanticChangeCursorPort
    clock: SemanticChangeReadClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        filters: SemanticChangeReportFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeReportPublic]:
        now = _authorize_reader(principal, self.clock)
        page = _valid_page_request(page)
        binding = SemanticChangeCursorBinding(
            workspace_id=principal.workspace_id,
            resource=SemanticChangeReadResource.REPORTS,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_REPORT_SORT_FINGERPRINT,
        )
        continuation = _decode(self.cursors, page.cursor, binding=binding, at=now)
        result = _call_store(
            lambda: self.store.list_reports(
                principal.workspace_id,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
            )
        )
        _validate_store_page(result, requested_size=page.size)
        for item in result.items:
            _validate_report(item, workspace_id=principal.workspace_id)
        return _public_page(
            result,
            binding=binding,
            cursors=self.cursors,
            at=now,
            continuation=continuation,
        )


@dataclass(frozen=True, slots=True)
class InspectSemanticChangeReport:
    store: SemanticChangeReadStorePort
    clock: SemanticChangeReadClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
    ) -> SemanticChangeReportPublic:
        _authorize_reader(principal, self.clock)
        return _load_report(self.store, principal.workspace_id, _valid_report_id(report_id))


@dataclass(frozen=True, slots=True)
class ListSemanticChangeFindings:
    store: SemanticChangeReadStorePort
    cursors: SemanticChangeCursorPort
    clock: SemanticChangeReadClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
        filters: SemanticChangeFindingFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeFindingPublic]:
        now = _authorize_reader(principal, self.clock)
        report_id = _valid_report_id(report_id)
        page = _valid_page_request(page)
        binding = SemanticChangeCursorBinding(
            workspace_id=principal.workspace_id,
            resource=SemanticChangeReadResource.FINDINGS,
            report_id=report_id,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_FINDING_SORT_FINGERPRINT,
        )
        continuation = _decode(self.cursors, page.cursor, binding=binding, at=now)
        _load_report(self.store, principal.workspace_id, report_id)
        result = _call_store(
            lambda: self.store.list_findings(
                principal.workspace_id,
                report_id,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
            )
        )
        _validate_store_page(result, requested_size=page.size)
        for item in result.items:
            _validate_finding(
                item,
                workspace_id=principal.workspace_id,
                report_id=report_id,
            )
        return _public_page(
            result,
            binding=binding,
            cursors=self.cursors,
            at=now,
            continuation=continuation,
        )


@dataclass(frozen=True, slots=True)
class ListSemanticChangeImpacts:
    store: SemanticChangeReadStorePort
    cursors: SemanticChangeCursorPort
    clock: SemanticChangeReadClockPort

    def execute(
        self,
        principal: AuthenticatedPrincipal,
        *,
        report_id: str,
        filters: SemanticChangeImpactFilter,
        page: SemanticChangePageRequest,
    ) -> SemanticChangePage[SemanticChangeImpactPublic]:
        now = _authorize_reader(principal, self.clock)
        report_id = _valid_report_id(report_id)
        page = _valid_page_request(page)
        binding = SemanticChangeCursorBinding(
            workspace_id=principal.workspace_id,
            resource=SemanticChangeReadResource.IMPACTS,
            report_id=report_id,
            filter_fingerprint=filters.fingerprint,
            sort_fingerprint=_IMPACT_SORT_FINGERPRINT,
        )
        continuation = _decode(self.cursors, page.cursor, binding=binding, at=now)
        _load_report(self.store, principal.workspace_id, report_id)
        result = _call_store(
            lambda: self.store.list_impacts(
                principal.workspace_id,
                report_id,
                filters=filters,
                page_size=page.size,
                after=None if continuation is None else continuation.last_key,
            )
        )
        _validate_store_page(result, requested_size=page.size)
        for item in result.items:
            _validate_impact(
                item,
                workspace_id=principal.workspace_id,
                report_id=report_id,
            )
        return _public_page(
            result,
            binding=binding,
            cursors=self.cursors,
            at=now,
            continuation=continuation,
        )


def _authorize_reader(
    principal: AuthenticatedPrincipal,
    clock: SemanticChangeReadClockPort,
) -> datetime:
    try:
        now = clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("semantic change clock returned a naive instant")
        if not principal.is_current(now) or not principal.roles:
            raise _unavailable()
    except SemanticChangeReadError:
        raise
    except (TypeError, ValueError) as error:
        raise _service_unavailable() from error
    return now


def _valid_report_id(value: str) -> str:
    if not isinstance(value, str) or _REPORT_ID.fullmatch(value) is None:
        raise SemanticChangeReadError(
            SemanticChangeReadErrorCode.INVALID_REQUEST,
            "semantic change request is invalid",
        )
    return value


def _valid_page_request(page: SemanticChangePageRequest) -> SemanticChangePageRequest:
    if (
        isinstance(page.size, bool)
        or not 1 <= page.size <= MAX_SEMANTIC_CHANGE_PAGE_SIZE
        or (
            page.cursor is not None
            and (
                not page.cursor
                or page.cursor.strip() != page.cursor
                or _NO_CONTROL.fullmatch(page.cursor) is None
                or len(page.cursor.encode("utf-8")) > MAX_SEMANTIC_CHANGE_CURSOR_BYTES
            )
        )
    ):
        raise SemanticChangeReadError(
            SemanticChangeReadErrorCode.INVALID_REQUEST,
            "semantic change request is invalid",
        )
    return page


def _load_report(
    store: SemanticChangeReadStorePort,
    workspace_id: str,
    report_id: str,
) -> SemanticChangeReportPublic:
    value = _call_store(lambda: store.load_report(workspace_id, report_id))
    if value is None:
        raise _unavailable()
    _validate_report(value, workspace_id=workspace_id, report_id=report_id)
    return value


def _validate_report(
    value: SemanticChangeReportPublic,
    *,
    workspace_id: str,
    report_id: str | None = None,
) -> None:
    if not isinstance(value, SemanticChangeReportPublic):
        raise _service_unavailable()
    baseline_pair = (value.baseline_revision is None) == (value.baseline_fingerprint is None)
    impact_counts = (
        value.mapping_impact_count,
        value.join_impact_count,
        value.workflow_impact_count,
        value.recipe_impact_count,
    )
    fingerprints = tuple(
        item
        for item in (
            value.pointer_fingerprint,
            value.registry_fingerprint,
            value.observation_fingerprint,
            value.impact_set_fingerprint,
            value.fingerprint,
            value.baseline_fingerprint,
        )
        if item is not None
    )
    if (
        value.workspace_id != workspace_id
        or _REPORT_ID.fullmatch(value.report_id) is None
        or (report_id is not None and value.report_id != report_id)
        or isinstance(value.pointer_generation, bool)
        or value.pointer_generation < 1
        or isinstance(value.registry_version, bool)
        or value.registry_version < 1
        or not baseline_pair
        or (
            value.baseline_revision is not None
            and (isinstance(value.baseline_revision, bool) or value.baseline_revision < 1)
        )
        or isinstance(value.catalog_generation_count, bool)
        or not 0 <= value.catalog_generation_count <= 100_000
        or isinstance(value.finding_count, bool)
        or not 0 <= value.finding_count <= 2_000
        or any(isinstance(count, bool) or count < 0 or count > 10_000 for count in impact_counts)
        or isinstance(value.dependency_watermark, bool)
        or value.dependency_watermark < 0
        or any(_SHA256.fullmatch(item) is None for item in fingerprints)
        or value.inspected_at.tzinfo is None
        or value.inspected_at.utcoffset() is None
    ):
        raise _service_unavailable()


def _validate_finding(
    value: SemanticChangeFindingPublic,
    *,
    workspace_id: str,
    report_id: str,
) -> None:
    if not isinstance(value, SemanticChangeFindingPublic):
        raise _service_unavailable()
    fingerprints = tuple(
        item
        for item in (
            value.previous_fingerprint,
            value.current_fingerprint,
            value.fingerprint,
        )
        if item is not None
    )
    if (
        value.workspace_id != workspace_id
        or value.report_id != report_id
        or _FINDING_ID.fullmatch(value.finding_id) is None
        or not value.target_id
        or len(value.target_id) > 200
        or _NO_CONTROL.fullmatch(value.target_id) is None
        or isinstance(value.target_version, bool)
        or value.target_version < 1
        or not 1 <= len(value.risks) <= 20
        or value.risks != tuple(sorted(set(value.risks)))
        or any(
            not risk or len(risk) > 200 or _NO_CONTROL.fullmatch(risk) is None
            for risk in value.risks
        )
        or any(_SHA256.fullmatch(item) is None for item in fingerprints)
    ):
        raise _service_unavailable()


def _validate_impact(
    value: SemanticChangeImpactPublic,
    *,
    workspace_id: str,
    report_id: str,
) -> None:
    if not isinstance(value, SemanticChangeImpactPublic):
        raise _service_unavailable()
    if (
        value.workspace_id != workspace_id
        or value.report_id != report_id
        or not value.artifact_id
        or len(value.artifact_id) > 200
        or _NO_CONTROL.fullmatch(value.artifact_id) is None
        or (
            value.artifact_version is not None
            and (isinstance(value.artifact_version, bool) or value.artifact_version < 1)
        )
        or not 1 <= len(value.finding_ids) <= 2_000
        or value.finding_ids != tuple(sorted(set(value.finding_ids)))
        or any(_FINDING_ID.fullmatch(item) is None for item in value.finding_ids)
        or _SHA256.fullmatch(value.fingerprint) is None
    ):
        raise _service_unavailable()


def _validate_store_page(
    page: SemanticChangeStorePage[_ReadableT],
    *,
    requested_size: int,
) -> None:
    if not isinstance(page, SemanticChangeStorePage):
        raise _service_unavailable()
    item_count = len(page.items)
    if (
        page.page_size != requested_size
        or item_count > requested_size
        or page.rows_read not in {item_count, item_count + 1}
        or page.rows_read > requested_size + 1
        or page.has_more != (page.rows_read == item_count + 1)
        or (page.has_more and not page.items)
        or bool(page.items) != (page.last_key is not None)
    ):
        raise _service_unavailable()
    if page.last_key is not None and (
        not page.last_key.sort_value
        or not page.last_key.stable_id
        or len(page.last_key.sort_value) > 500
        or len(page.last_key.stable_id) > 500
        or _NO_CONTROL.fullmatch(page.last_key.sort_value) is None
        or _NO_CONTROL.fullmatch(page.last_key.stable_id) is None
    ):
        raise _service_unavailable()


def _public_page(
    page: SemanticChangeStorePage[_ReadableT],
    *,
    binding: SemanticChangeCursorBinding,
    cursors: SemanticChangeCursorPort,
    at: datetime,
    continuation: SemanticChangeCursorPosition | None,
) -> SemanticChangePage[_ReadableT]:
    next_cursor = None
    if page.has_more:
        assert page.last_key is not None
        issued_at = at if continuation is None else continuation.issued_at
        try:
            next_cursor = cursors.encode(
                binding=binding,
                last_key=page.last_key,
                issued_at=issued_at,
            )
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor.encode("utf-8")) > MAX_SEMANTIC_CHANGE_CURSOR_BYTES
                or _NO_CONTROL.fullmatch(next_cursor) is None
            ):
                raise ValueError("invalid cursor response")
        except (SemanticChangeCursorError, TypeError, ValueError) as error:
            raise _unavailable() from error
    return SemanticChangePage(items=page.items, next_cursor=next_cursor, as_of=at)


def _decode(
    cursors: SemanticChangeCursorPort,
    cursor: str | None,
    *,
    binding: SemanticChangeCursorBinding,
    at: datetime,
) -> SemanticChangeCursorPosition | None:
    if cursor is None:
        return None
    try:
        position = cursors.decode(
            cursor=cursor,
            expected_binding=binding,
            at=at,
        )
        if (
            not isinstance(position, SemanticChangeCursorPosition)
            or not isinstance(position.last_key, SemanticChangePageKey)
            or not position.last_key.sort_value
            or not position.last_key.stable_id
            or position.issued_at.tzinfo is None
            or position.issued_at.utcoffset() is None
            or position.expires_at.tzinfo is None
            or position.expires_at.utcoffset() is None
            or not position.issued_at <= at < position.expires_at
        ):
            raise ValueError("invalid cursor position")
        return position
    except (SemanticChangeCursorError, TypeError, ValueError) as error:
        raise _unavailable() from error


def _call_store(operation: Callable[[], _ReadableT]) -> _ReadableT:
    try:
        return operation()
    except SemanticChangeReadError:
        raise
    except SemanticChangeReadPortError as error:
        raise _service_unavailable() from error
    except Exception as error:
        raise _service_unavailable() from error


def _unavailable() -> SemanticChangeReadError:
    return SemanticChangeReadError(
        SemanticChangeReadErrorCode.UNAVAILABLE,
        "semantic change resource is unavailable",
    )


def _service_unavailable() -> SemanticChangeReadError:
    return SemanticChangeReadError(
        SemanticChangeReadErrorCode.SERVICE_UNAVAILABLE,
        "semantic change service is unavailable",
    )


__all__ = [
    "InspectSemanticChangeReport",
    "ListSemanticChangeFindings",
    "ListSemanticChangeImpacts",
    "ListSemanticChangeReports",
    "SemanticChangeReadError",
    "SemanticChangeReadErrorCode",
]
