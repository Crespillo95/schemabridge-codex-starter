"""Opaque HMAC identities and text-free preview tokens for Query Studio."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime

from pydantic import ValidationError

from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.query_studio import (
    GovernedFieldBinding,
    OpaqueCandidateId,
    PreviewTokenPayload,
    QueryStudioScopeSnapshot,
    SignedPreviewToken,
)

_MINIMUM_KEY_BYTES = 32
_MINIMUM_DISTINCT_KEY_BYTES = 8
_SIGNATURE_BYTES = hashlib.sha256().digest_size


class SystemQueryStudioClock:
    """Timezone-aware system clock kept outside the pure domain."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SecureQueryStudioNonce:
    """Generate a fresh opaque nonce for each preview."""

    def new_nonce(self) -> str:
        return secrets.token_urlsafe(24)


class HmacQueryStudioCandidateIds:
    """Issue request-scoped opaque IDs without encoding locator material."""

    def __init__(self, signing_key: bytes) -> None:
        self._signing_key = _validated_key(signing_key)

    def issue(
        self,
        *,
        scope: QueryStudioScopeSnapshot,
        binding: GovernedFieldBinding,
        request_digest: str,
        nonce: str,
    ) -> OpaqueCandidateId:
        payload = _canonical_json(
            {
                "version": 1,
                "scope_fingerprint": scope.fingerprint,
                "binding_fingerprint": binding.binding_fingerprint,
                "request_digest": request_digest,
                "nonce": nonce,
            }
        )
        digest = hmac.new(
            self._signing_key,
            payload,
            hashlib.sha256,
        ).digest()
        return OpaqueCandidateId(f"qsc1_{_b64encode(digest)}")


class HmacQueryStudioPreviewTokens:
    """Authenticate one bounded digest-only preview payload."""

    def __init__(self, signing_key: bytes) -> None:
        self._signing_key = _validated_key(signing_key)

    def issue(self, payload: PreviewTokenPayload) -> SignedPreviewToken:
        encoded_payload = _canonical_json(payload.model_dump(mode="json"))
        signature = hmac.new(
            self._signing_key,
            encoded_payload,
            hashlib.sha256,
        ).digest()
        return SignedPreviewToken(f"qsp1.{_b64encode(encoded_payload + signature)}")

    def verify(
        self,
        token: SignedPreviewToken,
        *,
        at: datetime,
    ) -> PreviewTokenPayload:
        if at.tzinfo is None or at.utcoffset() is None:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID)
        try:
            encoded = _b64decode(token.root.removeprefix("qsp1."))
        except (ValueError, UnicodeError) as error:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID) from error
        if len(encoded) <= _SIGNATURE_BYTES:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID)
        payload_bytes = encoded[:-_SIGNATURE_BYTES]
        supplied_signature = encoded[-_SIGNATURE_BYTES:]
        expected_signature = hmac.new(
            self._signing_key,
            payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID)
        try:
            raw = json.loads(payload_bytes)
            if not isinstance(raw, dict):
                raise TypeError("preview claims are not an object")
            payload = PreviewTokenPayload.model_validate(raw)
        except (
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID) from error
        if at < payload.issued_at:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_INVALID)
        if at > payload.expires_at:
            raise _token_error(QueryStudioPortErrorCode.TOKEN_EXPIRED)
        return payload


def _validated_key(value: bytes) -> bytes:
    if (
        not isinstance(value, bytes)
        or len(value) < _MINIMUM_KEY_BYTES
        or len(set(value)) < _MINIMUM_DISTINCT_KEY_BYTES
    ):
        raise ValueError(
            "Query Studio signing key must contain at least 32 bytes and 8 distinct values"
        )
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(character not in _B64URL for character in value):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _token_error(code: QueryStudioPortErrorCode) -> QueryStudioPortError:
    return QueryStudioPortError(
        code,
        "the Query Studio preview token is unavailable",
    )


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


__all__ = [
    "HmacQueryStudioCandidateIds",
    "HmacQueryStudioPreviewTokens",
    "SecureQueryStudioNonce",
    "SystemQueryStudioClock",
]
