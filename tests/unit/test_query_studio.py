"""Pure Query Studio bounds, state separation, and non-executable model contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.concepts import CanonicalType, LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    MAX_DESCRIPTION_BYTES,
    QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    ExecutableEvidenceStatus,
    GovernedBindingFactsFilters,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchFilters,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
    GovernedSearchKey,
    OpaqueCandidateId,
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryStatus,
    PreviewTokenPayload,
    ProposalAmbiguityKind,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProviderConfigurationFacts,
    QueryFieldPurpose,
    QueryStudioModelProposal,
    QueryStudioOperationalState,
    QueryStudioScopeSnapshot,
    SearchSignal,
    SearchSignalBreakdown,
    SearchSignalCode,
    SemanticMatchState,
    atomic_description_expansion,
    classify_description_expansion_route,
    governed_probe_search_requests,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import DateGrain, FilterOperator, MetricOperation
from schemabridge.domain.semantic_registry import (
    PhysicalValueType,
    SemanticRegistryScope,
)

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _scope() -> QueryStudioScopeSnapshot:
    return QueryStudioScopeSnapshot(
        scope=SemanticRegistryScope(
            workspace_id="workspace_alpha",
            catalog_scope="synthetic-demo",
            registry_id="synthetic_enterprise",
        ),
        registry_version=1,
        registry_fingerprint=SHA_A,
        pointer_generation=4,
        pointer_fingerprint=SHA_B,
        evidence_head_revision=7,
        evidence_baseline_revision=6,
        evidence_baseline_fingerprint=SHA_C,
        catalog_generation_vector_fingerprint=SHA_D,
    )


def _binding(
    *,
    logical_field: str = "Customer.registration_date",
    physical_field: str = "crm.customers.registration_date",
    binding_id: str = "binding_customer_registration",
    score: int = 800,
) -> GovernedFieldBinding:
    schema_name, table_name, *field_path = physical_field.split(".")
    return GovernedFieldBinding(
        binding_id=binding_id,
        binding_fingerprint=SHA_A,
        logical_field=LogicalFieldRef(logical_field),
        physical_field=PhysicalFieldRef(physical_field),
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id="workspace_alpha",
                connection_id=CatalogConnectionId("connection_primary"),
                asset_id=CatalogAssetId(f"asset:{schema_name}.{table_name}"),
            ),
            field_path=tuple(field_path),
        ),
        mapping_version=1,
        mapping_approval_decision_id=f"decision-{binding_id}",
        physical_type=PhysicalValueType.TIMESTAMP,
        evidence_status=ExecutableEvidenceStatus.CURRENT,
        catalog_generation=3,
        catalog_generation_fingerprint=SHA_B,
        asset_qualified_name=f"{schema_name}.{table_name}",
        asset_metadata_fingerprint=SHA_A,
        field_metadata_fingerprint=SHA_B,
        field_definition_fingerprint=SHA_C,
        field_terms_fingerprint=SHA_D,
        native_type="timestamp",
        definition="Synthetic registration timestamp.",
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


def test_description_normalization_is_bounded_by_characters_and_utf8_bytes() -> None:
    assert DescriptionQuery("  fecha   de registro\ncliente  ").root == (
        "fecha de registro cliente"
    )

    assert len(DescriptionQuery("😀" * 2_000).root.encode("utf-8")) <= (MAX_DESCRIPTION_BYTES)
    with pytest.raises(ValidationError, match="2,000 characters"):
        DescriptionQuery("a" * 2_001)
    with pytest.raises(ValidationError, match="control"):
        DescriptionQuery("fecha\x00registro")
    with pytest.raises(ValidationError, match="blank"):
        DescriptionQuery(" \n\t ")


def test_description_expansion_route_is_nominal_not_inferred_from_text() -> None:
    text = DescriptionQuery("customer_id")
    field_match = DescriptionExpansionInput(
        text=text,
        language=UserLanguage.ENGLISH,
        lane=DescriptionExpansionRoute.FIELD_MATCH,
    )
    analytical = DescriptionExpansionInput(
        text=text,
        language=UserLanguage.ENGLISH,
        lane=DescriptionExpansionRoute.ANALYTICAL,
    )

    assert (
        classify_description_expansion_route(field_match) is DescriptionExpansionRoute.FIELD_MATCH
    )
    assert classify_description_expansion_route(analytical) is DescriptionExpansionRoute.ANALYTICAL


@pytest.mark.parametrize("text", ("a" * 257, "😀" * 129))
def test_atomic_description_expansion_rejects_overlong_local_source_span(text: str) -> None:
    with pytest.raises(ValueError, match="atomic field description"):
        atomic_description_expansion(
            DescriptionExpansionInput(
                text=DescriptionQuery(text),
                language=UserLanguage.ENGLISH,
                lane=DescriptionExpansionRoute.FIELD_MATCH,
            )
        )


def test_expansion_stably_deduplicates_only_set_like_enum_collections() -> None:
    probe = DescriptionSearchProbe(
        purpose_id="registration_date",
        query=DescriptionQuery("fecha de registro"),
        intended_use=QueryFieldPurpose.DIMENSION,
        roles=(
            LogicalFieldRole.TEMPORAL,
            LogicalFieldRole.ATTRIBUTE,
            LogicalFieldRole.TEMPORAL,
            LogicalFieldRole.ATTRIBUTE,
        ),
        canonical_types=(
            CanonicalType.DATE,
            CanonicalType.TIMESTAMP,
            CanonicalType.DATE,
        ),
        date_grain=DateGrain.DAY,
    )
    expansion = DescriptionExpansion(
        probes=(probe,),
        ambiguity_hints=(
            ProposalAmbiguityKind.DATE_MEANING,
            ProposalAmbiguityKind.FIELD_MEANING,
            ProposalAmbiguityKind.DATE_MEANING,
        ),
    )

    assert probe.roles == (
        LogicalFieldRole.TEMPORAL,
        LogicalFieldRole.ATTRIBUTE,
    )
    assert probe.canonical_types == (
        CanonicalType.DATE,
        CanonicalType.TIMESTAMP,
    )
    assert expansion.ambiguity_hints == (
        ProposalAmbiguityKind.DATE_MEANING,
        ProposalAmbiguityKind.FIELD_MEANING,
    )
    clean_probe = DescriptionSearchProbe(
        purpose_id="registration_date",
        query=DescriptionQuery("fecha de registro"),
        intended_use=QueryFieldPurpose.DIMENSION,
        roles=probe.roles,
        canonical_types=probe.canonical_types,
        date_grain=DateGrain.DAY,
    )
    assert (
        expansion.fingerprint
        == DescriptionExpansion(
            probes=(clean_probe,),
            ambiguity_hints=expansion.ambiguity_hints,
        ).fingerprint
    )

    with pytest.raises(ValidationError, match="purposes must be unique"):
        DescriptionExpansion(probes=(probe, probe))
    with pytest.raises(ValidationError, match="metric operation"):
        DescriptionSearchProbe(
            purpose_id="customer_count",
            query=DescriptionQuery("número de clientes"),
            intended_use=QueryFieldPurpose.METRIC,
            roles=(
                LogicalFieldRole.IDENTIFIER,
                LogicalFieldRole.IDENTIFIER,
            ),
            canonical_types=(
                CanonicalType.STRING,
                CanonicalType.STRING,
            ),
        )


def test_model_proposal_forbids_sql_assets_tools_and_approvals() -> None:
    candidate = OpaqueCandidateId("qsc1_" + "a" * 64)
    payload = {
        "semantic_state": "aligned",
        "primary_candidate_id": candidate.root,
        "metrics": [
            {
                "candidate_id": candidate.root,
                "operation": "count_distinct",
                "alias": "customers",
            }
        ],
        "raw_sql": "DROP TABLE customers",
        "physical_asset": "crm.customers",
        "tool": "database",
        "approved": True,
        "execute": True,
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryStudioModelProposal.model_validate(payload)


def test_semantic_and_operational_states_are_distinct_closed_enums() -> None:
    assert SemanticMatchState.AMBIGUOUS.value == "ambiguous"
    assert QueryStudioOperationalState.PROVIDER_UNAVAILABLE.value == "provider_unavailable"
    assert not set(SemanticMatchState).intersection(QueryStudioOperationalState)


def test_physical_discovery_has_no_executable_or_logical_candidate_surface() -> None:
    discovery = PhysicalDiscoveryCandidate(
        locator=_binding().locator,
        generation=3,
        asset_qualified_name="crm.customers",
        native_type="timestamp",
        definition="Synthetic field.",
        metadata_fingerprint=SHA_A,
    )
    dumped = discovery.model_dump()

    assert discovery.status is PhysicalDiscoveryStatus.NEEDS_MAPPING_REVIEW
    assert "candidate_id" not in dumped
    assert "logical_field" not in dumped
    assert "mapping_version" not in dumped


def test_governed_page_uses_integer_order_and_scope_bound_continuation() -> None:
    first = _binding(score=900)
    second = _binding(
        logical_field="Customer.customer_key",
        physical_field="crm.customers.customer_id",
        binding_id="binding_customer_key",
        score=800,
    )
    scope = _scope()
    request = GovernedFieldSearchRequest(
        scope=scope.scope,
        query=DescriptionQuery("registration date"),
    )
    next_key = second.search_key(
        scope.fingerprint,
        request.request_fingerprint,
        SHA_B,
    )
    page = GovernedFieldSearchPage(
        scope=scope,
        request_fingerprint=request.request_fingerprint,
        binding_facts_fingerprint=SHA_B,
        items=(first, second),
        page_size=2,
        rows_read=3,
        next_key=next_key,
    )

    assert page.next_key is not None
    assert page.next_key.scope_fingerprint == scope.fingerprint
    changed = next_key.model_copy(update={"scope_fingerprint": SHA_A})
    with pytest.raises(ValidationError, match="continuation"):
        GovernedFieldSearchPage(
            scope=scope,
            request_fingerprint=request.request_fingerprint,
            binding_facts_fingerprint=SHA_B,
            items=(first, second),
            page_size=2,
            rows_read=3,
            next_key=GovernedSearchKey.model_validate(changed),
        )
    with pytest.raises(ValidationError, match="query or logical filters"):
        GovernedFieldSearchRequest(
            scope=scope.scope,
            query=DescriptionQuery("different request"),
            after=next_key,
            expected_scope=scope,
        )


def test_governed_cursor_invalidates_logical_and_raw_filter_changes_not_page_size() -> None:
    scope = _scope()
    logical = GovernedFieldSearchRequest(
        scope=scope.scope,
        query=DescriptionQuery("registration date"),
        filters=GovernedFieldSearchFilters(roles=(LogicalFieldRole.TEMPORAL,)),
        page_size=20,
    )
    assert (
        logical.request_fingerprint
        == logical.model_copy(update={"page_size": 50}).request_fingerprint
    )
    raw = GovernedBindingFactsRequest(
        scope=scope.scope,
        query=logical.query,
        filters=GovernedBindingFactsFilters(
            logical_fields=(LogicalFieldRef("Customer.registration_date"),),
            restrict_logical_fields=True,
        ),
        logical_request_fingerprint=logical.request_fingerprint,
    )
    key = _binding().search_key(
        scope.fingerprint,
        logical.request_fingerprint,
        raw.request_fingerprint,
    )

    GovernedBindingFactsRequest(
        **raw.model_dump(mode="python", exclude={"after", "expected_scope"}),
        after=key,
        expected_scope=scope,
    )
    with pytest.raises(ValidationError, match="raw filters"):
        GovernedBindingFactsRequest(
            scope=scope.scope,
            query=raw.query,
            filters=GovernedBindingFactsFilters(
                logical_fields=(LogicalFieldRef("Customer.customer_key"),),
                restrict_logical_fields=True,
            ),
            logical_request_fingerprint=logical.request_fingerprint,
            after=key,
            expected_scope=scope,
        )
    with pytest.raises(ValidationError, match="logical filters"):
        GovernedFieldSearchRequest(
            scope=scope.scope,
            query=logical.query,
            filters=GovernedFieldSearchFilters(roles=(LogicalFieldRole.ATTRIBUTE,)),
            after=key,
            expected_scope=scope,
        )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("customer count", "stable customer identifier"),
        ("conteo de clientes", "stable customer identifier"),
        ("product count", "stable product identifier"),
        ("cuenta pedidos", "stable order identifier"),
    ],
)
def test_count_distinct_probe_adds_one_identifier_role_retrieval_variant(
    query: str,
    expected: str,
) -> None:
    probe = DescriptionSearchProbe(
        purpose_id="entity_metric",
        query=DescriptionQuery(query),
        source_span=query,
        intended_use=QueryFieldPurpose.METRIC,
        metric_operation=MetricOperation.COUNT_DISTINCT,
    )

    requests = governed_probe_search_requests(_scope().scope, probe)
    derived = tuple(request for request in requests if request.query == DescriptionQuery(expected))

    assert len(derived) == 1
    assert derived[0].filters.roles == (LogicalFieldRole.IDENTIFIER,)
    assert derived[0].filters.canonical_types == ()
    assert derived[0].page_size == 21


@pytest.mark.parametrize(
    ("query", "operation"),
    [
        ("customer and account count", MetricOperation.COUNT_DISTINCT),
        ("stable customer identifier", MetricOperation.COUNT_DISTINCT),
        ("net revenue", MetricOperation.SUM),
    ],
)
def test_metric_probe_does_not_invent_an_identifier_variant_without_one_entity(
    query: str,
    operation: MetricOperation,
) -> None:
    probe = DescriptionSearchProbe(
        purpose_id="metric",
        query=DescriptionQuery(query),
        source_span=query,
        intended_use=QueryFieldPurpose.METRIC,
        metric_operation=operation,
    )

    requests = governed_probe_search_requests(_scope().scope, probe)

    assert all(
        request.filters.roles != (LogicalFieldRole.IDENTIFIER,) or request.query == probe.query
        for request in requests
    )


def test_preview_token_payload_is_text_free_scope_bound_and_at_most_ten_minutes() -> None:
    payload = PreviewTokenPayload(
        request_digest=SHA_A,
        shortlist_fingerprint=SHA_B,
        proposal_fingerprint=SHA_C,
        configuration_fingerprint=SHA_D,
        scope_digest=SHA_A,
        preview_fingerprint=SHA_B,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        nonce="nonce_0123456789abcdef",
    )
    dumped = payload.model_dump(mode="json")

    assert set(dumped) == {
        "version",
        "request_digest",
        "shortlist_fingerprint",
        "proposal_fingerprint",
        "configuration_fingerprint",
        "scope_digest",
        "preview_fingerprint",
        "issued_at",
        "expires_at",
        "nonce",
    }
    assert "text" not in dumped
    assert "candidate" not in dumped
    assert "workspace" not in dumped
    with pytest.raises(ValidationError, match="ten minutes"):
        PreviewTokenPayload.model_validate(
            {
                **payload.model_dump(mode="python"),
                "expires_at": NOW + timedelta(seconds=601),
            }
        )


def test_provider_configuration_fingerprint_detects_model_or_region_change() -> None:
    configuration = ProviderConfigurationFacts.create(
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

    assert configuration.orchestration_policy_version == QUERY_STUDIO_ORCHESTRATION_POLICY_VERSION
    assert len(configuration.fingerprint) == 64
    with pytest.raises(ValidationError, match="does not match"):
        ProviderConfigurationFacts.model_validate(
            {
                **configuration.model_dump(mode="python"),
                "model_snapshot": "another-model",
            }
        )
    with pytest.raises(ValidationError, match="does not match"):
        ProviderConfigurationFacts.model_validate(
            {
                **configuration.model_dump(mode="python"),
                "orchestration_policy_version": "m27-local-atomic-preflight-v999",
            }
        )


def test_aligned_proposal_requires_candidate_only_typed_request() -> None:
    candidate = OpaqueCandidateId("qsc1_" + "b" * 64)
    proposal = QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=candidate,
        metrics=(
            ProposedMetric(
                candidate_id=candidate,
                operation=MetricOperation.COUNT_DISTINCT,
            ),
        ),
    )

    assert proposal.referenced_candidate_ids == (candidate,)
    assert "sql" not in proposal.model_dump_json().casefold()
    with pytest.raises(ValidationError, match="complete unambiguous"):
        QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate,
            metrics=(),
        )


def test_model_proposal_rejects_duplicate_filter_candidate_ids() -> None:
    candidate = OpaqueCandidateId("qsc1_" + "d" * 64)

    with pytest.raises(ValidationError, match="unique per selection kind"):
        QueryStudioModelProposal(
            semantic_state=SemanticMatchState.ALIGNED,
            primary_candidate_id=candidate,
            metrics=(
                ProposedMetric(
                    candidate_id=candidate,
                    operation=MetricOperation.COUNT_DISTINCT,
                ),
            ),
            filters=(
                ProposedFilter(
                    candidate_id=candidate,
                    operator=FilterOperator.EQUALS,
                    value="first",
                ),
                ProposedFilter(
                    candidate_id=candidate,
                    operator=FilterOperator.EQUALS,
                    value="second",
                ),
            ),
        )


def test_ambiguous_proposal_cannot_smuggle_a_partial_executable_request() -> None:
    candidate = OpaqueCandidateId("qsc1_" + "c" * 64)

    with pytest.raises(ValidationError, match="only explicit ambiguity"):
        QueryStudioModelProposal(
            semantic_state=SemanticMatchState.AMBIGUOUS,
            primary_candidate_id=candidate,
            dimensions=(ProposedDimension(candidate_id=candidate),),
            ambiguities=(ProposalAmbiguityKind.FIELD_MEANING,),
        )
