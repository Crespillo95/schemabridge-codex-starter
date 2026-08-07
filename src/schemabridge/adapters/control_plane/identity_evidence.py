"""Owner-only signed envelopes for opaque dual-key identity evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from schemabridge.application.ports.identity_evidence import (
    IdentityEvidenceError,
    IdentityEvidenceErrorCode,
)
from schemabridge.domain.identity_rotation import VerifiedDualKeyOidcDerivation

_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_MAX_EVIDENCE_LIFETIME = timedelta(minutes=15)
_SIGNATURE_CONTEXT = b"schemabridge-identity-evidence-v1:"


class SignedIdentityEvidenceEnvelope(BaseModel):
    """Signed transient evidence containing no raw claims or direct identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1] = 1
    workspace_id: str = Field(pattern=r"^sb_workspace_v[1-9][0-9]{0,5}_[0-9a-f]{64}$")
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...] = Field(
        min_length=1,
        max_length=10_000,
    )
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_key_version: str = Field(pattern=r"^v[1-9][0-9]{0,5}$")
    payload_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evidence_is_bounded_and_self_identifying(self) -> SignedIdentityEvidenceEnvelope:
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > _MAX_EVIDENCE_LIFETIME
        ):
            raise ValueError("identity evidence validity window is invalid")
        if any(item.previous.workspace_id != self.workspace_id for item in self.derivations):
            raise ValueError("identity evidence crosses its declared workspace")
        if self.payload_fingerprint != identity_evidence_fingerprint(
            format_version=self.format_version,
            workspace_id=self.workspace_id,
            derivations=self.derivations,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            nonce=self.nonce,
            signature_key_version=self.signature_key_version,
        ):
            raise ValueError("identity evidence fingerprint does not match")
        return self


def build_signed_identity_evidence(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    *,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    signature_key: bytes,
    signature_key_version: str,
) -> SignedIdentityEvidenceEnvelope:
    """Build an envelope only after an external boundary verified the OIDC session."""

    if not derivations:
        raise ValueError("identity evidence requires at least one derivation")
    if len(signature_key) < 32 or len(set(signature_key)) < 8:
        raise ValueError("identity evidence signing key is invalid")
    workspace_id = derivations[0].previous.workspace_id
    fingerprint = identity_evidence_fingerprint(
        format_version=1,
        workspace_id=workspace_id,
        derivations=derivations,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        signature_key_version=signature_key_version,
    )
    signature = hmac.new(
        signature_key,
        _SIGNATURE_CONTEXT + fingerprint.encode(),
        hashlib.sha256,
    ).hexdigest()
    return SignedIdentityEvidenceEnvelope(
        workspace_id=workspace_id,
        derivations=derivations,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        signature_key_version=signature_key_version,
        payload_fingerprint=fingerprint,
        signature=signature,
    )


def identity_evidence_fingerprint(
    *,
    format_version: int,
    workspace_id: str,
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    signature_key_version: str,
) -> str:
    payload = {
        "format_version": format_version,
        "workspace_id": workspace_id,
        "derivations": [item.model_dump(mode="json", warnings=False) for item in derivations],
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "nonce": nonce,
        "signature_key_version": signature_key_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class IdentityEvidenceFileReader:
    """Read one immutable owner-only envelope and verify its exact signature."""

    def __init__(
        self,
        path: Path,
        *,
        signing_keys: dict[str, bytes],
        clock: Callable[[], datetime],
        max_bytes: int = _MAX_EVIDENCE_BYTES,
    ) -> None:
        if (
            not signing_keys
            or any(
                not version or len(key) < 32 or len(set(key)) < 8
                for version, key in signing_keys.items()
            )
            or not 1 <= max_bytes <= _MAX_EVIDENCE_BYTES
        ):
            raise ValueError("identity evidence reader configuration is invalid")
        self._path = path
        self._signing_keys = dict(signing_keys)
        self._clock = clock
        self._max_bytes = max_bytes

    def read(
        self,
        *,
        expected_fingerprint: str | None = None,
    ) -> SignedIdentityEvidenceEnvelope:
        before = self._read_owner_only_file()
        try:
            payload = json.loads(before, object_pairs_hook=_unique_object)
            envelope = SignedIdentityEvidenceEnvelope.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.PAYLOAD_INVALID,
                "identity evidence payload is invalid",
            ) from error
        key = self._signing_keys.get(envelope.signature_key_version)
        expected_signature = (
            None
            if key is None
            else hmac.new(
                key,
                _SIGNATURE_CONTEXT + envelope.payload_fingerprint.encode(),
                hashlib.sha256,
            ).hexdigest()
        )
        if expected_signature is None or not hmac.compare_digest(
            expected_signature,
            envelope.signature,
        ):
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.SIGNATURE_INVALID,
                "identity evidence signature is invalid",
            )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("identity evidence clock must include a timezone")
        if now < envelope.issued_at or now >= envelope.expires_at:
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.EXPIRED,
                "identity evidence is outside its validity window",
            )
        if expected_fingerprint is not None and not hmac.compare_digest(
            expected_fingerprint,
            envelope.payload_fingerprint,
        ):
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.FINGERPRINT_MISMATCH,
                "identity evidence does not match the reviewed fingerprint",
            )
        if self._read_owner_only_file() != before:
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.FILE_UNSAFE,
                "identity evidence changed during verification",
            )
        return envelope

    def _read_owner_only_file(self) -> str:
        try:
            if self._path.is_symlink():
                raise IdentityEvidenceError(
                    IdentityEvidenceErrorCode.FILE_UNSAFE,
                    "identity evidence file is unsafe",
                )
            path = self._path.resolve(strict=True)
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or not 1 <= metadata.st_size <= self._max_bytes
            ):
                raise IdentityEvidenceError(
                    IdentityEvidenceErrorCode.FILE_UNSAFE,
                    "identity evidence file is unsafe",
                )
            return path.read_text(encoding="utf-8")
        except IdentityEvidenceError:
            raise
        except UnicodeDecodeError as error:
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.PAYLOAD_INVALID,
                "identity evidence payload is invalid",
            ) from error
        except OSError as error:
            raise IdentityEvidenceError(
                IdentityEvidenceErrorCode.FILE_UNAVAILABLE,
                "identity evidence file is unavailable",
            ) from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("identity evidence JSON repeats a key")
        result[key] = value
    return result
