"""Exact, bounded PostgreSQL catalog evidence for governed semantic resources."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.relationships import RelationshipWorkflowError
from schemabridge.application.ports.semantic_change import (
    SemanticChangePortError,
    SemanticChangePortErrorCode,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    BatchSemanticJoinProfileEvidencePort,
    SemanticJoinProfileEvidencePort,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.semantic_change import (
    AggregateJoinProfile,
    CatalogGenerationObservation,
    CatalogGenerationVector,
    GovernedJoinRef,
    GovernedMappingRef,
    GovernedResourceBinding,
    ObservedFieldEvidence,
    SemanticBindingSelection,
    SemanticBindingSelectionSet,
    SemanticChangeInspectionContext,
    SemanticEvidenceBaseline,
    SemanticEvidenceObservation,
)
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
)


@dataclass(frozen=True, slots=True)
class PostgresSemanticChangeEvidenceReader:
    """Read only exact active-generation rows; never search by similarity."""

    dsn: str = field(repr=False)
    relationships: SemanticJoinProfileEvidencePort
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-reconciler"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
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

    def observe(
        self,
        context: SemanticChangeInspectionContext,
        registry: GovernedSemanticRegistrySnapshot,
        baseline: SemanticEvidenceBaseline | None,
        *,
        binding_selections: SemanticBindingSelectionSet | None = None,
    ) -> SemanticEvidenceObservation:
        _validate_registry_context(context, registry, baseline)
        selections = _binding_selections(context, baseline, binding_selections)
        try:
            with self._database.connect() as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                rows = (
                    self._load_bound_rows(connection, context, baseline)
                    if baseline is not None
                    else self._load_initial_candidates(connection, context, selections)
                )
                fields, connection_ids = _field_evidence(
                    context,
                    baseline,
                    rows,
                    selections,
                )
                generations = self._load_generation_vector(
                    connection,
                    context,
                    connection_ids,
                )
            joins = self._profile_joins(context, registry, fields)
            observed_at = self.clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("semantic evidence clock returned a naive instant")
            return SemanticEvidenceObservation.create(
                context=context,
                catalog_generations=generations,
                fields=fields,
                joins=joins,
                observed_at=observed_at,
                complete=all(item.present for item in fields),
            )
        except SemanticChangePortError:
            raise
        except RelationshipWorkflowError as error:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE,
                "aggregate relationship evidence is unavailable",
            ) from error
        except (psycopg.Error, ValidationError, TypeError, ValueError) as error:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.RESOURCE_UNAVAILABLE,
                "governed catalog evidence is unavailable",
            ) from error

    def _load_initial_candidates(
        self,
        connection: psycopg.Connection[Any],
        context: SemanticChangeInspectionContext,
        selections: dict[int, SemanticBindingSelection],
    ) -> tuple[tuple[object, ...], ...]:
        candidate_lookup = self._database.table("load_semantic_initial_catalog_candidates")
        requested = [
            {
                "ordinal": ordinal,
                "dataset_ref": _physical_parts(mapping)[0],
                "field_path": list(_physical_parts(mapping)[1]),
                "selected_connection_id": (
                    None
                    if ordinal not in selections
                    else selections[ordinal].locator.asset.connection_id.root
                ),
                "selected_asset_id": (
                    None
                    if ordinal not in selections
                    else selections[ordinal].locator.asset.asset_id.root
                ),
                "selected_field_path": (
                    None
                    if ordinal not in selections
                    else list(selections[ordinal].locator.field_path)
                ),
            }
            for ordinal, mapping in enumerate(context.mappings)
        ]
        rows = connection.execute(
            sql.SQL(
                """
                SELECT
                    ordinal, connection_id, catalog_generation,
                    catalog_generation_fingerprint, asset_key, asset_id,
                    qualified_name, asset_metadata_fingerprint, field_key,
                    field_path, normalized_type, nullable, is_part_of_key,
                    field_metadata_fingerprint, field_definition_fingerprint,
                    field_terms_fingerprint, asset_present, field_present,
                    candidate_count
                FROM {candidate_lookup}(%s, %s, %s::jsonb)
                ORDER BY ordinal, connection_id, asset_id
                """
            ).format(candidate_lookup=candidate_lookup),
            (
                context.scope.workspace_id,
                context.scope.catalog_scope,
                Jsonb(requested),
            ),
        ).fetchall()
        return tuple(tuple(row) for row in rows)

    def _load_bound_rows(
        self,
        connection: psycopg.Connection[Any],
        context: SemanticChangeInspectionContext,
        baseline: SemanticEvidenceBaseline,
    ) -> tuple[tuple[object, ...], ...]:
        exact_lookup = self._database.table("load_semantic_bound_catalog_evidence")
        requested = [
            {
                "ordinal": ordinal,
                "connection_id": item.binding.locator.asset.connection_id.root,
                "asset_id": item.binding.locator.asset.asset_id.root,
                "field_path": list(item.binding.locator.field_path),
            }
            for ordinal, item in enumerate(baseline.fields)
            if item.binding is not None
        ]
        if len(requested) != len(context.mappings):
            raise ValueError("approved semantic baseline contains an incomplete binding set")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT
                    ordinal, connection_id, catalog_generation,
                    catalog_generation_fingerprint, asset_key, asset_id,
                    qualified_name, asset_metadata_fingerprint, field_key,
                    field_path, normalized_type, nullable, is_part_of_key,
                    field_metadata_fingerprint, field_definition_fingerprint,
                    field_terms_fingerprint, asset_present, field_present
                FROM {exact_lookup}(%s, %s, %s::jsonb)
                ORDER BY ordinal
                """
            ).format(exact_lookup=exact_lookup),
            (
                context.scope.workspace_id,
                context.scope.catalog_scope,
                Jsonb(requested),
            ),
        ).fetchall()
        return tuple(tuple(row) for row in rows)

    def _load_generation_vector(
        self,
        connection: psycopg.Connection[Any],
        context: SemanticChangeInspectionContext,
        connection_ids: set[str],
    ) -> CatalogGenerationVector:
        if not connection_ids:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE,
                "no exact governed catalog connection could be resolved",
            )
        projection = self._database.table("semantic_catalog_connection_evidence_projection")
        rows = connection.execute(
            sql.SQL(
                """
                SELECT DISTINCT connection_id, catalog_generation,
                                catalog_generation_fingerprint
                FROM {projection}
                WHERE workspace_id = %s
                  AND catalog_scope = %s
                  AND connection_id = ANY(%s)
                ORDER BY connection_id
                """
            ).format(projection=projection),
            (
                context.scope.workspace_id,
                context.scope.catalog_scope,
                sorted(connection_ids),
            ),
        ).fetchall()
        if len(rows) != len(connection_ids):
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE,
                "an exact governed catalog connection is unavailable",
            )
        return CatalogGenerationVector.create(
            tuple(
                CatalogGenerationObservation(
                    connection_id=CatalogConnectionId(str(row[0])),
                    generation=_integer(row[1]),
                    inventory_fingerprint=str(row[2]),
                )
                for row in rows
            )
        )

    def _profile_joins(
        self,
        context: SemanticChangeInspectionContext,
        registry: GovernedSemanticRegistrySnapshot,
        fields: tuple[ObservedFieldEvidence, ...],
    ) -> tuple[AggregateJoinProfile, ...]:
        contracts = {
            (item.id, item.version, item.approval_decision_id): item
            for item in registry.join_contracts.contracts
        }
        connection_by_field = {
            item.mapping.physical_field: item.binding.locator.asset.connection_id
            for item in fields
            if item.present and item.binding is not None
        }
        governed_joins: list[GovernedJoinRef] = []
        proposals: list[SemanticJoinProfileProposal] = []
        for governed in context.joins:
            contract = contracts.get(
                (
                    governed.contract_id,
                    governed.version,
                    governed.approval_decision_id,
                )
            )
            if (
                contract is None
                or contract.left_key.physical_field != governed.left_field
                or contract.right_key.physical_field != governed.right_field
            ):
                raise ValueError("governed join does not match the strict registry contract")
            left_connection = connection_by_field.get(governed.left_field)
            right_connection = connection_by_field.get(governed.right_field)
            if left_connection is None or right_connection is None:
                continue
            if left_connection != right_connection:
                raise SemanticChangePortError(
                    SemanticChangePortErrorCode.EVIDENCE_UNAVAILABLE,
                    "cross-connection relationship profiling is not supported",
                )
            proposal = SemanticJoinProfileProposal(
                connection_id=left_connection,
                proposal=JoinProposal(
                    id=contract.id,
                    left_key=contract.left_key,
                    right_key=contract.right_key,
                    default_join_type=contract.default_join_type,
                ),
            )
            governed_joins.append(governed)
            proposals.append(proposal)
        ordered_proposals = tuple(proposals)
        if isinstance(self.relationships, BatchSemanticJoinProfileEvidencePort):
            observed = self.relationships.profiles_bound(ordered_proposals)
        elif isinstance(self.relationships, SemanticJoinProfileEvidencePort):
            observed = tuple(
                self.relationships.profile_bound(proposal) for proposal in ordered_proposals
            )
        else:
            observed = tuple(
                self.relationships.profile_bound(proposal) for proposal in ordered_proposals
            )
        if len(observed) != len(governed_joins):
            raise ValueError("aggregate relationship evidence batch is incomplete")
        return tuple(
            AggregateJoinProfile.create(
                join=governed,
                profile=profile,
            )
            for governed, profile in zip(
                governed_joins,
                observed,
                strict=True,
            )
        )


