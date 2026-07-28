"""PostgreSQL persistence and opaque alias resolution for identity rotation."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import _ControlDatabase
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.identity_rotation import (
    ApprovedIdentityRotation,
    IdentityAuthorizationScope,
    IdentityBindingKind,
    IdentityInitializationApproval,
    IdentityRotationCompletion,
    IdentityRotationInvariantCode,
    IdentityRotationInvariantError,
    IdentityRotationState,
    OpaqueIdentityRotationBinding,
    VerifiedDualKeyOidcDerivation,
    build_identity_rotation_completion,
    validate_identity_initialization_approval,
    validate_identity_rotation_plan_against_state,
)

_MAX_BINDINGS = 10_001
_MAX_ALIASES = 1_001


@dataclass(frozen=True, slots=True)
class PostgresIdentityRotationStore:
    """Transactional rotation store that persists only opaque identity facts."""

    dsn: str = field(repr=False)
    audit_signing_keys: Mapping[str, bytes] = field(repr=False)
    active_audit_key_version: str
    schema: str = "schemabridge_control"
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not self.audit_signing_keys
            or self.active_audit_key_version not in self.audit_signing_keys
            or any(
                not version.strip() or len(version) > 80 or len(key) < 32 or len(set(key)) < 8
                for version, key in self.audit_signing_keys.items()
            )
        ):
            raise ValueError("identity rotation audit signing keys are invalid")
        object.__setattr__(self, "_database", _ControlDatabase(self.dsn, self.schema))

    @property
    def _db(self) -> _ControlDatabase:
        return self._database

    def initialize_verified_state(
        self,
        derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
        approval: IdentityInitializationApproval,
    ) -> IdentityRotationState:
        """Seed one lineage from verified dual-key evidence before its first rotation.

        Verification identifiers and browser/OIDC claims are intentionally discarded before
        the first SQL statement. Replays must describe the exact same opaque state.
        """

        validated_approval = _validated_initialization_approval(approval)
        try:
            validate_identity_initialization_approval(derivations, validated_approval)
        except IdentityRotationInvariantError as error:
            raise _invariant_store_error(error) from error
        initial = _initial_bindings(derivations)
        workspace = next(
            item for item in initial if item.binding_kind is IdentityBindingKind.WORKSPACE
        )
        actors = tuple(item for item in initial if item.binding_kind is IdentityBindingKind.ACTOR)
        table = self._db.table("identity_bindings")
        try:
            with self._db.connect() as connection:
                _lock_identity_lineage(connection, workspace.opaque_id)
                existing_anchor = _find_lineage_anchor(
                    connection,
                    table,
                    workspace.opaque_id,
                    require_active=False,
                )
                if existing_anchor is not None:
                    _verify_initialized_replay(connection, table, existing_anchor, initial)
                    initial_payload = _initial_audit_payload(
                        initial,
                        validated_approval,
                    )
                    _verify_exact_audit_event(
                        connection,
                        self._db.table("control_audit_events"),
                        keys=self.audit_signing_keys,
                        workspace_id=existing_anchor,
                        operation="identity_rotation_initialized",
                        event_id=_audit_event_id(
                            "identity_rotation_initialized",
                            validated_approval.id,
                        ),
                        event_payload=initial_payload,
                        occurred_at=validated_approval.approved_at,
                    )
                    active_workspace = _active_workspace_for_anchor(
                        connection,
                        table,
                        existing_anchor,
                    )
                    return self._load_state(connection, active_workspace)
                _reject_opaque_collisions(
                    connection,
                    table,
                    tuple(item.opaque_id for item in initial),
                )
                grant_owners = _grant_owners_for_workspaces(
                    connection,
                    self._db.table("workflow_access_grants"),
                    (workspace.opaque_id,),
                )
                expected_owners = frozenset(item.opaque_id for item in actors)
                if grant_owners and grant_owners != expected_owners:
                    raise _store_error(
                        IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
                        "identity initialization does not bind every historical owner",
                    )
                now = validated_approval.approved_at
                for item in initial:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {table} (
                                binding_id, workspace_id, binding_kind,
                                stable_reference_digest, opaque_id, key_version,
                                provenance_version, policy_version,
                                provenance_fingerprint, status, created_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'active', %s, %s
                            )
                            """
                        ).format(table=table),
                        (
                            _binding_id(
                                workspace.opaque_id,
                                item.binding_kind,
                                item.stable_reference_digest,
                                item.key_version,
                            ),
                            workspace.opaque_id,
                            item.binding_kind.value,
                            item.stable_reference_digest,
                            item.opaque_id,
                            item.key_version,
                            item.provenance_version,
                            item.policy_version,
                            item.provenance_fingerprint,
                            now,
                            now,
                        ),
                    )
                initial_payload = _initial_audit_payload(initial, validated_approval)
                _lock_audit_chain(connection, workspace.opaque_id)
                _append_audit_event(
                    connection,
                    self._db.table("control_audit_events"),
                    keys=self.audit_signing_keys,
                    active_key_version=self.active_audit_key_version,
                    workspace_id=workspace.opaque_id,
                    operation="identity_rotation_initialized",
                    event_id=_audit_event_id(
                        "identity_rotation_initialized",
                        validated_approval.id,
                    ),
                    event_payload=initial_payload,
                    occurred_at=now,
                )
                return self._load_state(connection, workspace.opaque_id)
        except IdentityRotationStoreError:
            raise
        except UniqueViolation as error:
            raise _store_error(
                IdentityRotationStoreErrorCode.COLLISION,
                "identity initialization collides with existing opaque bindings",
            ) from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def load_state(self, workspace_id: str) -> IdentityRotationState:
        try:
            with self._db.connect() as connection:
                return self._load_state(connection, workspace_id)
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def reserve_approved_plan(
        self,
        approved: ApprovedIdentityRotation,
    ) -> ApprovedIdentityRotation:
        validated = _validated_approved(approved)
        plans = self._db.table("identity_rotation_plans")
        rotation_bindings = self._db.table("identity_rotation_bindings")
        identity_bindings = self._db.table("identity_bindings")
        try:
            with self._db.connect() as connection:
                existing_anchor = _plan_anchor(connection, plans, validated.plan.id)
                if existing_anchor is not None:
                    _lock_identity_lineage(connection, existing_anchor)
                    existing = _load_approved_row(
                        connection,
                        plans,
                        validated.plan.id,
                        for_update=True,
                    )
                    if existing is None or existing.approved != validated:
                        raise _conflict("identity rotation plan is already reserved")
                    _verify_exact_audit_event(
                        connection,
                        self._db.table("control_audit_events"),
                        keys=self.audit_signing_keys,
                        workspace_id=existing.anchor,
                        operation="identity_rotation_approved",
                        event_id=_audit_event_id(
                            "identity_rotation_approved",
                            validated.approval.id,
                        ),
                        event_payload={"approved": validated.model_dump(mode="json")},
                        occurred_at=validated.approval.approved_at,
                    )
                    return existing.approved
                anchor = _find_lineage_anchor(
                    connection,
                    identity_bindings,
                    validated.plan.old_workspace_id,
                    require_active=True,
                )
                if anchor is None:
                    raise _store_error(
                        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                        "identity rotation source workspace is not active",
                    )
                _lock_identity_lineage(connection, anchor)
                existing = _load_approved_row(
                    connection,
                    plans,
                    validated.plan.id,
                    for_update=True,
                )
                if existing is not None:
                    raise _conflict("identity rotation plan became concurrently reserved")
                state = self._load_state(connection, validated.plan.old_workspace_id)
                try:
                    validate_identity_rotation_plan_against_state(
                        validated.plan,
                        state,
                    )
                except IdentityRotationInvariantError as error:
                    raise _invariant_store_error(error) from error
                _reject_reserved_binding_collisions(
                    connection,
                    plans,
                    rotation_bindings,
                    identity_bindings,
                    validated,
                )
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {plans} (
                            plan_id, workspace_id, from_key_version, to_key_version,
                            provenance_version, policy_version, plan_fingerprint,
                            approval_id, actor, approved_at, status,
                            expected_binding_count, verified_binding_count,
                            payload_json, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'approved', %s, 0, %s, %s, %s
                        )
                        """
                    ).format(plans=plans),
                    (
                        validated.plan.id,
                        anchor,
                        validated.plan.from_key_version,
                        validated.plan.to_key_version,
                        validated.plan.provenance_version,
                        validated.plan.policy_version,
                        validated.plan.fingerprint,
                        validated.approval.id,
                        validated.approval.actor,
                        validated.approval.approved_at,
                        validated.plan.expected_binding_count,
                        Jsonb(validated.model_dump(mode="json")),
                        validated.approval.approved_at,
                        validated.approval.approved_at,
                    ),
                )
                for binding in validated.plan.bindings:
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {bindings} (
                                plan_id, binding_kind, stable_reference_digest,
                                old_opaque_id, new_opaque_id, binding_fingerprint,
                                status, verified_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'planned', NULL)
                            """
                        ).format(bindings=rotation_bindings),
                        (
                            validated.plan.id,
                            binding.binding_kind.value,
                            binding.stable_reference_digest,
                            binding.old_opaque_id,
                            binding.new_opaque_id,
                            binding.fingerprint,
                        ),
                    )
                _lock_audit_chain(connection, anchor)
                _append_audit_event(
                    connection,
                    self._db.table("control_audit_events"),
                    keys=self.audit_signing_keys,
                    active_key_version=self.active_audit_key_version,
                    workspace_id=anchor,
                    operation="identity_rotation_approved",
                    event_id=_audit_event_id(
                        "identity_rotation_approved",
                        validated.approval.id,
                    ),
                    event_payload={"approved": validated.model_dump(mode="json")},
                    occurred_at=validated.approval.approved_at,
                )
                return validated
        except IdentityRotationStoreError:
            raise
        except UniqueViolation as error:
            raise _conflict("identity rotation reservation conflicts with durable state") from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def load_approved_plan(self, plan_id: str) -> ApprovedIdentityRotation | None:
        plans = self._db.table("identity_rotation_plans")
        try:
            with self._db.connect() as connection:
                row = _load_approved_row(connection, plans, plan_id)
                if row is None:
                    return None
                _verify_approval_audit(
                    connection,
                    self._db.table("control_audit_events"),
                    self.audit_signing_keys,
                    row,
                )
                return row.approved
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def load_completion(self, plan_id: str) -> IdentityRotationCompletion | None:
        plans = self._db.table("identity_rotation_plans")
        rotation_bindings = self._db.table("identity_rotation_bindings")
        try:
            with self._db.connect() as connection:
                row = _load_approved_row(connection, plans, plan_id)
                if row is None or row.status != "completed":
                    return None
                _verify_approval_audit(
                    connection,
                    self._db.table("control_audit_events"),
                    self.audit_signing_keys,
                    row,
                )
                completion = _completion_from_row(connection, rotation_bindings, row)
                _verify_completion_audit(
                    connection,
                    self._db.table("control_audit_events"),
                    self.audit_signing_keys,
                    completion,
                    row.anchor,
                )
                return completion
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def complete_rotation(
        self,
        completion: IdentityRotationCompletion,
    ) -> IdentityRotationCompletion:
        validated = _validated_completion(completion)
        plans = self._db.table("identity_rotation_plans")
        rotation_bindings = self._db.table("identity_rotation_bindings")
        identity_bindings = self._db.table("identity_bindings")
        plan = validated.approved.plan
        try:
            with self._db.connect() as connection:
                anchor = _plan_anchor(connection, plans, plan.id)
                if anchor is None:
                    raise _conflict("identity rotation approval is not reserved")
                _lock_identity_lineage(connection, anchor)
                stored = _load_approved_row(
                    connection,
                    plans,
                    plan.id,
                    for_update=True,
                )
                if stored is None or stored.approved != validated.approved:
                    raise _conflict("identity rotation completion does not match its reservation")
                _verify_approval_audit(
                    connection,
                    self._db.table("control_audit_events"),
                    self.audit_signing_keys,
                    stored,
                )
                if stored.status == "completed":
                    completed = _completion_from_row(
                        connection,
                        rotation_bindings,
                        stored,
                    )
                    _verify_exact_audit_event(
                        connection,
                        self._db.table("control_audit_events"),
                        keys=self.audit_signing_keys,
                        workspace_id=stored.anchor,
                        operation="identity_rotation_completed",
                        event_id=_audit_event_id(
                            "identity_rotation_completed",
                            completed.id,
                        ),
                        event_payload={"completion": completed.model_dump(mode="json")},
                        occurred_at=completed.completed_at,
                    )
                    return completed
                if stored.status != "approved":
                    raise _conflict("identity rotation reservation cannot be completed")
                state = self._load_state(connection, plan.old_workspace_id)
                try:
                    validate_identity_rotation_plan_against_state(plan, state)
                except IdentityRotationInvariantError as error:
                    raise _invariant_store_error(error) from error
                _verify_planned_bindings(
                    connection,
                    rotation_bindings,
                    validated.approved,
                    required_status="planned",
                )
                for binding in plan.bindings:
                    updated = connection.execute(
                        sql.SQL(
                            """
                            UPDATE {bindings}
                            SET status = 'previous', updated_at = %s
                            WHERE workspace_id = %s
                              AND binding_kind = %s
                              AND stable_reference_digest = %s
                              AND opaque_id = %s
                              AND key_version = %s
                              AND provenance_version = %s
                              AND policy_version = %s
                              AND provenance_fingerprint = %s
                              AND status = 'active'
                            """
                        ).format(bindings=identity_bindings),
                        (
                            validated.completed_at,
                            anchor,
                            binding.binding_kind.value,
                            binding.stable_reference_digest,
                            binding.old_opaque_id,
                            binding.from_key_version,
                            binding.provenance_version,
                            binding.policy_version,
                            binding.provenance_fingerprint,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise _store_error(
                            IdentityRotationStoreErrorCode.STALE_PLAN,
                            "identity rotation source binding changed",
                        )
                    connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {bindings} (
                                binding_id, workspace_id, binding_kind,
                                stable_reference_digest, opaque_id, key_version,
                                provenance_version, policy_version,
                                provenance_fingerprint, status, created_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'active', %s, %s
                            )
                            """
                        ).format(bindings=identity_bindings),
                        (
                            _binding_id(
                                anchor,
                                binding.binding_kind,
                                binding.stable_reference_digest,
                                binding.to_key_version,
                            ),
                            anchor,
                            binding.binding_kind.value,
                            binding.stable_reference_digest,
                            binding.new_opaque_id,
                            binding.to_key_version,
                            binding.provenance_version,
                            binding.policy_version,
                            binding.provenance_fingerprint,
                            validated.completed_at,
                            validated.completed_at,
                        ),
                    )
                    verified = connection.execute(
                        sql.SQL(
                            """
                            UPDATE {bindings}
                            SET status = 'verified', verified_at = %s
                            WHERE plan_id = %s
                              AND binding_kind = %s
                              AND stable_reference_digest = %s
                              AND binding_fingerprint = %s
                              AND status = 'planned'
                            """
                        ).format(bindings=rotation_bindings),
                        (
                            validated.completed_at,
                            plan.id,
                            binding.binding_kind.value,
                            binding.stable_reference_digest,
                            binding.fingerprint,
                        ),
                    )
                    if verified.rowcount != 1:
                        raise _conflict("identity rotation binding reservation changed")
                updated_plan = connection.execute(
                    sql.SQL(
                        """
                        UPDATE {plans}
                        SET status = 'completed',
                            verified_binding_count = expected_binding_count,
                            updated_at = %s,
                            completed_at = %s
                        WHERE plan_id = %s AND status = 'approved'
                        """
                    ).format(plans=plans),
                    (validated.completed_at, validated.completed_at, plan.id),
                )
                if updated_plan.rowcount != 1:
                    raise _conflict("identity rotation reservation changed")
                _lock_audit_chain(connection, anchor)
                _append_audit_event(
                    connection,
                    self._db.table("control_audit_events"),
                    keys=self.audit_signing_keys,
                    active_key_version=self.active_audit_key_version,
                    workspace_id=anchor,
                    operation="identity_rotation_completed",
                    event_id=_audit_event_id(
                        "identity_rotation_completed",
                        validated.id,
                    ),
                    event_payload={"completion": validated.model_dump(mode="json")},
                    occurred_at=validated.completed_at,
                )
                return validated
        except IdentityRotationStoreError:
            raise
        except UniqueViolation as error:
            raise _store_error(
                IdentityRotationStoreErrorCode.COLLISION,
                "identity rotation target collides with durable bindings",
            ) from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        try:
            with self._db.connect() as connection:
                anchor = _active_lineage_anchor(
                    connection,
                    self._db.table("identity_bindings"),
                    workspace_id,
                )
                rows = _workspace_binding_rows(
                    connection,
                    self._db.table("identity_bindings"),
                    anchor,
                )
                aliases = tuple(str(row[1]) for row in rows)
                if not aliases or len(aliases) > _MAX_ALIASES:
                    raise _store_error(
                        IdentityRotationStoreErrorCode.CYCLE,
                        "identity workspace alias history is invalid",
                    )
                return aliases
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, TypeError, ValueError) as error:
            raise _unavailable() from error

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        table = self._db.table("identity_bindings")
        try:
            with self._db.connect() as connection:
                anchor = _active_lineage_anchor(connection, table, workspace_id)
                active_actor = connection.execute(
                    sql.SQL(
                        """
                        SELECT stable_reference_digest, key_version
                        FROM {table}
                        WHERE workspace_id = %s
                          AND binding_kind = 'actor'
                          AND opaque_id = %s
                          AND status = 'active'
                        """
                    ).format(table=table),
                    (anchor, actor_id),
                ).fetchall()
                if len(active_actor) != 1:
                    raise _store_error(
                        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                        "identity actor is not active in the requested workspace",
                    )
                workspace_rows = _workspace_binding_rows(connection, table, anchor)
                active_workspace = tuple(
                    row for row in workspace_rows if row[1] == workspace_id and row[2] == "active"
                )
                if len(active_workspace) != 1 or str(active_workspace[0][0]) != str(
                    active_actor[0][1]
                ):
                    raise _store_error(
                        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                        "identity actor and workspace keys do not match",
                    )
                actor_rows = connection.execute(
                    sql.SQL(
                        """
                        SELECT key_version, opaque_id, status
                        FROM {table}
                        WHERE workspace_id = %s
                          AND binding_kind = 'actor'
                          AND stable_reference_digest = %s
                          AND status IN ('active', 'previous')
                        ORDER BY key_version, opaque_id
                        """
                    ).format(table=table),
                    (anchor, active_actor[0][0]),
                ).fetchall()
                actors_by_key = {str(row[0]): (str(row[1]), str(row[2])) for row in actor_rows}
                if len(actors_by_key) != len(actor_rows):
                    raise _store_error(
                        IdentityRotationStoreErrorCode.COLLISION,
                        "identity actor history contains duplicate key bindings",
                    )
                scopes: list[IdentityAuthorizationScope] = []
                for workspace_row in workspace_rows:
                    key_version = workspace_row[0]
                    workspace_alias = workspace_row[1]
                    status = workspace_row[2]
                    actor = actors_by_key.get(str(key_version))
                    if actor is None or actor[1] != str(status):
                        raise _store_error(
                            IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
                            "identity actor lacks a same-key workspace binding",
                        )
                    scopes.append(
                        IdentityAuthorizationScope(
                            workspace_id=str(workspace_alias),
                            actor_id=actor[0],
                            key_version=str(key_version),
                        )
                    )
                if not scopes or len(scopes) > _MAX_ALIASES:
                    raise _store_error(
                        IdentityRotationStoreErrorCode.CYCLE,
                        "identity authorization history is invalid",
                    )
                return tuple(scopes)
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def _load_state(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
    ) -> IdentityRotationState:
        identity_bindings = self._db.table("identity_bindings")
        plans = self._db.table("identity_rotation_plans")
        anchor = _active_lineage_anchor(connection, identity_bindings, workspace_id)
        workspace_rows = _workspace_binding_rows(connection, identity_bindings, anchor)
        active_workspaces = tuple(row for row in workspace_rows if row[2] == "active")
        if len(active_workspaces) != 1 or str(active_workspaces[0][1]) != workspace_id:
            raise _store_error(
                IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
                "identity workspace is not the active lineage binding",
            )
        active_key = str(active_workspaces[0][0])
        workspace_reference = str(active_workspaces[0][3])
        provenance_version = cast(int, active_workspaces[0][4])
        policy_version = cast(int, active_workspaces[0][5])
        provenance_fingerprint = str(active_workspaces[0][6])
        actor_rows = connection.execute(
            sql.SQL(
                """
                SELECT stable_reference_digest, opaque_id, key_version,
                       provenance_version, policy_version, provenance_fingerprint
                FROM {table}
                WHERE workspace_id = %s
                  AND binding_kind = 'actor'
                  AND status = 'active'
                ORDER BY opaque_id
                """
            ).format(table=identity_bindings),
            (anchor,),
        ).fetchall()
        if not actor_rows:
            raise _store_error(
                IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
                "identity workspace has no active owners",
            )
        owner_ids = tuple(str(row[1]) for row in actor_rows)
        if len(owner_ids) != len(set(owner_ids)) or any(
            str(row[2]) != active_key
            or int(row[3]) != provenance_version
            or int(row[4]) != policy_version
            or str(row[5]) != provenance_fingerprint
            for row in actor_rows
        ):
            raise _store_error(
                IdentityRotationStoreErrorCode.COLLISION,
                "identity active bindings are inconsistent",
            )
        aliases = tuple(str(row[1]) for row in workspace_rows)
        _verify_grant_bindings(
            connection,
            self._db.table("workflow_access_grants"),
            identity_bindings,
            anchor,
            workspace_rows,
            actor_rows,
        )
        completed_rows = connection.execute(
            sql.SQL(
                """
                SELECT plan_id, workspace_id, from_key_version, to_key_version,
                       provenance_version, policy_version, plan_fingerprint,
                       approval_id, actor, approved_at, status,
                       expected_binding_count, verified_binding_count,
                       payload_json, completed_at
                FROM {plans}
                WHERE workspace_id = %s AND status = 'completed'
                ORDER BY completed_at, plan_id
                """
            ).format(plans=plans),
            (anchor,),
        ).fetchall()
        history: list[OpaqueIdentityRotationBinding] = []
        for row in completed_rows:
            stored = _stored_plan(row)
            _verify_approval_audit(
                connection,
                self._db.table("control_audit_events"),
                self.audit_signing_keys,
                stored,
            )
            completion = _completion_from_row(
                connection,
                self._db.table("identity_rotation_bindings"),
                stored,
            )
            _verify_completion_audit(
                connection,
                self._db.table("control_audit_events"),
                self.audit_signing_keys,
                completion,
                stored.anchor,
            )
            history.extend(stored.approved.plan.bindings)
        canonical_history = tuple(sorted(history, key=_binding_sort_key))
        previous_keys = tuple(sorted(str(row[0]) for row in workspace_rows if row[2] == "previous"))
        if len(aliases) != len(set(aliases)) or len(previous_keys) + 1 != len(aliases):
            raise _store_error(
                IdentityRotationStoreErrorCode.CYCLE,
                "identity workspace history is not a linear key lineage",
            )
        return IdentityRotationState(
            workspace_id=workspace_id,
            workspace_reference_digest=workspace_reference,
            active_key_version=active_key,
            provenance_version=provenance_version,
            policy_version=policy_version,
            revision=len(completed_rows),
            owner_actor_ids=tuple(sorted(owner_ids)),
            previous_key_versions=previous_keys,
            historical_bindings=canonical_history,
        )


@dataclass(frozen=True, slots=True)
class _InitialBinding:
    binding_kind: IdentityBindingKind
    stable_reference_digest: str
    opaque_id: str
    key_version: str
    provenance_version: int
    policy_version: int
    provenance_fingerprint: str


@dataclass(frozen=True, slots=True)
class _StoredPlan:
    approved: ApprovedIdentityRotation
    anchor: str
    status: str
    verified_binding_count: int
    completed_at: datetime | None


def _initial_bindings(
    derivations: tuple[VerifiedDualKeyOidcDerivation, ...],
) -> tuple[_InitialBinding, ...]:
    if not derivations or len(derivations) + 1 > _MAX_BINDINGS:
        raise _store_error(
            IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
            "identity initialization requires a bounded owner set",
        )
    first = derivations[0]
    previous = first.previous
    current = first.current
    if any(
        item.previous.workspace_id != previous.workspace_id
        or item.previous.workspace_reference_digest != previous.workspace_reference_digest
        or item.previous.key_version != previous.key_version
        or item.previous.provenance_version != previous.provenance_version
        or item.previous.policy_version != previous.policy_version
        or item.previous.provenance_fingerprint != previous.provenance_fingerprint
        or item.current.workspace_id != current.workspace_id
        or item.current.key_version != current.key_version
        for item in derivations
    ):
        raise _store_error(
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
            "identity initialization combines different workspaces or policies",
        )
    actor_ids = tuple(item.previous.actor_id for item in derivations)
    actor_references = tuple(item.previous.actor_reference_digest for item in derivations)
    if len(actor_ids) != len(set(actor_ids)) or len(actor_references) != len(set(actor_references)):
        raise _store_error(
            IdentityRotationStoreErrorCode.COLLISION,
            "identity initialization contains an actor collision",
        )
    bindings = [
        _InitialBinding(
            binding_kind=IdentityBindingKind.WORKSPACE,
            stable_reference_digest=previous.workspace_reference_digest,
            opaque_id=previous.workspace_id,
            key_version=previous.key_version,
            provenance_version=previous.provenance_version,
            policy_version=previous.policy_version,
            provenance_fingerprint=previous.provenance_fingerprint,
        )
    ]
    bindings.extend(
        _InitialBinding(
            binding_kind=IdentityBindingKind.ACTOR,
            stable_reference_digest=item.previous.actor_reference_digest,
            opaque_id=item.previous.actor_id,
            key_version=item.previous.key_version,
            provenance_version=item.previous.provenance_version,
            policy_version=item.previous.policy_version,
            provenance_fingerprint=item.previous.provenance_fingerprint,
        )
        for item in derivations
    )
    return tuple(
        sorted(
            bindings,
            key=lambda item: (
                item.binding_kind.value,
                item.stable_reference_digest,
                item.opaque_id,
            ),
        )
    )


def _active_lineage_anchor(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    workspace_id: str,
) -> str:
    anchor = _find_lineage_anchor(
        connection,
        table,
        workspace_id,
        require_active=True,
    )
    if anchor is None:
        raise _store_error(
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
            "identity workspace is unavailable or no longer active",
        )
    return anchor


def _find_lineage_anchor(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    workspace_id: str,
    *,
    require_active: bool,
) -> str | None:
    status_clause = sql.SQL(" AND status = 'active'") if require_active else sql.SQL("")
    rows = connection.execute(
        sql.SQL(
            """
            SELECT workspace_id
            FROM {table}
            WHERE binding_kind = 'workspace'
              AND opaque_id = %s{status_clause}
            """
        ).format(table=table, status_clause=status_clause),
        (workspace_id,),
    ).fetchall()
    if len(rows) > 1:
        raise _store_error(
            IdentityRotationStoreErrorCode.COLLISION,
            "opaque workspace belongs to more than one identity lineage",
        )
    return None if not rows else str(rows[0][0])


def _workspace_binding_rows(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    anchor: str,
) -> list[tuple[object, ...]]:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT key_version, opaque_id, status, stable_reference_digest,
                   provenance_version, policy_version, provenance_fingerprint
            FROM {table}
            WHERE workspace_id = %s
              AND binding_kind = 'workspace'
              AND status IN ('active', 'previous')
            ORDER BY key_version, opaque_id
            """
        ).format(table=table),
        (anchor,),
    ).fetchall()
    if len(rows) > _MAX_ALIASES:
        raise _store_error(
            IdentityRotationStoreErrorCode.CYCLE,
            "identity workspace history exceeds the supported bound",
        )
    if rows and (
        len({str(row[0]) for row in rows}) != len(rows)
        or len({str(row[1]) for row in rows}) != len(rows)
        or len({str(row[3]) for row in rows}) != 1
    ):
        raise _store_error(
            IdentityRotationStoreErrorCode.COLLISION,
            "identity workspace history contains collisions",
        )
    return rows


