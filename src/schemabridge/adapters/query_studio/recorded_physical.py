"""Bounded, non-executable physical discovery over the synthetic recording."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from schemabridge.adapters.query_studio.recorded_catalog_stream import (
    RECORDED_CATALOG_CONNECTION_ID,
    RecordedCatalogStreamError,
    iter_recorded_catalog_assets,
    recorded_catalog_cardinality,
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
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryCardinality,
    PhysicalDiscoveryCursor,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    query_studio_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)

_CURSOR_PREFIX = "qspd1"
_CURSOR_KEYS = frozenset(
    {
        "asset_position",
        "catalog_sha256",
        "field_position",
        "query_fingerprint",
        "scope_fingerprint",
        "version",
    }
)


@dataclass(frozen=True, slots=True)
class _Position:
    asset: int
    field: int


class RecordedPhysicalFieldDiscovery:
    """Scan one immutable recording with constant memory and a separate signed cursor."""

    def __init__(self, catalog_snapshot_path: Path, signing_key: bytes) -> None:
        if (
            not isinstance(signing_key, bytes)
            or not 32 <= len(signing_key) <= 1_024
            or len(set(signing_key)) < 8
        ):
            raise ValueError("recorded physical discovery signing key is invalid")
        self._catalog_path = catalog_snapshot_path.resolve()
        self._signing_key = bytes(signing_key)

    def search(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        try:
            catalog_digest = recorded_catalog_sha256(self._catalog_path)
            scope_fingerprint = semantic_registry_scope_fingerprint(request.scope)
            query_fingerprint = query_studio_fingerprint(
                {
                    "query": request.query.root if request.query is not None else None,
                    "lane": "recorded_physical_discovery",
                }
            )
            after = (
                self._decode_cursor(
                    request.cursor,
                    catalog_digest=catalog_digest,
                    scope_fingerprint=scope_fingerprint,
                    query_fingerprint=query_fingerprint,
                )
                if request.cursor is not None
                else None
            )
            matches: list[tuple[_Position, PhysicalDiscoveryCandidate]] = []
            for streamed in iter_recorded_catalog_assets(self._catalog_path):
                dataset, fields = _asset_fields(streamed.payload)
                for field_position, field in enumerate(fields):
                    position = _Position(streamed.position, field_position)
                    if after is not None and (
                        position.asset,
                        position.field,
                    ) <= (after.asset, after.field):
                        continue
                    candidate = _candidate(
                        request,
                        dataset=dataset,
                        field=field,
                    )
                    if candidate is None:
                        continue
                    matches.append((position, candidate))
                    if len(matches) == request.page_size + 1:
                        break
                if len(matches) == request.page_size + 1:
                    break

            visible = matches[: request.page_size]
            next_cursor = None
            if len(matches) == request.page_size + 1:
                next_cursor = self._encode_cursor(
                    visible[-1][0],
                    catalog_digest=catalog_digest,
                    scope_fingerprint=scope_fingerprint,
                    query_fingerprint=query_fingerprint,
                )
            return PhysicalFieldDiscoveryPage(
                items=tuple(candidate for _position, candidate in visible),
                page_size=request.page_size,
                next_cursor=next_cursor,
            )
        except QueryStudioPortError:
            raise
        except (
            OSError,
            RecordedCatalogStreamError,
            UnicodeError,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "recorded physical discovery is unavailable",
            ) from error

    def inspect_cardinality(
        self,
        scope: SemanticRegistryScope,
    ) -> PhysicalDiscoveryCardinality:
        """Return recording counts through the same non-executable physical lane."""

        try:
            cardinality = recorded_catalog_cardinality(self._catalog_path)
            return PhysicalDiscoveryCardinality(
                scope=scope,
                catalog_generation_vector_fingerprint=recorded_catalog_sha256(self._catalog_path),
                connection_count=1 if cardinality.asset_count else 0,
                asset_count=cardinality.asset_count,
                field_count=cardinality.field_count,
            )
        except (
            OSError,
            RecordedCatalogStreamError,
            UnicodeError,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "recorded physical discovery cardinality is unavailable",
            ) from error

    def _encode_cursor(
        self,
        position: _Position,
        *,
        catalog_digest: str,
        scope_fingerprint: str,
        query_fingerprint: str,
    ) -> PhysicalDiscoveryCursor:
        payload = {
            "asset_position": position.asset,
            "catalog_sha256": catalog_digest,
            "field_position": position.field,
            "query_fingerprint": query_fingerprint,
            "scope_fingerprint": scope_fingerprint,
            "version": 1,
        }
        encoded = _base64url(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = _base64url(
            hmac.new(
                self._signing_key,
                f"{_CURSOR_PREFIX}.{encoded}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return PhysicalDiscoveryCursor(f"{_CURSOR_PREFIX}.{encoded}.{signature}")

    def _decode_cursor(
        self,
        cursor: PhysicalDiscoveryCursor,
        *,
        catalog_digest: str,
        scope_fingerprint: str,
        query_fingerprint: str,
    ) -> _Position:
        parts = cursor.root.split(".")
        if len(parts) != 3 or parts[0] != _CURSOR_PREFIX:
            raise _invalid_cursor()
        expected = _base64url(
            hmac.new(
                self._signing_key,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected, parts[2]):
            raise _invalid_cursor()
        try:
            value: object = json.loads(_decode_base64url(parts[1]).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise _invalid_cursor() from None
        if not isinstance(value, dict) or frozenset(value) != _CURSOR_KEYS:
            raise _invalid_cursor()
        payload = cast(dict[str, object], value)
        asset_position = payload["asset_position"]
        field_position = payload["field_position"]
        if (
            payload["version"] != 1
            or not _nonnegative_int(asset_position)
            or not _nonnegative_int(field_position)
            or payload["catalog_sha256"] != catalog_digest
            or payload["scope_fingerprint"] != scope_fingerprint
            or payload["query_fingerprint"] != query_fingerprint
        ):
            raise _invalid_cursor()
        return _Position(cast(int, asset_position), cast(int, field_position))


def _asset_fields(payload: object) -> tuple[str, list[object]]:
    if not isinstance(payload, dict):
        raise TypeError("catalog asset is invalid")
    asset = cast(dict[str, object], payload)
    dataset = asset.get("dataset")
    fields = asset.get("fields")
    if not isinstance(dataset, str) or not dataset.strip() or not isinstance(fields, list):
        raise TypeError("catalog asset is invalid")
    return dataset, fields


def _candidate(
    request: PhysicalFieldDiscoveryRequest,
    *,
    dataset: str,
    field: object,
) -> PhysicalDiscoveryCandidate | None:
    if not isinstance(field, dict):
        raise TypeError("catalog field is invalid")
    payload = cast(dict[str, object], field)
    field_path = payload.get("field_path")
    native_type = _optional_text(payload.get("native_type"))
    definition = _optional_text(payload.get("description"))
    tags = _text_tuple(payload.get("tags"))
    glossary_terms = _text_tuple(payload.get("glossary_terms"))
    if not isinstance(field_path, str) or not field_path.strip():
        raise TypeError("catalog field is invalid")
    searchable = " ".join(
        (
            dataset,
            field_path,
            native_type or "",
            definition or "",
            *tags,
            *glossary_terms,
        )
    )
    if request.query is not None and _search_text(request.query.root) not in _search_text(
        searchable
    ):
        return None
    metadata = {
        "asset": dataset,
        "definition": definition,
        "field_path": field_path,
        "glossary_terms": glossary_terms,
        "native_type": native_type,
        "tags": tags,
    }
    return PhysicalDiscoveryCandidate(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=request.scope.workspace_id,
                connection_id=CatalogConnectionId(RECORDED_CATALOG_CONNECTION_ID),
                asset_id=CatalogAssetId(dataset),
            ),
            field_path=tuple(field_path.split(".")),
        ),
        generation=1,
        asset_qualified_name=dataset,
        native_type=native_type,
        definition=definition,
        metadata_fingerprint=query_studio_fingerprint(metadata),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError("catalog text is invalid")
    return value


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("catalog terms are invalid")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TypeError("catalog terms are invalid")
    result = tuple(cast(list[str], value))
    if len(result) != len(set(result)):
        raise TypeError("catalog terms are invalid")
    return result


def _search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _invalid_cursor() -> QueryStudioPortError:
    return QueryStudioPortError(
        QueryStudioPortErrorCode.SCOPE_CHANGED,
        "recorded physical discovery cursor is stale or invalid",
    )


__all__ = ["RecordedPhysicalFieldDiscovery"]
