"""Bounded GraphQL and exact DocumentInfo tests for live recipe inventory."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from schemabridge.adapters.datahub.recipe_inventory import (
    DataHubQueryRecipeInventory,
    DataHubQueryRecipeInventoryConfig,
    DataHubQueryRecipeInventoryRequest,
    DataHubQueryRecipeInventoryResponse,
)
from schemabridge.application.ports.semantic_dependency_sources import (
    SemanticDependencySourceError,
    SemanticDependencySourceErrorCode,
)
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.recipes import QueryRecipe, RecipeRegistryBinding
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    semantic_registry_scope_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 24, 16, 0, tzinfo=UTC)
SCOPE = SemanticRegistryScope(
    workspace_id="workspace-recipe-inventory",
    catalog_scope="synthetic-demo",
    registry_id="synthetic_enterprise",
)


def _recipe() -> QueryRecipe:
    recorded = QueryRecipe.model_validate(
        yaml.safe_load((ROOT / "examples/query-recipe-secondary-holders.yml").read_text())
    )
    return QueryRecipe.create(
        version=recorded.version,
        business_question=recorded.business_question,
        normalized_intent=recorded.normalized_intent,
        validated_request_fingerprint=recorded.validated_request_fingerprint,
        model_version=recorded.model_version,
        registry_binding=RecipeRegistryBinding(
            generation=7,
            pointer_fingerprint="b" * 64,
            scope_fingerprint=semantic_registry_scope_fingerprint(SCOPE),
            registry_version=recorded.model_version.version,
            registry_fingerprint=recorded.model_version.fingerprint,
        ),
        mapping_versions=recorded.mapping_versions,
        join_versions=recorded.join_versions,
        source_schema_fingerprint=recorded.source_schema_fingerprint,
        plan_fingerprint=recorded.plan_fingerprint,
        query_fingerprint=recorded.query_fingerprint,
        compiler_version=recorded.compiler_version,
        validation=recorded.validation,
        limitations=recorded.limitations,
        linked_asset_urns=recorded.linked_asset_urns,
        source_workflow_id=recorded.source_workflow_id,
        created_at=recorded.created_at,
    )


def _urns(recipe: QueryRecipe) -> tuple[str, str]:
    assert recipe.registry_binding is not None
    assert recipe.registry_binding.scope_fingerprint is not None
    prefix = (
        "urn:li:document:schemabridge-query-recipe-"
        f"{recipe.registry_binding.scope_fingerprint[:32]}-"
        f"{recipe.intent_fingerprint[:32]}"
    )
    return f"{prefix}-current", f"{prefix}-v{recipe.version}"


def _audit(recipe: QueryRecipe) -> str:
    current, versioned = _urns(recipe)
    records = (
        PublicationTargetAuditRecord(
            family=PublicationFamily.RECIPE,
            operation="versioned_document",
            target=versioned,
            approval_id="recipe-live-approval",
            actor="recipe-live-publisher",
            approved_at=NOW,
            previous_fingerprint=None,
            new_fingerprint=recipe.fingerprint,
            outcome=PublicationAuditOutcome.SUCCEEDED,
        ),
        PublicationTargetAuditRecord(
            family=PublicationFamily.RECIPE,
            operation="current_marker",
            target=current,
            approval_id="recipe-live-approval",
            actor="recipe-live-publisher",
            approved_at=NOW,
            previous_fingerprint=None,
            new_fingerprint=recipe.fingerprint,
            outcome=PublicationAuditOutcome.SUCCEEDED,
        ),
    )
    return json.dumps(
        [record.model_dump(mode="json") for record in records],
        sort_keys=True,
        separators=(",", ":"),
    )


def _properties(recipe: QueryRecipe, *, invalid_payload: bool) -> dict[str, str]:
    _, versioned = _urns(recipe)
    return {
        "schemabridge.queryRecipe": ("{}" if invalid_payload else recipe.model_dump_json()),
        "schemabridge.recipeFingerprint": recipe.fingerprint,
        "schemabridge.recipeContentFingerprint": recipe.content_fingerprint,
        "schemabridge.intentFingerprint": recipe.intent_fingerprint,
        "schemabridge.contextFingerprint": recipe.model_version.fingerprint,
        "schemabridge.planFingerprint": recipe.plan_fingerprint,
        "schemabridge.queryFingerprint": recipe.query_fingerprint,
        "schemabridge.compilerVersion": recipe.compiler_version,
        "schemabridge.approvalId": "recipe-live-approval",
        "schemabridge.approvedBy": "recipe-live-publisher",
        "schemabridge.publishedAt": NOW.isoformat(),
        "schemabridge.versionedDocumentUrn": versioned,
        "schemabridge.publicationAudit": _audit(recipe),
    }


@dataclass
class _Transport:
    recipe: QueryRecipe
    invalid_payload: bool = False
    missing_status: bool = False
    status_code: int = 200
    requests: list[DataHubQueryRecipeInventoryRequest] = field(default_factory=list)

    def execute(
        self,
        request: DataHubQueryRecipeInventoryRequest,
    ) -> DataHubQueryRecipeInventoryResponse:
        self.requests.append(request)
        if self.status_code != 200:
            return DataHubQueryRecipeInventoryResponse(
                status_code=self.status_code,
                body=b"{}",
            )
        if request.method == "POST":
            assert request.body is not None
            body = json.loads(request.body)
            scroll_id = body["variables"]["input"].get("scrollId")
            current, versioned = _urns(self.recipe)
            urn = current if scroll_id is None else versioned
            payload: object = {
                "data": {
                    "scrollAcrossEntities": {
                        "nextScrollId": "scroll-page-2" if scroll_id is None else None,
                        "count": 1,
                        "total": 2,
                        "searchResults": [{"entity": {"urn": urn}}],
                    }
                }
            }
        else:
            parsed = urllib.parse.urlsplit(request.endpoint)
            encoded_urn = parsed.path.split("/aspects/", 1)[1]
            urn = urllib.parse.unquote(encoded_urn)
            aspect = urllib.parse.parse_qs(parsed.query)["aspect"][0]
            if aspect == "status":
                if self.missing_status:
                    return DataHubQueryRecipeInventoryResponse(
                        status_code=404,
                        body=b"{}",
                    )
                payload = {
                    "aspect": {
                        "com.linkedin.common.Status": {
                            "removed": False,
                        }
                    }
                }
            else:
                payload = {
                    "aspect": {
                        "com.linkedin.knowledge.DocumentInfo": {
                            "status": {"state": "PUBLISHED"},
                            "title": "Synthetic validated query recipe",
                            "contents": {"text": "Synthetic SQL-free recipe context."},
                            "customProperties": _properties(
                                self.recipe,
                                invalid_payload=self.invalid_payload and urn.endswith("-current"),
                            ),
                            "relatedAssets": [
                                {"asset": asset} for asset in sorted(self.recipe.linked_asset_urns)
                            ],
                        }
                    }
                }
        return DataHubQueryRecipeInventoryResponse(
            status_code=200,
            body=json.dumps(payload).encode(),
        )


def _inventory(transport: _Transport) -> DataHubQueryRecipeInventory:
    return DataHubQueryRecipeInventory(
        DataHubQueryRecipeInventoryConfig(
            server="http://127.0.0.1:8080",
            token="synthetic-read-token",
            timeout_seconds=3,
            max_response_bytes=1_048_576,
        ),
        transport=transport,
    )


def test_inventory_scrolls_entire_reserved_namespace_and_returns_only_current_marker() -> None:
    recipe = _recipe()
    transport = _Transport(recipe)

    pages = tuple(_inventory(transport).pages(SCOPE, page_size=1))

    assert tuple((page.sequence, len(page.items), page.eof) for page in pages) == (
        (1, 1, False),
        (2, 0, True),
    )
    assert pages[0].items[0].recipe == recipe
    graph_requests = [request for request in transport.requests if request.method == "POST"]
    assert len(graph_requests) == 2
    for request in graph_requests:
        assert request.body is not None
        body = json.loads(request.body)
        assert body["variables"]["input"]["types"] == ["DOCUMENT"]
        assert body["variables"]["input"]["count"] == 1
        assert body["variables"]["input"]["orFilters"][0]["and"][0]["values"] == [
            "urn:li:document:schemabridge-query-recipe-"
            f"{semantic_registry_scope_fingerprint(SCOPE)[:32]}-"
        ]
        assert "mutation" not in body["query"].lower()
    assert all(request.timeout_seconds == 3 for request in transport.requests)
    assert all(request.max_response_bytes == 1_048_576 for request in transport.requests)


def test_invalid_typed_recipe_payload_fails_before_any_page_is_yielded() -> None:
    transport = _Transport(_recipe(), invalid_payload=True)

    with pytest.raises(SemanticDependencySourceError) as failure:
        tuple(_inventory(transport).pages(SCOPE, page_size=1))

    assert failure.value.code is SemanticDependencySourceErrorCode.INVALID_ARTIFACT


def test_absent_datahub_status_aspect_means_active_document() -> None:
    recipe = _recipe()
    pages = tuple(_inventory(_Transport(recipe, missing_status=True)).pages(SCOPE, page_size=1))

    assert pages[0].items[0].recipe == recipe


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (403, SemanticDependencySourceErrorCode.PERMISSION_DENIED),
        (503, SemanticDependencySourceErrorCode.UNAVAILABLE),
    ),
)
def test_http_failures_are_sanitized(
    status: int,
    expected: SemanticDependencySourceErrorCode,
) -> None:
    transport = _Transport(_recipe(), status_code=status)

    with pytest.raises(SemanticDependencySourceError) as failure:
        tuple(_inventory(transport).pages(SCOPE, page_size=1))

    assert failure.value.code is expected
    assert "synthetic-read-token" not in str(failure.value)
