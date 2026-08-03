"""M32 composition-root capability and admission boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.m32_target_support import (
    MutableTargetResolver,
    current_semantic_gate,
    execution_target,
    target_bound_registry,
)

import schemabridge.adapters.control_plane.postgres_query_studio_ai as ai_control_module
import schemabridge.adapters.language.openai_advanced_query_studio as openai_advanced_module
import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.language.openai_advanced_query_studio import (
    openai_advanced_interpretation_input_token_reservation_bound,
    openai_advanced_mention_input_token_reservation_bound,
)
from schemabridge.adapters.language.openai_boundary import OpenAIRegion, OpenAIResponsesConfig
from schemabridge.adapters.query_studio.advanced_fake_language import (
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedInterpretationPort,
    AdvancedMentionExtractionPort,
)
from schemabridge.application.query_studio_ai_admission import (
    AdmittedAdvancedInterpretation,
    AdmittedAdvancedMentionExtraction,
)
from schemabridge.bootstrap import NaturalSqlRuntimeServices, build_natural_sql_runtime
from schemabridge.config import Settings
from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
)
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.query_studio import ProviderConfigurationFacts

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _synthetic_secret(label: str) -> str:
    return hashlib.sha256(f"m32-bootstrap-test:{label}".encode()).hexdigest()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="m32-bootstrap-analyst",
        workspace_id="m32-bootstrap",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _settings(mode: str) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "OPENAI_API_KEY": None,
        "DATABASE_URL": None,
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE": mode,
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": _synthetic_secret("preview-signing"),
        "SCHEMABRIDGE_MAX_QUERY_TABLES": 2,
        "SCHEMABRIDGE_MAX_QUERY_ROWS": 321,
        "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 4_321,
    }
    if mode == "live":
        values.update(
            {
                "OPENAI_API_KEY": _synthetic_secret("provider-placeholder"),
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_DATABASE_URL": (
                    "postgresql://runtime:synthetic@control.example.test/control"
                ),
                "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": _synthetic_secret("control-audit"),
                "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": _synthetic_secret("identity-migration"),
                "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": _synthetic_secret("pseudonymization"),
            }
        )
    return Settings.model_validate(values)


@dataclass(frozen=True, slots=True)
class _RawMentionAdapter:
    configuration: ProviderConfigurationFacts

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        del value
        raise AssertionError("composition test must not call the provider")


@dataclass(frozen=True, slots=True)
class _RawInterpretationAdapter:
    configuration: ProviderConfigurationFacts

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        del value
        raise AssertionError("composition test must not call the provider")


@dataclass(frozen=True, slots=True)
class _SyntheticAiControl:
    dsn: str
    schema: str


def _configuration() -> ProviderConfigurationFacts:
    return ProviderConfigurationFacts.create(
        adapter="openai_responses_advanced",
        model_snapshot="gpt-5-nano-2025-08-07",
        reasoning_effort="minimal",
        endpoint_region="global",
        prompt_version="m32-openai-v1",
        schema_version="m32-advanced-v1",
        matcher_version="m27-governed-description-matcher-v1",
        attempt_policy_version="m27-durable-attempts-v3",
        external_ai=True,
    )


def test_disabled_mode_rejects_injected_language_ports() -> None:
    language = DeterministicAdvancedLanguageAdapter()

    with pytest.raises(ValueError, match="natural SQL interpretation is disabled"):
        build_natural_sql_runtime(
            principal=_principal(),
            repository_root=ROOT,
            settings=_settings("disabled"),
            mentions=language,
            interpreter=language,
        )


def test_live_mode_rejects_injected_language_ports_before_external_composition() -> None:
    language = DeterministicAdvancedLanguageAdapter()

    with pytest.raises(ValueError, match="limited to the local fake seam"):
        build_natural_sql_runtime(
            principal=_principal(),
            repository_root=ROOT,
            settings=_settings("live"),
            mentions=language,
            interpreter=language,
        )


def test_fake_runtime_shares_resolution_limits_and_has_no_executor_capability() -> None:
    runtime = build_natural_sql_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=_settings("fake"),
    )

    assert set(NaturalSqlRuntimeServices.__dataclass_fields__) == {
        "scope",
        "ai_mode",
        "prepare",
        "confirm",
        "generate",
    }
    assert not hasattr(runtime, "executor")
    assert not hasattr(runtime, "execute")
    assert runtime.prepare.registry is runtime.confirm.registry
    assert runtime.prepare.registry is runtime.generate.registry
    assert runtime.prepare.preview_tokens is runtime.confirm.preview_tokens
    assert runtime.prepare.target_resolver is runtime.confirm.target_resolver
    assert runtime.prepare.target_resolver is runtime.generate.target_resolver
    assert runtime.prepare.require_target_binding is False
    assert runtime.prepare.semantic_gate is None
    assert runtime.confirm.semantic_gate is None
    assert runtime.generate.semantic_gate is None
    assert runtime.prepare.limits is runtime.generate.limits
    assert runtime.prepare.limits is runtime.confirm.limits
    assert runtime.prepare.limits.max_tables == 2
    assert runtime.prepare.limits.max_preview_rows == 321
    assert runtime.prepare.limits.statement_timeout_ms == 4_321


def test_explicit_target_resolver_is_shared_and_makes_binding_mandatory() -> None:
    registry = target_bound_registry()
    target = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(target)
    semantic_gate = current_semantic_gate()

    runtime = build_natural_sql_runtime(
        principal=_principal(),
        repository_root=ROOT,
        settings=_settings("fake"),
        registry=registry,
        target_resolver=resolver,
        semantic_gate=semantic_gate,
    )

    assert runtime.prepare.target_resolver is resolver
    assert runtime.confirm.target_resolver is resolver
    assert runtime.generate.target_resolver is resolver
    assert runtime.prepare.semantic_gate is semantic_gate
    assert runtime.confirm.semantic_gate is semantic_gate
    assert runtime.generate.semantic_gate is semantic_gate
    assert runtime.prepare.require_target_binding is True
    assert runtime.confirm.require_target_binding is True
    assert runtime.generate.require_target_binding is True


def test_explicit_target_resolver_without_m26_gate_is_rejected() -> None:
    registry = target_bound_registry()
    target = execution_target(workspace_id=registry.load().scope.workspace_id)

    with pytest.raises(ValueError, match="requires the M26 semantic gate"):
        build_natural_sql_runtime(
            principal=_principal(),
            repository_root=ROOT,
            settings=_settings("fake"),
            registry=registry,
            target_resolver=MutableTargetResolver(target),
        )


def test_live_runtime_wraps_both_raw_stages_in_shared_durable_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal()
    live_settings = _settings("live")
    fake_settings = _settings("fake")
    semantic_registry = bootstrap_module.build_semantic_registry(
        repository_root=ROOT,
        settings=fake_settings,
        workspace_id=principal.workspace_id,
    )
    configuration = _configuration()
    raw_mentions = _RawMentionAdapter(configuration)
    raw_interpreter = _RawInterpretationAdapter(configuration)
    schema_checks: list[str] = []

    def build_recorded_registry(**_kwargs: object) -> object:
        return semantic_registry

    def create_raw_adapters(
        _config: OpenAIResponsesConfig,
        **_kwargs: object,
    ) -> tuple[_RawMentionAdapter, _RawInterpretationAdapter]:
        return raw_mentions, raw_interpreter

    monkeypatch.setattr(
        bootstrap_module,
        "build_semantic_registry",
        build_recorded_registry,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **kwargs: schema_checks.append(str(kwargs["credential_kind"])),
    )
    monkeypatch.setattr(
        openai_advanced_module,
        "create_openai_advanced_query_studio_adapters_from_environment",
        create_raw_adapters,
    )
    monkeypatch.setattr(
        ai_control_module,
        "PostgresQueryStudioAiControl",
        _SyntheticAiControl,
    )

    runtime = build_natural_sql_runtime(
        principal=principal,
        repository_root=ROOT,
        settings=live_settings,
    )

    assert runtime.ai_mode == "live"
    assert schema_checks == ["runtime"]
    assert isinstance(runtime.prepare.mentions, AdmittedAdvancedMentionExtraction)
    assert isinstance(runtime.prepare.interpreter, AdmittedAdvancedInterpretation)
    admitted_mentions = runtime.prepare.mentions
    admitted_interpreter = runtime.prepare.interpreter
    assert admitted_mentions.delegate is raw_mentions
    assert admitted_interpreter.delegate is raw_interpreter
    assert admitted_mentions.control is admitted_interpreter.control
    assert admitted_mentions.nonces is admitted_interpreter.nonces
    assert admitted_mentions.nonces is runtime.prepare.nonces
    assert admitted_mentions.configuration is configuration
    assert admitted_interpreter.configuration is configuration
    assert admitted_mentions.workspace_id == principal.workspace_id
    assert admitted_interpreter.workspace_id == principal.workspace_id
    assert admitted_mentions.actor_digest == admitted_interpreter.actor_digest
    assert (
        admitted_mentions.semantic_scope_fingerprint
        == admitted_interpreter.semantic_scope_fingerprint
    )
    assert admitted_mentions.estimated_input_tokens == (
        openai_advanced_mention_input_token_reservation_bound()
    )
    assert admitted_interpreter.estimated_input_tokens == (
        openai_advanced_interpretation_input_token_reservation_bound()
    )
    managed = OpenAIResponsesConfig.for_model(
        live_settings.query_studio_ai_model,
        region=OpenAIRegion(live_settings.query_studio_ai_region),
    )
    assert admitted_mentions.estimated_output_tokens == managed.expansion_max_output_tokens
    assert admitted_interpreter.estimated_output_tokens == managed.interpretation_max_output_tokens
    assert runtime.prepare.limits is runtime.generate.limits
    assert not hasattr(runtime, "executor")

    mention_port: AdvancedMentionExtractionPort = admitted_mentions
    interpretation_port: AdvancedInterpretationPort = admitted_interpreter
    assert mention_port is runtime.prepare.mentions
    assert interpretation_port is runtime.prepare.interpreter
