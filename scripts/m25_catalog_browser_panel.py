#!/usr/bin/env python3
"""Loopback-only M25 catalog browser-acceptance panel.

This is deliberately local development instrumentation, not a product UI.  It
keeps tenant bearer tokens and inventory cursors on the server side and exposes
only a small, sanitized catalog projection to the browser.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol
from urllib.parse import quote, urlencode, urlsplit

LOOPBACK_V4: Final = "127.0.0.1"
DEFAULT_PANEL_PORT: Final = 8510
DEFAULT_UPSTREAM: Final = "http://127.0.0.1:8520"
MAX_BROWSER_BODY_BYTES: Final = 8 * 1024
MAX_UPSTREAM_BODY_BYTES: Final = 1024 * 1024
MAX_TOKEN_BYTES: Final = 4096
MAX_CURSOR_HISTORY: Final = 64
MAX_SESSIONS: Final = 16
MAX_RATE_PROBE_REQUESTS: Final = 64
MAX_PAGE_SIZE: Final = 50
CURSOR_EXPIRY_PROBE_SECONDS: Final = 900
ALIASES: Final = frozenset({"small", "large", "datahub"})
SESSION_COOKIE: Final = "sb_m25_panel_session"

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_CONNECTION_ASSETS_PATH = re.compile(r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}/assets$")
_FIELDS_PATH = re.compile(
    r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}"
    r"/assets/[^/?#]{1,1500}/fields$"
)
_REFRESH_REQUEST_PATH = re.compile(r"^/v1/catalog/connections/[a-z0-9][a-z0-9_-]{2,199}/refreshes$")
_REFRESH_INSPECTION_PATH = re.compile(r"^/v1/catalog/refreshes/[a-z0-9][a-z0-9_-]{2,199}$")
_SAFE_BEARER = re.compile(r"^[\x21-\x7e]{16,4096}$")
_SAFE_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")

_PROBLEM_TITLES: Final[dict[int, str]] = {
    400: "The local acceptance request was rejected.",
    401: "The acceptance identity was not authenticated.",
    403: "The catalog operation was not authorized.",
    404: "The catalog resource is not available.",
    405: "The panel method is not allowed.",
    409: "The catalog request conflicts with current state.",
    413: "The local acceptance request is too large.",
    415: "The panel accepts JSON actions only.",
    422: "The catalog request is invalid.",
    429: "The configured request capacity has been reached.",
    500: "The catalog request failed safely.",
    502: "The local catalog API returned an invalid response.",
    503: "The catalog service is temporarily unavailable.",
    504: "The local catalog API did not respond in time.",
}

_CONNECTION_QUERY_KEYS: Final = frozenset({"query", "status", "page_size", "cursor"})
_ASSET_QUERY_KEYS: Final = frozenset(
    {"query", "platform", "schema_name", "page_size", "cursor", "generation"}
)
_FIELD_QUERY_KEYS: Final = frozenset({"query", "native_type", "page_size", "cursor", "generation"})

_ACTION_KEYS: Final[dict[str, frozenset[str]]] = {
    "select_alias": frozenset({"action", "alias"}),
    "connections": frozenset({"action", "direction", "page_size", "query", "status"}),
    "select_connection": frozenset({"action", "connection_id"}),
    "assets": frozenset(
        {
            "action",
            "direction",
            "page_size",
            "query",
            "platform",
            "schema_name",
        }
    ),
    "select_asset": frozenset({"action", "asset_id"}),
    "fields": frozenset({"action", "direction", "page_size", "query", "native_type"}),
    "refresh": frozenset({"action", "mode"}),
    "refresh_status": frozenset({"action"}),
    "cursor_probe": frozenset({"action", "resource", "probe"}),
    "rate_probe": frozenset({"action", "requests"}),
}


class PanelError(RuntimeError):
    """A sanitized, browser-safe panel failure."""

    def __init__(self, status: int, code: str, title: str) -> None:
        self.status = status
        self.code = code
        self.title = title
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status: int
    payload: object
    retry_after: int | None = None


class CatalogTransport(Protocol):
    def request(
        self,
        *,
        alias: str,
        method: str,
        path: str,
        query: Mapping[str, str | int],
        body: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> UpstreamResponse:
        """Make one allowlisted API request with a server-held bearer."""


def _forbidden_environment_key(key: str) -> bool:
    normalized = key.upper()
    if normalized in {"DATABASE_URL", "DATAHUB_GMS_TOKEN", "OPENAI_API_KEY"}:
        return True
    if normalized.startswith("SCHEMABRIDGE_CONTROL") and normalized.endswith("DATABASE_URL"):
        return True
    return "CURSOR_SIGNING_KEY" in normalized


def require_secret_free_environment(environment: Mapping[str, str]) -> None:
    """Fail closed when the acceptance relay process inherited protected secrets."""

    if any(_forbidden_environment_key(key) for key in environment):
        raise PanelError(
            78,
            "forbidden_environment",
            "The panel process inherited a forbidden secret-bearing environment.",
        )


def validate_loopback_host(host: str) -> str:
    """Accept only a numeric loopback address, avoiding DNS rebinding."""

    value = host.strip()
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise PanelError(
            78, "invalid_loopback", "A numeric loopback address is required."
        ) from error
    if not parsed.is_loopback:
        raise PanelError(78, "invalid_loopback", "A numeric loopback address is required.")
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
                "The upstream must be one loopback HTTP origin.",
            )
        host = validate_loopback_host(parsed.hostname)
        try:
            port = parsed.port
        except ValueError as error:
            raise PanelError(78, "invalid_upstream", "The upstream port is invalid.") from error
        if port is None or not 1 <= port <= 65535:
            raise PanelError(78, "invalid_upstream", "The upstream port is invalid.")
        return cls(host=host, port=port)


def read_bearer_file(path: Path) -> str:
    """Read one bounded, owner-only bearer file without putting it in the environment."""

    try:
        file_stat = path.lstat()
    except OSError as error:
        raise PanelError(78, "invalid_bearer_file", "A bearer file is unavailable.") from error
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or not 0 < file_stat.st_size <= MAX_TOKEN_BYTES
    ):
        raise PanelError(78, "invalid_bearer_file", "A bearer file is not safely configured.")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PanelError(78, "invalid_bearer_file", "A bearer file is unavailable.") from error
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise PanelError(78, "invalid_bearer_file", "A bearer file is invalid.") from error
    if not _SAFE_BEARER.fullmatch(token):
        raise PanelError(78, "invalid_bearer_file", "A bearer file is invalid.")
    return token


def _validate_upstream_request(
    *,
    method: str,
    path: str,
    query: Mapping[str, str | int],
    body: Mapping[str, object] | None,
    idempotency_key: str | None,
) -> None:
    """Enforce the complete upstream endpoint/method/parameter allowlist."""

    query_keys = frozenset(query)
    allowed = False
    if method == "GET" and path == "/v1/catalog/connections":
        allowed = query_keys <= _CONNECTION_QUERY_KEYS and body is None
    elif method == "GET" and _CONNECTION_ASSETS_PATH.fullmatch(path):
        allowed = query_keys <= _ASSET_QUERY_KEYS and body is None
    elif method == "GET" and _FIELDS_PATH.fullmatch(path):
        allowed = query_keys <= _FIELD_QUERY_KEYS and body is None
    elif method == "POST" and _REFRESH_REQUEST_PATH.fullmatch(path):
        allowed = (
            not query
            and body is not None
            and frozenset(body) == {"mode", "confirmation"}
            and body.get("mode") in {"full", "delta"}
            and body.get("confirmation") == "REQUEST CATALOG REFRESH"
            and idempotency_key is not None
        )
    elif method == "GET" and _REFRESH_INSPECTION_PATH.fullmatch(path):
        allowed = not query and body is None
    if not allowed:
        raise PanelError(
            500,
            "upstream_request_denied",
            "The panel denied a non-allowlisted upstream request.",
        )
    _validate_upstream_query(path, query)
    if method != "POST" and idempotency_key is not None:
        raise PanelError(
            500,
            "upstream_request_denied",
            "The panel denied a non-allowlisted upstream request.",
        )
    if idempotency_key is not None and not _SAFE_IDEMPOTENCY.fullmatch(idempotency_key):
        raise PanelError(
            500,
            "upstream_request_denied",
            "The panel denied a non-allowlisted upstream request.",
        )


def _validate_upstream_query(
    path: str,
    query: Mapping[str, str | int],
) -> None:
    for key, value in query.items():
        if key in {"page_size"}:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
                raise PanelError(
                    500, "upstream_request_denied", "The panel denied an invalid query."
                )
        elif key == "generation":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10**12:
                raise PanelError(
                    500, "upstream_request_denied", "The panel denied an invalid query."
                )
        elif key == "cursor":
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 1024
                or not value.isascii()
                or any(ord(character) < 33 or ord(character) == 127 for character in value)
            ):
                raise PanelError(
                    500, "upstream_request_denied", "The panel denied an invalid query."
                )
        else:
            maximum = {
                "query": 200,
                "status": 16,
                "platform": 100,
                "schema_name": 200,
                "native_type": 200,
            }.get(key)
            if (
                maximum is None
                or not isinstance(value, str)
                or not 1 <= len(value) <= maximum
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise PanelError(
                    500, "upstream_request_denied", "The panel denied an invalid query."
                )
            if key == "status" and value not in {"enabled", "disabled"}:
                raise PanelError(
                    500, "upstream_request_denied", "The panel denied an invalid query."
                )
    if path == "/v1/catalog/connections" and "generation" in query:
        raise PanelError(500, "upstream_request_denied", "The panel denied an invalid query.")


@dataclass(frozen=True, slots=True)
class HttpCatalogTransport:
    target: UpstreamTarget
    bearers: Mapping[str, str]
    timeout_seconds: float = 5.0
    max_response_bytes: int = MAX_UPSTREAM_BODY_BYTES

    def __post_init__(self) -> None:
        copied_bearers = dict(self.bearers)
        if frozenset(copied_bearers) != ALIASES:
            raise PanelError(78, "invalid_aliases", "All acceptance aliases are required.")
        if any(
            not isinstance(token, str) or not _SAFE_BEARER.fullmatch(token)
            for token in copied_bearers.values()
        ):
            raise PanelError(78, "invalid_aliases", "Acceptance identities are invalid.")
        if len(set(copied_bearers.values())) != len(copied_bearers):
            raise PanelError(
                78,
                "invalid_aliases",
                "Acceptance aliases must use distinct identities.",
            )
        object.__setattr__(self, "bearers", MappingProxyType(copied_bearers))
        if not 0.1 <= self.timeout_seconds <= 10.0:
            raise PanelError(78, "invalid_timeout", "The upstream timeout is invalid.")
        if not 1024 <= self.max_response_bytes <= MAX_UPSTREAM_BODY_BYTES:
            raise PanelError(78, "invalid_response_limit", "The response limit is invalid.")

    def request(
        self,
        *,
        alias: str,
        method: str,
        path: str,
        query: Mapping[str, str | int],
        body: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> UpstreamResponse:
        if alias not in ALIASES:
            raise PanelError(400, "invalid_alias", "Choose a configured acceptance alias.")
        _validate_upstream_request(
            method=method,
            path=path,
            query=query,
            body=body,
            idempotency_key=idempotency_key,
        )
        target = path
        if query:
            target = f"{path}?{urlencode(tuple(query.items()))}"
        encoded_body = (
            None
            if body is None
            else json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearers[alias]}",
            "Connection": "close",
        }
        if encoded_body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded_body))
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        connection = http.client.HTTPConnection(
            self.target.host,
            self.target.port,
            timeout=self.timeout_seconds,
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.request(method, target, body=encoded_body, headers=headers)
            response = connection.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise PanelError(
                    502,
                    "upstream_response_too_large",
                    _PROBLEM_TITLES[502],
                )
            protected_values = tuple(self.bearers.values()) + (
                (idempotency_key,) if idempotency_key is not None else ()
            )
            if any(value.encode("ascii") in raw for value in protected_values):
                raise PanelError(
                    502,
                    "protected_value_in_upstream_response",
                    _PROBLEM_TITLES[502],
                )
            content_type = response.getheader("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
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
            retry_after = _bounded_retry_after(response.getheader("Retry-After"))
            return UpstreamResponse(
                status=response.status,
                payload=payload,
                retry_after=retry_after,
            )
        except TimeoutError as error:
            raise PanelError(504, "upstream_timeout", _PROBLEM_TITLES[504]) from error
        except (ConnectionError, OSError, http.client.HTTPException) as error:
            raise PanelError(503, "upstream_unavailable", _PROBLEM_TITLES[503]) from error
        finally:
            if response is not None:
                response.close()
            connection.close()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PanelError(400, "duplicate_json_key", "Duplicate JSON keys are not accepted.")
        result[key] = value
    return result


def _bounded_retry_after(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if 1 <= parsed <= 60 else None


@dataclass(slots=True)
class _PageEntry:
    number: int
    request_cursor: str | None
    next_cursor: str | None = None
    next_cursor_recorded_at: float | None = None


@dataclass(slots=True)
class _Pager:
    entries: list[_PageEntry] = field(default_factory=list)
    position: int = 0
    filters: tuple[tuple[str, str | int], ...] | None = None
    generation: int | None = None

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
            self.generation = None
            return None
        if not self.entries or self.filters != filters:
            raise PanelError(
                409,
                "paging_context_changed",
                "Start at the first page after changing filters.",
            )
        if direction == "previous":
            if self.position == 0:
                raise PanelError(409, "no_previous_page", "There is no retained previous page.")
            self.position -= 1
            return self.entries[self.position].request_cursor
        if direction != "next":
            raise PanelError(400, "invalid_direction", "Choose first, next, or previous.")
        current = self.entries[self.position]
        if current.next_cursor is None:
            raise PanelError(409, "no_next_page", "There is no next page.")
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

    def record(self, *, next_cursor: str | None, generation: int | None) -> None:
        if not self.entries:
            raise PanelError(500, "paging_state_error", "Paging state is unavailable.")
        if self.generation is None:
            self.generation = generation
        elif generation is not None and generation != self.generation:
            raise PanelError(
                409,
                "catalog_generation_changed",
                "The catalog generation changed; restart at the first page.",
            )
        self.entries[self.position].next_cursor = next_cursor
        self.entries[self.position].next_cursor_recorded_at = (
            time.monotonic() if next_cursor is not None else None
        )

    @property
    def current(self) -> _PageEntry | None:
        return self.entries[self.position] if self.entries else None

    def public(self) -> dict[str, object]:
        current = self.current
        return {
            "page": current.number if current is not None else 0,
            "can_previous": self.position > 0,
            "has_next": bool(current is not None and current.next_cursor),
            "retained_pages": len(self.entries),
            "generation": self.generation,
        }


@dataclass(slots=True)
class _SessionState:
    alias: str = "small"
    selected_connection: str | None = None
    selected_asset: str | None = None
    selected_asset_generation: int | None = None
    connections: list[dict[str, object]] = field(default_factory=list)
    assets: list[dict[str, object]] = field(default_factory=list)
    fields: list[dict[str, object]] = field(default_factory=list)
    connection_pager: _Pager = field(default_factory=_Pager)
    asset_pager: _Pager = field(default_factory=_Pager)
    field_pager: _Pager = field(default_factory=_Pager)
    connection_stale: bool | None = None
    asset_stale: bool | None = None
    field_stale: bool | None = None
    connection_as_of: str | None = None
    asset_as_of: str | None = None
    field_as_of: str | None = None
    cursor_status: dict[str, object] = field(default_factory=lambda: {"state": "not_tested"})
    rate_status: dict[str, object] = field(default_factory=lambda: {"state": "not_tested"})
    refresh_id: str | None = None
    refresh: dict[str, object] | None = None
    last_problem: dict[str, object] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def reset_for_alias(self, alias: str) -> None:
        self.alias = alias
        self.selected_connection = None
        self.selected_asset = None
        self.selected_asset_generation = None
        self.connections = []
        self.assets = []
        self.fields = []
        self.connection_pager = _Pager()
        self.asset_pager = _Pager()
        self.field_pager = _Pager()
        self.connection_stale = None
        self.asset_stale = None
        self.field_stale = None
        self.connection_as_of = None
        self.asset_as_of = None
        self.field_as_of = None
        self.cursor_status = {"state": "not_tested"}
        self.rate_status = {"state": "not_tested"}
        self.refresh_id = None
        self.refresh = None
        self.last_problem = None

    def public(self) -> dict[str, object]:
        return {
            "instrumentation": "M25 local development acceptance — not a product UI",
            "alias": self.alias,
            "selected_connection": self.selected_connection,
            "selected_asset": self.selected_asset,
            "connections": self.connections,
            "assets": self.assets,
            "fields": self.fields,
            "paging": {
                "connections": self.connection_pager.public(),
                "assets": self.asset_pager.public(),
                "fields": self.field_pager.public(),
            },
            "freshness": {
                "connections_stale": self.connection_stale,
                "assets_stale": self.asset_stale,
                "fields_stale": self.field_stale,
                "connections_as_of": self.connection_as_of,
                "assets_as_of": self.asset_as_of,
                "fields_as_of": self.field_as_of,
            },
            "cursor_check": self.cursor_status,
            "rate_check": self.rate_status,
            "refresh": self.refresh,
            "last_problem": self.last_problem,
        }


@dataclass(slots=True)
class CatalogPanelBff:
    transport: CatalogTransport

    def new_state(self) -> _SessionState:
        return _SessionState()

    def apply(self, state: _SessionState, raw_action: object) -> dict[str, object]:
        with state.lock:
            try:
                action = _validated_action(raw_action)
                self._dispatch(state, action)
                state.last_problem = None
                return {"ok": True, "state": state.public()}
            except PanelError as error:
                problem = _problem(error.status, error.code, error.title)
                state.last_problem = problem
                return {"ok": False, "problem": problem, "state": state.public()}
            except Exception:
                problem = _problem(
                    500,
                    "panel_internal_error",
                    _PROBLEM_TITLES[500],
                )
                state.last_problem = problem
                return {"ok": False, "problem": problem, "state": state.public()}

    def _dispatch(self, state: _SessionState, action: dict[str, object]) -> None:
        name = _required_string(action, "action", 32)
        if name == "select_alias":
            alias = _required_string(action, "alias", 8)
            if alias not in ALIASES:
                raise PanelError(400, "invalid_alias", "Choose a configured acceptance alias.")
            state.reset_for_alias(alias)
        elif name == "connections":
            self._connections(state, action)
        elif name == "select_connection":
            self._select_connection(state, action)
        elif name == "assets":
            self._assets(state, action)
        elif name == "select_asset":
            self._select_asset(state, action)
        elif name == "fields":
            self._fields(state, action)
        elif name == "refresh":
            self._refresh(state, action)
        elif name == "refresh_status":
            self._refresh_status(state)
        elif name == "cursor_probe":
            self._cursor_probe(state, action)
        elif name == "rate_probe":
            self._rate_probe(state, action)
        else:
            raise PanelError(400, "invalid_action", "The panel action is not allowed.")

    def _connections(self, state: _SessionState, action: Mapping[str, object]) -> None:
        direction = _direction(action)
        filters = _filters(
            action,
            allowed=("query", "status"),
            enums={"status": frozenset({"enabled", "disabled"})},
        )
        page_size = _page_size(action)
        filter_key: tuple[tuple[str, str | int], ...] = tuple(
            sorted((*filters.items(), ("page_size", page_size)))
        )
        pager = _clone_pager(state.connection_pager)
        cursor = pager.prepare(direction=direction, filters=filter_key)
        query: dict[str, str | int] = {**filters, "page_size": page_size}
        if cursor is not None:
            query["cursor"] = cursor
        response = self.transport.request(
            alias=state.alias,
            method="GET",
            path="/v1/catalog/connections",
            query=query,
        )
        payload = _require_success(response)
        projected = _project_connection_page(payload)
        pager.record(next_cursor=_next_cursor(payload), generation=None)
        state.connection_pager = pager
        state.connections = projected["items"]
        state.connection_stale = projected["stale"]
        state.connection_as_of = projected["as_of"]
        visible_ids = {str(item["connection_id"]) for item in state.connections}
        if state.selected_connection not in visible_ids:
            state.selected_connection = None
            state.selected_asset = None
            state.assets = []
            state.fields = []

    def _select_connection(
        self,
        state: _SessionState,
        action: Mapping[str, object],
    ) -> None:
        identifier = _identifier(action, "connection_id")
        if identifier not in {str(item["connection_id"]) for item in state.connections}:
            raise PanelError(
                404,
                "connection_not_visible",
                "Select a connection from the current sanitized page.",
            )
        state.selected_connection = identifier
        state.selected_asset = None
        state.selected_asset_generation = None
        state.assets = []
        state.fields = []
        state.asset_pager = _Pager()
        state.field_pager = _Pager()
        state.refresh_id = None
        state.refresh = None

    def _assets(self, state: _SessionState, action: Mapping[str, object]) -> None:
        connection_id = _selected_connection(state)
        direction = _direction(action)
        filters = _filters(
            action,
            allowed=("query", "platform", "schema_name"),
            enums={},
        )
        page_size = _page_size(action)
        filter_key: tuple[tuple[str, str | int], ...] = tuple(
            sorted((*filters.items(), ("page_size", page_size)))
        )
        pager = _clone_pager(state.asset_pager)
        cursor = pager.prepare(direction=direction, filters=filter_key)
        query: dict[str, str | int] = {**filters, "page_size": page_size}
        if cursor is not None:
            query["cursor"] = cursor
        if pager.generation is not None:
            query["generation"] = pager.generation
        response = self.transport.request(
            alias=state.alias,
            method="GET",
            path=f"/v1/catalog/connections/{connection_id}/assets",
            query=query,
        )
        payload = _require_success(response)
        projected = _project_asset_page(payload)
        pager.record(
            next_cursor=_next_cursor(payload),
            generation=int(projected["generation"]),
        )
        state.asset_pager = pager
        state.assets = projected["items"]
        state.asset_stale = projected["stale"]
        state.asset_as_of = projected["as_of"]
        visible_ids = {str(item["asset_id"]) for item in state.assets}
        if state.selected_asset not in visible_ids:
            state.selected_asset = None
            state.selected_asset_generation = None
            state.fields = []

    def _select_asset(self, state: _SessionState, action: Mapping[str, object]) -> None:
        asset_id = _safe_asset_id(action.get("asset_id"))
        visible = {
            str(item["asset_id"]): _projected_int(item["generation"]) for item in state.assets
        }
        if asset_id not in visible:
            raise PanelError(
                404,
                "asset_not_visible",
                "Select an asset from the current sanitized page.",
            )
        state.selected_asset = asset_id
        state.selected_asset_generation = visible[asset_id]
        state.fields = []
        state.field_pager = _Pager(generation=visible[asset_id])

    def _fields(self, state: _SessionState, action: Mapping[str, object]) -> None:
        connection_id = _selected_connection(state)
        asset_id = state.selected_asset
        if asset_id is None or state.selected_asset_generation is None:
            raise PanelError(409, "asset_required", "Select one visible asset first.")
        direction = _direction(action)
        filters = _filters(
            action,
            allowed=("query", "native_type"),
            enums={},
        )
        page_size = _page_size(action)
        filter_key: tuple[tuple[str, str | int], ...] = tuple(
            sorted((*filters.items(), ("page_size", page_size)))
        )
        pager = _clone_pager(state.field_pager)
        cursor = pager.prepare(direction=direction, filters=filter_key)
        if pager.generation is None:
            pager.generation = state.selected_asset_generation
        query: dict[str, str | int] = {
            **filters,
            "page_size": page_size,
            "generation": pager.generation,
        }
        if cursor is not None:
            query["cursor"] = cursor
        encoded_asset = quote(asset_id, safe="")
        response = self.transport.request(
            alias=state.alias,
            method="GET",
            path=(f"/v1/catalog/connections/{connection_id}/assets/{encoded_asset}/fields"),
            query=query,
        )
        payload = _require_success(response)
        projected = _project_field_page(payload)
        pager.record(
            next_cursor=_next_cursor(payload),
            generation=int(projected["generation"]),
        )
        state.field_pager = pager
        state.fields = projected["items"]
        state.field_stale = projected["stale"]
        state.field_as_of = projected["as_of"]

    def _refresh(self, state: _SessionState, action: Mapping[str, object]) -> None:
        connection_id = _selected_connection(state)
        mode = _required_string(action, "mode", 5)
        if mode not in {"full", "delta"}:
            raise PanelError(400, "invalid_refresh_mode", "Choose full or delta refresh.")
        response = self.transport.request(
            alias=state.alias,
            method="POST",
            path=f"/v1/catalog/connections/{connection_id}/refreshes",
            query={},
            body={"mode": mode, "confirmation": "REQUEST CATALOG REFRESH"},
            idempotency_key=f"m25-panel-{secrets.token_urlsafe(24)}",
        )
        payload = _require_success(response, accepted_statuses={200, 202})
        if not isinstance(payload, dict) or not isinstance(payload.get("refresh"), dict):
            raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
        projected = _project_refresh(payload["refresh"])
        state.refresh_id = str(projected["refresh_id"])
        state.refresh = {
            **projected,
            "replayed": _safe_bool(payload.get("replayed"), "replayed"),
        }

    def _refresh_status(self, state: _SessionState) -> None:
        if state.refresh_id is None:
            raise PanelError(409, "refresh_required", "Request a refresh first.")
        response = self.transport.request(
            alias=state.alias,
            method="GET",
            path=f"/v1/catalog/refreshes/{state.refresh_id}",
            query={},
        )
        payload = _require_success(response)
        state.refresh = _project_refresh(payload)

    def _cursor_probe(
        self,
        state: _SessionState,
        action: Mapping[str, object],
    ) -> None:
        resource = _required_string(action, "resource", 16)
        probe = _required_string(action, "probe", 20)
        if probe not in {"tampered", "changed_filter", "cross_alias", "expired"}:
            raise PanelError(400, "invalid_cursor_probe", "Choose an allowed cursor check.")
        pager = {
            "connections": state.connection_pager,
            "assets": state.asset_pager,
            "fields": state.field_pager,
        }.get(resource)
        if pager is None or pager.current is None or pager.current.next_cursor is None:
            raise PanelError(
                409,
                "cursor_required",
                "Load a page with a next page before checking cursor denial.",
            )
        cursor = pager.current.next_cursor
        if probe == "expired":
            recorded_at = pager.current.next_cursor_recorded_at
            if recorded_at is None or time.monotonic() - recorded_at < CURSOR_EXPIRY_PROBE_SECONDS:
                raise PanelError(
                    409,
                    "cursor_not_expired",
                    "Wait for the complete cursor lifetime before checking expiry.",
                )
        alias = state.alias
        if probe == "tampered":
            cursor = f"{cursor[:-1]}{'a' if cursor[-1] != 'a' else 'b'}"
        elif probe == "cross_alias":
            alias = {
                "small": "large",
                "large": "small",
                "datahub": "small",
            }[state.alias]
        path, query = self._cursor_probe_request(state, resource, cursor)
        if probe == "changed_filter":
            query["query"] = "__m25_changed_filter__"
        response = self.transport.request(
            alias=alias,
            method="GET",
            path=path,
            query=query,
        )
        problem = _project_problem(response)
        state.cursor_status = {
            "state": "denied" if response.status >= 400 else "unexpectedly_accepted",
            "probe": probe,
            "resource": resource,
            "status": response.status,
            "code": problem["code"] if problem is not None else None,
        }

    def _cursor_probe_request(
        self,
        state: _SessionState,
        resource: str,
        cursor: str,
    ) -> tuple[str, dict[str, str | int]]:
        pager = {
            "connections": state.connection_pager,
            "assets": state.asset_pager,
            "fields": state.field_pager,
        }[resource]
        query = dict(pager.filters or ())
        query["cursor"] = cursor
        if resource == "connections":
            return "/v1/catalog/connections", query
        connection_id = _selected_connection(state)
        if pager.generation is not None:
            query["generation"] = pager.generation
        if resource == "assets":
            return f"/v1/catalog/connections/{connection_id}/assets", query
        if state.selected_asset is None:
            raise PanelError(409, "asset_required", "Select one visible asset first.")
        encoded_asset = quote(state.selected_asset, safe="")
        return (
            f"/v1/catalog/connections/{connection_id}/assets/{encoded_asset}/fields",
            query,
        )

    def _rate_probe(
        self,
        state: _SessionState,
        action: Mapping[str, object],
    ) -> None:
        requests = _strict_int(action.get("requests"), "requests", 1, MAX_RATE_PROBE_REQUESTS)
        completed = 0
        final_status = 200
        final_problem: dict[str, object] | None = None
        retry_after: int | None = None
        for _ in range(requests):
            response = self.transport.request(
                alias=state.alias,
                method="GET",
                path="/v1/catalog/connections",
                query={"page_size": 1},
            )
            completed += 1
            final_status = response.status
            if response.status >= 400:
                final_problem = _project_problem(response)
                retry_after = response.retry_after
                break
        state.rate_status = {
            "state": "denied" if final_status == 429 else "completed",
            "attempts": completed,
            "status": final_status,
            "code": final_problem["code"] if final_problem is not None else None,
            "retry_after_seconds": retry_after,
        }


def _clone_pager(value: _Pager) -> _Pager:
    return _Pager(
        entries=[
            _PageEntry(
                number=item.number,
                request_cursor=item.request_cursor,
                next_cursor=item.next_cursor,
                next_cursor_recorded_at=item.next_cursor_recorded_at,
            )
            for item in value.entries
        ],
        position=value.position,
        filters=value.filters,
        generation=value.generation,
    )


def _validated_action(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise PanelError(400, "invalid_action", "A JSON action object is required.")
    action = raw.get("action")
    if not isinstance(action, str) or action not in _ACTION_KEYS:
        raise PanelError(400, "invalid_action", "The panel action is not allowed.")
    if frozenset(raw) != _ACTION_KEYS[action]:
        raise PanelError(
            400,
            "invalid_action_parameters",
            "The action parameters do not match the allowlist.",
        )
    return raw


def _required_string(value: Mapping[str, object], key: str, maximum: int) -> str:
    raw = value.get(key)
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise PanelError(400, "invalid_action_parameters", "An action parameter is invalid.")
    return raw


def _identifier(value: Mapping[str, object], key: str) -> str:
    raw = _required_string(value, key, 200)
    if not _IDENTIFIER.fullmatch(raw):
        raise PanelError(400, "invalid_identifier", "The selected identifier is invalid.")
    return raw


def _safe_asset_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 500
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in value for part in ("/", "\\", "?", "#", "%", ".."))
    ):
        raise PanelError(400, "invalid_asset_id", "The selected asset identifier is invalid.")
    return value


def _direction(action: Mapping[str, object]) -> str:
    direction = _required_string(action, "direction", 8)
    if direction not in {"first", "next", "previous"}:
        raise PanelError(400, "invalid_direction", "Choose first, next, or previous.")
    return direction


def _strict_int(
    raw: object,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise PanelError(400, f"invalid_{name}", f"The {name} value is invalid.")
    return raw


def _projected_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PanelError(500, "panel_state_error", "Panel state is unavailable.")
    return value


def _page_size(action: Mapping[str, object]) -> int:
    return _strict_int(action.get("page_size"), "page_size", 1, MAX_PAGE_SIZE)


def _filters(
    action: Mapping[str, object],
    *,
    allowed: Sequence[str],
    enums: Mapping[str, frozenset[str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in allowed:
        raw = action.get(key)
        if raw is None:
            continue
        if (
            not isinstance(raw, str)
            or not 1 <= len(raw) <= 200
            or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        ):
            raise PanelError(400, "invalid_filter", "A catalog filter is invalid.")
        choices = enums.get(key)
        if choices is not None and raw not in choices:
            raise PanelError(400, "invalid_filter", "A catalog filter is invalid.")
        result[key] = raw
    return result


def _selected_connection(state: _SessionState) -> str:
    if state.selected_connection is None:
        raise PanelError(409, "connection_required", "Select one visible connection first.")
    return state.selected_connection


def _require_success(
    response: UpstreamResponse,
    *,
    accepted_statuses: set[int] | None = None,
) -> object:
    statuses = {200} if accepted_statuses is None else accepted_statuses
    if response.status not in statuses:
        problem = _project_problem(response)
        if problem is None:
            raise PanelError(
                502,
                "invalid_upstream_response",
                _PROBLEM_TITLES[502],
            )
        raise PanelError(
            response.status,
            str(problem["code"]),
            str(problem["title"]),
        )
    return response.payload


def _project_problem(response: UpstreamResponse) -> dict[str, object] | None:
    if response.status < 400:
        return None
    status = response.status if response.status in _PROBLEM_TITLES else 502
    raw_code: object = None
    if isinstance(response.payload, dict):
        raw_code = response.payload.get("code")
    code = (
        raw_code
        if isinstance(raw_code, str) and _SAFE_CODE.fullmatch(raw_code)
        else "upstream_request_failed"
    )
    result: dict[str, object] = {
        "status": status,
        "code": code,
        "title": _PROBLEM_TITLES[status],
    }
    if response.retry_after is not None:
        result["retry_after_seconds"] = response.retry_after
    return result


def _problem(status: int, code: str, title: str) -> dict[str, object]:
    safe_status = status if status in _PROBLEM_TITLES else 500
    safe_code = code if _SAFE_CODE.fullmatch(code) else "panel_request_failed"
    safe_title = (
        title
        if title in _PROBLEM_TITLES.values() or len(title) <= 120
        else _PROBLEM_TITLES[safe_status]
    )
    return {"status": safe_status, "code": safe_code, "title": safe_title}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_PAGE_SIZE:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return [_object(item) for item in value]


def _safe_text(value: object, name: str, maximum: int, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_bool(value: object, name: str, *, nullable: bool = False) -> bool | None:
    del name
    if nullable and value is None:
        return None
    if not isinstance(value, bool):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_enum(value: object, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_number(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    nullable: bool = False,
) -> int | None:
    del name
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 10**12:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _safe_text_list(value: object, name: str, *, maximum_items: int = 50) -> list[str]:
    del name
    if not isinstance(value, list) or len(value) > maximum_items:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    result: list[str] = []
    for item in value:
        projected = _safe_text(item, "list item", 200)
        assert projected is not None
        result.append(projected)
    return result


def _next_cursor(payload: object) -> str | None:
    value = _object(payload).get("next_cursor")
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return value


def _project_connection_page(payload: object) -> dict[str, Any]:
    root = _object(payload)
    if root.get("resource") != "connections":
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    projected_items: list[dict[str, object]] = []
    for raw in _items(root.get("items")):
        identifier = _safe_text(raw.get("connection_id"), "connection_id", 200)
        if identifier is None or not _IDENTIFIER.fullmatch(identifier):
            raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
        projected_items.append(
            {
                "connection_id": identifier,
                "display_name": _safe_text(raw.get("display_name"), "display_name", 200),
                "kind": _safe_enum(
                    raw.get("kind"),
                    frozenset({"datahub_graphql", "synthetic"}),
                ),
                "environment": _safe_text(raw.get("environment"), "environment", 100),
                "catalog_scope": _safe_text(raw.get("catalog_scope"), "catalog_scope", 200),
                "status": _safe_enum(
                    raw.get("status"),
                    frozenset({"enabled", "disabled"}),
                ),
                "active_generation": _safe_number(
                    raw.get("active_generation"),
                    "active_generation",
                    minimum=1,
                    nullable=True,
                ),
                "asset_count": _safe_number(raw.get("asset_count"), "asset_count"),
                "field_count": _safe_number(raw.get("field_count"), "field_count"),
                "last_completed_at": _safe_text(
                    raw.get("last_completed_at"),
                    "last_completed_at",
                    64,
                    nullable=True,
                ),
                "stale": _safe_bool(raw.get("stale"), "stale"),
            }
        )
    stale = _safe_bool(root.get("stale"), "stale")
    assert isinstance(stale, bool)
    as_of = _safe_text(root.get("as_of"), "as_of", 64)
    return {"items": projected_items, "stale": stale, "as_of": as_of}


def _project_asset_page(payload: object) -> dict[str, Any]:
    root = _object(payload)
    if root.get("resource") != "assets":
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    generation = _safe_number(root.get("generation"), "generation", minimum=1)
    assert isinstance(generation, int)
    projected_items: list[dict[str, object]] = []
    for raw in _items(root.get("items")):
        item_generation = _safe_number(raw.get("generation"), "generation", minimum=1)
        if item_generation != generation:
            raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
        projected_items.append(
            {
                "connection_id": _safe_text(raw.get("connection_id"), "connection_id", 200),
                "asset_id": _safe_text(raw.get("asset_id"), "asset_id", 500),
                "generation": item_generation,
                "qualified_name": _safe_text(raw.get("qualified_name"), "qualified_name", 500),
                "display_name": _safe_text(raw.get("display_name"), "display_name", 300),
                "platform": _safe_text(raw.get("platform"), "platform", 100),
                "environment": _safe_text(raw.get("environment"), "environment", 100),
                "schema_name": _safe_text(
                    raw.get("schema_name"),
                    "schema_name",
                    200,
                    nullable=True,
                ),
                "description": _safe_text(
                    raw.get("description"),
                    "description",
                    2000,
                    nullable=True,
                ),
                "field_count": _safe_number(raw.get("field_count"), "field_count"),
                "observed_at": _safe_text(raw.get("observed_at"), "observed_at", 64),
            }
        )
    stale = _safe_bool(root.get("stale"), "stale")
    assert isinstance(stale, bool)
    as_of = _safe_text(root.get("as_of"), "as_of", 64)
    return {
        "items": projected_items,
        "stale": stale,
        "generation": generation,
        "as_of": as_of,
    }


def _project_field_page(payload: object) -> dict[str, Any]:
    root = _object(payload)
    if root.get("resource") != "fields":
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    generation = _safe_number(root.get("generation"), "generation", minimum=1)
    assert isinstance(generation, int)
    projected_items: list[dict[str, object]] = []
    for raw in _items(root.get("items")):
        item_generation = _safe_number(raw.get("generation"), "generation", minimum=1)
        if item_generation != generation:
            raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
        projected_items.append(
            {
                "connection_id": _safe_text(raw.get("connection_id"), "connection_id", 200),
                "asset_id": _safe_text(raw.get("asset_id"), "asset_id", 500),
                "field_path": _safe_text_list(
                    raw.get("field_path"), "field_path", maximum_items=20
                ),
                "generation": item_generation,
                "native_type": _safe_text(
                    raw.get("native_type"),
                    "native_type",
                    200,
                    nullable=True,
                ),
                "description": _safe_text(
                    raw.get("description"),
                    "description",
                    2000,
                    nullable=True,
                ),
                "nullable": _safe_bool(raw.get("nullable"), "nullable", nullable=True),
                "is_part_of_key": _safe_bool(
                    raw.get("is_part_of_key"),
                    "is_part_of_key",
                    nullable=True,
                ),
                "tags": _safe_text_list(raw.get("tags"), "tags"),
                "glossary_terms": _safe_text_list(
                    raw.get("glossary_terms"),
                    "glossary_terms",
                ),
                "observed_at": _safe_text(raw.get("observed_at"), "observed_at", 64),
            }
        )
    stale = _safe_bool(root.get("stale"), "stale")
    assert isinstance(stale, bool)
    as_of = _safe_text(root.get("as_of"), "as_of", 64)
    return {
        "items": projected_items,
        "stale": stale,
        "generation": generation,
        "as_of": as_of,
    }


def _project_refresh(payload: object) -> dict[str, object]:
    raw = _object(payload)
    refresh_id = _safe_text(raw.get("refresh_id"), "refresh_id", 200)
    connection_id = _safe_text(raw.get("connection_id"), "connection_id", 200)
    if (
        refresh_id is None
        or connection_id is None
        or not _IDENTIFIER.fullmatch(refresh_id)
        or not _IDENTIFIER.fullmatch(connection_id)
    ):
        raise PanelError(502, "invalid_upstream_response", _PROBLEM_TITLES[502])
    return {
        "refresh_id": refresh_id,
        "connection_id": connection_id,
        "mode": _safe_enum(raw.get("mode"), frozenset({"full", "delta"})),
        "status": _safe_enum(
            raw.get("status"),
            frozenset({"requested", "leased", "staging", "completed", "failed"}),
        ),
        "base_generation": _safe_number(raw.get("base_generation"), "base_generation"),
        "target_generation": _safe_number(
            raw.get("target_generation"),
            "target_generation",
            minimum=1,
        ),
        "source_page_count": _safe_number(
            raw.get("source_page_count"),
            "source_page_count",
        ),
        "asset_count": _safe_number(raw.get("asset_count"), "asset_count"),
        "field_count": _safe_number(raw.get("field_count"), "field_count"),
        "source_complete": _safe_bool(raw.get("source_complete"), "source_complete"),
        "failure_code": _safe_text(
            raw.get("failure_code"),
            "failure_code",
            64,
            nullable=True,
        ),
        "requested_at": _safe_text(raw.get("requested_at"), "requested_at", 64),
        "updated_at": _safe_text(raw.get("updated_at"), "updated_at", 64),
        "completed_at": _safe_text(
            raw.get("completed_at"),
            "completed_at",
            64,
            nullable=True,
        ),
    }


_PANEL_HTML: Final = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SchemaBridge M25 — aceptación local</title>
  <link rel="stylesheet" href="/panel.css">
  <script src="/panel.js" defer></script>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">INSTRUMENTACIÓN DE DESARROLLO · SOLO ACEPTACIÓN LOCAL</p>
    <h1>Catálogo dinámico M25</h1>
    <p>Este panel no es la interfaz de producto. Los bearers y cursores permanecen en el servidor.</p>
  </header>
  <section aria-labelledby="tenant-title">
    <h2 id="tenant-title">1. Empresa o fuente indexada</h2>
    <div class="row">
      <button data-alias="small">Small · 10 tablas</button>
      <button data-alias="large">Large · 5.434 tablas</button>
      <button data-alias="datahub">DataHub · 11 tablas indexadas</button>
    </div>
    <p>Alias activo: <strong id="alias">small</strong></p>
  </section>
  <section aria-labelledby="connections-title">
    <h2 id="connections-title">2. Conexiones</h2>
    <div class="controls">
      <label>Búsqueda <input id="connection-query" maxlength="200"></label>
      <label>Estado <select id="connection-status"><option value="">Todos</option><option>enabled</option><option>disabled</option></select></label>
      <label>Página <select id="connection-size"><option>1</option><option>17</option><option selected>50</option></select></label>
    </div>
    <div class="row"><button data-page="connections:first">Primera</button><button data-page="connections:previous">Anterior</button><button data-page="connections:next">Siguiente</button></div>
    <label>Conexión visible <select id="connection-select"></select></label>
    <button id="select-connection">Seleccionar conexión</button>
  </section>
  <section aria-labelledby="assets-title">
    <h2 id="assets-title">3. Tablas</h2>
    <div class="controls">
      <label>Búsqueda <input id="asset-query" maxlength="200"></label>
      <label>Plataforma <input id="asset-platform" maxlength="100"></label>
      <label>Esquema <input id="asset-schema" maxlength="200"></label>
      <label>Página <select id="asset-size"><option>1</option><option>17</option><option selected>50</option></select></label>
    </div>
    <div class="row"><button data-page="assets:first">Primera</button><button data-page="assets:previous">Anterior</button><button data-page="assets:next">Siguiente</button></div>
    <label>Tabla visible <select id="asset-select"></select></label>
    <button id="select-asset">Seleccionar tabla</button>
  </section>
  <section aria-labelledby="fields-title">
    <h2 id="fields-title">4. Campos</h2>
    <div class="controls">
      <label>Búsqueda <input id="field-query" maxlength="200"></label>
      <label>Tipo nativo <input id="field-type" maxlength="200"></label>
      <label>Página <select id="field-size"><option>1</option><option>17</option><option selected>50</option></select></label>
    </div>
    <div class="row"><button data-page="fields:first">Primera</button><button data-page="fields:previous">Anterior</button><button data-page="fields:next">Siguiente</button></div>
  </section>
  <section aria-labelledby="checks-title">
    <h2 id="checks-title">5. Estados y denegaciones</h2>
    <div class="row">
      <button data-refresh="full">Refresco completo</button>
      <button data-refresh="delta">Refresco delta</button>
      <button id="refresh-status">Consultar refresco</button>
    </div>
    <div class="controls">
      <label>Recurso del cursor <select id="cursor-resource"><option>connections</option><option selected>assets</option><option>fields</option></select></label>
      <label>Prueba <select id="cursor-probe"><option>tampered</option><option>changed_filter</option><option>cross_alias</option><option>expired</option></select></label>
      <button id="run-cursor-probe">Probar denegación</button>
      <label>Peticiones de tasa <input id="rate-count" type="number" min="1" max="64" value="32"></label>
      <button id="run-rate-probe">Probar límite</button>
    </div>
  </section>
  <section aria-labelledby="state-title">
    <h2 id="state-title">Estado sanitizado</h2>
    <p id="notice" role="status" aria-live="polite">Preparado.</p>
    <pre id="state" tabindex="0"></pre>
  </section>
</main>
</body>
</html>
"""

