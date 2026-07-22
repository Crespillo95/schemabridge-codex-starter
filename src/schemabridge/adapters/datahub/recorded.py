"""Sanitized recorded-fixture catalog adapter for offline demonstrations."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from schemabridge.adapters.datahub.fake import CatalogRecord, FakeCatalogAdapter
from schemabridge.application.ports.catalog import (
    CatalogAsset,
    CatalogField,
    CatalogInvalidResponseError,
    GovernanceMetadata,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef

_FIXTURE_KIND = "sanitized_catalog_recording"
_FORBIDDEN_KEYS = frozenset({"password", "secret", "token"})


class RecordedCatalogAdapter(FakeCatalogAdapter):
    """Load a checked-in, token-free catalog snapshot into the fake contract."""

    def __init__(self, path: Path) -> None:
        try:
            raw_text = path.read_text(encoding="utf-8")
            decoded: object = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as error:
            raise CatalogInvalidResponseError(
                "load_recording",
                "recorded catalog fixture could not be loaded",
            ) from error
        root = _mapping(decoded)
        if root.get("fixture_kind") != _FIXTURE_KIND:
            raise CatalogInvalidResponseError(
                "load_recording",
                "recorded catalog fixture kind is unsupported",
            )
        if _contains_forbidden_key(root):
            raise CatalogInvalidResponseError(
                "load_recording",
                "recorded catalog fixture contains a forbidden secret-shaped key",
            )
        records = tuple(_record(_mapping(item)) for item in _sequence(root.get("assets")))
        super().__init__(
            records,
            documents_available=False,
            source_label="recorded:sanitized-datahub-m04",
        )


def _record(raw: Mapping[str, object]) -> CatalogRecord:
    operation = "load_recording"
    try:
        dataset = PhysicalDatasetRef(_string(raw, "dataset"))
        governance_raw = _mapping(raw.get("governance", {}))
        governance = GovernanceMetadata(
            owners=_strings(governance_raw.get("owners", [])),
            tags=_strings(governance_raw.get("tags", [])),
            glossary_terms=_strings(governance_raw.get("glossary_terms", [])),
            domain=_optional_string(governance_raw.get("domain")),
        )
        asset = CatalogAsset(
            dataset=dataset,
            urn=_string(raw, "urn"),
            name=_string(raw, "name"),
            platform=_string(raw, "platform"),
            description=_optional_string(raw.get("description")),
            governance=governance,
            subtypes=_strings(raw.get("subtypes", [])),
        )
        fields = tuple(
            _field(dataset, _mapping(raw_field)) for raw_field in _sequence(raw.get("fields"))
        )
        return CatalogRecord(asset=asset, fields=fields)
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise CatalogInvalidResponseError(
            operation,
            "recorded catalog fixture contains invalid metadata",
        ) from error


def _field(dataset: PhysicalDatasetRef, raw: Mapping[str, object]) -> CatalogField:
    path_text = _string(raw, "field_path")
    path = tuple(path_text.split("."))
    nullable = raw.get("nullable")
    is_key = raw.get("is_part_of_key")
    return CatalogField(
        id=PhysicalFieldRef(".".join((dataset.root, *path))),
        dataset=dataset,
        field_path=path,
        native_type=_optional_string(raw.get("native_type")),
        description=_optional_string(raw.get("description")),
        nullable=nullable if isinstance(nullable, bool) else None,
        is_part_of_key=is_key if isinstance(is_key, bool) else None,
        tags=_strings(raw.get("tags", [])),
        glossary_terms=_strings(raw.get("glossary_terms", [])),
    )


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _FORBIDDEN_KEYS or _contains_forbidden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CatalogInvalidResponseError("load_recording", "recorded value must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise CatalogInvalidResponseError("load_recording", "recorded value must be a list")
    return cast(Sequence[object], value)


def _string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise CatalogInvalidResponseError("load_recording", f"recorded value is missing {key}")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogInvalidResponseError("load_recording", "recorded value must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CatalogInvalidResponseError("load_recording", "recorded list contains invalid text")
    return tuple(cast(list[str], value))
