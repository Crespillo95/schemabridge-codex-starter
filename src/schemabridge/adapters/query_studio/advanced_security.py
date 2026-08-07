"""HMAC-authenticated qsp3 tokens for the target-bound natural SQL flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime

from pydantic import ValidationError

from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedPreviewTokenClaims,
    SignedAdvancedQueryPreviewToken,
)

_MINIMUM_KEY_BYTES = 32
_MINIMUM_DISTINCT_KEY_BYTES = 8
_SIGNATURE_BYTES = hashlib.sha256().digest_size
_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class HmacAdvancedQueryPreviewTokens:
    """Authenticate one bounded digest-only M32 preview payload."""

    def __init__(self, signing_key: bytes) -> None:
        self._signing_key = _validated_key(signing_key)

    def issue(
        self,
        payload: AdvancedPreviewTokenClaims,
    ) -> SignedAdvancedQueryPreviewToken:
        encoded_payload = _canonical_json(payload.model_dump(mode="json"))
        signature = hmac.new(
            self._signing_key,
            encoded_payload,
            hashlib.sha256,
        ).digest()
        return SignedAdvancedQueryPreviewToken(f"qsp3.{_b64encode(encoded_payload + signature)}")

    def verify(
        self,
        token: SignedAdvancedQueryPreviewToken,
        *,
        at: datetime,
    ) -> AdvancedPreviewTokenClaims:
        if at.tzinfo is None or at.utcoffset() is None:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID)
        try:
            encoded = _b64decode(token.root.removeprefix("qsp3."))
        except (ValueError, UnicodeError) as error:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID) from error
        if len(encoded) <= _SIGNATURE_BYTES:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID)
        payload_bytes = encoded[:-_SIGNATURE_BYTES]
        supplied_signature = encoded[-_SIGNATURE_BYTES:]
        expected_signature = hmac.new(
            self._signing_key,
            payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID)
        try:
            raw = json.loads(payload_bytes)
            if not isinstance(raw, dict):
                raise TypeError("advanced preview claims are not an object")
            payload = AdvancedPreviewTokenClaims.model_validate(raw)
        except (
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID) from error
        if at < payload.issued_at:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_INVALID)
        if at > payload.expires_at:
            raise _token_error(AdvancedQueryStudioPortErrorCode.TOKEN_EXPIRED)
        return payload


def _validated_key(value: bytes) -> bytes:
    if (
        not isinstance(value, bytes)
        or len(value) < _MINIMUM_KEY_BYTES
        or len(set(value)) < _MINIMUM_DISTINCT_KEY_BYTES
    ):
        raise ValueError(
            "advanced Query Studio signing key must contain at least 32 bytes and 8 distinct values"
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
    decoded = base64.urlsafe_b64decode(value + padding)
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


def _token_error(
    code: AdvancedQueryStudioPortErrorCode,
) -> AdvancedQueryStudioPortError:
    return AdvancedQueryStudioPortError(
        code,
        "the advanced Query Studio preview token is unavailable",
    )


__all__ = ["HmacAdvancedQueryPreviewTokens"]
