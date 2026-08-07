#!/usr/bin/env python3
"""Loopback-only, read-only M26 semantic-change acceptance panel.

The panel is development instrumentation, not Query Studio.  In upstream mode
it keeps the authenticated API bearer and every continuation cursor on the
server side.  The browser receives only the same minimized report, finding and
impact projections exposed by the M26 HTTP API.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlencode, urlsplit

LOOPBACK_V4: Final = "127.0.0.1"
DEFAULT_PANEL_PORT: Final = 8530
DEFAULT_UPSTREAM: Final = "http://127.0.0.1:8520"
MAX_BROWSER_BODY_BYTES: Final = 8 * 1024
MAX_UPSTREAM_BODY_BYTES: Final = 1024 * 1024
MAX_TOKEN_BYTES: Final = 16 * 1024
MAX_CURSOR_HISTORY: Final = 64
MAX_SESSIONS: Final = 16
MAX_PAGE_SIZE: Final = 50
SESSION_COOKIE: Final = "sb_m26_panel_session"

_REPORT_ID = re.compile(r"^report_[0-9a-f]{64}$")
_FINDING_ID = re.compile(r"^finding_[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SAFE_BEARER = re.compile(r"^[\x21-\x7e]{16,16384}$")
_REPORT_DETAIL_PATH = re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}$")
_FINDINGS_PATH = re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}/findings$")
_IMPACTS_PATH = re.compile(r"^/v1/semantic-changes/reports/report_[0-9a-f]{64}/impacts$")

_REPORT_STATUSES: Final = frozenset(
    {
        "current",
        "review_required",
        "blocked",
        "revalidated",
        "rejected",
        "superseded",
    }
)
_SCENARIO_STATUS: Final[dict[str, str]] = {
    "current": "current",
    "review": "review_required",
    "blocked": "blocked",
    "remediated": "revalidated",
}
_CHANGE_KINDS: Final = frozenset(
    {
        "baseline_required",
        "binding_missing",
        "binding_ambiguous",
        "asset_removed",
        "field_removed",
        "physical_type_changed",
        "nullability_changed",
        "key_status_changed",
        "field_definition_changed",
        "field_terms_changed",
        "asset_metadata_changed",
        "registry_changed",
        "join_cardinality_changed",
        "join_foreign_key_changed",
        "join_overlap_changed",
        "join_nulls_changed",
        "join_invalids_changed",
        "join_multiplicity_changed",
        "evidence_unavailable",
    }
)
_SEVERITIES: Final = frozenset({"informational", "review_required", "blocking"})
_TARGET_KINDS: Final = frozenset({"mapping", "join"})
_IMPACT_KINDS: Final = frozenset({"mapping", "join", "workflow", "recipe"})

_REPORT_QUERY_KEYS: Final = frozenset({"status", "page_size", "cursor"})
_FINDING_QUERY_KEYS: Final = frozenset({"kind", "severity", "page_size", "cursor"})
_IMPACT_QUERY_KEYS: Final = frozenset({"kind", "page_size", "cursor"})
_ACTION_KEYS: Final[dict[str, frozenset[str]]] = {
    "select_scenario": frozenset({"action", "scenario", "page_size"}),
    "reports": frozenset({"action", "direction", "page_size", "status"}),
    "select_report": frozenset({"action", "report_id"}),
    "findings": frozenset({"action", "direction", "page_size", "kind", "severity"}),
    "impacts": frozenset({"action", "direction", "page_size", "kind"}),
    "reset": frozenset({"action"}),
}

_PROBLEM_TITLES: Final[dict[int, str]] = {
    400: "La solicitud local de aceptación fue rechazada.",
    401: "La identidad de aceptación no está autenticada.",
    403: "La lectura semántica no está autorizada.",
    404: "El recurso de cambio semántico no está disponible.",
    405: "El método del panel no está permitido.",
    413: "La solicitud local de aceptación es demasiado grande.",
    415: "El panel sólo acepta acciones JSON.",
    422: "La lectura semántica no es válida.",
    500: "La lectura semántica falló de forma segura.",
    502: "La API semántica local devolvió una respuesta inválida.",
    503: "El servicio semántico no está disponible temporalmente.",
    504: "La API semántica local no respondió a tiempo.",
}


class PanelError(RuntimeError):
    """A bounded browser-safe panel failure."""

    def __init__(self, status: int, code: str, title: str) -> None:
        self.status = status
        self.code = code
        self.title = title
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status: int
    payload: object


class SemanticChangeTransport(Protocol):
    """Read one allowlisted semantic-change HTTP projection."""

    label: str

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str | int],
    ) -> UpstreamResponse:
        """Perform one read-only request."""


def _forbidden_environment_key(key: str) -> bool:
    normalized = key.upper()
    if normalized in {
        "DATABASE_URL",
        "DATAHUB_GMS_TOKEN",
        "OPENAI_API_KEY",
    }:
        return True
    if normalized.startswith("SCHEMABRIDGE_CONTROL") and normalized.endswith("DATABASE_URL"):
        return True
    if "CURSOR_SIGNING_KEY" in normalized:
        return True
    return "SEMANTIC_CHANGE" in normalized and "SIGNING_KEY" in normalized


def require_secret_free_environment(environment: Mapping[str, str]) -> None:
    """Fail closed when the panel inherited unrelated privileged material."""

    if any(_forbidden_environment_key(key) for key in environment):
        raise PanelError(
            78,
            "forbidden_environment",
            "El panel heredó un entorno con secretos no permitidos.",
        )


def validate_loopback_host(host: str) -> str:
    """Accept a numeric loopback address only."""

    value = host.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise PanelError(
            78,
            "invalid_loopback",
            "Se requiere una dirección loopback numérica.",
        ) from error
    if not parsed.is_loopback:
        raise PanelError(
            78,
            "invalid_loopback",
            "Se requiere una dirección loopback numérica.",
        )
    return value


@dataclass(frozen=True, slots=True)
class UpstreamTarget:
    host: str
    port: int

    @classmethod
    def parse(cls, raw: str) -> UpstreamTarget:
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            raise PanelError(
                78,
                "invalid_upstream",
                "El upstream debe ser un único origen HTTP loopback.",
            )
        host = validate_loopback_host(parsed.hostname)
        try:
            port = parsed.port
        except ValueError as error:
            raise PanelError(
                78,
                "invalid_upstream",
                "El puerto upstream no es válido.",
            ) from error
        if port is None or not 1 <= port <= 65535:
            raise PanelError(
                78,
                "invalid_upstream",
                "El puerto upstream no es válido.",
            )
        return cls(host=host, port=port)


def read_bearer_file(path: Path) -> str:
    """Read one bounded owner-only bearer without placing it in argv or env."""

    try:
        file_stat = path.lstat()
    except OSError as error:
        raise PanelError(
            78,
            "invalid_bearer_file",
            "El archivo bearer no está disponible.",
        ) from error
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not 0 < file_stat.st_size <= MAX_TOKEN_BYTES
    ):
        raise PanelError(
            78,
            "invalid_bearer_file",
            "El archivo bearer no está configurado de forma segura.",
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PanelError(
            78,
            "invalid_bearer_file",
            "El archivo bearer no está disponible.",
        ) from error
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise PanelError(
            78,
            "invalid_bearer_file",
            "El archivo bearer no es válido.",
        ) from error
    if not _SAFE_BEARER.fullmatch(token):
        raise PanelError(
            78,
            "invalid_bearer_file",
            "El archivo bearer no es válido.",
        )
    return token


def _validate_upstream_request(
    *,
    method: str,
    path: str,
    query: Mapping[str, str | int],
) -> None:
    """Permit exactly the four public M26 GET surfaces."""

    query_keys = frozenset(query)
    allowed = False
    if method == "GET" and path == "/v1/semantic-changes/reports":
        allowed = query_keys <= _REPORT_QUERY_KEYS
    elif method == "GET" and _REPORT_DETAIL_PATH.fullmatch(path):
        allowed = not query
    elif method == "GET" and _FINDINGS_PATH.fullmatch(path):
        allowed = query_keys <= _FINDING_QUERY_KEYS
    elif method == "GET" and _IMPACTS_PATH.fullmatch(path):
        allowed = query_keys <= _IMPACT_QUERY_KEYS
    if not allowed:
        raise PanelError(
            500,
            "upstream_request_denied",
            "El panel denegó una solicitud upstream no permitida.",
        )
    _validate_upstream_query(path, query)


def _validate_upstream_query(
    path: str,
    query: Mapping[str, str | int],
) -> None:
    for key, value in query.items():
        if key == "page_size":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= MAX_PAGE_SIZE
            ):
                raise PanelError(
                    500,
                    "upstream_request_denied",
                    "El panel denegó una paginación no válida.",
                )
        elif key == "cursor":
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 1024
                or not value.isascii()
                or any(ord(character) < 33 or ord(character) == 127 for character in value)
            ):
                raise PanelError(
                    500,
                    "upstream_request_denied",
                    "El panel denegó un cursor no válido.",
                )
        elif key == "status":
            if not isinstance(value, str) or value not in _REPORT_STATUSES:
                raise PanelError(
                    500,
                    "upstream_request_denied",
                    "El panel denegó un estado no válido.",
                )
        elif key == "kind":
            choices = _IMPACT_KINDS if _IMPACTS_PATH.fullmatch(path) else _CHANGE_KINDS
            if not isinstance(value, str) or value not in choices:
                raise PanelError(
                    500,
                    "upstream_request_denied",
                    "El panel denegó un tipo no válido.",
                )
        elif key == "severity" and (
            not _FINDINGS_PATH.fullmatch(path)
            or not isinstance(value, str)
            or value not in _SEVERITIES
        ):
            raise PanelError(
                500,
                "upstream_request_denied",
                "El panel denegó una severidad no válida.",
            )


@dataclass(frozen=True, slots=True)
class HttpSemanticChangeTransport:
    target: UpstreamTarget
    bearer: str = field(repr=False)
    timeout_seconds: float = 5.0
    max_response_bytes: int = MAX_UPSTREAM_BODY_BYTES
    label: str = "authenticated_http"

    def __post_init__(self) -> None:
        if not _SAFE_BEARER.fullmatch(self.bearer):
            raise PanelError(78, "invalid_bearer", "La identidad de aceptación no es válida.")
        if not 0.1 <= self.timeout_seconds <= 10.0:
            raise PanelError(78, "invalid_timeout", "El timeout upstream no es válido.")
        if not 1024 <= self.max_response_bytes <= MAX_UPSTREAM_BODY_BYTES:
            raise PanelError(
                78,
                "invalid_response_limit",
                "El límite de respuesta no es válido.",
            )

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str | int],
    ) -> UpstreamResponse:
        _validate_upstream_request(method=method, path=path, query=query)
        target = path if not query else f"{path}?{urlencode(tuple(query.items()))}"
        connection = http.client.HTTPConnection(
            self.target.host,
            self.target.port,
            timeout=self.timeout_seconds,
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.bearer}",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise PanelError(
                    502,
                    "upstream_response_too_large",
                    _PROBLEM_TITLES[502],
                )
            if self.bearer.encode("ascii") in raw:
                raise PanelError(
                    502,
                    "protected_value_in_upstream_response",
                    _PROBLEM_TITLES[502],
                )
            media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            valid_media_type = media_type == "application/json" or (
                response.status >= 400 and media_type == "application/problem+json"
            )
            if not valid_media_type:
                raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
            try:
                payload = json.loads(raw, object_pairs_hook=_unique_json_object)
            except (UnicodeDecodeError, json.JSONDecodeError, PanelError) as error:
                raise PanelError(
                    502,
                    "invalid_upstream_response",
                    _PROBLEM_TITLES[502],
                ) from error
            return UpstreamResponse(status=response.status, payload=payload)
        except TimeoutError as error:
            raise PanelError(504, "upstream_timeout", _PROBLEM_TITLES[504]) from error
        except (ConnectionError, OSError, http.client.HTTPException) as error:
            raise PanelError(
                503,
                "upstream_unavailable",
                _PROBLEM_TITLES[503],
            ) from error
        finally:
            if response is not None:
                response.close()
            connection.close()


@dataclass(frozen=True, slots=True)
class SyntheticSemanticChangeTransport:
    """Deterministic API-shaped evidence for a secret-free browser drill."""

    reports: tuple[dict[str, object], ...]
    findings: Mapping[str, tuple[dict[str, object], ...]]
    impacts: Mapping[str, tuple[dict[str, object], ...]]
    label: str = "synthetic_fixture"

    @classmethod
    def create(cls) -> SyntheticSemanticChangeTransport:
        reports = (
            _synthetic_report("a", "current", 0, (0, 0, 0, 0), registry_version=7),
            _synthetic_report(
                "b",
                "review_required",
                3,
                (0, 0, 3, 2),
                registry_version=7,
            ),
            _synthetic_report(
                "c",
                "blocked",
                57,
                (15, 16, 16, 16),
                registry_version=7,
            ),
            _synthetic_report(
                "d",
                "revalidated",
                4,
                (3, 0, 4, 0),
                registry_version=8,
            ),
        )
        review_id = _report_identifier("b")
        blocked_id = _report_identifier("c")
        remediated_id = _report_identifier("d")
        findings = {
            review_id: tuple(
                _synthetic_finding(
                    index,
                    "field_definition_changed" if index % 2 else "field_terms_changed",
                    "review_required",
                )
                for index in range(1, 4)
            ),
            blocked_id: tuple(
                _synthetic_finding(
                    index,
                    (
                        "physical_type_changed",
                        "key_status_changed",
                        "nullability_changed",
                        "field_removed",
                    )[index % 4],
                    "blocking",
                )
                for index in range(1, 58)
            ),
            remediated_id: tuple(
                _synthetic_finding(
                    index + 100,
                    "field_definition_changed",
                    "review_required",
                )
                for index in range(1, 5)
            ),
        }
        impacts = {
            review_id: tuple(
                _synthetic_impact(
                    index,
                    "workflow" if index % 2 else "recipe",
                    finding_index=((index - 1) % 3) + 1,
                )
                for index in range(1, 6)
            ),
            blocked_id: tuple(
                _synthetic_impact(
                    index,
                    ("mapping", "join", "workflow", "recipe")[index % 4],
                    finding_index=((index - 1) % 57) + 1,
                )
                for index in range(1, 64)
            ),
            remediated_id: tuple(
                _synthetic_impact(
                    index + 100,
                    "workflow" if index % 2 else "mapping",
                    finding_index=100 + (((index - 1) % 4) + 1),
                )
                for index in range(1, 8)
            ),
        }
        return cls(
            reports=reports,
            findings=findings,
            impacts=impacts,
        )

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str | int],
    ) -> UpstreamResponse:
        _validate_upstream_request(method=method, path=path, query=query)
        if path == "/v1/semantic-changes/reports":
            values = list(self.reports)
            status = query.get("status")
            if isinstance(status, str):
                values = [item for item in values if item["status"] == status]
            return _synthetic_page("reports", values, query, scope="reports")
        report_id = path.split("/")[4]
        report = next(
            (item for item in self.reports if item["report_id"] == report_id),
            None,
        )
        if report is None:
            return _synthetic_unavailable()
        if _REPORT_DETAIL_PATH.fullmatch(path):
            return UpstreamResponse(200, report)
        if _FINDINGS_PATH.fullmatch(path):
            values = list(self.findings.get(report_id, ()))
            kind = query.get("kind")
            severity = query.get("severity")
            if isinstance(kind, str):
                values = [item for item in values if item["kind"] == kind]
            if isinstance(severity, str):
                values = [item for item in values if item["severity"] == severity]
            return _synthetic_page(
                "findings",
                values,
                query,
                scope=f"findings:{report_id}",
            )
        values = list(self.impacts.get(report_id, ()))
        kind = query.get("kind")
        if isinstance(kind, str):
            values = [item for item in values if item["kind"] == kind]
        return _synthetic_page(
            "impacts",
            values,
            query,
            scope=f"impacts:{report_id}",
        )


def _synthetic_report(
    marker: str,
    status: str,
    finding_count: int,
    impact_counts: tuple[int, int, int, int],
    *,
    registry_version: int,
) -> dict[str, object]:
    report_id = _report_identifier(marker)
    mapping_count, join_count, workflow_count, recipe_count = impact_counts
    return {
        "report_id": report_id,
        "status": status,
        "pointer_generation": registry_version + 10,
        "pointer_fingerprint": marker * 64,
        "registry_version": registry_version,
        "registry_fingerprint": hashlib.sha256(f"registry-{marker}".encode()).hexdigest(),
        "catalog_generation_count": 2,
        "observation_fingerprint": hashlib.sha256(f"observation-{marker}".encode()).hexdigest(),
        "baseline_revision": None if status == "review_required" else registry_version,
        "baseline_fingerprint": (
            None
            if status == "review_required"
            else hashlib.sha256(f"baseline-{marker}".encode()).hexdigest()
        ),
        "finding_count": finding_count,
        "mapping_impact_count": mapping_count,
        "join_impact_count": join_count,
        "workflow_impact_count": workflow_count,
        "recipe_impact_count": recipe_count,
        "impacts_complete": True,
        "dependency_watermark": 42,
        "impact_set_fingerprint": hashlib.sha256(f"impacts-{marker}".encode()).hexdigest(),
        "inspected_at": f"2026-07-24T1{registry_version}:00:00Z",
        "fingerprint": report_id.removeprefix("report_"),
    }


def _report_identifier(marker: str) -> str:
    return f"report_{marker * 64}"


def _synthetic_finding(index: int, kind: str, severity: str) -> dict[str, object]:
    fingerprint = hashlib.sha256(f"finding-{index}".encode()).hexdigest()
    return {
        "finding_id": f"finding_{fingerprint}",
        "kind": kind,
        "severity": severity,
        "target_kind": "mapping" if index % 3 else "join",
        "target_id": f"governed-target-{index:03d}",
        "target_version": 2,
        "previous_fingerprint": hashlib.sha256(f"old-{index}".encode()).hexdigest(),
        "current_fingerprint": hashlib.sha256(f"new-{index}".encode()).hexdigest(),
        "risks": (
            ["requires_human_review"]
            if severity != "blocking"
            else ["semantic_context_stale", "source_io_blocked"]
        ),
        "fingerprint": fingerprint,
    }


def _synthetic_impact(
    index: int,
    kind: str,
    *,
    finding_index: int,
) -> dict[str, object]:
    finding_fingerprint = hashlib.sha256(f"finding-{finding_index}".encode()).hexdigest()
    return {
        "kind": kind,
        "artifact_id": f"synthetic-{kind}-{index:03d}",
        "artifact_version": 2,
        "finding_ids": [f"finding_{finding_fingerprint}"],
        "fingerprint": hashlib.sha256(f"impact-{index}-{kind}".encode()).hexdigest(),
    }


def _synthetic_page(
    resource: str,
    values: list[dict[str, object]],
    query: Mapping[str, str | int],
    *,
    scope: str,
) -> UpstreamResponse:
    page_size_value = query.get("page_size", 20)
    page_size = page_size_value if isinstance(page_size_value, int) else 20
    filters = tuple(
        sorted(
            (key, str(value)) for key, value in query.items() if key not in {"cursor", "page_size"}
        )
    )
    filter_digest = hashlib.sha256(
        json.dumps(
            {"scope": scope, "filters": filters},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()[:16]
    offset = 0
    cursor = query.get("cursor")
    if isinstance(cursor, str):
        match = re.fullmatch(r"fixture_([0-9]+)_([0-9a-f]{16})", cursor)
        if match is None or match.group(2) != filter_digest:
            return _synthetic_unavailable()
        offset = int(match.group(1))
    page = values[offset : offset + page_size]
    next_offset = offset + len(page)
    next_cursor = f"fixture_{next_offset}_{filter_digest}" if next_offset < len(values) else None
    return UpstreamResponse(
        200,
        {
            "resource": resource,
            "items": page,
            "next_cursor": next_cursor,
            "as_of": "2026-07-24T18:00:00Z",
        },
    )


def _synthetic_unavailable() -> UpstreamResponse:
    return UpstreamResponse(
        404,
        {
            "status": 404,
            "code": "semantic_change_resource_unavailable",
            "title": "The semantic-change resource is not available.",
        },
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PanelError(400, "duplicate_json_key", "No se aceptan claves JSON duplicadas.")
        result[key] = value
    return result


@dataclass(slots=True)
class _PageEntry:
    number: int
    request_cursor: str | None
    next_cursor: str | None = None


@dataclass(slots=True)
class _Pager:
    entries: list[_PageEntry] = field(default_factory=list)
    position: int = 0
    filters: tuple[tuple[str, str | int], ...] | None = None

    def prepare(
        self,
        *,
        direction: str,
        filters: tuple[tuple[str, str | int], ...],
    ) -> str | None:
        if direction == "first":
            self.entries = [_PageEntry(number=1, request_cursor=None)]
            self.position = 0
            self.filters = filters
            return None
        if not self.entries or self.filters != filters:
            raise PanelError(
                409,
                "paging_context_changed",
                "Los filtros cambiaron; vuelve a la primera página.",
            )
        if direction == "previous":
            if self.position == 0:
                raise PanelError(409, "page_unavailable", "No hay una página anterior.")
            self.position -= 1
            return self.entries[self.position].request_cursor
        if direction != "next":
            raise PanelError(400, "invalid_direction", "La dirección de página no es válida.")
        current = self.entries[self.position]
        if current.next_cursor is None:
            raise PanelError(409, "page_unavailable", "No hay una página siguiente.")
        if self.position + 1 < len(self.entries):
            self.position += 1
            return self.entries[self.position].request_cursor
        self.entries.append(
            _PageEntry(
                number=current.number + 1,
                request_cursor=current.next_cursor,
            )
        )
        self.position += 1
        if len(self.entries) > MAX_CURSOR_HISTORY:
            self.entries.pop(0)
            self.position -= 1
        return self.entries[self.position].request_cursor

    def record(self, next_cursor: str | None) -> None:
        if not self.entries:
            raise PanelError(500, "pager_state_invalid", _PROBLEM_TITLES[500])
        self.entries[self.position].next_cursor = next_cursor

    def public(self) -> dict[str, object]:
        if not self.entries:
            return {
                "page": 0,
                "has_previous": False,
                "has_next": False,
                "retained_pages": 0,
            }
        current = self.entries[self.position]
        return {
            "page": current.number,
            "has_previous": self.position > 0,
            "has_next": current.next_cursor is not None,
            "retained_pages": len(self.entries),
        }


@dataclass(slots=True)
class _SessionState:
    source: str
    scenario: str = "current"
    reports: list[dict[str, object]] = field(default_factory=list)
    selected_report: dict[str, object] | None = None
    findings: list[dict[str, object]] = field(default_factory=list)
    impacts: list[dict[str, object]] = field(default_factory=list)
    reports_as_of: str | None = None
    findings_as_of: str | None = None
    impacts_as_of: str | None = None
    report_pager: _Pager = field(default_factory=_Pager)
    finding_pager: _Pager = field(default_factory=_Pager)
    impact_pager: _Pager = field(default_factory=_Pager)

    def replace_with(self, other: _SessionState) -> None:
        self.scenario = other.scenario
        self.reports = other.reports
        self.selected_report = other.selected_report
        self.findings = other.findings
        self.impacts = other.impacts
        self.reports_as_of = other.reports_as_of
        self.findings_as_of = other.findings_as_of
        self.impacts_as_of = other.impacts_as_of
        self.report_pager = other.report_pager
        self.finding_pager = other.finding_pager
        self.impact_pager = other.impact_pager

    def public(self) -> dict[str, object]:
        return {
            "instrumentation": "m26_read_only_acceptance",
            "query_studio": False,
            "source": self.source,
            "scenario": self.scenario,
            "scenario_status": _SCENARIO_STATUS[self.scenario],
            "status_guide": [
                {"scenario": "current", "api_status": "current", "label": "Actual"},
                {
                    "scenario": "review",
                    "api_status": "review_required",
                    "label": "Revisión requerida",
                },
                {"scenario": "blocked", "api_status": "blocked", "label": "Bloqueado"},
                {
                    "scenario": "remediated",
                    "api_status": "revalidated",
                    "label": "Remediado",
                },
            ],
            "access_boundary": {
                "upstream_http_methods": ["GET"],
                "upstream_read_routes": 4,
                "upstream_mutation_routes": 0,
                "approval_controls": False,
                "operator_mutations": "CLI separada; no disponible en este panel",
            },
            "reports": self.reports,
            "selected_report": self.selected_report,
            "findings": self.findings,
            "impacts": self.impacts,
            "as_of": {
                "reports": self.reports_as_of,
                "findings": self.findings_as_of,
                "impacts": self.impacts_as_of,
            },
            "paging": {
                "reports": self.report_pager.public(),
                "findings": self.finding_pager.public(),
                "impacts": self.impact_pager.public(),
                "maximum_page_size": MAX_PAGE_SIZE,
            },
        }


@dataclass(slots=True)
class SemanticChangePanelBff:
    transport: SemanticChangeTransport
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def new_state(self) -> _SessionState:
        return _SessionState(source=self.transport.label)

    def apply(
        self,
        state: _SessionState,
        raw_action: object,
    ) -> dict[str, object]:
        try:
            action = _validated_action(raw_action)
            with self._lock:
                candidate = copy.deepcopy(state)
                self._apply(candidate, action)
                state.replace_with(candidate)
            return {"ok": True, "state": state.public()}
        except PanelError as error:
            return {
                "ok": False,
                "problem": _problem(error.status, error.code, error.title),
                "state": state.public(),
            }
        except Exception:
            return {
                "ok": False,
                "problem": _problem(
                    500,
                    "panel_internal_error",
                    _PROBLEM_TITLES[500],
                ),
                "state": state.public(),
            }

    def _apply(
        self,
        state: _SessionState,
        action: Mapping[str, object],
    ) -> None:
        name = _required_text(action, "action", 32)
        if name == "reset":
            state.replace_with(self.new_state())
            return
        if name == "select_scenario":
            scenario = _required_text(action, "scenario", 20)
            if scenario not in _SCENARIO_STATUS:
                raise PanelError(400, "invalid_scenario", "El escenario no es válido.")
            state.scenario = scenario
            state.reports = []
            state.selected_report = None
            state.findings = []
            state.impacts = []
            state.report_pager = _Pager()
            state.finding_pager = _Pager()
            state.impact_pager = _Pager()
            self._load_reports(
                state,
                direction="first",
                page_size=_page_size(action),
                status=_SCENARIO_STATUS[scenario],
            )
            return
        if name == "reports":
            status = _optional_enum(action.get("status"), _REPORT_STATUSES, "status")
            self._load_reports(
                state,
                direction=_direction(action),
                page_size=_page_size(action),
                status=status,
            )
            return
        if name == "select_report":
            report_id = _required_text(action, "report_id", 71)
            if not _REPORT_ID.fullmatch(report_id):
                raise PanelError(400, "invalid_report_id", "El reporte no es válido.")
            if report_id not in {str(item["report_id"]) for item in state.reports}:
                raise PanelError(
                    404,
                    "semantic_change_resource_unavailable",
                    _PROBLEM_TITLES[404],
                )
            response = self.transport.request(
                method="GET",
                path=f"/v1/semantic-changes/reports/{report_id}",
                query={},
            )
            payload = _require_success(response)
            detail = _project_report(payload)
            if detail["report_id"] != report_id:
                raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
            state.selected_report = detail
            state.findings = []
            state.impacts = []
            state.finding_pager = _Pager()
            state.impact_pager = _Pager()
            return
        if name == "findings":
            self._load_findings(
                state,
                direction=_direction(action),
                page_size=_page_size(action),
                kind=_optional_enum(action.get("kind"), _CHANGE_KINDS, "kind"),
                severity=_optional_enum(
                    action.get("severity"),
                    _SEVERITIES,
                    "severity",
                ),
            )
            return
        if name == "impacts":
            self._load_impacts(
                state,
                direction=_direction(action),
                page_size=_page_size(action),
                kind=_optional_enum(action.get("kind"), _IMPACT_KINDS, "kind"),
            )
            return
        raise PanelError(400, "invalid_action", "La acción no es válida.")

    def _load_reports(
        self,
        state: _SessionState,
        *,
        direction: str,
        page_size: int,
        status: str | None,
    ) -> None:
        filters = tuple(
            (key, value)
            for key, value in (("page_size", page_size), ("status", status))
            if value is not None
        )
        cursor = state.report_pager.prepare(direction=direction, filters=filters)
        query: dict[str, str | int] = dict(filters)
        if cursor is not None:
            query["cursor"] = cursor
        response = self.transport.request(
            method="GET",
            path="/v1/semantic-changes/reports",
            query=query,
        )
        page = _project_page(_require_success(response), "reports", _project_report)
        state.report_pager.record(page["next_cursor"])
        state.reports = page["items"]
        state.reports_as_of = page["as_of"]
        state.selected_report = None
        state.findings = []
        state.impacts = []
        state.finding_pager = _Pager()
        state.impact_pager = _Pager()

    def _load_findings(
        self,
        state: _SessionState,
        *,
        direction: str,
        page_size: int,
        kind: str | None,
        severity: str | None,
    ) -> None:
        report_id = _selected_report_id(state)
        filters = tuple(
            (key, value)
            for key, value in (
                ("page_size", page_size),
                ("kind", kind),
                ("severity", severity),
            )
            if value is not None
        )
        cursor = state.finding_pager.prepare(direction=direction, filters=filters)
        query: dict[str, str | int] = dict(filters)
        if cursor is not None:
            query["cursor"] = cursor
        response = self.transport.request(
            method="GET",
            path=f"/v1/semantic-changes/reports/{report_id}/findings",
            query=query,
        )
        page = _project_page(_require_success(response), "findings", _project_finding)
        state.finding_pager.record(page["next_cursor"])
        state.findings = page["items"]
        state.findings_as_of = page["as_of"]

    def _load_impacts(
        self,
        state: _SessionState,
        *,
        direction: str,
        page_size: int,
        kind: str | None,
    ) -> None:
        report_id = _selected_report_id(state)
        filters = tuple(
            (key, value)
            for key, value in (("page_size", page_size), ("kind", kind))
            if value is not None
        )
        cursor = state.impact_pager.prepare(direction=direction, filters=filters)
        query: dict[str, str | int] = dict(filters)
        if cursor is not None:
            query["cursor"] = cursor
        response = self.transport.request(
            method="GET",
            path=f"/v1/semantic-changes/reports/{report_id}/impacts",
            query=query,
        )
        page = _project_page(_require_success(response), "impacts", _project_impact)
        state.impact_pager.record(page["next_cursor"])
        state.impacts = page["items"]
        state.impacts_as_of = page["as_of"]


def _validated_action(raw: object) -> dict[str, object]:
    action = _object(raw)
    name = action.get("action")
    if not isinstance(name, str) or name not in _ACTION_KEYS:
        raise PanelError(400, "invalid_action", "La acción no es válida.")
    if frozenset(action) != _ACTION_KEYS[name]:
        raise PanelError(
            400,
            "invalid_action_parameters",
            "Los parámetros de la acción no son válidos.",
        )
    return action


def _required_text(value: Mapping[str, object], key: str, maximum: int) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not 1 <= len(item) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in item)
    ):
        raise PanelError(400, "invalid_action_value", "La acción contiene un valor no válido.")
    return item


def _direction(action: Mapping[str, object]) -> str:
    direction = _required_text(action, "direction", 10)
    if direction not in {"first", "previous", "next"}:
        raise PanelError(400, "invalid_direction", "La dirección de página no es válida.")
    return direction


def _page_size(action: Mapping[str, object]) -> int:
    value = action.get("page_size")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE_SIZE:
        raise PanelError(400, "invalid_page_size", "El tamaño de página no es válido.")
    return value


def _optional_enum(
    value: object,
    choices: frozenset[str],
    name: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise PanelError(400, f"invalid_{name}", f"El filtro {name} no es válido.")
    return value


def _selected_report_id(state: _SessionState) -> str:
    if state.selected_report is None:
        raise PanelError(409, "report_required", "Selecciona primero un reporte visible.")
    return str(state.selected_report["report_id"])


def _require_success(response: UpstreamResponse) -> object:
    if 200 <= response.status < 300:
        return response.payload
    problem = _object(response.payload)
    code = problem.get("code")
    safe_code = (
        code
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
        else "semantic_change_read_failed"
    )
    status = response.status if response.status in _PROBLEM_TITLES else 502
    raise PanelError(status, safe_code, _PROBLEM_TITLES[status])


def _project_page(
    raw: object,
    resource: str,
    projector: Any,
) -> dict[str, Any]:
    payload = _object(raw)
    if payload.get("resource") != resource:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_PAGE_SIZE:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    cursor = payload.get("next_cursor")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 1024
        or not cursor.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in cursor)
    ):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return {
        "items": [projector(item) for item in raw_items],
        "next_cursor": cursor,
        "as_of": _safe_text(payload.get("as_of"), "as_of", 40),
    }


def _project_report(raw: object) -> dict[str, object]:
    payload = _object(raw)
    report_id = _safe_text(payload.get("report_id"), "report_id", 71)
    fingerprint = _safe_text(payload.get("fingerprint"), "fingerprint", 64)
    if not _REPORT_ID.fullmatch(report_id) or not _SHA256.fullmatch(fingerprint):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    baseline_fingerprint = _optional_sha256(payload.get("baseline_fingerprint"))
    baseline_revision = _optional_positive_int(payload.get("baseline_revision"))
    return {
        "report_id": report_id,
        "status": _safe_enum(payload.get("status"), _REPORT_STATUSES),
        "pointer_generation": _positive_int(payload.get("pointer_generation")),
        "pointer_fingerprint": _sha256(payload.get("pointer_fingerprint")),
        "registry_version": _positive_int(payload.get("registry_version")),
        "registry_fingerprint": _sha256(payload.get("registry_fingerprint")),
        "catalog_generation_count": _bounded_int(
            payload.get("catalog_generation_count"),
            0,
            100_000,
        ),
        "observation_fingerprint": _sha256(payload.get("observation_fingerprint")),
        "baseline_revision": baseline_revision,
        "baseline_fingerprint": baseline_fingerprint,
        "finding_count": _bounded_int(payload.get("finding_count"), 0, 2_000),
        "mapping_impact_count": _bounded_int(
            payload.get("mapping_impact_count"),
            0,
            10_000,
        ),
        "join_impact_count": _bounded_int(
            payload.get("join_impact_count"),
            0,
            10_000,
        ),
        "workflow_impact_count": _bounded_int(
            payload.get("workflow_impact_count"),
            0,
            10_000,
        ),
        "recipe_impact_count": _bounded_int(
            payload.get("recipe_impact_count"),
            0,
            10_000,
        ),
        "impacts_complete": _safe_bool(payload.get("impacts_complete")),
        "dependency_watermark": _bounded_int(
            payload.get("dependency_watermark"),
            0,
            2**63 - 1,
        ),
        "impact_set_fingerprint": _sha256(payload.get("impact_set_fingerprint")),
        "inspected_at": _safe_text(payload.get("inspected_at"), "inspected_at", 40),
        "fingerprint": fingerprint,
    }


def _project_finding(raw: object) -> dict[str, object]:
    payload = _object(raw)
    finding_id = _safe_text(payload.get("finding_id"), "finding_id", 72)
    if not _FINDING_ID.fullmatch(finding_id):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    risks_raw = payload.get("risks")
    if (
        not isinstance(risks_raw, list)
        or not 1 <= len(risks_raw) <= 20
        or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in risks_raw)
    ):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return {
        "finding_id": finding_id,
        "kind": _safe_enum(payload.get("kind"), _CHANGE_KINDS),
        "severity": _safe_enum(payload.get("severity"), _SEVERITIES),
        "target_kind": _safe_enum(payload.get("target_kind"), _TARGET_KINDS),
        "target_id": _safe_text(payload.get("target_id"), "target_id", 200),
        "target_version": _positive_int(payload.get("target_version")),
        "previous_fingerprint": _optional_sha256(payload.get("previous_fingerprint")),
        "current_fingerprint": _optional_sha256(payload.get("current_fingerprint")),
        "risks": list(risks_raw),
        "fingerprint": _sha256(payload.get("fingerprint")),
    }


def _project_impact(raw: object) -> dict[str, object]:
    payload = _object(raw)
    finding_ids_raw = payload.get("finding_ids")
    if (
        not isinstance(finding_ids_raw, list)
        or not 1 <= len(finding_ids_raw) <= 2_000
        or any(
            not isinstance(item, str) or not _FINDING_ID.fullmatch(item) for item in finding_ids_raw
        )
    ):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return {
        "kind": _safe_enum(payload.get("kind"), _IMPACT_KINDS),
        "artifact_id": _safe_text(payload.get("artifact_id"), "artifact_id", 200),
        "artifact_version": _optional_positive_int(payload.get("artifact_version")),
        "finding_ids": list(finding_ids_raw),
        "fingerprint": _sha256(payload.get("fingerprint")),
    }


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_text(value: object, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PanelError(502, "invalid_upstream_response", f"El campo {name} no es válido.")
    return value


def _safe_enum(value: object, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _positive_int(value: object) -> int:
    return _bounded_int(value, 1, 2**63 - 1)


def _optional_positive_int(value: object) -> int | None:
    return None if value is None else _positive_int(value)


def _sha256(value: object) -> str:
    text = _safe_text(value, "fingerprint", 64)
    if not _SHA256.fullmatch(text):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return text


def _optional_sha256(value: object) -> str | None:
    return None if value is None else _sha256(value)


def _problem(status: int, code: str, title: str) -> dict[str, object]:
    safe_status = status if status in _PROBLEM_TITLES else 500
    safe_code = code if _SAFE_CODE.fullmatch(code) else "panel_internal_error"
    return {
        "status": safe_status,
        "code": safe_code,
        "title": title[:120],
    }


_PANEL_HTML: Final = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SchemaBridge M26 — aceptación local</title>
  <link rel="stylesheet" href="/panel.css">
  <script src="/panel.js" defer></script>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">INSTRUMENTACIÓN DE DESARROLLO · SOLO ACEPTACIÓN LOCAL</p>
    <h1>Cambios semánticos M26</h1>
    <p>Panel de solo lectura, separado de Query Studio. El bearer y los cursores permanecen en el servidor.</p>
    <div class="boundary" role="note">
      <strong>4 rutas GET · 0 rutas de mutación</strong>
      <span>Aprobar, rechazar o remediar sólo existe en la CLI de operador y no está disponible aquí.</span>
    </div>
  </header>
  <section aria-labelledby="scenario-title">
    <h2 id="scenario-title">1. Estado gobernado</h2>
    <div class="scenario-grid">
      <button data-scenario="current">Actual <small>current</small></button>
      <button data-scenario="review">Revisión requerida <small>review_required</small></button>
      <button data-scenario="blocked">Bloqueado <small>blocked</small></button>
      <button data-scenario="remediated">Remediado <small>revalidated</small></button>
    </div>
    <p>Escenario visible: <strong id="scenario">current</strong></p>
  </section>
  <section aria-labelledby="reports-title">
    <h2 id="reports-title">2. Reportes</h2>
    <div class="controls">
      <label>Estado
        <select id="report-status">
          <option value="">Todos</option>
          <option>current</option>
          <option>review_required</option>
          <option>blocked</option>
          <option>revalidated</option>
          <option>rejected</option>
          <option>superseded</option>
        </select>
      </label>
      <label>Tamaño de página
        <select id="report-size"><option>1</option><option>17</option><option selected>50</option></select>
      </label>
    </div>
    <div class="row">
      <button data-page="reports:first">Primera</button>
      <button data-page="reports:previous">Anterior</button>
      <button data-page="reports:next">Siguiente</button>
    </div>
    <label>Reporte visible <select id="report-select"></select></label>
    <button id="select-report">Inspeccionar reporte</button>
  </section>
  <section aria-labelledby="findings-title">
    <h2 id="findings-title">3. Hallazgos</h2>
    <div class="controls">
      <label>Clase <input id="finding-kind" maxlength="64" placeholder="physical_type_changed"></label>
      <label>Severidad
        <select id="finding-severity"><option value="">Todas</option><option>informational</option><option>review_required</option><option>blocking</option></select>
      </label>
      <label>Tamaño de página
        <select id="finding-size"><option>1</option><option>17</option><option selected>50</option></select>
      </label>
    </div>
    <div class="row">
      <button data-page="findings:first">Primera</button>
      <button data-page="findings:previous">Anterior</button>
      <button data-page="findings:next">Siguiente</button>
    </div>
  </section>
  <section aria-labelledby="impacts-title">
    <h2 id="impacts-title">4. Impactos</h2>
    <div class="controls">
      <label>Tipo
        <select id="impact-kind"><option value="">Todos</option><option>mapping</option><option>join</option><option>workflow</option><option>recipe</option></select>
      </label>
      <label>Tamaño de página
        <select id="impact-size"><option>1</option><option>17</option><option selected>50</option></select>
      </label>
    </div>
    <div class="row">
      <button data-page="impacts:first">Primera</button>
      <button data-page="impacts:previous">Anterior</button>
      <button data-page="impacts:next">Siguiente</button>
    </div>
  </section>
  <section aria-labelledby="state-title">
    <h2 id="state-title">Proyección HTTP sanitizada</h2>
    <p id="notice" role="status" aria-live="polite">Preparado.</p>
    <pre id="state" tabindex="0"></pre>
  </section>
</main>
</body>
</html>
"""