_PANEL_CSS: Final = """
:root{color-scheme:light dark;font:16px/1.45 system-ui,sans-serif;--accent:#4f7cff}
*{box-sizing:border-box}html,body{margin:0;max-width:100%;overflow-x:hidden}
body{background:#10131a;color:#eef2ff}main{width:min(1120px,100%);margin:auto;padding:1rem}
header,section{background:#191f2b;border:1px solid #30394c;border-radius:1rem;padding:1rem;margin:0 0 1rem}
h1,h2,p{overflow-wrap:anywhere}.eyebrow{color:#9fb6ff;font-weight:700;letter-spacing:.08em;font-size:.75rem}
.row,.controls{display:flex;flex-wrap:wrap;gap:.75rem;align-items:end;margin:.75rem 0}
label{display:grid;gap:.25rem;min-width:0;flex:1 1 10rem}
button,input,select{font:inherit;min-height:2.75rem;border-radius:.55rem;border:1px solid #56617a;padding:.55rem;max-width:100%}
button{background:var(--accent);color:white;font-weight:650;cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,pre:focus-visible{outline:3px solid #f6c85f;outline-offset:2px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#0d1016;border-radius:.5rem;padding:.75rem;max-width:100%}
#notice{min-height:1.5rem;color:#b9c8ff}
@media(max-width:480px){main{padding:.5rem}header,section{padding:.75rem;border-radius:.7rem}.controls>*{flex-basis:100%}.row button{flex:1 1 8rem}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

_PANEL_JS: Final = r"""
"use strict";
const byId=(id)=>document.getElementById(id);
const val=(id)=>byId(id).value;
const optional=(id)=>val(id)===""?null:val(id);
async function act(payload){
  byId("notice").textContent="Procesando…";
  try{
    const response=await fetch("/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),credentials:"same-origin"});
    const result=await response.json();
    render(result.state);
    byId("notice").textContent=result.ok?"Acción completada.":`${result.problem.code}: ${result.problem.title}`;
  }catch(_error){byId("notice").textContent="El panel local no está disponible."}
}
function options(id,items,key,label){
  const select=byId(id);select.replaceChildren();
  for(const item of items){const option=document.createElement("option");option.value=String(item[key]);option.textContent=String(item[label]);select.append(option)}
}
function render(state){
  byId("alias").textContent=state.alias;
  options("connection-select",state.connections,"connection_id","display_name");
  options("asset-select",state.assets,"asset_id","qualified_name");
  byId("state").textContent=JSON.stringify(state,null,2);
}
for(const button of document.querySelectorAll("[data-alias]"))button.addEventListener("click",()=>act({action:"select_alias",alias:button.dataset.alias}));
for(const button of document.querySelectorAll("[data-page]"))button.addEventListener("click",()=>{
  const [resource,direction]=button.dataset.page.split(":");
  if(resource==="connections")act({action:resource,direction,page_size:Number(val("connection-size")),query:optional("connection-query"),status:optional("connection-status")});
  if(resource==="assets")act({action:resource,direction,page_size:Number(val("asset-size")),query:optional("asset-query"),platform:optional("asset-platform"),schema_name:optional("asset-schema")});
  if(resource==="fields")act({action:resource,direction,page_size:Number(val("field-size")),query:optional("field-query"),native_type:optional("field-type")});
});
byId("select-connection").addEventListener("click",()=>act({action:"select_connection",connection_id:val("connection-select")}));
byId("select-asset").addEventListener("click",()=>act({action:"select_asset",asset_id:val("asset-select")}));
for(const button of document.querySelectorAll("[data-refresh]"))button.addEventListener("click",()=>act({action:"refresh",mode:button.dataset.refresh}));
byId("refresh-status").addEventListener("click",()=>act({action:"refresh_status"}));
byId("run-cursor-probe").addEventListener("click",()=>act({action:"cursor_probe",resource:val("cursor-resource"),probe:val("cursor-probe")}));
byId("run-rate-probe").addEventListener("click",()=>act({action:"rate_probe",requests:Number(val("rate-count"))}));
fetch("/state",{credentials:"same-origin"}).then((response)=>response.json()).then((result)=>render(result.state)).catch(()=>{byId("notice").textContent="El panel local no está disponible."});
"""


@dataclass(slots=True)
class _SessionStore:
    bff: CatalogPanelBff
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


class CatalogPanelServer(ThreadingHTTPServer):
    """HTTP server carrying only the acceptance BFF and bounded sessions."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        bff: CatalogPanelBff,
    ) -> None:
        if validate_loopback_host(address[0]) != LOOPBACK_V4:
            raise PanelError(
                78,
                "invalid_panel_bind",
                "The panel binds only to the IPv4 loopback address.",
            )
        self.sessions = _SessionStore(bff)
        super().__init__(address, CatalogPanelHandler)

    def handle_error(
        self,
        request: object,
        client_address: object,
    ) -> None:
        # Acceptance responses are explicit; never dump request data or tracebacks.
        del request, client_address


