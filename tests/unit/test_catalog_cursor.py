from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from schemabridge.adapters.catalog.cursor import (
    DEFAULT_INVENTORY_CURSOR_TTL,
    SignedInventoryCursorCodec,
)
from schemabridge.application.ports.catalog_inventory import (
    InventoryCursorError,
    InventoryCursorErrorCode,
    InventoryCursorPort,
)
from schemabridge.domain.catalog_inventory import (
    MAX_INVENTORY_CURSOR_BYTES,
    CatalogAssetId,
    CatalogConnectionId,
    InventoryCursorBinding,
    InventoryCursorResource,
    InventoryPageKey,
)

NOW = datetime(2026, 7, 23, 12, 30, 45, 123456, tzinfo=UTC)
KEY = b"m25-dedicated-cursor-signing-key-0123456789"
OTHER_KEY = b"m25-other-dedicated-cursor-key-9876543210"
SIGNATURE_CONTEXT = b"schemabridge-inventory-cursor-v1\x00"
FILTER = hashlib.sha256(b"normalized-filter").hexdigest()
SORT = hashlib.sha256(b"name-then-stable-id-v1").hexdigest()
CONNECTION = CatalogConnectionId("warehouse-primary")
ASSET = CatalogAssetId("urn:li:dataset:(urn:li:dataPlatform:postgres,crm.customers,PROD)")
PAGE_KEY = InventoryPageKey(sort_value="crm.customers", stable_id="asset-000050")


def _binding(
    *,
    workspace_id: str = "tenant-alpha",
    resource: InventoryCursorResource = InventoryCursorResource.FIELDS,
    filter_fingerprint: str = FILTER,
    sort_fingerprint: str = SORT,
    connection_id: CatalogConnectionId | None = CONNECTION,
    asset_id: CatalogAssetId | None = ASSET,
    generation: int | None = 7,
) -> InventoryCursorBinding:
    return InventoryCursorBinding(
        workspace_id=workspace_id,
        resource=resource,
        filter_fingerprint=filter_fingerprint,
        sort_fingerprint=sort_fingerprint,
        connection_id=connection_id,
        asset_id=asset_id,
        generation=generation,
    )


def _asset_binding(
    *,
    workspace_id: str = "tenant-alpha",
    connection_id: CatalogConnectionId = CONNECTION,
    generation: int = 7,
    filter_fingerprint: str = FILTER,
    sort_fingerprint: str = SORT,
) -> InventoryCursorBinding:
    return _binding(
        workspace_id=workspace_id,
        resource=InventoryCursorResource.ASSETS,
        filter_fingerprint=filter_fingerprint,
        sort_fingerprint=sort_fingerprint,
        connection_id=connection_id,
        asset_id=None,
        generation=generation,
    )


def _connection_binding(
    *,
    workspace_id: str = "tenant-alpha",
    filter_fingerprint: str = FILTER,
    sort_fingerprint: str = SORT,
) -> InventoryCursorBinding:
    return _binding(
        workspace_id=workspace_id,
        resource=InventoryCursorResource.CONNECTIONS,
        filter_fingerprint=filter_fingerprint,
        sort_fingerprint=sort_fingerprint,
        connection_id=None,
        asset_id=None,
        generation=None,
    )


def _decode_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + ("=" * (-len(segment) % 4)))


def _payload(cursor: str) -> dict[str, Any]:
    segment = cursor.split(".", maxsplit=1)[0]
    loaded = json.loads(_decode_segment(segment))
    assert isinstance(loaded, dict)
    return loaded


def _signed_raw(payload: bytes, *, key: bytes = KEY) -> str:
    signature = hmac.new(key, SIGNATURE_CONTEXT + payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )


def _signed_payload(payload: dict[str, Any], *, key: bytes = KEY) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _signed_raw(encoded, key=key)


def _assert_unavailable(
    codec: SignedInventoryCursorCodec,
    cursor: str,
    *,
    binding: InventoryCursorBinding | None = None,
    at: datetime = NOW,
) -> None:
    with pytest.raises(InventoryCursorError) as raised:
        codec.decode(
            cursor=cursor,
            expected_binding=binding or _binding(),
            at=at,
        )
    assert raised.value.code is InventoryCursorErrorCode.UNAVAILABLE
    assert str(raised.value) == "inventory cursor is unavailable"
    rendered = f"{raised.value!s} {raised.value!r}"
    for protected in (
        "tenant-alpha",
        "warehouse-primary",
        ASSET.root,
        PAGE_KEY.sort_value,
        PAGE_KEY.stable_id,
    ):
        assert protected not in rendered