def _verify_grant_bindings(
    connection: psycopg.Connection[Any],
    grants: sql.Composed,
    identity_bindings: sql.Composed,
    anchor: str,
    workspace_rows: Sequence[tuple[object, ...]],
    active_actor_rows: Sequence[tuple[object, ...]],
) -> None:
    aliases = tuple(str(row[1]) for row in workspace_rows)
    if not aliases:
        raise _store_error(
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
            "identity workspace has no durable aliases",
        )
    rows = connection.execute(
        sql.SQL(
            """
            SELECT workspace_id, owner_actor_id
            FROM {grants}
            WHERE workspace_id = ANY(%s)
            """
        ).format(grants=grants),
        (list(aliases),),
    ).fetchall()
    if not rows:
        return
    active_references = {str(row[0]) for row in active_actor_rows}
    workspace_keys = {str(row[1]): str(row[0]) for row in workspace_rows}
    identity_rows = connection.execute(
        sql.SQL(
            """
            SELECT stable_reference_digest, opaque_id, key_version
            FROM {identity_bindings}
            WHERE workspace_id = %s
              AND binding_kind = 'actor'
              AND status IN ('active', 'previous')
            """
        ).format(identity_bindings=identity_bindings),
        (anchor,),
    ).fetchall()
    actor_lookup = {(str(row[1]), str(row[2])): str(row[0]) for row in identity_rows}
    for grant_workspace, grant_actor in rows:
        reference = actor_lookup.get((str(grant_actor), workspace_keys[str(grant_workspace)]))
        if reference is None or reference not in active_references:
            raise _store_error(
                IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
                "historical workflow owner lacks an active opaque binding",
            )


