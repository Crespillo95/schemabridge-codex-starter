"""Exact PostgreSQL catalog-evidence resolver for M33 semantic onboarding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypedDict

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_onboarding import (
    OnboardingCatalogGeneration,
    OnboardingRegistryBase,
    PhysicalCatalogObservation,
    PreflightSemanticOnboardingRequest,
    ResolvedOnboardingCatalogEvidence,
    SemanticOnboardingMappingInput,
    SemanticOnboardingPreflight,
)
from schemabridge.domain.semantic_registry import PhysicalValueType, SemanticRegistryScope

_DATAHUB_DATASET_URN_PREFIX = "urn:li:dataset:"


class _RequestedCatalogField(TypedDict):
    asset_id: str
    field_path: list[str]
    expected_asset_metadata_fingerprint: str | None
    expected_field_metadata_fingerprint: str | None
    physical_field: str | None


@dataclass(frozen=True, slots=True)
class PostgresSemanticOnboardingCatalogEvidence:
    """Resolve all requested fields in one tenant-scoped repeatable-read transaction."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
    stale_after_seconds: int = 900
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 60 <= self.stale_after_seconds <= 2_592_000:
            raise ValueError("semantic onboarding catalog stale threshold is invalid")
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

    def resolve_exact(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        generation: int,
        expected_generation_fingerprint: str,
        selections: tuple[SemanticOnboardingMappingInput, ...],
    ) -> ResolvedOnboardingCatalogEvidence:
        requested: list[_RequestedCatalogField] = [
            {
                "asset_id": selection.asset_id.root,
                "expected_asset_metadata_fingerprint": (
                    selection.expected_asset_metadata_fingerprint
                ),
                "expected_field_metadata_fingerprint": (
                    selection.expected_field_metadata_fingerprint
                ),
                "field_path": list(selection.field_path),
                "physical_field": selection.physical_field.root,
            }
            for selection in selections
        ]
        evidence, _ = self._resolve_snapshot(
            scope,
            connection_id,
            requested=requested,
            expected_generation=generation,
            expected_generation_fingerprint=expected_generation_fingerprint,
            include_registry_base=False,
        )
        return evidence

    def resolve_active(
        self,
        scope: SemanticRegistryScope,
        request: PreflightSemanticOnboardingRequest,
    ) -> SemanticOnboardingPreflight:
        requested: list[_RequestedCatalogField] = [
            {
                "asset_id": selection.asset_id.root,
                "expected_asset_metadata_fingerprint": None,
                "expected_field_metadata_fingerprint": None,
                "field_path": list(selection.field_path),
                "physical_field": None,
            }
            for selection in request.selections
        ]
        evidence, base = self._resolve_snapshot(
            scope,
            request.connection_id,
            requested=requested,
            expected_generation=None,
            expected_generation_fingerprint=None,
            include_registry_base=True,
        )
        if base is None:
            raise _invalid_response()
        try:
            return SemanticOnboardingPreflight.create(
                scope=scope,
                evidence=evidence,
                base_registry=base,
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise _invalid_response() from error

    def _resolve_snapshot(
        self,
        scope: SemanticRegistryScope,
        connection_id: CatalogConnectionId,
        *,
        requested: list[_RequestedCatalogField],
        expected_generation: int | None,
        expected_generation_fingerprint: str | None,
        include_registry_base: bool,
    ) -> tuple[ResolvedOnboardingCatalogEvidence, OnboardingRegistryBase | None]:
        identities: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
            (item["asset_id"], tuple(item["field_path"])) for item in requested
        )
        if not requested or len(identities) != len(set(identities)):
            raise _resource_unavailable()
        connections = self._database.table("catalog_connections")
        assets = self._database.table("catalog_assets")
        fields = self._database.table("catalog_fields")
        pointers = self._database.table("registry_active_pointers")
        try:
            with self._database.connect() as connection:
                # `_ControlDatabase` configures the timeout transactionally. End
                # that transaction so snapshot characteristics are the first
                # command of this authority read.
                connection.commit()
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    if expected_generation is None:
                        connection_row = connection.execute(
                            sql.SQL(
                                """
                                SELECT active_generation,
                                       active_generation_fingerprint,
                                       active_generation_completed_at IS NULL
                                       OR active_generation_completed_at
                                          < clock_timestamp()
                                            - make_interval(secs => %s) AS stale
                                FROM {connections}
                                WHERE workspace_id = %s
                                  AND connection_id = %s
                                  AND status = 'enabled'
                                  AND catalog_scope = %s
                                  AND active_generation IS NOT NULL
                                  AND active_generation_fingerprint IS NOT NULL
                                """
                            ).format(connections=connections),
                            (
                                self.stale_after_seconds,
                                scope.workspace_id,
                                connection_id.root,
                                scope.catalog_scope,
                            ),
                        ).fetchone()
                    else:
                        connection_row = connection.execute(
                            sql.SQL(
                                """
                                SELECT active_generation,
                                       active_generation_fingerprint,
                                       active_generation_completed_at IS NULL
                                       OR active_generation_completed_at
                                          < clock_timestamp()
                                            - make_interval(secs => %s) AS stale
                                FROM {connections}
                                WHERE workspace_id = %s
                                  AND connection_id = %s
                                  AND status = 'enabled'
                                  AND catalog_scope = %s
                                  AND active_generation = %s
                                  AND active_generation_fingerprint = %s
                                """
                            ).format(connections=connections),
                            (
                                self.stale_after_seconds,
                                scope.workspace_id,
                                connection_id.root,
                                scope.catalog_scope,
                                expected_generation,
                                expected_generation_fingerprint,
                            ),
                        ).fetchone()
                    if connection_row is None or bool(connection_row[2]):
                        raise _resource_unavailable()
                    generation = int(connection_row[0])
                    generation_fingerprint = str(connection_row[1])
                    rows = connection.execute(
                        sql.SQL(
                            """
                            SELECT requested.ordinality,
                                   asset.asset_id,
                                   asset.platform,
                                   asset.schema_name,
                                   asset.table_name,
                                   asset.metadata_fingerprint,
                                   field.field_path,
                                   field.normalized_type,
                                   field.metadata_fingerprint
                            FROM ROWS FROM (
                                jsonb_to_recordset(%s::jsonb) AS (
                                    asset_id text,
                                    field_path varchar(200)[],
                                    expected_asset_metadata_fingerprint text,
                                    expected_field_metadata_fingerprint text,
                                    physical_field text
                                )
                            ) WITH ORDINALITY AS requested(
                                asset_id,
                                field_path,
                                expected_asset_metadata_fingerprint,
                                expected_field_metadata_fingerprint,
                                physical_field,
                                ordinality
                            )
                            JOIN {assets} AS asset
                              ON asset.workspace_id = %s
                             AND asset.connection_id = %s
                             AND asset.generation = %s
                             AND asset.asset_id = requested.asset_id
                             AND (
                                  requested.expected_asset_metadata_fingerprint IS NULL
                                  OR asset.metadata_fingerprint
                                     = requested.expected_asset_metadata_fingerprint
                             )
                             AND lower(asset.platform) = 'postgres'
                             AND asset.schema_name IS NOT NULL
                             AND asset.table_name IS NOT NULL
                            JOIN {fields} AS field
                              ON field.workspace_id = asset.workspace_id
                             AND field.connection_id = asset.connection_id
                             AND field.generation = asset.generation
                             AND field.asset_key = asset.asset_key
                             AND field.field_path = requested.field_path
                             AND (
                                  requested.expected_field_metadata_fingerprint IS NULL
                                  OR field.metadata_fingerprint
                                     = requested.expected_field_metadata_fingerprint
                             )
                             AND field.normalized_type IS NOT NULL
                             AND field.normalized_type <> 'unknown'
                            WHERE requested.physical_field IS NULL
                               OR requested.physical_field = concat_ws(
                                    '.',
                                    asset.schema_name,
                                    asset.table_name,
                                    array_to_string(field.field_path, '.')
                               )
                            ORDER BY requested.ordinality
                            """
                        ).format(assets=assets, fields=fields),
                        (
                            Jsonb(requested),
                            scope.workspace_id,
                            connection_id.root,
                            generation,
                        ),
                    ).fetchall()
                    pointer_row = None
                    if include_registry_base:
                        pointer_row = connection.execute(
                            sql.SQL(
                                """
                                SELECT generation, registry_version, registry_fingerprint,
                                       registry_target, transition_id, activated_by,
                                       activated_at, decision_ids_json
                                FROM {pointers}
                                WHERE workspace_id = %s
                                  AND catalog_scope = %s
                                  AND registry_id = %s
                                """
                            ).format(pointers=pointers),
                            (
                                scope.workspace_id,
                                scope.catalog_scope,
                                scope.registry_id,
                            ),
                        ).fetchone()
            if len(rows) != len(requested):
                raise _resource_unavailable()
            observations: list[PhysicalCatalogObservation] = []
            for expected_ordinal, row in enumerate(rows, start=1):
                if int(row[0]) != expected_ordinal:
                    raise _invalid_response()
                asset_id = str(row[1])
                field_path = tuple(str(item) for item in row[6])
                physical_field = PhysicalFieldRef(".".join((str(row[3]), str(row[4]), *field_path)))
                observations.append(
                    PhysicalCatalogObservation(
                        locator=CatalogFieldLocator(
                            asset=CatalogAssetLocator(
                                workspace_id=scope.workspace_id,
                                connection_id=connection_id,
                                asset_id=CatalogAssetId(asset_id),
                            ),
                            field_path=field_path,
                        ),
                        catalog_scope=scope.catalog_scope,
                        generation=generation,
                        generation_fingerprint=generation_fingerprint,
                        asset_metadata_fingerprint=str(row[5]),
                        field_metadata_fingerprint=str(row[8]),
                        physical_field=physical_field,
                        physical_type=PhysicalValueType(str(row[7])),
                        observed_datahub_asset_urn=(
                            asset_id if asset_id.startswith(_DATAHUB_DATASET_URN_PREFIX) else None
                        ),
                    )
                )
            evidence = ResolvedOnboardingCatalogEvidence(
                generation=OnboardingCatalogGeneration(
                    workspace_id=scope.workspace_id,
                    connection_id=connection_id,
                    catalog_scope=scope.catalog_scope,
                    generation=generation,
                    inventory_fingerprint=generation_fingerprint,
                    enabled=True,
                    stale=False,
                ),
                observations=tuple(observations),
            )
            base = _registry_base(scope, pointer_row) if include_registry_base else None
            return evidence, base
        except SemanticOnboardingPortError:
            raise
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _invalid_response() from error
        except psycopg.Error as error:
            raise _unavailable() from error


def _registry_base(
    scope: SemanticRegistryScope,
    row: tuple[object, ...] | None,
) -> OnboardingRegistryBase:
    if row is None:
        return OnboardingRegistryBase()
    pointer = ActiveRegistryPointer.model_validate(
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
    return OnboardingRegistryBase(
        registry_version=pointer.registry_version,
        registry_fingerprint=pointer.registry_fingerprint,
        activation_generation=pointer.generation,
        active_pointer_fingerprint=registry_projection_fingerprint(pointer),
    )


def _resource_unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.RESOURCE_UNAVAILABLE,
        "semantic onboarding catalog evidence is unavailable",
    )


def _invalid_response() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.INVALID_RESPONSE,
        "semantic onboarding catalog evidence is invalid",
    )


def _unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "semantic onboarding catalog service is unavailable",
    )


__all__ = ["PostgresSemanticOnboardingCatalogEvidence"]
