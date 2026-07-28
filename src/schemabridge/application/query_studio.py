"""Dynamic Query Studio orchestration over governed candidates and exact confirmation."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedDimensionInput,
    GuidedFilterInput,
    GuidedMetricInput,
    GuidedOrderInput,
    GuidedRequestInput,
)
from schemabridge.application.ports.planning import GovernedSemanticRegistryPort
from schemabridge.application.ports.query_studio import (
    DescriptionExpansionPort,
    GovernedBindingFactsSearchPort,
    GovernedFieldSearchPort,
    GuidedQueryStudioRecomputePort,
    GuidedQueryStudioSelectionPort,
    PhysicalFieldDiscoveryPort,
    QueryStudioCandidateIdPort,
    QueryStudioClockPort,
    QueryStudioIntentPort,
    QueryStudioNoncePort,
    QueryStudioPortError,
    QueryStudioPortErrorCode,
    QueryStudioPreviewTokenPort,
)
from schemabridge.domain.concepts import CanonicalType, LogicalModelRef
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    MAX_CANDIDATES_PER_PURPOSE,
    MAX_EXECUTABLE_SHORTLIST,
    MAX_GOVERNED_SEARCH_PAGE_SIZE,
    MAX_GUIDED_BINDINGS,
    MAX_GUIDED_LOGICAL_FIELDS,
    MAX_PREVIEW_TTL_SECONDS,
    MAX_PROMPT_FIELDS,
    MAX_PROMPT_JOINS,
    MAX_PROMPT_MODELS,
    ApprovedPublicMetadataSurface,
    CandidateRiskCode,
    ConfirmedQueryStudioRequest,
    DescriptionExpansion,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    DescriptionExpansionRoute,
    DescriptionQuery,
    DescriptionSearchProbe,
    ExecutableEvidenceStatus,
    GovernedBindingFactsFilters,
    GovernedBindingFactsRequest,
    GovernedFieldBinding,
    GovernedFieldSearchPage,
    GovernedFieldSearchRequest,
    GovernedSearchKey,
    GovernedShortlist,
    GuidedBrowseContext,
    GuidedDraftDimension,
    GuidedDraftFilter,
    GuidedDraftMetric,
    GuidedDraftOrder,
    GuidedGovernedCandidate,
    GuidedGovernedFieldPage,
    GuidedQueryStudioEvidence,
    GuidedRequestDraft,
    GuidedRequestPreview,
    OpaqueCandidateId,
    PhysicalDiscoveryCardinality,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    PreviewTokenPayload,
    ProviderConfigurationFacts,
    ProviderOutputFailureCategory,
    ProviderUsageFacts,
    QueryFieldPurpose,
    QueryStudioConfirmation,
    QueryStudioEditableCandidate,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioInterpretationRevision,
    QueryStudioModelProposal,
    QueryStudioOperationalState,
    QueryStudioPreview,
    QueryStudioPromptCandidate,
    QueryStudioPromptJoin,
    QueryStudioPromptModel,
    QueryStudioPromptPurposeScore,
    QueryStudioPromptVocabulary,
    QueryStudioRequiredSelectionCounts,
    QueryStudioScopeSnapshot,
    RankedGovernedCandidate,
    SemanticMatchState,
    confirmation_fingerprint,
    find_explicit_logical_field_references,
    find_malformed_qualified_paths,
    governed_probe_search_requests,
    query_studio_fingerprint,
    query_studio_request_digest,
)
from schemabridge.domain.query_studio_matching import (
    GOVERNED_DESCRIPTION_MATCHER_VERSION,
    score_governed_description,
)
from schemabridge.domain.request_context import LogicalFieldRole
from schemabridge.domain.requests import DateGrain, FilterOperator, MetricOperation, SortDirection
from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)
from schemabridge.domain.transformations import MapValuesStep

_SQL_LIKE_VALUE = re.compile(
    r"(?i)(\b(?:select|insert|update|delete|drop|alter|create|truncate|copy|grant|revoke)\b|"
    r"/\*|--|;)"
)
_NON_ATOMIC_ANALYTICAL_SOURCE_SPAN = re.compile(
    r"\bagrupa(?:r|dos?|das?)?\b[^.?!]{0,100}\bpor\b"
    r"|\bgroup(?:ed|ing)?\b[^.?!]{0,100}\bby\b"
    r"|\b(?:count|sum|average|avg|suma(?:r)?|promedio)\b[^.?!]{0,100}\b(?:by|por)\b"
)
_CONDITIONAL_SOURCE_SPAN = re.compile(
    r"\b(?:where|whose|donde)\b"
    r"|\b(?:who|that)\s+(?:is|are)\b"
    r"|\bque\s+sean?\b"
)
_BARE_IDENTIFIER_LIKE_FIELD = re.compile(
    r"^(?:[a-z][a-z0-9_]*_(?:id|key)|(?:id|key|identifier|identificador|clave))$"
)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class QueryStudioErrorCode(StrEnum):
    INVALID_CONTEXT = "query_studio_invalid_context"
    INVALID_CANDIDATE = "query_studio_invalid_candidate"
    INVALID_PROPOSAL = "query_studio_invalid_proposal"
    CLOSURE_OVERFLOW = "query_studio_closure_overflow"
    STALE_PREVIEW = "query_studio_stale_preview"
    CONFIRMATION_REQUIRED = "query_studio_confirmation_required"


class QueryStudioError(RuntimeError):
    """Fail-closed application error without prompt, metadata, or provider payloads."""

    def __init__(self, code: QueryStudioErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Closure:
    shortlist: GovernedShortlist
    vocabulary: QueryStudioPromptVocabulary | None
    semantic_state: SemanticMatchState | None = None
    operational_state: QueryStudioOperationalState | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class SearchGovernedFields:
    """Expose only the executable governed lane with current-registry validation."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort

    def execute(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        loaded = self.registry.load()
        if request.scope != loaded.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed field search scope does not match the active registry",
            )
        page = _validated_model(
            self.search.search(request),
            GovernedFieldSearchPage,
            "governed field-search response",
        )
        if page.request_fingerprint != request.request_fingerprint:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed field-search response does not match its request",
            )
        _ensure_registry_still_matches(loaded, page.scope)
        for item in page.items:
            _validate_binding_against_registry(item, loaded.registry)
        if request.expected_scope is not None and request.expected_scope != page.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed field-search continuation no longer matches current state",
            )
        return page


@dataclass(frozen=True, slots=True)
class RegistryAwareGovernedFieldSearch:
    """Rank a bounded governed population with active logical-registry context."""

    registry: GovernedSemanticRegistryPort
    facts: GovernedBindingFactsSearchPort

    def search(self, request: GovernedFieldSearchRequest) -> GovernedFieldSearchPage:
        request = GovernedFieldSearchRequest.model_validate(request.model_dump(mode="python"))
        loaded = self.registry.load()
        if request.scope != loaded.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed field search scope does not match the active registry",
            )
        allowlist = _logical_allowlist(
            roles=request.filters.roles,
            canonical_types=request.filters.canonical_types,
            logical_models=request.filters.logical_models,
            registry=loaded.registry,
        )
        if request.query is not None:
            return self._search_description(
                request=request,
                loaded=loaded,
                allowlist=allowlist,
            )
        facts_request = GovernedBindingFactsRequest(
            scope=request.scope,
            query=None,
            filters=allowlist,
            page_size=request.page_size,
            after=request.after,
            expected_scope=request.expected_scope,
            logical_request_fingerprint=request.request_fingerprint,
        )
        page = _validated_model(
            self.facts.search(facts_request),
            GovernedFieldSearchPage,
            "governed binding-facts response",
        )
        if (
            page.request_fingerprint != request.request_fingerprint
            or page.binding_facts_fingerprint != facts_request.request_fingerprint
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed binding-facts response does not match its request",
            )
        _ensure_registry_still_matches(loaded, page.scope)
        if request.expected_scope is not None and request.expected_scope != page.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed binding-facts continuation no longer matches current state",
            )
        allowed = {field.root for field in allowlist.logical_fields}
        if allowlist.restrict_logical_fields and any(
            item.logical_field.root not in allowed for item in page.items
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CANDIDATE,
                "Governed binding-facts response escaped its logical allowlist",
            )
        for item in page.items:
            _validate_binding_against_registry(item, loaded.registry)
        return page

    def _search_description(
        self,
        *,
        request: GovernedFieldSearchRequest,
        loaded: ScopedSemanticRegistrySnapshot,
        allowlist: GovernedBindingFactsFilters,
    ) -> GovernedFieldSearchPage:
        """Scan at most 1,000 governed bindings, never the physical inventory."""

        query = request.query
        if query is None:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed description search requires bounded business text",
            )
        raw_items: list[GovernedFieldBinding] = []
        seen_bindings: set[str] = set()
        seen_continuations: set[tuple[int, str, str]] = set()
        raw_after: GovernedSearchKey | None = None
        raw_expected_scope: QueryStudioScopeSnapshot | None = None
        current_scope: QueryStudioScopeSnapshot | None = None
        binding_facts_fingerprint: str | None = None

        while True:
            facts_request = GovernedBindingFactsRequest(
                scope=request.scope,
                query=None,
                filters=allowlist,
                page_size=MAX_GOVERNED_SEARCH_PAGE_SIZE,
                after=raw_after,
                expected_scope=raw_expected_scope,
                logical_request_fingerprint=request.request_fingerprint,
            )
            page = _validated_model(
                self.facts.search(facts_request),
                GovernedFieldSearchPage,
                "governed binding-facts response",
            )
            if (
                page.request_fingerprint != request.request_fingerprint
                or page.binding_facts_fingerprint != facts_request.request_fingerprint
            ):
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Governed binding-facts response does not match its request",
                )
            if (
                binding_facts_fingerprint is not None
                and binding_facts_fingerprint != page.binding_facts_fingerprint
            ):
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Governed binding-facts pages do not share one request",
                )
            binding_facts_fingerprint = page.binding_facts_fingerprint
            _ensure_registry_still_matches(loaded, page.scope)
            if request.expected_scope is not None and request.expected_scope != page.scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Governed binding-facts continuation no longer matches current state",
                )
            if current_scope is not None and current_scope != page.scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Governed binding-facts pages came from different current states",
                )
            current_scope = page.scope
            allowed = {field.root for field in allowlist.logical_fields}
            for item in page.items:
                if allowlist.restrict_logical_fields and item.logical_field.root not in allowed:
                    raise QueryStudioError(
                        QueryStudioErrorCode.INVALID_CANDIDATE,
                        "Governed binding-facts response escaped its logical allowlist",
                    )
                _validate_binding_against_registry(item, loaded.registry)
                if item.binding_id in seen_bindings:
                    raise QueryStudioError(
                        QueryStudioErrorCode.INVALID_CONTEXT,
                        "Governed binding-facts keyset repeated a binding",
                    )
                seen_bindings.add(item.binding_id)
                raw_items.append(item)
                if len(raw_items) > 1_000:
                    raise QueryStudioError(
                        QueryStudioErrorCode.CLOSURE_OVERFLOW,
                        "Governed description search exceeds the 1,000-binding limit",
                    )
            if page.next_key is None:
                break
            if len(page.items) != MAX_GOVERNED_SEARCH_PAGE_SIZE:
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Governed binding-facts continuation returned a partial raw page",
                )
            if len(raw_items) == 1_000:
                raise QueryStudioError(
                    QueryStudioErrorCode.CLOSURE_OVERFLOW,
                    "Governed description search exceeds the 1,000-binding limit",
                )
            continuation = page.next_key.sort_tuple
            if continuation in seen_continuations:
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Governed binding-facts keyset did not progress",
                )
            seen_continuations.add(continuation)
            raw_after = page.next_key
            raw_expected_scope = page.scope

        if current_scope is None or binding_facts_fingerprint is None:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Governed binding-facts search returned no scope",
            )
        public_binding_facts_fingerprint = query_studio_fingerprint(
            {
                "binding_facts_fingerprint": binding_facts_fingerprint,
                "matcher_version": GOVERNED_DESCRIPTION_MATCHER_VERSION,
            }
        )
        _ensure_registry_still_matches(self.registry.load(), current_scope)
        scored = tuple(
            enriched
            for item in raw_items
            if (
                enriched := _score_binding_from_registry(
                    item=item,
                    query=query.root,
                    registry=loaded.registry,
                )
            )
            is not None
        )
        ordered = rank_governed_bindings(scored)
        if request.after is not None:
            expected_keys = tuple(
                item.search_key(
                    current_scope.fingerprint,
                    request.request_fingerprint,
                    public_binding_facts_fingerprint,
                )
                for item in ordered
            )
            if request.after not in expected_keys:
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Governed description-search continuation is not current",
                )
            ordered = tuple(
                item for item in ordered if _binding_sort_tuple(item) > request.after.sort_tuple
            )
        rows = ordered[: request.page_size + 1]
        items = rows[: request.page_size]
        next_key = (
            items[-1].search_key(
                current_scope.fingerprint,
                request.request_fingerprint,
                public_binding_facts_fingerprint,
            )
            if len(rows) == request.page_size + 1
            else None
        )
        return GovernedFieldSearchPage(
            scope=current_scope,
            request_fingerprint=request.request_fingerprint,
            binding_facts_fingerprint=public_binding_facts_fingerprint,
            items=items,
            page_size=request.page_size,
            rows_read=len(rows),
            next_key=next_key,
        )