def test_cursor_round_trips_canonically_through_the_application_port() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    port: InventoryCursorPort = codec

    cursor = port.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)

    position = port.decode(cursor=cursor, expected_binding=_binding(), at=NOW)
    assert position.last_key == PAGE_KEY
    assert position.issued_at == NOW
    assert position.expires_at == NOW + DEFAULT_INVENTORY_CURSOR_TTL
    assert cursor == port.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    assert len(cursor.encode("ascii")) <= MAX_INVENTORY_CURSOR_BYTES
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", cursor)
    assert "=" not in cursor
    payload_segment, signature_segment = cursor.split(".")
    decoded_payload = _decode_segment(payload_segment)
    assert base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode("ascii") == payload_segment
    assert len(_decode_segment(signature_segment)) == hashlib.sha256().digest_size
    assert decoded_payload == json.dumps(
        json.loads(decoded_payload),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@pytest.mark.parametrize(
    "binding",
    (
        _connection_binding(),
        _asset_binding(),
        _binding(),
    ),
)
def test_every_resource_shape_round_trips(
    binding: InventoryCursorBinding,
) -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=binding, last_key=PAGE_KEY, issued_at=NOW)

    assert (
        codec.decode(
            cursor=cursor,
            expected_binding=binding,
            at=NOW,
        ).last_key
        == PAGE_KEY
    )


def test_scope_identifiers_are_keyed_digests_not_decodable_cursor_content() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    rendered_payload = _decode_segment(cursor.split(".", maxsplit=1)[0]).decode("ascii")

    assert "tenant-alpha" not in rendered_payload
    assert CONNECTION.root not in rendered_payload
    assert ASSET.root not in rendered_payload
    assert _payload(cursor)["w"] != hashlib.sha256(b"tenant-alpha").hexdigest()


def test_cursor_preserves_unicode_keyset_values_without_affecting_url_safety() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    page_key = InventoryPageKey(sort_value="clientes.ámbito", stable_id="activo-ñ-0001")

    cursor = codec.encode(binding=_binding(), last_key=page_key, issued_at=NOW)

    assert (
        codec.decode(
            cursor=cursor,
            expected_binding=_binding(),
            at=NOW,
        ).last_key
        == page_key
    )
    assert cursor.isascii()


def test_cursor_is_valid_until_but_not_at_its_exact_expiry() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)

    assert (
        codec.decode(
            cursor=cursor,
            expected_binding=_binding(),
            at=NOW + DEFAULT_INVENTORY_CURSOR_TTL - timedelta(microseconds=1),
        ).last_key
        == PAGE_KEY
    )
    _assert_unavailable(
        codec,
        cursor,
        at=NOW + DEFAULT_INVENTORY_CURSOR_TTL,
    )
    _assert_unavailable(codec, cursor, at=NOW - timedelta(microseconds=1))


def test_generation_deadline_must_cover_the_complete_cursor_lifetime() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    exact_deadline = NOW + DEFAULT_INVENTORY_CURSOR_TTL

    cursor = codec.encode(
        binding=_binding(),
        last_key=PAGE_KEY,
        issued_at=NOW,
        not_after=exact_deadline,
    )

    assert (
        codec.decode(
            cursor=cursor,
            expected_binding=_binding(),
            at=NOW,
        ).expires_at
        == exact_deadline
    )
    with pytest.raises(InventoryCursorError) as too_short:
        codec.encode(
            binding=_binding(),
            last_key=PAGE_KEY,
            issued_at=NOW,
            not_after=exact_deadline - timedelta(microseconds=1),
        )
    assert too_short.value.code is InventoryCursorErrorCode.UNAVAILABLE


def test_configurable_shorter_ttl_and_equivalent_timezones_are_exact() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY, ttl=timedelta(seconds=1))
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    equivalent = NOW.astimezone(timezone(timedelta(hours=2)))

    assert (
        codec.decode(
            cursor=cursor,
            expected_binding=_binding(),
            at=equivalent,
        ).last_key
        == PAGE_KEY
    )
    _assert_unavailable(codec, cursor, at=NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    "expected",
    (
        _binding(workspace_id="tenant-bravo"),
        _binding(connection_id=CatalogConnectionId("warehouse-secondary")),
        _binding(
            asset_id=CatalogAssetId(
                "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.accounts,PROD)"
            )
        ),
        _binding(generation=8),
        _binding(filter_fingerprint=hashlib.sha256(b"another-filter").hexdigest()),
        _binding(sort_fingerprint=hashlib.sha256(b"another-sort").hexdigest()),
        _asset_binding(),
    ),
    ids=(
        "workspace",
        "connection",
        "asset",
        "generation",
        "filter",
        "sort",
        "resource",
    ),
)
def test_cursor_is_bound_to_the_complete_expected_scope(
    expected: InventoryCursorBinding,
) -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)

    _assert_unavailable(codec, cursor, binding=expected)


def test_key_material_is_dedicated_to_one_codec_instance() -> None:
    issuing = SignedInventoryCursorCodec(signing_key=KEY)
    verifying = SignedInventoryCursorCodec(signing_key=OTHER_KEY)
    cursor = issuing.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)

    _assert_unavailable(verifying, cursor)


