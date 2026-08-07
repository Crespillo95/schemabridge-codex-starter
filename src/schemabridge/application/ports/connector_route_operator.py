"""Private migrator port for exact connector-route inspection and mutation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    MAX_ROUTE_REVISION,
    ConnectorRouteApplyResult,
    ConnectorRouteCapability,
    ConnectorRouteConfirmation,
    ConnectorRouteOperation,
    ConnectorRouteSnapshot,
    QueryCostBudget,
    connector_private_bindings_fingerprint,
    connector_route_confirmation_for,
    validate_postgres_type_contract_identity,
)

_BINDING_REF = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_ID = re.compile(r"^connector_route_audit_[0-9a-f]{64}$")
_APPROVAL_ID = re.compile(r"^connector_route_approval_[0-9a-f]{64}$")
_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class ConnectorRouteStoreErrorCode(StrEnum):
    """Stable adapter failures that disclose no private route material."""

    UNAVAILABLE = "connector_route_store_unavailable"
    STATE_CONFLICT = "connector_route_state_conflict"
    IDEMPOTENCY_CONFLICT = "connector_route_idempotency_conflict"
    INVALID_RESPONSE = "connector_route_invalid_response"


class ConnectorRouteStoreError(RuntimeError):
    """Sanitized private route-store failure."""

    def __init__(self, code: ConnectorRouteStoreErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ConnectorPrivateBinding:
    """One opaque handle pinned to an immutable provider version."""

    reference: str = field(repr=False)
    provider_secret_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, str)
            or _BINDING_REF.fullmatch(self.reference) is None
            or "://" in self.reference
            or "@" in self.reference
            or type(self.provider_secret_version) is not int
            or not 1 <= self.provider_secret_version <= MAX_ROUTE_REVISION
        ):
            raise ValueError("connector private binding is invalid")


@dataclass(frozen=True, slots=True)
class ConnectorPrivateBindings:
    """Four distinct adapter-private handles with exact provider versions."""

    preflight: ConnectorPrivateBinding = field(repr=False)
    catalog: ConnectorPrivateBinding = field(repr=False)
    execution: ConnectorPrivateBinding = field(repr=False)
    profile: ConnectorPrivateBinding = field(repr=False)

    def __post_init__(self) -> None:
        values = (self.preflight, self.catalog, self.execution, self.profile)
        if any(not isinstance(value, ConnectorPrivateBinding) for value in values):
            raise ValueError("connector private bindings are invalid")
        references = tuple(value.reference for value in values)
        if len(set(references)) != len(references):
            raise ValueError("connector private bindings must be capability-distinct")

    @property
    def fingerprint(self) -> str:
        """Bind one-way per-capability digests without exposing handle values."""

        return connector_private_bindings_fingerprint(
            {
                ConnectorRouteCapability.PREFLIGHT: (
                    _digest(self.preflight.reference),
                    self.preflight.provider_secret_version,
                ),
                ConnectorRouteCapability.CATALOG: (
                    _digest(self.catalog.reference),
                    self.catalog.provider_secret_version,
                ),
                ConnectorRouteCapability.EXECUTION: (
                    _digest(self.execution.reference),
                    self.execution.provider_secret_version,
                ),
                ConnectorRouteCapability.PROFILE: (
                    _digest(self.profile.reference),
                    self.profile.provider_secret_version,
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ConnectorRouteWrite:
    """Complete exact argument set for the fixed PostgreSQL CAS function."""

    workspace_id: str
    connection_id: CatalogConnectionId
    operation: ConnectorRouteOperation
    expected_head_revision: int
    contract_version: int
    route_revision: int
    route_fingerprint: str
    expected_reader: str
    source_identity_fingerprint: str
    catalog_identity_fingerprint: str
    type_contract_version: int
    type_contract_fingerprint: str
    cost_budget: QueryCostBudget
    cost_budget_fingerprint: str
    contract_fingerprint: str
    target_fingerprint: str
    private_bindings: ConnectorPrivateBindings | None = field(repr=False)
    proposal_fingerprint: str
    approval_id: str
    approval_fingerprint: str
    actor_id: str
    idempotency_digest: str
    audit_id: str
    audit_fingerprint: str
    head_fingerprint: str
    confirmation: ConnectorRouteConfirmation

    def __post_init__(self) -> None:
        validate_postgres_type_contract_identity(
            version=self.type_contract_version,
            fingerprint=self.type_contract_fingerprint,
        )
        if (
            not isinstance(self.workspace_id, str)
            or _SAFE_ID.fullmatch(self.workspace_id) is None
            or not isinstance(self.connection_id, CatalogConnectionId)
            or type(self.expected_head_revision) is not int
            or not 0 <= self.expected_head_revision < 9_223_372_036_854_775_807
            or type(self.contract_version) is not int
            or not 1 <= self.contract_version <= 9_223_372_036_854_775_807
            or type(self.route_revision) is not int
            or not 1 <= self.route_revision <= 9_223_372_036_854_775_807
            or not isinstance(self.actor_id, str)
            or _SAFE_ID.fullmatch(self.actor_id) is None
            or not isinstance(self.expected_reader, str)
            or _READER.fullmatch(self.expected_reader) is None
            or not isinstance(self.approval_id, str)
            or _APPROVAL_ID.fullmatch(self.approval_id) is None
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in (
                    self.route_fingerprint,
                    self.source_identity_fingerprint,
                    self.catalog_identity_fingerprint,
                    self.type_contract_fingerprint,
                    self.cost_budget_fingerprint,
                    self.contract_fingerprint,
                    self.target_fingerprint,
                    self.proposal_fingerprint,
                    self.approval_fingerprint,
                    self.idempotency_digest,
                    self.audit_fingerprint,
                    self.head_fingerprint,
                )
            )
            or not isinstance(self.audit_id, str)
            or _AUDIT_ID.fullmatch(self.audit_id) is None
            or self.cost_budget_fingerprint != self.cost_budget.fingerprint
            or self.confirmation is not connector_route_confirmation_for(self.operation)
        ):
            raise ValueError("connector route write is invalid")
        if self.operation is ConnectorRouteOperation.DISABLE:
            if self.private_bindings is not None:
                raise ValueError("connector route disable cannot carry private bindings")
        elif self.private_bindings is None:
            raise ValueError("connector route create/rotate requires private bindings")


class ConnectorRouteOperatorPort(Protocol):
    """Migrator-only exact inspection and CAS mutation boundary."""

    def inspect(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> ConnectorRouteSnapshot | None:
        """Read current public route state without private route access."""

    def apply(self, change: ConnectorRouteWrite) -> ConnectorRouteApplyResult:
        """Invoke the fixed CAS function and return its sanitized exact result."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ConnectorPrivateBinding",
    "ConnectorPrivateBindings",
    "ConnectorRouteOperatorPort",
    "ConnectorRouteStoreError",
    "ConnectorRouteStoreErrorCode",
    "ConnectorRouteWrite",
]
