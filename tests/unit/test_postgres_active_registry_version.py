"""Fail-closed API reader tests for one exact active registry publication."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

import pytest
from tests.unit.test_registry_publication_jobs import (
    CAPABILITY_A,
    CAPABILITY_B,
    NOW,
    _authorization,
    _job,
    _receipt,
)
from tests.unit.test_registry_publication_v2 import _proposal

from schemabridge.adapters.control_plane.postgres_active_registry_version import (
    PostgresActiveRegistryVersionReader,
)
from schemabridge.adapters.storage.postgres import ControlConnectionProvider
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_publication import (
    PublishableRegistryVersion,
    assemble_publishable_registry_version,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJob,
    authorize_registry_publication_job,
    complete_registry_publication_job,
    lease_registry_publication_job,
    record_registry_publication_candidate,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
    semantic_registry_decision_ids,
)

_SECRET_DSN = "postgresql://schemabridge_api:do-not-render@control.example/control"


@dataclass(slots=True)
class _FakeConnection:
    rows: tuple[tuple[object, ...], ...]
    failure: Exception | None = None
    statement: str | None = None
    params: tuple[object, ...] | None = None
    executions: int = 0

    def execute(self, statement: object, params: tuple[object, ...]) -> _FakeConnection:
        self.executions += 1
        self.statement = statement.as_string()  # type: ignore[attr-defined]
        self.params = params
        if self.failure is not None:
            raise self.failure
        return self

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self.rows


@dataclass(slots=True)
class _FakeProvider:
    value: _FakeConnection

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield self.value


@dataclass(slots=True)
class _Pointers:
    values: list[ActiveRegistryPointer | Exception | None]
    calls: list[SemanticRegistryScope] = field(default_factory=list)

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        self.calls.append(scope)
        selected = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        if isinstance(selected, Exception):
            raise selected
        return selected


def test_reader_returns_strict_active_snapshot_from_exact_activation_ready_job() -> None:
    job = _activation_ready_job()
    pointer = _pointer(job)
    connection = _FakeConnection((_row(job),))
    pointers = _Pointers([pointer])
    reader = _reader(pointers, connection)

    result = reader.load_version(job.scope, job.proposal.target_registry_version)
    assert job.authorization is not None
    assert job.candidate is not None

    assert result.trust is RegistryVersionTrust.STRICT
    assert result.publication_approval_id == job.authorization.id
    assert result.snapshot.registry == job.candidate.registry
    assert result.snapshot.activation_generation == pointer.generation
    assert result.snapshot.active_pointer_fingerprint == registry_projection_fingerprint(pointer)
    assert pointers.calls == [job.scope, job.scope]
    assert connection.params == (
        job.id,
        job.scope.workspace_id,
        job.scope.catalog_scope,
        job.scope.registry_id,
        job.proposal.target_registry_version,
    )
    assert connection.statement is not None
    assert "status = 'activation_ready'" in connection.statement
    assert "LIMIT 2" in connection.statement
    assert _SECRET_DSN not in repr(reader)


def test_reader_rejects_non_active_version_before_publication_payload_read() -> None:
    job = _activation_ready_job()
    inactive = _pointer(job, version=job.proposal.target_registry_version + 1)
    connection = _FakeConnection((_row(job),))
    reader = _reader(_Pointers([inactive]), connection)

    with pytest.raises(RegistryControlError) as raised:
        reader.load_version(job.scope, job.proposal.target_registry_version)

    assert raised.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE
    assert connection.executions == 0


@pytest.mark.parametrize(
    ("row_count", "expected_code"),
    [
        (0, RegistryControlErrorCode.VERSION_UNAVAILABLE),
        (2, RegistryControlErrorCode.VERSION_INVALID),
    ],
)
def test_reader_requires_exactly_one_scoped_job(
    row_count: int,
    expected_code: RegistryControlErrorCode,
) -> None:
    job = _activation_ready_job()
    selected = tuple(_row(job) for _ in range(row_count))
    reader = _reader(_Pointers([_pointer(job)]), _FakeConnection(selected))

    with pytest.raises(RegistryControlError) as raised:
        reader.load_version(job.scope, job.proposal.target_registry_version)

    assert raised.value.code is expected_code


def test_reader_rejects_denormalized_tenant_or_candidate_payload_tampering() -> None:
    job = _activation_ready_job()
    pointer = _pointer(job)
    tenant_row = list(_row(job))
    tenant_row[2] = "foreign-workspace"
    tenant_reader = _reader(_Pointers([pointer]), _FakeConnection((tuple(tenant_row),)))

    with pytest.raises(RegistryControlError) as tenant_error:
        tenant_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert tenant_error.value.code is RegistryControlErrorCode.VERSION_INVALID

    forged_payload = job.model_dump(mode="json")
    forged_payload["candidate"]["fingerprint"] = "b" * 64
    payload_row = list(_row(job))
    payload_row[0] = forged_payload
    payload_reader = _reader(_Pointers([pointer]), _FakeConnection((tuple(payload_row),)))

    with pytest.raises(RegistryControlError) as payload_error:
        payload_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert payload_error.value.code is RegistryControlErrorCode.VERSION_INVALID


def test_reader_requires_observed_authorization_to_equal_attempt_authorization() -> None:
    job = _activation_ready_job()
    forged_id = f"registry-authorization-{'f' * 64}"
    payload = job.model_dump(mode="json")
    payload["receipt"]["observed_authorization_id"] = forged_id
    row = list(_row(job))
    row[0] = payload
    row[11] = forged_id
    reader = _reader(_Pointers([_pointer(job)]), _FakeConnection((tuple(row),)))

    with pytest.raises(RegistryControlError) as raised:
        reader.load_version(job.scope, job.proposal.target_registry_version)

    assert raised.value.code is RegistryControlErrorCode.VERSION_INVALID


def test_reader_rejects_pointer_fingerprint_mismatch_and_concurrent_pointer_drift() -> None:
    job = _activation_ready_job()
    mismatched = _pointer(job, registry_fingerprint="b" * 64)
    mismatch_reader = _reader(
        _Pointers([mismatched]),
        _FakeConnection((_row(job),)),
    )

    with pytest.raises(RegistryControlError) as mismatch_error:
        mismatch_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert mismatch_error.value.code is RegistryControlErrorCode.VERSION_INVALID

    original = _pointer(job)
    changed = _pointer(job, generation=original.generation + 1, suffix="d")
    drift_reader = _reader(
        _Pointers([original, changed]),
        _FakeConnection((_row(job),)),
    )

    with pytest.raises(RegistryControlError) as drift_error:
        drift_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert drift_error.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE


def test_reader_rejects_foreign_pointer_scope_without_querying_the_job() -> None:
    job = _activation_ready_job()
    foreign_scope = job.scope.model_copy(update={"workspace_id": "foreign-workspace"})
    foreign_pointer = _pointer(job, scope=foreign_scope)
    connection = _FakeConnection((_row(job),))
    reader = _reader(_Pointers([foreign_pointer]), connection)

    with pytest.raises(RegistryControlError) as raised:
        reader.load_version(job.scope, job.proposal.target_registry_version)

    assert raised.value.code is RegistryControlErrorCode.VERSION_INVALID
    assert connection.executions == 0


def test_reader_sanitizes_pointer_and_control_database_failures() -> None:
    job = _activation_ready_job()
    leaked = "postgresql://user:super-secret@private/control"
    pointer_reader = _reader(
        _Pointers([RuntimeError(leaked)]),
        _FakeConnection((_row(job),)),
    )
    with pytest.raises(RegistryControlError) as pointer_error:
        pointer_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert pointer_error.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE
    assert leaked not in str(pointer_error.value)

    database_reader = _reader(
        _Pointers([_pointer(job)]),
        _FakeConnection((), failure=RuntimeError(leaked)),
    )
    with pytest.raises(RegistryControlError) as database_error:
        database_reader.load_version(job.scope, job.proposal.target_registry_version)
    assert database_error.value.code is RegistryControlErrorCode.VERSION_UNAVAILABLE
    assert leaked not in str(database_error.value)


def _reader(
    pointers: _Pointers,
    connection: _FakeConnection,
) -> PostgresActiveRegistryVersionReader:
    provider = cast(ControlConnectionProvider, _FakeProvider(connection))
    return PostgresActiveRegistryVersionReader(
        _SECRET_DSN,
        pointers,
        connection_provider=provider,
    )


def _activation_ready_job() -> RegistryPublicationJob:
    proposal = _proposal()
    candidate = assemble_publishable_registry_version(proposal, base=None)
    preparing = lease_registry_publication_job(
        _job(proposal),
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        acquired_at=NOW,
        lease_duration=timedelta(seconds=60),
    )
    awaiting = record_registry_publication_candidate(
        preparing,
        candidate,
        worker_id="publisher-worker-a",
        lease_capability=CAPABILITY_A,
        fencing_token=1,
        completed_at=NOW + timedelta(seconds=1),
    )
    authorization = _authorization(candidate, at=NOW + timedelta(seconds=2))
    approved = authorize_registry_publication_job(
        awaiting,
        authorization,
        authorized_at=NOW + timedelta(seconds=2),
    )
    publishing = lease_registry_publication_job(
        approved,
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        acquired_at=NOW + timedelta(seconds=3),
        lease_duration=timedelta(seconds=60),
    )
    return complete_registry_publication_job(
        publishing,
        _receipt(candidate, authorization, at=NOW + timedelta(seconds=4)),
        worker_id="publisher-worker-b",
        lease_capability=CAPABILITY_B,
        fencing_token=2,
        completed_at=NOW + timedelta(seconds=4),
    )


def _pointer(
    job: RegistryPublicationJob,
    *,
    scope: SemanticRegistryScope | None = None,
    version: int | None = None,
    registry_fingerprint: str | None = None,
    generation: int = 7,
    suffix: str = "c",
) -> ActiveRegistryPointer:
    candidate = job.candidate
    assert isinstance(candidate, PublishableRegistryVersion)
    selected_scope = scope or job.scope
    selected_version = version or candidate.registry.version
    return ActiveRegistryPointer(
        scope=selected_scope,
        generation=generation,
        registry_version=selected_version,
        registry_fingerprint=registry_fingerprint or candidate.registry.fingerprint,
        registry_target=datahub_registry_document_urn(selected_scope, selected_version),
        transition_id=f"registry-transition-v1-{suffix * 64}",
        activated_by="registry-operator",
        activated_at=NOW,
        decision_ids=semantic_registry_decision_ids(candidate.registry),
    )


def _row(
    job: RegistryPublicationJob,
) -> tuple[object, ...]:
    candidate = job.candidate
    authorization = job.authorization
    receipt = job.receipt
    assert candidate is not None
    assert authorization is not None
    assert receipt is not None
    return (
        job.model_dump(mode="json"),
        job.id,
        job.scope.workspace_id,
        job.scope.catalog_scope,
        job.scope.registry_id,
        job.proposal.target_registry_version,
        job.proposal.id,
        job.proposal.fingerprint,
        job.status.value,
        candidate.fingerprint,
        authorization.id,
        receipt.observed_authorization_id,
    )