def _grant_owners_for_workspaces(
    connection: psycopg.Connection[Any],
    grants: sql.Composed,
    workspace_ids: tuple[str, ...],
) -> frozenset[str]:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT DISTINCT owner_actor_id
            FROM {grants}
            WHERE workspace_id = ANY(%s)
            """
        ).format(grants=grants),
        (list(workspace_ids),),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _verify_initialized_replay(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    anchor: str,
    expected: tuple[_InitialBinding, ...],
) -> None:
    key_versions = {item.key_version for item in expected}
    if len(key_versions) != 1:
        raise _conflict("identity initialization replay mixes key versions")
    key_version = next(iter(key_versions))
    rows = connection.execute(
        sql.SQL(
            """
            SELECT binding_kind, stable_reference_digest, opaque_id, key_version,
                   provenance_version, policy_version, provenance_fingerprint, status
            FROM {table}
            WHERE workspace_id = %s
              AND key_version = %s
            ORDER BY binding_kind, stable_reference_digest, opaque_id
            """
        ).format(table=table),
        (anchor, key_version),
    ).fetchall()
    workspace_statuses = {
        str(row[7]) for row in rows if str(row[0]) == IdentityBindingKind.WORKSPACE.value
    }
    if len(workspace_statuses) != 1:
        raise _conflict("identity initialization replay has no exact workspace state")
    expected_status = next(iter(workspace_statuses))
    if expected_status not in {"active", "previous"}:
        raise _conflict("identity initialization replay is not active or historical")
    observed = tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            int(row[4]),
            int(row[5]),
            str(row[6]),
            str(row[7]),
        )
        for row in rows
    )
    wanted = tuple(
        (
            item.binding_kind.value,
            item.stable_reference_digest,
            item.opaque_id,
            item.key_version,
            item.provenance_version,
            item.policy_version,
            item.provenance_fingerprint,
            expected_status,
        )
        for item in expected
    )
    if observed != wanted:
        raise _conflict("identity initialization replay differs from durable state")


def _active_workspace_for_anchor(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    anchor: str,
) -> str:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT opaque_id
            FROM {table}
            WHERE workspace_id = %s
              AND binding_kind = 'workspace'
              AND status = 'active'
            """
        ).format(table=table),
        (anchor,),
    ).fetchall()
    if len(rows) != 1:
        raise _store_error(
            IdentityRotationStoreErrorCode.CYCLE,
            "identity lineage has no exact active workspace",
        )
    return str(rows[0][0])


