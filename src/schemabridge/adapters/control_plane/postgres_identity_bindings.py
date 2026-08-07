"""Read-only PostgreSQL resolver for verified opaque identity lineages.

The API and worker roles use this adapter to follow a current OIDC pseudonym to
historical workflow coordinates.  It deliberately has no mutation method and
receives no control-audit signing key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.identity_rotation import (
    IdentityRotationStoreError,
    IdentityRotationStoreErrorCode,
)
from schemabridge.domain.identity_rotation import IdentityAuthorizationScope

_OPAQUE_WORKSPACE = re.compile(r"^sb_workspace_v[1-9][0-9]{0,5}_[0-9a-f]{64}$")
_OPAQUE_ACTOR = re.compile(r"^sb_actor_v[1-9][0-9]{0,5}_[0-9a-f]{64}$")
_KEY_VERSION = re.compile(r"^v[1-9][0-9]{0,5}$")
_MAX_LINEAGE_SCOPES = 128


@dataclass(frozen=True, slots=True)
class PostgresIdentityBindingResolver:
    """Resolve only complete, linear, same-lineage identity bindings."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    def resolve_workspace_aliases(self, workspace_id: str) -> tuple[str, ...]:
        """Return the complete historical-to-current workspace chain."""

        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                lineage = self._load_workspace_lineage(connection, workspace_id)
                return tuple(node.opaque_id for node in lineage.nodes)
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def resolve_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        """Return exact same-key historical coordinates for one current actor."""

        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                lineage = self._load_workspace_lineage(connection, workspace_id)
                actor_nodes = self._load_actor_lineage(
                    connection,
                    lineage=lineage,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                )
                return _authorization_scopes(lineage.nodes, actor_nodes)
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def resolve_persisted_authorization_scopes(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> tuple[IdentityAuthorizationScope, ...]:
        """Validate one historical job identity against its complete live lineage."""

        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                lineage = self._load_workspace_lineage(
                    connection,
                    workspace_id,
                    require_active=False,
                )
                actor_nodes = self._load_actor_lineage(
                    connection,
                    lineage=lineage,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    require_active=False,
                )
                return _authorization_scopes(lineage.nodes, actor_nodes)
        except IdentityRotationStoreError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise _unavailable() from error

    def _load_workspace_lineage(
        self,
        connection: psycopg.Connection[Any],
        workspace_id: str,
        *,
        require_active: bool = True,
    ) -> _WorkspaceLineage:
        if _OPAQUE_WORKSPACE.fullmatch(workspace_id) is None:
            raise _cross_workspace()
        bindings = self._database.table("identity_bindings")
        status_filter = (
            sql.SQL("status = 'active'")
            if require_active
            else sql.SQL("status IN ('active', 'previous')")
        )
        anchor_rows = connection.execute(
            sql.SQL(
                """
                SELECT workspace_id
                FROM {bindings}
                WHERE binding_kind = 'workspace'
                  AND opaque_id = %s
                  AND {status_filter}
                """
            ).format(bindings=bindings, status_filter=status_filter),
            (workspace_id,),
        ).fetchall()
        if not anchor_rows:
            raise _cross_workspace()
        if len(anchor_rows) != 1:
            raise _collision("workspace belongs to multiple identity lineages")
        anchor = str(anchor_rows[0][0])
        rows = connection.execute(
            sql.SQL(
                """
                SELECT stable_reference_digest, opaque_id, key_version,
                       provenance_version, policy_version,
                       provenance_fingerprint, status
                FROM {bindings}
                WHERE workspace_id = %s
                  AND binding_kind = 'workspace'
                  AND status IN ('active', 'previous')
                ORDER BY key_version, opaque_id
                """
            ).format(bindings=bindings),
            (anchor,),
        ).fetchall()
        nodes = _binding_nodes(rows, kind="workspace")
        active = tuple(node for node in nodes if node.status == "active")
        requested = tuple(node for node in nodes if node.opaque_id == workspace_id)
        if len(active) != 1 or len(requested) != 1:
            raise _cycle("identity lineage has no exact active workspace")
        if require_active and active[0].opaque_id != workspace_id:
            raise _cross_workspace()
        rotations = self._load_rotations(connection, anchor)
        ordered = self._validate_chain(
            connection,
            nodes=nodes,
            rotations=rotations,
            kind="workspace",
        )
        return _WorkspaceLineage(
            anchor=anchor,
            nodes=ordered,
            rotations=rotations,
        )

    def _load_actor_lineage(
        self,
        connection: psycopg.Connection[Any],
        *,
        lineage: _WorkspaceLineage,
        workspace_id: str,
        actor_id: str,
        require_active: bool = True,
    ) -> tuple[_BindingNode, ...]:
        if _OPAQUE_ACTOR.fullmatch(actor_id) is None:
            raise _cross_workspace()
        bindings = self._database.table("identity_bindings")
        status_filter = (
            sql.SQL("status = 'active'")
            if require_active
            else sql.SQL("status IN ('active', 'previous')")
        )
        requested_rows = connection.execute(
            sql.SQL(
                """
                SELECT stable_reference_digest, opaque_id, key_version,
                       provenance_version, policy_version,
                       provenance_fingerprint, status
                FROM {bindings}
                WHERE workspace_id = %s
                  AND binding_kind = 'actor'
                  AND opaque_id = %s
                  AND {status_filter}
                """
            ).format(bindings=bindings, status_filter=status_filter),
            (lineage.anchor, actor_id),
        ).fetchall()
        if not requested_rows:
            raise _cross_workspace()
        if len(requested_rows) != 1:
            raise _collision("actor has multiple identity bindings")
        requested = _binding_node(requested_rows[0], kind="actor")
        workspace_nodes = tuple(node for node in lineage.nodes if node.opaque_id == workspace_id)
        if len(workspace_nodes) != 1:
            raise _cross_workspace()
        workspace = workspace_nodes[0]
        if (
            requested.key_version != workspace.key_version
            or requested.status != workspace.status
            or requested.provenance_version != workspace.provenance_version
            or requested.policy_version != workspace.policy_version
            or requested.provenance_fingerprint != workspace.provenance_fingerprint
        ):
            raise _cross_workspace()
        rows = connection.execute(
            sql.SQL(
                """
                SELECT stable_reference_digest, opaque_id, key_version,
                       provenance_version, policy_version,
                       provenance_fingerprint, status
                FROM {bindings}
                WHERE workspace_id = %s
                  AND binding_kind = 'actor'
                  AND stable_reference_digest = %s
                  AND status IN ('active', 'previous')
                ORDER BY key_version, opaque_id
                """
            ).format(bindings=bindings),
            (lineage.anchor, requested.stable_reference_digest),
        ).fetchall()
        nodes = _binding_nodes(rows, kind="actor")
        if len(nodes) != len(lineage.nodes):
            raise _incomplete("actor lineage does not cover every workspace key")
        workspace_by_key = {node.key_version: node for node in lineage.nodes}
        for node in nodes:
            matching_workspace = workspace_by_key.get(node.key_version)
            if (
                matching_workspace is None
                or node.status != matching_workspace.status
                or node.provenance_version != matching_workspace.provenance_version
                or node.policy_version != matching_workspace.policy_version
                or node.provenance_fingerprint != matching_workspace.provenance_fingerprint
            ):
                raise _incomplete("actor and workspace lineage bindings differ")
        ordered = self._validate_chain(
            connection,
            nodes=nodes,
            rotations=lineage.rotations,
            kind="actor",
        )
        if require_active and ordered[-1].opaque_id != actor_id:
            raise _cross_workspace()
        if not any(node.opaque_id == actor_id for node in ordered):
            raise _cross_workspace()
        return ordered

    def _load_rotations(
        self,
        connection: psycopg.Connection[Any],
        anchor: str,
    ) -> tuple[_CompletedRotation, ...]:
        plans = self._database.table("identity_rotation_plans")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT plan_id, from_key_version, to_key_version,
                       expected_binding_count, verified_binding_count
                FROM {plans}
                WHERE workspace_id = %s
                  AND status = 'completed'
                ORDER BY completed_at, plan_id
                """
            ).format(plans=plans),
            (anchor,),
        ).fetchall()
        if len(rows) >= _MAX_LINEAGE_SCOPES:
            raise _cycle("identity lineage exceeds the supported bound")
        rotations: list[_CompletedRotation] = []
        for row in rows:
            from_key = str(row[1])
            to_key = str(row[2])
            expected = int(row[3])
            verified = int(row[4])
            if (
                _KEY_VERSION.fullmatch(from_key) is None
                or _KEY_VERSION.fullmatch(to_key) is None
                or from_key == to_key
                or expected < 2
                or verified != expected
            ):
                raise _cycle("completed identity rotation is inconsistent")
            rotations.append(
                _CompletedRotation(
                    plan_id=str(row[0]),
                    from_key_version=from_key,
                    to_key_version=to_key,
                )
            )
        if len({item.plan_id for item in rotations}) != len(rotations):
            raise _collision("identity rotation plan history is ambiguous")
        return tuple(rotations)

    def _validate_chain(
        self,
        connection: psycopg.Connection[Any],
        *,
        nodes: tuple[_BindingNode, ...],
        rotations: tuple[_CompletedRotation, ...],
        kind: str,
    ) -> tuple[_BindingNode, ...]:
        if len(rotations) != len(nodes) - 1:
            raise _cycle("identity binding history is not a complete linear chain")
        if not rotations:
            return nodes
        rotation_bindings = self._database.table("identity_rotation_bindings")
        reference = nodes[0].stable_reference_digest
        rows = connection.execute(
            sql.SQL(
                """
                SELECT plan_id, stable_reference_digest,
                       old_opaque_id, new_opaque_id, status
                FROM {rotation_bindings}
                WHERE plan_id = ANY(%s)
                  AND binding_kind = %s
                  AND stable_reference_digest = %s
                ORDER BY plan_id
                """
            ).format(rotation_bindings=rotation_bindings),
            ([item.plan_id for item in rotations], kind, reference),
        ).fetchall()
        edges_by_plan: dict[str, tuple[str, str]] = {}
        for row in rows:
            plan_id = str(row[0])
            if str(row[1]) != reference or str(row[4]) != "verified" or plan_id in edges_by_plan:
                raise _collision("identity rotation binding history is ambiguous")
            edges_by_plan[plan_id] = (str(row[2]), str(row[3]))
        if set(edges_by_plan) != {item.plan_id for item in rotations}:
            raise _incomplete("identity rotation binding history is incomplete")

        nodes_by_id = {node.opaque_id: node for node in nodes}
        next_by_id: dict[str, str] = {}
        incoming: dict[str, int] = {node.opaque_id: 0 for node in nodes}
        for rotation in rotations:
            old_id, new_id = edges_by_plan[rotation.plan_id]
            old = nodes_by_id.get(old_id)
            new = nodes_by_id.get(new_id)
            if (
                old is None
                or new is None
                or old.key_version != rotation.from_key_version
                or new.key_version != rotation.to_key_version
                or old.status != "previous"
                or old_id in next_by_id
            ):
                raise _cycle("identity rotation edge does not match its bindings")
            next_by_id[old_id] = new_id
            incoming[new_id] = incoming.get(new_id, 0) + 1
        roots = tuple(node.opaque_id for node in nodes if incoming.get(node.opaque_id, 0) == 0)
        if len(roots) != 1 or any(value > 1 for value in incoming.values()):
            raise _cycle("identity rotation history branches or cycles")
        ordered: list[_BindingNode] = []
        cursor = roots[0]
        while cursor not in {node.opaque_id for node in ordered}:
            node = nodes_by_id.get(cursor)
            if node is None:
                raise _cycle("identity rotation history references an unknown binding")
            ordered.append(node)
            next_id = next_by_id.get(cursor)
            if next_id is None:
                break
            cursor = next_id
        if (
            len(ordered) != len(nodes)
            or ordered[-1].status != "active"
            or ordered[-1].opaque_id != nodes[-1].opaque_id
        ):
            raise _cycle("identity rotation history does not end at the active binding")
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class _BindingNode:
    stable_reference_digest: str
    opaque_id: str
    key_version: str
    provenance_version: int
    policy_version: int
    provenance_fingerprint: str
    status: str


@dataclass(frozen=True, slots=True)
class _CompletedRotation:
    plan_id: str
    from_key_version: str
    to_key_version: str


@dataclass(frozen=True, slots=True)
class _WorkspaceLineage:
    anchor: str
    nodes: tuple[_BindingNode, ...]
    rotations: tuple[_CompletedRotation, ...]


def _authorization_scopes(
    workspaces: tuple[_BindingNode, ...],
    actors: tuple[_BindingNode, ...],
) -> tuple[IdentityAuthorizationScope, ...]:
    actors_by_key = {node.key_version: node for node in actors}
    return tuple(
        IdentityAuthorizationScope(
            workspace_id=workspace.opaque_id,
            actor_id=actors_by_key[workspace.key_version].opaque_id,
            key_version=workspace.key_version,
        )
        for workspace in workspaces
    )


def _binding_nodes(
    rows: list[tuple[object, ...]],
    *,
    kind: str,
) -> tuple[_BindingNode, ...]:
    if not rows or len(rows) > _MAX_LINEAGE_SCOPES:
        raise _cycle("identity binding history is empty or exceeds the supported bound")
    nodes = tuple(_binding_node(row, kind=kind) for row in rows)
    first = nodes[0]
    if (
        len({node.opaque_id for node in nodes}) != len(nodes)
        or len({node.key_version for node in nodes}) != len(nodes)
        or any(
            node.stable_reference_digest != first.stable_reference_digest
            or node.provenance_version != first.provenance_version
            or node.policy_version != first.policy_version
            or node.provenance_fingerprint != first.provenance_fingerprint
            for node in nodes
        )
        or sum(node.status == "active" for node in nodes) != 1
    ):
        raise _collision("identity binding history is inconsistent")
    # Put the active node last so the single-node and final-chain checks are uniform.
    return tuple(
        sorted(
            nodes,
            key=lambda node: (node.status == "active", node.key_version, node.opaque_id),
        )
    )


def _binding_node(row: tuple[object, ...], *, kind: str) -> _BindingNode:
    pattern = _OPAQUE_WORKSPACE if kind == "workspace" else _OPAQUE_ACTOR
    node = _BindingNode(
        stable_reference_digest=str(row[0]),
        opaque_id=str(row[1]),
        key_version=str(row[2]),
        provenance_version=_required_integer(row[3]),
        policy_version=_required_integer(row[4]),
        provenance_fingerprint=str(row[5]),
        status=str(row[6]),
    )
    if (
        pattern.fullmatch(node.opaque_id) is None
        or _KEY_VERSION.fullmatch(node.key_version) is None
        or node.status not in {"active", "previous"}
    ):
        raise _collision("identity binding history is inconsistent")
    return node


def _cross_workspace() -> IdentityRotationStoreError:
    return IdentityRotationStoreError(
        IdentityRotationStoreErrorCode.CROSS_WORKSPACE,
        "identity is not initialized as the active member of this lineage",
    )


def _required_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("identity binding integer is invalid")
    return value


def _collision(message: str) -> IdentityRotationStoreError:
    return IdentityRotationStoreError(
        IdentityRotationStoreErrorCode.COLLISION,
        message,
    )


def _cycle(message: str) -> IdentityRotationStoreError:
    return IdentityRotationStoreError(
        IdentityRotationStoreErrorCode.CYCLE,
        message,
    )


def _incomplete(message: str) -> IdentityRotationStoreError:
    return IdentityRotationStoreError(
        IdentityRotationStoreErrorCode.INCOMPLETE_OWNER_BINDING,
        message,
    )


def _unavailable() -> IdentityRotationStoreError:
    return IdentityRotationStoreError(
        IdentityRotationStoreErrorCode.UNAVAILABLE,
        "identity binding resolution is unavailable",
    )