_PANEL_CSS: Final = """
:root{color-scheme:dark;font:16px/1.45 Inter,ui-sans-serif,system-ui,sans-serif;--ink:#f4f7ff;--muted:#a8b4ca;--panel:#151b28;--line:#2b3851;--accent:#79a7ff;--safe:#5ed6a8}
*{box-sizing:border-box}html,body{margin:0;max-width:100%;overflow-x:hidden}
body{background:radial-gradient(circle at 80% 0,#1b2b4a 0,transparent 38rem),#0a0e16;color:var(--ink)}
main{width:min(1100px,100%);margin:auto;padding:1rem}
header,section{min-width:0;background:color-mix(in srgb,var(--panel) 94%,transparent);border:1px solid var(--line);border-radius:1rem;padding:1rem;margin:0 0 1rem;box-shadow:0 14px 40px #0004}
h1{font-size:clamp(2rem,7vw,4rem);line-height:1;margin:.2rem 0 1rem}h2{font-size:1.1rem}
h1,h2,p,span,strong,small{overflow-wrap:anywhere}.eyebrow{color:var(--accent);font-weight:750;letter-spacing:.11em;font-size:.72rem}
.boundary{display:grid;gap:.25rem;border-left:4px solid var(--safe);background:#0c241f;padding:.75rem;border-radius:.25rem .65rem .65rem .25rem}.boundary span{color:#c5ded7}
.scenario-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.65rem}.scenario-grid button{display:grid;gap:.2rem}.scenario-grid small{font-weight:500;opacity:.75}
.row,.controls{display:flex;flex-wrap:wrap;gap:.7rem;align-items:end;margin:.75rem 0}
label{display:grid;gap:.3rem;min-width:0;flex:1 1 12rem;color:var(--muted)}
button,input,select{font:inherit;min-width:0;max-width:100%;min-height:2.75rem;border-radius:.58rem;border:1px solid #536582;padding:.55rem .7rem}
button{background:#2c5fbf;color:white;font-weight:700;cursor:pointer}button:hover{background:#3971d6}
button:focus-visible,input:focus-visible,select:focus-visible,pre:focus-visible{outline:3px solid #ffd36b;outline-offset:2px}
input,select{background:#0c111c;color:var(--ink)}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#080b11;border:1px solid #252f43;border-radius:.6rem;padding:.8rem;max-width:100%;max-height:36rem;overflow-y:auto}
#notice{min-height:1.5rem;color:#b8c8e8}
@media(max-width:680px){.scenario-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:480px){main{padding:.5rem}header,section{padding:.75rem;border-radius:.7rem}.controls>*{flex-basis:100%}.row button{flex:1 1 8rem}.scenario-grid{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

_PANEL_JS: Final = r"""
"use strict";
const byId=(id)=>document.getElementById(id);
const value=(id)=>byId(id).value;
const optional=(id)=>value(id)===""?null:value(id);
async function readAction(payload){
  byId("notice").textContent="Cargando lectura…";
  try{
    const response=await fetch("/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),credentials:"same-origin"});
    const result=await response.json();
    render(result.state);
    byId("notice").textContent=result.ok?"Lectura completada.":`${result.problem.code}: ${result.problem.title}`;
  }catch(_error){byId("notice").textContent="El panel local no está disponible."}
}
function render(state){
  byId("scenario").textContent=state.scenario;
  byId("report-status").value=state.scenario_status;
  const select=byId("report-select");select.replaceChildren();
  for(const report of state.reports){
    const option=document.createElement("option");
    option.value=String(report.report_id);
    option.textContent=`${report.status} · ${report.report_id.slice(0,18)}…`;
    select.append(option);
  }
  byId("state").textContent=JSON.stringify(state,null,2);
}
for(const button of document.querySelectorAll("[data-scenario]"))button.addEventListener("click",()=>readAction({action:"select_scenario",scenario:button.dataset.scenario,page_size:50}));
for(const button of document.querySelectorAll("[data-page]"))button.addEventListener("click",()=>{
  const [resource,direction]=button.dataset.page.split(":");
  if(resource==="reports")readAction({action:"reports",direction,page_size:Number(value("report-size")),status:optional("report-status")});
  if(resource==="findings")readAction({action:"findings",direction,page_size:Number(value("finding-size")),kind:optional("finding-kind"),severity:optional("finding-severity")});
  if(resource==="impacts")readAction({action:"impacts",direction,page_size:Number(value("impact-size")),kind:optional("impact-kind")});
});
byId("select-report").addEventListener("click",()=>readAction({action:"select_report",report_id:value("report-select")}));
fetch("/state",{credentials:"same-origin"}).then((response)=>response.json()).then((result)=>render(result.state)).catch(()=>{byId("notice").textContent="El panel local no está disponible."});
"""


@dataclass(slots=True)
class _SessionStore:
    bff: SemanticChangePanelBff
    maximum: int = MAX_SESSIONS
    _states: OrderedDict[str, _SessionState] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def get(self, identifier: str | None) -> tuple[str, _SessionState, bool]:
        with self._lock:
            if identifier is not None and identifier in self._states:
                state = self._states.pop(identifier)
                self._states[identifier] = state
                return identifier, state, False
            new_identifier = secrets.token_hex(16)
            state = self.bff.new_state()
            self._states[new_identifier] = state
            while len(self._states) > self.maximum:
                self._states.popitem(last=False)
            return new_identifier, state, True


class SemanticChangePanelServer(ThreadingHTTPServer):
    """HTTP server carrying only a bounded acceptance BFF."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        bff: SemanticChangePanelBff,
    ) -> None:
        if validate_loopback_host(address[0]) != LOOPBACK_V4:
            raise PanelError(
                78,
                "invalid_panel_bind",
                "El panel sólo escucha en el loopback IPv4.",
            )
        self.sessions = _SessionStore(bff)
        super().__init__(address, SemanticChangePanelHandler)

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


