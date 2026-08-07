from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schemabridge.application.query_studio import (
    QueryStudioError,
    QueryStudioErrorCode,
)
from schemabridge.bootstrap import QueryStudioRuntimeServices, build_query_studio_runtime
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.query_studio import (
    ProviderStage,
    QueryStudioConfirmation,
    QueryStudioConfirmationAction,
    QueryStudioOperationalState,
    SemanticMatchState,
)
from schemabridge.entrypoints.streamlit.query_studio_acceptance import (
    M27_BROWSER_DELAY_SECONDS,
    M27_BROWSER_RELEASE_REF,
    M27_BROWSER_SCENARIO_ENV,
    M27BrowserScenario,
    decorate_m27_query_studio_runtime,
    load_m27_browser_scenario,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
NORTH_STAR = (
    "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="m27-browser-scenario-analyst",
        workspace_id="m27-browser-scenario",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _runtime() -> QueryStudioRuntimeServices:
    return build_query_studio_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=Settings(
            _env_file=None,
            OPENAI_API_KEY=None,
            DATABASE_URL=None,
            SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="fake",
            SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
                "m27-browser-scenario-signing-key-with-byte-diversity-2026"
            ),
        ),
    )


def _isolated_environment() -> dict[str, str]:
    return {
        "SCHEMABRIDGE_ENVIRONMENT": "development",
        "SCHEMABRIDGE_AUTH_MODE": "local-demo",
        "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
        "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "fake",
        "SCHEMABRIDGE_RELEASE_REF": M27_BROWSER_RELEASE_REF,
        M27_BROWSER_SCENARIO_ENV: M27BrowserScenario.BASELINE.value,
    }


def test_scenario_loader_requires_exact_key_free_profile_and_closed_value() -> None:
    environment = _isolated_environment()

    assert load_m27_browser_scenario(environment) is M27BrowserScenario.BASELINE

    for key in (
        "SCHEMABRIDGE_ENVIRONMENT",
        "SCHEMABRIDGE_AUTH_MODE",
        "SCHEMABRIDGE_PUBLICATION_MODE",
        "SCHEMABRIDGE_JUDGE_EXECUTION",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE",
        "SCHEMABRIDGE_RELEASE_REF",
    ):
        with pytest.raises(ValueError, match="isolated fake"):
            load_m27_browser_scenario({**environment, key: "attacker-value"})
    with pytest.raises(ValueError, match="absent provider key"):
        load_m27_browser_scenario({**environment, "OPENAI_API_KEY": ""})
    with pytest.raises(ValueError, match="closed allowlist"):
        load_m27_browser_scenario({**environment, M27_BROWSER_SCENARIO_ENV: "caller-controlled"})
    without_scenario = dict(environment)
    without_scenario.pop(M27_BROWSER_SCENARIO_ENV)
    with pytest.raises(ValueError, match="scenario is required"):
        load_m27_browser_scenario(without_scenario)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        (
            M27BrowserScenario.PROVIDER_UNAVAILABLE,
            QueryStudioOperationalState.PROVIDER_UNAVAILABLE,
        ),
        (M27BrowserScenario.RATE_LIMITED, QueryStudioOperationalState.RATE_LIMITED),
        (
            M27BrowserScenario.QUOTA_EXHAUSTED,
            QueryStudioOperationalState.QUOTA_EXHAUSTED,
        ),
    ),
)
def test_provider_failure_scenarios_flow_through_the_real_application_mapping(
    scenario: M27BrowserScenario,
    expected: QueryStudioOperationalState,
) -> None:
    runtime = decorate_m27_query_studio_runtime(_runtime(), scenario)
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(NORTH_STAR, UserLanguage.SPANISH)

    assert preview.operational_state is expected
    assert preview.semantic_state is None
    assert preview.token is None
    assert preview.guided_preview is None
    assert preview.expansion is not None
    assert preview.shortlist is not None
    assert preview.vocabulary is not None
    assert tuple(item.stage for item in preview.provider_usage) == (ProviderStage.EXPANSION,)


def test_conflicting_intent_uses_real_retrieval_and_successful_fake_usage() -> None:
    runtime = decorate_m27_query_studio_runtime(
        _runtime(),
        M27BrowserScenario.CONFLICTING_INTENT,
    )
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(NORTH_STAR, UserLanguage.SPANISH)

    assert preview.semantic_state is SemanticMatchState.CONFLICTING
    assert preview.operational_state is None
    assert preview.shortlist is not None
    assert preview.vocabulary is not None
    assert preview.proposal is not None
    assert preview.proposal.semantic_state is SemanticMatchState.CONFLICTING
    assert len(preview.provider_usage) == 2
    assert preview.token is None


def test_delayed_scenario_waits_once_in_local_expansion_then_delegates() -> None:
    waits: list[float] = []
    runtime = decorate_m27_query_studio_runtime(
        _runtime(),
        M27BrowserScenario.DELAYED_EXPANSION,
        sleeper=lambda seconds: waits.append(seconds),
    )
    assert runtime.prepare_natural is not None

    preview = runtime.prepare_natural.execute(NORTH_STAR, UserLanguage.SPANISH)

    assert waits == [M27_BROWSER_DELAY_SECONDS]
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.token is not None


def test_expired_token_scenario_issues_a_real_signature_then_fails_confirmation() -> None:
    runtime = decorate_m27_query_studio_runtime(
        _runtime(),
        M27BrowserScenario.EXPIRED_TOKEN,
    )
    assert runtime.prepare_natural is not None
    assert runtime.confirm_natural is not None
    preview = runtime.prepare_natural.execute(NORTH_STAR, UserLanguage.SPANISH)
    assert preview.semantic_state is SemanticMatchState.ALIGNED
    assert preview.expansion is not None
    assert preview.proposal is not None
    assert preview.token is not None

    with pytest.raises(QueryStudioError) as expired:
        runtime.confirm_natural.execute(
            QueryStudioConfirmation(
                original_text=NORTH_STAR,
                language=UserLanguage.SPANISH,
                expansion=preview.expansion,
                proposal=preview.proposal,
                token=preview.token,
                action=QueryStudioConfirmationAction.CONFIRM_INTERPRETATION,
            )
        )

    assert expired.value.code is QueryStudioErrorCode.STALE_PREVIEW


def test_decorator_rejects_disabled_runtime_and_non_enum_scenarios() -> None:
    fake = _runtime()
    disabled = build_query_studio_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=Settings(_env_file=None, SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="disabled"),
    )

    with pytest.raises(ValueError, match="complete fake"):
        decorate_m27_query_studio_runtime(disabled, M27BrowserScenario.BASELINE)
    with pytest.raises(TypeError, match="closed enum"):
        decorate_m27_query_studio_runtime(fake, "baseline")  # type: ignore[arg-type]