def _reject_opaque_collisions(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    opaque_ids: tuple[str, ...],
) -> None:
    row = connection.execute(
        sql.SQL("SELECT 1 FROM {table} WHERE opaque_id = ANY(%s) LIMIT 1").format(table=table),
        (list(opaque_ids),),
    ).fetchone()
    if row is not None:
        raise _store_error(
            IdentityRotationStoreErrorCode.COLLISION,
            "opaque identity collides with an existing lineage",
        )


def _reject_reserved_binding_collisions(
    connection: psycopg.Connection[Any],
    plans: sql.Composed,
    rotation_bindings: sql.Composed,
    identity_bindings: sql.Composed,
    approved: ApprovedIdentityRotation,
) -> None:
    targets = tuple(binding.new_opaque_id for binding in approved.plan.bindings)
    existing = connection.execute(
        sql.SQL(
            """
            SELECT 1
            FROM {identity_bindings}
            WHERE opaque_id = ANY(%s)
            LIMIT 1
            """
        ).format(identity_bindings=identity_bindings),
        (list(targets),),
    ).fetchone()
    reserved = connection.execute(
        sql.SQL(
            """
            SELECT 1
            FROM {rotation_bindings} AS binding
            JOIN {plans} AS plan ON plan.plan_id = binding.plan_id
            WHERE binding.new_opaque_id = ANY(%s)
              AND plan.plan_id <> %s
              AND plan.status IN ('approved', 'applying', 'completed')
            LIMIT 1
            """
        ).format(rotation_bindings=rotation_bindings, plans=plans),
        (list(targets), approved.plan.id),
    ).fetchone()
    if existing is not None or reserved is not None:
        raise _store_error(
            IdentityRotationStoreErrorCode.COLLISION,
            "identity rotation target collides with another durable binding",
        )