class CatalogPanelHandler(BaseHTTPRequestHandler):
    """Small same-origin boundary; caller headers never reach the catalog API."""

    server: CatalogPanelServer
    protocol_version = "HTTP/1.1"
    server_version = "SchemaBridgeM25Panel"
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
        status = 200 if result["ok"] else _projected_int(_object(result["problem"])["status"])
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
        return f"[{host_text}]:{port}" if ":" in host_text else f"{host_text}:{port}"

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
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
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
        # Early rejection may intentionally leave an untrusted request body unread.
        # Closing prevents those bytes from being parsed as a second request.
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
        description="Run the loopback-only M25 catalog acceptance panel.",
    )
    parser.add_argument("--bind", default=LOOPBACK_V4)
    parser.add_argument("--port", type=int, default=DEFAULT_PANEL_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--small-bearer-file", type=Path, required=True)
    parser.add_argument("--large-bearer-file", type=Path, required=True)
    parser.add_argument("--datahub-bearer-file", type=Path, required=True)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    return parser


def build_server(arguments: argparse.Namespace) -> CatalogPanelServer:
    require_secret_free_environment(os.environ)
    bind = validate_loopback_host(str(arguments.bind))
    port = int(arguments.port)
    if not 1 <= port <= 65535:
        raise PanelError(78, "invalid_port", "The panel port is invalid.")
    target = UpstreamTarget.parse(str(arguments.upstream))
    transport = HttpCatalogTransport(
        target=target,
        bearers={
            "small": read_bearer_file(Path(arguments.small_bearer_file)),
            "large": read_bearer_file(Path(arguments.large_bearer_file)),
            "datahub": read_bearer_file(Path(arguments.datahub_bearer_file)),
        },
        timeout_seconds=float(arguments.request_timeout),
    )
    return CatalogPanelServer((bind, port), CatalogPanelBff(transport))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        server = build_server(arguments)
    except PanelError as error:
        # The message is fixed and never contains a secret, path, URL, or exception text.
        print(f"M25 panel refused to start: {error.code}.")
        return error.status
    try:
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        print(f"M25 local development acceptance panel: http://{host_text}:{port}")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
