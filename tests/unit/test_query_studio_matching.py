"""Service-free dynamic matching, closure, guided preview, and confirmation tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabridge.adapters.query_studio.recorded import (
    RecordedGovernedBindingFactsSearch,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.guided_requests import BuildGuidedRequest
from schemabridge.application.ports.query_studio import QueryStudioPortErrorCode
from schemabridge.application.query_studio import (
    ConfirmGuidedQueryStudioPreview,
    ConfirmQueryStudioPreview,
    DiscoverPhysicalFields,
    PrepareGuidedQueryStudioPreview,
    PrepareNaturalLanguageQueryStudioPreview,
    QueryStudioError,
    QueryStudioErrorCode,
    RecomputeNaturalLanguageQueryStudioPreview,
    RegistryAwareGovernedFieldSearch,
    natural_language_edit_candidates,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalModelRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    MAX_DESCRIPTION_PROBES,
    MAX_EXECUTABLE_SHORTLIST,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    ExecutableEvidenceStatus,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchFilters,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
    GuidedQueryStudioEvidence,
    OpaqueCandidateId,
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryCardinality,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    PreviewTokenPayload,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderStage,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioInterpretationRevision,
    QueryStudioModelProposal,
    QueryStudioOperationalState,
    QueryStudioRevisionAction,
    QueryStudioScopeSnapshot,
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
    SemanticMatchState,
    SignedPreviewToken,
)
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
    score_governed_description,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import (
    DateGrain,
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
CATALOG = ROOT / "demo/datahub/catalog_snapshot.json"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


@dataclass
class StaticClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class StaticNonce:
    value: str = "nonce_0123456789abcdef"

    def new_nonce(self) -> str:
        return self.value


class FakeTokens:
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


class FakeCandidateIds:
    def issue(
        self,
        *,
        scope: QueryStudioScopeSnapshot,
        binding: GovernedFieldBinding,
        request_digest: str,
        nonce: str,
    ) -> OpaqueCandidateId:
        digest = hashlib.sha256(
            (scope.fingerprint + binding.binding_id + request_digest + nonce).encode()
        ).hexdigest()
        return OpaqueCandidateId(f"qsc1_{digest}")


@dataclass
class StaticRegistry:
    value: ScopedSemanticRegistrySnapshot
    loads: int = 0

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.value.scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        self.loads += 1
        return self.value


class FakeFacts:
    def __init__(
        self,
        scope: QueryStudioScopeSnapshot,
        rows: dict[str, tuple[GovernedFieldBinding, ...]],
    ) -> None:
        self.scope = scope
        self.rows = rows
        self.requests: list[GovernedBindingFactsRequest] = []

    def search(self, request: GovernedBindingFactsRequest) -> GovernedFieldSearchPage:
        self.requests.append(request)
        if request.query is None:
            by_binding = {item.binding_id: item for group in self.rows.values() for item in group}
            values = tuple(
                sorted(
                    by_binding.values(),
                    key=lambda item: (
                        -item.signals.total,
                        item.logical_field.root,
                        item.binding_id,
                    ),
                )
            )
        else:
            values = self.rows.get(request.query.root, ())
        if request.filters.restrict_logical_fields:
            allowed = {item.root for item in request.filters.logical_fields}
            values = tuple(item for item in values if item.logical_field.root in allowed)
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
        rows = values[: request.page_size + 1]
        items = rows[: request.page_size]
        next_key = (
            items[-1].search_key(
                self.scope.fingerprint,
                request.continuation_request_fingerprint,
                request.request_fingerprint,
            )
            if len(rows) == request.page_size + 1
            else None
        )
        return GovernedFieldSearchPage(
            scope=self.scope,
            request_fingerprint=request.continuation_request_fingerprint,
            binding_facts_fingerprint=request.request_fingerprint,
            items=items,
            page_size=request.page_size,
            rows_read=len(rows),
            next_key=next_key,
        )


class StaticGovernedSearch:
    def __init__(
        self,
        scope: QueryStudioScopeSnapshot,
        rows: dict[str, tuple[GovernedFieldBinding, ...]],
    ) -> None:
        self.scope = scope
        self.rows = rows
        self.requests: list[GovernedFieldSearchRequest] = []

    def search(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        self.requests.append(request)
        values = self.rows.get(request.query.root if request.query is not None else "", ())
        rows = values[: request.page_size + 1]
        items = rows[: request.page_size]
        next_key = (
            items[-1].search_key(
                self.scope.fingerprint,
                request.request_fingerprint,
                SHA_D,
            )
            if len(rows) == request.page_size + 1
            else None
        )
        return GovernedFieldSearchPage(
            scope=self.scope,
            request_fingerprint=request.request_fingerprint,
            binding_facts_fingerprint=SHA_D,
            items=items,
            page_size=request.page_size,
            rows_read=len(rows),
            next_key=next_key,
        )


@dataclass
class RecordingGovernedSearch:
    delegate: RegistryAwareGovernedFieldSearch
    requests: list[GovernedFieldSearchRequest]

    def search(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        self.requests.append(request)
        return self.delegate.search(request)


class FakeExpansion:
    def __init__(
        self,
        configuration: ProviderConfigurationFacts,
        expansion: DescriptionExpansion | None = None,
    ) -> None:
        self.configuration = configuration
        self.value = expansion or _north_star_expansion()
        self.calls = 0
        self.inputs: list[DescriptionExpansionInput] = []

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        self.calls += 1
        self.inputs.append(value)
        return DescriptionExpansionResult(
            expansion=self.value,
            usage=_usage(self.configuration, ProviderStage.EXPANSION),
        )


class FakeInterpreter:
    def __init__(self, configuration: ProviderConfigurationFacts) -> None:
        self.configuration = configuration
        self.calls = 0

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        ids = {item.logical_field.root: item.candidate_id for item in value.vocabulary.candidates}
        return QueryStudioInterpretationResult(
            proposal=QueryStudioModelProposal(
                semantic_state=SemanticMatchState.ALIGNED,
                primary_candidate_id=ids["Customer.customer_key"],
                dimensions=(
                    ProposedDimension(
                        candidate_id=ids["Customer.registration_date"],
                        grain=DateGrain.DAY,
                    ),
                ),
                metrics=(
                    ProposedMetric(
                        candidate_id=ids["Customer.customer_key"],
                        operation=MetricOperation.COUNT_DISTINCT,
                        alias="secondary_holder_customers",
                    ),
                ),
                filters=(
                    ProposedFilter(
                        candidate_id=ids["AccountHolder.holder_role"],
                        operator=FilterOperator.EQUALS,
                        value="SECONDARY",
                    ),
                ),
                order_by=(
                    ProposedOrder(
                        candidate_id=ids["Customer.registration_date"],
                        direction=SortDirection.ASC,
                    ),
                ),
                limit=500,
            ),
            usage=_usage(self.configuration, ProviderStage.INTERPRETATION),
        )


class RevenueInterpreter:
    def __init__(self, configuration: ProviderConfigurationFacts) -> None:
        self.configuration = configuration
        self.calls = 0

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        candidates = {
            item.logical_field.root: item.candidate_id for item in value.vocabulary.candidates
        }
        return QueryStudioInterpretationResult(
            proposal=QueryStudioModelProposal(
                semantic_state=SemanticMatchState.ALIGNED,
                primary_candidate_id=candidates["SaleLine.net_amount"],
                dimensions=(
                    ProposedDimension(
                        candidate_id=candidates["SalesOrder.ordered_at"],
                        grain=DateGrain.DAY,
                    ),
                    ProposedDimension(candidate_id=candidates["Product.category"]),
                ),
                metrics=(
                    ProposedMetric(
                        candidate_id=candidates["SaleLine.net_amount"],
                        operation=MetricOperation.SUM,
                        alias="net_revenue",
                    ),
                ),
                order_by=(
                    ProposedOrder(candidate_id=candidates["SalesOrder.ordered_at"]),
                    ProposedOrder(candidate_id=candidates["Product.category"]),
                ),
                limit=100,
            ),
            usage=_usage(self.configuration, ProviderStage.INTERPRETATION),
        )


class FirstMetricInterpreter:
    def __init__(self, configuration: ProviderConfigurationFacts) -> None:
        self.configuration = configuration
        self.calls = 0

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        self.calls += 1
        candidate = value.vocabulary.candidates[0].candidate_id
        return QueryStudioInterpretationResult(
            proposal=QueryStudioModelProposal(
                semantic_state=SemanticMatchState.ALIGNED,
                primary_candidate_id=candidate,
                metrics=(
                    ProposedMetric(
                        candidate_id=candidate,
                        operation=MetricOperation.COUNT_DISTINCT,
                        alias="distinct_entities",
                    ),
                ),
                limit=100,
            ),
            usage=_usage(self.configuration, ProviderStage.INTERPRETATION),
        )


@dataclass
class StaticGuidedRecompute:
    value: GuidedQueryStudioEvidence

    def recompute(
        self,
        confirmation: QueryStudioConfirmation,
        *,
        nonce: str,
    ) -> GuidedQueryStudioEvidence:
        del confirmation, nonce
        return self.value


class FakePhysicalDiscovery:
    def __init__(self, page: PhysicalFieldDiscoveryPage) -> None:
        self.page = page
        self.calls = 0

    def search(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        del request
        self.calls += 1
        return self.page

    def inspect_cardinality(
        self,
        scope: SemanticRegistryScope,
    ) -> PhysicalDiscoveryCardinality:
        return PhysicalDiscoveryCardinality(
            scope=scope,
            catalog_generation_vector_fingerprint=SHA_A,
            connection_count=1,
            asset_count=1,
            field_count=len(self.page.items),
        )


def _registry() -> StaticRegistry:
    scope = SemanticRegistryScope(
        workspace_id="workspace_alpha",
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    recorded = RecordedGovernedSemanticRegistry(MANIFEST, scope).load()
    return StaticRegistry(recorded)


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
    *,
    definition: str = "Synthetic public field metadata.",
) -> GovernedFieldBinding:
    governed = next(
        item
        for item in registry.mapping_set.mappings
        if item.mapping.logical_field.root == logical_field
    )
    physical = governed.mapping.physical_field.root
    schema_name, table_name, *field_path = physical.split(".")
    safe_id = logical_field.replace(".", "_").casefold()
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
    return GovernedFieldBinding(
        binding_id=f"binding_{safe_id}",
        binding_fingerprint=SHA_A,
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
        definition=definition,
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


def _rank_generic_browser_fixture(
    registry: GovernedSemanticRegistrySnapshot,
    query: str,
) -> tuple[tuple[str, int], ...]:
    """Mirror the generic PostgreSQL metadata retained by the M27 browser seed."""

    fields = registry.logical_context.field_index()
    models = registry.logical_context.model_index()
    scored: list[tuple[str, int]] = []
    for governed in registry.mapping_set.mappings:
        logical_field = governed.mapping.logical_field.root
        physical_field = governed.mapping.physical_field.root
        field = fields[logical_field]
        model = models[logical_field.split(".", 1)[0]]
        signals = score_governed_description(
            query,
            logical_field=logical_field,
            model_description=model.description,
            field_definition=field.definition,
            role=field.role,
            canonical_type=field.canonical_type,
            allowed_values=field.allowed_values,
            physical_field=physical_field,
            physical_definitions=(f"Stable definition for {physical_field}",),
            native_types=(governed.physical_type.value,),
            taxonomy=("governed", "semantic"),
        )
        if signals.total:
            scored.append((logical_field, signals.total))
    return tuple(sorted(scored, key=lambda item: (-item[1], item[0])))


def _north_star_rows(
    registry: GovernedSemanticRegistrySnapshot,
) -> dict[str, tuple[GovernedFieldBinding, ...]]:
    return {
        "fecha de registro del cliente": (_binding(registry, "Customer.registration_date", 900),),
        "identificador estable del cliente": (_binding(registry, "Customer.customer_key", 950),),
        "tipo de titular": (_binding(registry, "AccountHolder.holder_role", 925),),
    }


def _north_star_expansion() -> DescriptionExpansion:
    return DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="registration_date",
                query=DescriptionQuery("fecha de registro del cliente"),
                intended_use=QueryFieldPurpose.DIMENSION,
                roles=(LogicalFieldRole.TEMPORAL,),
                date_grain=DateGrain.DAY,
            ),
            DescriptionSearchProbe(
                purpose_id="customer_metric",
                query=DescriptionQuery("identificador estable del cliente"),
                intended_use=QueryFieldPurpose.METRIC,
                roles=(LogicalFieldRole.IDENTIFIER,),
                metric_operation=MetricOperation.COUNT_DISTINCT,
            ),
            DescriptionSearchProbe(
                purpose_id="holder_role",
                query=DescriptionQuery("tipo de titular"),
                intended_use=QueryFieldPurpose.FILTER,
                roles=(LogicalFieldRole.ATTRIBUTE,),
                filter_operator=FilterOperator.EQUALS,
            ),
        )
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


def _usage(
    configuration: ProviderConfigurationFacts,
    stage: ProviderStage,
) -> ProviderUsageFacts:
    return ProviderUsageFacts(
        stage=stage,
        model_snapshot=configuration.model_snapshot,
        configuration_fingerprint=configuration.fingerprint,
        input_tokens=100,
        output_tokens=20,
        duration_ms=5,
        outcome=ProviderOutcomeCode.SUCCEEDED,
    )


def _service_parts() -> tuple[
    StaticRegistry,
    FakeFacts,
    RegistryAwareGovernedFieldSearch,
    FakeExpansion,
    FakeInterpreter,
    FakeTokens,
    ProviderConfigurationFacts,
]:
    registry = _registry()
    configuration = _configuration()
    facts = FakeFacts(
        _scope(registry.value.registry),
        _north_star_rows(registry.value.registry),
    )
    search = RegistryAwareGovernedFieldSearch(registry=registry, facts=facts)
    expansion = FakeExpansion(configuration)
    interpreter = FakeInterpreter(configuration)
    tokens = FakeTokens()
    return registry, facts, search, expansion, interpreter, tokens, configuration


def test_registry_wrapper_derives_role_allowlist_without_postgres_owning_logical_metadata() -> None:
    registry, facts, search, *_ = _service_parts()
    page = search.search(
        request=_north_star_search_request(
            registry.value.scope,
            "fecha de registro",
            LogicalFieldRole.TEMPORAL,
        )
    )

    raw = facts.requests[-1]
    assert page.items[0].logical_field.root == "Customer.registration_date"
    assert raw.filters.restrict_logical_fields is True
    assert "Customer.registration_date" in {item.root for item in raw.filters.logical_fields}
    assert not hasattr(raw.filters, "roles")


def test_shared_matcher_keeps_registration_as_discriminating_spanish_evidence() -> None:
    registration = score_governed_description(
        "fecha de registro",
        logical_field="Customer.registration_date",
        model_description="Approved customer context.",
        field_definition="Calendar date on which the customer was registered.",
        role=LogicalFieldRole.TEMPORAL,
        canonical_type=CanonicalType.DATE,
        physical_field="crm.customers.registration_date",
    )
    unrelated_temporal = score_governed_description(
        "fecha de registro",
        logical_field="Shipment.delivered_at",
        model_description="Approved shipment context.",
        field_definition="Timestamp when the shipment was delivered.",
        role=LogicalFieldRole.TEMPORAL,
        canonical_type=CanonicalType.TIMESTAMP,
        physical_field="fulfillment.shipments.delivered_at",
    )

    assert registration.total > 0
    assert unrelated_temporal.total == 0


@pytest.mark.parametrize(
    "description",
    [
        "product reference recorded on the sales line",
        "referencia del producto vendido en la línea",
    ],
)
def test_shared_matcher_ranks_sale_line_product_with_generic_browser_metadata(
    description: str,
) -> None:
    registry = _registry().value.registry

    ranked = _rank_generic_browser_fixture(registry, description)

    assert GOVERNED_DESCRIPTION_MATCHER_VERSION == "m27-deterministic-v9"
    assert "SaleLine.product_key" in tuple(item[0] for item in ranked[:3])


@pytest.mark.parametrize(
    ("description", "expected_logical_field"),
    [
        ("indicador de si el producto está activo", "Product.is_active"),
        ("importe bruto total del pedido", "SalesOrder.order_total"),
    ],
)
def test_v45_field_matches_rank_with_sparse_browser_metadata(
    description: str,
    expected_logical_field: str,
) -> None:
    ranked = _rank_generic_browser_fixture(
        _registry().value.registry,
        description,
    )

    assert expected_logical_field in tuple(item[0] for item in ranked[:3])


def test_reference_normalization_preserves_ambiguity_and_no_match_safety() -> None:
    registry = _registry().value.registry

    order_reference = _rank_generic_browser_fixture(registry, "referencia del pedido")
    generic_reference = _rank_generic_browser_fixture(registry, "reference")
    unrelated_reference = _rank_generic_browser_fixture(
        registry,
        "support ticket reference",
    )

    assert {item[0] for item in order_reference[:3]} == {
        "SaleLine.order_key",
        "SalesOrder.order_key",
        "Shipment.order_key",
    }
    assert len({item[1] for item in order_reference[:3]}) == 1
    assert len(generic_reference) >= 2
    assert unrelated_reference == ()


def test_disconnected_lower_ranked_candidate_cannot_poison_coherent_closure() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="registration_date",
                    query=DescriptionQuery("customer registration date"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                    roles=(LogicalFieldRole.TEMPORAL,),
                    date_grain=DateGrain.DAY,
                ),
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("customer identifier"),
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
                DescriptionSearchProbe(
                    purpose_id="holder_role",
                    query=DescriptionQuery("secondary holder role"),
                    intended_use=QueryFieldPurpose.FILTER,
                    roles=(LogicalFieldRole.ATTRIBUTE,),
                    filter_operator=FilterOperator.EQUALS,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer registration date": (
                _binding(registry.value.registry, "Customer.registration_date", 9_000),
                _binding(registry.value.registry, "SalesOrder.ordered_at", 4_000),
            ),
            "customer identifier": (
                _binding(registry.value.registry, "Customer.customer_key", 8_000),
                _binding(registry.value.registry, "AccountHolder.customer_key", 7_000),
            ),
            "secondary holder role": (
                _binding(registry.value.registry, "AccountHolder.holder_role", 9_000),
            ),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "Agrupa por fecha de registro todos los clientes que sean segundo titular.",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.vocabulary is not None
    assert {item.id.root for item in preview.vocabulary.models} == {
        "Customer",
        "AccountHolder",
    }
    assert all(
        item.logical_field.root != "SalesOrder.ordered_at" for item in preview.vocabulary.candidates
    )
    assert preview.shortlist is not None
    assert any(
        item.binding.logical_field.root == "SalesOrder.ordered_at"
        for item in preview.shortlist.candidates
    )
    assert interpreter.calls == 1


def test_equal_disconnected_coherent_branches_are_ambiguous_without_provider() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="date",
                    query=DescriptionQuery("registration date"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                    roles=(LogicalFieldRole.TEMPORAL,),
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "registration date": (
                _binding(registry.value.registry, "Customer.registration_date", 900),
                _binding(registry.value.registry, "SalesOrder.ordered_at", 900),
            ),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("registration date", UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.AMBIGUOUS
    assert preview.vocabulary is None
    assert interpreter.calls == 0


def test_bare_identifier_uses_analytical_lane_before_cross_connection_ambiguity() -> None:
    registry = _registry()
    configuration = _configuration()
    primary = _binding(registry.value.registry, "Customer.customer_key", 1_000)
    shadow_locator = primary.locator.model_copy(
        update={
            "asset": primary.locator.asset.model_copy(
                update={"connection_id": CatalogConnectionId("connection_shadow")}
            )
        }
    )
    shadow = primary.model_copy(
        update={
            "binding_id": "binding_customer_customer_key_shadow",
            "locator": shadow_locator,
            "signals": SearchSignalBreakdown.create(
                (
                    SearchSignal(
                        code=SearchSignalCode.EXACT_PHYSICAL_FIELD,
                        value=900,
                    ),
                )
            ),
        }
    )
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="requested_field",
                    query=DescriptionQuery("customer identifier"),
                    source_span="customer_id",
                    intended_use=QueryFieldPurpose.DIMENSION,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer identifier": (primary, shadow),
            "customer_id": (primary, shadow),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("customer_id", UserLanguage.SPANISH)

    assert preview.semantic_state is SemanticMatchState.AMBIGUOUS
    assert preview.operational_state is None
    assert preview.vocabulary is None
    assert preview.shortlist is not None
    assert preview.expansion == expansion.value
    assert len(preview.provider_usage) == 1
    assert {
        item.binding.locator.asset.connection_id.root for item in preview.shortlist.candidates
    } == {"connection_primary", "connection_shadow"}
    assert any(item.query == DescriptionQuery("customer_id") for item in search.requests)
    assert expansion.calls == 1
    assert expansion.inputs[0].lane is DescriptionExpansionRoute.ANALYTICAL
    assert interpreter.calls == 0


def test_qualified_identifier_continues_through_live_language_stages() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("stable customer identifier"),
                    source_span="stable customer identifier",
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "stable customer identifier": (
                _binding(registry.value.registry, "Customer.customer_key", 1_000),
            ),
        },
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "count distinct stable customer identifier",
        UserLanguage.ENGLISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.operational_state is None
    assert expansion.calls == 1
    assert interpreter.calls == 1
    assert len(preview.provider_usage) == 2


def test_count_distinct_entity_variant_prevents_analytical_no_match() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("customer count"),
                    source_span="customers",
                    intended_use=QueryFieldPurpose.METRIC,
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
                DescriptionSearchProbe(
                    purpose_id="customer_country",
                    query=DescriptionQuery("country"),
                    source_span="country",
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
                DescriptionSearchProbe(
                    purpose_id="customer_status",
                    query=DescriptionQuery("customer status"),
                    source_span="active customers",
                    intended_use=QueryFieldPurpose.FILTER,
                    filter_operator=FilterOperator.EQUALS,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "stable customer identifier": (
                _binding(registry.value.registry, "Customer.customer_key", 1_000),
            ),
            "country": (_binding(registry.value.registry, "Customer.country_code", 950),),
            "customer status": (
                _binding(registry.value.registry, "Customer.customer_status", 975),
            ),
        },
    )

    class ActiveCustomerInterpreter:
        calls = 0

        def interpret(
            self,
            value: QueryStudioInterpretationInput,
        ) -> QueryStudioInterpretationResult:
            self.calls += 1
            purpose_ids = {
                item.logical_field.root: item.purpose_ids for item in value.vocabulary.candidates
            }
            assert purpose_ids == {
                "Customer.customer_key": ("customer_metric",),
                "Customer.country_code": ("customer_country",),
                "Customer.customer_status": ("customer_status",),
            }
            candidates = {
                item.logical_field.root: item.candidate_id for item in value.vocabulary.candidates
            }
            return QueryStudioInterpretationResult(
                proposal=QueryStudioModelProposal(
                    semantic_state=SemanticMatchState.ALIGNED,
                    primary_candidate_id=candidates["Customer.customer_key"],
                    dimensions=(
                        ProposedDimension(
                            candidate_id=candidates["Customer.country_code"],
                        ),
                    ),
                    metrics=(
                        ProposedMetric(
                            candidate_id=candidates["Customer.customer_key"],
                            operation=MetricOperation.COUNT_DISTINCT,
                        ),
                    ),
                    filters=(
                        ProposedFilter(
                            candidate_id=candidates["Customer.customer_status"],
                            operator=FilterOperator.EQUALS,
                            value="ACTIVE",
                        ),
                    ),
                    limit=100,
                ),
                usage=_usage(configuration, ProviderStage.INTERPRETATION),
            )

    interpreter = ActiveCustomerInterpreter()
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "count active customers grouped by country",
        UserLanguage.ENGLISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.guided_preview is not None
    assert interpreter.calls == 1
    assert any(
        request.query == DescriptionQuery("stable customer identifier")
        and request.filters.roles == (LogicalFieldRole.IDENTIFIER,)
        for request in search.requests
    )


def test_prompt_purpose_ids_exclude_a_candidate_outside_that_purpose_top_two() -> None:
    registry = _registry()
    configuration = _configuration()
    metric = _binding(registry.value.registry, "Customer.customer_key", 1_000)
    metric_as_third_dimension = metric.model_copy(
        update={
            "signals": SearchSignalBreakdown.create(
                (SearchSignal(code=SearchSignalCode.DEFINITION_OVERLAP, value=700),)
            )
        }
    )
    country = _binding(registry.value.registry, "Customer.country_code", 950)
    status = _binding(registry.value.registry, "Customer.customer_status", 900)
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="metric_1",
                    query=DescriptionQuery("customer identifier"),
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
                DescriptionSearchProbe(
                    purpose_id="dimension_1",
                    query=DescriptionQuery("customer attribute"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer identifier": (metric,),
            "stable customer identifier": (metric,),
            "customer attribute": (
                country,
                status,
                metric_as_third_dimension,
            ),
        },
    )

    class PurposeInspectingInterpreter:
        calls = 0

        def interpret(
            self,
            value: QueryStudioInterpretationInput,
        ) -> QueryStudioInterpretationResult:
            self.calls += 1
            purpose_ids = {
                item.logical_field.root: item.purpose_ids for item in value.vocabulary.candidates
            }
            assert purpose_ids["Customer.customer_key"] == ("metric_1",)
            assert purpose_ids["Customer.country_code"] == ("dimension_1",)
            assert purpose_ids["Customer.customer_status"] == ("dimension_1",)
            return QueryStudioInterpretationResult(
                proposal=QueryStudioModelProposal(
                    semantic_state=SemanticMatchState.NO_MATCH,
                ),
                usage=_usage(configuration, ProviderStage.INTERPRETATION),
            )

    interpreter = PurposeInspectingInterpreter()
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "count customers by customer attribute",
        UserLanguage.ENGLISH,
    )

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert interpreter.calls == 1


def test_no_joinable_purpose_combination_is_closure_overflow() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_date",
                    query=DescriptionQuery("customer registration date"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
                DescriptionSearchProbe(
                    purpose_id="order_status",
                    query=DescriptionQuery("order status"),
                    intended_use=QueryFieldPurpose.FILTER,
                    filter_operator=FilterOperator.EQUALS,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer registration date": (
                _binding(registry.value.registry, "Customer.registration_date", 900),
            ),
            "order status": (_binding(registry.value.registry, "SalesOrder.order_status", 900),),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("customer registrations by order status", UserLanguage.ENGLISH)

    assert preview.operational_state is QueryStudioOperationalState.CLOSURE_OVERFLOW
    assert preview.reason_code == "join_closure_overflow"
    assert preview.vocabulary is None
    assert interpreter.calls == 0


def test_disconnected_top_one_is_never_replaced_by_joinable_runner_up() -> None:
    registry = _registry()
    configuration = _configuration()
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_date",
                    query=DescriptionQuery("customer registration date"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
                DescriptionSearchProbe(
                    purpose_id="order_metric",
                    query=DescriptionQuery("order identifier"),
                    intended_use=QueryFieldPurpose.METRIC,
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
            )
        ),
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer registration date": (
                _binding(registry.value.registry, "Customer.registration_date", 900),
            ),
            "order identifier": (
                _binding(registry.value.registry, "SalesOrder.order_key", 900),
                _binding(registry.value.registry, "Customer.customer_key", 800),
            ),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("customers by order identifier", UserLanguage.ENGLISH)

    assert preview.operational_state is QueryStudioOperationalState.CLOSURE_OVERFLOW
    assert preview.reason_code == "join_closure_overflow"
    assert preview.vocabulary is None
    assert interpreter.calls == 0


def test_global_shortlist_cutoff_cannot_drop_a_required_purpose() -> None:
    registry = _registry()
    configuration = _configuration()
    required = "AccountHolder.holder_role"
    remaining = list(
        dict.fromkeys(
            item.mapping.logical_field.root
            for item in registry.value.registry.mapping_set.mappings
            if item.mapping.logical_field.root != required
        )
    )
    high_fields = [
        "Customer.customer_key",
        "Customer.registration_date",
        *(
            field
            for field in remaining
            if field not in {"Customer.customer_key", "Customer.registration_date"}
        ),
    ][:20]
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "broad field": tuple(
                _binding(registry.value.registry, field, 10_000 - index)
                for index, field in enumerate(high_fields)
            ),
            "holder role": (_binding(registry.value.registry, required, 1),),
        },
    )
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="broad_dimension",
                    query=DescriptionQuery("broad field"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
                DescriptionSearchProbe(
                    purpose_id="holder_filter",
                    query=DescriptionQuery("holder role"),
                    intended_use=QueryFieldPurpose.FILTER,
                    filter_operator=FilterOperator.EQUALS,
                ),
            )
        ),
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("broad field where holder role", UserLanguage.ENGLISH)

    assert preview.operational_state is QueryStudioOperationalState.CLOSURE_OVERFLOW
    assert preview.reason_code == "shortlist_closure_overflow"
    assert preview.vocabulary is None
    assert interpreter.calls == 0


@pytest.mark.parametrize(
    "probe",
    [
        "segundo titular",
        "second holder",
        "secondary account holder",
    ],
)
def test_shared_matcher_aligns_secondary_holder_synonyms_to_governed_value(
    probe: str,
) -> None:
    result = score_governed_description(
        probe,
        logical_field="AccountHolder.holder_role",
        model_description="Approved relationship between a customer and an account.",
        field_definition="Approved normalized role of the customer on the account.",
        role=LogicalFieldRole.ATTRIBUTE,
        canonical_type=CanonicalType.STRING,
        allowed_values=("PRIMARY", "SECONDARY"),
        physical_field="bank.account_holders.holder_type",
    )

    codes = {signal.code for signal in result.signals}
    assert result.total > 0
    assert SearchSignalCode.TAXONOMY_OVERLAP in codes
    assert SearchSignalCode.LOGICAL_NAME_OVERLAP in codes


def test_incompatible_role_and_type_constraints_fail_closed_before_ranking() -> None:
    registry, facts, search, _, interpreter, tokens, configuration = _service_parts()
    probes = _north_star_expansion().probes
    misleading_hints = DescriptionExpansion(
        probes=(
            probes[0].model_copy(
                update={
                    "roles": (LogicalFieldRole.IDENTIFIER,),
                    "canonical_types": (CanonicalType.TIMESTAMP,),
                }
            ),
            probes[1].model_copy(
                update={
                    "roles": (LogicalFieldRole.ATTRIBUTE,),
                    "canonical_types": (CanonicalType.INTEGER,),
                }
            ),
            probes[2].model_copy(
                update={
                    "roles": (LogicalFieldRole.IDENTIFIER,),
                    "canonical_types": (CanonicalType.DATE,),
                }
            ),
        )
    )
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, misleading_hints),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=tokens,
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "Agrupa por fecha de registro los clientes que sean segundo titular",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.vocabulary is None
    assert interpreter.calls == 0
    assert len(facts.requests) == 3
    assert all(request.page_size <= 50 for request in facts.requests)


def test_grounded_source_span_fallback_recovers_governed_fields_from_provider_rewrites() -> None:
    registry = _registry()
    configuration = _configuration()
    spans = ("fecha de registro", "clientes", "segundo titular")
    provider_queries = (
        "customer lifecycle chronology",
        "stable party identity",
        "account participation classification",
    )
    base_probes = _north_star_expansion().probes
    expansion = DescriptionExpansion(
        probes=tuple(
            probe.model_copy(
                update={
                    "query": DescriptionQuery(provider_query),
                    "source_span": source_span,
                }
            )
            for probe, provider_query, source_span in zip(
                base_probes,
                provider_queries,
                spans,
                strict=True,
            )
        )
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            spans[0]: (_binding(registry.value.registry, "Customer.registration_date", 900),),
            spans[1]: (_binding(registry.value.registry, "Customer.customer_key", 950),),
            spans[2]: (_binding(registry.value.registry, "AccountHolder.holder_role", 925),),
        },
    )

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, expansion),
        interpreter=FakeInterpreter(configuration),
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "Agrupa por fecha de registro los clientes que sean segundo titular",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.shortlist is not None
    assert {item.binding.logical_field.root for item in preview.shortlist.candidates} == {
        "Customer.registration_date",
        "Customer.customer_key",
        "AccountHolder.holder_role",
    }
    assert all(request.query is not None for request in search.requests)
    assert tuple(
        request.query.root for request in search.requests if request.query is not None
    ) == tuple(
        query
        for provider_query, source_span in zip(provider_queries, spans, strict=True)
        for query in (provider_query, source_span)
    )
    source_requests = tuple(
        request
        for request in search.requests
        if request.query is not None and request.query.root in spans
    )
    assert len(source_requests) == len(spans)
    assert all(
        search.requests[index].filters == search.requests[index + 1].filters
        for index in range(0, len(search.requests), 2)
    )


def test_revenue_core_ignores_only_out_of_branch_source_span_cutoff_tie() -> None:
    registry = _registry()
    configuration = _configuration()
    facts = RecordedGovernedBindingFactsSearch(registry.value, CATALOG)
    search = RecordingGovernedSearch(
        delegate=RegistryAwareGovernedFieldSearch(registry=registry, facts=facts),
        requests=[],
    )
    expansion = DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="order_date",
                query=DescriptionQuery("commercial order date"),
                source_span="fecha de pedido",
                intended_use=QueryFieldPurpose.DIMENSION,
                roles=(LogicalFieldRole.TEMPORAL,),
                canonical_types=(CanonicalType.TIMESTAMP,),
                date_grain=DateGrain.DAY,
            ),
            DescriptionSearchProbe(
                purpose_id="product_category",
                query=DescriptionQuery("product business category"),
                source_span="categoría de producto",
                intended_use=QueryFieldPurpose.DIMENSION,
                roles=(LogicalFieldRole.ATTRIBUTE,),
                canonical_types=(CanonicalType.STRING,),
            ),
            DescriptionSearchProbe(
                purpose_id="net_revenue",
                query=DescriptionQuery("net line amount"),
                source_span="importe neto",
                intended_use=QueryFieldPurpose.METRIC,
                roles=(LogicalFieldRole.MEASURE,),
                canonical_types=(CanonicalType.DECIMAL,),
                metric_operation=MetricOperation.SUM,
            ),
        )
    )
    interpreter = RevenueInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, expansion),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "suma el importe neto por fecha de pedido y categoría de producto",
        UserLanguage.SPANISH,
    )

    assert preview.operational_state is None
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.vocabulary is not None
    assert {item.id.root for item in preview.vocabulary.models} == {
        "SalesOrder",
        "SaleLine",
        "Product",
    }
    assert {item.id for item in preview.vocabulary.joins} == {
        "sales_order_to_sale_line",
        "product_to_sale_line",
    }
    assert {item.logical_field.root for item in preview.vocabulary.candidates} == {
        "SalesOrder.ordered_at",
        "Product.category",
        "SaleLine.net_amount",
    }
    assert preview.shortlist is not None
    assert {
        item.binding.logical_field.root
        for item in preview.shortlist.candidates
        if item.binding.logical_field.root.startswith("Shipment.")
    } == {"Shipment.delivered_at", "Shipment.shipped_at"}
    assert interpreter.calls == 1
    assert len(search.requests) == 6
    assert all(request.page_size == MAX_EXECUTABLE_SHORTLIST + 1 for request in search.requests)
    source_spans = {"fecha de pedido", "categoría de producto", "importe neto"}
    source_requests = tuple(
        request
        for request in search.requests
        if request.query is not None and request.query.root in source_spans
    )
    assert len(source_requests) == 3
    assert all(
        search.requests[index].filters == search.requests[index + 1].filters
        for index in range(0, len(search.requests), 2)
    )


def test_ungrounded_source_span_blocks_provider_query_before_governed_search() -> None:
    registry = _registry()
    configuration = _configuration()
    provider_query = "private customer credit score"
    expansion = DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="registration_date",
                query=DescriptionQuery(provider_query),
                source_span="credit score",
                intended_use=QueryFieldPurpose.DIMENSION,
            ),
        )
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            provider_query: (_binding(registry.value.registry, "SalesOrder.order_status", 9_000),),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, expansion),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("fecha de registro", UserLanguage.SPANISH)

    assert preview.operational_state is QueryStudioOperationalState.PROVIDER_UNAVAILABLE
    assert preview.reason_code == QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value
    assert preview.shortlist is None
    assert search.requests == []
    assert interpreter.calls == 0


def test_provider_query_cannot_promote_a_binding_outside_the_active_registry() -> None:
    registry = _registry()
    configuration = _configuration()
    provider_query = "invented private registration field"
    approved = _binding(registry.value.registry, "Customer.registration_date", 900)
    unapproved = approved.model_copy(update={"mapping_version": approved.mapping_version + 1})
    expansion = DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="registration_date",
                query=DescriptionQuery(provider_query),
                source_span="fecha de registro",
                intended_use=QueryFieldPurpose.DIMENSION,
            ),
        )
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            provider_query: (unapproved,),
            "fecha de registro": (approved,),
        },
    )
    interpreter = FakeInterpreter(configuration)

    with pytest.raises(QueryStudioError) as captured:
        PrepareNaturalLanguageQueryStudioPreview(
            registry=registry,
            search=search,
            expansion=FakeExpansion(configuration, expansion),
            interpreter=interpreter,
            candidate_ids=FakeCandidateIds(),
            preview_tokens=FakeTokens(),
            clock=StaticClock(),
            nonces=StaticNonce(),
            configuration=configuration,
        ).execute("fecha de registro", UserLanguage.SPANISH)

    assert captured.value.code is QueryStudioErrorCode.INVALID_CANDIDATE
    assert interpreter.calls == 0


def test_non_atomic_full_request_source_span_fails_closed_without_search() -> None:
    registry = _registry()
    configuration = _configuration()
    text = "Agrupa por fecha de registro los clientes que sean segundo titular"
    expansion = DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="registration_date",
                query=DescriptionQuery("customer registration date"),
                source_span=text,
                intended_use=QueryFieldPurpose.DIMENSION,
                roles=(LogicalFieldRole.TEMPORAL,),
            ),
        )
    )
    search = StaticGovernedSearch(
        _scope(registry.value.registry),
        {
            "customer registration date": (
                _binding(registry.value.registry, "Customer.registration_date", 900),
            ),
        },
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, expansion),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(text, UserLanguage.SPANISH)

    assert preview.operational_state is QueryStudioOperationalState.PROVIDER_UNAVAILABLE
    assert preview.reason_code == QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value
    assert search.requests == []
    assert interpreter.calls == 0


def test_source_span_search_count_and_pages_remain_bounded_at_maximum_probes() -> None:
    registry = _registry()
    configuration = _configuration()
    source_spans = tuple(f"field concept {index}" for index in range(MAX_DESCRIPTION_PROBES))
    probes = tuple(
        DescriptionSearchProbe(
            purpose_id=f"field_{index}",
            query=DescriptionQuery(f"provider rewrite {index}"),
            source_span=source_span,
            intended_use=QueryFieldPurpose.DIMENSION,
            roles=(LogicalFieldRole.ATTRIBUTE,),
            canonical_types=(CanonicalType.STRING,),
        )
        for index, source_span in enumerate(source_spans)
    )
    search = StaticGovernedSearch(_scope(registry.value.registry), {})

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, DescriptionExpansion(probes=probes)),
        interpreter=FakeInterpreter(configuration),
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(", ".join(source_spans), UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert len(search.requests) == MAX_DESCRIPTION_PROBES * 2
    assert all(request.page_size == MAX_EXECUTABLE_SHORTLIST + 1 for request in search.requests)
    assert all(request.query is not None for request in search.requests)


def test_absent_source_span_preserves_the_exact_hard_filters() -> None:
    registry = _registry()
    configuration = _configuration()
    query = "customer registration date"
    expansion = DescriptionExpansion(
        probes=(
            DescriptionSearchProbe(
                purpose_id="registration_date",
                query=DescriptionQuery(query),
                intended_use=QueryFieldPurpose.DIMENSION,
                roles=(LogicalFieldRole.TEMPORAL,),
                canonical_types=(CanonicalType.DATE,),
            ),
        )
    )
    search = StaticGovernedSearch(_scope(registry.value.registry), {})

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(configuration, expansion),
        interpreter=FakeInterpreter(configuration),
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(query, UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert all(request.query is not None for request in search.requests)
    assert tuple(
        request.query.root for request in search.requests if request.query is not None
    ) == (query,)
    assert search.requests[0].filters != GovernedFieldSearchFilters()


def test_generic_physical_definition_cannot_override_logical_definition_precedence() -> None:
    registry = _registry()
    facts = FakeFacts(
        _scope(registry.value.registry),
        {
            "raw": (
                _binding(
                    registry.value.registry,
                    "AccountHolder.customer_key",
                    900,
                    definition=("Stable definition for bank.account_holders.gf_customer_id."),
                ),
                _binding(
                    registry.value.registry,
                    "Customer.customer_key",
                    900,
                    definition="Acceptance-approved customer identity definition.",
                ),
            )
        },
    )
    search = RegistryAwareGovernedFieldSearch(registry=registry, facts=facts)

    page = search.search(
        _north_star_search_request(
            registry.value.scope,
            "identificador estable del cliente",
            LogicalFieldRole.IDENTIFIER,
        )
    )

    assert tuple(item.logical_field.root for item in page.items) == (
        "Customer.customer_key",
        "AccountHolder.customer_key",
    )
    assert page.items[0].signals.total > page.items[1].signals.total


def test_pg_like_generic_physical_metadata_keeps_north_star_interpretation_aligned() -> None:
    registry = _registry()
    configuration = _configuration()
    logical_fields = tuple(
        sorted(
            {
                item.mapping.logical_field.root
                for item in registry.value.registry.mapping_set.mappings
            }
        )
    )
    facts = FakeFacts(
        _scope(registry.value.registry),
        {
            "raw": tuple(
                _binding(
                    registry.value.registry,
                    logical_field,
                    900,
                    definition=f"Stable definition for {logical_field}.",
                )
                for logical_field in logical_fields
            )
        },
    )
    search = RegistryAwareGovernedFieldSearch(registry=registry, facts=facts)
    expansion = FakeExpansion(configuration)
    expansion.expand = lambda value: DescriptionExpansionResult(  # type: ignore[method-assign]
        expansion=DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="registration_date",
                    query=DescriptionQuery("customer registration date"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                    roles=(LogicalFieldRole.TEMPORAL,),
                    date_grain=DateGrain.DAY,
                ),
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("stable customer identifier"),
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
                DescriptionSearchProbe(
                    purpose_id="holder_role",
                    query=DescriptionQuery("secondary account holder"),
                    intended_use=QueryFieldPurpose.FILTER,
                    roles=(LogicalFieldRole.ATTRIBUTE,),
                    filter_operator=FilterOperator.EQUALS,
                ),
            )
        ),
        usage=_usage(configuration, ProviderStage.EXPANSION),
    )

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=FakeInterpreter(configuration),
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "Agrupa por fecha de registro los clientes que sean segundo titular",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.operational_state is None
    assert preview.proposal is not None
    assert preview.vocabulary is not None
    logical_fields_by_id = {
        item.candidate_id: item.logical_field.root for item in preview.vocabulary.candidates
    }
    assert logical_fields_by_id[preview.proposal.dimensions[0].candidate_id] == (
        "Customer.registration_date"
    )
    assert logical_fields_by_id[preview.proposal.metrics[0].candidate_id] == (
        "Customer.customer_key"
    )
    assert logical_fields_by_id[preview.proposal.filters[0].candidate_id] == (
        "AccountHolder.holder_role"
    )


def test_natural_preview_then_confirmation_is_dynamic_and_builder_runs_only_after_signature() -> (
    None
):
    registry, _, search, expansion, interpreter, tokens, configuration = _service_parts()
    prepare = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=tokens,
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    )

    preview = prepare.execute(
        "Agrupa por fecha de registro los clientes que sean segundo titular",
        UserLanguage.SPANISH,
    )

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.guided_preview is not None
    assert preview.token is not None
    assert preview.vocabulary is not None
    assert len(preview.vocabulary.joins) == 1
    prompt_join = preview.vocabulary.joins[0]
    approved_contract = next(
        contract
        for contract in registry.value.registry.join_contracts.contracts
        if contract.id == prompt_join.id
    )
    assert prompt_join.left_field == approved_contract.left_key.logical_field
    assert prompt_join.right_field == approved_contract.right_key.logical_field
    role = next(
        item
        for item in preview.vocabulary.candidates
        if item.logical_field.root == "AccountHolder.holder_role"
    )
    assert role.allowed_values == ("PRIMARY", "SECONDARY")
    assert registry.loads >= 1

    assert preview.proposal is not None
    assert preview.expansion is not None
    confirmation = QueryStudioConfirmation(
        original_text=DescriptionQuery(
            "Agrupa por fecha de registro los clientes que sean segundo titular"
        ),
        language=UserLanguage.SPANISH,
        expansion=preview.expansion,
        proposal=preview.proposal,
        token=preview.token,
        action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
    )
    confirmed = ConfirmQueryStudioPreview(
        registry=registry,
        search=search,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=tokens,
        clock=StaticClock(),
        configuration=configuration,
        guided_builder=BuildGuidedRequest(registry),
    ).execute(confirmation)

    assert confirmed.validated_request.request.metrics[0].operation is (
        MetricOperation.COUNT_DISTINCT
    )
    assert tuple(model.root for model in confirmed.validated_request.required_models) == (
        "Customer",
        "AccountHolder",
    )
    assert confirmed.validated_request.join_contract_ids == ("customer_to_account_holder",)
    assert expansion.calls == 1
    assert interpreter.calls == 1


def test_natural_edit_requires_exact_server_resign_without_provider_calls_or_ttl_extension() -> (
    None
):
    registry, _, search, expansion, interpreter, tokens, configuration = _service_parts()
    clock = StaticClock()
    candidate_ids = FakeCandidateIds()
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=candidate_ids,
        preview_tokens=tokens,
        clock=clock,
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "Agrupa por fecha de registro los clientes que sean segundo titular",
        UserLanguage.SPANISH,
    )
    assert preview.proposal is not None
    assert preview.expansion is not None
    assert preview.token is not None
    original_payload = tokens.payloads[preview.token.root]
    revised = preview.proposal.model_copy(update={"limit": 250})
    original_context = {
        "original_text": DescriptionQuery(
            "Agrupa por fecha de registro los clientes que sean segundo titular"
        ),
        "language": UserLanguage.SPANISH,
        "expansion": preview.expansion,
    }

    confirmer = ConfirmQueryStudioPreview(
        registry=registry,
        search=search,
        candidate_ids=candidate_ids,
        preview_tokens=tokens,
        clock=clock,
        configuration=configuration,
        guided_builder=BuildGuidedRequest(registry),
    )
    with pytest.raises(QueryStudioError) as old_token_edit:
        confirmer.execute(
            QueryStudioConfirmation(
                **original_context,
                proposal=revised,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
    assert old_token_edit.value.code is QueryStudioErrorCode.STALE_PREVIEW

    resigned = RecomputeNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        candidate_ids=candidate_ids,
        preview_tokens=tokens,
        clock=clock,
        configuration=configuration,
    ).execute(
        QueryStudioInterpretationRevision(
            **original_context,
            signed_proposal=preview.proposal,
            revised_proposal=revised,
            token=preview.token,
            action=QueryStudioRevisionAction.RECOMPUTE_INTERPRETATION,
        )
    )

    assert resigned.proposal == revised
    assert resigned.expansion == preview.expansion
    assert resigned.token is not None
    resigned_payload = tokens.payloads[resigned.token.root]
    assert resigned_payload.issued_at == original_payload.issued_at
    assert resigned_payload.expires_at == original_payload.expires_at
    assert resigned_payload.nonce == original_payload.nonce
    assert expansion.calls == 1
    assert interpreter.calls == 1

    with pytest.raises(QueryStudioError) as new_token_old_proposal:
        confirmer.execute(
            QueryStudioConfirmation(
                **original_context,
                proposal=preview.proposal,
                token=resigned.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )
    assert new_token_old_proposal.value.code is QueryStudioErrorCode.STALE_PREVIEW

    confirmed = confirmer.execute(
        QueryStudioConfirmation(
            **original_context,
            proposal=revised,
            token=resigned.token,
            action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
        )
    )
    assert confirmed.validated_request.request.limit == 250
    assert confirmed.original_text == original_context["original_text"]
    assert confirmed.language is UserLanguage.SPANISH
    assert expansion.calls == 1
    assert interpreter.calls == 1


def test_natural_edit_controls_are_derived_only_from_the_bounded_vocabulary() -> None:
    registry, _, search, expansion, interpreter, tokens, configuration = _service_parts()
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=tokens,
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("consulta de titulares por fecha", UserLanguage.SPANISH)
    assert preview.vocabulary is not None
    narrowed = preview.vocabulary.model_copy(
        update={
            "metric_operations": (MetricOperation.COUNT,),
            "filter_operators": (FilterOperator.EQUALS,),
            "date_grains": (DateGrain.MONTH,),
            "sort_directions": (SortDirection.DESC,),
        }
    )

    controls = natural_language_edit_candidates(narrowed)

    assert tuple(item.candidate_id for item in controls) == tuple(
        item.candidate_id for item in narrowed.candidates
    )
    assert all(
        set(item.metric_operations).issubset(narrowed.metric_operations)
        and set(item.filter_operators).issubset(narrowed.filter_operators)
        and set(item.date_grains).issubset(narrowed.date_grains)
        and set(item.sort_directions).issubset(narrowed.sort_directions)
        for item in controls
    )
    temporal = next(
        item for item in controls if item.logical_field.root == "Customer.registration_date"
    )
    assert temporal.date_grains == (DateGrain.MONTH,)


def test_natural_resign_rejects_noop_expired_and_unknown_candidate_revisions() -> None:
    registry, _, search, expansion, interpreter, tokens, configuration = _service_parts()
    clock = StaticClock()
    candidate_ids = FakeCandidateIds()
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=candidate_ids,
        preview_tokens=tokens,
        clock=clock,
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("consulta de titulares por fecha", UserLanguage.SPANISH)
    assert preview.proposal is not None
    assert preview.expansion is not None
    assert preview.token is not None
    context = {
        "original_text": DescriptionQuery("consulta de titulares por fecha"),
        "language": UserLanguage.SPANISH,
        "expansion": preview.expansion,
        "signed_proposal": preview.proposal,
        "token": preview.token,
        "action": QueryStudioRevisionAction.RECOMPUTE_INTERPRETATION,
    }
    resign = RecomputeNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        candidate_ids=candidate_ids,
        preview_tokens=tokens,
        clock=clock,
        configuration=configuration,
    )

    with pytest.raises(ValidationError, match="must change"):
        QueryStudioInterpretationRevision(
            **context,
            revised_proposal=preview.proposal,
        )

    unknown = OpaqueCandidateId("qsc1_" + "z" * 64)
    unknown_revision = preview.proposal.model_copy(
        update={
            "dimensions": (
                *preview.proposal.dimensions,
                ProposedDimension(candidate_id=unknown),
            )
        }
    )
    with pytest.raises(QueryStudioError) as invalid_candidate:
        resign.execute(
            QueryStudioInterpretationRevision(
                **context,
                revised_proposal=unknown_revision,
            )
        )
    assert invalid_candidate.value.code is QueryStudioErrorCode.INVALID_CANDIDATE

    clock.value = NOW + timedelta(minutes=11)
    with pytest.raises(QueryStudioError) as expired:
        resign.execute(
            QueryStudioInterpretationRevision(
                **context,
                revised_proposal=preview.proposal.model_copy(update={"limit": 200}),
            )
        )
    assert expired.value.code is QueryStudioErrorCode.STALE_PREVIEW
    assert expansion.calls == 1
    assert interpreter.calls == 1


def test_guided_preview_recomputes_before_builder_and_rejects_changed_evidence() -> None:
    registry, _, search, expansion, interpreter, tokens, configuration = _service_parts()
    natural = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=tokens,
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("consulta guiada de titulares", UserLanguage.SPANISH)
    assert natural.shortlist is not None
    assert natural.vocabulary is not None
    assert natural.proposal is not None
    evidence = GuidedQueryStudioEvidence(
        shortlist=natural.shortlist,
        vocabulary=natural.vocabulary,
    )
    guided = PrepareGuidedQueryStudioPreview(
        preview_tokens=tokens,
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(evidence, natural.proposal)
    assert guided.token is not None
    confirmation = QueryStudioConfirmation(
        original_text=None,
        language=None,
        expansion=None,
        proposal=natural.proposal,
        token=guided.token,
        action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
    )
    changed_scope = evidence.shortlist.scope.model_copy(
        update={"catalog_generation_vector_fingerprint": SHA_D}
    )
    changed_evidence = GuidedQueryStudioEvidence(
        shortlist=evidence.shortlist.model_copy(update={"scope": changed_scope}),
        vocabulary=evidence.vocabulary,
    )

    with pytest.raises(QueryStudioError) as captured:
        ConfirmGuidedQueryStudioPreview(
            registry=registry,
            recompute=StaticGuidedRecompute(changed_evidence),
            preview_tokens=tokens,
            clock=StaticClock(),
            configuration=configuration,
            guided_builder=BuildGuidedRequest(registry),
        ).execute(confirmation)
    assert captured.value.code is QueryStudioErrorCode.STALE_PREVIEW


def test_equal_second_third_candidate_tie_outside_top_one_branch_is_ignored() -> None:
    registry = _registry()
    configuration = _configuration()
    rows: dict[str, tuple[GovernedFieldBinding, ...]] = {
        "cliente distinto": (
            _binding(registry.value.registry, "Customer.customer_key", 900),
            _binding(registry.value.registry, "Account.account_key", 800),
            _binding(registry.value.registry, "AccountHolder.customer_key", 800),
        )
    }
    search = StaticGovernedSearch(_scope(registry.value.registry), rows)
    expansion = FakeExpansion(configuration)
    expansion.expand = lambda value: DescriptionExpansionResult(  # type: ignore[method-assign]
        expansion=DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("cliente distinto"),
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
            )
        ),
        usage=_usage(configuration, ProviderStage.EXPANSION),
    )
    interpreter = FirstMetricInterpreter(configuration)
    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("cuenta cliente", UserLanguage.SPANISH)

    assert preview.operational_state is None
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.vocabulary is not None
    assert tuple(item.logical_field.root for item in preview.vocabulary.candidates) == (
        "Customer.customer_key",
    )
    assert interpreter.calls == 1


def test_equal_second_third_candidate_tie_inside_top_one_branch_stays_fail_closed() -> None:
    registry = _registry()
    configuration = _configuration()
    rows: dict[str, tuple[GovernedFieldBinding, ...]] = {
        "cliente distinto": (
            _binding(registry.value.registry, "Customer.customer_key", 900),
            _binding(registry.value.registry, "Customer.country_code", 800),
            _binding(registry.value.registry, "Customer.registration_date", 800),
        )
    }
    search = StaticGovernedSearch(_scope(registry.value.registry), rows)
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery("cliente distinto"),
                    intended_use=QueryFieldPurpose.METRIC,
                    roles=(LogicalFieldRole.IDENTIFIER,),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
            )
        ),
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("cuenta cliente", UserLanguage.SPANISH)

    assert preview.operational_state is None
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.reason_code is None
    assert preview.vocabulary is not None
    assert tuple(item.logical_field.root for item in preview.vocabulary.candidates) == (
        "Customer.customer_key",
    )
    assert interpreter.calls == 1


def test_owner_allowlist_is_applied_before_the_twenty_one_candidate_page_cutoff() -> None:
    registry = _registry()
    configuration = _configuration()
    query = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    wrong_fields = tuple(
        dict.fromkeys(
            item.mapping.logical_field.root
            for item in registry.value.registry.mapping_set.mappings
            if not item.mapping.logical_field.root.startswith("Customer.")
        )
    )[:21]
    assert len(wrong_fields) == 21
    wrong = tuple(
        _binding(
            registry.value.registry,
            logical_field,
            10_000 - index,
            definition=query,
        )
        for index, logical_field in enumerate(wrong_fields)
    )
    correct = _binding(
        registry.value.registry,
        "Customer.customer_key",
        1,
        definition="alpha bravo charlie delta echo foxtrot golf",
    )
    facts = FakeFacts(
        _scope(registry.value.registry),
        {"governed_population": (*wrong, correct)},
    )
    search = RecordingGovernedSearch(
        RegistryAwareGovernedFieldSearch(registry=registry, facts=facts),
        [],
    )
    expansion = FakeExpansion(
        configuration,
        DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="customer_metric",
                    query=DescriptionQuery(query),
                    intended_use=QueryFieldPurpose.METRIC,
                    semantic_focus=("customer", "identifier"),
                    owner_focus=("customer",),
                    metric_operation=MetricOperation.COUNT_DISTINCT,
                ),
            )
        ),
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("count distinct customers", UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.vocabulary is not None
    assert tuple(item.logical_field.root for item in preview.vocabulary.candidates) == (
        "Customer.customer_key",
    )
    assert search.requests[0].page_size == 21
    assert facts.requests
    assert all(request.filters.restrict_logical_fields for request in facts.requests)
    assert all(
        {field.root.split(".", 1)[0] for field in request.filters.logical_fields} == {"Customer"}
        for request in facts.requests
    )
    assert interpreter.calls == 1


def test_unknown_owner_focus_returns_typed_no_match_with_an_empty_exact_allowlist() -> None:
    registry = _registry()
    configuration = _configuration()
    facts = FakeFacts(
        _scope(registry.value.registry),
        {"governed_population": (_binding(registry.value.registry, "Customer.customer_key", 900),)},
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=RegistryAwareGovernedFieldSearch(registry=registry, facts=facts),
        expansion=FakeExpansion(
            configuration,
            DescriptionExpansion(
                probes=(
                    DescriptionSearchProbe(
                        purpose_id="unknown_metric",
                        query=DescriptionQuery("unknown entity identifier"),
                        intended_use=QueryFieldPurpose.METRIC,
                        semantic_focus=("unknown", "identifier"),
                        owner_focus=("unknown",),
                        metric_operation=MetricOperation.COUNT_DISTINCT,
                    ),
                )
            ),
        ),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("count distinct unknown entities", UserLanguage.ENGLISH)

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert facts.requests
    assert all(request.filters.restrict_logical_fields for request in facts.requests)
    assert all(request.filters.logical_fields == () for request in facts.requests)
    assert interpreter.calls == 0


def test_unknown_qualified_metric_cannot_fall_back_after_probe_rewrite() -> None:
    registry = _registry()
    configuration = _configuration()
    decoy = _binding(
        registry.value.registry,
        "Customer.country_code",
        10_000,
        definition="Customer secret credit score.",
    )
    foreign = _binding(
        registry.value.registry,
        "Product.category",
        9_000,
        definition="Customer secret credit score.",
    )
    facts = FakeFacts(
        _scope(registry.value.registry),
        {"governed_population": (foreign, decoy)},
    )
    search = RecordingGovernedSearch(
        RegistryAwareGovernedFieldSearch(registry=registry, facts=facts),
        [],
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(
            configuration,
            DescriptionExpansion(
                probes=(
                    DescriptionSearchProbe(
                        purpose_id="metric_1",
                        query=DescriptionQuery("customer identifier"),
                        source_span="Customer",
                        intended_use=QueryFieldPurpose.METRIC,
                        semantic_focus=("customer", "identifier"),
                        owner_focus=("customer",),
                        roles=(LogicalFieldRole.IDENTIFIER,),
                        metric_operation=MetricOperation.COUNT_DISTINCT,
                    ),
                )
            ),
        ),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "count Customer.secret_credit_score by Customer.registration_date",
        UserLanguage.ENGLISH,
    )

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.shortlist.scope == _scope(registry.value.registry)
    assert preview.shortlist.candidates == ()
    assert len(search.requests) == 1
    assert search.requests[0].query == DescriptionQuery("customer.secret_credit_score")
    assert search.requests[0].filters.logical_models == (LogicalModelRef("Customer"),)
    assert len(facts.requests) == 1
    assert facts.requests[0].filters.restrict_logical_fields is True
    assert {field.root.split(".", 1)[0] for field in facts.requests[0].filters.logical_fields} == {
        "Customer"
    }
    assert interpreter.calls == 0


def test_three_segment_path_uses_empty_owner_allowlist_before_decoy_scoring() -> None:
    registry = _registry()
    configuration = _configuration()
    decoy = _binding(
        registry.value.registry,
        "Customer.customer_key",
        10_000,
        definition="Database customer key.",
    )
    facts = FakeFacts(
        _scope(registry.value.registry),
        {"governed_population": (decoy,)},
    )
    search = RecordingGovernedSearch(
        RegistryAwareGovernedFieldSearch(registry=registry, facts=facts),
        [],
    )
    interpreter = FirstMetricInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=FakeExpansion(
            configuration,
            DescriptionExpansion(
                probes=(
                    DescriptionSearchProbe(
                        purpose_id="metric_1",
                        query=DescriptionQuery("customer identifier"),
                        source_span="Customer",
                        intended_use=QueryFieldPurpose.METRIC,
                        semantic_focus=("customer", "identifier"),
                        owner_focus=("customer",),
                        roles=(LogicalFieldRole.IDENTIFIER,),
                        metric_operation=MetricOperation.COUNT_DISTINCT,
                    ),
                )
            ),
        ),
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute(
        "count db.Customer.customer_key by Customer.registration_date",
        UserLanguage.ENGLISH,
    )

    assert preview.semantic_state is SemanticMatchState.NO_MATCH
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.shortlist.candidates == ()
    assert len(search.requests) == 1
    assert search.requests[0].query == DescriptionQuery("db.customer.customer_key")
    assert search.requests[0].filters.logical_models == ()
    assert len(facts.requests) == 1
    assert facts.requests[0].filters.restrict_logical_fields is True
    assert facts.requests[0].filters.logical_fields == ()
    assert interpreter.calls == 0


def test_equal_twentieth_twenty_first_candidate_tie_is_not_silently_truncated() -> None:
    registry = _registry()
    configuration = _configuration()
    logical_fields = tuple(
        item.mapping.logical_field.root
        for item in registry.value.registry.mapping_set.mappings[:21]
    )
    values = tuple(
        sorted(
            (
                _binding(
                    registry.value.registry,
                    logical_field,
                    10_000 - index if index < 19 else 100,
                )
                for index, logical_field in enumerate(logical_fields)
            ),
            key=lambda item: (
                -item.signals.total,
                item.logical_field.root,
                item.binding_id,
            ),
        )
    )
    search = StaticGovernedSearch(_scope(registry.value.registry), {"campo": values})
    expansion = FakeExpansion(configuration)
    expansion.expand = lambda value: DescriptionExpansionResult(  # type: ignore[method-assign]
        expansion=DescriptionExpansion(
            probes=(
                DescriptionSearchProbe(
                    purpose_id="requested_field",
                    query=DescriptionQuery("campo"),
                    intended_use=QueryFieldPurpose.DIMENSION,
                ),
            )
        ),
        usage=_usage(configuration, ProviderStage.EXPANSION),
    )
    interpreter = FakeInterpreter(configuration)

    preview = PrepareNaturalLanguageQueryStudioPreview(
        registry=registry,
        search=search,
        expansion=expansion,
        interpreter=interpreter,
        candidate_ids=FakeCandidateIds(),
        preview_tokens=FakeTokens(),
        clock=StaticClock(),
        nonces=StaticNonce(),
        configuration=configuration,
    ).execute("campo", UserLanguage.SPANISH)

    assert search.requests[-1].page_size == 21
    assert preview.operational_state is QueryStudioOperationalState.CLOSURE_OVERFLOW
    assert preview.reason_code == "candidate_tie_overflow"
    assert interpreter.calls == 0


def test_physical_discovery_remains_non_executable_and_never_calls_planner() -> None:
    registry = _registry()
    binding = _binding(registry.value.registry, "Customer.customer_key", 900)
    page = PhysicalFieldDiscoveryPage(
        items=(
            PhysicalDiscoveryCandidate(
                locator=binding.locator,
                generation=4,
                asset_qualified_name="crm.customers",
                native_type="varchar",
                definition="Ungoverned synthetic discovery result.",
                metadata_fingerprint=SHA_A,
            ),
        ),
        page_size=20,
    )
    fake = FakePhysicalDiscovery(page)
    result = DiscoverPhysicalFields(fake).execute(
        PhysicalFieldDiscoveryRequest(
            scope=registry.value.scope,
            query=DescriptionQuery("customer id"),
            page_size=20,
        )
    )

    assert fake.calls == 1
    assert result.items[0].status.value == "needs_mapping_review"
    assert "candidate_id" not in result.model_dump_json()
    assert "logical_field" not in result.model_dump_json()


def _north_star_search_request(
    scope: SemanticRegistryScope,
    query: str,
    role: LogicalFieldRole,
) -> GovernedFieldSearchRequest:
    return GovernedFieldSearchRequest(
        scope=scope,
        query=DescriptionQuery(query),
        filters=GovernedFieldSearchFilters(roles=(role,)),
        page_size=20,
    )