def _verify_planned_bindings(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    approved: ApprovedIdentityRotation,
    *,
    required_status: str,
) -> None:
    rows = connection.execute(
        sql.SQL(
            """
            SELECT binding_kind, stable_reference_digest, old_opaque_id,
                   new_opaque_id, binding_fingerprint, status
            FROM {table}
            WHERE plan_id = %s
            ORDER BY binding_kind, stable_reference_digest, old_opaque_id
            """
        ).format(table=table),
        (approved.plan.id,),
    ).fetchall()
    observed = tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
        )
        for row in rows
    )
    expected = tuple(
        (
            binding.binding_kind.value,
            binding.stable_reference_digest,
            binding.old_opaque_id,
            binding.new_opaque_id,
            binding.fingerprint,
            required_status,
        )
        for binding in approved.plan.bindings
    )
    if observed != expected:
        raise _conflict("identity rotation binding reservation is incomplete")


def _load_approved_row(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    plan_id: str,
    *,
    for_update: bool = False,
) -> _StoredPlan | None:
    suffix = sql.SQL(" FOR UPDATE") if for_update else sql.SQL("")
    row = connection.execute(
        sql.SQL(
            """
            SELECT plan_id, workspace_id, from_key_version, to_key_version,
                   provenance_version, policy_version, plan_fingerprint,
                   approval_id, actor, approved_at, status,
                   expected_binding_count, verified_binding_count,
                   payload_json, completed_at
            FROM {table}
            WHERE plan_id = %s{suffix}
            """
        ).format(table=table, suffix=suffix),
        (plan_id,),
    ).fetchone()
    return None if row is None else _stored_plan(row)


