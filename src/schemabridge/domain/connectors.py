"""Pure, secret-free connector and governed execution-target contracts."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.semantic_registry import PhysicalValueType

MAX_ROUTE_REVISION = 9_223_372_036_854_775_807
MAX_EXPLAIN_TIMEOUT_MS = 60_000
MAX_EXPLAIN_RESPONSE_BYTES = 4 * 1_024 * 1_024
MAX_QUERY_TOTAL_COST = Decimal("999999999999999999")
MAX_ESTIMATED_ROOT_ROWS = 9_223_372_036_854_775_807
MAX_PLAN_NODES = 100_000
MAX_PLAN_DEPTH = 256
MAX_PLAN_WIDTH = 2_147_483_647
MAX_COST_DECIMAL_PLACES = 6
POSTGRES_TYPE_CONTRACT_VERSION: Literal[1] = 1
POSTGRES_TYPE_CONTRACT_FINGERPRINT = (
    "07e1d8336a0dab019b080f114a1d31e082aed2b434ab84a1be59d98e2d000589"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_POSTGRES_READER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_POSTGRES_NATIVE_TYPE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_POSTGRES_ARRAY_SUFFIX = re.compile(r"(?:\s*\[\s*\])+$")
_POSTGRES_TYPE_MODIFIER = re.compile(r"\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\)\s*")
_COST_BUDGET_FINGERPRINT_VERSION = "m28-query-cost-budget-v1"
_TARGET_FINGERPRINT_VERSION = "m28-governed-execution-target-v1"
_COST_ASSESSMENT_FINGERPRINT_VERSION = "m28-query-cost-assessment-v1"
_POSTGRES_TYPE_CONTRACT_FINGERPRINT_VERSION = "m28-postgresql-native-type-contract-v1"
_POSTGRES_SOURCE_IDENTITY_FINGERPRINT_VERSION = "m28-postgresql-source-identity-v1"
_ROUTE_CONTRACT_FINGERPRINT_VERSION = "m28-connector-contract-v1"
_ROUTE_BINDING_SET_FINGERPRINT_VERSION = "m28-connector-private-binding-set-v1"
_ROUTE_FINGERPRINT_VERSION = "m28-connector-route-v1"
_ROUTE_STATE_FINGERPRINT_VERSION = "m28-connector-route-state-v1"
_ROUTE_PROPOSAL_FINGERPRINT_VERSION = "m28-connector-route-proposal-v1"
_ROUTE_APPROVAL_FINGERPRINT_VERSION = "m28-connector-route-approval-v1"
_ROUTE_HEAD_FINGERPRINT_VERSION = "m28-connector-route-head-v1"
_ROUTE_AUDIT_ID_VERSION = "m28-connector-route-audit-id-v1"
_ROUTE_AUDIT_FINGERPRINT_VERSION = "m28-connector-route-audit-v1"

_POSTGRES_TYPE_ALIASES: tuple[tuple[PhysicalValueType, tuple[str, ...]], ...] = (
    (
        PhysicalValueType.STRING,
        (
            "bpchar",
            "char",
            "character",
            "character varying",
            "citext",
            "name",
            "text",
            "uuid",
            "varchar",
        ),
    ),
    (
        PhysicalValueType.INTEGER,
        (
            "bigint",
            "bigserial",
            "int",
            "int2",
            "int4",
            "int8",
            "integer",
            "serial",
            "serial2",
            "serial4",
            "serial8",
            "smallint",
            "smallserial",
        ),
    ),
    (
        PhysicalValueType.DECIMAL,
        (
            "decimal",
            "money",
            "numeric",
        ),
    ),
    (
        PhysicalValueType.FLOAT,
        (
            "double precision",
            "float",
            "float4",
            "float8",
            "real",
        ),
    ),
    (
        PhysicalValueType.BOOLEAN,
        (
            "bool",
            "boolean",
        ),
    ),
    (
        PhysicalValueType.DATE,
        ("date",),
    ),
    (
        PhysicalValueType.TIMESTAMP,
        (
            "timestamp",
            "timestamp with time zone",
            "timestamp without time zone",
            "timestamptz",
        ),
    ),
    (
        PhysicalValueType.BINARY,
        (
            "bit",
            "bit varying",
            "bytea",
            "varbit",
        ),
    ),
    (
        PhysicalValueType.STRUCT,
        (
            "hstore",
            "json",
            "jsonb",
        ),
    ),
)
_POSTGRES_ALIAS_LOOKUP = {
    alias: normalized for normalized, aliases in _POSTGRES_TYPE_ALIASES for alias in aliases
}
_POSTGRES_EXECUTABLE_TYPES = frozenset(
    {
        PhysicalValueType.STRING,
        PhysicalValueType.INTEGER,
        PhysicalValueType.FLOAT,
        PhysicalValueType.DECIMAL,
        PhysicalValueType.BOOLEAN,
        PhysicalValueType.DATE,
        PhysicalValueType.TIMESTAMP,
    }
)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _postgres_type_contract_payload() -> dict[str, object]:
    return {
        "fingerprint_version": _POSTGRES_TYPE_CONTRACT_FINGERPRINT_VERSION,
        "version": POSTGRES_TYPE_CONTRACT_VERSION,
        "aliases": {
            normalized.value: list(aliases) for normalized, aliases in _POSTGRES_TYPE_ALIASES
        },
        "executable_types": sorted(item.value for item in _POSTGRES_EXECUTABLE_TYPES),
        "array_policy": "explicit_non_executable",
        "domain_policy": "explicit_base_required",
        "numeric_scale_policy": "decimal",
        "timestamp_timezone_policy": "timestamp",
        "unknown_policy": "explicit_non_executable",
    }


def _validate_postgres_type_contract_definition() -> None:
    if _fingerprint(_postgres_type_contract_payload()) != POSTGRES_TYPE_CONTRACT_FINGERPRINT:
        raise RuntimeError("reviewed PostgreSQL type contract identity is out of sync")


_validate_postgres_type_contract_definition()


class SourceConnectorKind(StrEnum):
    """Closed executable source connectors supported by M28."""

    POSTGRESQL = "postgresql"


class SourceDialect(StrEnum):
    """Closed SQL dialects with a complete M28 safety toolchain."""

    POSTGRESQL = "postgresql"


class ConnectorRouteStatus(StrEnum):
    """Closed public lifecycle of one current connector route."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class ConnectorRouteOperation(StrEnum):
    """Closed connector-route mutations supported by the migrator."""

    CREATE = "create"
    ROTATE = "rotate"
    DISABLE = "disable"


