"""Lazy heterogeneous catalog source used for deterministic scale validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    MAX_SOURCE_ASSETS_PER_PAGE,
    MAX_SOURCE_FIELDS_PER_PAGE,
    MAX_TENANT_ASSET_LIMIT,
    CatalogAssetId,
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshMode,
    CatalogSourceAsset,
    CatalogSourceChange,
    CatalogSourceChangeKind,
    CatalogSourceField,
    CatalogSourcePage,
)
from schemabridge.domain.connectors import normalize_postgres_native_type

_CHECKPOINT = re.compile(r"^synthetic-v2\.(?P<offset>[0-9]{1,10})\.(?P<sequence>[0-9]{1,10})$")
_SCHEMAS = ("bank", "commerce", "crm", "fulfillment", "legacy", "support")
_TABLE_STEMS = (
    "account_holders",
    "accounts",
    "clients",
    "contracts",
    "customers",
    "orders",
    "registrations",
    "shipments",
    "tickets",
)
_FIELD_TEMPLATES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("contract_id", "varchar(20)", "Contract identifier preserving leading zeroes.", False, True),
    ("gf_contract_id", "bigint", "Legacy numeric representation of a contract key.", True, False),
    ("customer_key", "varchar(32)", "Governed customer business key.", False, True),
    ("client_id", "double precision", "Unsafe legacy client identifier candidate.", True, False),
    (
        "registration_date",
        "timestamp with time zone",
        "Timestamp when the record was registered.",
        True,
        False,
    ),
    ("holder_type", "varchar(30)", "Role of the holder on the account.", True, False),
    (
        "amount",
        "numeric(18,2)",
        "Monetary amount in the declared transaction currency.",
        True,
        False,
    ),
    ("is_active", "boolean", "Whether the source record is currently active.", False, False),
    ("payload_version", "smallint", "Source payload contract version.", False, False),
    ("notes", "text", "Free-form synthetic test description.", True, False),
    ("event_day", "date", "Calendar date associated with the source event.", True, False),
    ("updated_at", "timestamp", "Source-local update timestamp without timezone.", True, False),
)
_EXTENDED_NATIVE_TYPES = (
    "varchar(20)",
    "bigint",
    "numeric(38,0)",
    "uuid",
    "double precision",
)
_MAX_SYNTHETIC_FIELDS_PER_ASSET = MAX_SOURCE_FIELDS_PER_PAGE // MAX_SOURCE_ASSETS_PER_PAGE


@dataclass(frozen=True, slots=True)
class SyntheticDeltaSpecification:
    """Algorithmic delta counts; no event collection is retained."""

    update_count: int = 0
    addition_count: int = 0
    deletion_count: int = 0

    def __post_init__(self) -> None:
        if min(self.update_count, self.addition_count, self.deletion_count) < 0:
            raise ValueError("synthetic delta counts cannot be negative")
        if self.deletion_count > 1_000_000:
            raise ValueError("synthetic deletion count is invalid")

    @property
    def event_count(self) -> int:
        return self.update_count + self.addition_count + self.deletion_count


@dataclass(frozen=True, slots=True)
class SyntheticCatalogSpecification:
    """One lazily generated connection inventory."""

    asset_count: int
    minimum_fields: int = 3
    maximum_fields: int = 12
    wide_asset_every: int | None = None
    wide_field_count: int | None = None
    delta: SyntheticDeltaSpecification | None = None
    database_name: str = "synthetic"

    def __post_init__(self) -> None:
        if not 0 <= self.asset_count <= MAX_TENANT_ASSET_LIMIT:
            raise ValueError("synthetic asset count is invalid")
        if not 1 <= self.minimum_fields <= self.maximum_fields <= _MAX_SYNTHETIC_FIELDS_PER_ASSET:
            raise ValueError("synthetic field-count range is invalid")
        if (self.wide_asset_every is None) is not (self.wide_field_count is None):
            raise ValueError("synthetic wide-asset profile is incomplete")
        if (
            not self.database_name
            or self.database_name.strip() != self.database_name
            or len(self.database_name) > 200
            or len(self.database_name.encode("utf-8")) > 400
        ):
            raise ValueError("synthetic database name is invalid")
        if self.wide_asset_every is not None and (
            not 1 <= self.wide_asset_every <= MAX_TENANT_ASSET_LIMIT
            or self.wide_field_count is None
            or not self.maximum_fields < self.wide_field_count <= _MAX_SYNTHETIC_FIELDS_PER_ASSET
        ):
            raise ValueError("synthetic wide-asset profile is invalid")
        if self.delta is not None and (
            self.delta.update_count > self.asset_count
            or self.delta.deletion_count > self.asset_count - self.delta.update_count
        ):
            raise ValueError("synthetic delta exceeds its base inventory")


@dataclass(slots=True)
class LazySyntheticCatalogSource:
    """Generate only the requested page for any configured connection cardinality."""

    specifications: Mapping[CatalogConnectionId, SyntheticCatalogSpecification]

    @property
    def source_label(self) -> str:
        return "synthetic:lazy-catalog-v2"

    def read_page(
        self,
        route: CatalogConnectionRoute,
        *,
        mode: CatalogRefreshMode,
        checkpoint: str | None,
        page_size: int,
    ) -> CatalogSourcePage:
        if (
            route.kind is not CatalogConnectionKind.SYNTHETIC
            or route.status is not CatalogConnectionStatus.ENABLED
        ):
            raise _resource_unavailable()
        specification = self.specifications.get(route.connection_id)
        if specification is None:
            raise _resource_unavailable()
        if not 1 <= page_size <= MAX_SOURCE_ASSETS_PER_PAGE:
            raise ValueError("synthetic source page size must be between one and fifty")
        offset, sequence = _decode_checkpoint(checkpoint)
        total = (
            specification.asset_count
            if mode is CatalogRefreshMode.FULL
            else _delta_total(specification)
        )
        if offset > total:
            raise _invalid_response()
        end = min(offset + page_size, total)
        if mode is CatalogRefreshMode.FULL:
            changes = tuple(
                CatalogSourceChange(
                    kind=CatalogSourceChangeKind.UPSERT_ASSET,
                    asset=_asset(specification, index),
                )
                for index in range(offset, end)
            )
        else:
            changes = tuple(_delta_change(specification, index) for index in range(offset, end))
        source_complete = end == total
        next_checkpoint = None if source_complete else f"synthetic-v2.{end}.{sequence + 1}"
        return CatalogSourcePage.create(
            mode=mode,
            sequence=sequence,
            changes=changes,
            next_checkpoint=next_checkpoint,
            source_complete=source_complete,
        )


def _decode_checkpoint(checkpoint: str | None) -> tuple[int, int]:
    if checkpoint is None:
        return 0, 1
    matched = _CHECKPOINT.fullmatch(checkpoint)
    if matched is None:
        raise _invalid_response()
    offset = int(matched.group("offset"))
    sequence = int(matched.group("sequence"))
    if sequence < 2:
        raise _invalid_response()
    return offset, sequence


def _delta_total(specification: SyntheticCatalogSpecification) -> int:
    if specification.delta is None:
        raise CatalogInventoryError(
            CatalogInventoryErrorCode.DELTA_UNSUPPORTED,
            "catalog delta is unavailable",
        )
    return specification.delta.event_count


def _asset(
    specification: SyntheticCatalogSpecification,
    index: int,
    *,
    changed: bool = False,
) -> CatalogSourceAsset:
    schema = _SCHEMAS[index % len(_SCHEMAS)]
    stem = _TABLE_STEMS[index % len(_TABLE_STEMS)]
    qualified_name = f"{schema}.{stem}_{index:08d}"
    field_count = specification.minimum_fields + (
        index % (specification.maximum_fields - specification.minimum_fields + 1)
    )
    if (
        specification.wide_asset_every is not None
        and (index + 1) % specification.wide_asset_every == 0
    ):
        assert specification.wide_field_count is not None
        field_count = specification.wide_field_count
    fields = tuple(
        sorted(
            (_field(index=index, position=position) for position in range(field_count)),
            key=lambda field: field.field_path,
        )
    )
    description = (
        f"Synthetic heterogeneous table {index}."
        if not changed
        else f"Synthetic heterogeneous table {index}, updated by the delta feed."
    )
    asset_id = CatalogAssetId(f"synthetic-asset-{index:08d}")
    display_name = f"{stem}_{index:08d}"
    payload = {
        "asset_id": asset_id.root,
        "qualified_name": qualified_name,
        "display_name": display_name,
        "environment": "PROD",
        "database_name": specification.database_name,
        "schema_name": schema,
        "description": description,
        "platform": "postgres",
    }
    return CatalogSourceAsset(
        asset_id=asset_id,
        qualified_name=qualified_name,
        display_name=display_name,
        platform="postgres",
        environment="PROD",
        database_name=specification.database_name,
        schema_name=schema,
        description=description,
        fields=fields,
        metadata_fingerprint=_fingerprint(payload),
    )


def _field(*, index: int, position: int) -> CatalogSourceField:
    field_path: tuple[str, ...]
    native_type: str | None
    description: str | None
    nullable: bool | None
    is_key: bool | None
    tags: tuple[str, ...]
    if position < len(_FIELD_TEMPLATES):
        template = _FIELD_TEMPLATES[(index + position) % len(_FIELD_TEMPLATES)]
        name, native_type, description, nullable, is_key = template
        field_path = (name if position == 0 else f"{name}_{position}",)
        tags = ("synthetic",)
    else:
        native_type = _EXTENDED_NATIVE_TYPES[index % len(_EXTENDED_NATIVE_TYPES)]
        description = (
            None
            if (index + position) % 4 == 0
            else "Shared identifier deliberately represented with heterogeneous source types."
        )
        nullable = None if (index + position) % 5 == 0 else bool(index % 2)
        is_key = position % 11 == 0
        if position % 3 == 0:
            field_path = ("payload", "identifiers", f"shared_identifier_{position}")
        elif position % 3 == 1:
            field_path = (f"dirección_envío_{position}",)
        else:
            field_path = (f"shared_identifier_{position}",)
        tags = ("heterogeneous", "synthetic")
    normalized_type = normalize_postgres_native_type(native_type).normalized_type
    payload = {
        "description": description,
        "field_path": field_path,
        "is_part_of_key": is_key,
        "native_type": native_type,
        "normalized_type": normalized_type.value,
        "nullable": nullable,
        "tags": tags,
        "glossary_terms": (),
    }
    return CatalogSourceField(
        field_path=field_path,
        native_type=native_type,
        normalized_type=normalized_type,
        description=description,
        nullable=nullable,
        is_part_of_key=is_key,
        tags=tags,
        glossary_terms=(),
        metadata_fingerprint=_fingerprint(payload),
    )


def _delta_change(
    specification: SyntheticCatalogSpecification,
    event_index: int,
) -> CatalogSourceChange:
    delta = specification.delta
    if delta is None:
        raise _invalid_response()
    if event_index < delta.update_count:
        return CatalogSourceChange(
            kind=CatalogSourceChangeKind.UPSERT_ASSET,
            asset=_asset(specification, event_index, changed=True),
        )
    addition_offset = event_index - delta.update_count
    if addition_offset < delta.addition_count:
        return CatalogSourceChange(
            kind=CatalogSourceChangeKind.UPSERT_ASSET,
            asset=_asset(
                specification,
                specification.asset_count + addition_offset,
            ),
        )
    deletion_offset = addition_offset - delta.addition_count
    delete_index = specification.asset_count - 1 - deletion_offset
    return CatalogSourceChange(
        kind=CatalogSourceChangeKind.DELETE_ASSET,
        asset_id=CatalogAssetId(f"synthetic-asset-{delete_index:08d}"),
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resource_unavailable() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.RESOURCE_UNAVAILABLE,
        "catalog source is unavailable",
    )


def _invalid_response() -> CatalogInventoryError:
    return CatalogInventoryError(
        CatalogInventoryErrorCode.INVALID_RESPONSE,
        "catalog source response is invalid",
    )