@dataclass(frozen=True, slots=True)
class BrowseGuidedGovernedFields:
    """Page executable options while keeping their browser identity opaque and short-lived."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort
    candidate_ids: QueryStudioCandidateIdPort
    clock: QueryStudioClockPort
    nonces: QueryStudioNoncePort

    def execute(
        self,
        request: GovernedFieldSearchRequest,
        *,
        context: GuidedBrowseContext | None = None,
    ) -> GuidedGovernedFieldPage:
        now = self.clock.now()
        _require_aware_time(now)
        if context is None and request.after is not None:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "A guided continuation requires its original browse context",
            )
        if context is not None:
            if now > context.expires_at:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Guided browse context expired; start a new browse",
                )
            if request.scope != context.scope.scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Guided browse request crossed its registry scope",
                )
            if request.after is not None and request.expected_scope != context.scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Guided continuation no longer matches its current scope",
                )

        page = SearchGovernedFields(registry=self.registry, search=self.search).execute(request)
        if context is None:
            context = GuidedBrowseContext(
                scope=page.scope,
                nonce=self.nonces.new_nonce(),
                issued_at=now,
                expires_at=now + timedelta(seconds=MAX_PREVIEW_TTL_SECONDS),
            )
        elif context.scope != page.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided governed evidence changed; start a new browse",
            )

        loaded = self.registry.load()
        _ensure_registry_still_matches(loaded, page.scope)
        request_digest = query_studio_request_digest(None, None, None)
        options = tuple(
            _guided_browse_candidate(
                item=item,
                candidate_id=self.candidate_ids.issue(
                    scope=page.scope,
                    binding=item,
                    request_digest=request_digest,
                    nonce=context.nonce,
                ),
                registry=loaded.registry,
            )
            for item in page.items
        )
        return GuidedGovernedFieldPage(
            context=context,
            request_fingerprint=page.request_fingerprint,
            binding_facts_fingerprint=page.binding_facts_fingerprint,
            items=options,
            page_size=page.page_size,
            rows_read=page.rows_read,
            next_key=page.next_key,
        )


@dataclass(frozen=True, slots=True)
class RecomputeGuidedQueryStudioEvidence:
    """Resolve opaque selections by bounded keyset traversal of current governed evidence."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort
    candidate_ids: QueryStudioCandidateIdPort

    def resolve(
        self,
        proposal: QueryStudioModelProposal,
        *,
        nonce: str,
    ) -> GuidedQueryStudioEvidence:
        if proposal.semantic_state is not SemanticMatchState.ALIGNED:
            raise QueryStudioError(
                QueryStudioErrorCode.CONFIRMATION_REQUIRED,
                "Only an aligned guided selection can be recomputed",
            )
        selected_ids = tuple(item.root for item in proposal.referenced_candidate_ids)
        if not selected_ids:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided selection contains no governed candidate",
            )
        if len(selected_ids) > MAX_PROMPT_FIELDS:
            raise QueryStudioError(
                QueryStudioErrorCode.CLOSURE_OVERFLOW,
                "Guided selection exceeds the twelve-field closure",
            )

        loaded = self.registry.load()
        registry = loaded.registry
        if len(registry.logical_context.field_index()) > MAX_GUIDED_LOGICAL_FIELDS:
            raise QueryStudioError(
                QueryStudioErrorCode.CLOSURE_OVERFLOW,
                "Active guided registry exceeds the bounded logical-field universe",
            )
        request_digest = query_studio_request_digest(None, None, None)
        wanted = set(selected_ids)
        found: dict[str, GovernedFieldBinding] = {}
        seen_bindings: set[str] = set()
        seen_continuations: set[tuple[int, str, str]] = set()
        rows_seen = 0
        after: GovernedSearchKey | None = None
        expected_scope: QueryStudioScopeSnapshot | None = None
        current_scope: QueryStudioScopeSnapshot | None = None

        while True:
            page = SearchGovernedFields(registry=self.registry, search=self.search).execute(
                GovernedFieldSearchRequest(
                    scope=loaded.scope,
                    page_size=MAX_GOVERNED_SEARCH_PAGE_SIZE,
                    after=after,
                    expected_scope=expected_scope,
                )
            )
            if current_scope is not None and page.scope != current_scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Guided keyset pages came from different current states",
                )
            current_scope = page.scope
            for item in page.items:
                rows_seen += 1
                if rows_seen > MAX_GUIDED_BINDINGS:
                    raise QueryStudioError(
                        QueryStudioErrorCode.CLOSURE_OVERFLOW,
                        "Guided recomputation exceeded the bounded binding universe",
                    )
                if item.binding_id in seen_bindings:
                    raise QueryStudioError(
                        QueryStudioErrorCode.INVALID_CONTEXT,
                        "Guided keyset traversal repeated a governed binding",
                    )
                seen_bindings.add(item.binding_id)
                candidate_id = self.candidate_ids.issue(
                    scope=page.scope,
                    binding=item,
                    request_digest=request_digest,
                    nonce=nonce,
                )
                if candidate_id.root in wanted:
                    previous = found.setdefault(candidate_id.root, item)
                    if previous.binding_id != item.binding_id:
                        raise QueryStudioError(
                            QueryStudioErrorCode.INVALID_CANDIDATE,
                            "Opaque guided candidate identity is not unique",
                        )
            if wanted == set(found):
                break
            if page.next_key is None:
                break
            continuation = page.next_key.sort_tuple
            if continuation in seen_continuations:
                raise QueryStudioError(
                    QueryStudioErrorCode.INVALID_CONTEXT,
                    "Guided keyset continuation did not progress",
                )
            seen_continuations.add(continuation)
            after = page.next_key
            expected_scope = page.scope

        if current_scope is None or wanted != set(found):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CANDIDATE,
                "Guided selection contains an unknown or stale candidate",
            )
        _ensure_registry_still_matches(self.registry.load(), current_scope)

        ordered_bindings = rank_governed_bindings(tuple(found.values()))
        logical_counts: dict[str, int] = {}
        leaf_counts: dict[str, int] = {}
        for item in ordered_bindings:
            logical_counts[item.logical_field.root] = (
                logical_counts.get(item.logical_field.root, 0) + 1
            )
            leaf = item.physical_field.root.rsplit(".", 1)[-1].casefold()
            leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
        purposes = _guided_candidate_purposes(proposal)
        ranked = tuple(
            _enrich_candidate(
                item=item,
                candidate_id=_candidate_id_for_binding(found, item),
                rank=index,
                purposes=purposes[_candidate_id_for_binding(found, item).root],
                duplicate_logical=logical_counts[item.logical_field.root] > 1,
                homonymous=(
                    leaf_counts[item.physical_field.root.rsplit(".", 1)[-1].casefold()] > 1
                ),
                registry=registry,
            )
            for index, item in enumerate(ordered_bindings, start=1)
        )
        shortlist = GovernedShortlist(
            scope=current_scope,
            query_fingerprint=request_digest,
            expansion_fingerprint=query_studio_fingerprint(
                {
                    "mode": "guided",
                    "candidate_ids": tuple(sorted(selected_ids)),
                }
            ),
            candidates=ranked,
        )
        vocabulary = _build_vocabulary(
            ranked,
            registry,
            purpose_uses={purpose.value: purpose for purpose in QueryFieldPurpose},
            required_selection_counts=QueryStudioRequiredSelectionCounts(
                dimensions=len(proposal.dimensions),
                metrics=len(proposal.metrics),
                filters=len(proposal.filters),
            ),
        )
        _validate_proposal_vocabulary(proposal, vocabulary)
        proposal_to_guided_draft(proposal, vocabulary)
        return GuidedQueryStudioEvidence(shortlist=shortlist, vocabulary=vocabulary)

    def recompute(
        self,
        confirmation: QueryStudioConfirmation,
        *,
        nonce: str,
    ) -> GuidedQueryStudioEvidence:
        if (
            confirmation.original_text is not None
            or confirmation.language is not None
            or confirmation.expansion is not None
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided evidence cannot be recomputed from natural-language context",
            )
        return self.resolve(confirmation.proposal, nonce=nonce)