class ConnectorRouteConfirmation(StrEnum):
    """Exact human confirmation phrases for route mutations."""

    CREATE = "CREATE CONNECTOR ROUTE"
    ROTATE = "ROTATE CONNECTOR ROUTE"
    DISABLE = "DISABLE CONNECTOR ROUTE"


class ConnectorRouteCapability(StrEnum):
    """Distinct private bindings; one capability cannot substitute for another."""

    PREFLIGHT = "preflight"
    CATALOG = "catalog"
    EXECUTION = "execution"
    PROFILE = "profile"


def connector_route_confirmation_for(
    operation: ConnectorRouteOperation,
) -> ConnectorRouteConfirmation:
    """Return the only confirmation accepted for one closed operation."""

    return {
        ConnectorRouteOperation.CREATE: ConnectorRouteConfirmation.CREATE,
        ConnectorRouteOperation.ROTATE: ConnectorRouteConfirmation.ROTATE,
        ConnectorRouteOperation.DISABLE: ConnectorRouteConfirmation.DISABLE,
    }[operation]


class PostgresNativeTypeKind(StrEnum):
    """How PostgreSQL metadata established one normalized type."""

    SCALAR = "scalar"
    ARRAY = "array"
    DOMAIN = "domain"
    UNKNOWN = "unknown"


class PostgresNativeTypeNormalization(FrozenDomainModel):
    """Deterministic PostgreSQL type evidence without casts or inferred approval."""

    contract_version: Literal[1] = POSTGRES_TYPE_CONTRACT_VERSION
    contract_fingerprint: str
    native_type: str | None = Field(default=None, max_length=200)
    domain_base_native_type: str | None = Field(default=None, max_length=200)
    kind: PostgresNativeTypeKind
    normalized_type: PhysicalValueType
    executable: bool

    @field_validator("contract_fingerprint")
    @classmethod
    def contract_fingerprint_must_be_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @field_validator("native_type", "domain_base_native_type")
    @classmethod
    def native_types_must_be_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_native_type(value)

    @model_validator(mode="after")
    def normalization_must_match_contract(self) -> PostgresNativeTypeNormalization:
        validate_postgres_type_contract_identity(
            version=self.contract_version,
            fingerprint=self.contract_fingerprint,
        )
        if self.kind is PostgresNativeTypeKind.DOMAIN:
            if self.domain_base_native_type is None:
                raise ValueError("PostgreSQL domain normalization requires an explicit base type")
            base_kind, base_type = _classify_postgres_native_type(self.domain_base_native_type)
            expected_type = (
                PhysicalValueType.UNKNOWN
                if base_kind is PostgresNativeTypeKind.UNKNOWN
                else base_type
            )
            if self.normalized_type is not expected_type:
                raise ValueError("PostgreSQL domain normalization does not match its base type")
        elif self.domain_base_native_type is not None:
            raise ValueError("only PostgreSQL domains may carry a base type")
        else:
            expected_kind, expected_type = _classify_postgres_native_type(self.native_type)
            if self.kind is not expected_kind or self.normalized_type is not expected_type:
                raise ValueError("PostgreSQL type normalization does not match native evidence")
        expected_executable = self.normalized_type in _POSTGRES_EXECUTABLE_TYPES
        if self.kind is PostgresNativeTypeKind.ARRAY and self.executable:
            raise ValueError("PostgreSQL arrays remain non-executable evidence")
        if self.kind is PostgresNativeTypeKind.UNKNOWN and self.executable:
            raise ValueError("unknown PostgreSQL types remain non-executable evidence")
        if self.executable is not expected_executable:
            raise ValueError("PostgreSQL normalized type execution support is inconsistent")
        return self


def normalize_postgres_native_type(
    native_type: str | None,
    *,
    domain_base_native_type: str | None = None,
) -> PostgresNativeTypeNormalization:
    """Normalize one bounded native type without guessing custom/domain semantics."""

    normalized_native = None if native_type is None else _bounded_native_type(native_type)
    normalized_base = (
        None if domain_base_native_type is None else _bounded_native_type(domain_base_native_type)
    )
    contract_fingerprint = postgres_type_contract_fingerprint()
    if normalized_base is not None:
        base_kind, base_type = _classify_postgres_native_type(normalized_base)
        normalized_type = (
            PhysicalValueType.UNKNOWN
            if base_kind in {PostgresNativeTypeKind.DOMAIN, PostgresNativeTypeKind.UNKNOWN}
            else base_type
        )
        return PostgresNativeTypeNormalization(
            contract_fingerprint=contract_fingerprint,
            native_type=normalized_native,
            domain_base_native_type=normalized_base,
            kind=PostgresNativeTypeKind.DOMAIN,
            normalized_type=normalized_type,
            executable=normalized_type in _POSTGRES_EXECUTABLE_TYPES,
        )
    kind, normalized_type = _classify_postgres_native_type(normalized_native)
    return PostgresNativeTypeNormalization(
        contract_fingerprint=contract_fingerprint,
        native_type=normalized_native,
        kind=kind,
        normalized_type=normalized_type,
        executable=normalized_type in _POSTGRES_EXECUTABLE_TYPES,
    )


def postgres_type_contract_fingerprint() -> str:
    """Return the immutable identity of the reviewed PostgreSQL type rules."""

    return POSTGRES_TYPE_CONTRACT_FINGERPRINT


def validate_postgres_type_contract_identity(
    *,
    version: int,
    fingerprint: str,
) -> None:
    """Reject any PostgreSQL type contract other than the reviewed v1 identity."""

    if (
        type(version) is not int
        or version != POSTGRES_TYPE_CONTRACT_VERSION
        or not isinstance(fingerprint, str)
        or fingerprint != postgres_type_contract_fingerprint()
    ):
        raise ValueError("PostgreSQL type contract identity is unsupported")


