"""Explicit synthetic governed-field search used by local demos and deterministic evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

from schemabridge.adapters.query_studio.recorded_catalog_stream import (
    RECORDED_CATALOG_CONNECTION_ID,
    RecordedCatalogStreamError,
    iter_recorded_catalog_assets,
    recorded_catalog_sha256,
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
from schemabridge.domain.query_studio import (
    ExecutableEvidenceStatus,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchPage,
    QueryStudioScopeSnapshot,
    query_studio_fingerprint,
)
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
    score_governed_description,
)
from schemabridge.domain.semantic_registry import (
    GovernedFieldMapping,
    ScopedSemanticRegistrySnapshot,
)

RECORDED_MATCHER_VERSION = GOVERNED_DESCRIPTION_MATCHER_VERSION


class _CatalogField(TypedDict):
    native_type: str | None
    description: str | None
    nullable: bool | None
    is_part_of_key: bool | None
    tags: tuple[str, ...]
    glossary_terms: tuple[str, ...]


class _CatalogAsset(TypedDict):
    description: str | None
    fields: dict[str, _CatalogField]


class RecordedGovernedBindingFactsSearch:
    """Search one immutable synthetic registry/catalog pair without service I/O."""

    def __init__(
        self,
        scoped_registry: ScopedSemanticRegistrySnapshot,
        catalog_snapshot_path: Path,
    ) -> None:
        self._registry = scoped_registry
        self._catalog_path = catalog_snapshot_path.resolve()
        self._assets = _load_governed_catalog(
            self._catalog_path,
            scoped_registry.registry.mapping_set.mappings,
        )
        catalog_digest = recorded_catalog_sha256(self._catalog_path)
        pointer_generation = scoped_registry.activation_generation or 1
        pointer_fingerprint = (
            scoped_registry.active_pointer_fingerprint
            or query_studio_fingerprint(
                {
                    "mode": "recorded",
                    "registry_fingerprint": scoped_registry.registry.fingerprint,
                    "pointer_generation": pointer_generation,
                }
            )
        )
        self._scope = QueryStudioScopeSnapshot(
            scope=scoped_registry.scope,
            registry_version=scoped_registry.registry.version,
            registry_fingerprint=scoped_registry.registry.fingerprint,
            pointer_generation=pointer_generation,
            pointer_fingerprint=pointer_fingerprint,
            evidence_head_revision=1,
            evidence_baseline_revision=1,
            evidence_baseline_fingerprint=query_studio_fingerprint(
                {
                    "mode": "recorded",
                    "registry_fingerprint": scoped_registry.registry.fingerprint,
                    "evidence": "current",
                }
            ),
            catalog_generation_vector_fingerprint=query_studio_fingerprint(
                {"catalog_sha256": catalog_digest, "generation": 1}
            ),
        )

    @property
    def scope(self) -> QueryStudioScopeSnapshot:
        return self._scope

    def search(self, request: GovernedBindingFactsRequest) -> GovernedFieldSearchPage:
        if request.scope != self._scope.scope:
            raise _unavailable(QueryStudioPortErrorCode.RESOURCE_UNAVAILABLE)
        if request.expected_scope is not None and request.expected_scope != self._scope:
            raise _unavailable(QueryStudioPortErrorCode.SCOPE_CHANGED)
        allowed = {value.root for value in request.filters.logical_fields}
        bindings = tuple(
            binding
            for governed in self._registry.registry.mapping_set.mappings
            if (
                not request.filters.restrict_logical_fields
                or governed.mapping.logical_field.root in allowed
            )
            if (
                binding := self._binding(
                    governed,
                    request.query.root if request.query is not None else None,
                )
            )
            is not None
        )
        ordered = tuple(
            sorted(
                bindings,
                key=lambda item: (
                    -item.signals.total,
                    item.logical_field.root,
                    item.binding_id,
                ),
            )
        )
        if request.after is not None:
            if request.after.scope_fingerprint != self._scope.fingerprint:
                raise _unavailable(QueryStudioPortErrorCode.SCOPE_CHANGED)
            ordered = tuple(
                item
                for item in ordered
                if (
                    -item.signals.total,
                    item.logical_field.root,
                    item.binding_id,
                )
                > request.after.sort_tuple
            )
        rows = ordered[: request.page_size + 1]
        items = rows[: request.page_size]
        rows_read = len(rows)
        logical_fingerprint = request.continuation_request_fingerprint
        facts_fingerprint = request.request_fingerprint
        next_key = (
            items[-1].search_key(
                self._scope.fingerprint,
                logical_fingerprint,
                facts_fingerprint,
            )
            if rows_read == request.page_size + 1
            else None
        )
        return GovernedFieldSearchPage(
            scope=self._scope,
            request_fingerprint=logical_fingerprint,
            binding_facts_fingerprint=facts_fingerprint,
            items=items,
            page_size=request.page_size,
            rows_read=rows_read,
            next_key=next_key,
        )

    def _binding(
        self,
        governed: GovernedFieldMapping,
        query: str | None,
    ) -> GovernedFieldBinding | None:
        mapping = governed.mapping
        parts = mapping.physical_field.root.split(".")
        if len(parts) < 3:
            raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE)
        dataset = ".".join(parts[:2])
        field_path = tuple(parts[2:])
        asset = self._assets.get(dataset)
        if asset is None:
            raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE)
        field = asset["fields"].get(".".join(field_path))
        if field is None:
            raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE)
        logical_field = self._registry.registry.logical_context.field_index().get(
            mapping.logical_field.root
        )
        logical_model_name = mapping.logical_field.root.split(".", 1)[0]
        logical_model = self._registry.registry.logical_context.model_index().get(
            logical_model_name
        )
        approval_decision_id = governed.approval_decision_id
        if logical_field is None or logical_model is None or approval_decision_id is None:
            raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE)
        signals = score_governed_description(
            query,
            logical_field=mapping.logical_field.root,
            model_description=logical_model.description,
            field_definition=logical_field.definition,
            role=logical_field.role,
            canonical_type=logical_field.canonical_type,
            allowed_values=logical_field.allowed_values,
            physical_field=mapping.physical_field.root,
            physical_definitions=tuple(
                value
                for value in (
                    asset.get("description"),
                    field.get("description"),
                )
                if isinstance(value, str) and value.strip()
            ),
            native_types=((field["native_type"],) if field["native_type"] is not None else ()),
            taxonomy=tuple(field["tags"]) + tuple(field["glossary_terms"]),
        )
        if query is not None and signals.total == 0:
            return None
        binding_id = (
            "binding-"
            + query_studio_fingerprint(
                {
                    "logical_field": mapping.logical_field.root,
                    "physical_field": mapping.physical_field.root,
                    "mapping_version": mapping.version,
                }
            )[:40]
        )
        binding_fingerprint = query_studio_fingerprint(
            {
                "binding_id": binding_id,
                "registry_fingerprint": self._registry.registry.fingerprint,
                "mapping": mapping.model_dump(mode="json"),
                "approval_decision_id": approval_decision_id,
                "physical_type": governed.physical_type.value,
            }
        )
        asset_metadata = {
            "dataset": dataset,
            "description": asset.get("description"),
            "generation": 1,
        }
        field_metadata = {
            "field_path": field_path,
            "native_type": field.get("native_type"),
            "description": field.get("description"),
            "nullable": field.get("nullable"),
            "is_part_of_key": field.get("is_part_of_key"),
            "tags": field["tags"],
            "glossary_terms": field["glossary_terms"],
        }
        return GovernedFieldBinding(
            binding_id=binding_id,
            binding_fingerprint=binding_fingerprint,
            logical_field=mapping.logical_field,
            physical_field=mapping.physical_field,
            locator=CatalogFieldLocator(
                asset=CatalogAssetLocator(
                    workspace_id=self._scope.scope.workspace_id,
                    connection_id=CatalogConnectionId(RECORDED_CATALOG_CONNECTION_ID),
                    asset_id=CatalogAssetId(dataset),
                ),
                field_path=field_path,
            ),
            mapping_version=mapping.version,
            mapping_approval_decision_id=approval_decision_id,
            physical_type=governed.physical_type,
            evidence_status=ExecutableEvidenceStatus.CURRENT,
            catalog_generation=1,
            catalog_generation_fingerprint=query_studio_fingerprint(
                {"catalog": self._scope.catalog_generation_vector_fingerprint, "generation": 1}
            ),
            asset_qualified_name=dataset,
            asset_metadata_fingerprint=query_studio_fingerprint(asset_metadata),
            field_metadata_fingerprint=query_studio_fingerprint(field_metadata),
            field_definition_fingerprint=query_studio_fingerprint(
                {"definition": field.get("description")}
            ),
            field_terms_fingerprint=query_studio_fingerprint(
                {
                    "tags": field["tags"],
                    "glossary_terms": field["glossary_terms"],
                }
            ),
            native_type=field.get("native_type"),
            definition=field.get("description"),
            nullable=field.get("nullable"),
            is_part_of_key=field.get("is_part_of_key"),
            tags=tuple(field["tags"]),
            glossary_terms=tuple(field["glossary_terms"]),
            signals=signals,
        )


def _load_governed_catalog(
    path: Path,
    mappings: tuple[GovernedFieldMapping, ...],
) -> dict[str, _CatalogAsset]:
    wanted: dict[str, set[str]] = {}
    for governed in mappings:
        parts = governed.mapping.physical_field.root.split(".")
        if len(parts) < 3:
            raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE)
        wanted.setdefault(".".join(parts[:2]), set()).add(".".join(parts[2:]))
    result: dict[str, _CatalogAsset] = {}
    try:
        for streamed in iter_recorded_catalog_assets(path):
            asset_payload = streamed.payload
            dataset = asset_payload.get("dataset")
            raw_fields = asset_payload.get("fields")
            if not isinstance(dataset, str) or not isinstance(raw_fields, list):
                raise TypeError("catalog asset shape is invalid")
            required_fields = wanted.get(dataset)
            if required_fields is None:
                continue
            if dataset in result:
                raise TypeError("catalog dataset identity is duplicated")
            fields: dict[str, _CatalogField] = {}
            for raw_field in raw_fields:
                if not isinstance(raw_field, dict):
                    raise TypeError("catalog field shape is invalid")
                field_payload = cast(dict[str, object], raw_field)
                field_path = field_payload.get("field_path")
                if not isinstance(field_path, str) or not field_path.strip():
                    raise TypeError("catalog field shape is invalid")
                if field_path not in required_fields:
                    continue
                if field_path in fields:
                    raise TypeError("catalog field identity is duplicated")
                fields[field_path] = _CatalogField(
                    native_type=_optional_text(field_payload.get("native_type")),
                    description=_optional_text(field_payload.get("description")),
                    nullable=_optional_bool(field_payload.get("nullable")),
                    is_part_of_key=_optional_bool(field_payload.get("is_part_of_key")),
                    tags=_text_tuple(field_payload.get("tags")),
                    glossary_terms=_text_tuple(field_payload.get("glossary_terms")),
                )
            result[dataset] = {
                "description": _optional_text(asset_payload.get("description")),
                "fields": fields,
            }
            if set(result) == set(wanted) and all(
                set(result[asset]["fields"]) == field_paths for asset, field_paths in wanted.items()
            ):
                break
        if set(result) != set(wanted) or any(
            set(result[asset]["fields"]) != field_paths
            for asset, field_paths in wanted.items()
            if asset in result
        ):
            raise TypeError("governed catalog projection is incomplete")
        return result
    except (
        OSError,
        RecordedCatalogStreamError,
        UnicodeError,
        KeyError,
        TypeError,
    ) as error:
        raise _unavailable(QueryStudioPortErrorCode.INVALID_RESPONSE) from error


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError("catalog text is invalid")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise TypeError("catalog boolean is invalid")


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("catalog term collection is invalid")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TypeError("catalog term is invalid")
    result = tuple(cast(list[str], value))
    if len(result) != len(set(result)):
        raise TypeError("catalog terms are duplicated")
    return result


def _unavailable(code: QueryStudioPortErrorCode) -> QueryStudioPortError:
    return QueryStudioPortError(code, "recorded governed field evidence is unavailable")


__all__ = [
    "RECORDED_MATCHER_VERSION",
    "RecordedGovernedBindingFactsSearch",
]