def _stored_plan(row: Sequence[object]) -> _StoredPlan:
    approved = ApprovedIdentityRotation.model_validate(row[13])
    plan = approved.plan
    approval = approved.approval
    if (
        str(row[0]) != plan.id
        or str(row[2]) != plan.from_key_version
        or str(row[3]) != plan.to_key_version
        or cast(int, row[4]) != plan.provenance_version
        or cast(int, row[5]) != plan.policy_version
        or str(row[6]) != plan.fingerprint
        or str(row[7]) != approval.id
        or str(row[8]) != approval.actor
        or row[9] != approval.approved_at
        or cast(int, row[11]) != plan.expected_binding_count
    ):
        raise _conflict("identity rotation plan row does not match its typed payload")
    status = str(row[10])
    verified_count = cast(int, row[12])
    completed_at = row[14]
    if (
        status not in {"approved", "applying", "completed", "blocked"}
        or verified_count < 0
        or verified_count > plan.expected_binding_count
        or (status == "completed")
        != (completed_at is not None and verified_count == plan.expected_binding_count)
    ):
        raise _conflict("identity rotation plan status is inconsistent")
    return _StoredPlan(
        approved=approved,
        anchor=str(row[1]),
        status=status,
        verified_binding_count=verified_count,
        completed_at=completed_at if isinstance(completed_at, datetime) else None,
    )