def postgres_source_identity_fingerprint(
    *,
    server_address: str,
    server_port: int,
    database: str,
    user: str,
) -> str:
    """Fingerprint PostgreSQL-observed identity without retaining its coordinates."""

    try:
        if not isinstance(server_address, str) or "%" in server_address:
            raise ValueError
        interface = ipaddress.ip_interface(server_address)
        if interface.network.prefixlen != interface.max_prefixlen:
            raise ValueError
        normalized_address = interface.ip.compressed
    except (TypeError, ValueError):
        raise ValueError("PostgreSQL source identity is invalid") from None
    if type(server_port) is not int or not 1 <= server_port <= 65_535:
        raise ValueError("PostgreSQL source identity is invalid")
    try:
        normalized_database = _bounded_identity_text(
            database,
            "database",
            maximum_bytes=63,
        )
        normalized_user = _postgres_reader(user, "source user")
    except (TypeError, ValueError):
        raise ValueError("PostgreSQL source identity is invalid") from None
    return _fingerprint(
        {
            "database": normalized_database,
            "fingerprint_version": _POSTGRES_SOURCE_IDENTITY_FINGERPRINT_VERSION,
            "server_address": normalized_address,
            "server_port": server_port,
            "user": normalized_user,
        }
    )


class QueryCostDecision(StrEnum):
    """Closed result of one independently bounded cost preflight."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class QueryCostRejectionCode(StrEnum):
    """Sanitized reasons that may reject a cost preflight."""

    TOTAL_COST_EXCEEDED = "total_cost_exceeded"
    ESTIMATED_ROWS_EXCEEDED = "estimated_rows_exceeded"
    PLAN_WIDTH_EXCEEDED = "plan_width_exceeded"
    PLAN_NODES_EXCEEDED = "plan_nodes_exceeded"
    PLAN_DEPTH_EXCEEDED = "plan_depth_exceeded"
    RESPONSE_BYTES_EXCEEDED = "response_bytes_exceeded"
    TIMEOUT = "query_cost_timeout"
    UNAVAILABLE = "query_cost_unavailable"
    INVALID = "query_cost_invalid"
    TARGET_FINGERPRINT_MISMATCH = "target_fingerprint_mismatch"
    BUDGET_FINGERPRINT_MISMATCH = "budget_fingerprint_mismatch"


class QueryCostBudget(FrozenDomainModel):
    """Immutable public bounds for one PostgreSQL cost preflight."""

    version: Literal[1] = 1
    explain_timeout_ms: int = Field(strict=True, ge=1, le=MAX_EXPLAIN_TIMEOUT_MS)
    max_response_bytes: int = Field(strict=True, ge=1, le=MAX_EXPLAIN_RESPONSE_BYTES)
    max_total_cost: Decimal
    max_estimated_rows: int = Field(strict=True, ge=0, le=MAX_ESTIMATED_ROOT_ROWS)
    max_plan_nodes: int = Field(strict=True, ge=1, le=MAX_PLAN_NODES)
    max_plan_depth: int = Field(strict=True, ge=1, le=MAX_PLAN_DEPTH)
    max_plan_width: int = Field(strict=True, ge=0, le=MAX_PLAN_WIDTH)

    @field_validator("max_total_cost", mode="before")
    @classmethod
    def total_cost_must_be_canonical(cls, value: object) -> Decimal:
        return _bounded_cost_decimal(value)

    @property
    def fingerprint(self) -> str:
        """Return the canonical identity of these exact public bounds."""

        return query_cost_budget_fingerprint(self)


class GovernedExecutionTarget(FrozenDomainModel):
    """One exact public execution target; secret material has no model surface."""

    version: Literal[1] = 1
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    connector_kind: SourceConnectorKind
    dialect: SourceDialect
    route_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_fingerprint: str
    expected_reader: str = Field(min_length=1, max_length=63)
    source_identity_fingerprint: str
    catalog_identity_fingerprint: str
    type_contract_fingerprint: str
    cost_budget: QueryCostBudget
    cost_budget_fingerprint: str

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        if _SAFE_WORKSPACE_ID.fullmatch(value) is None:
            raise ValueError("execution target workspace must be an inert lowercase identifier")
        return value

    @field_validator("expected_reader")
    @classmethod
    def reader_must_be_an_unquoted_postgres_identity(cls, value: str) -> str:
        return _postgres_reader(value, "expected reader")

    @field_validator(
        "route_fingerprint",
        "source_identity_fingerprint",
        "catalog_identity_fingerprint",
        "type_contract_fingerprint",
        "cost_budget_fingerprint",
    )
    @classmethod
    def fingerprints_must_be_lowercase_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @model_validator(mode="after")
    def execution_contract_must_be_exact(self) -> GovernedExecutionTarget:
        if (
            self.connector_kind is not SourceConnectorKind.POSTGRESQL
            or self.dialect is not SourceDialect.POSTGRESQL
        ):
            raise ValueError("only the PostgreSQL connector and dialect are executable")
        if self.cost_budget_fingerprint != self.cost_budget.fingerprint:
            raise ValueError("cost budget fingerprint does not match the governed budget")
        validate_postgres_type_contract_identity(
            version=POSTGRES_TYPE_CONTRACT_VERSION,
            fingerprint=self.type_contract_fingerprint,
        )
        return self

    @property
    def fingerprint(self) -> str:
        """Return the canonical identity used to bind approval and execution."""

        return governed_execution_target_fingerprint(self)


class ConnectorRouteSnapshot(FrozenDomainModel):
    """Sanitized current route state returned by migrator-only inspection."""

    version: Literal[1] = 1
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    head_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_status: ConnectorRouteStatus
    connection_status: ConnectorRouteStatus
    contract_version: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    type_contract_version: Literal[1] = POSTGRES_TYPE_CONTRACT_VERSION
    target: GovernedExecutionTarget

    @field_validator("workspace_id")
    @classmethod
    def workspace_must_be_inert(cls, value: str) -> str:
        if _SAFE_WORKSPACE_ID.fullmatch(value) is None:
            raise ValueError("connector route workspace must be an inert identifier")
        return value

    @model_validator(mode="after")
    def snapshot_must_identify_one_exact_target(self) -> ConnectorRouteSnapshot:
        validate_postgres_type_contract_identity(
            version=self.type_contract_version,
            fingerprint=self.target.type_contract_fingerprint,
        )
        if (
            self.target.workspace_id != self.workspace_id
            or self.target.connection_id != self.connection_id
            or self.head_revision < self.target.route_revision
        ):
            raise ValueError("connector route snapshot does not match its target")
        return self

    @property
    def fingerprint(self) -> str:
        """Return the canonical public state used for prepare/apply CAS."""

        return connector_route_state_fingerprint(
            workspace_id=self.workspace_id,
            connection_id=self.connection_id,
            snapshot=self,
        )


class ConnectorRouteProposal(FrozenDomainModel):
    """Exact public proposal; private binding values have no model surface."""

    version: Literal[1] = 1
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    operation: ConnectorRouteOperation
    expected_head_revision: int = Field(strict=True, ge=0, le=MAX_ROUTE_REVISION - 1)
    expected_state_fingerprint: str
    contract_version: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    type_contract_version: Literal[1] = POSTGRES_TYPE_CONTRACT_VERSION
    contract_fingerprint: str
    route_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_fingerprint: str
    target: GovernedExecutionTarget
    private_bindings_fingerprint: str | None = None
    idempotency_digest: str
    proposed_by: str = Field(min_length=3, max_length=200)
    fingerprint: str

    @field_validator("workspace_id", "proposed_by")
    @classmethod
    def identifiers_must_be_inert(cls, value: str) -> str:
        if _SAFE_WORKSPACE_ID.fullmatch(value) is None:
            raise ValueError("connector route proposal identifier is invalid")
        return value

    @field_validator(
        "expected_state_fingerprint",
        "contract_fingerprint",
        "route_fingerprint",
        "idempotency_digest",
        "fingerprint",
    )
    @classmethod
    def required_fingerprints_must_be_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @field_validator("private_bindings_fingerprint")
    @classmethod
    def optional_binding_fingerprint_must_be_sha256(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _lowercase_sha256(value)

    @model_validator(mode="after")
    def proposal_must_be_self_identifying(self) -> ConnectorRouteProposal:
        validate_postgres_type_contract_identity(
            version=self.type_contract_version,
            fingerprint=self.target.type_contract_fingerprint,
        )
        if (
            self.target.workspace_id != self.workspace_id
            or self.target.connection_id != self.connection_id
            or self.target.route_revision != self.route_revision
            or self.target.route_fingerprint != self.route_fingerprint
        ):
            raise ValueError("connector route proposal target is inconsistent")
        if self.operation is ConnectorRouteOperation.CREATE:
            if (
                self.expected_head_revision != 0
                or self.contract_version != 1
                or self.route_revision != 1
            ):
                raise ValueError("connector route creation revisions are invalid")
        elif self.expected_head_revision < 1:
            raise ValueError("connector route mutation requires an existing head")
        if self.operation is ConnectorRouteOperation.DISABLE:
            if self.private_bindings_fingerprint is not None:
                raise ValueError("connector route disable cannot carry private bindings")
        elif self.private_bindings_fingerprint is None:
            raise ValueError("connector route create/rotate requires private bindings")
        if self.fingerprint != connector_route_proposal_fingerprint(self):
            raise ValueError("connector route proposal fingerprint is invalid")
        return self


class ConnectorRouteApproval(FrozenDomainModel):
    """Separate human approval bound to one exact immutable proposal."""

    version: Literal[1] = 1
    proposal_fingerprint: str
    confirmation: ConnectorRouteConfirmation
    approved_by: str = Field(min_length=3, max_length=200)
    approval_id: str = Field(min_length=3, max_length=200)
    approval_fingerprint: str

    @field_validator("proposal_fingerprint", "approval_fingerprint")
    @classmethod
    def fingerprints_must_be_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @field_validator("approved_by")
    @classmethod
    def actor_must_be_inert(cls, value: str) -> str:
        if _SAFE_WORKSPACE_ID.fullmatch(value) is None:
            raise ValueError("connector route approval actor is invalid")
        return value

    @field_validator("approval_id")
    @classmethod
    def approval_id_must_be_canonical(cls, value: str) -> str:
        if re.fullmatch(r"connector_route_approval_[0-9a-f]{64}", value) is None:
            raise ValueError("connector route approval id is invalid")
        return value

    @model_validator(mode="after")
    def approval_must_be_self_identifying(self) -> ConnectorRouteApproval:
        expected_id = connector_route_approval_id(
            proposal_fingerprint=self.proposal_fingerprint,
            confirmation=self.confirmation,
            approved_by=self.approved_by,
        )
        if self.approval_id != expected_id:
            raise ValueError("connector route approval id is invalid")
        if self.approval_fingerprint != connector_route_approval_fingerprint(self):
            raise ValueError("connector route approval fingerprint is invalid")
        return self


class ConnectorRouteApplyResult(FrozenDomainModel):
    """Sanitized exact result of one applied or replayed route change."""

    version: Literal[1] = 1
    workspace_id: str = Field(min_length=3, max_length=200)
    connection_id: CatalogConnectionId
    head_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    contract_version: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_revision: int = Field(strict=True, ge=1, le=MAX_ROUTE_REVISION)
    route_fingerprint: str
    target_fingerprint: str
    status: ConnectorRouteStatus
    audit_id: str = Field(min_length=3, max_length=96)

    @field_validator("workspace_id")
    @classmethod
    def result_workspace_must_be_inert(cls, value: str) -> str:
        if _SAFE_WORKSPACE_ID.fullmatch(value) is None:
            raise ValueError("connector route result workspace is invalid")
        return value

    @field_validator("route_fingerprint", "target_fingerprint")
    @classmethod
    def result_fingerprints_must_be_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @field_validator("audit_id")
    @classmethod
    def audit_id_must_be_canonical(cls, value: str) -> str:
        if re.fullmatch(r"connector_route_audit_[0-9a-f]{64}", value) is None:
            raise ValueError("connector route audit id is invalid")
        return value


class QueryCostAssessment(FrozenDomainModel):
    """Sanitized observed cost facts with no raw plan, SQL, or topology."""

    version: Literal[1] = 1
    decision: QueryCostDecision
    rejection_codes: tuple[QueryCostRejectionCode, ...] = Field(
        default=(),
        max_length=len(QueryCostRejectionCode),
    )
    total_cost: Decimal | None = None
    estimated_root_rows: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_ESTIMATED_ROOT_ROWS,
    )
    plan_width: int | None = Field(default=None, strict=True, ge=0, le=MAX_PLAN_WIDTH)
    plan_node_count: int | None = Field(default=None, strict=True, ge=1, le=MAX_PLAN_NODES)
    plan_depth: int | None = Field(default=None, strict=True, ge=1, le=MAX_PLAN_DEPTH)
    response_bytes: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EXPLAIN_RESPONSE_BYTES,
    )
    observed_reader: str | None = Field(default=None, min_length=1, max_length=63)
    read_only: bool | None = Field(default=None, strict=True)
    explain_timeout_ms: int = Field(strict=True, ge=1, le=MAX_EXPLAIN_TIMEOUT_MS)
    target_fingerprint: str
    budget_fingerprint: str
    cost_budget: QueryCostBudget = Field(exclude=True, repr=False)

    @field_validator("total_cost", mode="before")
    @classmethod
    def observed_cost_must_be_canonical(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _bounded_cost_decimal(value)

    @field_validator("observed_reader")
    @classmethod
    def observed_reader_must_be_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _postgres_reader(value, "observed reader")

    @field_validator("target_fingerprint", "budget_fingerprint")
    @classmethod
    def assessment_fingerprints_must_be_sha256(cls, value: str) -> str:
        return _lowercase_sha256(value)

    @field_validator("rejection_codes")
    @classmethod
    def rejection_codes_must_be_canonical(
        cls,
        values: tuple[QueryCostRejectionCode, ...],
    ) -> tuple[QueryCostRejectionCode, ...]:
        canonical = tuple(sorted(set(values), key=lambda item: item.value))
        if values != canonical:
            raise ValueError("query cost rejection codes must be unique and sorted")
        return values

    @model_validator(mode="after")
    def decision_and_budget_must_be_consistent(self) -> QueryCostAssessment:
        if self.budget_fingerprint != self.cost_budget.fingerprint:
            raise ValueError("assessment budget fingerprint does not match the governed budget")
        if self.decision is QueryCostDecision.REJECTED:
            if not self.rejection_codes:
                raise ValueError("rejected query cost assessment requires a rejection code")
            return self
        if self.rejection_codes:
            raise ValueError("accepted query cost assessment cannot contain rejection codes")
        required_metrics = (
            self.total_cost,
            self.estimated_root_rows,
            self.plan_width,
            self.plan_node_count,
            self.plan_depth,
            self.response_bytes,
            self.observed_reader,
            self.read_only,
        )
        if any(value is None for value in required_metrics):
            raise ValueError("accepted query cost assessment requires complete observed facts")
        if self.read_only is not True:
            raise ValueError("accepted query cost assessment requires a read-only transaction")
        if not self._is_within_budget():
            raise ValueError("accepted query cost assessment exceeds its governed budget")
        return self

    def _is_within_budget(self) -> bool:
        budget = self.cost_budget
        return (
            self.total_cost is not None
            and self.total_cost <= budget.max_total_cost
            and self.estimated_root_rows is not None
            and self.estimated_root_rows <= budget.max_estimated_rows
            and self.plan_width is not None
            and self.plan_width <= budget.max_plan_width
            and self.plan_node_count is not None
            and self.plan_node_count <= budget.max_plan_nodes
            and self.plan_depth is not None
            and self.plan_depth <= budget.max_plan_depth
            and self.response_bytes is not None
            and self.response_bytes <= budget.max_response_bytes
            and self.explain_timeout_ms <= budget.explain_timeout_ms
        )

    @property
    def fingerprint(self) -> str:
        """Return the canonical sanitized assessment identity."""

        return query_cost_assessment_fingerprint(self)


def query_cost_budget_fingerprint(budget: QueryCostBudget) -> str:
    """Fingerprint a budget without depending on Decimal JSON coercion."""

    payload = {
        "fingerprint_version": _COST_BUDGET_FINGERPRINT_VERSION,
        "version": budget.version,
        "explain_timeout_ms": budget.explain_timeout_ms,
        "max_response_bytes": budget.max_response_bytes,
        "max_total_cost": _canonical_decimal_text(budget.max_total_cost),
        "max_estimated_rows": budget.max_estimated_rows,
        "max_plan_nodes": budget.max_plan_nodes,
        "max_plan_depth": budget.max_plan_depth,
        "max_plan_width": budget.max_plan_width,
    }
    return _fingerprint(payload)


def governed_execution_target_fingerprint(target: GovernedExecutionTarget) -> str:
    """Fingerprint every public fact that can change the governed source target."""

    payload = {
        "fingerprint_version": _TARGET_FINGERPRINT_VERSION,
        "version": target.version,
        "workspace_id": target.workspace_id,
        "connection_id": target.connection_id.root,
        "connector_kind": target.connector_kind.value,
        "dialect": target.dialect.value,
        "route_revision": target.route_revision,
        "route_fingerprint": target.route_fingerprint,
        "expected_reader": target.expected_reader,
        "source_identity_fingerprint": target.source_identity_fingerprint,
        "catalog_identity_fingerprint": target.catalog_identity_fingerprint,
        "type_contract_fingerprint": target.type_contract_fingerprint,
        "cost_budget": {
            "version": target.cost_budget.version,
            "explain_timeout_ms": target.cost_budget.explain_timeout_ms,
            "max_response_bytes": target.cost_budget.max_response_bytes,
            "max_total_cost": _canonical_decimal_text(target.cost_budget.max_total_cost),
            "max_estimated_rows": target.cost_budget.max_estimated_rows,
            "max_plan_nodes": target.cost_budget.max_plan_nodes,
            "max_plan_depth": target.cost_budget.max_plan_depth,
            "max_plan_width": target.cost_budget.max_plan_width,
        },
        "cost_budget_fingerprint": target.cost_budget_fingerprint,
    }
    return _fingerprint(payload)


def connector_contract_fingerprint(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    contract_version: int,
    expected_reader: str,
    source_identity_fingerprint: str,
    catalog_identity_fingerprint: str,
    type_contract_version: int,
    type_contract_fingerprint: str,
    cost_budget: QueryCostBudget,
) -> str:
    """Fingerprint one immutable public connector contract revision."""

    if _SAFE_WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise ValueError("connector contract workspace is invalid")
    if not 1 <= contract_version <= MAX_ROUTE_REVISION:
        raise ValueError("connector contract version is invalid")
    validate_postgres_type_contract_identity(
        version=type_contract_version,
        fingerprint=type_contract_fingerprint,
    )
    return _fingerprint(
        {
            "connection_id": connection_id.root,
            "connector_kind": SourceConnectorKind.POSTGRESQL.value,
            "cost_budget": _cost_budget_payload(cost_budget),
            "cost_budget_fingerprint": cost_budget.fingerprint,
            "contract_version": contract_version,
            "dialect": SourceDialect.POSTGRESQL.value,
            "expected_reader": _postgres_reader(expected_reader, "expected reader"),
            "fingerprint_version": _ROUTE_CONTRACT_FINGERPRINT_VERSION,
            "source_identity_fingerprint": _lowercase_sha256(source_identity_fingerprint),
            "catalog_identity_fingerprint": _lowercase_sha256(catalog_identity_fingerprint),
            "type_contract_fingerprint": type_contract_fingerprint,
            "type_contract_version": type_contract_version,
            "version": 1,
            "workspace_id": workspace_id,
        }
    )


def connector_private_bindings_fingerprint(
    capability_digests: dict[ConnectorRouteCapability, str],
) -> str:
    """Bind four one-way capability digests without accepting private values."""

    if set(capability_digests) != set(ConnectorRouteCapability):
        raise ValueError("connector private binding digest set is incomplete")
    normalized = {
        capability.value: _lowercase_sha256(capability_digests[capability])
        for capability in ConnectorRouteCapability
    }
    if len(set(normalized.values())) != len(ConnectorRouteCapability):
        raise ValueError("connector private binding digests must be distinct")
    return _fingerprint(
        {
            "capability_digests": normalized,
            "fingerprint_version": _ROUTE_BINDING_SET_FINGERPRINT_VERSION,
            "version": 1,
        }
    )


def connector_route_fingerprint(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    contract_version: int,
    contract_fingerprint: str,
    route_revision: int,
    private_bindings_fingerprint: str,
) -> str:
    """Fingerprint one public route revision without exposing its handles."""

    if _SAFE_WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise ValueError("connector route workspace is invalid")
    if not 1 <= contract_version <= MAX_ROUTE_REVISION:
        raise ValueError("connector contract version is invalid")
    if not 1 <= route_revision <= MAX_ROUTE_REVISION:
        raise ValueError("connector route revision is invalid")
    return _fingerprint(
        {
            "connection_id": connection_id.root,
            "contract_fingerprint": _lowercase_sha256(contract_fingerprint),
            "contract_version": contract_version,
            "fingerprint_version": _ROUTE_FINGERPRINT_VERSION,
            "private_bindings_fingerprint": _lowercase_sha256(private_bindings_fingerprint),
            "route_revision": route_revision,
            "version": 1,
            "workspace_id": workspace_id,
        }
    )


def connector_route_state_fingerprint(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    snapshot: ConnectorRouteSnapshot | None,
) -> str:
    """Fingerprint current public state, including the explicit absent state."""

    if _SAFE_WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise ValueError("connector route workspace is invalid")
    if snapshot is None:
        state: dict[str, object] = {"status": "absent"}
    else:
        if snapshot.workspace_id != workspace_id or snapshot.connection_id != connection_id:
            raise ValueError("connector route state scope is invalid")
        state = {
            "connection_status": snapshot.connection_status.value,
            "contract_version": snapshot.contract_version,
            "head_revision": snapshot.head_revision,
            "route_status": snapshot.route_status.value,
            "target_fingerprint": snapshot.target.fingerprint,
            "type_contract_version": snapshot.type_contract_version,
        }
    return _fingerprint(
        {
            "connection_id": connection_id.root,
            "fingerprint_version": _ROUTE_STATE_FINGERPRINT_VERSION,
            "state": state,
            "version": 1,
            "workspace_id": workspace_id,
        }
    )


def build_connector_route_proposal(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    operation: ConnectorRouteOperation,
    expected_head_revision: int,
    expected_state_fingerprint: str,
    contract_version: int,
    type_contract_version: int,
    contract_fingerprint: str,
    route_revision: int,
    route_fingerprint: str,
    target: GovernedExecutionTarget,
    private_bindings_fingerprint: str | None,
    idempotency_digest: str,
    proposed_by: str,
) -> ConnectorRouteProposal:
    """Build and validate one self-identifying exact proposal."""

    values: dict[str, object] = {
        "version": 1,
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "operation": operation,
        "expected_head_revision": expected_head_revision,
        "expected_state_fingerprint": expected_state_fingerprint,
        "contract_version": contract_version,
        "type_contract_version": type_contract_version,
        "contract_fingerprint": contract_fingerprint,
        "route_revision": route_revision,
        "route_fingerprint": route_fingerprint,
        "target": target,
        "private_bindings_fingerprint": private_bindings_fingerprint,
        "idempotency_digest": idempotency_digest,
        "proposed_by": proposed_by,
    }
    fingerprint = _fingerprint(_connector_route_proposal_payload(values))
    return ConnectorRouteProposal.model_validate({**values, "fingerprint": fingerprint})


def connector_route_proposal_fingerprint(proposal: ConnectorRouteProposal) -> str:
    """Fingerprint one exact prepared route change."""

    return _fingerprint(_connector_route_proposal_payload(proposal))


def connector_route_approval_id(
    *,
    proposal_fingerprint: str,
    confirmation: ConnectorRouteConfirmation,
    approved_by: str,
) -> str:
    """Return the deterministic immutable approval identifier."""

    if _SAFE_WORKSPACE_ID.fullmatch(approved_by) is None:
        raise ValueError("connector route approval actor is invalid")
    digest = _fingerprint(
        {
            "approved_by": approved_by,
            "confirmation": confirmation.value,
            "kind": "connector_route_approval_id",
            "proposal_fingerprint": _lowercase_sha256(proposal_fingerprint),
            "version": 1,
        }
    )
    return f"connector_route_approval_{digest}"


def connector_route_approval_fingerprint(approval: ConnectorRouteApproval) -> str:
    """Fingerprint one separately confirmed human approval."""

    return _fingerprint(
        {
            "approval_id": approval.approval_id,
            "approved_by": approval.approved_by,
            "confirmation": approval.confirmation.value,
            "fingerprint_version": _ROUTE_APPROVAL_FINGERPRINT_VERSION,
            "proposal_fingerprint": approval.proposal_fingerprint,
            "version": approval.version,
        }
    )


def build_connector_route_approval(
    *,
    proposal: ConnectorRouteProposal,
    confirmation: ConnectorRouteConfirmation,
    approved_by: str,
) -> ConnectorRouteApproval:
    """Create a separate human approval for one exact proposal."""

    expected_confirmation = connector_route_confirmation_for(proposal.operation)
    if confirmation is not expected_confirmation:
        raise ValueError("connector route confirmation does not match the operation")
    approval_id = connector_route_approval_id(
        proposal_fingerprint=proposal.fingerprint,
        confirmation=confirmation,
        approved_by=approved_by,
    )
    payload = {
        "version": 1,
        "proposal_fingerprint": proposal.fingerprint,
        "confirmation": confirmation,
        "approved_by": approved_by,
        "approval_id": approval_id,
    }
    approval_fingerprint = _fingerprint(
        {
            "approval_id": approval_id,
            "approved_by": approved_by,
            "confirmation": confirmation.value,
            "fingerprint_version": _ROUTE_APPROVAL_FINGERPRINT_VERSION,
            "proposal_fingerprint": proposal.fingerprint,
            "version": 1,
        }
    )
    return ConnectorRouteApproval.model_validate(
        {**payload, "approval_fingerprint": approval_fingerprint}
    )


def connector_route_head_fingerprint(
    *,
    proposal: ConnectorRouteProposal,
    approval: ConnectorRouteApproval,
) -> str:
    """Fingerprint the exact resulting head without database clock data."""

    _validate_route_approval_pair(proposal, approval)
    return _fingerprint(
        {
            "approval_fingerprint": approval.approval_fingerprint,
            "connection_id": proposal.connection_id.root,
            "contract_version": proposal.contract_version,
            "fingerprint_version": _ROUTE_HEAD_FINGERPRINT_VERSION,
            "head_revision": proposal.expected_head_revision + 1,
            "route_revision": proposal.route_revision,
            "status": _resulting_status(proposal.operation).value,
            "target_fingerprint": proposal.target.fingerprint,
            "version": 1,
            "workspace_id": proposal.workspace_id,
        }
    )


def connector_route_audit_id(
    *,
    proposal: ConnectorRouteProposal,
    approval: ConnectorRouteApproval,
    head_fingerprint: str,
) -> str:
    """Return a deterministic replay-stable audit identifier."""

    _validate_route_approval_pair(proposal, approval)
    digest = _fingerprint(
        {
            "approval_fingerprint": approval.approval_fingerprint,
            "fingerprint_version": _ROUTE_AUDIT_ID_VERSION,
            "head_fingerprint": _lowercase_sha256(head_fingerprint),
            "idempotency_digest": proposal.idempotency_digest,
            "proposal_fingerprint": proposal.fingerprint,
            "version": 1,
        }
    )
    return f"connector_route_audit_{digest}"


def connector_route_audit_fingerprint(
    *,
    proposal: ConnectorRouteProposal,
    approval: ConnectorRouteApproval,
    head_fingerprint: str,
    audit_id: str,
) -> str:
    """Fingerprint every stable fact persisted by the route audit row."""

    _validate_route_approval_pair(proposal, approval)
    if re.fullmatch(r"connector_route_audit_[0-9a-f]{64}", audit_id) is None:
        raise ValueError("connector route audit id is invalid")
    return _fingerprint(
        {
            "approval_fingerprint": approval.approval_fingerprint,
            "audit_id": audit_id,
            "connection_id": proposal.connection_id.root,
            "contract_fingerprint": proposal.contract_fingerprint,
            "contract_version": proposal.contract_version,
            "expected_head_revision": proposal.expected_head_revision,
            "fingerprint_version": _ROUTE_AUDIT_FINGERPRINT_VERSION,
            "head_fingerprint": _lowercase_sha256(head_fingerprint),
            "idempotency_digest": proposal.idempotency_digest,
            "operation": proposal.operation.value,
            "proposal_fingerprint": proposal.fingerprint,
            "resulting_head_revision": proposal.expected_head_revision + 1,
            "resulting_status": _resulting_status(proposal.operation).value,
            "route_fingerprint": proposal.route_fingerprint,
            "route_revision": proposal.route_revision,
            "target_fingerprint": proposal.target.fingerprint,
            "version": 1,
            "workspace_id": proposal.workspace_id,
        }
    )


def query_cost_assessment_fingerprint(assessment: QueryCostAssessment) -> str:
    """Fingerprint only sanitized cost facts and their public bindings."""

    payload = {
        "fingerprint_version": _COST_ASSESSMENT_FINGERPRINT_VERSION,
        "version": assessment.version,
        "decision": assessment.decision.value,
        "rejection_codes": [item.value for item in assessment.rejection_codes],
        "total_cost": (
            _canonical_decimal_text(assessment.total_cost)
            if assessment.total_cost is not None
            else None
        ),
        "estimated_root_rows": assessment.estimated_root_rows,
        "plan_width": assessment.plan_width,
        "plan_node_count": assessment.plan_node_count,
        "plan_depth": assessment.plan_depth,
        "response_bytes": assessment.response_bytes,
        "observed_reader": assessment.observed_reader,
        "read_only": assessment.read_only,
        "explain_timeout_ms": assessment.explain_timeout_ms,
        "target_fingerprint": assessment.target_fingerprint,
        "budget_fingerprint": assessment.budget_fingerprint,
    }
    return _fingerprint(payload)


def _cost_budget_payload(budget: QueryCostBudget) -> dict[str, object]:
    return {
        "explain_timeout_ms": budget.explain_timeout_ms,
        "max_estimated_rows": budget.max_estimated_rows,
        "max_plan_depth": budget.max_plan_depth,
        "max_plan_nodes": budget.max_plan_nodes,
        "max_plan_width": budget.max_plan_width,
        "max_response_bytes": budget.max_response_bytes,
        "max_total_cost": _canonical_decimal_text(budget.max_total_cost),
        "version": budget.version,
    }


def _connector_route_proposal_payload(
    proposal: ConnectorRouteProposal | dict[str, object],
) -> dict[str, object]:
    if isinstance(proposal, ConnectorRouteProposal):
        connection_value: object = proposal.connection_id
        operation_value: object = proposal.operation
        target_value: object = proposal.target
        values: dict[str, object] = {
            "contract_fingerprint": proposal.contract_fingerprint,
            "contract_version": proposal.contract_version,
            "expected_head_revision": proposal.expected_head_revision,
            "expected_state_fingerprint": proposal.expected_state_fingerprint,
            "idempotency_digest": proposal.idempotency_digest,
            "private_bindings_fingerprint": proposal.private_bindings_fingerprint,
            "proposed_by": proposal.proposed_by,
            "route_fingerprint": proposal.route_fingerprint,
            "route_revision": proposal.route_revision,
            "type_contract_version": proposal.type_contract_version,
            "version": proposal.version,
            "workspace_id": proposal.workspace_id,
        }
    else:
        connection_value = proposal["connection_id"]
        operation_value = proposal["operation"]
        target_value = proposal["target"]
        values = proposal
    if not isinstance(connection_value, CatalogConnectionId):
        raise ValueError("connector route proposal connection is invalid")
    if not isinstance(operation_value, ConnectorRouteOperation):
        raise ValueError("connector route proposal operation is invalid")
    if not isinstance(target_value, GovernedExecutionTarget):
        raise ValueError("connector route proposal target is invalid")
    return {
        "connection_id": connection_value.root,
        "contract_fingerprint": values["contract_fingerprint"],
        "contract_version": values["contract_version"],
        "expected_head_revision": values["expected_head_revision"],
        "expected_state_fingerprint": values["expected_state_fingerprint"],
        "fingerprint_version": _ROUTE_PROPOSAL_FINGERPRINT_VERSION,
        "idempotency_digest": values["idempotency_digest"],
        "operation": operation_value.value,
        "private_bindings_fingerprint": values["private_bindings_fingerprint"],
        "proposed_by": values["proposed_by"],
        "route_fingerprint": values["route_fingerprint"],
        "route_revision": values["route_revision"],
        "target_fingerprint": target_value.fingerprint,
        "type_contract_version": values["type_contract_version"],
        "version": values["version"],
        "workspace_id": values["workspace_id"],
    }


def _validate_route_approval_pair(
    proposal: ConnectorRouteProposal,
    approval: ConnectorRouteApproval,
) -> None:
    if (
        approval.proposal_fingerprint != proposal.fingerprint
        or approval.confirmation is not connector_route_confirmation_for(proposal.operation)
    ):
        raise ValueError("connector route approval does not match its proposal")


def _resulting_status(operation: ConnectorRouteOperation) -> ConnectorRouteStatus:
    if operation is ConnectorRouteOperation.DISABLE:
        return ConnectorRouteStatus.DISABLED
    return ConnectorRouteStatus.ENABLED


def _classify_postgres_native_type(
    native_type: str | None,
) -> tuple[PostgresNativeTypeKind, PhysicalValueType]:
    if native_type is None:
        return PostgresNativeTypeKind.UNKNOWN, PhysicalValueType.UNKNOWN
    candidate = " ".join(native_type.casefold().split())
    if candidate.startswith("pg_catalog."):
        candidate = candidate.removeprefix("pg_catalog.")
    if candidate == "array":
        return PostgresNativeTypeKind.ARRAY, PhysicalValueType.ARRAY
    if _POSTGRES_ARRAY_SUFFIX.search(candidate) is not None:
        return PostgresNativeTypeKind.ARRAY, PhysicalValueType.ARRAY
    if candidate.startswith("_") and candidate[1:] in _POSTGRES_ALIAS_LOOKUP:
        return PostgresNativeTypeKind.ARRAY, PhysicalValueType.ARRAY
    candidate = " ".join(_POSTGRES_TYPE_MODIFIER.sub(" ", candidate, count=1).split())
    normalized = _POSTGRES_ALIAS_LOOKUP.get(candidate)
    if normalized is None:
        return PostgresNativeTypeKind.UNKNOWN, PhysicalValueType.UNKNOWN
    return PostgresNativeTypeKind.SCALAR, normalized


def _bounded_native_type(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("PostgreSQL native type must be bounded inert text") from None
    if (
        not value
        or value.strip() != value
        or len(value) > 200
        or len(encoded) > 400
        or _POSTGRES_NATIVE_TYPE.fullmatch(value) is None
    ):
        raise ValueError("PostgreSQL native type must be bounded inert text")
    return value


def _bounded_cost_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("query cost must be a decimal, not a boolean")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("query cost must be finite")
        candidate = str(value)
    elif isinstance(value, (Decimal, int, str)):
        candidate = str(value)
    else:
        raise ValueError("query cost must be a bounded decimal")
    if isinstance(value, str) and (not value or value.strip() != value):
        raise ValueError("query cost must be a canonical decimal")
    try:
        parsed = Decimal(candidate)
    except (InvalidOperation, ValueError):
        raise ValueError("query cost must be a bounded decimal") from None
    if not parsed.is_finite():
        raise ValueError("query cost must be finite")
    if parsed < 0 or parsed > MAX_QUERY_TOTAL_COST:
        raise ValueError("query cost is outside the supported bounds")
    normalized = parsed.normalize()
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int) or max(0, -exponent) > MAX_COST_DECIMAL_PLACES:
        raise ValueError("query cost has excessive decimal precision")
    return Decimal(_canonical_decimal_text(parsed))


def _canonical_decimal_text(value: Decimal) -> str:
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    if fixed in {"", "-0"}:
        return "0"
    return fixed


def _lowercase_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("connector fingerprint must be lowercase SHA-256")
    return value


def _postgres_reader(value: str, label: str) -> str:
    if _POSTGRES_READER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded PostgreSQL role identifier")
    return value


def _bounded_identity_text(value: str, label: str, *, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"PostgreSQL source {label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"PostgreSQL source {label} is invalid") from None
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"PostgreSQL source {label} is invalid")
    return value