@dataclass(frozen=True, slots=True)
class DiscoverPhysicalFields:
    """Keep M25 physical discovery outside governed planning and confirmation."""

    discovery: PhysicalFieldDiscoveryPort

    def execute(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        page = _validated_model(
            self.discovery.search(request),
            PhysicalFieldDiscoveryPage,
            "physical field-discovery response",
        )
        if any(
            item.locator.asset.workspace_id != request.scope.workspace_id for item in page.items
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Physical discovery response crossed workspace scope",
            )
        return page


@dataclass(frozen=True, slots=True)
class InspectPhysicalDiscoveryCardinality:
    """Read aggregate current inventory facts without materializing discovery rows."""

    discovery: PhysicalFieldDiscoveryPort

    def execute(self, scope: SemanticRegistryScope) -> PhysicalDiscoveryCardinality:
        snapshot = _validated_model(
            self.discovery.inspect_cardinality(scope),
            PhysicalDiscoveryCardinality,
            "physical discovery cardinality",
        )
        if snapshot.scope != scope:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Physical discovery cardinality crossed semantic scope",
            )
        return snapshot


@dataclass(frozen=True, slots=True)
class PrepareGuidedQueryStudioPreview:
    """Sign an already recomputed guided selection without validating its request."""

    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    nonces: QueryStudioNoncePort
    configuration: ProviderConfigurationFacts

    def execute(
        self,
        evidence: GuidedQueryStudioEvidence,
        proposal: QueryStudioModelProposal,
    ) -> QueryStudioPreview:
        now = self.clock.now()
        _require_aware_time(now)
        nonce = self.nonces.new_nonce()
        return _signed_guided_preview(
            evidence=evidence,
            proposal=proposal,
            nonce=nonce,
            now=now,
            preview_tokens=self.preview_tokens,
            configuration=self.configuration,
        )


@dataclass(frozen=True, slots=True)
class PrepareGuidedSelectionQueryStudioPreview:
    """Recompute an opaque guided selection before issuing its confirmable preview."""

    resolver: GuidedQueryStudioSelectionPort
    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    configuration: ProviderConfigurationFacts

    def execute(
        self,
        proposal: QueryStudioModelProposal,
        *,
        context: GuidedBrowseContext,
    ) -> QueryStudioPreview:
        now = self.clock.now()
        _require_aware_time(now)
        if now > context.expires_at:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided browse context expired; start a new selection",
            )
        try:
            evidence = self.resolver.resolve(proposal, nonce=context.nonce)
        except QueryStudioError as error:
            if error.code is not QueryStudioErrorCode.CLOSURE_OVERFLOW:
                raise
            return QueryStudioPreview(
                mode="guided",
                operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
                reason_code="guided_closure_overflow",
            )
        if evidence.shortlist.scope != context.scope:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided selection scope changed; start a new selection",
            )
        return _signed_guided_preview(
            evidence=evidence,
            proposal=proposal,
            nonce=context.nonce,
            now=now,
            preview_tokens=self.preview_tokens,
            configuration=self.configuration,
        )


@dataclass(frozen=True, slots=True)
class ConfirmGuidedQueryStudioPreview:
    """Recompute guided evidence and invoke the builder only after token verification."""

    registry: GovernedSemanticRegistryPort
    recompute: GuidedQueryStudioRecomputePort
    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    configuration: ProviderConfigurationFacts
    guided_builder: BuildGuidedRequest

    def execute(self, confirmation: QueryStudioConfirmation) -> ConfirmedQueryStudioRequest:
        if (
            confirmation.original_text is not None
            or confirmation.language is not None
            or confirmation.expansion is not None
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided confirmation cannot carry natural-language context",
            )
        now = self.clock.now()
        _require_aware_time(now)
        try:
            payload = self.preview_tokens.verify(confirmation.token, at=now)
        except QueryStudioPortError as error:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided Query Studio preview is unavailable",
            ) from error
        if (
            now > payload.expires_at
            or payload.request_digest != query_studio_request_digest(None, None, None)
            or payload.proposal_fingerprint != confirmation.proposal.fingerprint
            or payload.configuration_fingerprint != self.configuration.fingerprint
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided Query Studio preview changed or expired",
            )
        try:
            evidence = self.recompute.recompute(confirmation, nonce=payload.nonce)
        except (
            QueryStudioError,
            QueryStudioPortError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed guided evidence could not be reproduced",
            ) from error
        if (
            evidence.shortlist.fingerprint != payload.shortlist_fingerprint
            or evidence.shortlist.scope.scope_digest != payload.scope_digest
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed guided evidence changed; prepare a new preview",
            )
        draft = proposal_to_guided_draft(confirmation.proposal, evidence.vocabulary)
        guided_preview = GuidedRequestPreview(
            mode="guided",
            scope=evidence.shortlist.scope,
            shortlist_fingerprint=evidence.shortlist.fingerprint,
            proposal_fingerprint=confirmation.proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            draft=draft,
        )
        if guided_preview.fingerprint != payload.preview_fingerprint:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Guided typed interpretation changed; prepare a new preview",
            )
        validated = self.guided_builder.execute(_guided_input(draft))
        _ensure_registry_still_matches(self.registry.load(), evidence.shortlist.scope)
        return ConfirmedQueryStudioRequest(
            original_text=None,
            language=None,
            validated_request=validated,
            preview_fingerprint=guided_preview.fingerprint,
            confirmation_fingerprint=confirmation_fingerprint(confirmation, payload),
        )


@dataclass(frozen=True, slots=True)
class PrepareNaturalLanguageQueryStudioPreview:
    """Prepare one bounded preview; no validated request is produced here."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort
    expansion: DescriptionExpansionPort
    interpreter: QueryStudioIntentPort
    candidate_ids: QueryStudioCandidateIdPort
    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    nonces: QueryStudioNoncePort
    configuration: ProviderConfigurationFacts

    def execute(self, text: str, language: UserLanguage | str) -> QueryStudioPreview:
        try:
            parsed_language = (
                language if isinstance(language, UserLanguage) else UserLanguage(language)
            )
            query = DescriptionQuery(text)
        except (TypeError, ValueError, ValidationError) as error:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Query Studio business text or language is invalid",
            ) from error

        try:
            expansion_result = _validated_model(
                self.expansion.expand(
                    DescriptionExpansionInput(
                        text=query,
                        language=parsed_language,
                        lane=DescriptionExpansionRoute.ANALYTICAL,
                    )
                ),
                DescriptionExpansionResult,
                "description-expansion response",
            )
            expansion_usage = (
                ()
                if expansion_result.usage is None
                else (
                    _validate_usage(
                        expansion_result.usage,
                        self.configuration,
                    ),
                )
            )
        except QueryStudioPortError as error:
            return _provider_failure_preview(error)
        except (QueryStudioError, TypeError, ValueError, ValidationError):
            return _operational_preview(
                QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value,
                output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
            )

        now = self.clock.now()
        _require_aware_time(now)
        nonce = self.nonces.new_nonce()
        request_digest = query_studio_request_digest(
            query,
            parsed_language,
            expansion_result.expansion,
        )
        try:
            closure = _build_closure(
                registry_port=self.registry,
                search_port=self.search,
                candidate_ids=self.candidate_ids,
                original_text=query,
                expansion=expansion_result.expansion,
                request_digest=request_digest,
                nonce=nonce,
            )
        except QueryStudioPortError as error:
            if error.code is not QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT:
                raise
            return _provider_failure_preview(
                error,
                expansion=expansion_result.expansion,
                usage=expansion_usage,
            )
        if closure.operational_state is not None:
            return QueryStudioPreview(
                mode="natural_language",
                operational_state=closure.operational_state,
                shortlist=closure.shortlist,
                expansion=expansion_result.expansion,
                provider_usage=expansion_usage,
                reason_code=closure.reason_code,
            )
        if closure.semantic_state is not None:
            return QueryStudioPreview(
                mode="natural_language",
                semantic_state=closure.semantic_state,
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                provider_usage=expansion_usage,
            )
        if closure.vocabulary is None:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Query Studio closure did not produce a bounded vocabulary",
            )

        try:
            interpretation_result = _validated_model(
                self.interpreter.interpret(
                    QueryStudioInterpretationInput(
                        text=query,
                        language=parsed_language,
                        vocabulary=closure.vocabulary,
                        expansion=expansion_result.expansion,
                        public_metadata_surface=ApprovedPublicMetadataSurface.create(
                            policy_fingerprint=(
                                self.configuration.public_metadata_policy_fingerprint
                            ),
                            semantic_scope_fingerprint=(closure.shortlist.scope.scope_digest),
                            registry_fingerprint=(closure.shortlist.scope.registry_fingerprint),
                            vocabulary_fingerprint=closure.vocabulary.fingerprint,
                        ),
                    )
                ),
                QueryStudioInterpretationResult,
                "Query Studio interpretation response",
            )
            _validate_usage(interpretation_result.usage, self.configuration)
        except QueryStudioPortError as error:
            return _provider_failure_preview(
                error,
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                usage=expansion_usage,
            )
        except (QueryStudioError, TypeError, ValueError, ValidationError):
            return _operational_preview(
                QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value,
                output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                usage=expansion_usage,
            )

        proposal = interpretation_result.proposal
        usage = (*expansion_usage, interpretation_result.usage)
        try:
            _validate_proposal_vocabulary(proposal, closure.vocabulary)
        except QueryStudioError:
            return _operational_preview(
                QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value,
                output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                usage=usage,
            )
        if proposal.semantic_state is not SemanticMatchState.ALIGNED:
            return QueryStudioPreview(
                mode="natural_language",
                semantic_state=proposal.semantic_state,
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                proposal=proposal,
                provider_usage=usage,
            )

        try:
            draft = proposal_to_guided_draft(proposal, closure.vocabulary)
        except QueryStudioError:
            return _operational_preview(
                QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT.value,
                output_failure_category=(ProviderOutputFailureCategory.SEMANTIC_CONTRACT),
                shortlist=closure.shortlist,
                vocabulary=closure.vocabulary,
                expansion=expansion_result.expansion,
                usage=usage,
            )
        guided_preview = GuidedRequestPreview(
            mode="natural_language",
            scope=closure.shortlist.scope,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            draft=draft,
        )
        expires_at = now + timedelta(seconds=MAX_PREVIEW_TTL_SECONDS)
        payload = PreviewTokenPayload(
            request_digest=request_digest,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            scope_digest=closure.shortlist.scope.scope_digest,
            preview_fingerprint=guided_preview.fingerprint,
            issued_at=now,
            expires_at=expires_at,
            nonce=nonce,
        )
        token = self.preview_tokens.issue(payload)
        return QueryStudioPreview(
            mode="natural_language",
            semantic_state=SemanticMatchState.ALIGNED,
            shortlist=closure.shortlist,
            vocabulary=closure.vocabulary,
            expansion=expansion_result.expansion,
            proposal=proposal,
            guided_preview=guided_preview,
            token=token,
            provider_usage=usage,
        )


@dataclass(frozen=True, slots=True)
class ConfirmQueryStudioPreview:
    """Recompute exact retrieval and only then invoke the existing guided validator."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort
    candidate_ids: QueryStudioCandidateIdPort
    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    configuration: ProviderConfigurationFacts
    guided_builder: BuildGuidedRequest

    def execute(self, confirmation: QueryStudioConfirmation) -> ConfirmedQueryStudioRequest:
        now = self.clock.now()
        _require_aware_time(now)
        try:
            payload = self.preview_tokens.verify(confirmation.token, at=now)
        except QueryStudioPortError as error:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio preview is unavailable; prepare a new preview",
            ) from error
        if now > payload.expires_at:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio preview expired; prepare a new preview",
            )
        request_digest = query_studio_request_digest(
            confirmation.original_text,
            confirmation.language,
            confirmation.expansion,
        )
        if (
            payload.request_digest != request_digest
            or payload.proposal_fingerprint != confirmation.proposal.fingerprint
            or payload.configuration_fingerprint != self.configuration.fingerprint
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio confirmation no longer matches the signed preview",
            )
        if confirmation.expansion is None or confirmation.original_text is None:
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Natural-language confirmation requires its typed search purposes",
            )

        try:
            closure = _build_closure(
                registry_port=self.registry,
                search_port=self.search,
                candidate_ids=self.candidate_ids,
                original_text=confirmation.original_text,
                expansion=confirmation.expansion,
                request_digest=request_digest,
                nonce=payload.nonce,
            )
        except QueryStudioPortError as error:
            if error.code is not QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT:
                raise
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio expansion is invalid; prepare a new preview",
            ) from error
        if (
            closure.operational_state is not None
            or closure.semantic_state is not None
            or closure.vocabulary is None
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed Query Studio context changed; prepare a new preview",
            )
        if (
            closure.shortlist.fingerprint != payload.shortlist_fingerprint
            or closure.shortlist.scope.scope_digest != payload.scope_digest
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed candidates changed; prepare a new preview",
            )

        draft = proposal_to_guided_draft(confirmation.proposal, closure.vocabulary)
        guided_preview = GuidedRequestPreview(
            mode="natural_language",
            scope=closure.shortlist.scope,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=confirmation.proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            draft=draft,
        )
        if guided_preview.fingerprint != payload.preview_fingerprint:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Typed interpretation changed; prepare a new preview",
            )

        validated = self.guided_builder.execute(_guided_input(draft))
        _ensure_registry_still_matches(self.registry.load(), closure.shortlist.scope)
        return ConfirmedQueryStudioRequest(
            original_text=confirmation.original_text,
            language=confirmation.language,
            validated_request=validated,
            preview_fingerprint=guided_preview.fingerprint,
            confirmation_fingerprint=confirmation_fingerprint(confirmation, payload),
        )


