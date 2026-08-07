"""Durable, complete-by-construction workflow and recipe dependency index."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.semantic_change import (
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.application.ports.semantic_dependency_sources import (
    MAX_SEMANTIC_DEPENDENCY_ARTIFACTS,
    MAX_SEMANTIC_DEPENDENCY_EDGES,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeFinding,
    SemanticChangeImpact,
    SemanticChangeInspectionContext,
    SemanticDependencyIndexState,
    SemanticImpactKind,
    SemanticImpactSet,
    semantic_change_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IndexedArtifactKind(StrEnum):
    WORKFLOW = "workflow"
    QUERY_RECIPE = "query_recipe"


@dataclass(frozen=True, slots=True)
class IndexedSemanticArtifact:
    kind: IndexedArtifactKind
    artifact_id: str
    version: int
    fingerprint: str
    mapping_decision_ids: tuple[str, ...] = ()
    join_contracts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.artifact_id.strip()
            or len(self.artifact_id) > 500
            or self.version < 1
            or _SHA256.fullmatch(self.fingerprint) is None
            or self.mapping_decision_ids != tuple(sorted(set(self.mapping_decision_ids)))
            or self.join_contracts != tuple(sorted(set(self.join_contracts)))
            or any(not value.strip() for value in self.mapping_decision_ids)
            or any(
                not contract_id.strip() or version < 1
                for contract_id, version in self.join_contracts
            )
        ):
            raise ValueError("semantic artifact dependency is invalid")


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeDependencyIndex:
    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-reconciler"
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

    def reconcile(
        self,
        scope: SemanticRegistryScope,
        *,
        registry_generation: int,
        registry_version: int,
        registry_fingerprint: str,
        pointer_transition_id: str,
        watermark: int,
        complete: bool,
        artifacts: tuple[IndexedSemanticArtifact, ...],
        indexed_at: datetime,
    ) -> SemanticDependencyIndexState:
        if (
            registry_generation < 1
            or registry_version < 1
            or watermark < 1
            or indexed_at.tzinfo is None
            or indexed_at.utcoffset() is None
            or tuple((item.kind.value, item.artifact_id, item.version) for item in artifacts)
            != tuple(
                sorted({(item.kind.value, item.artifact_id, item.version) for item in artifacts})
            )
            or len(artifacts) > MAX_SEMANTIC_DEPENDENCY_ARTIFACTS
            or sum(len(item.mapping_decision_ids) + len(item.join_contracts) for item in artifacts)
            > MAX_SEMANTIC_DEPENDENCY_EDGES
        ):
            raise ValueError("semantic dependency index snapshot is invalid")
        fingerprint = semantic_change_fingerprint(
            {
                "scope": scope,
                "watermark": watermark,
                "complete": complete,
                "registry_generation": registry_generation,
                "registry_fingerprint": registry_fingerprint,
                "artifacts": artifacts,
            }
        )
        state = SemanticDependencyIndexState(
            scope=scope,
            watermark=watermark,
            fingerprint=fingerprint,
            complete=complete,
        )
        dependencies = self._database.table("semantic_artifact_dependencies")
        states = self._database.table("semantic_dependency_index_states")
        try:
            with self._database.connect() as connection:
                current = connection.execute(
                    sql.SQL(
                        """
                        SELECT watermark, index_fingerprint, complete
                        FROM {states}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        FOR UPDATE
                        """
                    ).format(states=states),
                    _scope(scope),
                ).fetchone()
                if current is not None:
                    if _integer(current[0]) > watermark:
                        raise SemanticChangePortError(
                            SemanticChangePortErrorCode.CAS_CONFLICT,
                            "semantic dependency index watermark changed",
                        )
                    if _integer(current[0]) == watermark:
                        replay = SemanticDependencyIndexState(
                            scope=scope,
                            watermark=_integer(current[0]),
                            fingerprint=str(current[1]),
                            complete=bool(current[2]),
                        )
                        if replay != state:
                            raise SemanticChangePortError(
                                SemanticChangePortErrorCode.CAS_CONFLICT,
                                "semantic dependency index watermark already has other content",
                            )
                        return replay
                self._insert_dependencies(
                    connection,
                    dependencies,
                    scope,
                    registry_generation=registry_generation,
                    registry_fingerprint=registry_fingerprint,
                    watermark=watermark,
                    artifacts=artifacts,
                    indexed_at=indexed_at,
                )
                connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {states} (
                            workspace_id, catalog_scope, registry_id,
                            registry_generation, registry_version,
                            registry_fingerprint, pointer_transition_id,
                            watermark, index_fingerprint, complete, indexed_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (workspace_id, catalog_scope, registry_id)
                        DO UPDATE SET
                            registry_generation = EXCLUDED.registry_generation,
                            registry_version = EXCLUDED.registry_version,
                            registry_fingerprint = EXCLUDED.registry_fingerprint,
                            pointer_transition_id = EXCLUDED.pointer_transition_id,
                            watermark = EXCLUDED.watermark,
                            index_fingerprint = EXCLUDED.index_fingerprint,
                            complete = EXCLUDED.complete,
                            indexed_at = EXCLUDED.indexed_at
                        """
                    ).format(states=states),
                    (
                        *_scope(scope),
                        registry_generation,
                        registry_version,
                        registry_fingerprint,
                        pointer_transition_id,
                        watermark,
                        fingerprint,
                        complete,
                        indexed_at,
                    ),
                )
            return state
        except SemanticChangePortError:
            raise
        except psycopg.Error as error:
            raise _store_unavailable("semantic dependency index reconciliation failed") from error

    def load_state(
        self,
        scope: SemanticRegistryScope,
    ) -> SemanticDependencyIndexState:
        reader = self._database.table("load_semantic_dependency_index_state")
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    sql.SQL(
                        """
                        SELECT watermark, index_fingerprint, complete
                        FROM {reader}(%s, %s, %s)
                        """
                    ).format(reader=reader),
                    _scope(scope),
                ).fetchone()
            if row is None:
                return SemanticDependencyIndexState(
                    scope=scope,
                    watermark=0,
                    fingerprint=semantic_change_fingerprint(
                        {"scope": scope, "watermark": 0, "complete": False}
                    ),
                    complete=False,
                )
            return SemanticDependencyIndexState(
                scope=scope,
                watermark=_integer(row[0]),
                fingerprint=str(row[1]),
                complete=bool(row[2]),
            )
        except (psycopg.Error, TypeError, ValueError) as error:
            raise _store_unavailable("semantic dependency index read failed") from error

    def resolve_impacts(
        self,
        context: SemanticChangeInspectionContext,
        findings: tuple[SemanticChangeFinding, ...],
    ) -> SemanticImpactSet:
        dependencies = self._database.table("semantic_artifact_dependencies")
        states = self._database.table("semantic_dependency_index_states")
        mapping_findings: dict[str, list[SemanticChangeFinding]] = defaultdict(list)
        join_findings: dict[tuple[str, int], list[SemanticChangeFinding]] = defaultdict(list)
        for finding in findings:
            if finding.mapping is not None:
                mapping_findings[finding.mapping.approval_decision_id].append(finding)
                for join in context.joins:
                    if finding.mapping.physical_field in {
                        join.left_field,
                        join.right_field,
                    }:
                        join_findings[(join.contract_id, join.version)].append(finding)
            elif finding.join is not None:
                join_findings[(finding.join.contract_id, finding.join.version)].append(finding)
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                state_row = connection.execute(
                    sql.SQL(
                        """
                        SELECT registry_generation, registry_fingerprint,
                               watermark, index_fingerprint, complete
                        FROM {states}
                        WHERE workspace_id = %s
                          AND catalog_scope = %s
                          AND registry_id = %s
                        """
                    ).format(states=states),
                    _scope(context.scope),
                ).fetchone()
                if state_row is None:
                    rows: list[tuple[object, ...]] = []
                    state = SemanticDependencyIndexState(
                        scope=context.scope,
                        watermark=0,
                        fingerprint=semantic_change_fingerprint(
                            {
                                "scope": context.scope,
                                "watermark": 0,
                                "complete": False,
                            }
                        ),
                        complete=False,
                    )
                    registry_generation = context.pointer_generation
                else:
                    registry_generation = _integer(state_row[0])
                    state = SemanticDependencyIndexState(
                        scope=context.scope,
                        watermark=_integer(state_row[2]),
                        fingerprint=str(state_row[3]),
                        complete=bool(state_row[4]),
                    )
                    if (
                        registry_generation != context.pointer_generation
                        or str(state_row[1]) != context.registry_fingerprint
                    ):
                        state = SemanticDependencyIndexState(
                            scope=context.scope,
                            watermark=state.watermark,
                            fingerprint=state.fingerprint,
                            complete=False,
                        )
                    rows = connection.execute(
                        sql.SQL(
                            """
                            SELECT artifact_kind, artifact_id, artifact_version,
                                   artifact_fingerprint, dependency_kind,
                                   mapping_decision_id, join_contract_id,
                                   join_contract_version
                            FROM {dependencies}
                            WHERE workspace_id = %s
                              AND catalog_scope = %s
                              AND registry_id = %s
                              AND registry_generation = %s
                              AND dependency_index_watermark = %s
                              AND (
                                  mapping_decision_id = ANY(%s)
                                  OR (join_contract_id, join_contract_version)
                                     IN (
                                         SELECT *
                                         FROM unnest(%s::varchar[], %s::bigint[])
                                     )
                              )
                            ORDER BY artifact_kind, artifact_id, artifact_version,
                                     dependency_id
                            """
                        ).format(dependencies=dependencies),
                        (
                            *_scope(context.scope),
                            registry_generation,
                            state.watermark,
                            sorted(mapping_findings),
                            [item[0] for item in sorted(join_findings)],
                            [item[1] for item in sorted(join_findings)],
                        ),
                    ).fetchall()
            if state != context.dependency_index:
                raise SemanticChangePortError(
                    SemanticChangePortErrorCode.DEPENDENCY_INDEX_INCOMPLETE,
                    "semantic dependency index changed during inspection",
                )
            grouped = _direct_impacts(findings, join_findings)
            for row in rows:
                dependency_findings = (
                    mapping_findings.get(str(row[5]), [])
                    if str(row[4]) == "mapping"
                    else join_findings.get(
                        (str(row[6]), _integer(row[7])),
                        [],
                    )
                )
                key = (
                    (
                        SemanticImpactKind.WORKFLOW
                        if str(row[0]) == "workflow"
                        else SemanticImpactKind.RECIPE
                    ),
                    str(row[1]),
                    _integer(row[2]),
                )
                grouped[key].update(item.id for item in dependency_findings)
            impacts = tuple(
                SemanticChangeImpact.create(
                    kind=kind,
                    artifact_id=artifact_id,
                    artifact_version=artifact_version,
                    finding_ids=tuple(sorted(finding_ids)),
                )
                for (kind, artifact_id, artifact_version), finding_ids in grouped.items()
                if finding_ids
            )
            return SemanticImpactSet.create(
                impacts=impacts,
                complete=state.complete,
                watermark=state.watermark,
                dependency_index_fingerprint=state.fingerprint,
            )
        except SemanticChangePortError:
            raise
        except (psycopg.Error, TypeError, ValueError) as error:
            raise _store_unavailable("semantic dependency impact read failed") from error

    def _insert_dependencies(
        self,
        connection: psycopg.Connection[Any],
        table: sql.Composed,
        scope: SemanticRegistryScope,
        *,
        registry_generation: int,
        registry_fingerprint: str,
        watermark: int,
        artifacts: tuple[IndexedSemanticArtifact, ...],
        indexed_at: datetime,
    ) -> None:
        statement = sql.SQL(
            """
                INSERT INTO {table} (
                    dependency_id, workspace_id, catalog_scope, registry_id,
                    registry_generation, registry_fingerprint, artifact_kind,
                    artifact_id, artifact_version, artifact_fingerprint,
                    dependency_kind, mapping_decision_id, join_contract_id,
                    join_contract_version, binding_id, connection_id,
                    asset_key, field_key, dependency_index_watermark,
                    dependency_fingerprint, indexed_at
                )
                SELECT
                    item.dependency_id, item.workspace_id, item.catalog_scope,
                    item.registry_id, item.registry_generation,
                    item.registry_fingerprint, item.artifact_kind,
                    item.artifact_id, item.artifact_version,
                    item.artifact_fingerprint, item.dependency_kind,
                    item.mapping_decision_id, item.join_contract_id,
                    item.join_contract_version, NULL, NULL, NULL, NULL,
                    item.dependency_index_watermark,
                    item.dependency_fingerprint, item.indexed_at
                FROM jsonb_to_recordset(%s::jsonb) AS item(
                    dependency_id varchar(200),
                    workspace_id varchar(200),
                    catalog_scope varchar(120),
                    registry_id varchar(80),
                    registry_generation bigint,
                    registry_fingerprint char(64),
                    artifact_kind varchar(24),
                    artifact_id varchar(500),
                    artifact_version bigint,
                    artifact_fingerprint char(64),
                    dependency_kind varchar(24),
                    mapping_decision_id varchar(200),
                    join_contract_id varchar(200),
                    join_contract_version bigint,
                    dependency_index_watermark bigint,
                    dependency_fingerprint char(64),
                    indexed_at timestamptz
                )
                ON CONFLICT DO NOTHING
            """
        ).format(table=table)
        batch: list[dict[str, object]] = []
        for artifact in artifacts:
            for mapping_id in artifact.mapping_decision_ids:
                batch.append(
                    _dependency_record(
                        scope,
                        registry_generation=registry_generation,
                        registry_fingerprint=registry_fingerprint,
                        watermark=watermark,
                        artifact=artifact,
                        dependency_kind="mapping",
                        mapping_decision_id=mapping_id,
                        join_contract=None,
                        indexed_at=indexed_at,
                    )
                )
                if len(batch) == 500:
                    connection.execute(statement, (Jsonb(batch),))
                    batch.clear()
            for contract in artifact.join_contracts:
                batch.append(
                    _dependency_record(
                        scope,
                        registry_generation=registry_generation,
                        registry_fingerprint=registry_fingerprint,
                        watermark=watermark,
                        artifact=artifact,
                        dependency_kind="join_contract",
                        mapping_decision_id=None,
                        join_contract=contract,
                        indexed_at=indexed_at,
                    )
                )
                if len(batch) == 500:
                    connection.execute(statement, (Jsonb(batch),))
                    batch.clear()
        if batch:
            connection.execute(statement, (Jsonb(batch),))


