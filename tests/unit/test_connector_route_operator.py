from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from schemabridge.application.connector_route_operator import (
    ConnectorRouteOperator,
    ConnectorRouteOperatorError,
    ConnectorRouteOperatorErrorCode,
    PreparedConnectorRouteChange,
)
from schemabridge.application.ports.connector_route_operator import (
    ConnectorPrivateBindings,
    ConnectorRouteStoreError,
    ConnectorRouteStoreErrorCode,
    ConnectorRouteWrite,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    ConnectorRouteApplyResult,
    ConnectorRouteApproval,
    ConnectorRouteOperation,
    ConnectorRouteSnapshot,
    ConnectorRouteStatus,
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    connector_route_confirmation_for,
    postgres_source_identity_fingerprint,
    postgres_type_contract_fingerprint,
)

WORKSPACE_ID = "workspace-route-test"
CONNECTION_ID = CatalogConnectionId("connection-route-test")
ACTOR_ID = "platform-admin"
IDEMPOTENCY_KEY = "connector-route-create-request-0001"
SOURCE_IDENTITY_FINGERPRINT = "1" * 64
CATALOG_IDENTITY_FINGERPRINT = "2" * 64
PRIVATE_VALUES = (
    "vault:preflight:opaque-alpha",
    "vault:catalog:opaque-bravo",
    "vault:execution:opaque-charlie",
    "vault:profile:opaque-delta",
)


def _budget(*, total_cost: str = "12345.67") -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=2_500,
        max_response_bytes=262_144,
        max_total_cost=Decimal(total_cost),
        max_estimated_rows=250_000,
        max_plan_nodes=500,
        max_plan_depth=32,
        max_plan_width=8_192,
    )


def _bindings() -> ConnectorPrivateBindings:
    return ConnectorPrivateBindings(
        preflight=PRIVATE_VALUES[0],
        catalog=PRIVATE_VALUES[1],
        execution=PRIVATE_VALUES[2],
        profile=PRIVATE_VALUES[3],
    )