@dataclass(frozen=True, slots=True)
class RecomputeNaturalLanguageQueryStudioPreview:
    """Revalidate and re-sign a typed edit without invoking either provider stage."""

    registry: GovernedSemanticRegistryPort
    search: GovernedFieldSearchPort
    candidate_ids: QueryStudioCandidateIdPort
    preview_tokens: QueryStudioPreviewTokenPort
    clock: QueryStudioClockPort
    configuration: ProviderConfigurationFacts

    def execute(self, revision: QueryStudioInterpretationRevision) -> QueryStudioPreview:
        now = self.clock.now()
        _require_aware_time(now)
        try:
            payload = self.preview_tokens.verify(revision.token, at=now)
        except QueryStudioPortError as error:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio preview is unavailable; prepare a new preview",
            ) from error
        if now > payload.expires_at:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio preview expired; prepare a new preview",
            )

        request_digest = query_studio_request_digest(
            revision.original_text,
            revision.language,
            revision.expansion,
        )
        if (
            payload.request_digest != request_digest
            or payload.proposal_fingerprint != revision.signed_proposal.fingerprint
            or payload.configuration_fingerprint != self.configuration.fingerprint
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio edit no longer matches the signed preview",
            )

        try:
            closure = _build_closure(
                registry_port=self.registry,
                search_port=self.search,
                candidate_ids=self.candidate_ids,
                original_text=revision.original_text,
                expansion=revision.expansion,
                request_digest=request_digest,
                nonce=payload.nonce,
            )
        except QueryStudioPortError as error:
            if error.code is not QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT:
                raise
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Query Studio expansion is invalid; prepare a new preview",
            ) from error
        if (
            closure.operational_state is not None
            or closure.semantic_state is not None
            or closure.vocabulary is None
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed Query Studio context changed; prepare a new preview",
            )
        if (
            closure.shortlist.fingerprint != payload.shortlist_fingerprint
            or closure.shortlist.scope.scope_digest != payload.scope_digest
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Governed candidates changed; prepare a new preview",
            )

        signed_draft = proposal_to_guided_draft(
            revision.signed_proposal,
            closure.vocabulary,
        )
        signed_preview = GuidedRequestPreview(
            mode="natural_language",
            scope=closure.shortlist.scope,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=revision.signed_proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            draft=signed_draft,
        )
        if signed_preview.fingerprint != payload.preview_fingerprint:
            raise QueryStudioError(
                QueryStudioErrorCode.STALE_PREVIEW,
                "Signed interpretation cannot be reproduced from current evidence",
            )

        revised_draft = proposal_to_guided_draft(
            revision.revised_proposal,
            closure.vocabulary,
        )
        revised_preview = GuidedRequestPreview(
            mode="natural_language",
            scope=closure.shortlist.scope,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=revision.revised_proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            draft=revised_draft,
        )
        revised_payload = PreviewTokenPayload(
            request_digest=request_digest,
            shortlist_fingerprint=closure.shortlist.fingerprint,
            proposal_fingerprint=revision.revised_proposal.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            scope_digest=closure.shortlist.scope.scope_digest,
            preview_fingerprint=revised_preview.fingerprint,
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
            nonce=payload.nonce,
        )
        _ensure_registry_still_matches(self.registry.load(), closure.shortlist.scope)
        return QueryStudioPreview(
            mode="natural_language",
            semantic_state=SemanticMatchState.ALIGNED,
            shortlist=closure.shortlist,
            vocabulary=closure.vocabulary,
            expansion=revision.expansion,
            proposal=revision.revised_proposal,
            guided_preview=revised_preview,
            token=self.preview_tokens.issue(revised_payload),
        )


def rank_governed_bindings(
    values: tuple[GovernedFieldBinding, ...],
) -> tuple[GovernedFieldBinding, ...]:
    """Stable integer ordering shared by closure and adapter contract tests."""

    return tuple(
        sorted(
            values,
            key=lambda item: (
                -item.signals.total,
                item.logical_field.root,
                item.binding_id,
            ),
        )
    )


def natural_language_edit_candidates(
    vocabulary: QueryStudioPromptVocabulary,
) -> tuple[QueryStudioEditableCandidate, ...]:
    """Derive every natural-edit control only from the signed bounded vocabulary."""

    return tuple(
        QueryStudioEditableCandidate(
            candidate_id=candidate.candidate_id,
            logical_field=candidate.logical_field,
            canonical_type=candidate.canonical_type,
            role=candidate.role,
            allowed_values=candidate.allowed_values,
            metric_operations=tuple(
                operation
                for operation in vocabulary.metric_operations
                if operation
                in _compatible_metric_operations(candidate.canonical_type, candidate.role)
            ),
            filter_operators=tuple(
                operator
                for operator in vocabulary.filter_operators
                if operator in _compatible_filter_operators(candidate.canonical_type)
            ),
            date_grains=(
                vocabulary.date_grains
                if candidate.role is LogicalFieldRole.TEMPORAL
                and candidate.canonical_type in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
                else ()
            ),
            sort_directions=vocabulary.sort_directions,
        )
        for candidate in vocabulary.candidates
    )


def _validate_proposal_vocabulary(
    proposal: QueryStudioModelProposal,
    vocabulary: QueryStudioPromptVocabulary,
) -> None:
    allowed_candidates = {item.candidate_id.root for item in vocabulary.candidates}
    if any(item.root not in allowed_candidates for item in proposal.referenced_candidate_ids):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CANDIDATE,
            "Model proposal references a candidate outside the exact supplied vocabulary",
        )
    if proposal.semantic_state is SemanticMatchState.ALIGNED:
        required = vocabulary.required_selection_counts
        if (
            len(proposal.dimensions),
            len(proposal.metrics),
            len(proposal.filters),
        ) != (required.dimensions, required.metrics, required.filters):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Model proposal does not cover the exact required selections",
            )
    if any(item.operation not in vocabulary.metric_operations for item in proposal.metrics) or any(
        item.operator not in vocabulary.filter_operators for item in proposal.filters
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_PROPOSAL,
            "Model proposal uses an operation outside the exact supplied vocabulary",
        )
    if any(
        item.grain is not None and item.grain not in vocabulary.date_grains
        for item in proposal.dimensions
    ) or any(item.direction not in vocabulary.sort_directions for item in proposal.order_by):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_PROPOSAL,
            "Model proposal uses a grain or ordering outside the supplied vocabulary",
        )


def proposal_to_guided_draft(
    proposal: QueryStudioModelProposal,
    vocabulary: QueryStudioPromptVocabulary,
) -> GuidedRequestDraft:
    """Resolve only supplied opaque IDs; this deliberately does not validate context."""

    if proposal.semantic_state is not SemanticMatchState.ALIGNED:
        raise QueryStudioError(
            QueryStudioErrorCode.CONFIRMATION_REQUIRED,
            "Only an aligned proposal can become a guided draft",
        )
    candidate_index = {item.candidate_id.root: item for item in vocabulary.candidates}
    _validate_proposal_vocabulary(proposal, vocabulary)
    if proposal.primary_candidate_id is None:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_PROPOSAL,
            "Aligned proposal requires a primary candidate",
        )

    primary_field = candidate_index[proposal.primary_candidate_id.root].logical_field
    primary_model = LogicalModelRef(primary_field.root.split(".", 1)[0])
    _validate_proposal_candidate_compatibility(proposal, candidate_index)
    for proposed_filter in proposal.filters:
        candidate = candidate_index[proposed_filter.candidate_id.root]
        values = (
            proposed_filter.value
            if isinstance(proposed_filter.value, tuple)
            else (proposed_filter.value,)
        )
        if any(isinstance(value, str) and _SQL_LIKE_VALUE.search(value) for value in values):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Model proposal contains SQL-like text in a filter value",
            )
        if (
            proposed_filter.operator not in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}
            and candidate.allowed_values
            and any(value not in candidate.allowed_values for value in values)
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Model proposal contains a value outside the supplied governed set",
            )

    selected = tuple(dict.fromkeys(proposal.referenced_candidate_ids))
    return GuidedRequestDraft(
        primary_entity=primary_model,
        dimensions=tuple(
            GuidedDraftDimension(
                field=candidate_index[item.candidate_id.root].logical_field,
                grain=item.grain,
            )
            for item in proposal.dimensions
        ),
        metrics=tuple(
            GuidedDraftMetric(
                field=candidate_index[item.candidate_id.root].logical_field,
                operation=item.operation,
                alias=item.alias,
            )
            for item in proposal.metrics
        ),
        filters=tuple(
            GuidedDraftFilter(
                field=candidate_index[item.candidate_id.root].logical_field,
                operator=item.operator,
                value=item.value,
            )
            for item in proposal.filters
        ),
        order_by=tuple(
            GuidedDraftOrder(
                field=candidate_index[item.candidate_id.root].logical_field,
                direction=item.direction,
            )
            for item in proposal.order_by
        ),
        limit=proposal.limit,
        selected_candidate_ids=selected,
    )