def _completion_from_row(
    connection: psycopg.Connection[Any],
    bindings: sql.Composed,
    stored: _StoredPlan,
) -> IdentityRotationCompletion:
    if stored.status != "completed" or stored.completed_at is None:
        raise _conflict("identity rotation is not complete")
    _verify_planned_bindings(
        connection,
        bindings,
        stored.approved,
        required_status="verified",
    )
    return build_identity_rotation_completion(
        stored.approved,
        completed_at=stored.completed_at,
    )


def _plan_anchor(
    connection: psycopg.Connection[Any],
    plans: sql.Composed,
    plan_id: str,
) -> str | None:
    rows = connection.execute(
        sql.SQL("SELECT workspace_id FROM {plans} WHERE plan_id = %s").format(plans=plans),
        (plan_id,),
    ).fetchall()
    if len(rows) > 1:
        raise _conflict("identity rotation plan identity is ambiguous")
    return None if not rows else str(rows[0][0])


def _lock_identity_lineage(
    connection: psycopg.Connection[Any],
    anchor: str,
) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"schemabridge:identity:{anchor}",),
    )


def _lock_audit_chain(
    connection: psycopg.Connection[Any],
    workspace_id: str,
) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (_audit_lock_id(workspace_id),),
    )


def _append_audit_event(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    *,
    keys: Mapping[str, bytes],
    active_key_version: str,
    workspace_id: str,
    operation: str,
    event_id: str,
    event_payload: dict[str, object],
    occurred_at: datetime,
) -> str:
    previous_row = connection.execute(
        sql.SQL(
            """
            SELECT event_hash
            FROM {table}
            WHERE workspace_id = %s
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).format(table=table),
        (workspace_id,),
    ).fetchone()
    previous_hash = None if previous_row is None else str(previous_row[0])
    payload_fingerprint = _canonical_fingerprint(event_payload)
    event_hash = _audit_hash(
        key=keys[active_key_version],
        event_id=event_id,
        workspace_id=workspace_id,
        operation=operation,
        previous_hash=previous_hash,
        payload_fingerprint=payload_fingerprint,
        key_version=active_key_version,
        occurred_at=occurred_at.isoformat(),
    )
    connection.execute(
        sql.SQL(
            """
            INSERT INTO {table} (
                event_id, workspace_id, operation, transition_id,
                previous_hash, event_hash, payload_fingerprint,
                key_version, event_json, occurred_at
            ) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
            """
        ).format(table=table),
        (
            event_id,
            workspace_id,
            operation,
            previous_hash,
            event_hash,
            payload_fingerprint,
            active_key_version,
            Jsonb(event_payload),
            occurred_at,
        ),
    )
    return event_hash


def _verify_exact_audit_event(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    *,
    keys: Mapping[str, bytes],
    workspace_id: str,
    operation: str,
    event_id: str,
    event_payload: dict[str, object],
    occurred_at: datetime,
) -> None:
    row = connection.execute(
        sql.SQL(
            """
            SELECT sequence, operation, transition_id, previous_hash,
                   event_hash, payload_fingerprint, key_version,
                   event_json, occurred_at
            FROM {table}
            WHERE event_id = %s AND workspace_id = %s
            """
        ).format(table=table),
        (event_id, workspace_id),
    ).fetchone()
    if row is None:
        raise _conflict("identity rotation audit evidence is missing")
    previous = connection.execute(
        sql.SQL(
            """
            SELECT event_hash
            FROM {table}
            WHERE workspace_id = %s AND sequence < %s
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).format(table=table),
        (workspace_id, row[0]),
    ).fetchone()
    expected_previous = None if previous is None else str(previous[0])
    key_version = str(row[6])
    key = keys.get(key_version)
    fingerprint = _canonical_fingerprint(event_payload)
    expected_hash = (
        None
        if key is None
        else _audit_hash(
            key=key,
            event_id=event_id,
            workspace_id=workspace_id,
            operation=operation,
            previous_hash=expected_previous,
            payload_fingerprint=fingerprint,
            key_version=key_version,
            occurred_at=occurred_at.isoformat(),
        )
    )
    if (
        str(row[1]) != operation
        or row[2] is not None
        or row[3] != expected_previous
        or str(row[5]) != fingerprint
        or row[7] != event_payload
        or row[8] != occurred_at
        or expected_hash is None
        or not hmac.compare_digest(str(row[4]), expected_hash)
    ):
        raise _conflict("identity rotation audit evidence is invalid")


