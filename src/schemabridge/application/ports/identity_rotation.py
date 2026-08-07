"""Persistence boundary for approval-gated identity-key rotation."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from schemabridge.domain.identity_rotation import (
    ApprovedIdentityRotation,
    IdentityAuthorizationScope,
    IdentityInitializationApproval,
    IdentityRotationCompletion,
    IdentityRotationState,
    VerifiedDualKeyOidcDerivation,
)


class IdentityRotationStoreErrorCode(StrEnum):
    """Sanitized durable-store failures."""

    CONFLICT = "identity_rotation_store_conflict"
    UNAVAILABLE = "identity_rotation_store_unavailable"
    CROSS_WORKSPACE = "identity_rotation_cross_workspace"
    COLLISION = "identity_rotation_collision"
    CYCLE = "identity_rotation_cycle"
    INCOMPLETE_OWNER_BINDING = "identity_rotation_incomplete_owner_binding"
    STALE_PLAN = "identity_rotation_stale_plan"


class IdentityRotationStoreError(RuntimeError):
    """Safe failure returned by a concrete identity-rotation store."""

    def __init__(self, code: IdentityRotationStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class IdentityRotationStorePort(Protocol):
    """Persist only opaque bindings, approval facts, and versioned provenance."""

    def initialize_verified_state(
        self,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        approval: IdentityInitializationApproval,
    ) -> IdentityRotationState:
        """Atomically initialize opaque bindings and the exact approved audit event."""

    def load_state(self, workspace_id: str) -> IdentityRotationState:
        """Read owners and binding history without historical workflow payloads."""

    def reserve_approved_plan(
        self,
        approved: ApprovedIdentityRotation,
    ) -> ApprovedIdentityRotation:
        """Reserve an exact plan idempotently or reject an identity collision."""

    def load_approved_plan(self, plan_id: str) -> ApprovedIdentityRotation | None:
        """Load one exact prior approval reservation."""

    def load_completion(self, plan_id: str) -> IdentityRotationCompletion | None:
        """Load an immutable completion for idempotent replay."""

    def complete_rotation(
        self,
        completion: IdentityRotationCompletion,
    ) -> IdentityRotationCompletion:
        """Atomically append new bindings; never rewrite historical payload bytes."""


class IdentityBindingResolverPort(Protocol):
    """Resolve current opaque identities to same-lineage historical grant aliases."""

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        """Return bounded active/previous workspace aliases for one active workspace."""

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        """Return only same-key workspace/actor pairs for one current principal."""


class PersistedIdentityBindingResolverPort(Protocol):
    """Validate immutable job identities against verified rotation history."""

    def resolve_persisted_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        """Return a verified chain containing one exact persisted identity pair."""
