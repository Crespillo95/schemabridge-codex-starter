"""Pure Phase-A authoring contracts for governed registry-v2 join changes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.registry_changes import (
    PreparedRegistryJoinProposal,
    RegistryJoinChangeDraft,
    RegistryJoinProfileRequest,
)
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileJob

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
_MAX_AUTHORING_BYTES = 256 * 1024
MAX_REGISTRY_CHANGE_HISTORY = 1_000


class RegistryChangePermission(StrEnum):
    """Closed permissions for the tenant-bound join-change authoring boundary."""

    VIEW = "registry_change:view"
    REQUEST_PROFILE = "registry_change:request_profile"
    FINALIZE_DRAFT = "registry_change:finalize_draft"
    DECIDE = "registry_change:decide"
    PREPARE_PUBLICATION = "registry_change:prepare_publication"
    AUDIT_VIEW = "registry_change:audit_view"


class RegistryChangeAuditEvent(StrEnum):
    PROFILE_REQUESTED = "profile_requested"
    PROFILE_JOB_BOUND = "profile_job_bound"
    DRAFT_FINALIZED = "draft_finalized"
    DECISION_RECORDED = "decision_recorded"
    PUBLICATION_PREPARED = "publication_prepared"


class RequestRegistryJoinProfileInput(FrozenDomainModel):
    """Client-safe intent; every authority-bearing fact is reread server-side."""

    change_id: str = Field(min_length=3, max_length=80)
    connection_id: CatalogConnectionId
    proposal: JoinProposal
    expected_base_registry: OnboardingRegistryBase
    expected_execution_target_fingerprint: str

    @field_validator("change_id")
    @classmethod
    def change_id_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry change identifier is invalid")
        return value

    @field_validator("expected_execution_target_fingerprint")
    @classmethod
    def target_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry change execution target fingerprint")


class RegistryJoinProfileAuthoringRequest(FrozenDomainModel):
    """Durable owner-bound request persisted before the profile job is enqueued."""

    id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=200)
    owner_actor_id: str = Field(min_length=1, max_length=200)
    request: RegistryJoinProfileRequest
    created_at: datetime
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("id")
    @classmethod
    def identifier_must_be_inert(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("registry join authoring request identifier is invalid")
        return value

    @field_validator("workspace_id", "owner_actor_id")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry join authoring request text must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry join authoring request timestamp")

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_must_be_sha256(cls, value: str) -> str:
        return _sha256(value, "registry join authoring request fingerprint")

    @model_validator(mode="after")
    def request_must_be_scoped_and_fingerprinted(
        self,
    ) -> RegistryJoinProfileAuthoringRequest:
        if (
            self.request.scope.workspace_id != self.workspace_id
            or self.request.requested_at != self.created_at
            or self.fingerprint
            != registry_change_authoring_fingerprint(
                self.model_dump(mode="json", exclude={"fingerprint"})
            )
        ):
            raise ValueError("registry join authoring request authority does not match")
        if len(_canonical_json(self.model_dump(mode="json"))) > _MAX_AUTHORING_BYTES:
            raise ValueError("registry join authoring request exceeds its byte limit")
        return self

    @classmethod
    def create(
        cls,
        *,
        id: str,
        workspace_id: str,
        owner_actor_id: str,
        request: RegistryJoinProfileRequest,
        created_at: datetime,
    ) -> RegistryJoinProfileAuthoringRequest:
        payload = {
            "id": id,
            "workspace_id": workspace_id,
            "owner_actor_id": owner_actor_id,
            "request": request,
            "created_at": created_at,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_change_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RegistryChangeAuditRecord(FrozenDomainModel):
    """Immutable zero-external-write audit fact for one authoring transition."""

    id: str = Field(min_length=3, max_length=200)
    workspace_id: str = Field(min_length=3, max_length=200)
    change_id: str = Field(min_length=3, max_length=80)
    event: RegistryChangeAuditEvent
    actor_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    source_revision: int = Field(ge=0)
    resulting_revision: int = Field(ge=0)
    previous_fingerprint: str | None = None
    resulting_fingerprint: str
    profile_request_fingerprint: str | None = None
    profile_job_id: str | None = Field(default=None, min_length=3, max_length=200)
    decision_id: str | None = Field(default=None, min_length=3, max_length=200)
    proposal_id: str | None = Field(default=None, min_length=3, max_length=200)
    external_writes_performed: Literal[False] = False
    fingerprint: str

    @field_validator("workspace_id", "actor_id")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("registry change audit text must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "registry change audit timestamp")

    @field_validator(
        "previous_fingerprint",
        "resulting_fingerprint",
        "profile_request_fingerprint",
        "fingerprint",
    )
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, "registry change audit fingerprint")
        return value

    @model_validator(mode="after")
    def event_must_be_exact_and_fingerprinted(self) -> RegistryChangeAuditRecord:
        request = self.profile_request_fingerprint is not None
        job = self.profile_job_id is not None
        decision = self.decision_id is not None
        proposal = self.proposal_id is not None
        if self.event is RegistryChangeAuditEvent.PROFILE_REQUESTED:
            valid = (
                self.source_revision == self.resulting_revision == 0
                and self.previous_fingerprint is None
                and request
                and not job
                and not decision
                and not proposal
            )
        elif self.event is RegistryChangeAuditEvent.PROFILE_JOB_BOUND:
            valid = (
                self.source_revision == self.resulting_revision == 0
                and request
                and job
                and not decision
                and not proposal
            )
        elif self.event is RegistryChangeAuditEvent.DRAFT_FINALIZED:
            valid = (
                self.source_revision == 0
                and self.resulting_revision == 1
                and self.previous_fingerprint is not None
                and request
                and job
                and not decision
                and not proposal
            )
        elif self.event is RegistryChangeAuditEvent.DECISION_RECORDED:
            valid = (
                self.resulting_revision == self.source_revision + 1
                and self.previous_fingerprint is not None
                and not request
                and not job
                and decision
                and not proposal
            )
        else:
            valid = (
                self.event is RegistryChangeAuditEvent.PUBLICATION_PREPARED
                and self.resulting_revision == self.source_revision
                and self.previous_fingerprint is not None
                and not request
                and not job
                and not decision
                and proposal
            )
        if not valid:
            raise ValueError("registry change audit event binding is invalid")
        if self.fingerprint != registry_change_authoring_fingerprint(
            self.model_dump(mode="json", exclude={"fingerprint"})
        ):
            raise ValueError("registry change audit fingerprint does not match")
        return self

    @classmethod
    def create(
        cls,
        *,
        workspace_id: str,
        change_id: str,
        event: RegistryChangeAuditEvent,
        actor_id: str,
        occurred_at: datetime,
        source_revision: int,
        resulting_revision: int,
        previous_fingerprint: str | None,
        resulting_fingerprint: str,
        profile_request_fingerprint: str | None = None,
        profile_job_id: str | None = None,
        decision_id: str | None = None,
        proposal_id: str | None = None,
    ) -> RegistryChangeAuditRecord:
        identity = registry_change_authoring_fingerprint(
            {
                "actor_id": actor_id,
                "change_id": change_id,
                "event": event.value,
                "resulting_fingerprint": resulting_fingerprint,
                "resulting_revision": resulting_revision,
                "workspace_id": workspace_id,
            }
        )
        payload = {
            "id": f"registry_change_audit_{identity}",
            "workspace_id": workspace_id,
            "change_id": change_id,
            "event": event,
            "actor_id": actor_id,
            "occurred_at": occurred_at,
            "source_revision": source_revision,
            "resulting_revision": resulting_revision,
            "previous_fingerprint": previous_fingerprint,
            "resulting_fingerprint": resulting_fingerprint,
            "profile_request_fingerprint": profile_request_fingerprint,
            "profile_job_id": profile_job_id,
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "external_writes_performed": False,
        }
        construct: Any = cls.model_construct
        provisional = construct(**payload, fingerprint="0" * 64)
        return cls(
            **payload,
            fingerprint=registry_change_authoring_fingerprint(
                provisional.model_dump(mode="json", exclude={"fingerprint"})
            ),
        )


class RegistryJoinProfileAuthoringMutation(FrozenDomainModel):
    authoring: RegistryJoinProfileAuthoringRequest
    job: SemanticJoinProfileJob | None = None
    replayed: bool = False
    external_writes_performed: Literal[False] = False

    @model_validator(mode="after")
    def job_must_match_request(self) -> RegistryJoinProfileAuthoringMutation:
        if self.job is not None and (
            self.job.workspace_id != self.authoring.workspace_id
            or self.job.scan_id != self.authoring.request.scan_id
            or self.job.bound_proposal != self.authoring.request.proposal
            or self.job.execution_target != self.authoring.request.execution_target
        ):
            raise ValueError("registry join profile job differs from its persisted request")
        return self


class RegistryJoinDraftMutation(FrozenDomainModel):
    authoring: RegistryJoinProfileAuthoringRequest
    draft: RegistryJoinChangeDraft
    replayed: bool = False
    external_writes_performed: Literal[False] = False

    @model_validator(mode="after")
    def draft_must_match_authoring(self) -> RegistryJoinDraftMutation:
        if (
            self.draft.id != self.authoring.id
            or self.draft.workspace_id != self.authoring.workspace_id
            or self.draft.owner_actor_id != self.authoring.owner_actor_id
            or self.draft.profile_campaign.request != self.authoring.request
        ):
            raise ValueError("registry join draft differs from its authoring request")
        return self


class RegistryJoinPreparation(FrozenDomainModel):
    authoring: RegistryJoinProfileAuthoringRequest
    draft: RegistryJoinChangeDraft
    proposal: PreparedRegistryJoinProposal
    replayed: bool = False
    external_writes_performed: Literal[False] = False

    @model_validator(mode="after")
    def preparation_must_be_exact(self) -> RegistryJoinPreparation:
        if (
            self.draft.id != self.authoring.id
            or self.draft.prepared_proposal_id != self.proposal.id
            or self.draft.prepared_proposal_fingerprint != self.proposal.fingerprint
            or self.proposal.draft_id != self.draft.id
        ):
            raise ValueError("registry join preparation binding is invalid")
        return self


class RegistryJoinChangeSnapshot(FrozenDomainModel):
    authoring: RegistryJoinProfileAuthoringRequest
    draft: RegistryJoinChangeDraft | None = None
    audit_visible: bool = False
    history_truncated: bool = False
    audit: tuple[RegistryChangeAuditRecord, ...] = ()

    @model_validator(mode="after")
    def snapshot_must_be_tenant_bound(self) -> RegistryJoinChangeSnapshot:
        if self.draft is not None and (
            self.draft.workspace_id != self.authoring.workspace_id
            or self.draft.id != self.authoring.id
        ):
            raise ValueError("registry join snapshot combines different resources")
        if not self.audit_visible and (self.audit or self.history_truncated):
            raise ValueError("hidden registry change audit cannot carry history")
        if any(
            item.workspace_id != self.authoring.workspace_id or item.change_id != self.authoring.id
            for item in self.audit
        ):
            raise ValueError("registry join snapshot audit belongs to another resource")
        return self


def registry_change_authoring_fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(_jsonable(value))).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, FrozenDomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


__all__ = [
    "MAX_REGISTRY_CHANGE_HISTORY",
    "RegistryChangeAuditEvent",
    "RegistryChangeAuditRecord",
    "RegistryChangePermission",
    "RegistryJoinChangeSnapshot",
    "RegistryJoinDraftMutation",
    "RegistryJoinPreparation",
    "RegistryJoinProfileAuthoringMutation",
    "RegistryJoinProfileAuthoringRequest",
    "RequestRegistryJoinProfileInput",
    "registry_change_authoring_fingerprint",
]
