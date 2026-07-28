"""Bounded PostgreSQL retrieval of current governed binding/catalog facts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.query_studio import (
    ExecutableEvidenceStatus,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchPage,
    QueryStudioScopeSnapshot,
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
)
from schemabridge.domain.semantic_registry import PhysicalValueType


@dataclass(frozen=True, slots=True)
class _GovernanceScopeRow:
    registry_generation: int
    registry_version: int
    registry_fingerprint: str
    pointer_transition_id: str
    pointer_fingerprint: str
    head_revision: int
    baseline_revision: int
    baseline_fingerprint: str
    catalog_generation_vector_fingerprint: str
    eligible_mapping_count: int


@dataclass(frozen=True, slots=True)
class PostgresGovernedBindingFactsSearch:
    """Return DB facts only; registry enrichment and authority remain in application."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
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

    def search(self, request: GovernedBindingFactsRequest) -> GovernedFieldSearchPage:
        load_scope = sql.SQL(
            """
            SELECT *
            FROM {}.load_query_studio_governance_scope(
                %s::varchar, %s::varchar, %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))
        search = sql.SQL(
            """
            SELECT *
            FROM {}.search_governed_query_studio_fields(
                %s::varchar, %s::varchar, %s::varchar,
                %s::bigint, %s::bigint, %s,
                %s::varchar, %s, %s::bigint, %s::bigint,
                %s, %s, %s::varchar, %s::varchar[],
                %s::boolean, %s::integer, %s::integer,
                %s::varchar, %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))
        scope_values = (
            request.scope.workspace_id,
            request.scope.catalog_scope,
            request.scope.registry_id,
        )
        try:
            with self._database.connect() as connection, connection.transaction():
                scope_row = connection.execute(load_scope, scope_values).fetchone()
                if scope_row is None:
                    raise QueryStudioPortError(
                        (
                            QueryStudioPortErrorCode.SCOPE_CHANGED
                            if request.expected_scope is not None
                            else QueryStudioPortErrorCode.RESOURCE_UNAVAILABLE
                        ),
                        "governed Query Studio scope is unavailable",
                    )
                internal = _scope_from_row(scope_row)
                public_scope = _public_scope(request, internal)
                if request.expected_scope is not None and request.expected_scope != public_scope:
                    raise QueryStudioPortError(
                        QueryStudioPortErrorCode.SCOPE_CHANGED,
                        "governed Query Studio scope changed",
                    )
                after = request.after
                rows = connection.execute(
                    search,
                    (
                        *scope_values,
                        internal.registry_generation,
                        internal.registry_version,
                        internal.registry_fingerprint,
                        internal.pointer_transition_id,
                        internal.pointer_fingerprint,
                        internal.head_revision,
                        internal.baseline_revision,
                        internal.baseline_fingerprint,
                        internal.catalog_generation_vector_fingerprint,
                        "" if request.query is None else request.query.root,
                        [value.root for value in request.filters.logical_fields],
                        request.filters.restrict_logical_fields,
                        request.page_size,
                        None if after is None else after.score,
                        None if after is None else after.logical_field.root,
                        None if after is None else after.binding_id,
                    ),
                ).fetchall()
                confirmed_scope_row = connection.execute(
                    load_scope,
                    scope_values,
                ).fetchone()
                if confirmed_scope_row is None or _scope_from_row(confirmed_scope_row) != internal:
                    raise QueryStudioPortError(
                        QueryStudioPortErrorCode.SCOPE_CHANGED,
                        "governed Query Studio scope changed during retrieval",
                    )
            row_count = len(rows)
            if row_count > request.page_size + 1 or any(
                not _row_matches_scope(row, internal) for row in rows
            ):
                raise ValueError("governed search escaped its exact scope or row bound")
            items = tuple(
                _binding_from_row(row, workspace_id=request.scope.workspace_id)
                for row in rows[: request.page_size]
            )
            if any(item.signals.total != int(rows[index][43]) for index, item in enumerate(items)):
                raise ValueError("governed search score did not match its signal facts")
            next_key = (
                items[-1].search_key(
                    public_scope.fingerprint,
                    request.continuation_request_fingerprint,
                    request.request_fingerprint,
                )
                if row_count == request.page_size + 1
                else None
            )
            return GovernedFieldSearchPage(
                scope=public_scope,
                request_fingerprint=request.continuation_request_fingerprint,
                binding_facts_fingerprint=request.request_fingerprint,
                items=items,
                page_size=request.page_size,
                rows_read=row_count,
                next_key=next_key,
            )
        except QueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError, IndexError) as error:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "governed binding-facts response is invalid",
            ) from error
        except psycopg.Error as error:
            code = (
                QueryStudioPortErrorCode.INVALID_RESPONSE
                if error.sqlstate in {"22023", "22003"}
                else QueryStudioPortErrorCode.RESOURCE_UNAVAILABLE
            )
            raise QueryStudioPortError(
                code,
                "governed binding-facts search failed",
            ) from error