def _signed_guided_preview(
    *,
    evidence: GuidedQueryStudioEvidence,
    proposal: QueryStudioModelProposal,
    nonce: str,
    now: datetime,
    preview_tokens: QueryStudioPreviewTokenPort,
    configuration: ProviderConfigurationFacts,
) -> QueryStudioPreview:
    draft = proposal_to_guided_draft(proposal, evidence.vocabulary)
    request_digest = query_studio_request_digest(None, None, None)
    guided_preview = GuidedRequestPreview(
        mode="guided",
        scope=evidence.shortlist.scope,
        shortlist_fingerprint=evidence.shortlist.fingerprint,
        proposal_fingerprint=proposal.fingerprint,
        configuration_fingerprint=configuration.fingerprint,
        draft=draft,
    )
    payload = PreviewTokenPayload(
        request_digest=request_digest,
        shortlist_fingerprint=evidence.shortlist.fingerprint,
        proposal_fingerprint=proposal.fingerprint,
        configuration_fingerprint=configuration.fingerprint,
        scope_digest=evidence.shortlist.scope.scope_digest,
        preview_fingerprint=guided_preview.fingerprint,
        issued_at=now,
        expires_at=now + timedelta(seconds=MAX_PREVIEW_TTL_SECONDS),
        nonce=nonce,
    )
    return QueryStudioPreview(
        mode="guided",
        semantic_state=SemanticMatchState.ALIGNED,
        shortlist=evidence.shortlist,
        vocabulary=evidence.vocabulary,
        proposal=proposal,
        guided_preview=guided_preview,
        token=preview_tokens.issue(payload),
    )


def _guided_browse_candidate(
    *,
    item: GovernedFieldBinding,
    candidate_id: OpaqueCandidateId,
    registry: GovernedSemanticRegistrySnapshot,
) -> GuidedGovernedCandidate:
    context = registry.logical_context
    field = context.field_index().get(item.logical_field.root)
    model = context.model_index().get(item.logical_field.root.split(".", 1)[0])
    if (
        field is None
        or model is None
        or len(model.description) > 300
        or len(field.definition) > 300
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Guided option exceeds the bounded logical metadata contract",
        )
    allowed_values = _governed_allowed_values(item, registry)
    if len(allowed_values) > 12:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Guided option exceeds the bounded allowed-value contract",
        )
    risks = _candidate_risks(
        item,
        role=field.role,
        duplicate_logical=False,
        homonymous=False,
    )
    return GuidedGovernedCandidate(
        candidate_id=candidate_id,
        binding=item,
        logical_model=model.id,
        model_definition=model.description,
        field_definition=field.definition,
        canonical_type=field.canonical_type,
        role=field.role,
        allowed_values=allowed_values,
        metric_operations=_compatible_metric_operations(field.canonical_type, field.role),
        filter_operators=_compatible_filter_operators(field.canonical_type),
        date_grains=(
            tuple(DateGrain)
            if field.role is LogicalFieldRole.TEMPORAL
            and field.canonical_type in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
            else ()
        ),
        sort_directions=tuple(SortDirection),
        risks=risks,
    )


def _guided_candidate_purposes(
    proposal: QueryStudioModelProposal,
) -> dict[str, tuple[str, ...]]:
    values: dict[str, set[str]] = {
        candidate.root: set() for candidate in proposal.referenced_candidate_ids
    }
    if proposal.primary_candidate_id is not None:
        values[proposal.primary_candidate_id.root].add("primary_entity")
    for dimension in proposal.dimensions:
        values[dimension.candidate_id.root].add("dimension")
    for metric in proposal.metrics:
        values[metric.candidate_id.root].add("metric")
    for request_filter in proposal.filters:
        values[request_filter.candidate_id.root].add("filter")
    for ordering in proposal.order_by:
        values[ordering.candidate_id.root].add("order")
    return {candidate: tuple(sorted(purposes)) for candidate, purposes in values.items()}


def _candidate_id_for_binding(
    found: dict[str, GovernedFieldBinding],
    binding: GovernedFieldBinding,
) -> OpaqueCandidateId:
    matches = tuple(
        candidate_id
        for candidate_id, value in found.items()
        if value.binding_id == binding.binding_id
    )
    if len(matches) != 1:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CANDIDATE,
            "Guided binding does not have one exact opaque identity",
        )
    return OpaqueCandidateId(matches[0])


def _compatible_metric_operations(
    canonical_type: CanonicalType,
    role: LogicalFieldRole,
) -> tuple[MetricOperation, ...]:
    numeric = canonical_type in {CanonicalType.INTEGER, CanonicalType.DECIMAL}
    ordered = numeric or canonical_type in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
    return tuple(
        operation
        for operation in MetricOperation
        if operation in {MetricOperation.COUNT, MetricOperation.COUNT_DISTINCT}
        or (
            operation in {MetricOperation.SUM, MetricOperation.AVG}
            and role is LogicalFieldRole.MEASURE
            and numeric
        )
        or (
            operation in {MetricOperation.MIN, MetricOperation.MAX}
            and ordered
            and role in {LogicalFieldRole.MEASURE, LogicalFieldRole.TEMPORAL}
        )
    )


def _compatible_filter_operators(
    canonical_type: CanonicalType,
) -> tuple[FilterOperator, ...]:
    ordered = canonical_type in {
        CanonicalType.INTEGER,
        CanonicalType.DECIMAL,
        CanonicalType.DATE,
        CanonicalType.TIMESTAMP,
    }
    comparison = {
        FilterOperator.GREATER_THAN,
        FilterOperator.GREATER_THAN_OR_EQUAL,
        FilterOperator.LESS_THAN,
        FilterOperator.LESS_THAN_OR_EQUAL,
    }
    return tuple(operator for operator in FilterOperator if ordered or operator not in comparison)


def _validate_proposal_candidate_compatibility(
    proposal: QueryStudioModelProposal,
    candidate_index: dict[str, QueryStudioPromptCandidate],
) -> None:
    for dimension in proposal.dimensions:
        candidate = candidate_index[dimension.candidate_id.root]
        if dimension.grain is not None and (
            candidate.role is not LogicalFieldRole.TEMPORAL
            or candidate.canonical_type not in {CanonicalType.DATE, CanonicalType.TIMESTAMP}
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided date grain is incompatible with the selected field",
            )
    for metric in proposal.metrics:
        candidate = candidate_index[metric.candidate_id.root]
        if metric.operation not in _compatible_metric_operations(
            candidate.canonical_type,
            candidate.role,
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided metric operation is incompatible with the selected field",
            )
    for request_filter in proposal.filters:
        candidate = candidate_index[request_filter.candidate_id.root]
        if request_filter.operator not in _compatible_filter_operators(candidate.canonical_type):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided filter operator is incompatible with the selected field",
            )
        values = (
            request_filter.value
            if isinstance(request_filter.value, tuple)
            else (request_filter.value,)
        )
        if request_filter.operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            valid_shape = request_filter.value is None
        elif request_filter.operator is FilterOperator.IN:
            valid_shape = (
                isinstance(request_filter.value, tuple)
                and bool(request_filter.value)
                and all(value is not None for value in request_filter.value)
            )
        else:
            valid_shape = request_filter.value is not None and not isinstance(
                request_filter.value,
                tuple,
            )
        if not valid_shape or not _filter_values_match_canonical_type(
            values,
            candidate.canonical_type,
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_PROPOSAL,
                "Guided filter value is incompatible with the selected field",
            )


def _filter_values_match_canonical_type(
    values: tuple[object, ...],
    canonical_type: CanonicalType,
) -> bool:
    if all(value is None for value in values):
        return True
    for value in values:
        if value is None:
            return False
        if canonical_type is CanonicalType.STRING and not isinstance(value, str):
            return False
        if canonical_type is CanonicalType.INTEGER and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            return False
        if canonical_type is CanonicalType.DECIMAL and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            return False
        if canonical_type is CanonicalType.BOOLEAN and not isinstance(value, bool):
            return False
        if canonical_type is CanonicalType.DATE and type(value) is not date:
            return False
        if canonical_type is CanonicalType.TIMESTAMP and not isinstance(value, datetime):
            return False
    return True