def _dependency_record(
    scope: SemanticRegistryScope,
    *,
    registry_generation: int,
    registry_fingerprint: str,
    watermark: int,
    artifact: IndexedSemanticArtifact,
    dependency_kind: str,
    mapping_decision_id: str | None,
    join_contract: tuple[str, int] | None,
    indexed_at: datetime,
) -> dict[str, object]:
    payload = {
        "scope": scope,
        "registry_generation": registry_generation,
        "registry_fingerprint": registry_fingerprint,
        "watermark": watermark,
        "artifact": artifact,
        "dependency_kind": dependency_kind,
        "mapping_decision_id": mapping_decision_id,
        "join_contract": join_contract,
    }
    fingerprint = semantic_change_fingerprint(payload)
    return {
        "dependency_id": "dependency_" + fingerprint,
        "workspace_id": scope.workspace_id,
        "catalog_scope": scope.catalog_scope,
        "registry_id": scope.registry_id,
        "registry_generation": registry_generation,
        "registry_fingerprint": registry_fingerprint,
        "artifact_kind": artifact.kind.value,
        "artifact_id": artifact.artifact_id,
        "artifact_version": artifact.version,
        "artifact_fingerprint": artifact.fingerprint,
        "dependency_kind": dependency_kind,
        "mapping_decision_id": mapping_decision_id,
        "join_contract_id": None if join_contract is None else join_contract[0],
        "join_contract_version": None if join_contract is None else join_contract[1],
        "dependency_index_watermark": watermark,
        "dependency_fingerprint": fingerprint,
        "indexed_at": indexed_at.isoformat(),
    }


