"""Acceptance-only Query Studio runtime decorators for internal-browser evidence.

This module is never imported by the production Streamlit entrypoint.  The
dedicated M27 browser script selects one closed scenario and decorates the real
fake-mode use cases, ports, token codec, and governed retrieval path.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

import streamlit as st

from schemabridge.application.ports.query_studio import (
    DescriptionExpansionPort,
    QueryStudioIntentPort,
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.bootstrap import (
    QueryStudioRuntimeServices,
    QueryStudioRuntimeSettings,
    build_query_studio_runtime,
)
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.query_studio import (
    MAX_PREVIEW_TTL_SECONDS,
    ConfirmedQueryStudioRequest,
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    QueryStudioModelProposal,
    SemanticMatchState,
)

M27_BROWSER_SCENARIO_ENV: Final = "SCHEMABRIDGE_M27_BROWSER_SCENARIO"
M27_BROWSER_RELEASE_REF: Final = "m27-browser-acceptance"
M27_BROWSER_DELAY_SECONDS: Final = 2.0
M27_DELAYED_COMPLETE_BUTTON_KEY: Final = "m27-complete-delayed-expansion"
M27_DELAYED_LOADING_TEXT: Final = (
    "query_studio_loading: Ejecutando expansión local determinista con retardo de aceptación."
)
_DELAYED_CANCEL_BUTTON_KEY: Final = "m27-cancel-delayed-expansion"
_DELAYED_PHASE_KEY: Final = "_m27_browser_delayed_expansion_phase"
_PRODUCTION_PREPARE_BUTTON_KEY: Final = "prepare-natural-query-studio"
_DELAYED_PHASE_LOADING: Final = "loading"
_DELAYED_PHASE_EXECUTE: Final = "execute"
_PREPARE_TIME: Final = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
_EXPIRED_TIME: Final = _PREPARE_TIME + timedelta(seconds=MAX_PREVIEW_TTL_SECONDS + 1)
_BASE_RUNTIME_BUILDER: Final = build_query_studio_runtime


class M27BrowserScenario(StrEnum):
    """Closed synthetic states available to the dedicated browser runtime."""

    BASELINE = "baseline"
    CONFLICTING_INTENT = "conflicting_intent"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    DELAYED_EXPANSION = "delayed_expansion"
    EXPIRED_TOKEN = "expired_token"


def load_m27_browser_scenario(environment: Mapping[str, str]) -> M27BrowserScenario:
    """Fail closed unless the process is the exact key-free acceptance profile."""

    required = {
        "SCHEMABRIDGE_ENVIRONMENT": "development",
        "SCHEMABRIDGE_AUTH_MODE": "local-demo",
        "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
        "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": "fake",
        "SCHEMABRIDGE_RELEASE_REF": M27_BROWSER_RELEASE_REF,
    }
    if any(environment.get(key) != value for key, value in required.items()):
        raise ValueError("M27 browser scenarios require the isolated fake acceptance profile")
    if "OPENAI_API_KEY" in environment:
        raise ValueError("M27 browser scenarios require an absent provider key")
    raw = environment.get(M27_BROWSER_SCENARIO_ENV)
    if raw is None:
        raise ValueError("M27 browser scenario is required")
    try:
        return M27BrowserScenario(raw)
    except ValueError as error:
        raise ValueError("M27 browser scenario is not in the closed allowlist") from error


def decorate_m27_query_studio_runtime(
    runtime: QueryStudioRuntimeServices,
    scenario: M27BrowserScenario,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> QueryStudioRuntimeServices:
    """Decorate the composed fake runtime without replacing governed behavior."""

    if not isinstance(scenario, M27BrowserScenario):
        raise TypeError("M27 browser scenario must be a closed enum value")
    if (
        runtime.ai_mode != "fake"
        or runtime.configuration.external_ai
        or runtime.expansion is None
        or runtime.prepare_natural is None
        or runtime.confirm_natural is None
        or runtime.recompute_natural is None
    ):
        raise ValueError("M27 browser scenarios require a complete fake Query Studio runtime")
    if scenario is M27BrowserScenario.BASELINE:
        return runtime
    if scenario is M27BrowserScenario.CONFLICTING_INTENT:
        interpreter = ConflictingQueryStudioIntent(runtime.prepare_natural.interpreter)
        return replace(
            runtime,
            prepare_natural=replace(
                runtime.prepare_natural,
                interpreter=interpreter,
            ),
        )
    failure_codes = {
        M27BrowserScenario.PROVIDER_UNAVAILABLE: (QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE),
        M27BrowserScenario.RATE_LIMITED: QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED,
        M27BrowserScenario.QUOTA_EXHAUSTED: (QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED),
    }
    if scenario in failure_codes:
        failure_interpreter: QueryStudioIntentPort = FailingQueryStudioIntent(
            runtime.prepare_natural.interpreter,
            failure_codes[scenario],
        )
        return _replace_interpreter(runtime, failure_interpreter)
    if scenario is M27BrowserScenario.DELAYED_EXPANSION:
        expansion = DelayedLocalDescriptionExpansion(
            runtime.expansion,
            delay_seconds=M27_BROWSER_DELAY_SECONDS,
            sleeper=sleeper,
        )
        return _replace_expansion(runtime, expansion)
    if scenario is M27BrowserScenario.EXPIRED_TOKEN:
        prepare_clock = FixedQueryStudioClock(_PREPARE_TIME)
        expired_clock = FixedQueryStudioClock(_EXPIRED_TIME)
        return replace(
            runtime,
            browse_guided=replace(runtime.browse_guided, clock=prepare_clock),
            prepare_guided=replace(runtime.prepare_guided, clock=prepare_clock),
            confirm_guided=replace(runtime.confirm_guided, clock=expired_clock),
            prepare_natural=replace(runtime.prepare_natural, clock=prepare_clock),
            confirm_natural=replace(runtime.confirm_natural, clock=expired_clock),
            recompute_natural=replace(runtime.recompute_natural, clock=expired_clock),
        )
    raise AssertionError("closed M27 browser scenario was not handled")


def build_m27_browser_scenario_runtime(
    *,
    principal: AuthenticatedPrincipal,
    scenario: M27BrowserScenario,
    repository_root: Path | None = None,
    settings: QueryStudioRuntimeSettings | None = None,
) -> QueryStudioRuntimeServices:
    """Compose from the captured production builder, then apply one decorator."""

    runtime = _BASE_RUNTIME_BUILDER(
        principal=principal,
        repository_root=repository_root,
        settings=settings,
    )
    return decorate_m27_query_studio_runtime(runtime, scenario)


def render_m27_delayed_query_studio(
    runtime: QueryStudioRuntimeServices,
    *,
    can_create: bool,
    delegate: Callable[..., ConfirmedQueryStudioRequest | None],
) -> ConfirmedQueryStudioRequest | None:
    """Expose loading for one full rerun before executing the delayed fake port."""

    phase = st.session_state.get(_DELAYED_PHASE_KEY)
    if phase not in {None, _DELAYED_PHASE_LOADING, _DELAYED_PHASE_EXECUTE}:
        st.session_state.pop(_DELAYED_PHASE_KEY, None)
        phase = None
    if not can_create:
        st.session_state.pop(_DELAYED_PHASE_KEY, None)
        return delegate(runtime, can_create=can_create)
    loading_placeholder = None
    if phase == _DELAYED_PHASE_LOADING:
        loading_placeholder = st.empty()
        with loading_placeholder.container():
            st.markdown("#### Carga visible de aceptación")
            st.info(M27_DELAYED_LOADING_TEXT)
            st.caption(
                "La expansión es local, determinista, key-free y no ejecutable. "
                "Permanecerá visible hasta que completes o canceles esta fase; todavía "
                "no hay interpretación externa, SQL ni acceso a datos."
            )
            complete, cancel = st.columns(2)
            complete_clicked = complete.button(
                "Completar carga determinista",
                type="primary",
                key=M27_DELAYED_COMPLETE_BUTTON_KEY,
            )
            cancel_clicked = cancel.button(
                "Cancelar carga de aceptación",
                key=_DELAYED_CANCEL_BUTTON_KEY,
            )
        if complete_clicked:
            st.session_state[_DELAYED_PHASE_KEY] = _DELAYED_PHASE_EXECUTE
            phase = _DELAYED_PHASE_EXECUTE
        elif cancel_clicked:
            st.session_state.pop(_DELAYED_PHASE_KEY, None)
            phase = None
        else:
            return _render_with_delayed_prepare_button(
                runtime,
                can_create=can_create,
                delegate=delegate,
                execute=False,
                loading=True,
            )
    result = _render_with_delayed_prepare_button(
        runtime,
        can_create=can_create,
        delegate=delegate,
        execute=phase == _DELAYED_PHASE_EXECUTE,
        loading=False,
    )
    if loading_placeholder is not None:
        loading_placeholder.empty()
    return result


def _render_with_delayed_prepare_button(
    runtime: QueryStudioRuntimeServices,
    *,
    can_create: bool,
    delegate: Callable[..., ConfirmedQueryStudioRequest | None],
    execute: bool,
    loading: bool,
) -> ConfirmedQueryStudioRequest | None:
    original_button = cast(Callable[..., bool], st.button)

    def delayed_button(*args: object, **kwargs: object) -> bool:
        if kwargs.get("key") != _PRODUCTION_PREPARE_BUTTON_KEY:
            return original_button(*args, **kwargs)
        if loading:
            kwargs["disabled"] = True
            original_button(*args, **kwargs)
            return False
        clicked = original_button(*args, **kwargs)
        if execute:
            st.session_state.pop(_DELAYED_PHASE_KEY, None)
            return True
        if clicked:
            st.session_state[_DELAYED_PHASE_KEY] = _DELAYED_PHASE_LOADING
            st.rerun()
        return False

    previous_button = st.__dict__["button"]
    try:
        st.__dict__["button"] = delayed_button
        return delegate(runtime, can_create=can_create)
    finally:
        st.__dict__["button"] = previous_button


@dataclass(frozen=True, slots=True)
class FailingQueryStudioIntent:
    """Simulate one sanitized failure at the typed interpretation boundary."""

    delegate: QueryStudioIntentPort
    code: QueryStudioPortErrorCode

    def __post_init__(self) -> None:
        if self.code not in {
            QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
            QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED,
            QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED,
        }:
            raise ValueError("unsupported M27 provider failure scenario")

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        del value
        raise QueryStudioPortError(self.code, "synthetic M27 provider boundary failure")


@dataclass(frozen=True, slots=True)
class DelayedLocalDescriptionExpansion:
    """Delay local deterministic expansion so the browser can observe loading."""

    delegate: DescriptionExpansionPort
    delay_seconds: float
    sleeper: Callable[[float], None]

    def __post_init__(self) -> None:
        if self.delay_seconds != M27_BROWSER_DELAY_SECONDS:
            raise ValueError("M27 browser delay must use the fixed bounded duration")

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        self.sleeper(self.delay_seconds)
        return self.delegate.expand(value)


@dataclass(frozen=True, slots=True)
class ConflictingQueryStudioIntent:
    """Preserve real deterministic usage while returning a valid conflict state."""

    delegate: QueryStudioIntentPort

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        interpreted = self.delegate.interpret(value)
        return QueryStudioInterpretationResult(
            proposal=QueryStudioModelProposal(
                semantic_state=SemanticMatchState.CONFLICTING,
            ),
            usage=interpreted.usage,
        )


@dataclass(frozen=True, slots=True)
class FixedQueryStudioClock:
    """One aware fixed instant used to prove genuine signed-token expiry."""

    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise ValueError("M27 fixed clock must be timezone-aware")

    def now(self) -> datetime:
        return self.value


def _replace_expansion(
    runtime: QueryStudioRuntimeServices,
    expansion: DescriptionExpansionPort,
) -> QueryStudioRuntimeServices:
    assert runtime.prepare_natural is not None
    return replace(
        runtime,
        expansion=expansion,
        prepare_natural=replace(runtime.prepare_natural, expansion=expansion),
    )


def _replace_interpreter(
    runtime: QueryStudioRuntimeServices,
    interpreter: QueryStudioIntentPort,
) -> QueryStudioRuntimeServices:
    assert runtime.prepare_natural is not None
    return replace(
        runtime,
        prepare_natural=replace(runtime.prepare_natural, interpreter=interpreter),
    )


__all__ = [
    "M27_BROWSER_DELAY_SECONDS",
    "M27_BROWSER_RELEASE_REF",
    "M27_BROWSER_SCENARIO_ENV",
    "M27_DELAYED_COMPLETE_BUTTON_KEY",
    "M27_DELAYED_LOADING_TEXT",
    "ConflictingQueryStudioIntent",
    "DelayedLocalDescriptionExpansion",
    "FailingQueryStudioIntent",
    "FixedQueryStudioClock",
    "M27BrowserScenario",
    "build_m27_browser_scenario_runtime",
    "decorate_m27_query_studio_runtime",
    "load_m27_browser_scenario",
    "render_m27_delayed_query_studio",
]