def _scope_from_row(row: Sequence[Any]) -> _GovernanceScopeRow:
    if len(row) != 10:
        raise ValueError("governance scope row has an invalid shape")
    return _GovernanceScopeRow(
        registry_generation=int(row[0]),
        registry_version=int(row[1]),
        registry_fingerprint=str(row[2]),
        pointer_transition_id=str(row[3]),
        pointer_fingerprint=str(row[4]),
        head_revision=int(row[5]),
        baseline_revision=int(row[6]),
        baseline_fingerprint=str(row[7]),
        catalog_generation_vector_fingerprint=str(row[8]),
        eligible_mapping_count=int(row[9]),
    )


def _public_scope(
    request: GovernedBindingFactsRequest,
    row: _GovernanceScopeRow,
) -> QueryStudioScopeSnapshot:
    if not 0 <= row.eligible_mapping_count <= 1_000:
        raise ValueError("governed mapping population exceeds its bounded registry")
    return QueryStudioScopeSnapshot(
        scope=request.scope,
        registry_version=row.registry_version,
        registry_fingerprint=row.registry_fingerprint,
        pointer_generation=row.registry_generation,
        pointer_fingerprint=row.pointer_fingerprint,
        evidence_head_revision=row.head_revision,
        evidence_baseline_revision=row.baseline_revision,
        evidence_baseline_fingerprint=row.baseline_fingerprint,
        catalog_generation_vector_fingerprint=row.catalog_generation_vector_fingerprint,
    )


def _binding_from_row(row: Sequence[Any], *, workspace_id: str) -> GovernedFieldBinding:
    if len(row) != 44:
        raise ValueError("governed binding row has an invalid shape")
    field_path = tuple(str(value) for value in row[15])
    signals = _signals_from_row(row)
    qualified_name = str(row[12])
    binding_state = str(row[6])
    if binding_state not in {"approved", "revalidated"}:
        raise ValueError("governed binding state is not executable")
    return GovernedFieldBinding(
        binding_id=str(row[3]),
        binding_fingerprint=str(row[4]),
        logical_field=LogicalFieldRef(str(row[0])),
        physical_field=PhysicalFieldRef(f"{qualified_name}.{'.'.join(field_path)}"),
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=workspace_id,
                connection_id=CatalogConnectionId(str(row[7])),
                asset_id=CatalogAssetId(str(row[11])),
            ),
            field_path=field_path,
        ),
        mapping_version=int(row[2]),
        mapping_approval_decision_id=str(row[1]),
        physical_type=PhysicalValueType(str(row[18])),
        evidence_status=(
            ExecutableEvidenceStatus.CURRENT
            if binding_state == "approved"
            else ExecutableEvidenceStatus.REVALIDATED
        ),
        catalog_generation=int(row[8]),
        catalog_generation_fingerprint=str(row[9]),
        asset_qualified_name=qualified_name,
        asset_metadata_fingerprint=str(row[13]),
        field_metadata_fingerprint=str(row[24]),
        field_definition_fingerprint=str(row[25]),
        field_terms_fingerprint=str(row[26]),
        native_type=None if row[17] is None else str(row[17]),
        definition=None if row[21] is None else str(row[21]),
        nullable=row[19],
        is_part_of_key=row[20],
        tags=tuple(str(value) for value in row[22]),
        glossary_terms=tuple(str(value) for value in row[23]),
        signals=signals,
    )


def _signals_from_row(row: Sequence[Any]) -> SearchSignalBreakdown:
    values: list[SearchSignal] = []
    if row[36]:
        values.append(SearchSignal(code=SearchSignalCode.EXACT_LOGICAL_FIELD, value=1_000))
    if row[37]:
        values.append(SearchSignal(code=SearchSignalCode.EXACT_PHYSICAL_FIELD, value=900))
    if row[38]:
        values.append(SearchSignal(code=SearchSignalCode.PHYSICAL_NAME_OVERLAP, value=400))
    taxonomy = (700 if row[39] else 0) + (650 if row[40] else 0)
    if taxonomy:
        values.append(SearchSignal(code=SearchSignalCode.TAXONOMY_OVERLAP, value=taxonomy))
    if row[41]:
        values.append(SearchSignal(code=SearchSignalCode.DEFINITION_OVERLAP, value=500))
    if row[42]:
        values.append(SearchSignal(code=SearchSignalCode.TYPE_MATCH, value=250))
    return SearchSignalBreakdown.create(tuple(values))


def _row_matches_scope(row: Sequence[Any], scope: _GovernanceScopeRow) -> bool:
    return len(row) == 44 and (
        int(row[27]),
        int(row[28]),
        str(row[29]),
        str(row[30]),
        str(row[31]),
        int(row[32]),
        int(row[33]),
        str(row[34]),
        str(row[35]),
    ) == (
        scope.registry_generation,
        scope.registry_version,
        scope.registry_fingerprint,
        scope.pointer_transition_id,
        scope.pointer_fingerprint,
        scope.head_revision,
        scope.baseline_revision,
        scope.baseline_fingerprint,
        scope.catalog_generation_vector_fingerprint,
    )


__all__ = ["PostgresGovernedBindingFactsSearch"]