@dataclass
class _Store:
    current: ConnectorRouteSnapshot | None = None
    writes: list[ConnectorRouteWrite] = field(default_factory=list)
    inspections: int = 0
    replays: dict[str, tuple[ConnectorRouteWrite, ConnectorRouteApplyResult]] = field(
        default_factory=dict
    )
    persist_read_back: bool = True

    def inspect(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> ConnectorRouteSnapshot | None:
        assert workspace_id == WORKSPACE_ID
        assert connection_id == CONNECTION_ID
        self.inspections += 1
        return self.current

    def apply(self, change: ConnectorRouteWrite) -> ConnectorRouteApplyResult:
        self.writes.append(change)
        replay = self.replays.get(change.idempotency_digest)
        if replay is not None:
            previous, result = replay
            if previous != change:
                raise ConnectorRouteStoreError(
                    ConnectorRouteStoreErrorCode.IDEMPOTENCY_CONFLICT,
                    "sanitized conflict",
                )
            return result
        status = (
            ConnectorRouteStatus.DISABLED
            if change.operation is ConnectorRouteOperation.DISABLE
            else ConnectorRouteStatus.ENABLED
        )
        target = GovernedExecutionTarget(
            workspace_id=change.workspace_id,
            connection_id=change.connection_id,
            connector_kind=SourceConnectorKind.POSTGRESQL,
            dialect=SourceDialect.POSTGRESQL,
            route_revision=change.route_revision,
            route_fingerprint=change.route_fingerprint,
            expected_reader=change.expected_reader,
            source_identity_fingerprint=change.source_identity_fingerprint,
            catalog_identity_fingerprint=change.catalog_identity_fingerprint,
            type_contract_fingerprint=change.type_contract_fingerprint,
            cost_budget=change.cost_budget,
            cost_budget_fingerprint=change.cost_budget_fingerprint,
        )
        result = ConnectorRouteApplyResult(
            workspace_id=change.workspace_id,
            connection_id=change.connection_id,
            head_revision=change.expected_head_revision + 1,
            contract_version=change.contract_version,
            route_revision=change.route_revision,
            route_fingerprint=change.route_fingerprint,
            target_fingerprint=change.target_fingerprint,
            status=status,
            audit_id=change.audit_id,
        )
        if self.persist_read_back:
            self.current = ConnectorRouteSnapshot(
                workspace_id=change.workspace_id,
                connection_id=change.connection_id,
                head_revision=result.head_revision,
                route_status=status,
                connection_status=ConnectorRouteStatus.ENABLED,
                contract_version=change.contract_version,
                type_contract_version=change.type_contract_version,
                target=target,
            )
        self.replays[change.idempotency_digest] = (change, result)
        return result


def _prepare_create(
    operator: ConnectorRouteOperator,
    *,
    bindings: ConnectorPrivateBindings | None = None,
    source_identity_fingerprint: str = SOURCE_IDENTITY_FINGERPRINT,
    catalog_identity_fingerprint: str = CATALOG_IDENTITY_FINGERPRINT,
    type_contract_version: int = 1,
    type_contract_fingerprint: str | None = None,
) -> PreparedConnectorRouteChange:
    return operator.prepare(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
        operation=ConnectorRouteOperation.CREATE,
        expected_head_revision=0,
        contract_version=1,
        route_revision=1,
        expected_reader="schemabridge_source_reader",
        source_identity_fingerprint=source_identity_fingerprint,
        catalog_identity_fingerprint=catalog_identity_fingerprint,
        type_contract_version=type_contract_version,
        type_contract_fingerprint=(
            postgres_type_contract_fingerprint()
            if type_contract_fingerprint is None
            else type_contract_fingerprint
        ),
        cost_budget=_budget(),
        private_bindings=bindings or _bindings(),
        proposed_by=ACTOR_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _approve(
    operator: ConnectorRouteOperator,
    prepared: PreparedConnectorRouteChange,
) -> ConnectorRouteApproval:
    confirmation = connector_route_confirmation_for(prepared.proposal.operation)
    return operator.approve(
        prepared.proposal,
        expected_proposal_fingerprint=prepared.proposal.fingerprint,
        confirmation=confirmation.value,
        approved_by=ACTOR_ID,
    )


def test_prepare_and_separate_approval_perform_zero_store_writes() -> None:
    store = _Store()
    operator = ConnectorRouteOperator(store)

    prepared = _prepare_create(operator)
    approval = _approve(operator, prepared)

    assert store.writes == []
    assert prepared.proposal.expected_head_revision == 0
    assert prepared.proposal.target.dialect is SourceDialect.POSTGRESQL
    assert approval.proposal_fingerprint == prepared.proposal.fingerprint
    assert approval.confirmation.value == "CREATE CONNECTOR ROUTE"


@pytest.mark.parametrize(
    ("type_contract_version", "type_contract_fingerprint"),
    (
        (2, postgres_type_contract_fingerprint()),
        (1, "f" * 64),
    ),
)
def test_prepare_rejects_unsupported_type_contract_before_store_access(
    type_contract_version: int,
    type_contract_fingerprint: str,
) -> None:
    store = _Store()

    with pytest.raises(ConnectorRouteOperatorError) as raised:
        _prepare_create(
            ConnectorRouteOperator(store),
            type_contract_version=type_contract_version,
            type_contract_fingerprint=type_contract_fingerprint,
        )

    assert raised.value.code is ConnectorRouteOperatorErrorCode.INVALID_REQUEST
    assert store.inspections == 0
    assert store.writes == []


def test_private_bindings_are_absent_from_repr_public_model_and_errors() -> None:
    bindings = _bindings()
    store = _Store()
    operator = ConnectorRouteOperator(store)
    prepared = _prepare_create(operator, bindings=bindings)

    rendered = repr(bindings) + repr(prepared)
    public_json = prepared.proposal.model_dump_json()
    for private_value in PRIVATE_VALUES:
        assert private_value not in rendered
        assert private_value not in public_json

    changed = ConnectorPrivateBindings(
        preflight="vault:preflight:changed-echo",
        catalog=PRIVATE_VALUES[1],
        execution=PRIVATE_VALUES[2],
        profile=PRIVATE_VALUES[3],
    )
    with pytest.raises(ValueError) as raised:
        PreparedConnectorRouteChange(
            proposal=prepared.proposal,
            private_bindings=changed,
        )
    for private_value in (*PRIVATE_VALUES, changed.preflight):
        assert private_value not in str(raised.value)


def test_public_source_or_catalog_identity_change_rebinds_contract_target_and_proposal() -> None:
    baseline = _prepare_create(ConnectorRouteOperator(_Store()))
    changed_source = _prepare_create(
        ConnectorRouteOperator(_Store()),
        source_identity_fingerprint="3" * 64,
    )
    changed_catalog = _prepare_create(
        ConnectorRouteOperator(_Store()),
        catalog_identity_fingerprint="4" * 64,
    )

    assert (
        len(
            {
                baseline.proposal.contract_fingerprint,
                changed_source.proposal.contract_fingerprint,
                changed_catalog.proposal.contract_fingerprint,
            }
        )
        == 3
    )
    assert (
        len(
            {
                baseline.proposal.target.fingerprint,
                changed_source.proposal.target.fingerprint,
                changed_catalog.proposal.target.fingerprint,
            }
        )
        == 3
    )
    assert (
        len(
            {
                baseline.proposal.fingerprint,
                changed_source.proposal.fingerprint,
                changed_catalog.proposal.fingerprint,
            }
        )
        == 3
    )


def test_postgres_source_identity_fingerprint_is_exact_canonical_and_sensitive() -> None:
    payload = {
        "database": "tenant_alpha",
        "fingerprint_version": "m28-postgresql-source-identity-v1",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "user": "schemabridge_source_reader",
    }
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    observed = postgres_source_identity_fingerprint(
        server_address="127.0.0.1",
        server_port=5432,
        database="tenant_alpha",
        user="schemabridge_source_reader",
    )

    assert observed == expected
    changed = {
        postgres_source_identity_fingerprint(
            server_address="127.0.0.2",
            server_port=5432,
            database="tenant_alpha",
            user="schemabridge_source_reader",
        ),
        postgres_source_identity_fingerprint(
            server_address="127.0.0.1",
            server_port=5433,
            database="tenant_alpha",
            user="schemabridge_source_reader",
        ),
        postgres_source_identity_fingerprint(
            server_address="127.0.0.1",
            server_port=5432,
            database="tenant_bravo",
            user="schemabridge_source_reader",
        ),
        postgres_source_identity_fingerprint(
            server_address="127.0.0.1",
            server_port=5432,
            database="tenant_alpha",
            user="schemabridge_other_reader",
        ),
    }
    assert observed not in changed
    assert len(changed) == 4


def test_apply_uses_exact_cas_read_back_and_exact_replay_is_idempotent() -> None:
    store = _Store()
    operator = ConnectorRouteOperator(store)
    prepared = _prepare_create(operator)
    approval = _approve(operator, prepared)

    first = operator.apply(
        prepared,
        approval,
        expected_proposal_fingerprint=prepared.proposal.fingerprint,
        expected_approval_fingerprint=approval.approval_fingerprint,
    )
    replay = operator.apply(
        prepared,
        approval,
        expected_proposal_fingerprint=prepared.proposal.fingerprint,
        expected_approval_fingerprint=approval.approval_fingerprint,
    )

    assert replay == first
    assert first.head_revision == 1
    assert first.target_fingerprint == prepared.proposal.target.fingerprint
    assert len(store.writes) == 2
    assert store.writes[0] == store.writes[1]
    assert store.current is not None
    assert store.current.target == prepared.proposal.target
    assert store.writes[0].source_identity_fingerprint == SOURCE_IDENTITY_FINGERPRINT
    assert store.writes[0].catalog_identity_fingerprint == CATALOG_IDENTITY_FINGERPRINT


def test_changed_payload_reusing_idempotency_identity_is_a_sanitized_conflict() -> None:
    store = _Store()
    operator = ConnectorRouteOperator(store)
    baseline = _prepare_create(operator)
    changed = _prepare_create(
        operator,
        bindings=ConnectorPrivateBindings(
            preflight="vault:preflight:changed-echo",
            catalog=PRIVATE_VALUES[1],
            execution=PRIVATE_VALUES[2],
            profile=PRIVATE_VALUES[3],
        ),
    )
    baseline_approval = _approve(operator, baseline)
    changed_approval = _approve(operator, changed)
    operator.apply(
        baseline,
        baseline_approval,
        expected_proposal_fingerprint=baseline.proposal.fingerprint,
        expected_approval_fingerprint=baseline_approval.approval_fingerprint,
    )

    with pytest.raises(ConnectorRouteOperatorError) as raised:
        operator.apply(
            changed,
            changed_approval,
            expected_proposal_fingerprint=changed.proposal.fingerprint,
            expected_approval_fingerprint=changed_approval.approval_fingerprint,
        )

    assert raised.value.code is ConnectorRouteOperatorErrorCode.IDEMPOTENCY_CONFLICT
    assert "vault:" not in str(raised.value)


def test_apply_rejects_missing_exact_read_back_after_database_outcome() -> None:
    store = _Store(persist_read_back=False)
    operator = ConnectorRouteOperator(store)
    prepared = _prepare_create(operator)
    approval = _approve(operator, prepared)

    with pytest.raises(ConnectorRouteOperatorError) as raised:
        operator.apply(
            prepared,
            approval,
            expected_proposal_fingerprint=prepared.proposal.fingerprint,
            expected_approval_fingerprint=approval.approval_fingerprint,
        )

    assert raised.value.code is ConnectorRouteOperatorErrorCode.INVALID_RESPONSE
    assert len(store.writes) == 1


def test_approval_requires_operation_specific_phrase_and_exact_fingerprint() -> None:
    operator = ConnectorRouteOperator(_Store())
    prepared = _prepare_create(operator)

    for expected_fingerprint, confirmation in (
        ("f" * 64, "CREATE CONNECTOR ROUTE"),
        (prepared.proposal.fingerprint, "ROTATE CONNECTOR ROUTE"),
    ):
        with pytest.raises(ConnectorRouteOperatorError) as raised:
            operator.approve(
                prepared.proposal,
                expected_proposal_fingerprint=expected_fingerprint,
                confirmation=confirmation,
                approved_by=ACTOR_ID,
            )
        assert raised.value.code is ConnectorRouteOperatorErrorCode.CONFIRMATION_MISMATCH
    assert _Store().writes == []


def test_disable_derives_exact_current_contract_and_carries_no_bindings() -> None:
    store = _Store()
    operator = ConnectorRouteOperator(store)
    created = _prepare_create(operator)
    creation_approval = _approve(operator, created)
    operator.apply(
        created,
        creation_approval,
        expected_proposal_fingerprint=created.proposal.fingerprint,
        expected_approval_fingerprint=creation_approval.approval_fingerprint,
    )

    disabled = operator.prepare(
        workspace_id=WORKSPACE_ID,
        connection_id=CONNECTION_ID,
        operation=ConnectorRouteOperation.DISABLE,
        expected_head_revision=1,
        contract_version=None,
        route_revision=None,
        expected_reader=None,
        source_identity_fingerprint=None,
        catalog_identity_fingerprint=None,
        type_contract_version=None,
        type_contract_fingerprint=None,
        cost_budget=None,
        private_bindings=None,
        proposed_by=ACTOR_ID,
        idempotency_key="connector-route-disable-request-0001",
    )

    assert disabled.private_bindings is None
    assert disabled.proposal.private_bindings_fingerprint is None
    assert disabled.proposal.target == created.proposal.target
    assert disabled.proposal.target.source_identity_fingerprint == SOURCE_IDENTITY_FINGERPRINT
    assert disabled.proposal.target.catalog_identity_fingerprint == CATALOG_IDENTITY_FINGERPRINT
    assert store.writes == [store.writes[0]]
