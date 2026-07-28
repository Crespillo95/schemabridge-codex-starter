"""Canonical HMAC-signed keyset cursors for the catalog inventory."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from schemabridge.application.ports.catalog_inventory import (
    InventoryCursorError,
)
from schemabridge.domain.catalog_inventory import (
    MAX_INVENTORY_CURSOR_BYTES,
    InventoryCursorBinding,
    InventoryCursorPosition,
    InventoryPageKey,
)

DEFAULT_INVENTORY_CURSOR_TTL = timedelta(minutes=15)

_FORMAT_VERSION = 1
_MINIMUM_SIGNING_KEY_BYTES = 32
_MINIMUM_SIGNING_KEY_DISTINCT_BYTES = 8
_MAXIMUM_CURSOR_TTL = timedelta(minutes=15)
_SIGNATURE_CONTEXT = b"schemabridge-inventory-cursor-v1\x00"
_SCOPE_CONTEXT = b"schemabridge-inventory-cursor-scope-v1\x00"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_KEYS = frozenset({"a", "c", "e", "f", "g", "i", "p", "r", "s", "v", "w"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SignedInventoryCursorCodec:
    """Encode and verify one short-lived cursor without exposing scope identifiers."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        ttl: timedelta = DEFAULT_INVENTORY_CURSOR_TTL,
        max_cursor_bytes: int = MAX_INVENTORY_CURSOR_BYTES,
    ) -> None:
        if (
            not isinstance(signing_key, bytes)
            or len(signing_key) < _MINIMUM_SIGNING_KEY_BYTES
            or len(set(signing_key)) < _MINIMUM_SIGNING_KEY_DISTINCT_BYTES
        ):
            raise ValueError("inventory cursor signing key is invalid")
        ttl_microseconds = _timedelta_microseconds(ttl)
        if ttl_microseconds <= 0 or ttl_microseconds > _timedelta_microseconds(_MAXIMUM_CURSOR_TTL):
            raise ValueError("inventory cursor TTL is invalid")
        if not 1 <= max_cursor_bytes <= MAX_INVENTORY_CURSOR_BYTES:
            raise ValueError("inventory cursor byte limit is invalid")
        self._signing_key = signing_key
        self._ttl_microseconds = ttl_microseconds
        self._max_cursor_bytes = max_cursor_bytes

    def encode(
        self,
        *,
        binding: InventoryCursorBinding,
        last_key: InventoryPageKey,
        issued_at: datetime,
        not_after: datetime | None = None,
    ) -> str:
        """Return a deterministic canonical cursor for one exact keyset position."""

        issued_microseconds = _datetime_microseconds(
            issued_at,
            error_message="inventory cursor issue time must include a timezone",
        )
        if issued_microseconds < 0:
            raise ValueError("inventory cursor issue time is invalid")
        expires_microseconds = issued_microseconds + self._ttl_microseconds
        if not_after is not None:
            not_after_microseconds = _datetime_microseconds(
                not_after,
                error_message="inventory cursor generation deadline must include a timezone",
            )
            if expires_microseconds > not_after_microseconds:
                raise _unavailable()
        payload: dict[str, object] = {
            "a": self._optional_scope_digest("asset", binding.asset_id),
            "c": self._optional_scope_digest("connection", binding.connection_id),
            "e": expires_microseconds,
            "f": binding.filter_fingerprint,
            "g": binding.generation,
            "i": issued_microseconds,
            "p": [last_key.sort_value, last_key.stable_id],
            "r": binding.resource.value,
            "s": binding.sort_fingerprint,
            "v": _FORMAT_VERSION,
            "w": self._scope_digest("workspace", binding.workspace_id),
        }
        try:
            payload_bytes = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeError) as error:
            raise _unavailable() from error
        signature = hmac.new(
            self._signing_key,
            _SIGNATURE_CONTEXT + payload_bytes,
            hashlib.sha256,
        ).digest()
        cursor = f"{_base64url_encode(payload_bytes)}.{_base64url_encode(signature)}"
        if len(cursor.encode("ascii")) > self._max_cursor_bytes:
            raise _unavailable()
        return cursor

    def decode(
        self,
        *,
        cursor: str,
        expected_binding: InventoryCursorBinding,
        at: datetime,
    ) -> InventoryCursorPosition:
        """Verify signature, lifetime, canonical form, and complete scope before use."""

        at_microseconds = _datetime_microseconds(
            at,
            error_message="inventory cursor verification time must include a timezone",
        )
        try:
            payload_bytes = self._verified_payload_bytes(cursor)
            payload = _load_canonical_payload(payload_bytes)
            position = self._validate_payload(
                payload,
                expected_binding=expected_binding,
                at_microseconds=at_microseconds,
            )
        except InventoryCursorError:
            raise
        except (binascii.Error, UnicodeError, ValueError, TypeError, OverflowError):
            raise _unavailable() from None
        return position

    def _verified_payload_bytes(self, cursor: str) -> bytes:
        if not isinstance(cursor, str):
            raise _unavailable()
        try:
            cursor_bytes = cursor.encode("ascii")
        except UnicodeEncodeError:
            raise _unavailable() from None
        if (
            not cursor_bytes
            or len(cursor_bytes) > self._max_cursor_bytes
            or _TOKEN_PATTERN.fullmatch(cursor) is None
        ):
            raise _unavailable()
        payload_segment, signature_segment = cursor.split(".", maxsplit=1)
        payload_bytes = _base64url_decode(payload_segment)
        supplied_signature = _base64url_decode(signature_segment)
        if len(supplied_signature) != hashlib.sha256().digest_size:
            raise _unavailable()
        expected_signature = hmac.new(
            self._signing_key,
            _SIGNATURE_CONTEXT + payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise _unavailable()
        return payload_bytes

    def _validate_payload(
        self,
        payload: Mapping[str, object],
        *,
        expected_binding: InventoryCursorBinding,
        at_microseconds: int,
    ) -> InventoryCursorPosition:
        if set(payload) != _PAYLOAD_KEYS:
            raise _unavailable()
        version = _exact_int(payload["v"])
        issued = _exact_int(payload["i"])
        expires = _exact_int(payload["e"])
        generation = _optional_exact_int(payload["g"])
        resource = _exact_string(payload["r"])
        workspace_digest = _exact_digest(payload["w"])
        connection_digest = _optional_exact_digest(payload["c"])
        asset_digest = _optional_exact_digest(payload["a"])
        filter_fingerprint = _exact_fingerprint(payload["f"])
        sort_fingerprint = _exact_fingerprint(payload["s"])
        page_values = payload["p"]
        if (
            version != _FORMAT_VERSION
            or issued < 0
            or expires - issued != self._ttl_microseconds
            or at_microseconds < issued
            or at_microseconds >= expires
            or not isinstance(page_values, list)
            or len(page_values) != 2
            or any(not isinstance(value, str) for value in page_values)
        ):
            raise _unavailable()

        expected_workspace = self._scope_digest("workspace", expected_binding.workspace_id)
        expected_connection = self._optional_scope_digest(
            "connection",
            expected_binding.connection_id,
        )
        expected_asset = self._optional_scope_digest("asset", expected_binding.asset_id)
        scope_matches = all(
            (
                hmac.compare_digest(workspace_digest, expected_workspace),
                _optional_digest_matches(connection_digest, expected_connection),
                _optional_digest_matches(asset_digest, expected_asset),
                hmac.compare_digest(resource, expected_binding.resource.value),
                generation == expected_binding.generation,
                hmac.compare_digest(
                    filter_fingerprint,
                    expected_binding.filter_fingerprint,
                ),
                hmac.compare_digest(
                    sort_fingerprint,
                    expected_binding.sort_fingerprint,
                ),
            )
        )
        if not scope_matches:
            raise _unavailable()
        try:
            return InventoryCursorPosition(
                last_key=InventoryPageKey(
                    sort_value=page_values[0],
                    stable_id=page_values[1],
                ),
                issued_at=_datetime_from_microseconds(issued),
                expires_at=_datetime_from_microseconds(expires),
            )
        except (OverflowError, TypeError, ValueError):
            raise _unavailable() from None

    def _optional_scope_digest(self, kind: str, value: object | None) -> str | None:
        if value is None:
            return None
        return self._scope_digest(kind, _identifier_text(value))

    def _scope_digest(self, kind: str, value: str) -> str:
        if not value:
            raise ValueError("inventory cursor scope is invalid")
        framed = (
            _SCOPE_CONTEXT
            + len(kind.encode("utf-8")).to_bytes(2, "big")
            + kind.encode("utf-8")
            + len(value.encode("utf-8")).to_bytes(4, "big")
            + value.encode("utf-8")
        )
        return _base64url_encode(hmac.new(self._signing_key, framed, hashlib.sha256).digest())


def _identifier_text(value: object) -> str:
    if isinstance(value, str):
        return value
    for attribute in ("value", "root"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, str):
            return candidate
    raise ValueError("inventory cursor identifier is invalid")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("non-canonical base64url")
    padded = value + ("=" * (-len(value) % 4))
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _load_canonical_payload(payload_bytes: bytes) -> Mapping[str, object]:
    payload_text = payload_bytes.decode("ascii")
    payload = json.loads(payload_text, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("inventory cursor payload is invalid")
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not hmac.compare_digest(payload_bytes, canonical):
        raise ValueError("inventory cursor payload is not canonical")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inventory cursor payload key")
        result[key] = value
    return result


def _exact_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("inventory cursor integer is invalid")
    return value


def _optional_exact_int(value: object) -> int | None:
    if value is None:
        return None
    return _exact_int(value)


def _exact_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("inventory cursor string is invalid")
    return value


def _exact_digest(value: object) -> str:
    candidate = _exact_string(value)
    if _DIGEST_PATTERN.fullmatch(candidate) is None:
        raise ValueError("inventory cursor digest is invalid")
    return candidate


def _optional_exact_digest(value: object) -> str | None:
    if value is None:
        return None
    return _exact_digest(value)


def _exact_fingerprint(value: object) -> str:
    candidate = _exact_string(value)
    if _FINGERPRINT_PATTERN.fullmatch(candidate) is None:
        raise ValueError("inventory cursor fingerprint is invalid")
    return candidate


def _optional_digest_matches(candidate: str | None, expected: str | None) -> bool:
    if candidate is None or expected is None:
        return candidate is expected
    return hmac.compare_digest(candidate, expected)


def _datetime_microseconds(value: datetime, *, error_message: str) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(error_message)
    delta = value.astimezone(UTC) - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def _timedelta_microseconds(value: timedelta) -> int:
    if not isinstance(value, timedelta):
        raise ValueError("inventory cursor TTL is invalid")
    return ((value.days * 86_400 + value.seconds) * 1_000_000) + value.microseconds


def _datetime_from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _unavailable() -> InventoryCursorError:
    return InventoryCursorError()
