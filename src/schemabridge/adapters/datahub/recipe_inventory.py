"""Bounded, mutation-free DataHub inventory of current query-recipe markers."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError

from schemabridge.application.ports.semantic_dependency_sources import (
    MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE,
    CurrentQueryRecipeDocument,
    SemanticDependencySourceError,
    SemanticDependencySourceErrorCode,
    SemanticDependencySourcePage,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.recipes import QueryRecipe
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)

_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_SCROLL_ID_BYTES = 1_024
_MAX_DOCUMENT_TEXT_BYTES = 32 * 1024
_MAX_RELATED_ASSETS = 3
_PRINTABLE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_RECIPE_DOCUMENT = re.compile(
    r"^urn:li:document:schemabridge-query-recipe-"
    r"(?:(?P<scope>[0-9a-f]{32})-)?(?P<intent>[0-9a-f]{32})-"
    r"(?P<marker>current|v[1-9][0-9]*)$"
)
_RECIPE_PROPERTIES = frozenset(
    {
        "schemabridge.queryRecipe",
        "schemabridge.recipeFingerprint",
        "schemabridge.recipeContentFingerprint",
        "schemabridge.intentFingerprint",
        "schemabridge.contextFingerprint",
        "schemabridge.planFingerprint",
        "schemabridge.queryFingerprint",
        "schemabridge.compilerVersion",
        "schemabridge.approvalId",
        "schemabridge.approvedBy",
        "schemabridge.publishedAt",
        "schemabridge.versionedDocumentUrn",
        "schemabridge.publicationAudit",
    }
)
_SCROLL_QUERY = """\
query SchemaBridgeQueryRecipeScroll($input: ScrollAcrossEntitiesInput!) {
  scrollAcrossEntities(input: $input) {
    nextScrollId
    count
    total
    searchResults {
      entity {
        urn
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class DataHubQueryRecipeInventoryConfig:
    """Read-only DataHub endpoint settings; the bearer is never represented."""

    server: str
    token: str = field(repr=False)
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.server)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DataHub query-recipe inventory server is invalid")
        normalized = self.server.rstrip("/")
        object.__setattr__(self, "server", normalized)
        if (
            not self.token
            or self.token != self.token.strip()
            or len(self.token.encode("utf-8")) > 16 * 1024
            or _PRINTABLE.fullmatch(self.token) is None
        ):
            raise ValueError("DataHub query-recipe inventory credential is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not 0.1 <= self.timeout_seconds <= 30.0
            or isinstance(self.max_response_bytes, bool)
            or not 1_024 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("DataHub query-recipe inventory bounds are invalid")


@dataclass(frozen=True, slots=True)
class DataHubQueryRecipeInventoryRequest:
    """One injectable bounded HTTP request."""

    method: str
    endpoint: str
    token: str = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES


@dataclass(frozen=True, slots=True)
class DataHubQueryRecipeInventoryResponse:
    """One bounded transport response."""

    status_code: int
    body: bytes = field(repr=False)


class DataHubQueryRecipeInventoryTransport(Protocol):
    """Minimal read-only HTTP transport."""

    def execute(
        self,
        request: DataHubQueryRecipeInventoryRequest,
    ) -> DataHubQueryRecipeInventoryResponse:
        """Execute one GET or POST without following redirects."""


@dataclass(frozen=True, slots=True)
class _ValidatedDocument:
    properties: dict[str, str]
    related_assets: tuple[str, ...]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True, slots=True)
class UrllibDataHubQueryRecipeInventoryTransport:
    """Standard-library transport with per-response byte and timeout bounds."""

    def execute(
        self,
        request: DataHubQueryRecipeInventoryRequest,
    ) -> DataHubQueryRecipeInventoryResponse:
        headers = {
            "Authorization": f"Bearer {request.token}",
            "Accept": "application/json",
        }
        if request.body is not None:
            headers["Content-Type"] = "application/json"
        http_request = urllib.request.Request(
            request.endpoint,
            data=request.body,
            headers=headers,
            method=request.method,
        )
        opener = urllib.request.build_opener(_RejectRedirects())
        try:
            with opener.open(http_request, timeout=request.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                body = response.read(request.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read(request.max_response_bytes + 1)
        if len(body) > request.max_response_bytes:
            raise ValueError("DataHub query-recipe response exceeds its byte bound")
        return DataHubQueryRecipeInventoryResponse(status_code=status, body=body)


@dataclass(slots=True)
class DataHubQueryRecipeInventory:
    """Discover reserved recipe documents with GraphQL, then validate exact aspects."""

    config: DataHubQueryRecipeInventoryConfig
    transport: DataHubQueryRecipeInventoryTransport = field(
        default_factory=UrllibDataHubQueryRecipeInventoryTransport,
        repr=False,
    )

    def pages(
        self,
        scope: SemanticRegistryScope,
        *,
        page_size: int,
    ) -> Iterator[SemanticDependencySourcePage[CurrentQueryRecipeDocument]]:
        """Yield every current marker while traversing the complete reserved namespace."""

        if not isinstance(scope, SemanticRegistryScope):
            raise ValueError("semantic registry scope is invalid")
        if not 1 <= page_size <= MAX_SEMANTIC_DEPENDENCY_PAGE_SIZE:
            raise ValueError("query-recipe inventory page size must be between 1 and 50")

        scroll_id: str | None = None
        previous_scroll_id: str | None = None
        last_urn: str | None = None
        expected_total: int | None = None
        seen = 0
        sequence = 1
        scope_fingerprint = semantic_registry_scope_fingerprint(scope)
        try:
            while True:
                root = self._scroll(
                    page_size=page_size,
                    scroll_id=scroll_id,
                    scope_fingerprint=scope_fingerprint,
                )
                raw_results = _sequence(root, "searchResults")
                count = _nonnegative_integer(root, "count")
                total = _nonnegative_integer(root, "total")
                next_scroll_id = _optional_scroll_id(root.get("nextScrollId"))
                if (
                    len(raw_results) > page_size
                    or count < len(raw_results)
                    or count > page_size
                    or (expected_total is not None and total != expected_total)
                ):
                    raise ValueError("DataHub query-recipe scroll counts are invalid")
                expected_total = total
                urns = tuple(_search_result_urn(item) for item in raw_results)
                if urns != tuple(sorted(set(urns))):
                    raise ValueError("DataHub query-recipe scroll order is invalid")
                if last_urn is not None and urns and urns[0] <= last_urn:
                    raise ValueError("DataHub query-recipe scroll did not advance")
                for urn in urns:
                    if _RECIPE_DOCUMENT.fullmatch(urn) is None:
                        raise ValueError("DataHub reserved query-recipe identity is invalid")

                seen_after = seen + len(urns)
                if seen_after > total:
                    raise ValueError("DataHub query-recipe scroll exceeded its total")
                eof = seen_after == total
                if not eof and (
                    not urns
                    or next_scroll_id is None
                    or next_scroll_id in (scroll_id, previous_scroll_id)
                ):
                    raise ValueError("DataHub query-recipe scroll stalled")

                current = tuple(self._load_current(urn) for urn in urns if urn.endswith("-current"))
                position = f"datahub:{urns[-1]}" if urns else f"datahub:eof:{seen_after}"
                yield SemanticDependencySourcePage(
                    sequence=sequence,
                    items=current,
                    source_position=position,
                    eof=eof,
                )
                if eof:
                    return
                previous_scroll_id = scroll_id
                scroll_id = next_scroll_id
                last_urn = urns[-1]
                seen = seen_after
                sequence += 1
        except SemanticDependencySourceError:
            raise
        except (ValidationError, KeyError, TypeError, ValueError) as error:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.INVALID_ARTIFACT,
                "DataHub query-recipe inventory returned invalid evidence",
            ) from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.UNAVAILABLE,
                "DataHub query-recipe inventory is unavailable",
            ) from error
        except Exception as error:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.UNAVAILABLE,
                "DataHub query-recipe inventory is unavailable",
            ) from error

    def _scroll(
        self,
        *,
        page_size: int,
        scroll_id: str | None,
        scope_fingerprint: str,
    ) -> dict[str, object]:
        scroll_input: dict[str, object] = {
            "types": ["DOCUMENT"],
            "query": "*",
            "keepAlive": "5m",
            "count": page_size,
            "orFilters": [
                {
                    "and": [
                        {
                            "field": "urn",
                            "values": [
                                "urn:li:document:schemabridge-query-recipe-"
                                f"{scope_fingerprint[:32]}-"
                            ],
                            "condition": "START_WITH",
                            "negated": False,
                        }
                    ]
                }
            ],
            "searchFlags": {
                "skipHighlighting": True,
                "skipAggregates": True,
                "includeSoftDeleted": False,
            },
            "sortInput": {"sortCriteria": [{"field": "urn", "sortOrder": "ASCENDING"}]},
        }
        if scroll_id is not None:
            scroll_input["scrollId"] = scroll_id
        body = json.dumps(
            {
                "query": _SCROLL_QUERY,
                "variables": {"input": scroll_input},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        payload = self._request_json(
            DataHubQueryRecipeInventoryRequest(
                method="POST",
                endpoint=f"{self.config.server}/api/graphql",
                token=self.config.token,
                body=body,
                timeout_seconds=self.config.timeout_seconds,
                max_response_bytes=self.config.max_response_bytes,
            )
        )
        if payload is None:
            raise ValueError("DataHub query-recipe GraphQL response is absent")
        errors = payload.get("errors")
        if errors is not None:
            if _permission_error(errors):
                raise SemanticDependencySourceError(
                    SemanticDependencySourceErrorCode.PERMISSION_DENIED,
                    "DataHub query-recipe inventory is unavailable",
                )
            raise ValueError("DataHub query-recipe GraphQL response contains errors")
        if set(payload) - {"data", "extensions"}:
            raise ValueError("DataHub query-recipe GraphQL envelope is invalid")
        data = payload.get("data")
        if not isinstance(data, dict) or set(data) != {"scrollAcrossEntities"}:
            raise ValueError("DataHub query-recipe GraphQL data is invalid")
        root = data.get("scrollAcrossEntities")
        if not isinstance(root, dict):
            raise ValueError("DataHub query-recipe GraphQL scroll is invalid")
        if set(root) != {"nextScrollId", "count", "total", "searchResults"}:
            raise ValueError("DataHub query-recipe GraphQL scroll shape is invalid")
        return root

    def _load_current(self, urn: str) -> CurrentQueryRecipeDocument:
        document = self._document(urn)
        properties = document.properties
        if set(properties) != _RECIPE_PROPERTIES:
            raise ValueError("DataHub current query-recipe properties are incomplete")
        serialized = properties["schemabridge.queryRecipe"]
        recipe = QueryRecipe.model_validate_json(serialized)
        expected_current = _current_urn(recipe)
        versioned_urn = properties["schemabridge.versionedDocumentUrn"]
        published_at = datetime.fromisoformat(properties["schemabridge.publishedAt"])
        if (
            urn != expected_current
            or properties["schemabridge.recipeFingerprint"] != recipe.fingerprint
            or properties["schemabridge.recipeContentFingerprint"] != recipe.content_fingerprint
            or properties["schemabridge.intentFingerprint"] != recipe.intent_fingerprint
            or properties["schemabridge.contextFingerprint"] != recipe.model_version.fingerprint
            or properties["schemabridge.planFingerprint"] != recipe.plan_fingerprint
            or properties["schemabridge.queryFingerprint"] != recipe.query_fingerprint
            or properties["schemabridge.compilerVersion"] != recipe.compiler_version
            or versioned_urn != _versioned_urn(recipe)
            or not properties["schemabridge.approvalId"].strip()
            or not properties["schemabridge.approvedBy"].strip()
            or published_at.tzinfo is None
            or published_at.utcoffset() is None
            or document.related_assets != tuple(sorted(recipe.linked_asset_urns))
        ):
            raise ValueError("DataHub current query-recipe binding is invalid")
        self._validate_current_audit(
            properties["schemabridge.publicationAudit"],
            recipe=recipe,
            current_urn=urn,
            versioned_urn=versioned_urn,
            approval_id=properties["schemabridge.approvalId"],
            actor=properties["schemabridge.approvedBy"],
            published_at=published_at,
        )
        self._validate_version(recipe, versioned_urn)
        return CurrentQueryRecipeDocument(
            document_urn=urn,
            versioned_document_urn=versioned_urn,
            approval_id=properties["schemabridge.approvalId"],
            published_at=published_at,
            recipe=recipe,
        )

    def _validate_version(self, recipe: QueryRecipe, urn: str) -> None:
        document = self._document(urn)
        properties = document.properties
        if (
            set(properties) != _RECIPE_PROPERTIES
            or properties["schemabridge.recipeFingerprint"] != recipe.fingerprint
            or properties["schemabridge.recipeContentFingerprint"] != recipe.content_fingerprint
            or properties["schemabridge.queryRecipe"] != recipe.model_dump_json()
            or urn != _versioned_urn(recipe)
            or document.related_assets != tuple(sorted(recipe.linked_asset_urns))
        ):
            raise ValueError("DataHub versioned query-recipe binding is invalid")

    def _validate_current_audit(
        self,
        serialized: str,
        *,
        recipe: QueryRecipe,
        current_urn: str,
        versioned_urn: str,
        approval_id: str,
        actor: str,
        published_at: datetime,
    ) -> None:
        raw = json.loads(serialized, object_pairs_hook=_unique_json_pairs)
        if not isinstance(raw, list):
            raise ValueError("DataHub query-recipe publication audit is invalid")
        records = tuple(PublicationTargetAuditRecord.model_validate(item) for item in raw)
        by_operation = {item.operation: item for item in records}
        expected_targets = {
            "versioned_document": versioned_urn,
            "current_marker": current_urn,
        }
        if len(records) != 2 or set(by_operation) != set(expected_targets):
            raise ValueError("DataHub query-recipe publication audit is incomplete")
        if any(
            record.family is not PublicationFamily.RECIPE
            or record.target != expected_targets[record.operation]
            or record.approval_id != approval_id
            or record.actor != actor
            or record.approved_at != published_at
            or record.new_fingerprint != recipe.fingerprint
            or record.outcome
            not in {
                PublicationAuditOutcome.SUCCEEDED,
                PublicationAuditOutcome.ALREADY_CURRENT,
            }
            or record.reason_code is not None
            or record.decision_ids
            for record in records
        ):
            raise ValueError("DataHub query-recipe publication audit binding is invalid")

    def _document(self, urn: str) -> _ValidatedDocument:
        info = self._aspect(urn, "documentInfo", "com.linkedin.knowledge.DocumentInfo")
        if info is None:
            raise ValueError("DataHub query-recipe document info is absent")
        status = self._aspect(
            urn,
            "status",
            "com.linkedin.common.Status",
            allow_absent=True,
        )
        if status is not None and status.get("removed") is not False:
            raise ValueError("DataHub query-recipe document is removed")
        document_status = info.get("status")
        title = info.get("title")
        contents = info.get("contents")
        properties = info.get("customProperties")
        related = info.get("relatedAssets")
        if (
            not isinstance(document_status, dict)
            or document_status.get("state") != "PUBLISHED"
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(contents, dict)
            or set(contents) != {"text"}
            or not isinstance(contents.get("text"), str)
            or not contents["text"].strip()
            or len(contents["text"].encode("utf-8")) > _MAX_DOCUMENT_TEXT_BYTES
            or not isinstance(properties, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in properties.items()
            )
            or not isinstance(related, list)
            or len(related) > _MAX_RELATED_ASSETS
        ):
            raise ValueError("DataHub query-recipe document aspect is invalid")
        assets = tuple(item.get("asset", "") if isinstance(item, dict) else "" for item in related)
        if any(not asset for asset in assets) or len(assets) != len(set(assets)):
            raise ValueError("DataHub query-recipe related assets are invalid")
        return _ValidatedDocument(
            properties=properties,
            related_assets=tuple(sorted(assets)),
        )

    def _aspect(
        self,
        urn: str,
        aspect_name: str,
        expected_type: str,
        *,
        allow_absent: bool = False,
    ) -> dict[str, object] | None:
        encoded_urn = urllib.parse.quote(urn, safe="")
        payload = self._request_json(
            DataHubQueryRecipeInventoryRequest(
                method="GET",
                endpoint=(
                    f"{self.config.server}/aspects/{encoded_urn}?aspect={aspect_name}&version=0"
                ),
                token=self.config.token,
                timeout_seconds=self.config.timeout_seconds,
                max_response_bytes=self.config.max_response_bytes,
            ),
            allow_not_found=allow_absent,
        )
        if payload is None:
            return None
        envelope = payload.get("aspect")
        if not isinstance(envelope, dict) or set(envelope) != {expected_type}:
            raise ValueError("DataHub query-recipe aspect envelope is invalid")
        aspect = envelope.get(expected_type)
        if not isinstance(aspect, dict):
            raise ValueError("DataHub query-recipe aspect payload is invalid")
        return aspect

    def _request_json(
        self,
        request: DataHubQueryRecipeInventoryRequest,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, object] | None:
        try:
            response = self.transport.execute(request)
        except SemanticDependencySourceError:
            raise
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.UNAVAILABLE,
                "DataHub query-recipe inventory is unavailable",
            ) from error
        if len(response.body) > request.max_response_bytes:
            raise ValueError("DataHub query-recipe response exceeds its byte bound")
        if response.status_code in {401, 403}:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.PERMISSION_DENIED,
                "DataHub query-recipe inventory is unavailable",
            )
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise SemanticDependencySourceError(
                SemanticDependencySourceErrorCode.UNAVAILABLE,
                "DataHub query-recipe inventory is unavailable",
            )
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError("DataHub query-recipe inventory response is invalid")
        try:
            payload = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=_unique_json_pairs,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("DataHub query-recipe response is invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("DataHub query-recipe response must be an object")
        return payload


def _current_urn(recipe: QueryRecipe) -> str:
    return f"urn:li:document:schemabridge-query-recipe-{_recipe_identity_prefix(recipe)}-current"


def _versioned_urn(recipe: QueryRecipe) -> str:
    return (
        "urn:li:document:schemabridge-query-recipe-"
        f"{_recipe_identity_prefix(recipe)}-v{recipe.version}"
    )


def _recipe_identity_prefix(recipe: QueryRecipe) -> str:
    binding = recipe.registry_binding
    if binding is None or binding.scope_fingerprint is None:
        return recipe.intent_fingerprint[:32]
    return f"{binding.scope_fingerprint[:32]}-{recipe.intent_fingerprint[:32]}"


def _search_result_urn(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"entity"}:
        raise ValueError("DataHub query-recipe search result is invalid")
    entity = value.get("entity")
    if not isinstance(entity, dict) or set(entity) != {"urn"}:
        raise ValueError("DataHub query-recipe search entity is invalid")
    urn = entity.get("urn")
    if not isinstance(urn, str) or len(urn.encode("utf-8")) > 500:
        raise ValueError("DataHub query-recipe search URN is invalid")
    return urn


def _sequence(value: dict[str, object], key: str) -> list[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValueError("DataHub query-recipe sequence is invalid")
    return result


def _nonnegative_integer(value: dict[str, object], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("DataHub query-recipe count is invalid")
    return result


def _optional_scroll_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_SCROLL_ID_BYTES
        or _PRINTABLE.fullmatch(value) is None
    ):
        raise ValueError("DataHub query-recipe scroll identity is invalid")
    return value


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _permission_error(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        extensions = item.get("extensions")
        if isinstance(extensions, dict) and extensions.get("code") in {
            "FORBIDDEN",
            "UNAUTHENTICATED",
            "UNAUTHORIZED",
        }:
            return True
    return False


__all__ = [
    "DataHubQueryRecipeInventory",
    "DataHubQueryRecipeInventoryConfig",
    "DataHubQueryRecipeInventoryRequest",
    "DataHubQueryRecipeInventoryResponse",
    "DataHubQueryRecipeInventoryTransport",
    "UrllibDataHubQueryRecipeInventoryTransport",
]