def _verify_approval_audit(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    keys: Mapping[str, bytes],
    stored: _StoredPlan,
) -> None:
    approved = stored.approved
    _verify_exact_audit_event(
        connection,
        table,
        keys=keys,
        workspace_id=stored.anchor,
        operation="identity_rotation_approved",
        event_id=_audit_event_id(
            "identity_rotation_approved",
            approved.approval.id,
        ),
        event_payload={"approved": approved.model_dump(mode="json")},
        occurred_at=approved.approval.approved_at,
    )


def _verify_completion_audit(
    connection: psycopg.Connection[Any],
    table: sql.Composed,
    keys: Mapping[str, bytes],
    completion: IdentityRotationCompletion,
    anchor: str,
) -> None:
    _verify_exact_audit_event(
        connection,
        table,
        keys=keys,
        workspace_id=anchor,
        operation="identity_rotation_completed",
        event_id=_audit_event_id(
            "identity_rotation_completed",
            completion.id,
        ),
        event_payload={"completion": completion.model_dump(mode="json")},
        occurred_at=completion.completed_at,
    )


def _initial_audit_payload(
    bindings: tuple[_InitialBinding, ...],
    approval: IdentityInitializationApproval,
) -> dict[str, object]:
    return {
        "approval": approval.model_dump(mode="json", warnings=False),
        "bindings": [
            {
                "binding_kind": item.binding_kind.value,
                "stable_reference_digest": item.stable_reference_digest,
                "opaque_id": item.opaque_id,
                "key_version": item.key_version,
                "provenance_version": item.provenance_version,
                "policy_version": item.policy_version,
                "provenance_fingerprint": item.provenance_fingerprint,
            }
            for item in bindings
        ],
    }


def _audit_event_id(kind: str, identity: str) -> str:
    return f"control-{kind}-v1-{_canonical_fingerprint({'identity': identity})}"


def _audit_lock_id(workspace_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"schemabridge.audit:{workspace_id}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _audit_hash(
    *,
    key: bytes,
    event_id: str,
    workspace_id: str,
    operation: str,
    previous_hash: str | None,
    payload_fingerprint: str,
    key_version: str,
    occurred_at: str,
) -> str:
    payload = {
        "event_id": event_id,
        "workspace_id": workspace_id,
        "operation": operation,
        "transition_id": None,
        "previous_hash": previous_hash,
        "payload_fingerprint": payload_fingerprint,
        "key_version": key_version,
        "occurred_at": occurred_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def _binding_id(
    anchor: str,
    kind: IdentityBindingKind,
    stable_reference: str,
    key_version: str,
) -> str:
    payload = json.dumps(
        {
            "workspace_anchor": anchor,
            "kind": kind.value,
            "stable_reference_digest": stable_reference,
            "key_version": key_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"identity-binding-{hashlib.sha256(payload).hexdigest()}"


def _binding_sort_key(
    binding: OpaqueIdentityRotationBinding,
) -> tuple[str, str, str, str]:
    return (
        binding.binding_kind.value,
        binding.stable_reference_digest,
        binding.old_opaque_id,
        binding.new_opaque_id,
    )


def _validated_initialization_approval(
    value: IdentityInitializationApproval,
) -> IdentityInitializationApproval:
    try:
        return IdentityInitializationApproval.model_validate(
            value.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _conflict("identity initialization approval payload is invalid") from error


def _validated_approved(value: ApprovedIdentityRotation) -> ApprovedIdentityRotation:
    try:
        return ApprovedIdentityRotation.model_validate(
            value.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _conflict("identity rotation approval payload is invalid") from error


def _validated_completion(
    value: IdentityRotationCompletion,
) -> IdentityRotationCompletion:
    try:
        return IdentityRotationCompletion.model_validate(
            value.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _conflict("identity rotation completion payload is invalid") from error


def _invariant_store_error(
    error: IdentityRotationInvariantError,
) -> IdentityRotationStoreError:
    mapping = {
        IdentityRotationInvariantCode.DERIVATION_MISMATCH: (
            IdentityRotationStoreErrorCode.CONFLICT
        ),
        IdentityRotationInvariantCode.CROSS_WORKSPACE: (
            IdentityRotationStoreErrorCode.CROSS_WORKSPACE
        ),
        IdentityRotationInvariantCode.COLLISION: (IdentityRotationStoreErrorCode.COLLISION),
        IdentityRotationInvariantCode.CYCLE: IdentityRotationStoreErrorCode.CYCLE,
        IdentityRotationInvariantCode.INCOMPLETE_OWNER_BINDING: (
            IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING
        ),
        IdentityRotationInvariantCode.STALE_PLAN: (IdentityRotationStoreErrorCode.STALE_PLAN),
        IdentityRotationInvariantCode.APPROVAL_MISMATCH: (IdentityRotationStoreErrorCode.CONFLICT),
    }
    return _store_error(mapping[error.code], str(error))


def _conflict(message: str) -> IdentityRotationStoreError:
    return _store_error(IdentityRotationStoreErrorCode.CONFLICT, message)


def _unavailable() -> IdentityRotationStoreError:
    return _store_error(
        IdentityRotationStoreErrorCode.UNAVAILABLE,
        "identity rotation control store failed",
    )


def _store_error(
    code: IdentityRotationStoreErrorCode,
    message: str,
) -> IdentityRotationStoreError:
    return IdentityRotationStoreError(code, message)