def _validate_registry_context(
    context: SemanticChangeInspectionContext,
    registry: GovernedSemanticRegistrySnapshot,
    baseline: SemanticEvidenceBaseline | None,
) -> None:
    expected_mappings = tuple(
        sorted(
            (
                GovernedMappingRef(
                    logical_field=item.mapping.logical_field,
                    physical_field=item.mapping.physical_field,
                    version=item.mapping.version,
                    approval_decision_id=_decision_id(item.approval_decision_id),
                    physical_type=item.physical_type,
                )
                for item in registry.mapping_set.mappings
            ),
            key=lambda item: item.identity,
        )
    )
    expected_joins = tuple(
        sorted(
            (
                GovernedJoinRef(
                    contract_id=item.id,
                    version=item.version,
                    approval_decision_id=_decision_id(item.approval_decision_id),
                    left_field=item.left_key.physical_field,
                    right_field=item.right_key.physical_field,
                    cardinality=item.cardinality,
                    fanout_policy=item.fanout_policy,
                )
                for item in registry.join_contracts.contracts
            ),
            key=lambda item: item.identity,
        )
    )
    if (
        registry.registry_id != context.scope.registry_id
        or registry.catalog_scope != context.scope.catalog_scope
        or registry.version != context.registry_version
        or registry.fingerprint != context.registry_fingerprint
        or context.mappings != expected_mappings
        or context.joins != expected_joins
        or (baseline is not None and baseline.scope != context.scope)
    ):
        raise SemanticChangePortError(
            SemanticChangePortErrorCode.INVALID_RESPONSE,
            "semantic inspection context does not match the strict registry",
        )


