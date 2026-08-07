from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import yaml

from schemabridge.bootstrap import (
    QueryStudioRuntimeServices,
    build_query_studio_runtime,
    build_semantic_request_planner,
)
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    ConfirmedQueryStudioRequest,
    DescriptionQuery,
    GovernedFieldSearchRequest,
    OpaqueCandidateId,
    ProposedDimension,
    ProposedFilter,
    ProposedMetric,
    ProposedOrder,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioModelProposal,
    QueryStudioPreview,
    SemanticMatchState,
)
from schemabridge.domain.request_context import (
    validated_analytical_request_fingerprint,
)
from schemabridge.domain.resolution import resolved_semantic_plan_fingerprint

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)


def test_all_five_core_queries_converge_between_natural_and_guided_modes() -> None:
    principal = AuthenticatedPrincipal(
        actor_id="query-studio-equivalence-analyst",
        workspace_id="query-studio-equivalence",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    settings = Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATABASE_URL=None,
        SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="fake",
        SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
            "query-studio-equivalence-signing-key-with-byte-diversity"
        ),
    )
    runtime = build_query_studio_runtime(
        principal=principal,
        repository_root=ROOT,
        settings=settings,
    )
    guided_page = runtime.browse_guided.execute(
        GovernedFieldSearchRequest(scope=runtime.scope, page_size=50)
    )
    assert guided_page.next_key is None
    guided_ids: dict[str, OpaqueCandidateId] = {}
    for item in guided_page.items:
        guided_ids.setdefault(item.binding.logical_field.root, item.candidate_id)
    planner = build_semantic_request_planner(
        repository_root=ROOT,
        settings=settings,
        workspace_id=principal.workspace_id,
    )

    cases = _core_cases()
    assert len(cases) == 5
    for case in cases:
        text = cast(str, case["text"])
        language = UserLanguage(cast(str, case["language"]))
        natural_preview = _prepare_natural(runtime, text, language)
        natural_confirmed = _confirm_natural(
            runtime,
            natural_preview,
            text=text,
            language=language,
        )
        guided_proposal = _translate_proposal(
            natural_preview,
            guided_ids=guided_ids,
        )
        guided_preview = runtime.prepare_guided.execute(
            guided_proposal,
            context=guided_page.context,
        )
        assert guided_preview.token is not None
        guided_confirmed = runtime.confirm_guided.execute(
            QueryStudioConfirmation(
                original_text=None,
                language=None,
                expansion=None,
                proposal=guided_proposal,
                token=guided_preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )

        natural_request = natural_confirmed.validated_request
        guided_request = guided_confirmed.validated_request
        assert natural_request == guided_request, case["id"]
        assert validated_analytical_request_fingerprint(natural_request) == (
            validated_analytical_request_fingerprint(guided_request)
        )
        assert resolved_semantic_plan_fingerprint(planner.execute(natural_request)) == (
            resolved_semantic_plan_fingerprint(planner.execute(guided_request))
        )


def _prepare_natural(
    runtime: QueryStudioRuntimeServices,
    text: str,
    language: UserLanguage,
) -> QueryStudioPreview:
    assert runtime.prepare_natural is not None
    preview = runtime.prepare_natural.execute(text, language)
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.proposal is not None
    assert preview.expansion is not None
    assert preview.vocabulary is not None
    assert preview.token is not None
    return preview


def _confirm_natural(
    runtime: QueryStudioRuntimeServices,
    preview: QueryStudioPreview,
    *,
    text: str,
    language: UserLanguage,
) -> ConfirmedQueryStudioRequest:
    assert runtime.confirm_natural is not None
    assert preview.expansion is not None
    assert preview.proposal is not None
    assert preview.token is not None
    return runtime.confirm_natural.execute(
        QueryStudioConfirmation(
            original_text=DescriptionQuery(text),
            language=language,
            expansion=preview.expansion,
            proposal=preview.proposal,
            token=preview.token,
            action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
        )
    )


def _translate_proposal(
    preview: QueryStudioPreview,
    *,
    guided_ids: dict[str, OpaqueCandidateId],
) -> QueryStudioModelProposal:
    assert preview.proposal is not None
    assert preview.vocabulary is not None
    logical_by_natural_id = {
        item.candidate_id.root: item.logical_field.root for item in preview.vocabulary.candidates
    }

    def translate(value: OpaqueCandidateId) -> OpaqueCandidateId:
        return guided_ids[logical_by_natural_id[value.root]]

    proposal = preview.proposal
    assert proposal.primary_candidate_id is not None
    return QueryStudioModelProposal(
        semantic_state=SemanticMatchState.ALIGNED,
        primary_candidate_id=translate(proposal.primary_candidate_id),
        dimensions=tuple(
            ProposedDimension(
                candidate_id=translate(item.candidate_id),
                grain=item.grain,
            )
            for item in proposal.dimensions
        ),
        metrics=tuple(
            ProposedMetric(
                candidate_id=translate(item.candidate_id),
                operation=item.operation,
                alias=item.alias,
            )
            for item in proposal.metrics
        ),
        filters=tuple(
            ProposedFilter(
                candidate_id=translate(item.candidate_id),
                operator=item.operator,
                value=item.value,
            )
            for item in proposal.filters
        ),
        order_by=tuple(
            ProposedOrder(
                candidate_id=translate(item.candidate_id),
                direction=item.direction,
            )
            for item in proposal.order_by
        ),
        limit=proposal.limit,
    )


def _core_cases() -> list[dict[str, object]]:
    payload: object = yaml.safe_load(
        (ROOT / "demo/ground_truth/query_studio_matching.yml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    cases = cast(dict[str, object], payload).get("core_queries")
    assert isinstance(cases, list)
    assert all(isinstance(item, dict) for item in cases)
    return cast(list[dict[str, object]], cases)
