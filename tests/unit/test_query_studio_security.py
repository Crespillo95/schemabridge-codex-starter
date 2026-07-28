from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.query_studio.security import (
    HmacQueryStudioPreviewTokens,
    SecureQueryStudioNonce,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.query_studio import (
    PreviewTokenPayload,
    SignedPreviewToken,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
KEY = bytes(range(32))


def _payload() -> PreviewTokenPayload:
    return PreviewTokenPayload(
        request_digest="1" * 64,
        shortlist_fingerprint="2" * 64,
        proposal_fingerprint="3" * 64,
        configuration_fingerprint="4" * 64,
        scope_digest="5" * 64,
        preview_fingerprint="6" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        nonce="nonce_ABCDEFGHIJKLMNOP",
    )


def test_preview_token_round_trip_contains_digests_but_no_text_or_candidates() -> None:
    codec = HmacQueryStudioPreviewTokens(KEY)
    token = codec.issue(_payload())

    assert codec.verify(token, at=NOW + timedelta(minutes=5)) == _payload()
    encoded = token.root.removeprefix("qsp1.")
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    claims = decoded[:-32]
    assert b"request_digest" in claims
    assert b"business" not in claims
    assert b"definition" not in claims
    assert b"candidate" not in claims
    assert b"workspace" not in claims
    assert b"actor" not in claims


def test_preview_token_tamper_future_and_expiry_fail_closed() -> None:
    codec = HmacQueryStudioPreviewTokens(KEY)
    token = codec.issue(_payload())
    replacement = "A" if token.root[-1] != "A" else "B"
    tampered = SignedPreviewToken(token.root[:-1] + replacement)

    with pytest.raises(QueryStudioPortError) as changed:
        codec.verify(tampered, at=NOW)
    assert changed.value.code is QueryStudioPortErrorCode.TOKEN_INVALID

    with pytest.raises(QueryStudioPortError) as future:
        codec.verify(token, at=NOW - timedelta(microseconds=1))
    assert future.value.code is QueryStudioPortErrorCode.TOKEN_INVALID

    with pytest.raises(QueryStudioPortError) as expired:
        codec.verify(token, at=NOW + timedelta(minutes=10, microseconds=1))
    assert expired.value.code is QueryStudioPortErrorCode.TOKEN_EXPIRED


@pytest.mark.parametrize("key", (b"", b"a" * 32, bytes(range(31))))
def test_query_studio_signing_key_strength_is_enforced(key: bytes) -> None:
    with pytest.raises(ValueError, match="32 bytes and 8 distinct"):
        HmacQueryStudioPreviewTokens(key)


def test_secure_nonce_is_fresh_ascii_and_bounded() -> None:
    source = SecureQueryStudioNonce()
    values = {source.new_nonce() for _ in range(100)}

    assert len(values) == 100
    assert all(value.isascii() and 16 <= len(value) <= 120 for value in values)
