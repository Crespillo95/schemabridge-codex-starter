"""Authoritative PostgreSQL registry pointer, outbox, and HMAC audit store."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import _ControlDatabase
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    ControlAuditChainVerification,
    RegistryActivationTransition,
    RegistryControlCommit,
    RegistryProjectionOutboxItem,
    RegistryProjectionOutboxStatus,
    RegistryProjectionOutcome,
    RegistryProjectionState,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(frozen=True, slots=True)
class PostgresRegistryControlStore:
    """CAS store whose transition, pointer, outbox, and audit commit atomically."""

    dsn: str = field(repr=False)
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    active_audit_key_version: str
    schema: str = "schemabridge_control"
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        database = _ControlDatabase(self.dsn, self.schema)
        if not self.audit_signing_keys:
            raise ValueError("control audit signing keys cannot be empty")
        if self.active_audit_key_version not in self.audit_signing_keys:
            raise ValueError("active control audit key version is unavailable")
        if any(
            not version.strip() or len(version) > 80 or len(key) < 32 or len(set(key)) < 8
            for version, key in self.audit_signing_keys.items()
        ):
            raise ValueError("control audit signing key configuration is invalid")
        object.__setattr__(self, "_database", database)

    @property
    def _db(self) -> _ControlDatabase:
        return self._database

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        table = self._db.table("registry_active_pointers")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT generation, registry_version, registry_fingerprint,
                               registry_target, transition_id, activated_by,
                               activated_at, decision_ids_json
                        FROM {table}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        """
                    ).format(table=table),
                    _scope_params(scope),
                ).fetchone()
            return None if row is None else _pointer_from_row(scope, row)
        except RegistryControlError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("active registry pointer read failed") from error

    def list_transitions(
        self,
        scope: SemanticRegistryScope,
        *,
        limit: int = 100,
    ) -> tuple[RegistryActivationTransition, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("registry transition limit must be between 1 and 1000")
        table = self._db.table("registry_activation_transitions")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT payload_json
                        FROM {table}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        ORDER BY generation
                        LIMIT %s
                        """
                    ).format(table=table),
                    (*_scope_params(scope), limit),
                ).fetchall()
            return tuple(RegistryActivationTransition.model_validate(row[0]) for row in rows)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("registry transition history read failed") from error

    def commit_transition(
        self,
        transition: RegistryActivationTransition,
        outbox: RegistryProjectionOutboxItem,
    ) -> RegistryControlCommit:
        _validate_commit_input(transition, outbox)
        transitions = self._db.table("registry_activation_transitions")
        pointers = self._db.table("registry_active_pointers")
        outboxes = self._db.table("registry_reconciliation_outbox")
        try:
            with self._db.connect() as connection:
                self._lock_workspace_audit(connection, transition.active_pointer.scope.workspace_id)
                replay = self._load_replayed_commit(connection, transition, outbox)
                if replay is not None:
                    return replay
                current = self._load_active_for_update(
                    connection,
                    transition.active_pointer.scope,
                )
                if current != transition.previous_pointer:
                    raise RegistryControlError(
                        RegistryControlErrorCode.CAS_CONFLICT,
                        "active semantic registry changed; prepare a new activation",
                    )
                self._insert_transition(connection, transitions, transition)
                self._write_pointer(connection, pointers, transition)
                self._insert_outbox(connection, outboxes, outbox)
                audit_hash = self._append_audit_event(
                    connection,
                    workspace_id=transition.active_pointer.scope.workspace_id,
                    operation=f"registry_{transition.approval.proposal.action.value}",
                    transition_id=transition.id,
                    event_id=_event_id("registry_transition", transition.id),
                    event_payload={
                        "transition": transition.model_dump(mode="json"),
                        "outbox": outbox.model_dump(mode="json"),
                    },
                    occurred_at=transition.committed_at,
                )
                return RegistryControlCommit(
                    transition=transition,
                    outbox=outbox,
                    audit_event_hash=audit_hash,
                )
        except RegistryControlError:
            raise
        except UniqueViolation as error:
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "active semantic registry changed; prepare a new activation",
            ) from error
        except psycopg.Error as error:
            raise _store_unavailable("registry activation commit failed") from error

    def load_pending_outbox(
        self,
        scope: SemanticRegistryScope,
    ) -> RegistryProjectionOutboxItem | None:
        table = self._db.table("registry_reconciliation_outbox")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT outbox_id, transition_id, desired_projection_json,
                               status, attempts, created_at, last_reason_code
                        FROM {table}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND status = 'pending'
                        ORDER BY generation DESC
                        LIMIT 1
                        """
                    ).format(table=table),
                    _scope_params(scope),
                ).fetchone()
            return None if row is None else _outbox_from_row(row)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("registry projection outbox read failed") from error

    def load_transition_outbox(
        self,
        scope: SemanticRegistryScope,
        transition_id: str,
    ) -> RegistryProjectionOutboxItem | None:
        table = self._db.table("registry_reconciliation_outbox")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT outbox_id, transition_id, desired_projection_json,
                               status, attempts, created_at, last_reason_code
                        FROM {table}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                          AND transition_id = %s
                        """
                    ).format(table=table),
                    (*_scope_params(scope), transition_id),
                ).fetchone()
            return None if row is None else _outbox_from_row(row)
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("registry transition outbox read failed") from error

    def has_audit_event(self, transition_id: str) -> bool:
        table = self._db.table("control_audit_events")
        try:
            with self._db.connect() as connection:
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM {table}
                            WHERE transition_id = %s
                              AND operation IN ('registry_activate', 'registry_rollback')
                        )
                        """
                    ).format(table=table),
                    (transition_id,),
                ).fetchone()
            if row is None or not isinstance(row[0], bool):
                raise ValueError("control audit existence response is invalid")
            return row[0]
        except (psycopg.Error, TypeError, ValueError) as error:
            raise _store_unavailable("control audit read failed") from error

    def record_projection_outcome(self, outcome: RegistryProjectionOutcome) -> None:
        outboxes = self._db.table("registry_reconciliation_outbox")
        try:
            with self._db.connect() as connection:
                self._lock_workspace_audit(connection, outcome.scope.workspace_id)
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT transition_id, workspace_id, catalog_scope, registry_id,
                               generation, status, desired_projection_json,
                               observed_projection_json, last_reason_code, attempts, created_at
                        FROM {outboxes}
                        WHERE outbox_id = %s
                        FOR UPDATE
                        """
                    ).format(outboxes=outboxes),
                    (outcome.outbox_id,),
                ).fetchone()
                if row is None:
                    raise RegistryControlError(
                        RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                        "registry projection outbox item was not found",
                    )
                _validate_outcome_row(row, outcome)
                current_status = RegistryProjectionOutboxStatus(str(row[5]))
                if current_status is not RegistryProjectionOutboxStatus.PENDING:
                    if _terminal_outcome_matches(row, outcome):
                        return
                    raise RegistryControlError(
                        RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                        "registry projection outbox was already closed differently",
                    )
                delivered_at = (
                    outcome.occurred_at
                    if outcome.status is RegistryProjectionOutboxStatus.DELIVERED
                    else None
                )
                updated = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {outboxes}
                        SET status = %s,
                            attempts = attempts + 1,
                            last_reason_code = %s,
                            observed_projection_json = %s,
                            updated_at = %s,
                            delivered_at = %s
                        WHERE outbox_id = %s AND status = 'pending'
                        """
                    ).format(outboxes=outboxes),
                    (
                        outcome.status.value,
                        outcome.reason_code,
                        (
                            None
                            if outcome.observed_projection is None
                            else Jsonb(outcome.observed_projection.model_dump(mode="json"))
                        ),
                        outcome.occurred_at,
                        delivered_at,
                        outcome.outbox_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise RegistryControlError(
                        RegistryControlErrorCode.RECONCILIATION_CONFLICT,
                        "registry projection outbox changed concurrently",
                    )
                self._append_audit_event(
                    connection,
                    workspace_id=outcome.scope.workspace_id,
                    operation="registry_projection_" + outcome.status.value,
                    transition_id=outcome.transition_id,
                    event_id=_event_id(
                        "registry_projection",
                        _canonical_fingerprint(outcome.model_dump(mode="json")),
                    ),
                    event_payload={"outcome": outcome.model_dump(mode="json")},
                    occurred_at=outcome.occurred_at,
                )
        except RegistryControlError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _store_unavailable("registry projection outcome commit failed") from error

    def verify_audit_chain(self, workspace_id: str) -> ControlAuditChainVerification:
        table = self._db.table("control_audit_events")
        try:
            with self._db.connect() as connection:
                rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT event_id, operation, transition_id, previous_hash,
                               event_hash, payload_fingerprint, key_version,
                               event_json, occurred_at
                        FROM {table}
                        WHERE workspace_id = %s
                        ORDER BY sequence
                        """
                    ).format(table=table),
                    (workspace_id,),
                ).fetchall()
            previous: str | None = None
            for row in rows:
                key_version = str(row[6])
                key = self.audit_signing_keys.get(key_version)
                if key is None or row[3] != previous:
                    return ControlAuditChainVerification(
                        workspace_id=workspace_id,
                        event_count=len(rows),
                        head_hash=previous,
                        valid=False,
                    )
                event_payload = row[7]
                payload_fingerprint = _canonical_fingerprint(event_payload)
                if payload_fingerprint != row[5]:
                    return ControlAuditChainVerification(
                        workspace_id=workspace_id,
                        event_count=len(rows),
                        head_hash=previous,
                        valid=False,
                    )
                expected = _audit_hash(
                    key=key,
                    event_id=str(row[0]),
                    workspace_id=workspace_id,
                    operation=str(row[1]),
                    transition_id=None if row[2] is None else str(row[2]),
                    previous_hash=previous,
                    payload_fingerprint=payload_fingerprint,
                    key_version=key_version,
                    occurred_at=_isoformat(row[8]),
                )
                if not hmac.compare_digest(expected, str(row[4])):
                    return ControlAuditChainVerification(
                        workspace_id=workspace_id,
                        event_count=len(rows),
                        head_hash=previous,
                        valid=False,
                    )
                previous = str(row[4])
            return ControlAuditChainVerification(
                workspace_id=workspace_id,
                event_count=len(rows),
                head_hash=previous,
                valid=True,
            )
        except (psycopg.Error, TypeError, ValueError) as error:
            raise _store_unavailable("control audit verification failed") from error

    def _load_replayed_commit(
        self,
        connection: psycopg.Connection[Any],
        transition: RegistryActivationTransition,
        outbox: RegistryProjectionOutboxItem,
    ) -> RegistryControlCommit | None:
        transitions = self._db.table("registry_activation_transitions")
        outboxes = self._db.table("registry_reconciliation_outbox")
        audits = self._db.table("control_audit_events")
        row = connection.execute(
            sql.SQL("SELECT payload_json FROM {transitions} WHERE transition_id = %s").format(
                transitions=transitions
            ),
            (transition.id,),
        ).fetchone()
        if row is None:
            return None
        stored_transition = RegistryActivationTransition.model_validate(row[0])
        outbox_row = connection.execute(
            sql.SQL(
                """
                SELECT outbox_id, transition_id, desired_projection_json,
                       status, attempts, created_at, last_reason_code
                FROM {outboxes}
                WHERE transition_id = %s
                """
            ).format(outboxes=outboxes),
            (transition.id,),
        ).fetchone()
        audit_row = connection.execute(
            sql.SQL(
                """
                SELECT event_hash FROM {audits}
                WHERE transition_id = %s
                  AND operation IN ('registry_activate', 'registry_rollback')
                """
            ).format(audits=audits),
            (transition.id,),
        ).fetchone()
        if outbox_row is None or audit_row is None:
            raise _store_unavailable("registry activation replay state is incomplete")
        stored_outbox = _outbox_from_row(outbox_row)
        if (
            stored_transition != transition
            or stored_outbox.id != outbox.id
            or stored_outbox.transition_id != outbox.transition_id
            or stored_outbox.desired != outbox.desired
        ):
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "registry transition identity already contains different content",
            )
        return RegistryControlCommit(
            transition=stored_transition,
            outbox=outbox,
            audit_event_hash=str(audit_row[0]),
            replayed=True,
        )

    def _load_active_for_update(
        self,
        connection: psycopg.Connection[Any],
        scope: SemanticRegistryScope,
    ) -> ActiveRegistryPointer | None:
        table = self._db.table("registry_active_pointers")
        row = connection.execute(
            sql.SQL(
                """
                SELECT generation, registry_version, registry_fingerprint,
                       registry_target, transition_id, activated_by,
                       activated_at, decision_ids_json
                FROM {table}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                FOR UPDATE
                """
            ).format(table=table),
            _scope_params(scope),
        ).fetchone()
        return None if row is None else _pointer_from_row(scope, row)

    def _insert_transition(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        transition: RegistryActivationTransition,
    ) -> None:
        proposal = transition.approval.proposal
        pointer = transition.active_pointer
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    transition_id, workspace_id, catalog_scope, registry_id,
                    generation, action, expected_generation,
                    expected_registry_version, expected_registry_fingerprint,
                    expected_transition_id, target_registry_version,
                    target_registry_fingerprint, target_registry_urn,
                    target_publication_approval_id, rollback_transition_id,
                    proposal_fingerprint, approval_id, actor, approved_at,
                    decision_ids_json, committed_at, payload_json
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """
            ).format(table=table),
            (
                transition.id,
                pointer.scope.workspace_id,
                pointer.scope.catalog_scope,
                pointer.scope.registry_id,
                pointer.generation,
                proposal.action.value,
                proposal.expected_generation,
                proposal.expected_registry_version,
                proposal.expected_registry_fingerprint,
                proposal.expected_transition_id,
                proposal.target_registry_version,
                proposal.target_registry_fingerprint,
                proposal.target_registry_urn,
                proposal.target_publication_approval_id,
                proposal.rollback_transition_id,
                proposal.fingerprint,
                transition.approval.id,
                transition.approval.actor,
                transition.approval.approved_at,
                Jsonb(list(proposal.decision_ids)),
                transition.committed_at,
                Jsonb(transition.model_dump(mode="json")),
            ),
        )

    def _write_pointer(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        transition: RegistryActivationTransition,
    ) -> None:
        pointer = transition.active_pointer
        params = (
            pointer.generation,
            pointer.registry_version,
            pointer.registry_fingerprint,
            pointer.registry_target,
            pointer.transition_id,
            pointer.activated_by,
            pointer.activated_at,
            Jsonb(list(pointer.decision_ids)),
        )
        if transition.previous_pointer is None:
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {table} (
                        workspace_id, catalog_scope, registry_id, generation,
                        registry_version, registry_fingerprint, registry_target,
                        transition_id, activated_by, activated_at, decision_ids_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                ).format(table=table),
                (*_scope_params(pointer.scope), *params),
            )
            return
        updated = connection.execute(
            sql.SQL(
                """
                UPDATE {table}
                SET generation = %s,
                    registry_version = %s,
                    registry_fingerprint = %s,
                    registry_target = %s,
                    transition_id = %s,
                    activated_by = %s,
                    activated_at = %s,
                    decision_ids_json = %s
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND registry_id = %s
                  AND generation = %s
                  AND transition_id = %s
                """
            ).format(table=table),
            (
                *params,
                *_scope_params(pointer.scope),
                transition.previous_pointer.generation,
                transition.previous_pointer.transition_id,
            ),
        )
        if updated.rowcount != 1:
            raise RegistryControlError(
                RegistryControlErrorCode.CAS_CONFLICT,
                "active semantic registry changed; prepare a new activation",
            )

    def _insert_outbox(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        outbox: RegistryProjectionOutboxItem,
    ) -> None:
        pointer = outbox.desired.pointer
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    outbox_id, transition_id, workspace_id, catalog_scope,
                    registry_id, generation, desired_projection_fingerprint,
                    desired_projection_json, status, attempts, last_reason_code,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                outbox.id,
                outbox.transition_id,
                pointer.scope.workspace_id,
                pointer.scope.catalog_scope,
                pointer.scope.registry_id,
                pointer.generation,
                outbox.desired.projection_fingerprint,
                Jsonb(outbox.desired.model_dump(mode="json")),
                outbox.status.value,
                outbox.attempts,
                outbox.last_reason_code,
                outbox.created_at,
                outbox.created_at,
            ),
        )

    def _lock_workspace_audit(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
    ) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_advisory_lock_id(workspace_id),),
        )

    def _append_audit_event(
        self,
        connection: psycopg.Connection[Any],
        *,
        workspace_id: str,
        operation: str,
        transition_id: str | None,
        event_id: str,
        event_payload: dict[str, object],
        occurred_at: datetime,
    ) -> str:
        table = self._db.table("control_audit_events")
        previous_row = connection.execute(
            sql.SQL(
                """
                SELECT event_hash FROM {table}
                WHERE workspace_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).format(table=table),
            (workspace_id,),
        ).fetchone()
        previous_hash = None if previous_row is None else str(previous_row[0])
        payload_fingerprint = _canonical_fingerprint(event_payload)
        key = self.audit_signing_keys[self.active_audit_key_version]
        event_hash = _audit_hash(
            key=key,
            event_id=event_id,
            workspace_id=workspace_id,
            operation=operation,
            transition_id=transition_id,
            previous_hash=previous_hash,
            payload_fingerprint=payload_fingerprint,
            key_version=self.active_audit_key_version,
            occurred_at=_isoformat(occurred_at),
        )
        connection.execute(
            sql.SQL(
                """
                INSERT INTO {table} (
                    event_id, workspace_id, operation, transition_id,
                    previous_hash, event_hash, payload_fingerprint,
                    key_version, event_json, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
            ).format(table=table),
            (
                event_id,
                workspace_id,
                operation,
                transition_id,
                previous_hash,
                event_hash,
                payload_fingerprint,
                self.active_audit_key_version,
                Jsonb(event_payload),
                occurred_at,
            ),
        )
        return event_hash


def _scope_params(scope: SemanticRegistryScope) -> tuple[str, str, str]:
    return scope.workspace_id, scope.catalog_scope, scope.registry_id


def _pointer_from_row(
    scope: SemanticRegistryScope,
    row: tuple[object, ...],
) -> ActiveRegistryPointer:
    return ActiveRegistryPointer.model_validate(
        {
            "scope": scope.model_dump(mode="json"),
            "generation": row[0],
            "registry_version": row[1],
            "registry_fingerprint": row[2],
            "registry_target": row[3],
            "transition_id": row[4],
            "activated_by": row[5],
            "activated_at": row[6],
            "decision_ids": row[7],
        }
    )


def _outbox_from_row(row: tuple[object, ...]) -> RegistryProjectionOutboxItem:
    return RegistryProjectionOutboxItem.model_validate(
        {
            "id": row[0],
            "transition_id": row[1],
            "desired": row[2],
            "status": row[3],
            "attempts": row[4],
            "created_at": row[5],
            "last_reason_code": row[6],
        }
    )


def _validate_commit_input(
    transition: RegistryActivationTransition,
    outbox: RegistryProjectionOutboxItem,
) -> None:
    try:
        validated_transition = RegistryActivationTransition.model_validate(
            transition.model_dump(mode="python", warnings=False)
        )
        validated_outbox = RegistryProjectionOutboxItem.model_validate(
            outbox.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry control commit input is invalid",
        ) from error
    if (
        validated_transition != transition
        or validated_outbox != outbox
        or outbox.transition_id != transition.id
        or outbox.desired.pointer != transition.active_pointer
        or outbox.status is not RegistryProjectionOutboxStatus.PENDING
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.INVALID_RESPONSE,
            "registry transition and outbox do not match",
        )


def _validate_outcome_row(
    row: tuple[object, ...],
    outcome: RegistryProjectionOutcome,
) -> None:
    generation = row[4]
    if (
        str(row[0]) != outcome.transition_id
        or str(row[1]) != outcome.scope.workspace_id
        or str(row[2]) != outcome.scope.catalog_scope
        or str(row[3]) != outcome.scope.registry_id
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation != outcome.generation
    ):
        raise RegistryControlError(
            RegistryControlErrorCode.RECONCILIATION_CONFLICT,
            "registry projection outcome identifies another outbox item",
        )


def _terminal_outcome_matches(
    row: tuple[object, ...],
    outcome: RegistryProjectionOutcome,
) -> bool:
    observed = None if row[7] is None else RegistryProjectionState.model_validate(row[7])
    return (
        str(row[5]) == outcome.status.value
        and observed == outcome.observed_projection
        and row[8] == outcome.reason_code
    )


def _event_id(kind: str, identity: str) -> str:
    return f"control-{kind}-v1-{_canonical_fingerprint({'identity': identity})}"


def _advisory_lock_id(workspace_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"schemabridge.audit:{workspace_id}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _audit_hash(
    *,
    key: bytes,
    event_id: str,
    workspace_id: str,
    operation: str,
    transition_id: str | None,
    previous_hash: str | None,
    payload_fingerprint: str,
    key_version: str,
    occurred_at: str,
) -> str:
    payload = {
        "event_id": event_id,
        "workspace_id": workspace_id,
        "operation": operation,
        "transition_id": transition_id,
        "previous_hash": previous_hash,
        "payload_fingerprint": payload_fingerprint,
        "key_version": key_version,
        "occurred_at": occurred_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _isoformat(value: object) -> str:
    if not isinstance(value, datetime):
        raise ValueError("control audit timestamp is invalid")
    return value.isoformat()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def _store_unavailable(message: str) -> RegistryControlError:
    return RegistryControlError(RegistryControlErrorCode.STORE_UNAVAILABLE, message)
