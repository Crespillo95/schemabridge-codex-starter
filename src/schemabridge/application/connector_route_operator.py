"""Governed inspect, prepare, approve, and CAS-apply connector-route flow."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import ValidationError

from schemabridge.application.ports.connector_route_operator import (
    ConnectorPrivateBindings,
    ConnectorRouteOperatorPort,
    ConnectorRouteStoreError,
    ConnectorRouteStoreErrorCode,
    ConnectorRouteWrite,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    ConnectorRouteApplyResult,
    ConnectorRouteApproval,
    ConnectorRouteOperation,
    ConnectorRouteProposal,
    ConnectorRouteSnapshot,
    ConnectorRouteStatus,
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    build_connector_route_approval,
    build_connector_route_proposal,
    connector_contract_fingerprint,
    connector_route_audit_fingerprint,
    connector_route_audit_id,
    connector_route_confirmation_for,
    connector_route_fingerprint,
    connector_route_head_fingerprint,
    connector_route_state_fingerprint,
    validate_postgres_type_contract_identity,
)

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INERT_IDEMPOTENCY = re.compile(r"^[^\x00-\x1f\x7f]{16,200}$")


class ConnectorRouteOperatorErrorCode(StrEnum):
    """Sanitized operator failures."""

    INVALID_REQUEST = "connector_route_invalid_request"
    UNAVAILABLE = "connector_route_unavailable"
    VERSION_CONFLICT = "connector_route_version_conflict"
    STATE_CONFLICT = "connector_route_state_conflict"
    IDEMPOTENCY_CONFLICT = "connector_route_idempotency_conflict"
    CONFIRMATION_MISMATCH = "connector_route_confirmation_mismatch"
    INVALID_RESPONSE = "connector_route_invalid_response"


class ConnectorRouteOperatorError(RuntimeError):
    """A bounded route-operator failure with no private binding material."""

    def __init__(
        self,
        code: ConnectorRouteOperatorErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PreparedConnectorRouteChange:
    """Exact proposal plus adapter-private handles for owner-only persistence."""

    proposal: ConnectorRouteProposal
    private_bindings: ConnectorPrivateBindings | None = field(repr=False)

    def __post_init__(self) -> None:
        expected = None if self.private_bindings is None else self.private_bindings.fingerprint
        if expected != self.proposal.private_bindings_fingerprint:
            raise ValueError("connector route prepared bindings do not match the proposal")


@dataclass(frozen=True, slots=True)
class ConnectorRouteOperator:
    """Keep connector routes read-only until a separate exact approval exists."""

    store: ConnectorRouteOperatorPort

    def inspect(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> ConnectorRouteSnapshot | None:
        _validate_scope(workspace_id, connection_id)
        try:
            current = self.store.inspect(
                workspace_id=workspace_id,
                connection_id=connection_id,
            )
        except ConnectorRouteStoreError as error:
            raise _operator_store_error(error, operation="inspection") from error
        except (TypeError, ValueError, ValidationError) as error:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.INVALID_RESPONSE,
                "connector route inspection response is invalid",
            ) from error
        if current is not None and (
            current.workspace_id != workspace_id or current.connection_id != connection_id
        ):
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.INVALID_RESPONSE,
                "connector route inspection response is invalid",
            )
        return current

    def prepare(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
        operation: ConnectorRouteOperation,
        expected_head_revision: int,
        contract_version: int | None,
        route_revision: int | None,
        expected_reader: str | None,
        source_identity_fingerprint: str | None,
        catalog_identity_fingerprint: str | None,
        type_contract_version: int | None,
        type_contract_fingerprint: str | None,
        cost_budget: QueryCostBudget | None,
        private_bindings: ConnectorPrivateBindings | None,
        proposed_by: str,
        idempotency_key: str,
    ) -> PreparedConnectorRouteChange:
        """Prepare one exact proposal after public inspection and perform no store write."""

        _validate_prepare_request(
            workspace_id=workspace_id,
            connection_id=connection_id,
            operation=operation,
            expected_head_revision=expected_head_revision,
            proposed_by=proposed_by,
            idempotency_key=idempotency_key,
        )
        _validate_requested_type_contract(
            operation=operation,
            type_contract_version=type_contract_version,
            type_contract_fingerprint=type_contract_fingerprint,
        )
        current = self.inspect(
            workspace_id=workspace_id,
            connection_id=connection_id,
        )
        expected_state_fingerprint = connector_route_state_fingerprint(
            workspace_id=workspace_id,
            connection_id=connection_id,
            snapshot=current,
        )
        if operation is ConnectorRouteOperation.DISABLE:
            values = _prepare_disable_values(
                current=current,
                expected_head_revision=expected_head_revision,
                contract_version=contract_version,
                route_revision=route_revision,
                expected_reader=expected_reader,
                source_identity_fingerprint=source_identity_fingerprint,
                catalog_identity_fingerprint=catalog_identity_fingerprint,
                type_contract_version=type_contract_version,
                type_contract_fingerprint=type_contract_fingerprint,
                cost_budget=cost_budget,
                private_bindings=private_bindings,
            )
        else:
            values = _prepare_enabled_values(
                operation=operation,
                current=current,
                expected_head_revision=expected_head_revision,
                contract_version=contract_version,
                route_revision=route_revision,
                expected_reader=expected_reader,
                source_identity_fingerprint=source_identity_fingerprint,
                catalog_identity_fingerprint=catalog_identity_fingerprint,
                type_contract_version=type_contract_version,
                type_contract_fingerprint=type_contract_fingerprint,
                cost_budget=cost_budget,
                private_bindings=private_bindings,
                workspace_id=workspace_id,
                connection_id=connection_id,
            )
        resolved_contract_version = values.contract_version
        resolved_route_revision = values.route_revision
        resolved_reader = values.expected_reader
        resolved_source_identity = values.source_identity_fingerprint
        resolved_catalog_identity = values.catalog_identity_fingerprint
        resolved_type_version = values.type_contract_version
        resolved_type_fingerprint = values.type_contract_fingerprint
        resolved_budget = values.cost_budget
        resolved_bindings = values.private_bindings
        contract_fingerprint = connector_contract_fingerprint(
            workspace_id=workspace_id,
            connection_id=connection_id,
            contract_version=resolved_contract_version,
            expected_reader=resolved_reader,
            source_identity_fingerprint=resolved_source_identity,
            catalog_identity_fingerprint=resolved_catalog_identity,
            type_contract_version=resolved_type_version,
            type_contract_fingerprint=resolved_type_fingerprint,
            cost_budget=resolved_budget,
        )
        if operation is ConnectorRouteOperation.DISABLE:
            assert current is not None
            route_fingerprint = current.target.route_fingerprint
            target = current.target
            binding_fingerprint = None
        else:
            assert resolved_bindings is not None
            binding_fingerprint = resolved_bindings.fingerprint
            route_fingerprint = connector_route_fingerprint(
                workspace_id=workspace_id,
                connection_id=connection_id,
                contract_version=resolved_contract_version,
                contract_fingerprint=contract_fingerprint,
                route_revision=resolved_route_revision,
                private_bindings_fingerprint=binding_fingerprint,
            )
            target = GovernedExecutionTarget(
                workspace_id=workspace_id,
                connection_id=connection_id,
                connector_kind=SourceConnectorKind.POSTGRESQL,
                dialect=SourceDialect.POSTGRESQL,
                route_revision=resolved_route_revision,
                route_fingerprint=route_fingerprint,
                expected_reader=resolved_reader,
                source_identity_fingerprint=resolved_source_identity,
                catalog_identity_fingerprint=resolved_catalog_identity,
                type_contract_fingerprint=resolved_type_fingerprint,
                cost_budget=resolved_budget,
                cost_budget_fingerprint=resolved_budget.fingerprint,
            )
        proposal = build_connector_route_proposal(
            workspace_id=workspace_id,
            connection_id=connection_id,
            operation=operation,
            expected_head_revision=expected_head_revision,
            expected_state_fingerprint=expected_state_fingerprint,
            contract_version=resolved_contract_version,
            type_contract_version=resolved_type_version,
            contract_fingerprint=contract_fingerprint,
            route_revision=resolved_route_revision,
            route_fingerprint=route_fingerprint,
            target=target,
            private_bindings_fingerprint=binding_fingerprint,
            idempotency_digest=_digest_idempotency_key(idempotency_key),
            proposed_by=proposed_by,
        )
        return PreparedConnectorRouteChange(
            proposal=proposal,
            private_bindings=resolved_bindings,
        )

    def approve(
        self,
        proposal: ConnectorRouteProposal,
        *,
        expected_proposal_fingerprint: str,
        confirmation: str,
        approved_by: str,
    ) -> ConnectorRouteApproval:
        """Create a separate approval object; this performs no store write."""

        if (
            not isinstance(proposal, ConnectorRouteProposal)
            or not isinstance(expected_proposal_fingerprint, str)
            or _SHA256.fullmatch(expected_proposal_fingerprint) is None
            or expected_proposal_fingerprint != proposal.fingerprint
            or not isinstance(approved_by, str)
            or _SAFE_ID.fullmatch(approved_by) is None
        ):
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.CONFIRMATION_MISMATCH,
                "connector route proposal was not confirmed exactly",
            )
        expected_confirmation = connector_route_confirmation_for(proposal.operation)
        if confirmation != expected_confirmation.value:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.CONFIRMATION_MISMATCH,
                "connector route confirmation phrase is invalid",
            )
        try:
            return build_connector_route_approval(
                proposal=proposal,
                confirmation=expected_confirmation,
                approved_by=approved_by,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.INVALID_REQUEST,
                "connector route approval is invalid",
            ) from error

    def apply(
        self,
        prepared: PreparedConnectorRouteChange,
        approval: ConnectorRouteApproval,
        *,
        expected_proposal_fingerprint: str,
        expected_approval_fingerprint: str,
    ) -> ConnectorRouteApplyResult:
        """Revalidate public state then invoke one exact database CAS mutation."""

        proposal = prepared.proposal
        _validate_apply_confirmation(
            proposal=proposal,
            approval=approval,
            expected_proposal_fingerprint=expected_proposal_fingerprint,
            expected_approval_fingerprint=expected_approval_fingerprint,
        )
        current = self.inspect(
            workspace_id=proposal.workspace_id,
            connection_id=proposal.connection_id,
        )
        observed_head = 0 if current is None else current.head_revision
        observed_state = connector_route_state_fingerprint(
            workspace_id=proposal.workspace_id,
            connection_id=proposal.connection_id,
            snapshot=current,
        )
        replay_or_conflict_probe = observed_head == proposal.expected_head_revision + 1
        if not replay_or_conflict_probe and observed_head != proposal.expected_head_revision:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.VERSION_CONFLICT,
                "connector route head changed after preparation",
            )
        if not replay_or_conflict_probe and observed_state != proposal.expected_state_fingerprint:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.STATE_CONFLICT,
                "connector route state changed after preparation",
            )
        if (
            prepared.private_bindings is None and proposal.private_bindings_fingerprint is not None
        ) or (
            prepared.private_bindings is not None
            and prepared.private_bindings.fingerprint != proposal.private_bindings_fingerprint
        ):
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.CONFIRMATION_MISMATCH,
                "connector route private bindings do not match the proposal",
            )
        head_fingerprint = connector_route_head_fingerprint(
            proposal=proposal,
            approval=approval,
        )
        audit_id = connector_route_audit_id(
            proposal=proposal,
            approval=approval,
            head_fingerprint=head_fingerprint,
        )
        audit_fingerprint = connector_route_audit_fingerprint(
            proposal=proposal,
            approval=approval,
            head_fingerprint=head_fingerprint,
            audit_id=audit_id,
        )
        change = ConnectorRouteWrite(
            workspace_id=proposal.workspace_id,
            connection_id=proposal.connection_id,
            operation=proposal.operation,
            expected_head_revision=proposal.expected_head_revision,
            contract_version=proposal.contract_version,
            route_revision=proposal.route_revision,
            route_fingerprint=proposal.route_fingerprint,
            expected_reader=proposal.target.expected_reader,
            source_identity_fingerprint=proposal.target.source_identity_fingerprint,
            catalog_identity_fingerprint=proposal.target.catalog_identity_fingerprint,
            type_contract_version=proposal.type_contract_version,
            type_contract_fingerprint=proposal.target.type_contract_fingerprint,
            cost_budget=proposal.target.cost_budget,
            cost_budget_fingerprint=proposal.target.cost_budget_fingerprint,
            contract_fingerprint=proposal.contract_fingerprint,
            target_fingerprint=proposal.target.fingerprint,
            private_bindings=prepared.private_bindings,
            proposal_fingerprint=proposal.fingerprint,
            approval_id=approval.approval_id,
            approval_fingerprint=approval.approval_fingerprint,
            actor_id=approval.approved_by,
            idempotency_digest=proposal.idempotency_digest,
            audit_id=audit_id,
            audit_fingerprint=audit_fingerprint,
            head_fingerprint=head_fingerprint,
            confirmation=approval.confirmation,
        )
        try:
            result = self.store.apply(change)
        except ConnectorRouteStoreError as error:
            raise _operator_store_error(error, operation="apply") from error
        except (TypeError, ValueError, ValidationError) as error:
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.INVALID_RESPONSE,
                "connector route apply response is invalid",
            ) from error
        _validate_apply_result(result, proposal=proposal, audit_id=audit_id)
        read_back = self.inspect(
            workspace_id=proposal.workspace_id,
            connection_id=proposal.connection_id,
        )
        if (
            read_back is None
            or read_back.head_revision != result.head_revision
            or read_back.contract_version != result.contract_version
            or read_back.target.fingerprint != result.target_fingerprint
            or read_back.route_status is not result.status
        ):
            raise ConnectorRouteOperatorError(
                ConnectorRouteOperatorErrorCode.INVALID_RESPONSE,
                "connector route apply read-back did not match the exact proposal",
            )
        return result


@dataclass(frozen=True, slots=True)
class _PreparedValues:
    contract_version: int
    route_revision: int
    expected_reader: str
    source_identity_fingerprint: str
    catalog_identity_fingerprint: str
    type_contract_version: int
    type_contract_fingerprint: str
    cost_budget: QueryCostBudget
    private_bindings: ConnectorPrivateBindings | None = field(repr=False)


def _prepare_enabled_values(
    *,
    operation: ConnectorRouteOperation,
    current: ConnectorRouteSnapshot | None,
    expected_head_revision: int,
    contract_version: int | None,
    route_revision: int | None,
    expected_reader: str | None,
    source_identity_fingerprint: str | None,
    catalog_identity_fingerprint: str | None,
    type_contract_version: int | None,
    type_contract_fingerprint: str | None,
    cost_budget: QueryCostBudget | None,
    private_bindings: ConnectorPrivateBindings | None,
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> _PreparedValues:
    if (
        contract_version is None
        or route_revision is None
        or expected_reader is None
        or source_identity_fingerprint is None
        or catalog_identity_fingerprint is None
        or type_contract_version is None
        or type_contract_fingerprint is None
        or cost_budget is None
        or private_bindings is None
    ):
        raise _invalid_request("connector route create/rotate values are incomplete")
    try:
        validate_postgres_type_contract_identity(
            version=type_contract_version,
            fingerprint=type_contract_fingerprint,
        )
    except ValueError:
        raise _invalid_request("connector route type contract is unsupported") from None
    if operation is ConnectorRouteOperation.CREATE:
        if (
            current is not None
            or expected_head_revision != 0
            or contract_version != 1
            or route_revision != 1
        ):
            raise _version_conflict("connector route creation state is stale")
    else:
        if (
            operation is not ConnectorRouteOperation.ROTATE
            or current is None
            or current.route_status is not ConnectorRouteStatus.ENABLED
            or current.connection_status is not ConnectorRouteStatus.ENABLED
            or expected_head_revision != current.head_revision
            or route_revision != current.target.route_revision + 1
            or contract_version not in {current.contract_version, current.contract_version + 1}
        ):
            raise _version_conflict("connector route rotation state is stale")
        current_contract_fingerprint = connector_contract_fingerprint(
            workspace_id=workspace_id,
            connection_id=connection_id,
            contract_version=current.contract_version,
            expected_reader=current.target.expected_reader,
            source_identity_fingerprint=current.target.source_identity_fingerprint,
            catalog_identity_fingerprint=current.target.catalog_identity_fingerprint,
            type_contract_version=current.type_contract_version,
            type_contract_fingerprint=current.target.type_contract_fingerprint,
            cost_budget=current.target.cost_budget,
        )
        desired_contract_fingerprint = connector_contract_fingerprint(
            workspace_id=workspace_id,
            connection_id=connection_id,
            contract_version=contract_version,
            expected_reader=expected_reader,
            source_identity_fingerprint=source_identity_fingerprint,
            catalog_identity_fingerprint=catalog_identity_fingerprint,
            type_contract_version=type_contract_version,
            type_contract_fingerprint=type_contract_fingerprint,
            cost_budget=cost_budget,
        )
        if contract_version == current.contract_version and (
            desired_contract_fingerprint != current_contract_fingerprint
        ):
            raise _version_conflict("connector contract changes require the next contract version")
    return _PreparedValues(
        contract_version=contract_version,
        route_revision=route_revision,
        expected_reader=expected_reader,
        source_identity_fingerprint=source_identity_fingerprint,
        catalog_identity_fingerprint=catalog_identity_fingerprint,
        type_contract_version=type_contract_version,
        type_contract_fingerprint=type_contract_fingerprint,
        cost_budget=cost_budget,
        private_bindings=private_bindings,
    )


def _prepare_disable_values(
    *,
    current: ConnectorRouteSnapshot | None,
    expected_head_revision: int,
    contract_version: int | None,
    route_revision: int | None,
    expected_reader: str | None,
    source_identity_fingerprint: str | None,
    catalog_identity_fingerprint: str | None,
    type_contract_version: int | None,
    type_contract_fingerprint: str | None,
    cost_budget: QueryCostBudget | None,
    private_bindings: ConnectorPrivateBindings | None,
) -> _PreparedValues:
    if any(
        value is not None
        for value in (
            contract_version,
            route_revision,
            expected_reader,
            source_identity_fingerprint,
            catalog_identity_fingerprint,
            type_contract_version,
            type_contract_fingerprint,
            cost_budget,
            private_bindings,
        )
    ):
        raise _invalid_request("connector route disable derives its exact current target")
    if (
        current is None
        or current.route_status is not ConnectorRouteStatus.ENABLED
        or expected_head_revision != current.head_revision
    ):
        raise _version_conflict("connector route disable state is stale")
    return _PreparedValues(
        contract_version=current.contract_version,
        route_revision=current.target.route_revision,
        expected_reader=current.target.expected_reader,
        source_identity_fingerprint=current.target.source_identity_fingerprint,
        catalog_identity_fingerprint=current.target.catalog_identity_fingerprint,
        type_contract_version=current.type_contract_version,
        type_contract_fingerprint=current.target.type_contract_fingerprint,
        cost_budget=current.target.cost_budget,
        private_bindings=None,
    )


def _validate_scope(
    workspace_id: str,
    connection_id: CatalogConnectionId,
) -> None:
    if (
        not isinstance(workspace_id, str)
        or _SAFE_ID.fullmatch(workspace_id) is None
        or not isinstance(connection_id, CatalogConnectionId)
    ):
        raise _invalid_request("connector route scope is invalid")


def _validate_prepare_request(
    *,
    workspace_id: str,
    connection_id: CatalogConnectionId,
    operation: ConnectorRouteOperation,
    expected_head_revision: int,
    proposed_by: str,
    idempotency_key: str,
) -> None:
    _validate_scope(workspace_id, connection_id)
    if (
        not isinstance(operation, ConnectorRouteOperation)
        or type(expected_head_revision) is not int
        or not 0 <= expected_head_revision < 9_223_372_036_854_775_807
        or not isinstance(proposed_by, str)
        or _SAFE_ID.fullmatch(proposed_by) is None
        or not isinstance(idempotency_key, str)
        or _INERT_IDEMPOTENCY.fullmatch(idempotency_key) is None
        or len(idempotency_key.encode("utf-8")) > 200
    ):
        raise _invalid_request("connector route prepare request is invalid")


def _validate_requested_type_contract(
    *,
    operation: ConnectorRouteOperation,
    type_contract_version: int | None,
    type_contract_fingerprint: str | None,
) -> None:
    if operation is ConnectorRouteOperation.DISABLE:
        return
    if type_contract_version is None or type_contract_fingerprint is None:
        raise _invalid_request("connector route create/rotate values are incomplete")
    try:
        validate_postgres_type_contract_identity(
            version=type_contract_version,
            fingerprint=type_contract_fingerprint,
        )
    except ValueError:
        raise _invalid_request("connector route type contract is unsupported") from None


def _validate_apply_confirmation(
    *,
    proposal: ConnectorRouteProposal,
    approval: ConnectorRouteApproval,
    expected_proposal_fingerprint: str,
    expected_approval_fingerprint: str,
) -> None:
    if (
        not isinstance(proposal, ConnectorRouteProposal)
        or not isinstance(approval, ConnectorRouteApproval)
        or expected_proposal_fingerprint != proposal.fingerprint
        or expected_approval_fingerprint != approval.approval_fingerprint
        or approval.proposal_fingerprint != proposal.fingerprint
        or approval.confirmation is not connector_route_confirmation_for(proposal.operation)
    ):
        raise ConnectorRouteOperatorError(
            ConnectorRouteOperatorErrorCode.CONFIRMATION_MISMATCH,
            "connector route proposal and approval were not confirmed exactly",
        )


def _validate_apply_result(
    result: ConnectorRouteApplyResult,
    *,
    proposal: ConnectorRouteProposal,
    audit_id: str,
) -> None:
    expected_status = (
        ConnectorRouteStatus.DISABLED
        if proposal.operation is ConnectorRouteOperation.DISABLE
        else ConnectorRouteStatus.ENABLED
    )
    if (
        not isinstance(result, ConnectorRouteApplyResult)
        or result.workspace_id != proposal.workspace_id
        or result.connection_id != proposal.connection_id
        or result.head_revision != proposal.expected_head_revision + 1
        or result.contract_version != proposal.contract_version
        or result.route_revision != proposal.route_revision
        or result.route_fingerprint != proposal.route_fingerprint
        or result.target_fingerprint != proposal.target.fingerprint
        or result.status is not expected_status
        or result.audit_id != audit_id
    ):
        raise ConnectorRouteOperatorError(
            ConnectorRouteOperatorErrorCode.INVALID_RESPONSE,
            "connector route apply response did not match the exact proposal",
        )


def _digest_idempotency_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operator_store_error(
    error: ConnectorRouteStoreError,
    *,
    operation: str,
) -> ConnectorRouteOperatorError:
    code = {
        ConnectorRouteStoreErrorCode.STATE_CONFLICT: (
            ConnectorRouteOperatorErrorCode.STATE_CONFLICT
        ),
        ConnectorRouteStoreErrorCode.IDEMPOTENCY_CONFLICT: (
            ConnectorRouteOperatorErrorCode.IDEMPOTENCY_CONFLICT
        ),
        ConnectorRouteStoreErrorCode.INVALID_RESPONSE: (
            ConnectorRouteOperatorErrorCode.INVALID_RESPONSE
        ),
        ConnectorRouteStoreErrorCode.UNAVAILABLE: ConnectorRouteOperatorErrorCode.UNAVAILABLE,
    }[error.code]
    return ConnectorRouteOperatorError(
        code,
        f"connector route {operation} is unavailable",
    )


def _invalid_request(message: str) -> ConnectorRouteOperatorError:
    return ConnectorRouteOperatorError(
        ConnectorRouteOperatorErrorCode.INVALID_REQUEST,
        message,
    )


def _version_conflict(message: str) -> ConnectorRouteOperatorError:
    return ConnectorRouteOperatorError(
        ConnectorRouteOperatorErrorCode.VERSION_CONFLICT,
        message,
    )


__all__ = [
    "ConnectorRouteOperator",
    "ConnectorRouteOperatorError",
    "ConnectorRouteOperatorErrorCode",
    "PreparedConnectorRouteChange",
]
