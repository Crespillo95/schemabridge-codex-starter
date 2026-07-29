from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, cast

import pytest

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
    ResolvedPostgresSecret,
)
from schemabridge.adapters.connectors.postgres_profile_routing import (
    PostgresSemanticProfileConnectorRouteReader,
    ProfilePostgresConnectorRoute,
)
from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.adapters.postgres.relationships import (
    PsycopgRelationshipEvidenceAdapter,
)
from schemabridge.adapters.postgres.routed_relationships import (
    RoutedSemanticJoinProfileEvidenceFactory,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileRouteContext,
    SemanticJoinProfileSourceCancelled,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import SourceDialect
from schemabridge.domain.joins import JoinProposal, RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import (
    SemanticJoinProfileProposal,
    SemanticJoinProfileTargetRef,
)

WORKSPACE_ID = "workspace-profile-routing"
CONNECTION_ID = CatalogConnectionId("warehouse-primary")
JOB_ID = f"profile_job_{'a' * 64}"
WORKER_ID = "profile-worker-routing"
LEASE_CAPABILITY = "profile-worker-lease-capability-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ROUTE_REVISION = 3
CONTRACT_VERSION = 5
ROUTE_FINGERPRINT = "b" * 64
TARGET_FINGERPRINT = "c" * 64
SOURCE_IDENTITY_FINGERPRINT = "d" * 64
EXPECTED_READER = "schemabridge_reader"
PRIVATE_REFERENCE = "profile.route.private-v3"
PROVIDER_SECRET_VERSION = 83
PRIVATE_DSN = (
    "postgresql://schemabridge_reader:synthetic-password@source.example.test:5432/analytics"
)
CONTROL_DSN = "postgresql://private-control.invalid/control"


def _proposal() -> JoinProposal:
    return build_north_star_join_proposals()[0]


def _bound_proposal(
    connection_id: CatalogConnectionId = CONNECTION_ID,
) -> SemanticJoinProfileProposal:
    return SemanticJoinProfileProposal(
        connection_id=connection_id,
        proposal=_proposal(),
    )


def _profile() -> RelationshipProfile:
    return RelationshipProfile(
        left_row_count=7,
        right_row_count=9,
        left_null_count=0,
        right_null_count=1,
        left_invalid_count=0,
        right_invalid_count=2,
        left_distinct_valid=7,
        right_distinct_valid=5,
        matching_distinct_keys=5,
        left_max_multiplicity=1,
        right_max_multiplicity=2,
        reader_user=EXPECTED_READER,
        transaction_read_only=True,
        statement_timeout_ms=5_000,
    )


def _context() -> SemanticJoinProfileRouteContext:
    return SemanticJoinProfileRouteContext(
        job_id=JOB_ID,
        workspace_id=WORKSPACE_ID,
        worker_id=WORKER_ID,
        lease_capability=LEASE_CAPABILITY,
        fencing_token=2,
        execution_target=SemanticJoinProfileTargetRef(
            workspace_id=WORKSPACE_ID,
            connection_id=CONNECTION_ID,
            route_revision=ROUTE_REVISION,
            route_fingerprint=ROUTE_FINGERPRINT,
            target_fingerprint=TARGET_FINGERPRINT,
        ),
        connector_contract_version=CONTRACT_VERSION,
    )


def _route() -> ProfilePostgresConnectorRoute:
    return ProfilePostgresConnectorRoute(
        dialect=SourceDialect.POSTGRESQL,
        expected_reader=EXPECTED_READER,
        source_identity_fingerprint=SOURCE_IDENTITY_FINGERPRINT,
        secret_reference=OpaqueConnectorSecretRef(
            PRIVATE_REFERENCE,
            provider_secret_version=PROVIDER_SECRET_VERSION,
        ),
    )


@dataclass
class _RouteReader:
    route: ProfilePostgresConnectorRoute | None = None
    error: ConnectorTargetError | None = None
    calls: list[SemanticJoinProfileRouteContext] = field(default_factory=list)

    def load(
        self,
        context: SemanticJoinProfileRouteContext,
    ) -> ProfilePostgresConnectorRoute:
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        assert self.route is not None
        return self.route


@dataclass
class _SecretResolver:
    secret: ResolvedPostgresSecret | None = None
    error: ConnectorSecretResolutionError | None = None
    calls: list[tuple[OpaqueConnectorSecretRef, SourceDialect, str]] = field(default_factory=list)

    def resolve_postgres_route(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        dialect: SourceDialect,
        expected_reader: str,
    ) -> ResolvedPostgresSecret:
        assert reference.provider_secret_version == PROVIDER_SECRET_VERSION
        self.calls.append((reference, dialect, expected_reader))
        if self.error is not None:
            raise self.error
        assert self.secret is not None
        return self.secret


def _factory(
    route_reader: _RouteReader,
    secret_resolver: _SecretResolver,
) -> RoutedSemanticJoinProfileEvidenceFactory:
    return RoutedSemanticJoinProfileEvidenceFactory(
        route_reader=route_reader,
        secret_resolver=secret_resolver,
    )


def test_valid_claim_resolves_exact_route_and_profiles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    route_reader = _RouteReader(route=_route())
    secret_resolver = _SecretResolver(
        secret=ResolvedPostgresSecret(
            dsn=PRIVATE_DSN,
            expected_reader=EXPECTED_READER,
            dialect=SourceDialect.POSTGRESQL,
        )
    )
    source_calls: list[tuple[str, JoinProposal, Callable[[], bool] | None]] = []

    def profile(
        adapter: PsycopgRelationshipEvidenceAdapter,
        proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        source_calls.append((adapter._dsn, proposal, should_continue))
        assert adapter._expected_user == EXPECTED_READER
        assert adapter._expected_source_identity_fingerprint == SOURCE_IDENTITY_FINGERPRINT
        assert adapter._statement_timeout_ms == 5_000
        assert should_continue is not None and should_continue()
        return _profile()

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", profile)
    factory = _factory(route_reader, secret_resolver)
    evidence = factory.for_claim(context, should_continue=lambda: True)

    observed = evidence.profile_bound(_bound_proposal())

    assert observed == _profile()
    assert route_reader.calls == [context]
    assert len(secret_resolver.calls) == 1
    reference, dialect, reader = secret_resolver.calls[0]
    assert reference.value == PRIVATE_REFERENCE
    assert dialect is SourceDialect.POSTGRESQL
    assert reader == EXPECTED_READER
    assert len(source_calls) == 1
    assert source_calls[0][0] == PRIVATE_DSN
    rendered = f"{context!r} {factory!r} {evidence!r}"
    assert LEASE_CAPABILITY not in rendered
    assert PRIVATE_REFERENCE not in rendered
    assert PRIVATE_DSN not in rendered


def test_profile_source_identity_mismatch_is_sanitized_and_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_reader = _RouteReader(route=_route())
    secret_resolver = _SecretResolver(
        secret=ResolvedPostgresSecret(
            dsn=PRIVATE_DSN,
            expected_reader=EXPECTED_READER,
            dialect=SourceDialect.POSTGRESQL,
        )
    )

    def mismatch(
        _adapter: PsycopgRelationshipEvidenceAdapter,
        _proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        del should_continue
        raise PostgresSourceIdentityMismatchError("silently_retargeted_database must stay private")

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", mismatch)
    evidence = _factory(route_reader, secret_resolver).for_claim(
        _context(),
        should_continue=lambda: True,
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        evidence.profile_bound(_bound_proposal())

    assert raised.value.code is RelationshipErrorCode.EVIDENCE_NOT_ALLOWED
    assert str(raised.value) == "semantic profile connector route is unavailable"
    assert raised.value.__cause__ is None
    assert "silently_retargeted_database" not in str(raised.value)


def test_proposal_connection_mismatch_performs_zero_route_or_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_reader = _RouteReader(route=_route())
    secret_resolver = _SecretResolver()
    source_calls = 0

    def profile(
        _adapter: PsycopgRelationshipEvidenceAdapter,
        _proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        del should_continue
        nonlocal source_calls
        source_calls += 1
        return _profile()

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", profile)
    evidence = _factory(route_reader, secret_resolver).for_claim(
        _context(),
        should_continue=lambda: True,
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        evidence.profile_bound(_bound_proposal(CatalogConnectionId("warehouse-other")))

    assert raised.value.code is RelationshipErrorCode.EVIDENCE_NOT_ALLOWED
    assert route_reader.calls == []
    assert secret_resolver.calls == []
    assert source_calls == 0


def test_cancellation_before_route_performs_zero_private_or_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_reader = _RouteReader(route=_route())
    secret_resolver = _SecretResolver()
    source_calls = 0

    def profile(
        _adapter: PsycopgRelationshipEvidenceAdapter,
        _proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        del should_continue
        nonlocal source_calls
        source_calls += 1
        return _profile()

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", profile)
    evidence = _factory(route_reader, secret_resolver).for_claim(
        _context(),
        should_continue=lambda: False,
    )

    with pytest.raises(SemanticJoinProfileSourceCancelled):
        evidence.profile_bound(_bound_proposal())

    assert route_reader.calls == []
    assert secret_resolver.calls == []
    assert source_calls == 0


def test_route_rotation_is_terminal_and_performs_zero_secret_or_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_reader = _RouteReader(
        error=ConnectorTargetError(
            ConnectorTargetErrorCode.ROUTE_STALE,
            "connector route is stale",
        )
    )
    secret_resolver = _SecretResolver()
    source_calls = 0

    def profile(
        _adapter: PsycopgRelationshipEvidenceAdapter,
        _proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        del should_continue
        nonlocal source_calls
        source_calls += 1
        return _profile()

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", profile)
    evidence = _factory(route_reader, secret_resolver).for_claim(
        _context(),
        should_continue=lambda: True,
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        evidence.profile_bound(_bound_proposal())

    assert raised.value.code is RelationshipErrorCode.EVIDENCE_NOT_ALLOWED
    assert route_reader.calls == [_context()]
    assert secret_resolver.calls == []
    assert source_calls == 0


def test_secret_outage_is_retryable_without_source_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_reader = _RouteReader(route=_route())
    secret_resolver = _SecretResolver(
        error=ConnectorSecretResolutionError(
            ConnectorSecretErrorCode.UNAVAILABLE,
            "connector secret is unavailable",
        )
    )
    source_calls = 0

    def profile(
        _adapter: PsycopgRelationshipEvidenceAdapter,
        _proposal: JoinProposal,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> RelationshipProfile:
        del should_continue
        nonlocal source_calls
        source_calls += 1
        return _profile()

    monkeypatch.setattr(PsycopgRelationshipEvidenceAdapter, "profile", profile)
    evidence = _factory(route_reader, secret_resolver).for_claim(
        _context(),
        should_continue=lambda: True,
    )

    with pytest.raises(RelationshipWorkflowError) as raised:
        evidence.profile_bound(_bound_proposal())

    assert raised.value.code is RelationshipErrorCode.EVIDENCE_UNAVAILABLE
    assert route_reader.calls == [_context()]
    assert len(secret_resolver.calls) == 1
    assert source_calls == 0


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass
class _Cursor:
    rows: Sequence[tuple[object, ...]]

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return self.rows


@dataclass
class _Connection:
    rows: Sequence[tuple[object, ...]]
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)
    transaction_count: int = 0

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        text = query if isinstance(query, str) else cast(_Composable, query).as_string()
        normalized = " ".join(text.split())
        self.statements.append((normalized, params))
        if normalized == "SET TRANSACTION READ ONLY":
            return _Cursor(())
        return _Cursor(self.rows)

    @contextmanager
    def transaction(self) -> Iterator[object]:
        self.transaction_count += 1
        yield object()


@dataclass
class _Provider:
    connection_value: _Connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


def _route_row() -> tuple[object, ...]:
    context = _context()
    return (
        context.workspace_id,
        context.job_id,
        context.execution_target.connection_id.root,
        context.connector_contract_version,
        context.execution_target.route_revision,
        context.execution_target.target_fingerprint,
        SourceDialect.POSTGRESQL.value,
        EXPECTED_READER,
        SOURCE_IDENTITY_FINGERPRINT,
        PRIVATE_REFERENCE,
        PROVIDER_SECRET_VERSION,
    )


def test_postgres_profile_route_reader_uses_exact_lease_capability_read_only() -> None:
    context = _context()
    connection = _Connection((_route_row(),))
    reader = PostgresSemanticProfileConnectorRouteReader(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    route = reader.load(context)

    assert route.dialect is SourceDialect.POSTGRESQL
    assert route.expected_reader == EXPECTED_READER
    assert route.source_identity_fingerprint == SOURCE_IDENTITY_FINGERPRINT
    assert route.secret_reference.value == PRIVATE_REFERENCE
    assert route.secret_reference.provider_secret_version == PROVIDER_SECRET_VERSION
    assert connection.transaction_count == 1
    assert connection.statements[0] == ("SET TRANSACTION READ ONLY", None)
    query, params = connection.statements[1]
    assert "load_owned_profile_connector_route_v2" in query
    assert params == (
        context.workspace_id,
        context.job_id,
        context.worker_id,
        LEASE_CAPABILITY,
        context.fencing_token,
        context.execution_target.connection_id.root,
        context.connector_contract_version,
        context.execution_target.route_revision,
        context.execution_target.target_fingerprint,
    )
    rendered = f"{context!r} {reader!r} {route!r}"
    assert LEASE_CAPABILITY not in rendered
    assert CONTROL_DSN not in rendered
    assert PRIVATE_REFERENCE not in rendered


def test_postgres_profile_route_reader_reports_missing_rotated_route_as_stale() -> None:
    connection = _Connection(())
    reader = PostgresSemanticProfileConnectorRouteReader(
        dsn=CONTROL_DSN,
        connection_provider=_Provider(connection),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectorTargetError) as raised:
        reader.load(_context())

    assert raised.value.code is ConnectorTargetErrorCode.ROUTE_STALE
    assert connection.transaction_count == 1
