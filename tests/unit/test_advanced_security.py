from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.adapters.query_studio.advanced_security import (
    HmacAdvancedQueryPreviewTokens,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
    AdvancedQueryStudioPortErrorCode,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedPreviewTokenClaims,
    SignedAdvancedQueryPreviewToken,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
KEY = bytes(range(32))


def _claims() -> AdvancedPreviewTokenClaims:
    return AdvancedPreviewTokenClaims(
        request_digest="1" * 64,
        mention_fingerprint="2" * 64,
        semantic_context_fingerprint="3" * 64,
        approved_context_fingerprint="4" * 64,
        governed_registry_fingerprint="5" * 64,
        scope_fingerprint="6" * 64,
        interpretation_fingerprint="7" * 64,
        resolved_plan_fingerprint="a" * 64,
        routed_request_fingerprint="8" * 64,
        preview_fingerprint="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        nonce="nonce_ABCDEFGHIJKLMNOP",
    )


def test_qsp3_round_trip_contains_only_bounded_public_claims() -> None:
    codec = HmacAdvancedQueryPreviewTokens(KEY)

    token = codec.issue(_claims())

    assert token.root.startswith("qsp3.")
    assert codec.verify(token, at=NOW + timedelta(minutes=5)) == _claims()
    encoded = token.root.removeprefix("qsp3.")
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    payload = decoded[:-32]
    assert b"request_digest" in payload
    assert b"preview_fingerprint" in payload
    assert b"Para cada mes" not in payload
    assert b"sales.order_lines" not in payload
    assert b"workspace_id" not in payload
    assert b"sql" not in payload.lower()


def test_qsp3_authenticates_a_complete_target_binding_and_rejects_partial_claims() -> None:
    payload = _claims().model_dump(mode="python")
    with pytest.raises(ValidationError, match="target binding must be complete"):
        AdvancedPreviewTokenClaims.model_validate(
            {
                **payload,
                "connection_id": CatalogConnectionId("warehouse-primary"),
            }
        )
    claims = AdvancedPreviewTokenClaims.model_validate(
        {
            **payload,
            "connection_id": CatalogConnectionId("warehouse-primary"),
            "target_route_revision": 3,
            "target_fingerprint": "b" * 64,
            "target_type_contract_fingerprint": "c" * 64,
        }
    )
    codec = HmacAdvancedQueryPreviewTokens(KEY)

    assert codec.verify(codec.issue(claims), at=NOW) == claims


def test_qsp3_tampering_future_issue_time_and_expiry_fail_closed() -> None:
    codec = HmacAdvancedQueryPreviewTokens(KEY)
    token = codec.issue(_claims())
    position = len("qsp3.") + 8
    replacement = "A" if token.root[position] != "A" else "B"
    tampered = SignedAdvancedQueryPreviewToken(
        token.root[:position] + replacement + token.root[position + 1 :]
    )

    with pytest.raises(AdvancedQueryStudioPortError) as changed:
        codec.verify(tampered, at=NOW)
    assert changed.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_INVALID

    with pytest.raises(AdvancedQueryStudioPortError) as future:
        codec.verify(token, at=NOW - timedelta(microseconds=1))
    assert future.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_INVALID

    with pytest.raises(AdvancedQueryStudioPortError) as expired:
        codec.verify(token, at=NOW + timedelta(minutes=10, microseconds=1))
    assert expired.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_EXPIRED


def test_qsp3_rejects_a_noncanonical_base64url_spelling_of_the_same_bytes() -> None:
    codec = HmacAdvancedQueryPreviewTokens(KEY)
    claims = _claims().model_copy(update={"nonce": "nonce_ABCDEFGHIJKLMNOPxx"})
    token = codec.issue(claims)
    encoded = token.root.removeprefix("qsp3.")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(encoded) % 4 == 2
    replacement = alphabet[alphabet.index(encoded[-1]) + 1]
    noncanonical_encoded = encoded[:-1] + replacement
    padding = "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(noncanonical_encoded + padding) == (
        base64.urlsafe_b64decode(encoded + padding)
    )
    noncanonical = SignedAdvancedQueryPreviewToken(f"qsp3.{noncanonical_encoded}")

    with pytest.raises(AdvancedQueryStudioPortError) as captured:
        codec.verify(noncanonical, at=NOW)

    assert captured.value.code is AdvancedQueryStudioPortErrorCode.TOKEN_INVALID


@pytest.mark.parametrize("key", (b"", b"a" * 32, bytes(range(31))))
def test_qsp3_signing_key_strength_is_enforced(key: bytes) -> None:
    with pytest.raises(ValueError, match="32 bytes and 8 distinct"):
        HmacAdvancedQueryPreviewTokens(key)
