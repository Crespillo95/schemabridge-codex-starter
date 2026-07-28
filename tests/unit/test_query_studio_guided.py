"""Generic guided Query Studio paging, selection, recomputation, and stale-state tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.guided_requests import BuildGuidedRequest, GuidedRequestInput
from schemabridge.application.query_studio import (
    BrowseGuidedGovernedFields,
    ConfirmGuidedQueryStudioPreview,
    PrepareGuidedSelectionQueryStudioPreview,
    QueryStudioError,
    QueryStudioErrorCode,
    RecomputeGuidedQueryStudioEvidence,
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
    MAX_GUIDED_LOGICAL_FIELDS,
    ExecutableEvidenceStatus,
    GovernedBindingFactsFilters,
    GovernedFieldBinding,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
    OpaqueCandidateId,
    PreviewTokenPayload,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    ProviderConfigurationFacts,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioModelProposal,
    QueryStudioOperationalState,
    QueryStudioScopeSnapshot,
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
    SemanticMatchState,
    SignedPreviewToken,
)
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.requests import (
    FilterOperator,
    MetricOperation,
    SortDirection,
)
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    PhysicalValueType,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "demo/ground_truth/registries/manifest.yml"
NOW = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


@dataclass
class StaticRegistry:
    value: ScopedSemanticRegistrySnapshot

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.value.scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        return self.value


@dataclass
class StaticClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class StaticNonce:
    value: str = "guided_nonce_0123456789"

    def new_nonce(self) -> str:
        return self.value


class CandidateIds:
    def issue(
        self,
        *,
        scope: QueryStudioScopeSnapshot,
        binding: GovernedFieldBinding,
        request_digest: str,
        nonce: str,
    ) -> OpaqueCandidateId:
        digest = hashlib.sha256(
            (
                scope.fingerprint
                + binding.binding_id
                + binding.binding_fingerprint
                + request_digest
                + nonce
            ).encode()
        ).hexdigest()
        return OpaqueCandidateId(f"qsc1_{digest}")


class PreviewTokens:
    def __init__(self) -> None:
        self.payloads: dict[str, PreviewTokenPayload] = {}

    def issue(self, payload: PreviewTokenPayload) -> SignedPreviewToken:
        digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        token = SignedPreviewToken(f"qsp1.{digest}")
        self.payloads[token.root] = payload
        return token

    def verify(
        self,
        token: SignedPreviewToken,
        *,
        at: datetime,
    ) -> PreviewTokenPayload:
        del at
        return self.payloads[token.root]


class PagedGovernedSearch:
    """Small deterministic high-level port that implements the M27 keyset contract."""

    def __init__(
        self,
        scope: QueryStudioScopeSnapshot,
        values: tuple[GovernedFieldBinding, ...],
    ) -> None:
        self.scope = scope
        self.values = tuple(
            sorted(
                values,
                key=lambda item: (
                    -item.signals.total,
                    item.logical_field.root,
                    item.binding_id,
                ),
            )
        )
        self.requests: list[GovernedFieldSearchRequest] = []

    def search(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        self.requests.append(request)
        values = self.values
        if request.query is not None:
            tokens = set(request.query.root.casefold().split())
            values = tuple(
                item
                for item in values
                if tokens.intersection(
                    (item.logical_field.root.replace(".", " ") + " " + (item.definition or ""))
                    .casefold()
                    .split()
                )
            )
        if request.after is not None:
            values = tuple(
                item
                for item in values
                if (
                    -item.signals.total,
                    item.logical_field.root,
                    item.binding_id,
                )
                > request.after.sort_tuple
            )
        visible = values[: request.page_size]
        has_more = len(values) > request.page_size
        next_key = (
            visible[-1].search_key(
                self.scope.fingerprint,
                request.request_fingerprint,
                SHA_D,
            )
            if has_more
            else None
        )
        return GovernedFieldSearchPage(
            scope=self.scope,
            request_fingerprint=request.request_fingerprint,
            binding_facts_fingerprint=SHA_D,
            items=visible,
            page_size=request.page_size,
            rows_read=len(visible) + int(has_more),
            next_key=next_key,
        )


class SpyBuilder:
    def __init__(self, registry: StaticRegistry) -> None:
        self.delegate = BuildGuidedRequest(registry)
        self.calls = 0

    def execute(self, value: GuidedRequestInput) -> ValidatedAnalyticalRequest:
        self.calls += 1
        return self.delegate.execute(value)


def _registry() -> StaticRegistry:
    scope = SemanticRegistryScope(
        workspace_id="workspace_alpha",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    return StaticRegistry(RecordedGovernedSemanticRegistry(MANIFEST, scope).load())


def _scope(registry: GovernedSemanticRegistrySnapshot) -> QueryStudioScopeSnapshot:
    return QueryStudioScopeSnapshot(
        scope=SemanticRegistryScope(
            workspace_id="workspace_alpha",
            catalog_scope="synthetic-demo",
            registry_id="synthetic_enterprise",
        ),
        registry_version=registry.version,
        registry_fingerprint=registry.fingerprint,
        pointer_generation=2,
        pointer_fingerprint=SHA_A,
        evidence_head_revision=3,
        evidence_baseline_revision=2,
        evidence_baseline_fingerprint=SHA_B,
        catalog_generation_vector_fingerprint=SHA_C,
    )


def _binding(
    registry: GovernedSemanticRegistrySnapshot,
    logical_field: str,
    score: int,
) -> GovernedFieldBinding:
    governed = next(
        item
        for item in registry.mapping_set.mappings
        if item.mapping.logical_field.root == logical_field
    )
    physical = governed.mapping.physical_field.root
    schema_name, table_name, *field_path = physical.split(".")
    physical_type = governed.physical_type
    native_type = {
        PhysicalValueType.STRING: "varchar",
        PhysicalValueType.INTEGER: "bigint",
        PhysicalValueType.FLOAT: "double precision",
        PhysicalValueType.DECIMAL: "numeric",
        PhysicalValueType.BOOLEAN: "boolean",
        PhysicalValueType.DATE: "date",
        PhysicalValueType.TIMESTAMP: "timestamp",
    }[physical_type]
    safe_id = logical_field.replace(".", "_").casefold()
    return GovernedFieldBinding(
        binding_id=f"binding_{safe_id}",
        binding_fingerprint=hashlib.sha256(logical_field.encode()).hexdigest(),
        logical_field=governed.mapping.logical_field,
        physical_field=PhysicalFieldRef(physical),
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace_alpha",
                connection_id=CatalogConnectionId("connection_primary"),
                asset_id=CatalogAssetId(f"asset:{schema_name}.{table_name}"),
            ),
            field_path=tuple(field_path),
        ),
        mapping_version=governed.mapping.version,
        mapping_approval_decision_id=governed.approval_decision_id or "missing",
        physical_type=physical_type,
        evidence_status=ExecutableEvidenceStatus.CURRENT,
        catalog_generation=4,
        catalog_generation_fingerprint=SHA_B,
        asset_qualified_name=f"{schema_name}.{table_name}",
        asset_metadata_fingerprint=SHA_A,
        field_metadata_fingerprint=SHA_B,
        field_definition_fingerprint=SHA_C,
        field_terms_fingerprint=SHA_D,
        native_type=native_type,
        definition="Synthetic public governed field.",
        nullable=False,
        is_part_of_key=False,
        signals=SearchSignalBreakdown.create(
            (
                SearchSignal(
                    code=SearchSignalCode.DEFINITION_OVERLAP,
                    value=score,
                ),
            )
        ),
    )


def _configuration() -> ProviderConfigurationFacts:
    return ProviderConfigurationFacts.create(
        adapter="deterministic_fake",
        model_snapshot="query-studio-fake-v1",
        reasoning_effort="none",
        endpoint_region="local",
        prompt_version="m27-v1",
        schema_version="m27-v1",
        matcher_version="m27-v1",
        attempt_policy_version="m27-no-egress-v1",
        external_ai=False,
    )


def _browse(
    registry: StaticRegistry,
    search: PagedGovernedSearch,
) -> BrowseGuidedGovernedFields:
    return BrowseGuidedGovernedFields(
        registry=registry,
        search=search,
        candidate_ids=CandidateIds(),
        clock=StaticClock(),
        nonces=StaticNonce(),
    )


def test_guided_browse_is_keyset_opaque_and_derives_generic_controls() -> None:
    registry = _registry()
    values = (
        _binding(registry.value.registry, "Product.category", 900),
        _binding(registry.value.registry, "Product.unit_price", 800),
        _binding(registry.value.registry, "Product.created_at", 700),
    )
    search = PagedGovernedSearch(_scope(registry.value.registry), values)
    browse = _browse(registry, search)
    first = browse.execute(GovernedFieldSearchRequest(scope=registry.value.scope, page_size=1))

    assert len(first.items) == 1
    assert first.next_key is not None
    assert first.items[0].logical_model.root == "Product"
    assert first.items[0].binding.logical_field.root == "Product.category"
    assert first.items[0].candidate_id.root.startswith("qsc1_")
    assert "Product.category" not in first.items[0].candidate_id.root
    assert first.items[0].allowed_values == ("ELECTRONICS", "HOME", "BOOKS", "SPORTS")
    assert first.items[0].metric_operations == (
        MetricOperation.COUNT,
        MetricOperation.COUNT_DISTINCT,
    )
    assert FilterOperator.GREATER_THAN not in first.items[0].filter_operators
    assert not first.items[0].date_grains

    second = browse.execute(
        GovernedFieldSearchRequest(
            scope=registry.value.scope,
            page_size=1,
            after=first.next_key,
            expected_scope=first.context.scope,
        ),
        context=first.context,
    )
    assert second.items[0].binding.logical_field.root == "Product.unit_price"
    assert second.items[0].metric_operations == tuple(MetricOperation)
    assert second.context == first.context
    assert search.requests[-1].after == first.next_key


def test_guided_selection_recomputes_then_confirms_without_domain_branching() -> None:
    registry = _registry()
    values = (
        _binding(registry.value.registry, "Product.product_key", 900),
        _binding(registry.value.registry, "Product.category", 800),
    )
    search = PagedGovernedSearch(_scope(registry.value.registry), values)
    page = _browse(registry, search).execute(
        GovernedFieldSearchRequest(scope=registry.value.scope, page_size=20)
    )
    ids = {item.binding.logical_field.root: item.candidate_id for item in page.items}
    proposal = QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=ids["Product.product_key"],
        dimensions=(ProposedDimension(candidate_id=ids["Product.category"]),),
        metrics=(
            ProposedMetric(
                candidate_id=ids["Product.product_key"],
                operation=MetricOperation.COUNT_DISTINCT,
                alias="distinct_products",
            ),
        ),
        order_by=(
            ProposedOrder(
                candidate_id=ids["Product.category"],
                direction=SortDirection.ASC,
            ),
        ),
        limit=250,
    )
    resolver = RecomputeGuidedQueryStudioEvidence(
        registry=registry,
        search=search,
        candidate_ids=CandidateIds(),
    )
    tokens = PreviewTokens()
    preview = PrepareGuidedSelectionQueryStudioPreview(
        resolver=resolver,
        preview_tokens=tokens,
        clock=StaticClock(),
        configuration=_configuration(),
    ).execute(proposal, context=page.context)

    assert preview.token is not None
    assert preview.guided_preview is not None
    assert preview.guided_preview.draft.primary_entity.root == "Product"
    assert not hasattr(preview, "validated_request")
    assert search.requests[-1].query is None
    builder = SpyBuilder(registry)
    confirmation = QueryStudioConfirmation(
        original_text=None,
        language=None,
        expansion=None,
        proposal=proposal,
        token=preview.token,
        action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
    )
    requests_before_confirmation = len(search.requests)
    confirmed = ConfirmGuidedQueryStudioPreview(
        registry=registry,
        recompute=resolver,
        preview_tokens=tokens,
        clock=StaticClock(),
        configuration=_configuration(),
        guided_builder=builder,  # type: ignore[arg-type]
    ).execute(confirmation)

    assert builder.calls == 1
    assert len(search.requests) > requests_before_confirmation
    assert confirmed.validated_request.request.primary_entity.root == "Product"
    assert confirmed.validated_request.request.metrics[0].operation is (
        MetricOperation.COUNT_DISTINCT
    )
    assert confirmed.validated_request.join_contract_ids == ()


def test_guided_confirmation_fails_stale_before_builder_when_catalog_scope_changes() -> None:
    registry = _registry()
    values = (
        _binding(registry.value.registry, "Product.product_key", 900),
        _binding(registry.value.registry, "Product.category", 800),
    )
    search = PagedGovernedSearch(_scope(registry.value.registry), values)
    page = _browse(registry, search).execute(
        GovernedFieldSearchRequest(scope=registry.value.scope, page_size=20)
    )
    ids = {item.binding.logical_field.root: item.candidate_id for item in page.items}
    proposal = QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=ids["Product.product_key"],
        dimensions=(ProposedDimension(candidate_id=ids["Product.category"]),),
        metrics=(
            ProposedMetric(
                candidate_id=ids["Product.product_key"],
                operation=MetricOperation.COUNT_DISTINCT,
            ),
        ),
    )
    resolver = RecomputeGuidedQueryStudioEvidence(
        registry=registry,
        search=search,
        candidate_ids=CandidateIds(),
    )
    tokens = PreviewTokens()
    configuration = _configuration()
    preview = PrepareGuidedSelectionQueryStudioPreview(
        resolver=resolver,
        preview_tokens=tokens,
        clock=StaticClock(),
        configuration=configuration,
    ).execute(proposal, context=page.context)
    assert preview.token is not None

    search.scope = search.scope.model_copy(update={"catalog_generation_vector_fingerprint": SHA_D})
    builder = SpyBuilder(registry)
    with pytest.raises(QueryStudioError) as captured:
        ConfirmGuidedQueryStudioPreview(
            registry=registry,
            recompute=resolver,
            preview_tokens=tokens,
            clock=StaticClock(),
            configuration=configuration,
            guided_builder=builder,  # type: ignore[arg-type]
        ).execute(
            QueryStudioConfirmation(
                original_text=None,
                language=None,
                expansion=None,
                proposal=proposal,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )

    assert captured.value.code is QueryStudioErrorCode.STALE_PREVIEW
    assert builder.calls == 0


def test_guided_four_model_selection_is_typed_closure_overflow_without_token() -> None:
    registry = _registry()
    fields = (
        "Product.product_key",
        "Customer.country_code",
        "SalesOrder.order_status",
        "Shipment.shipment_status",
    )
    values = tuple(
        _binding(registry.value.registry, field, 1_000 - index)
        for index, field in enumerate(fields)
    )
    search = PagedGovernedSearch(_scope(registry.value.registry), values)
    page = _browse(registry, search).execute(
        GovernedFieldSearchRequest(scope=registry.value.scope, page_size=20)
    )
    ids = {item.binding.logical_field.root: item.candidate_id for item in page.items}
    proposal = QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=ids["Product.product_key"],
        dimensions=(
            ProposedDimension(candidate_id=ids["Customer.country_code"]),
            ProposedDimension(candidate_id=ids["Shipment.shipment_status"]),
        ),
        metrics=(
            ProposedMetric(
                candidate_id=ids["Product.product_key"],
                operation=MetricOperation.COUNT_DISTINCT,
            ),
        ),
        filters=(
            ProposedFilter(
                candidate_id=ids["SalesOrder.order_status"],
                operator=FilterOperator.EQUALS,
                value="CREATED",
            ),
        ),
    )
    result = PrepareGuidedSelectionQueryStudioPreview(
        resolver=RecomputeGuidedQueryStudioEvidence(
            registry=registry,
            search=search,
            candidate_ids=CandidateIds(),
        ),
        preview_tokens=PreviewTokens(),
        clock=StaticClock(),
        configuration=_configuration(),
    ).execute(proposal, context=page.context)

    assert result.operational_state is QueryStudioOperationalState.CLOSURE_OVERFLOW
    assert result.semantic_state is None
    assert result.token is None
    assert result.reason_code == "guided_closure_overflow"


def test_guided_logical_allowlist_is_hard_bounded_at_one_thousand() -> None:
    assert MAX_GUIDED_LOGICAL_FIELDS == 1_000
    fields = tuple(
        LogicalFieldRef(f"Entity.field_{index:04d}")
        for index in range(MAX_GUIDED_LOGICAL_FIELDS + 1)
    )

    with pytest.raises(ValidationError, match="at most 1000"):
        GovernedBindingFactsFilters(
            logical_fields=fields,
            restrict_logical_fields=True,
        )
