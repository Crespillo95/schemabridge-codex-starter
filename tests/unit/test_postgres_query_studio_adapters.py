from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from schemabridge.adapters.catalog.postgres_governed_search import (
    PostgresGovernedBindingFactsSearch,
)
from schemabridge.adapters.catalog.postgres_physical_discovery import (
    PostgresPhysicalFieldDiscovery,
)
from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
    PostgresQueryStudioAiControl,
)
from schemabridge.adapters.control_plane.postgres_query_studio_policy import (
    PostgresTenantAiPolicyOperator,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.application.ports.query_studio_ai_control import (
    AiAdmissionOutcome,
    AiAttemptReservationRequest,
    AiAttemptSettlementRequest,
    AiAttemptStatus,
    AiSettlementOutcome,
    TenantAiPolicyConfirmation,
    TenantAiPolicyWrite,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    GovernedBindingFactsRequest,
    PhysicalDiscoveryCursor,
    PhysicalDiscoveryStatus,
    PhysicalFieldDiscoveryRequest,
    ProviderStage,
    SearchSignalCode,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
EU_ENDPOINT_ORIGIN_FINGERPRINT = "83203af93d5b1b9a2b7ab344441b99f7e19d980c881ba78de44e16073bd199c7"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CURSOR_KEY = b"physical-discovery-cursor-key-v1-with-diversity-123"


@dataclass
class _Cursor:
    rows: Sequence[tuple[Any, ...]]

    def fetchone(self) -> tuple[Any, ...] | None:
        return None if not self.rows else self.rows[0]

    def fetchall(self) -> Sequence[tuple[Any, ...]]:
        return self.rows


@dataclass
class _Connection:
    batches: list[Sequence[tuple[Any, ...]]]
    statements: list[tuple[object, Sequence[object] | None]] = field(default_factory=list)

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        self.statements.append((query, params))
        return _Cursor(self.batches.pop(0))

    @contextmanager
    def transaction(self) -> Iterator[object]:
        yield object()


@dataclass
class _Provider:
    connection_value: _Connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


def _scope() -> SemanticRegistryScope:
    return SemanticRegistryScope(
        workspace_id="workspace-alpha",
        catalog_scope="catalog.prod",
        registry_id="registry_alpha",
    )


def _scope_row() -> tuple[Any, ...]:
    return (1, 2, SHA_A, "transition_alpha", SHA_B, 3, 2, SHA_C, SHA_A, 1)


def _binding_row() -> tuple[Any, ...]:
    return (
        "Customer.customer_id",
        "decision-alpha",
        4,
        "binding_customer_id",
        SHA_B,
        SHA_C,
        "approved",
        "connection_alpha",
        8,
        SHA_A,
        SHA_B,
        "urn:synthetic:customers",
        "public.customers",
        SHA_C,
        SHA_A,
        ["customer_id"],
        "customer_id",
        "varchar",
        "string",
        False,
        True,
        "Stable customer identifier",
        ["identifier"],
        ["Customer Identifier"],
        SHA_B,
        SHA_C,
        SHA_A,
        1,
        2,
        SHA_A,
        "transition_alpha",
        SHA_B,
        3,
        2,
        SHA_C,
        SHA_A,
        False,
        True,
        True,
        False,
        False,
        True,
        False,
        1_800,
    )


def test_governed_search_maps_only_bounded_current_facts() -> None:
    connection = _Connection(batches=[[_scope_row()], [_binding_row()], [_scope_row()]])
    adapter = PostgresGovernedBindingFactsSearch(
        dsn="postgresql://not-used.invalid/control",
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )
    request = GovernedBindingFactsRequest(
        scope=_scope(),
        query=DescriptionQuery("customer_id"),
        page_size=20,
    )

    page = adapter.search(request)

    assert page.scope.pointer_generation == 1
    assert page.scope.evidence_head_revision == 3
    assert page.request_fingerprint == request.request_fingerprint
    assert page.binding_facts_fingerprint == request.request_fingerprint
    assert page.rows_read == 1
    assert page.next_key is None
    assert page.items[0].logical_field.root == "Customer.customer_id"
    assert page.items[0].physical_field.root == "public.customers.customer_id"
    assert page.items[0].signals.total == 1_800
    assert {signal.code for signal in page.items[0].signals.signals} == {
        SearchSignalCode.EXACT_PHYSICAL_FIELD,
        SearchSignalCode.PHYSICAL_NAME_OVERLAP,
        SearchSignalCode.DEFINITION_OVERLAP,
    }
    search_params = connection.statements[-2][1]
    assert search_params is not None
    assert search_params[12] == "customer_id"
    assert search_params[13] == []
    assert search_params[14] is False


def test_ai_policy_and_attempt_rows_are_sanitized_and_typed() -> None:
    policy_row = (
        2,
        True,
        True,
        "gpt-5-nano-2025-08-07",
        "eu",
        SHA_A,
        30,
        20_000,
        5_000,
        2,
        60,
        2_592_000,
        NOW,
    )
    reservation_id = "air_" + SHA_B
    audit_id = "aia_" + SHA_C
    lease_expires = NOW + timedelta(seconds=60)
    connection = _Connection(
        batches=[
            [policy_row],
            [
                (
                    "reserved",
                    False,
                    reservation_id,
                    "reserved",
                    9,
                    lease_expires,
                    2,
                    "gpt-5-nano-2025-08-07",
                    "eu",
                    SHA_A,
                )
            ],
            [
                (
                    reservation_id,
                    False,
                    "settled",
                    "succeeded",
                    8,
                    3,
                    NOW,
                    audit_id,
                )
            ],
            [(0,)],
        ]
    )
    adapter = PostgresQueryStudioAiControl(
        dsn="postgresql://not-used.invalid/control",
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )
    capability = "opaque-capability-0123456789abcdef"
    reservation_request = AiAttemptReservationRequest(
        workspace_id="workspace-alpha",
        request_id="request-alpha",
        actor_digest=SHA_A,
        stage=ProviderStage.EXPANSION,
        attempt_number=1,
        idempotency_digest=SHA_B,
        request_fingerprint=SHA_C,
        semantic_scope_fingerprint=SHA_A,
        semantic_payload_fingerprint=SHA_B,
        configuration_fingerprint=SHA_A,
        estimated_input_tokens=10,
        estimated_output_tokens=4,
        lease_capability=capability,
    )

    policy = adapter.load_policy("workspace-alpha")
    reserved = adapter.reserve(reservation_request)
    settled = adapter.settle(
        AiAttemptSettlementRequest(
            workspace_id="workspace-alpha",
            reservation_id=reservation_id,
            fencing_token=9,
            outcome=AiSettlementOutcome.SUCCEEDED,
            reserved_input_tokens=10,
            reserved_output_tokens=4,
            observed_input_tokens=8,
            observed_output_tokens=3,
            duration_ms=120,
            lease_capability=capability,
        )
    )

    assert policy is not None
    assert policy.model_snapshot == "gpt-5-nano-2025-08-07"
    assert reserved.outcome is AiAdmissionOutcome.RESERVED
    assert reserved.status is AiAttemptStatus.RESERVED
    assert settled.outcome is AiSettlementOutcome.SUCCEEDED
    assert settled.charged_input_tokens == 8
    assert adapter.expire("workspace-alpha") == 0
    assert capability not in repr(reservation_request)


def _physical_row(
    *,
    asset_key: str,
    field_key: str,
    asset_id: str,
    qualified_name: str,
    field_name: str,
    score: int,
) -> tuple[Any, ...]:
    return (
        "connection_alpha",
        8,
        SHA_A,
        asset_key,
        asset_id,
        qualified_name,
        SHA_B,
        field_key,
        [field_name],
        field_name,
        "varchar",
        "Synthetic field definition",
        SHA_C,
        SHA_A,
        score,
    )


def test_physical_discovery_is_global_signed_bounded_and_non_executable() -> None:
    first_row = _physical_row(
        asset_key="1" * 64,
        field_key="2" * 64,
        asset_id="urn:synthetic:first",
        qualified_name="public.first",
        field_name="contract_id",
        score=1_900,
    )
    second_row = _physical_row(
        asset_key="3" * 64,
        field_key="4" * 64,
        asset_id="urn:synthetic:second",
        qualified_name="public.second",
        field_name="gf_contract_id",
        score=1_400,
    )
    scope_row = (SHA_A, 2, 12, 37)
    connection = _Connection(
        batches=[
            [scope_row],
            [first_row, second_row],
            [scope_row],
            [scope_row],
            [scope_row],
            [second_row],
            [scope_row],
        ]
    )
    adapter = PostgresPhysicalFieldDiscovery.from_signing_key(
        dsn="postgresql://secret:not-shown@not-used.invalid/control",
        cursor_signing_key=CURSOR_KEY,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    request = PhysicalFieldDiscoveryRequest(
        scope=_scope(),
        query=DescriptionQuery("contract"),
        page_size=1,
    )

    first = adapter.search(request)
    assert len(first.items) == 1
    assert first.items[0].status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
    assert not hasattr(first.items[0], "candidate_id")
    assert first.items[0].locator.asset.workspace_id == "workspace-alpha"
    assert first.next_cursor is not None
    assert "workspace-alpha" not in first.next_cursor.root
    assert "secret" not in repr(adapter)
    cardinality = adapter.inspect_cardinality(request.scope)
    assert (
        cardinality.connection_count,
        cardinality.asset_count,
        cardinality.field_count,
    ) == (2, 12, 37)

    continued = adapter.search(
        PhysicalFieldDiscoveryRequest(
            scope=request.scope,
            query=request.query,
            page_size=1,
            cursor=first.next_cursor,
        )
    )
    assert [item.locator.asset.asset_id.root for item in continued.items] == [
        "urn:synthetic:second"
    ]
    assert continued.next_cursor is None
    continuation_params = connection.statements[-2][1]
    assert continuation_params is not None
    assert continuation_params[-4:] == (
        1_900,
        "connection_alpha",
        "1" * 64,
        "2" * 64,
    )

    tampered = PhysicalDiscoveryCursor(
        first.next_cursor.root[:-1] + ("A" if first.next_cursor.root[-1] != "A" else "B")
    )
    with pytest.raises(QueryStudioPortError) as captured:
        adapter.search(
            PhysicalFieldDiscoveryRequest(
                scope=request.scope,
                query=request.query,
                page_size=1,
                cursor=tampered,
            )
        )
    assert captured.value.code is QueryStudioPortErrorCode.INVALID_RESPONSE


def _operator_policy_row(*, version: int, enabled: bool) -> tuple[Any, ...]:
    return (
        "workspace-alpha",
        version,
        enabled,
        enabled,
        SHA_A if enabled else None,
        NOW if enabled else None,
        "gpt-5-nano-2025-08-07",
        "eu",
        EU_ENDPOINT_ORIGIN_FINGERPRINT,
        SHA_C,
        20,
        100_000,
        20_000,
        2,
        60,
        2_592_000,
        "platform-admin",
        NOW,
    )


def test_tenant_ai_policy_adapter_uses_migrator_functions_and_exact_readback() -> None:
    connection = _Connection(
        batches=[
            [_operator_policy_row(version=1, enabled=False)],
            [("workspace-alpha", 2)],
            [_operator_policy_row(version=2, enabled=True)],
        ]
    )
    adapter = PostgresTenantAiPolicyOperator(
        dsn="postgresql://migrator:secret@not-used.invalid/control",
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    inspected = adapter.inspect("workspace-alpha")
    applied = adapter.apply(
        TenantAiPolicyWrite(
            workspace_id="workspace-alpha",
            expected_version=1,
            external_ai_enabled=True,
            provider_governance_accepted=True,
            provider_governance_fingerprint=SHA_A,
            model_snapshot="gpt-5-nano-2025-08-07",
            endpoint_region="eu",
            endpoint_origin_fingerprint=EU_ENDPOINT_ORIGIN_FINGERPRINT,
            configuration_fingerprint=SHA_C,
            requests_per_minute=20,
            daily_input_token_limit=100_000,
            daily_output_token_limit=20_000,
            concurrent_attempt_limit=2,
            reservation_lease_seconds=60,
            audit_retention_seconds=2_592_000,
            updated_by="platform-admin",
            confirmation=TenantAiPolicyConfirmation.APPLY,
        )
    )

    assert inspected is not None and inspected.version == 1
    assert applied.version == 2
    assert applied.external_ai_enabled is True
    apply_params = connection.statements[1][1]
    assert apply_params is not None
    assert apply_params[-1] == "APPLY TENANT AI POLICY"
    assert "secret" not in repr(adapter)
