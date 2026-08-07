"""Versioned, deployment-scoped pseudonymous identity derivation."""

from __future__ import annotations

import hashlib
import hmac
import re

PSEUDONYM_KEY_VERSION = "v1"
MINIMUM_PSEUDONYM_KEY_BYTES = 32
MINIMUM_PSEUDONYM_KEY_DISTINCT_BYTES = 8
_KEY_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]{0,5}$")


def derive_pseudonymous_id(
    kind: str,
    *parts: str,
    key: bytes,
    key_version: str = PSEUDONYM_KEY_VERSION,
) -> str:
    """Derive one non-reversible identifier without exposing its source values."""

    if kind not in {"actor", "workspace"}:
        raise ValueError("pseudonym kind is not supported")
    if _KEY_VERSION_PATTERN.fullmatch(key_version) is None:
        raise ValueError("pseudonym key version is not supported")
    if not isinstance(key, bytes):
        raise ValueError("pseudonymization key must be bytes")
    if len(key) < MINIMUM_PSEUDONYM_KEY_BYTES:
        raise ValueError("pseudonymization key is too short")
    if len(set(key)) < MINIMUM_PSEUDONYM_KEY_DISTINCT_BYTES:
        raise ValueError("pseudonymization key has insufficient diversity")
    if any(not part for part in parts):
        raise ValueError("pseudonym source parts must not be empty")
    framed = "|".join(f"{len(part)}:{part}" for part in parts)
    payload = f"schemabridge:{key_version}:{kind}|{framed}".encode()
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"sb_{kind}_{key_version}_{digest}"