def _binding_selections(
    context: SemanticChangeInspectionContext,
    baseline: SemanticEvidenceBaseline | None,
    selection_set: SemanticBindingSelectionSet | None,
) -> dict[int, SemanticBindingSelection]:
    if selection_set is None:
        return {}
    if baseline is not None or selection_set.scope != context.scope:
        raise SemanticChangePortError(
            SemanticChangePortErrorCode.INVALID_RESPONSE,
            "binding selections do not match this baseline-free inspection",
        )
    ordinals = {
        (
            mapping.approval_decision_id,
            mapping.version,
            mapping.physical_field.root,
        ): ordinal
        for ordinal, mapping in enumerate(context.mappings)
    }
    selected: dict[int, SemanticBindingSelection] = {}
    for selection in selection_set.selections:
        ordinal = ordinals.get(selection.mapping_identity)
        if ordinal is None:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.INVALID_RESPONSE,
                "binding selection does not identify a governed mapping",
            )
        selected[ordinal] = selection
    return selected


def _decision_id(value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError("strict registry decision identity is unavailable")
    return value


def _field_evidence(
    context: SemanticChangeInspectionContext,
    baseline: SemanticEvidenceBaseline | None,
    rows: tuple[tuple[object, ...], ...],
    selections: dict[int, SemanticBindingSelection],
) -> tuple[tuple[ObservedFieldEvidence, ...], set[str]]:
    grouped: defaultdict[int, list[tuple[object, ...]]] = defaultdict(list)
    for row in rows:
        grouped[_integer(row[0])].append(row)
    fields: list[ObservedFieldEvidence] = []
    connection_ids: set[str] = set()
    previous = () if baseline is None else baseline.fields
    for ordinal, mapping in enumerate(context.mappings):
        mapping_rows = grouped[ordinal]
        candidates = [row for row in mapping_rows if row[1] is not None]
        connection_ids.update(str(row[1]) for row in candidates)
        if baseline is None:
            candidate_count = 0 if not mapping_rows else _integer(mapping_rows[0][18])
            fields.append(
                _initial_field(
                    context.scope.workspace_id,
                    mapping,
                    candidates,
                    candidate_count,
                    selections.get(ordinal),
                )
            )
            continue
        if ordinal >= len(previous) or previous[ordinal].mapping != mapping:
            raise ValueError("approved baseline mapping order changed")
        fields.append(
            _bound_field(
                context.scope.workspace_id,
                mapping,
                previous[ordinal],
                candidates,
            )
        )
    return tuple(fields), connection_ids


def _initial_field(
    workspace_id: str,
    mapping: GovernedMappingRef,
    candidates: list[tuple[object, ...]],
    candidate_count: int,
    selection: SemanticBindingSelection | None,
) -> ObservedFieldEvidence:
    selected_candidates = (
        candidates
        if selection is None
        else [
            row
            for row in candidates
            if str(row[1]) == selection.locator.asset.connection_id.root
            and str(row[5]) == selection.locator.asset.asset_id.root
            and _string_tuple(row[9]) == selection.locator.field_path
        ]
    )
    if selection is not None:
        if len(selected_candidates) != 1:
            raise SemanticChangePortError(
                SemanticChangePortErrorCode.INVALID_RESPONSE,
                "binding selection is not an exact catalog candidate",
            )
        return _present_field(
            workspace_id,
            mapping,
            selected_candidates[0],
            candidate_count=candidate_count,
            explicit_selection=selection,
        )
    if candidate_count != 1 or len(candidates) != 1:
        return ObservedFieldEvidence.create(
            mapping=mapping,
            binding=None,
            present=False,
            candidate_count=candidate_count,
            normalized_type=None,
            nullable=None,
            is_part_of_key=None,
            asset_metadata_fingerprint=None,
            field_metadata_fingerprint=None,
            field_definition_fingerprint=None,
            field_terms_fingerprint=None,
            reason_code=("binding_missing" if candidate_count == 0 else "binding_ambiguous"),
        )
    return _present_field(workspace_id, mapping, candidates[0])


def _bound_field(
    workspace_id: str,
    mapping: GovernedMappingRef,
    previous: ObservedFieldEvidence,
    candidates: list[tuple[object, ...]],
) -> ObservedFieldEvidence:
    if len(candidates) != 1 or not bool(candidates[0][17]):
        reason = (
            "binding_missing"
            if not candidates or candidates[0][1] is None
            else ("asset_removed" if not bool(candidates[0][16]) else "field_removed")
        )
        return ObservedFieldEvidence.create(
            mapping=mapping,
            binding=previous.binding,
            present=False,
            candidate_count=0,
            normalized_type=None,
            nullable=None,
            is_part_of_key=None,
            asset_metadata_fingerprint=None,
            field_metadata_fingerprint=None,
            field_definition_fingerprint=None,
            field_terms_fingerprint=None,
            reason_code=reason,
        )
    return _present_field(
        workspace_id,
        mapping,
        candidates[0],
        explicit_selection=previous.explicit_selection,
    )


def _present_field(
    workspace_id: str,
    mapping: GovernedMappingRef,
    row: tuple[object, ...],
    *,
    candidate_count: int = 1,
    explicit_selection: SemanticBindingSelection | None = None,
) -> ObservedFieldEvidence:
    normalized_type = PhysicalValueType(str(row[10]))
    locator = CatalogFieldLocator(
        asset=CatalogAssetLocator(
            workspace_id=workspace_id,
            connection_id=CatalogConnectionId(str(row[1])),
            asset_id=CatalogAssetId(str(row[5])),
        ),
        field_path=_string_tuple(row[9]),
    )
    binding = GovernedResourceBinding.create(
        mapping=mapping,
        locator=locator,
        catalog_generation=_integer(row[2]),
        catalog_generation_fingerprint=str(row[3]),
        asset_metadata_fingerprint=str(row[7]),
        field_metadata_fingerprint=str(row[13]),
        field_definition_fingerprint=str(row[14]),
        field_terms_fingerprint=str(row[15]),
    )
    return ObservedFieldEvidence.create(
        mapping=mapping,
        binding=binding,
        explicit_selection=explicit_selection,
        present=True,
        candidate_count=candidate_count,
        normalized_type=normalized_type,
        nullable=bool(row[11]),
        is_part_of_key=bool(row[12]),
        asset_metadata_fingerprint=str(row[7]),
        field_metadata_fingerprint=str(row[13]),
        field_definition_fingerprint=str(row[14]),
        field_terms_fingerprint=str(row[15]),
        reason_code=None,
    )


def _physical_parts(mapping: GovernedMappingRef) -> tuple[str, tuple[str, ...]]:
    parts = mapping.physical_field.root.split(".")
    if len(parts) < 3:
        raise ValueError("governed physical field is not dataset-qualified")
    return ".".join(parts[:2]), tuple(parts[2:])


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("catalog evidence integer is invalid")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("catalog evidence field path is invalid")
    return tuple(value)
