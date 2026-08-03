"""Exact current-catalog verifier for retained registry-v2 physical bindings."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.registry_publication import (
    RegistryPublicationAuthorityError,
)
from schemabridge.domain.registry_publication_jobs import RegistryPublicationFailureCode
from schemabridge.domain.semantic_registry import (
    GovernedPhysicalBinding,
    SemanticRegistryScope,
)


@dataclass(frozen=True, slots=True)
class PostgresRegistryPhysicalBindingAuthority:
    """Match every retained binding against one current PostgreSQL catalog snapshot."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-publisher"
    stale_after_seconds: int = 900
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 60 <= self.stale_after_seconds <= 2_592_000:
            raise ValueError("registry publication catalog stale threshold is invalid")
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

    def require_current(
        self,
        scope: SemanticRegistryScope,
        bindings: tuple[GovernedPhysicalBinding, ...],
    ) -> None:
        if not bindings or any(
            binding.workspace_id != scope.workspace_id
            or binding.catalog_scope != scope.catalog_scope
            for binding in bindings
        ):
            raise _catalog_stale()
        requested = [
            {
                "workspace_id": binding.workspace_id,
                "connection_id": binding.connection_id.root,
                "catalog_scope": binding.catalog_scope,
                "generation": binding.catalog_generation,
                "generation_fingerprint": binding.catalog_generation_fingerprint,
                "asset_id": binding.locator.asset.asset_id.root,
                "field_path": list(binding.locator.field_path),
                "asset_metadata_fingerprint": binding.asset_metadata_fingerprint,
                "field_metadata_fingerprint": binding.field_metadata_fingerprint,
                "physical_field": binding.physical_field.root,
                "physical_type": binding.physical_type.value,
                "observed_urn": binding.observed_datahub_asset_urn,
            }
            for binding in bindings
        ]
        connections = self._database.table("catalog_connections")
        assets = self._database.table("catalog_assets")
        fields = self._database.table("catalog_fields")
        try:
            with self._database.connect() as connection:
                connection.commit()
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    rows = connection.execute(
                        sql.SQL(
                            """
                        SELECT requested.ordinality
                        FROM ROWS FROM (
                            jsonb_to_recordset(%s::jsonb) AS (
                                workspace_id text,
                                connection_id text,
                                catalog_scope text,
                                generation bigint,
                                generation_fingerprint text,
                                asset_id text,
                                field_path varchar(200)[],
                                asset_metadata_fingerprint text,
                                field_metadata_fingerprint text,
                                physical_field text,
                                physical_type text,
                                observed_urn text
                            )
                        ) WITH ORDINALITY AS requested(
                            workspace_id,
                            connection_id,
                            catalog_scope,
                            generation,
                            generation_fingerprint,
                            asset_id,
                            field_path,
                            asset_metadata_fingerprint,
                            field_metadata_fingerprint,
                            physical_field,
                            physical_type,
                            observed_urn,
                            ordinality
                        )
                        JOIN {connections} AS catalog_connection
                          ON catalog_connection.workspace_id = requested.workspace_id
                         AND catalog_connection.connection_id = requested.connection_id
                         AND catalog_connection.catalog_scope = requested.catalog_scope
                         AND catalog_connection.status = 'enabled'
                         AND catalog_connection.active_generation = requested.generation
                         AND catalog_connection.active_generation_fingerprint
                            = requested.generation_fingerprint
                         AND catalog_connection.active_generation_completed_at
                            >= clock_timestamp() - make_interval(secs => %s)
                        JOIN {assets} AS asset
                          ON asset.workspace_id = requested.workspace_id
                         AND asset.connection_id = requested.connection_id
                         AND asset.generation = requested.generation
                         AND asset.asset_id = requested.asset_id
                         AND asset.asset_id = requested.observed_urn
                         AND asset.metadata_fingerprint
                            = requested.asset_metadata_fingerprint
                         AND lower(asset.platform) = 'postgres'
                         AND asset.schema_name IS NOT NULL
                         AND asset.table_name IS NOT NULL
                        JOIN {fields} AS field
                          ON field.workspace_id = asset.workspace_id
                         AND field.connection_id = asset.connection_id
                         AND field.generation = asset.generation
                         AND field.asset_key = asset.asset_key
                         AND field.field_path = requested.field_path
                         AND field.metadata_fingerprint
                            = requested.field_metadata_fingerprint
                         AND field.normalized_type = requested.physical_type
                        WHERE requested.workspace_id = %s
                          AND requested.catalog_scope = %s
                          AND requested.physical_field = concat_ws(
                              '.',
                              asset.schema_name,
                              asset.table_name,
                              array_to_string(field.field_path, '.')
                          )
                        ORDER BY requested.ordinality
                        """
                        ).format(connections=connections, assets=assets, fields=fields),
                        (
                            Jsonb(requested),
                            self.stale_after_seconds,
                            scope.workspace_id,
                            scope.catalog_scope,
                        ),
                    ).fetchall()
            if tuple(int(row[0]) for row in rows) != tuple(range(1, len(bindings) + 1)):
                raise _catalog_stale()
        except RegistryPublicationAuthorityError:
            raise
        except psycopg.Error as error:
            raise RegistryPublicationAuthorityError(
                RegistryPublicationFailureCode.BASE_UNAVAILABLE,
                "registry publication catalog authority is unavailable",
            ) from error
        except (TypeError, ValueError) as error:
            raise _catalog_stale() from error


def _catalog_stale() -> RegistryPublicationAuthorityError:
    return RegistryPublicationAuthorityError(
        RegistryPublicationFailureCode.CATALOG_STALE,
        "registry publication retained physical authority changed",
    )


__all__ = ["PostgresRegistryPhysicalBindingAuthority"]
