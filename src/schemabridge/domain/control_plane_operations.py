"""Typed operator evidence for control-plane backup and restore."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


class ControlPlaneBackupManifest(FrozenDomainModel):
    """Signed description of one transaction-consistent PostgreSQL archive."""

    format_version: int = Field(default=1, ge=1, le=1)
    source_database_fingerprint: str
    schema_name: str = Field(min_length=3, max_length=63)
    schema_version: int = Field(ge=1)
    schema_checksum: str
    archive_name: str = Field(min_length=6, max_length=240)
    archive_size_bytes: int = Field(ge=1)
    archive_sha256: str
    state_sha256: str
    table_counts: dict[str, int] = Field(min_length=1, max_length=64)
    audit_key_version: str
    created_at: datetime
    manifest_hmac: str

    @field_validator(
        "source_database_fingerprint",
        "schema_checksum",
        "archive_sha256",
        "state_sha256",
        "manifest_hmac",
    )
    @classmethod
    def hashes_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("control-plane backup hashes must be lowercase SHA-256")
        return value

    @field_validator("schema_name")
    @classmethod
    def schema_must_be_inert(cls, value: str) -> str:
        if _TABLE_NAME.fullmatch(value) is None:
            raise ValueError("control-plane backup schema name is invalid")
        return value

    @field_validator("archive_name")
    @classmethod
    def archive_name_must_be_local(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in {".", ".."} or not value.endswith(".dump"):
            raise ValueError("control-plane backup archive name is invalid")
        return value

    @field_validator("audit_key_version")
    @classmethod
    def audit_version_must_be_inert(cls, value: str) -> str:
        if _KEY_VERSION.fullmatch(value) is None:
            raise ValueError("control-plane backup audit key version is invalid")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("control-plane backup creation time must be timezone-aware")
        return value

    @field_validator("table_counts")
    @classmethod
    def table_counts_must_be_bounded(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            _TABLE_NAME.fullmatch(name) is None or isinstance(count, bool) or count < 0
            for name, count in value.items()
        ):
            raise ValueError("control-plane backup table counts are invalid")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def archive_and_manifest_must_not_share_identity(self) -> ControlPlaneBackupManifest:
        if self.archive_sha256 == self.manifest_hmac:
            raise ValueError("control-plane backup manifest signature is invalid")
        return self

    def unsigned_payload(self) -> dict[str, object]:
        """Return the exact canonical payload covered by the HMAC."""

        payload = self.model_dump(mode="json")
        payload.pop("manifest_hmac")
        return payload


class ControlPlaneRestoreVerification(FrozenDomainModel):
    """Evidence produced only after a fresh restore passes every integrity check."""

    target_database_fingerprint: str
    schema_version: int = Field(ge=1)
    schema_checksum: str
    state_sha256: str
    table_counts: dict[str, int] = Field(min_length=1, max_length=64)
    audited_workspaces: int = Field(ge=0)
    audit_events: int = Field(ge=0)
    active_pointers: int = Field(ge=0)
    transition_records: int = Field(ge=0)
    pending_outbox_records: int = Field(ge=0)
    quarantine_records: int = Field(ge=0)
    verified_at: datetime

    @field_validator(
        "target_database_fingerprint",
        "schema_checksum",
        "state_sha256",
    )
    @classmethod
    def hashes_must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("control-plane restore hashes must be lowercase SHA-256")
        return value

    @field_validator("table_counts")
    @classmethod
    def table_counts_must_be_bounded(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            _TABLE_NAME.fullmatch(name) is None or isinstance(count, bool) or count < 0
            for name, count in value.items()
        ):
            raise ValueError("control-plane restore table counts are invalid")
        return dict(sorted(value.items()))

    @field_validator("verified_at")
    @classmethod
    def verified_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("control-plane restore verification time must be timezone-aware")
        return value