def _build_closure(
    *,
    registry_port: GovernedSemanticRegistryPort,
    search_port: GovernedFieldSearchPort,
    candidate_ids: QueryStudioCandidateIdPort,
    original_text: DescriptionQuery,
    expansion: DescriptionExpansion,
    request_digest: str,
    nonce: str,
) -> _Closure:
    _validate_expansion_source_spans(original_text, expansion)
    loaded = registry_port.load()
    registry = loaded.registry
    governed_logical_fields = frozenset(
        logical_field.casefold() for logical_field in registry.logical_context.field_index()
    )
    missing_qualified_references = tuple(
        reference
        for reference in find_explicit_logical_field_references(original_text.root)
        if reference.value not in governed_logical_fields
    )
    malformed_qualified_paths = find_malformed_qualified_paths(original_text.root)
    owner_models_by_purpose = {
        probe.purpose_id: _eligible_owner_models(probe, registry) for probe in expansion.probes
    }
    if missing_qualified_references or malformed_qualified_paths:
        # A qualified logical identifier is an exact identity assertion, not a
        # fuzzy description. An absent leaf or a dotted physical-looking path
        # cannot fall back to an approximate field, global population,
        # physical inventory, joins, or the LLM.
        guard_candidates = tuple(
            (
                reference.start,
                reference.value,
                reference.owner,
                reference.end,
            )
            for reference in missing_qualified_references
        ) + tuple(
            (
                path.start,
                path.value,
                (
                    path.owner
                    if re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", path.owner) is not None
                    else "malformed_path"
                ),
                path.end,
            )
            for path in malformed_qualified_paths
        )
        start, query, owner, end = min(guard_candidates, key=lambda item: item[0])
        source_span = original_text.root[start:end]
        probe = DescriptionSearchProbe(
            purpose_id="qualified_reference_guard",
            query=DescriptionQuery(query),
            source_span=(
                source_span
                if len(source_span) <= 256 and len(source_span.encode("utf-8")) <= 512
                else None
            ),
            intended_use=QueryFieldPurpose.DIMENSION,
            semantic_focus=(owner,),
            owner_focus=(owner,),
        )
        owner_model_ids = _eligible_owner_models(probe, registry)
        request = governed_probe_search_requests(
            loaded.scope,
            probe,
            owner_models=(
                tuple(LogicalModelRef(model_id) for model_id in owner_model_ids)
                if probe.owner_focus
                else None
            ),
        )[0]
        page = search_port.search(request)
        _ensure_registry_still_matches(loaded, page.scope)
        return _Closure(
            shortlist=GovernedShortlist(
                scope=page.scope,
                query_fingerprint=request_digest,
                expansion_fingerprint=expansion.fingerprint,
            ),
            vocabulary=None,
            semantic_state=SemanticMatchState.NO_MATCH,
        )
    pages: list[tuple[str, tuple[GovernedFieldBinding, ...]]] = []
    current_scope: QueryStudioScopeSnapshot | None = None
    owner_ambiguous = any(
        probe.owner_focus and len(owner_models_by_purpose[probe.purpose_id]) > 1
        for probe in expansion.probes
    )
    for probe in expansion.probes:
        owner_model_ids = owner_models_by_purpose[probe.purpose_id]
        requests = governed_probe_search_requests(
            loaded.scope,
            probe,
            owner_models=(
                tuple(LogicalModelRef(model_id) for model_id in owner_model_ids)
                if probe.owner_focus
                else None
            ),
        )
        best_by_binding: dict[str, GovernedFieldBinding] = {}
        for request in requests:
            page = search_port.search(request)
            _ensure_registry_still_matches(loaded, page.scope)
            if current_scope is not None and current_scope != page.scope:
                raise QueryStudioError(
                    QueryStudioErrorCode.STALE_PREVIEW,
                    "Governed search pages came from different current states",
                )
            current_scope = page.scope
            for item in page.items:
                _validate_binding_against_registry(item, registry)
                if not _binding_is_eligible_for_probe(
                    item,
                    probe=probe,
                    owner_models=owner_models_by_purpose[probe.purpose_id],
                    registry=registry,
                ):
                    continue
                previous = best_by_binding.get(item.binding_id)
                if previous is None or _binding_sort_tuple(item) < _binding_sort_tuple(previous):
                    best_by_binding[item.binding_id] = item
        pages.append(
            (
                probe.purpose_id,
                rank_governed_bindings(tuple(best_by_binding.values()))[
                    : MAX_EXECUTABLE_SHORTLIST + 1
                ],
            )
        )

    if current_scope is None:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Query Studio expansion contains no governed retrieval purpose",
        )
    all_pairs = tuple((purpose, item) for purpose, items in pages for item in items)
    conflict = _has_physical_meaning_conflict(tuple(item for _, item in all_pairs))
    per_purpose: dict[str, tuple[GovernedFieldBinding, ...]] = {}
    purpose_boundary_ties: dict[str, tuple[GovernedFieldBinding, ...]] = {}
    global_shortlist_tie_overflow = False
    retrieval_ambiguous = False
    for purpose, items in pages:
        logical_best: dict[str, GovernedFieldBinding] = {}
        for item in rank_governed_bindings(items):
            logical_best.setdefault(item.logical_field.root, item)
        ranked = rank_governed_bindings(tuple(logical_best.values()))
        per_purpose[purpose] = ranked
        if len(ranked) > MAX_CANDIDATES_PER_PURPOSE and (
            ranked[MAX_CANDIDATES_PER_PURPOSE - 1].signals.total
            == ranked[MAX_CANDIDATES_PER_PURPOSE].signals.total
        ):
            boundary_score = ranked[MAX_CANDIDATES_PER_PURPOSE - 1].signals.total
            purpose_boundary_ties[purpose] = tuple(
                item for item in ranked if item.signals.total == boundary_score
            )
        if len(ranked) > 1 and ranked[0].signals.total == ranked[1].signals.total:
            retrieval_ambiguous = True

    purpose_membership: dict[str, set[str]] = {}
    best_rows: dict[str, GovernedFieldBinding] = {}
    for purpose, item in all_pairs:
        purpose_membership.setdefault(item.binding_id, set()).add(purpose)
        previous = best_rows.get(item.binding_id)
        if previous is None or _binding_sort_tuple(item) < _binding_sort_tuple(previous):
            best_rows[item.binding_id] = item
    ranked_all = rank_governed_bindings(tuple(best_rows.values()))
    if len(ranked_all) > MAX_EXECUTABLE_SHORTLIST and (
        ranked_all[MAX_EXECUTABLE_SHORTLIST - 1].signals.total
        == ranked_all[MAX_EXECUTABLE_SHORTLIST].signals.total
    ):
        global_shortlist_tie_overflow = True
    ranked_all = ranked_all[:MAX_EXECUTABLE_SHORTLIST]

    duplicate_logical = {
        logical
        for logical in {item.logical_field.root for item in ranked_all}
        if sum(item.logical_field.root == logical for item in ranked_all) > 1
    }
    leaf_counts: dict[str, int] = {}
    for item in ranked_all:
        leaf = item.physical_field.root.rsplit(".", 1)[-1].casefold()
        leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1

    candidate_values: list[RankedGovernedCandidate] = []
    for rank, item in enumerate(ranked_all, start=1):
        candidate_id = candidate_ids.issue(
            scope=current_scope,
            binding=item,
            request_digest=request_digest,
            nonce=nonce,
        )
        candidate_values.append(
            _enrich_candidate(
                item=item,
                candidate_id=candidate_id,
                rank=rank,
                purposes=tuple(sorted(purpose_membership[item.binding_id])),
                duplicate_logical=item.logical_field.root in duplicate_logical,
                homonymous=leaf_counts[item.physical_field.root.rsplit(".", 1)[-1].casefold()] > 1,
                registry=registry,
            )
        )
    shortlist = GovernedShortlist(
        scope=current_scope,
        query_fingerprint=request_digest,
        expansion_fingerprint=expansion.fingerprint,
        candidates=tuple(candidate_values),
    )

    if global_shortlist_tie_overflow:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="candidate_tie_overflow",
        )
    if conflict:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            semantic_state=SemanticMatchState.CONFLICTING,
        )
    if owner_ambiguous:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            semantic_state=SemanticMatchState.AMBIGUOUS,
        )
    if any(not values for values in per_purpose.values()):
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            semantic_state=SemanticMatchState.NO_MATCH,
        )
    if _bare_identifier_requires_human_choice(original_text, shortlist):
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            semantic_state=SemanticMatchState.AMBIGUOUS,
        )

    selected_bindings, coherent_branch_ambiguous = _select_coherent_bindings(
        per_purpose=per_purpose,
        registry=registry,
    )
    if coherent_branch_ambiguous:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            semantic_state=SemanticMatchState.AMBIGUOUS,
        )
    if not selected_bindings:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="join_closure_overflow",
        )
    branch_models = {_binding_model(item) for item in selected_bindings}
    # A tie across the per-purpose 2/3 boundary is unsafe only when it can
    # change the bounded vocabulary. Alternatives outside the unique top-1
    # branch are deterministically excluded by the coherent-closure policy.
    if any(
        any(_binding_model(item) in branch_models for item in tied)
        for tied in purpose_boundary_ties.values()
    ):
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="candidate_tie_overflow",
        )
    selected_logical: dict[str, GovernedFieldBinding] = {}
    for item in selected_bindings:
        previous = selected_logical.get(item.logical_field.root)
        if previous is None or _binding_sort_tuple(item) < _binding_sort_tuple(previous):
            selected_logical[item.logical_field.root] = item
    selected_scores_by_logical: dict[str, dict[str, int]] = {}
    for purpose_id, values in per_purpose.items():
        for item in values[:MAX_CANDIDATES_PER_PURPOSE]:
            logical_field = item.logical_field.root
            retained = selected_logical.get(logical_field)
            if (
                retained is not None
                and item.binding_id == retained.binding_id
                and _binding_model(item) in branch_models
            ):
                selected_scores_by_logical.setdefault(logical_field, {})[purpose_id] = (
                    item.signals.total
                )
    if len(selected_logical) > MAX_PROMPT_FIELDS:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="field_closure_overflow",
        )
    shortlist_by_binding = {item.binding.binding_id: item for item in shortlist.candidates}
    closure_candidates = tuple(
        shortlist_by_binding[item.binding_id]
        for item in selected_logical.values()
        if item.binding_id in shortlist_by_binding
    )
    if len(closure_candidates) != len(selected_logical):
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="shortlist_closure_overflow",
        )
    try:
        vocabulary = _build_vocabulary(
            closure_candidates,
            registry,
            purpose_uses={probe.purpose_id: probe.intended_use for probe in expansion.probes},
            purpose_ids_by_binding={
                retained.binding_id: tuple(
                    sorted(selected_scores_by_logical.get(logical_field, {}))
                )
                for logical_field, retained in selected_logical.items()
            },
            purpose_scores_by_binding={
                retained.binding_id: tuple(
                    QueryStudioPromptPurposeScore(
                        purpose_id=purpose_id,
                        score=score,
                    )
                    for purpose_id, score in sorted(
                        selected_scores_by_logical.get(logical_field, {}).items()
                    )
                )
                for logical_field, retained in selected_logical.items()
            },
        )
    except QueryStudioError:
        return _Closure(
            shortlist=shortlist,
            vocabulary=None,
            operational_state=QueryStudioOperationalState.CLOSURE_OVERFLOW,
            reason_code="join_closure_overflow",
        )
    if retrieval_ambiguous:
        return _Closure(
            shortlist=shortlist,
            vocabulary=vocabulary,
            semantic_state=SemanticMatchState.AMBIGUOUS,
        )
    return _Closure(shortlist=shortlist, vocabulary=vocabulary)


def _bare_identifier_requires_human_choice(
    original_text: DescriptionQuery,
    shortlist: GovernedShortlist,
) -> bool:
    """Keep an under-specified physical-looking leaf out of model interpretation."""

    normalized = unicodedata.normalize("NFKC", original_text.root).casefold().strip()
    if _BARE_IDENTIFIER_LIKE_FIELD.fullmatch(normalized) is None:
        return False
    alternatives = {
        (
            item.binding.logical_field.root,
            item.binding.locator.asset.connection_id.root,
            item.binding.physical_field.root,
        )
        for item in shortlist.candidates
    }
    return len(alternatives) > 1


