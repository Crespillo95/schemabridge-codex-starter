"""Sanitized boundary failures for transient signed identity evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.identity_rotation import VerifiedDualKeyOidcDerivation


class IdentityEvidenceErrorCode(StrEnum):
    """Stable failures that do not reveal evidence or filesystem details."""

    FILE_UNSAFE = "identity_evidence_file_unsafe"
    FILE_UNAVAILABLE = "identity_evidence_file_unavailable"
    PAYLOAD_INVALID = "identity_evidence_payload_invalid"
    SIGNATURE_INVALID = "identity_evidence_signature_invalid"
    EXPIRED = "identity_evidence_expired"
    FINGERPRINT_MISMATCH = "identity_evidence_fingerprint_mismatch"


class IdentityEvidenceError(RuntimeError):
    """Sanitized evidence failure safe for an application/entrypoint boundary."""

    def __init__(self, code: IdentityEvidenceErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class IdentityEvidenceEnvelopePort(Protocol):
    """Bounded verified evidence metadata consumed outside its file adapter."""

    @property
    def workspace_id(self) -> str: ...

    @property
    def payload_fingerprint(self) -> str: ...

    @property
    def signature_key_version(self) -> str: ...

    @property
    def derivations(self) -> tuple[VerifiedDualKeyOidcDerivation, ...]: ...

    @property
    def issued_at(self) -> datetime: ...

    @property
    def expires_at(self) -> datetime: ...