class SemanticChangePanelHandler(BaseHTTPRequestHandler):
    """Same-origin boundary; caller headers never reach the API."""

    server: SemanticChangePanelServer
    protocol_version = "HTTP/1.1"
    server_version = "SchemaBridgeM26Panel"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10.0)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        if not self._valid_host() or "?" in self.path:
            self._send_problem(400, "invalid_panel_request")
            return
        if self.path == "/":
            self._send_static("text/html; charset=utf-8", _PANEL_HTML)
        elif self.path == "/panel.css":
            self._send_static("text/css; charset=utf-8", _PANEL_CSS)
        elif self.path == "/panel.js":
            self._send_static("text/javascript; charset=utf-8", _PANEL_JS)
        elif self.path == "/state":
            identifier, state, is_new = self._state()
            self._send_json(
                200,
                {"ok": True, "state": state.public()},
                set_cookie=identifier if is_new else None,
            )
        else:
            self._send_problem(404, "panel_route_unavailable")

    def do_POST(self) -> None:
        if not self._valid_host() or self.path != "/action" or "?" in self.path:
            self._send_problem(400, "invalid_panel_request")
            return
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self._send_problem(400, "invalid_panel_request")
            return
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) > 1:
            self._send_problem(400, "invalid_panel_request")
            return
        origin = origins[0] if origins else None
        if origin is not None and origin != self._expected_origin():
            self._send_problem(403, "origin_denied")
            return
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            self._send_problem(415, "json_required")
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self._send_problem(400, "invalid_panel_request")
            return
        length = int(lengths[0])
        if not 1 <= length <= MAX_BROWSER_BODY_BYTES:
            self._send_problem(413, "panel_request_too_large")
            return
        raw = self.rfile.read(length)
        try:
            action = json.loads(raw, object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, PanelError):
            self._send_problem(400, "invalid_json")
            return
        identifier, state, is_new = self._state()
        result = self.server.sessions.bff.apply(state, action)
        problem = result.get("problem")
        status = 200 if result["ok"] else _bounded_int(_object(problem)["status"], 400, 599)
        self._send_json(
            status,
            result,
            set_cookie=identifier if is_new else None,
        )

    def do_HEAD(self) -> None:
        self._send_problem(405, "method_not_allowed", write_body=False)

    def do_PUT(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def do_PATCH(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def do_DELETE(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def do_OPTIONS(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def do_TRACE(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def do_CONNECT(self) -> None:
        self._send_problem(405, "method_not_allowed")

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", failobj=[])
        return len(values) == 1 and values[0] == self._expected_authority()

    def _expected_authority(self) -> str:
        host, port = self.server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"{host_text}:{port}"

    def _expected_origin(self) -> str:
        return f"http://{self._expected_authority()}"

    def _state(self) -> tuple[str, _SessionState, bool]:
        cookie = SimpleCookie()
        cookie_headers = self.headers.get_all("Cookie", failobj=[])
        try:
            if len(cookie_headers) == 1:
                cookie.load(cookie_headers[0])
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(SESSION_COOKIE)
        identifier = (
            morsel.value
            if morsel is not None and re.fullmatch(r"[0-9a-f]{32}", morsel.value)
            else None
        )
        return self.server.sessions.get(identifier)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )

    def _send_static(self, content_type: str, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_problem(
        self,
        status: int,
        code: str,
        *,
        write_body: bool = True,
    ) -> None:
        safe_status = status if status in _PROBLEM_TITLES else 500
        self.close_connection = True
        self._send_json(
            safe_status,
            {
                "ok": False,
                "problem": _problem(
                    safe_status,
                    code,
                    _PROBLEM_TITLES[safe_status],
                ),
            },
            write_body=write_body,
        )

    def _send_json(
        self,
        status: int,
        content: Mapping[str, object],
        *,
        set_cookie: str | None = None,
        write_body: bool = True,
    ) -> None:
        body = json.dumps(content, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        if set_cookie is not None:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={set_cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600",
            )
        self.end_headers()
        if write_body:
            self.wfile.write(body)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only M26 semantic-change acceptance panel.",
    )
    parser.add_argument("--bind", default=LOOPBACK_V4)
    parser.add_argument("--port", type=int, default=DEFAULT_PANEL_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--bearer-file", type=Path)
    parser.add_argument("--synthetic-fixture", action="store_true")
    parser.add_argument("--request-timeout", type=float, default=5.0)
    return parser


def build_server(arguments: argparse.Namespace) -> SemanticChangePanelServer:
    require_secret_free_environment(os.environ)
    bind = validate_loopback_host(str(arguments.bind))
    port = int(arguments.port)
    if not 1 <= port <= 65535:
        raise PanelError(78, "invalid_port", "El puerto del panel no es válido.")
    if bool(arguments.synthetic_fixture):
        if arguments.bearer_file is not None:
            raise PanelError(
                78,
                "conflicting_source",
                "Elige fixture sintética o upstream autenticado, no ambos.",
            )
        transport: SemanticChangeTransport = SyntheticSemanticChangeTransport.create()
    else:
        if arguments.bearer_file is None:
            raise PanelError(
                78,
                "missing_bearer_file",
                "El modo upstream requiere un archivo bearer.",
            )
        transport = HttpSemanticChangeTransport(
            target=UpstreamTarget.parse(str(arguments.upstream)),
            bearer=read_bearer_file(Path(arguments.bearer_file)),
            timeout_seconds=float(arguments.request_timeout),
        )
    return SemanticChangePanelServer(
        (bind, port),
        SemanticChangePanelBff(transport),
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        server = build_server(arguments)
    except PanelError as error:
        print(f"M26 panel refused to start: {error.code}.")
        return error.status
    try:
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        print(f"M26 local read-only acceptance panel: http://{host_text}:{port}")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