def _validate_expansion_source_spans(
    original_text: DescriptionQuery,
    expansion: DescriptionExpansion,
) -> None:
    """Reject provider spans that are not exact, atomic excerpts before any retrieval."""

    source = original_text.root.casefold()
    for probe in expansion.probes:
        if probe.source_span is None:
            continue
        span = probe.source_span.casefold()
        atomicity_text = "".join(
            character
            for character in unicodedata.normalize("NFKD", span)
            if not unicodedata.combining(character)
        )
        invalid_analytical_span = (
            _NON_ATOMIC_ANALYTICAL_SOURCE_SPAN.search(atomicity_text) is not None
        )
        invalid_conditional_span = (
            probe.intended_use is not QueryFieldPurpose.FILTER
            and _CONDITIONAL_SOURCE_SPAN.search(atomicity_text) is not None
        )
        if span not in source or invalid_analytical_span or invalid_conditional_span:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "Query Studio expansion contains a non-grounded or non-atomic source span",
                output_failure_category=ProviderOutputFailureCategory.GROUNDING,
            )


def _select_coherent_bindings(
    *,
    per_purpose: dict[str, tuple[GovernedFieldBinding, ...]],
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[tuple[GovernedFieldBinding, ...], bool]:
    """Anchor every purpose at top-1, then retain only same-branch alternatives.

    Provider probes and lexical scores are retrieval evidence, never authority
    to repair meaning. A disconnected top-1 therefore fails closed instead of
    being replaced by a lower-ranked candidate merely because that candidate
    would make the joins connect.
    """

    bounded = {
        purpose: values[:MAX_CANDIDATES_PER_PURPOSE] for purpose, values in per_purpose.items()
    }
    if any(
        len(values) > 1
        and values[0].signals.total == values[1].signals.total
        and _binding_model(values[0]) != _binding_model(values[1])
        for values in bounded.values()
    ):
        return (), True
    anchors = tuple(bounded[purpose][0] for purpose in sorted(bounded))
    anchor_models = tuple(sorted({_binding_model(item) for item in anchors}))
    if not _models_form_prompt_closure(anchor_models, registry):
        return (), False
    allowed_models = set(anchor_models)
    coherent = tuple(
        item
        for purpose in sorted(bounded)
        for item in bounded[purpose]
        if _binding_model(item) in allowed_models
    )
    return coherent, False


def _binding_model(item: GovernedFieldBinding) -> str:
    return item.logical_field.root.split(".", 1)[0]


def _eligible_owner_models(
    probe: DescriptionSearchProbe,
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[str, ...]:
    """Resolve owner anchors only against exact governed model identity tokens."""

    if not probe.owner_focus:
        return tuple(sorted(model.id.root for model in registry.logical_context.models))
    required = frozenset(probe.owner_focus)
    return tuple(
        sorted(
            model.id.root
            for model in registry.logical_context.models
            if required.issubset(_model_identity_tokens(model.id.root))
        )
    )


def _model_identity_tokens(model_id: str) -> frozenset[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", model_id)
    tokens = frozenset(re.findall(r"[a-z0-9]+", separated.replace("_", " ").casefold()))
    compact = "".join(tokens)
    return frozenset((*tokens, compact)) if compact else tokens


def _binding_is_eligible_for_probe(
    item: GovernedFieldBinding,
    *,
    probe: DescriptionSearchProbe,
    owner_models: tuple[str, ...],
    registry: GovernedSemanticRegistrySnapshot,
) -> bool:
    """Apply server-owned owner, role, and type constraints before ranking."""

    if _binding_model(item) not in owner_models:
        return False
    field = registry.logical_context.field_index()[item.logical_field.root]
    return (not probe.roles or field.role in probe.roles) and (
        not probe.canonical_types or field.canonical_type in probe.canonical_types
    )


def _models_form_prompt_closure(
    model_ids: tuple[str, ...],
    registry: GovernedSemanticRegistrySnapshot,
) -> bool:
    if not model_ids or len(model_ids) > MAX_PROMPT_MODELS:
        return False
    if len(model_ids) == 1:
        return True
    joins = _prompt_joins_for_models(model_ids, registry)
    return len(joins) <= MAX_PROMPT_JOINS and _models_connected(model_ids, joins)


def _enrich_candidate(
    *,
    item: GovernedFieldBinding,
    candidate_id: OpaqueCandidateId,
    rank: int,
    purposes: tuple[str, ...],
    duplicate_logical: bool,
    homonymous: bool,
    registry: GovernedSemanticRegistrySnapshot,
) -> RankedGovernedCandidate:
    context = registry.logical_context
    field = context.field_index().get(item.logical_field.root)
    if field is None:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CANDIDATE,
            "Governed candidate references an unknown logical field",
        )
    model_id = item.logical_field.root.split(".", 1)[0]
    model = context.model_index().get(model_id)
    if model is None or len(model.description) > 300 or len(field.definition) > 300:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Governed definitions exceed the bounded model vocabulary",
        )
    allowed_values = _governed_allowed_values(item, registry)
    if len(allowed_values) > 12:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Governed allowed-value vocabulary exceeds the model boundary",
        )
    risks = _candidate_risks(
        item,
        role=field.role,
        duplicate_logical=duplicate_logical,
        homonymous=homonymous,
    )
    return RankedGovernedCandidate(
        candidate_id=candidate_id,
        binding=item,
        logical_model=model.id,
        model_definition=model.description,
        field_definition=field.definition,
        canonical_type=field.canonical_type,
        role=field.role,
        allowed_values=allowed_values,
        model_version=model.version,
        field_version=field.version,
        purposes=purposes,
        rank=rank,
        risks=risks,
    )


def _candidate_risks(
    item: GovernedFieldBinding,
    *,
    role: LogicalFieldRole,
    duplicate_logical: bool,
    homonymous: bool,
) -> tuple[CandidateRiskCode, ...]:
    risks: set[CandidateRiskCode] = set()
    if duplicate_logical:
        risks.add(CandidateRiskCode.MULTIPLE_PHYSICAL_BINDINGS)
    if homonymous:
        risks.add(CandidateRiskCode.HOMONYMOUS_FIELD)
    nonzero = {signal.code.value for signal in item.signals.signals if signal.value > 0}
    if nonzero and nonzero <= {"definition_overlap"}:
        risks.add(CandidateRiskCode.DESCRIPTION_ONLY_EVIDENCE)
    if role is LogicalFieldRole.IDENTIFIER and item.nullable is True:
        risks.add(CandidateRiskCode.NULLABLE_IDENTIFIER)
    if role is LogicalFieldRole.IDENTIFIER and item.physical_type.value == "float":
        risks.add(CandidateRiskCode.UNSAFE_FLOAT_IDENTIFIER)
    if item.evidence_status not in {
        ExecutableEvidenceStatus.CURRENT,
        ExecutableEvidenceStatus.REVALIDATED,
    }:
        risks.add(CandidateRiskCode.STALE_EVIDENCE)
    return tuple(sorted(risks, key=lambda value: value.value))


def _governed_allowed_values(
    item: GovernedFieldBinding,
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[str, ...]:
    field = registry.logical_context.field_index()[item.logical_field.root]
    if field.allowed_values:
        return field.allowed_values
    targets: set[str] = set()
    for governed in registry.mapping_set.mappings:
        if governed.mapping.logical_field != item.logical_field:
            continue
        for step in governed.mapping.transformation_plan.steps:
            if isinstance(step, MapValuesStep):
                targets.update(
                    entry.target
                    for entry in step.entries
                    if isinstance(entry.target, str) and entry.target.strip()
                )
    return tuple(sorted(targets))


def _build_vocabulary(
    candidates: tuple[RankedGovernedCandidate, ...],
    registry: GovernedSemanticRegistrySnapshot,
    *,
    purpose_uses: dict[str, QueryFieldPurpose],
    required_selection_counts: QueryStudioRequiredSelectionCounts | None = None,
    purpose_ids_by_binding: dict[str, tuple[str, ...]] | None = None,
    purpose_scores_by_binding: (dict[str, tuple[QueryStudioPromptPurposeScore, ...]] | None) = None,
) -> QueryStudioPromptVocabulary:
    if not candidates:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Query Studio prompt closure is empty",
        )
    context = registry.logical_context
    model_ids = tuple(sorted({item.logical_model.root for item in candidates}))
    if len(model_ids) > MAX_PROMPT_MODELS:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Query Studio prompt requires more than three models",
        )
    model_index = context.model_index()
    models = tuple(
        QueryStudioPromptModel(
            id=model_index[model_id].id,
            definition=model_index[model_id].description,
        )
        for model_id in model_ids
    )
    joins = _prompt_joins_for_models(model_ids, registry)
    if len(model_ids) > 1 and (
        len(joins) > MAX_PROMPT_JOINS or not _models_connected(model_ids, joins)
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Query Studio approved join closure is incomplete or too large",
        )
    if purpose_ids_by_binding is not None and any(
        not purpose_ids_by_binding.get(item.binding.binding_id)
        or any(
            purpose_id not in purpose_uses
            for purpose_id in purpose_ids_by_binding.get(item.binding.binding_id, ())
        )
        for item in candidates
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Query Studio exact purpose membership is incomplete",
        )
    if (purpose_ids_by_binding is None) != (purpose_scores_by_binding is None):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Query Studio purpose scores require exact purpose membership",
        )
    if purpose_ids_by_binding is not None:
        supplied_purposes = {
            purpose_id
            for item in candidates
            for purpose_id in purpose_ids_by_binding[item.binding.binding_id]
        }
        if supplied_purposes != set(purpose_uses):
            raise QueryStudioError(
                QueryStudioErrorCode.INVALID_CONTEXT,
                "Query Studio exact purpose membership does not cover every retrieval purpose",
            )
        if any(
            not 1
            <= sum(
                purpose_id in purpose_ids_by_binding[item.binding.binding_id] for item in candidates
            )
            <= MAX_CANDIDATES_PER_PURPOSE
            for purpose_id in purpose_uses
        ):
            raise QueryStudioError(
                QueryStudioErrorCode.CLOSURE_OVERFLOW,
                "Query Studio exact purpose membership exceeds its bounded candidate set",
            )
    prompt_candidates = tuple(
        QueryStudioPromptCandidate(
            candidate_id=item.candidate_id,
            logical_field=item.binding.logical_field,
            definition=item.field_definition,
            canonical_type=item.canonical_type,
            role=item.role,
            intended_uses=_candidate_intended_uses(
                (
                    item.purposes
                    if purpose_ids_by_binding is None
                    else purpose_ids_by_binding[item.binding.binding_id]
                ),
                purpose_uses,
            ),
            purpose_ids=(
                item.purposes
                if purpose_ids_by_binding is None
                else purpose_ids_by_binding[item.binding.binding_id]
            ),
            purpose_scores=(
                tuple(
                    QueryStudioPromptPurposeScore(
                        purpose_id=purpose_id,
                        score=item.binding.signals.total,
                    )
                    for purpose_id in item.purposes
                )
                if purpose_scores_by_binding is None
                else purpose_scores_by_binding[item.binding.binding_id]
            ),
            allowed_values=item.allowed_values,
            score=item.binding.signals.total,
            risks=item.risks,
        )
        for item in sorted(candidates, key=lambda value: value.rank)
    )
    try:
        return QueryStudioPromptVocabulary(
            context_source=context.source,
            context_version=context.version,
            models=models,
            candidates=prompt_candidates,
            joins=joins,
            required_selection_counts=(
                required_selection_counts
                if required_selection_counts is not None
                else QueryStudioRequiredSelectionCounts(
                    dimensions=sum(
                        purpose is QueryFieldPurpose.DIMENSION for purpose in purpose_uses.values()
                    ),
                    metrics=sum(
                        purpose is QueryFieldPurpose.METRIC for purpose in purpose_uses.values()
                    ),
                    filters=sum(
                        purpose is QueryFieldPurpose.FILTER for purpose in purpose_uses.values()
                    ),
                )
            ),
            metric_operations=tuple(MetricOperation),
            filter_operators=tuple(FilterOperator),
            date_grains=tuple(DateGrain),
            sort_directions=tuple(SortDirection),
        )
    except ValidationError as error:
        raise QueryStudioError(
            QueryStudioErrorCode.CLOSURE_OVERFLOW,
            "Query Studio prompt closure violates a bounded contract",
        ) from error


