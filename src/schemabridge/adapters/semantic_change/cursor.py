"""Canonical HMAC cursors for tenant-scoped semantic-change pages."""

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

from schemabridge.application.ports.semantic_change_read import (
    MAX_SEMANTIC_CHANGE_CURSOR_BYTES,
    SemanticChangeCursorBinding,
    SemanticChangeCursorError,
    SemanticChangeCursorPosition,
    SemanticChangePageKey,
    SemanticChangeReadResource,
)

DEFAULT_SEMANTIC_CHANGE_CURSOR_TTL = timedelta(minutes=15)

_FORMAT_VERSION = 1
_MINIMUM_SIGNING_KEY_BYTES = 32
_MINIMUM_SIGNING_KEY_DISTINCT_BYTES = 8
_MAXIMUM_CURSOR_TTL = timedelta(minutes=15)
_SIGNATURE_CONTEXT = b"schemabridge-semantic-change-cursor-v1\x00"
_SCOPE_CONTEXT = b"schemabridge-semantic-change-cursor-scope-v1\x00"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPORT_ID_PATTERN = re.compile(r"^report_[0-9a-f]{64}$")
_NO_CONTROL = re.compile(r"^[^\x00-\x1f\x7f]+$")
_PAYLOAD_KEYS = frozenset({"e", "f", "i", "p", "q", "r", "s", "v", "w"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SignedSemanticChangeCursorCodec:
    """Sign a short-lived keyset without disclosing tenant or report identity."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        ttl: timedelta = DEFAULT_SEMANTIC_CHANGE_CURSOR_TTL,
        max_cursor_bytes: int = MAX_SEMANTIC_CHANGE_CURSOR_BYTES,
    ) -> None:
        if (
            not isinstance(signing_key, bytes)
            or len(signing_key) < _MINIMUM_SIGNING_KEY_BYTES
            or len(set(signing_key)) < _MINIMUM_SIGNING_KEY_DISTINCT_BYTES
        ):
            raise ValueError("semantic change cursor signing key is invalid")
        ttl_microseconds = _timedelta_microseconds(ttl)
        maximum_ttl = _timedelta_microseconds(_MAXIMUM_CURSOR_TTL)
        if ttl_microseconds <= 0 or ttl_microseconds > maximum_ttl:
            raise ValueError("semantic change cursor TTL is invalid")
        if not 1 <= max_cursor_bytes <= MAX_SEMANTIC_CHANGE_CURSOR_BYTES:
            raise ValueError("semantic change cursor byte limit is invalid")
        self._signing_key = signing_key
        self._ttl_microseconds = ttl_microseconds
        self._max_cursor_bytes = max_cursor_bytes

    def encode(
        self,
        *,
        binding: SemanticChangeCursorBinding,
        last_key: SemanticChangePageKey,
        issued_at: datetime,
    ) -> str:
        """Return one deterministic cursor bound to the exact public read."""

        _validate_binding(binding)
        _validate_page_key(last_key)
        issued = _datetime_microseconds(issued_at)
        if issued < 0:
            raise ValueError("semantic change cursor issue time is invalid")
        payload: dict[str, object] = {
            "e": issued + self._ttl_microseconds,
            "f": binding.filter_fingerprint,
            "i": issued,
            "p": [last_key.sort_value, last_key.stable_id],
            "q": self._optional_scope_digest("report", binding.report_id),
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
        expected_binding: SemanticChangeCursorBinding,
        at: datetime,
    ) -> SemanticChangeCursorPosition:
        """Verify canonical form, HMAC, lifetime and every binding before use."""

        try:
            _validate_binding(expected_binding)
            at_microseconds = _datetime_microseconds(at)
            payload_bytes = self._verified_payload_bytes(cursor)
            payload = _load_canonical_payload(payload_bytes)
            return self._validate_payload(
                payload,
                expected_binding=expected_binding,
                at_microseconds=at_microseconds,
            )
        except SemanticChangeCursorError:
            raise
        except (binascii.Error, UnicodeError, ValueError, TypeError, OverflowError):
            raise _unavailable() from None

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
        expected_binding: SemanticChangeCursorBinding,
        at_microseconds: int,
    ) -> SemanticChangeCursorPosition:
        if set(payload) != _PAYLOAD_KEYS:
            raise _unavailable()
        version = _exact_int(payload["v"])
        issued = _exact_int(payload["i"])
        expires = _exact_int(payload["e"])
        resource = _exact_string(payload["r"])
        workspace_digest = _exact_digest(payload["w"])
        report_digest = _optional_exact_digest(payload["q"])
        filter_fingerprint = _exact_fingerprint(payload["f"])
        sort_fingerprint = _exact_fingerprint(payload["s"])
        position = payload["p"]
        if (
            version != _FORMAT_VERSION
            or issued < 0
            or expires - issued != self._ttl_microseconds
            or at_microseconds < issued
            or at_microseconds >= expires
            or not isinstance(position, list)
            or len(position) != 2
            or any(not isinstance(value, str) for value in position)
        ):
            raise _unavailable()
        expected_workspace = self._scope_digest(
            "workspace",
            expected_binding.workspace_id,
        )
        expected_report = self._optional_scope_digest(
            "report",
            expected_binding.report_id,
        )
        if not all(
            (
                hmac.compare_digest(workspace_digest, expected_workspace),
                _optional_digest_matches(report_digest, expected_report),
                hmac.compare_digest(resource, expected_binding.resource.value),
                hmac.compare_digest(
                    filter_fingerprint,
                    expected_binding.filter_fingerprint,
                ),
                hmac.compare_digest(
                    sort_fingerprint,
                    expected_binding.sort_fingerprint,
                ),
            )
        ):
            raise _unavailable()
        page_key = SemanticChangePageKey(
            sort_value=position[0],
            stable_id=position[1],
        )
        _validate_page_key(page_key)
        return SemanticChangeCursorPosition(
            last_key=page_key,
            issued_at=_datetime_from_microseconds(issued),
            expires_at=_datetime_from_microseconds(expires),
        )

    def _optional_scope_digest(self, kind: str, value: str | None) -> str | None:
        return None if value is None else self._scope_digest(kind, value)

    def _scope_digest(self, kind: str, value: str) -> str:
        if not value:
            raise ValueError("semantic change cursor scope is invalid")
        kind_bytes = kind.encode("utf-8")
        value_bytes = value.encode("utf-8")
        framed = (
            _SCOPE_CONTEXT
            + len(kind_bytes).to_bytes(2, "big")
            + kind_bytes
            + len(value_bytes).to_bytes(4, "big")
            + value_bytes
        )
        digest = hmac.new(self._signing_key, framed, hashlib.sha256).digest()
        return _base64url_encode(digest)


def _validate_binding(binding: SemanticChangeCursorBinding) -> None:
    if not isinstance(binding.resource, SemanticChangeReadResource):
        raise ValueError("semantic change cursor binding is invalid")
    report_required = binding.resource in {
        SemanticChangeReadResource.FINDINGS,
        SemanticChangeReadResource.IMPACTS,
    }
    if (
        not binding.workspace_id
        or len(binding.workspace_id) > 200
        or _NO_CONTROL.fullmatch(binding.workspace_id) is None
        or _FINGERPRINT_PATTERN.fullmatch(binding.filter_fingerprint) is None
        or _FINGERPRINT_PATTERN.fullmatch(binding.sort_fingerprint) is None
        or report_required != (binding.report_id is not None)
        or (
            binding.report_id is not None
            and _REPORT_ID_PATTERN.fullmatch(binding.report_id) is None
        )
    ):
        raise ValueError("semantic change cursor binding is invalid")


def _validate_page_key(value: SemanticChangePageKey) -> None:
    if (
        not value.sort_value
        or not value.stable_id
        or len(value.sort_value) > 500
        or len(value.stable_id) > 500
        or _NO_CONTROL.fullmatch(value.sort_value) is None
        or _NO_CONTROL.fullmatch(value.stable_id) is None
    ):
        raise ValueError("semantic change cursor page key is invalid")


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
    payload = json.loads(payload_bytes.decode("ascii"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("semantic change cursor payload is invalid")
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not hmac.compare_digest(payload_bytes, canonical):
        raise ValueError("semantic change cursor payload is not canonical")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate semantic change cursor payload key")
        result[key] = value
    return result


def _exact_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("semantic change cursor integer is invalid")
    return value


def _exact_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("semantic change cursor string is invalid")
    return value


def _exact_digest(value: object) -> str:
    candidate = _exact_string(value)
    if _DIGEST_PATTERN.fullmatch(candidate) is None:
        raise ValueError("semantic change cursor digest is invalid")
    return candidate


def _optional_exact_digest(value: object) -> str | None:
    return None if value is None else _exact_digest(value)


def _exact_fingerprint(value: object) -> str:
    candidate = _exact_string(value)
    if _FINGERPRINT_PATTERN.fullmatch(candidate) is None:
        raise ValueError("semantic change cursor fingerprint is invalid")
    return candidate


def _optional_digest_matches(candidate: str | None, expected: str | None) -> bool:
    if candidate is None or expected is None:
        return candidate is expected
    return hmac.compare_digest(candidate, expected)


def _datetime_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("semantic change cursor time must include a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def _datetime_from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _timedelta_microseconds(value: timedelta) -> int:
    if not isinstance(value, timedelta):
        raise ValueError("semantic change cursor TTL is invalid")
    return ((value.days * 86_400 + value.seconds) * 1_000_000) + value.microseconds


def _unavailable() -> SemanticChangeCursorError:
    return SemanticChangeCursorError()


__all__ = [
    "DEFAULT_SEMANTIC_CHANGE_CURSOR_TTL",
    "SignedSemanticChangeCursorCodec",
]
