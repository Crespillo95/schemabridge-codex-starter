"""Bounded-memory reader for the checked-in synthetic catalog recording."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_CHUNK_CHARACTERS = 64 * 1_024
_MAX_PREAMBLE_CHARACTERS = 1 * 1_024 * 1_024
_MAX_ASSET_CHARACTERS = 2 * 1_024 * 1_024
_ASSETS_START = re.compile(r'"assets"\s*:\s*\[')
RECORDED_CATALOG_CONNECTION_ID = "warehouse-primary"


class RecordedCatalogStreamError(ValueError):
    """The trusted synthetic recording does not satisfy its bounded JSON contract."""


@dataclass(frozen=True, slots=True)
class StreamedCatalogAsset:
    position: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RecordedCatalogCardinality:
    """Aggregate recording counts computed without retaining catalog rows."""

    asset_count: int
    field_count: int


def recorded_catalog_sha256(path: Path) -> str:
    """Hash a recording without loading it into process memory."""

    digest = hashlib.sha256()
    try:
        with path.resolve().open("rb") as handle:
            while chunk := handle.read(64 * 1_024):
                digest.update(chunk)
    except OSError as error:
        raise RecordedCatalogStreamError("recorded catalog is unavailable") from error
    return digest.hexdigest()


def recorded_catalog_cardinality(path: Path) -> RecordedCatalogCardinality:
    """Count assets and fields in one bounded-memory traversal."""

    asset_count = 0
    field_count = 0
    for streamed in iter_recorded_catalog_assets(path):
        fields = streamed.payload.get("fields")
        if not isinstance(fields, list):
            raise RecordedCatalogStreamError("recorded catalog asset fields must be an array")
        asset_count += 1
        field_count += len(fields)
    return RecordedCatalogCardinality(
        asset_count=asset_count,
        field_count=field_count,
    )


def iter_recorded_catalog_assets(path: Path) -> Iterator[StreamedCatalogAsset]:
    """Yield one validated top-level asset object at a time."""

    decoder = json.JSONDecoder()
    try:
        with path.resolve().open("r", encoding="utf-8") as handle:
            buffer = ""
            match: re.Match[str] | None = None
            while match is None:
                chunk = handle.read(_CHUNK_CHARACTERS)
                if not chunk:
                    raise RecordedCatalogStreamError("recorded catalog has no assets array")
                buffer += chunk
                if len(buffer) > _MAX_PREAMBLE_CHARACTERS:
                    raise RecordedCatalogStreamError("recorded catalog preamble exceeds its bound")
                match = _ASSETS_START.search(buffer)

            buffer = buffer[match.end() :]
            eof = False
            position = 0
            while True:
                while True:
                    buffer = buffer.lstrip()
                    if buffer.startswith(","):
                        buffer = buffer[1:]
                        continue
                    if buffer or eof:
                        break
                    chunk = handle.read(_CHUNK_CHARACTERS)
                    eof = not chunk
                    buffer += chunk
                if buffer.startswith("]"):
                    return
                if eof and not buffer:
                    raise RecordedCatalogStreamError("recorded catalog assets array is incomplete")

                while True:
                    try:
                        value, consumed = decoder.raw_decode(buffer)
                        break
                    except json.JSONDecodeError as error:
                        chunk = handle.read(_CHUNK_CHARACTERS)
                        if not chunk:
                            raise RecordedCatalogStreamError(
                                "recorded catalog asset JSON is invalid"
                            ) from error
                        buffer += chunk
                        if len(buffer) > _MAX_ASSET_CHARACTERS:
                            raise RecordedCatalogStreamError(
                                "recorded catalog asset exceeds its bound"
                            ) from error
                if not isinstance(value, dict):
                    raise RecordedCatalogStreamError("recorded catalog asset must be an object")
                yield StreamedCatalogAsset(
                    position=position,
                    payload=cast(dict[str, object], value),
                )
                position += 1
                buffer = buffer[consumed:]
    except (OSError, UnicodeError) as error:
        raise RecordedCatalogStreamError("recorded catalog is unavailable") from error


__all__ = [
    "RECORDED_CATALOG_CONNECTION_ID",
    "RecordedCatalogCardinality",
    "RecordedCatalogStreamError",
    "StreamedCatalogAsset",
    "iter_recorded_catalog_assets",
    "recorded_catalog_cardinality",
    "recorded_catalog_sha256",
]