def _direct_impacts(
    findings: tuple[SemanticChangeFinding, ...],
    affected_joins: dict[tuple[str, int], list[SemanticChangeFinding]],
) -> defaultdict[tuple[SemanticImpactKind, str, int], set[str]]:
    grouped: defaultdict[tuple[SemanticImpactKind, str, int], set[str]] = defaultdict(set)
    for finding in findings:
        if finding.mapping is not None:
            grouped[
                (
                    SemanticImpactKind.MAPPING,
                    finding.mapping.logical_field.root,
                    finding.mapping.version,
                )
            ].add(finding.id)
        elif finding.join is not None:
            grouped[
                (
                    SemanticImpactKind.JOIN,
                    finding.join.contract_id,
                    finding.join.version,
                )
            ].add(finding.id)
    for (contract_id, version), related in affected_joins.items():
        grouped[(SemanticImpactKind.JOIN, contract_id, version)].update(item.id for item in related)
    return grouped


def _scope(scope: SemanticRegistryScope) -> tuple[str, str, str]:
    return scope.workspace_id, scope.catalog_scope, scope.registry_id


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("semantic dependency integer is invalid")
    return value


def _store_unavailable(message: str) -> SemanticChangePortError:
    return SemanticChangePortError(
        SemanticChangePortErrorCode.STORE_UNAVAILABLE,
        message,
    )


__all__ = [
    "IndexedArtifactKind",
    "IndexedSemanticArtifact",
    "PostgresSemanticChangeDependencyIndex",
]