def _candidate_intended_uses(
    purpose_ids: tuple[str, ...],
    purpose_uses: dict[str, QueryFieldPurpose],
) -> tuple[QueryFieldPurpose, ...]:
    try:
        values = {purpose_uses[purpose] for purpose in purpose_ids}
    except KeyError as error:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Query Studio candidate references an unknown retrieval purpose",
        ) from error
    return tuple(sorted(values, key=lambda item: item.value))


def _prompt_joins_for_models(
    model_ids: tuple[str, ...],
    registry: GovernedSemanticRegistrySnapshot,
) -> tuple[QueryStudioPromptJoin, ...]:
    contracts = {contract.id: contract for contract in registry.join_contracts.contracts}
    return tuple(
        QueryStudioPromptJoin(
            id=join.id,
            left_model=join.left_model,
            right_model=join.right_model,
            left_field=contracts[join.id].left_key.logical_field,
            right_field=contracts[join.id].right_key.logical_field,
            cardinality=join.cardinality,
            fanout_policy=join.fanout_policy,
        )
        for join in registry.logical_context.joins
        if join.left_model.root in model_ids and join.right_model.root in model_ids
    )


def _models_connected(
    model_ids: tuple[str, ...],
    joins: tuple[QueryStudioPromptJoin, ...],
) -> bool:
    pending = {model_ids[0]}
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for join in joins:
            if join.left_model.root == current:
                pending.add(join.right_model.root)
            elif join.right_model.root == current:
                pending.add(join.left_model.root)
    return visited == set(model_ids)


def _validate_binding_against_registry(
    item: GovernedFieldBinding,
    registry: GovernedSemanticRegistrySnapshot,
) -> None:
    matches = tuple(
        governed
        for governed in registry.mapping_set.mappings
        if governed.mapping.logical_field == item.logical_field
        and governed.mapping.physical_field == item.physical_field
        and governed.mapping.version == item.mapping_version
        and governed.approval_decision_id == item.mapping_approval_decision_id
        and governed.physical_type == item.physical_type
    )
    if len(matches) != 1:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CANDIDATE,
            "Governed search returned a row outside the exact active registry",
        )


def _logical_allowlist(
    *,
    roles: tuple[LogicalFieldRole, ...],
    canonical_types: tuple[CanonicalType, ...],
    logical_models: tuple[LogicalModelRef, ...] | None,
    registry: GovernedSemanticRegistrySnapshot,
) -> GovernedBindingFactsFilters:
    restricted = bool(roles or canonical_types) or logical_models is not None
    if not restricted:
        return GovernedBindingFactsFilters()
    allowed_models = None if logical_models is None else {model.root for model in logical_models}
    logical_fields = tuple(
        sorted(
            (
                field.id
                for field in registry.logical_context.field_index().values()
                if (allowed_models is None or field.id.root.split(".", 1)[0] in allowed_models)
                if (not roles or field.role in roles)
                and (not canonical_types or field.canonical_type in canonical_types)
            ),
            key=lambda item: item.root,
        )
    )
    return GovernedBindingFactsFilters(
        logical_fields=logical_fields,
        restrict_logical_fields=True,
    )


def _score_binding_from_registry(
    *,
    item: GovernedFieldBinding,
    query: str,
    registry: GovernedSemanticRegistrySnapshot,
) -> GovernedFieldBinding | None:
    field = registry.logical_context.field_index().get(item.logical_field.root)
    model = registry.logical_context.model_index().get(item.logical_field.root.split(".", 1)[0])
    if field is None or model is None:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CANDIDATE,
            "Governed binding has no active logical definition",
        )
    signals = score_governed_description(
        query,
        logical_field=item.logical_field.root,
        model_description=model.description,
        field_definition=field.definition,
        role=field.role,
        canonical_type=field.canonical_type,
        allowed_values=field.allowed_values,
        physical_field=item.physical_field.root,
        physical_definitions=(item.definition,) if item.definition is not None else (),
        native_types=(item.native_type,) if item.native_type is not None else (),
        taxonomy=item.tags + item.glossary_terms,
    )
    if signals.total == 0:
        return None
    return item.model_copy(update={"signals": signals})


def _binding_sort_tuple(item: GovernedFieldBinding) -> tuple[int, str, str]:
    return (-item.signals.total, item.logical_field.root, item.binding_id)


def _has_physical_meaning_conflict(values: tuple[GovernedFieldBinding, ...]) -> bool:
    meanings: dict[str, str] = {}
    for item in values:
        previous = meanings.setdefault(
            item.physical_field.root,
            item.logical_field.root,
        )
        if previous != item.logical_field.root:
            return True
    return False


def _ensure_registry_still_matches(
    loaded: ScopedSemanticRegistrySnapshot,
    scope: QueryStudioScopeSnapshot,
) -> None:
    if (
        loaded.scope != scope.scope
        or loaded.registry.version != scope.registry_version
        or loaded.registry.fingerprint != scope.registry_fingerprint
        or (
            loaded.activation_generation is not None
            and loaded.activation_generation != scope.pointer_generation
        )
        or (
            loaded.active_pointer_fingerprint is not None
            and loaded.active_pointer_fingerprint != scope.pointer_fingerprint
        )
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.STALE_PREVIEW,
            "Query Studio registry or active pointer changed",
        )


def _guided_input(draft: GuidedRequestDraft) -> GuidedRequestInput:
    return GuidedRequestInput(
        primary_entity=draft.primary_entity.root,
        dimensions=tuple(
            GuidedDimensionInput(
                field=item.field.root,
                grain=item.grain.value if item.grain is not None else None,
            )
            for item in draft.dimensions
        ),
        metrics=tuple(
            GuidedMetricInput(
                operation=item.operation.value,
                field=item.field.root,
                alias=item.alias,
            )
            for item in draft.metrics
        ),
        filters=tuple(
            GuidedFilterInput(
                field=item.field.root,
                operator=item.operator.value,
                value=item.value,
            )
            for item in draft.filters
        ),
        order_by=tuple(
            GuidedOrderInput(field=item.field.root, direction=item.direction.value)
            for item in draft.order_by
        ),
        limit=draft.limit,
    )


def _validate_usage(
    usage: ProviderUsageFacts | None,
    configuration: ProviderConfigurationFacts,
) -> ProviderUsageFacts:
    if usage is None or (
        usage.model_snapshot != configuration.model_snapshot
        or usage.configuration_fingerprint != configuration.fingerprint
    ):
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Provider usage does not match the configured model snapshot",
        )
    return usage


def _provider_failure_preview(
    error: QueryStudioPortError,
    *,
    shortlist: GovernedShortlist | None = None,
    vocabulary: QueryStudioPromptVocabulary | None = None,
    expansion: DescriptionExpansion | None = None,
    usage: tuple[ProviderUsageFacts, ...] = (),
) -> QueryStudioPreview:
    state = {
        QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED: QueryStudioOperationalState.RATE_LIMITED,
        QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED: (
            QueryStudioOperationalState.QUOTA_EXHAUSTED
        ),
        QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED: (
            QueryStudioOperationalState.SENSITIVE_INPUT_BLOCKED
        ),
    }.get(error.code, QueryStudioOperationalState.PROVIDER_UNAVAILABLE)
    return _operational_preview(
        state,
        error.code.value,
        output_failure_category=error.output_failure_category,
        shortlist=shortlist,
        vocabulary=vocabulary,
        expansion=expansion,
        usage=usage,
    )


def _operational_preview(
    state: QueryStudioOperationalState,
    reason_code: str,
    *,
    output_failure_category: ProviderOutputFailureCategory | None = None,
    shortlist: GovernedShortlist | None = None,
    vocabulary: QueryStudioPromptVocabulary | None = None,
    expansion: DescriptionExpansion | None = None,
    usage: tuple[ProviderUsageFacts, ...] = (),
) -> QueryStudioPreview:
    return QueryStudioPreview(
        mode="natural_language",
        operational_state=state,
        shortlist=shortlist,
        vocabulary=vocabulary,
        expansion=expansion,
        provider_usage=usage,
        output_failure_category=output_failure_category,
        reason_code=reason_code,
    )


def _require_aware_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            "Query Studio clock returned a timezone-naive instant",
        )


def _validated_model(
    value: object,
    model: type[_ModelT],
    label: str,
) -> _ModelT:
    try:
        if not isinstance(value, BaseModel):
            raise TypeError("response is not a typed model")
        return model.model_validate(value.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise QueryStudioError(
            QueryStudioErrorCode.INVALID_CONTEXT,
            f"{label} is invalid",
        ) from error


__all__ = [
    "BrowseGuidedGovernedFields",
    "ConfirmGuidedQueryStudioPreview",
    "ConfirmQueryStudioPreview",
    "DiscoverPhysicalFields",
    "InspectPhysicalDiscoveryCardinality",
    "PrepareGuidedQueryStudioPreview",
    "PrepareGuidedSelectionQueryStudioPreview",
    "PrepareNaturalLanguageQueryStudioPreview",
    "QueryStudioError",
    "QueryStudioErrorCode",
    "RecomputeGuidedQueryStudioEvidence",
    "RecomputeNaturalLanguageQueryStudioPreview",
    "RegistryAwareGovernedFieldSearch",
    "SearchGovernedFields",
    "natural_language_edit_candidates",
    "proposal_to_guided_draft",
    "rank_governed_bindings",
]
