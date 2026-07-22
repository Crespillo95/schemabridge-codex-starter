"""Spanish natural-language and guided mode converge before deterministic compilation."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.application.governed_execution import PlanSemanticRequest
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    build_demo_guided_input,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.domain.intents import (
    IntentAlternativeId,
    IntentConfirmation,
    UserLanguage,
)
from schemabridge.domain.request_context import validated_analytical_request_fingerprint
from schemabridge.domain.resolution import (
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]


def test_natural_language_spanish_confirmation_matches_guided_request_and_plan() -> None:
    logical_path = ROOT / "demo/ground_truth/approved_logical_context.yml"
    context = RecordedRequestContextAdapter(logical_path)
    guided = BuildGuidedRequest(context).execute(
        build_demo_guided_input(GuidedRequestCase.NORTH_STAR)
    )
    resolver = ResolveNaturalLanguageIntent(
        parser=FakeIntentParser(),
        context=context,
        adapter_label="fake:typed-intent-only",
    )
    preview = resolver.preview(
        "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta.",
        UserLanguage.SPANISH,
    )
    natural = resolver.confirm(
        preview,
        IntentConfirmation(
            interpretation_fingerprint=preview.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )
    planner = PlanSemanticRequest(
        RecordedSemanticPlanningContext(
            logical_path,
            ROOT / "demo/ground_truth/planning_mappings.yml",
            ROOT / "demo/ground_truth/join_contracts.yml",
        ),
        ResolutionLimits(),
    )

    assert natural == guided
    assert validated_analytical_request_fingerprint(natural) == (
        validated_analytical_request_fingerprint(guided)
    )
    assert resolved_semantic_plan_fingerprint(planner.execute(natural)) == (
        resolved_semantic_plan_fingerprint(planner.execute(guided))
    )
