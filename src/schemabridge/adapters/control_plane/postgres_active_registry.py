"""Least-privilege PostgreSQL reader for one authoritative active pointer."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import ActiveRegistryPointer
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(frozen=True, slots=True)
class PostgresActiveRegistryPointerReader:
    """Read only the current pointer; no signing key or mutation method is present."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-worker"
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

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        table = self._database.table("registry_active_pointers")
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
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
                    (
                        scope.workspace_id,
                        scope.catalog_scope,
                        scope.registry_id,
                    ),
                ).fetchone()
            if row is None:
                return None
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
        except RegistryControlError:
            raise
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise RegistryControlError(
                RegistryControlErrorCode.STORE_UNAVAILABLE,
                "active registry pointer read failed",
            ) from error
