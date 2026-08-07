"""PostgreSQL workflow traversal and durable semantic-dependency sink."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.semantic_change.postgres_dependencies import (
    IndexedArtifactKind,
    IndexedSemanticArtifact,
    PostgresSemanticChangeDependencyIndex,
)
from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.semantic_dependency_sources import (
    MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE,
    SemanticDependencyArtifactKind,
    SemanticDependencyIndexWrite,
    SemanticDependencySourceError,
    SemanticDependencySourceErrorCode,
    SemanticDependencySourcePage,
)
from schemabridge.domain.semantic_change import SemanticDependencyIndexState
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.domain.workflows import AgentWorkflowDraft


@dataclass(frozen=True, slots=True)
class PostgresManagedWorkflowDependencySource:
    """Read every workspace workflow through one repeatable, read-only snapshot."""

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

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[AgentWorkflowDraft]]:
        """Yield keyset pages under one REPEATABLE READ transaction."""

        if not 1 <= page_size <= MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE:
            raise ValueError("managed workflow page size must be between 1 and 50")
        table = self._database.table("agent_workflow_drafts")
        try:
            with self._database.connect() as connection:
                # Shared setup may have called ``set_config`` in an implicit
                # transaction. Close it before opening the exact read snapshot.
                connection.commit()
                connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                after_id: str | None = None
                sequence = 1
                while True:
                    rows = connection.execute(
                        sql.SQL(
                            """
                            SELECT id, revision, payload, updated_at
                            FROM {table}
                            WHERE workspace_id = %s
                              AND (%s::text IS NULL OR id > %s)
                            ORDER BY id
                            LIMIT %s
                            """
                        ).format(table=table),
                        (
                            scope.workspace_id,
                            after_id,
                            after_id,
                            page_size + 1,
                        ),
                    ).fetchall()
                    has_more = len(rows) > page_size
                    selected = rows[:page_size]
                    drafts = tuple(_workflow(row) for row in selected)
                    ids = tuple(item.id for item in drafts)
                    if ids != tuple(sorted(set(ids))) or (
                        after_id is not None and ids and ids[0] <= after_id
                    ):
                        raise ValueError("managed workflow keyset did not advance")
                    eof = not has_more
                    position = f"workflow:{ids[-1]}" if ids else f"workflow:eof:{sequence}"
                    yield SemanticDependencySourcePage(
                        sequence=sequence,
                        items=drafts,
                        source_position=position,
                        eof=eof,
                    )
                    if eof:
                        return
                    if not ids:
                        raise ValueError("managed workflow source stalled")
                    after_id = ids[-1]
                    sequence += 1
        except SemanticDependencySourceError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            code = (
                SemanticDependencySourceErrorCode.INVALID_ARTIFACT
                if isinstance(error, (ValidationError, TypeError, ValueError))
                else SemanticDependencySourceErrorCode.UNAVAILABLE
            )
            raise SemanticDependencySourceError(
                code,
                "managed workflow dependency source is unavailable",
            ) from error


@dataclass(frozen=True, slots=True)
class PostgresSemanticDependencyIndexSink:
    """Translate application artifacts into the existing PostgreSQL index contract."""

    index: PostgresSemanticChangeDependencyIndex

    def reconcile(
        self,
        snapshot: SemanticDependencyIndexWrite,
    ) -> SemanticDependencyIndexState:
        artifacts = tuple(
            IndexedSemanticArtifact(
                kind=(
                    IndexedArtifactKind.WORKFLOW
                    if item.kind is SemanticDependencyArtifactKind.WORKFLOW
                    else IndexedArtifactKind.QUERY_RECIPE
                ),
                artifact_id=item.artifact_id,
                version=item.version,
                fingerprint=item.fingerprint,
                mapping_decision_ids=item.mapping_decision_ids,
                join_contracts=item.join_contracts,
            )
            for item in snapshot.artifacts
        )
        return self.index.reconcile(
            snapshot.scope,
            registry_generation=snapshot.registry_generation,
            registry_version=snapshot.registry_version,
            registry_fingerprint=snapshot.registry_fingerprint,
            pointer_transition_id=snapshot.pointer_transition_id,
            watermark=snapshot.watermark,
            complete=snapshot.complete,
            artifacts=artifacts,
            indexed_at=snapshot.indexed_at,
        )


def _workflow(row: tuple[object, ...]) -> AgentWorkflowDraft:
    if len(row) != 4:
        raise ValueError("managed workflow row shape is invalid")
    draft = AgentWorkflowDraft.model_validate(row[2])
    if (
        not isinstance(row[0], str)
        or isinstance(row[1], bool)
        or not isinstance(row[1], int)
        or draft.id != row[0]
        or draft.revision != row[1]
        or draft.updated_at != row[3]
    ):
        raise ValueError("managed workflow row disagrees with its typed payload")
    return draft


__all__ = [
    "PostgresManagedWorkflowDependencySource",
    "PostgresSemanticDependencyIndexSink",
]