@pytest.mark.parametrize("part", ("payload", "signature"))
def test_unsigned_tampering_has_one_sanitized_failure(part: str) -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    payload, signature = cursor.split(".")
    if part == "payload":
        payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    else:
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]

    _assert_unavailable(codec, f"{payload}.{signature}")


@pytest.mark.parametrize(
    "cursor",
    (
        "",
        " ",
        "not-a-token",
        "a.b.c",
        "é.abc",
        "a=.abc",
        "abc.def=",
        "a." + ("a" * MAX_INVENTORY_CURSOR_BYTES),
    ),
)
def test_malformed_or_oversized_tokens_share_the_same_boundary(cursor: str) -> None:
    _assert_unavailable(SignedInventoryCursorCodec(signing_key=KEY), cursor)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("v", 2),
        ("v", True),
        ("i", "0"),
        ("e", 0),
        ("g", True),
        ("g", 0),
        ("r", "unknown"),
        ("w", "0" * 43),
        ("c", 123),
        ("a", []),
        ("f", "f" * 63),
        ("s", "F" * 64),
        ("p", ["only-one"]),
        ("p", ["valid", 1]),
    ),
)
def test_validly_signed_but_invalid_payloads_fail_closed(
    field: str,
    value: object,
) -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    original = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    payload = _payload(original)
    payload[field] = value

    _assert_unavailable(codec, _signed_payload(payload))


def test_signed_payload_cannot_extend_the_configured_lifetime() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    original = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    payload = _payload(original)
    payload["e"] = int(payload["e"]) + 1

    _assert_unavailable(codec, _signed_payload(payload))


def test_extra_duplicate_and_noncanonical_json_members_are_rejected() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    original = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    payload = _payload(original)

    with_extra = dict(payload)
    with_extra["sql"] = "SELECT secret"
    _assert_unavailable(codec, _signed_payload(with_extra))

    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    duplicate = ("{" + f'"v":1,{canonical[1:]}').encode("ascii")
    _assert_unavailable(codec, _signed_raw(duplicate))

    noncanonical = json.dumps(payload, indent=1, sort_keys=False).encode("ascii")
    _assert_unavailable(codec, _signed_raw(noncanonical))


def test_noncanonical_base64url_padding_is_rejected_even_when_bytes_match() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    payload, signature = cursor.split(".")

    _assert_unavailable(codec, f"{payload}=.{signature}")
    _assert_unavailable(codec, f"{payload}.{signature}=")


def test_an_oversized_encoded_key_never_produces_an_oversized_cursor() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    oversized_key = InventoryPageKey(sort_value="x" * 500, stable_id="y" * 500)

    with pytest.raises(InventoryCursorError) as raised:
        codec.encode(binding=_binding(), last_key=oversized_key, issued_at=NOW)
    assert raised.value.code is InventoryCursorErrorCode.UNAVAILABLE
    assert str(raised.value) == "inventory cursor is unavailable"


@pytest.mark.parametrize(
    "key",
    (
        b"too-short",
        b"x" * 32,
        bytearray(b"m25-dedicated-cursor-signing-key-0123456789"),
    ),
)
def test_weak_or_non_bytes_signing_keys_are_rejected(key: object) -> None:
    with pytest.raises(ValueError, match="signing key is invalid"):
        SignedInventoryCursorCodec(signing_key=key)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "ttl",
    (
        timedelta(0),
        timedelta(microseconds=-1),
        timedelta(minutes=15, microseconds=1),
        "15 minutes",
    ),
)
def test_invalid_ttl_configuration_is_rejected(ttl: object) -> None:
    with pytest.raises(ValueError, match="TTL is invalid"):
        SignedInventoryCursorCodec(signing_key=KEY, ttl=ttl)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", (0, 1_025))
def test_cursor_limit_cannot_exceed_or_undercut_the_safe_envelope(limit: int) -> None:
    with pytest.raises(ValueError, match="byte limit is invalid"):
        SignedInventoryCursorCodec(signing_key=KEY, max_cursor_bytes=limit)


def test_internal_times_must_be_timezone_aware_without_echoing_scope() -> None:
    codec = SignedInventoryCursorCodec(signing_key=KEY)
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="issue time must include a timezone"):
        codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=naive)
    with pytest.raises(ValueError, match="generation deadline must include a timezone"):
        codec.encode(
            binding=_binding(),
            last_key=PAGE_KEY,
            issued_at=NOW,
            not_after=naive,
        )

    cursor = codec.encode(binding=_binding(), last_key=PAGE_KEY, issued_at=NOW)
    with pytest.raises(ValueError, match="verification time must include a timezone"):
        codec.decode(cursor=cursor, expected_binding=_binding(), at=naive)
