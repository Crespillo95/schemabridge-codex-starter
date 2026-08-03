"""M30 regressions for target-bound M32 preview, confirmation, and SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from tests.m32_target_support import (
    MutableTargetResolver,
    current_semantic_gate,
    execution_target,
    target_bound_registry,
)

from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.adapters.query_studio.advanced_security import (
    HmacAdvancedQueryPreviewTokens,
)
from schemabridge.adapters.query_studio.advanced_semantic_index import (
    RegistryWideAdvancedSemanticIndex,
)
from schemabridge.adapters.semantic_registry.memory import (
    InMemoryGovernedSemanticRegistry,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.export import PostgresCopyableSqlRenderer
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.natural_sql import (
    ConfirmNaturalSqlPreview,
    GenerateGovernedCopyableSql,
    NaturalSqlError,
    NaturalSqlErrorCode,
    NaturalSqlPreparation,
    PrepareNaturalSqlPreview,
)
from schemabridge.application.ports.connectors import ExecutionTargetResolverPort
from schemabridge.application.query_execution import CompiledQuery
from schemabridge.application.semantic_change import AssertSemanticContextCurrent
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.domain.advanced_plans import RestrictedQueryPlan
from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import GovernedExecutionTarget
from schemabridge.domain.intents import UserLanguage
from schemabridge.domain.resolution import (
    ResolutionLimits,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticChangeKind,
    SemanticChangeStatus,
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
KEY = bytes(range(32))


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> datetime:
        return NOW


@dataclass(frozen=True, slots=True)
class FixedNonce:
    def new_nonce(self) -> str:
        return "nonce_ABCDEFGHIJKLMNOP"


@dataclass(slots=True)
class CountingCompiler:
    calls: int = 0

    def compile(
        self,
        plan: RestrictedQueryPlan,
        *,
        max_preview_rows: int,
        target: GovernedExecutionTarget | None = None,
    ) -> CompiledQuery:
        self.calls += 1
        return PostgresQueryCompiler().compile(
            plan,
            max_preview_rows=max_preview_rows,
            target=target,
        )


@dataclass(slots=True)
class RotatingTargetResolver:
    first: GovernedExecutionTarget
    second: GovernedExecutionTarget
    calls: int = 0

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        assert workspace_id == self.first.workspace_id
        assert connection_id == self.first.connection_id
        self.calls += 1
        return self.first if self.calls == 1 else self.second


@dataclass(slots=True)
class UnexpectedTargetResolver:
    calls: int = 0

    def resolve_current(
        self,
        *,
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> GovernedExecutionTarget:
        del workspace_id, connection_id
        self.calls += 1
        raise RuntimeError("synthetic private resolver detail")


@dataclass(slots=True)
class CountingLanguage:
    delegate: DeterministicAdvancedLanguageAdapter
    extraction_calls: int = 0
    interpretation_calls: int = 0

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        self.extraction_calls += 1
        return self.delegate.extract(value)

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        self.interpretation_calls += 1
        return self.delegate.interpret(value)


@dataclass(slots=True)
class BlockedSemanticGate:
    calls: int = 0

    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        self.calls += 1
        return SemanticContextGateAssessment(
            dependencies_fingerprint=dependencies.fingerprint,
            eligible=False,
            status=SemanticChangeStatus.BLOCKED,
            reason_codes=(SemanticChangeKind.FIELD_REMOVED,),
        )


def _prepare(
    resolver: ExecutionTargetResolverPort,
) -> tuple[
    NaturalSqlPreparation,
    InMemoryGovernedSemanticRegistry,
    HmacAdvancedQueryPreviewTokens,
]:
    registry = target_bound_registry()
    scope = registry.load().scope
    language = DeterministicAdvancedLanguageAdapter()
    tokens = HmacAdvancedQueryPreviewTokens(KEY)
    preparation = PrepareNaturalSqlPreview(
        registry=registry,
        mentions=language,
        retrieval=RegistryWideAdvancedSemanticIndex(),
        interpreter=language,
        preview_tokens=tokens,
        clock=FixedClock(),
        nonces=FixedNonce(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=scope,
    ).execute(
        AdvancedNaturalLanguageInput(
            text=M32_SIMPLE_PRODUCTS_QUESTION_ES,
            language=UserLanguage.SPANISH,
        )
    )
    return preparation, registry, tokens


def _confirmation(preparation: NaturalSqlPreparation) -> AdvancedQueryConfirmation:
    assert preparation.preview is not None
    assert preparation.token is not None
    return AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preparation.request_digest,
        preview_fingerprint=preparation.preview.fingerprint,
        routed_request_fingerprint=preparation.preview.routed_request_fingerprint,
        token=preparation.token,
    )


def test_qsp3_binds_the_same_target_through_copyable_sql() -> None:
    registry = target_bound_registry()
    loaded = registry.load()
    target = execution_target(workspace_id=loaded.scope.workspace_id)
    resolver = MutableTargetResolver(target)
    preparation, registry, tokens = _prepare(resolver)
    preview = preparation.preview
    assert preview is not None
    assert preparation.token is not None

    assert preparation.token.root.startswith("qsp3.")
    assert preview.connection_id == target.connection_id
    assert preview.target_route_revision == target.route_revision
    assert preview.target_fingerprint == target.fingerprint
    assert preview.target_type_contract_fingerprint == target.type_contract_fingerprint

    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=FixedClock(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=registry.load().scope,
    ).execute(preparation, _confirmation(preparation))
    compiler = CountingCompiler()
    result = GenerateGovernedCopyableSql(
        registry=registry,
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
        renderer=PostgresCopyableSqlRenderer(),
        limits=ResolutionLimits(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=registry.load().scope,
    ).execute(confirmed)

    assert compiler.calls == 1
    assert result.target == target
    assert result.resolved_plan.execution_target == target
    assert result.artifact.target_fingerprint == target.fingerprint
    assert result.artifact.plan_fingerprint == resolved_semantic_plan_fingerprint(
        result.resolved_plan
    )
    assert result.as_dict()["connection_id"] == target.connection_id.root
    assert result.as_dict()["target_route_revision"] == target.route_revision
    assert result.as_dict()["executed"] is False
    assert resolver.calls == [
        (target.workspace_id, target.connection_id),
        (target.workspace_id, target.connection_id),
        (target.workspace_id, target.connection_id),
        (target.workspace_id, target.connection_id),
    ]


def test_route_rotation_after_preview_rejects_confirmation_without_sql() -> None:
    registry = target_bound_registry()
    current = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(current)
    preparation, registry, tokens = _prepare(resolver)
    resolver.target = execution_target(
        workspace_id=current.workspace_id,
        route_revision=2,
        marker="rotated",
    )

    with pytest.raises(NaturalSqlError) as captured:
        ConfirmNaturalSqlPreview(
            registry=registry,
            preview_tokens=tokens,
            clock=FixedClock(),
            target_resolver=resolver,
            require_target_binding=True,
            semantic_gate=current_semantic_gate(),
            semantic_scope=registry.load().scope,
        ).execute(preparation, _confirmation(preparation))

    assert captured.value.code is NaturalSqlErrorCode.TARGET_MISMATCH


def test_route_rotation_during_interpretation_never_issues_a_preview() -> None:
    registry = target_bound_registry()
    first = execution_target(workspace_id=registry.load().scope.workspace_id)
    second = execution_target(
        workspace_id=first.workspace_id,
        route_revision=2,
        marker="rotated-during-interpretation",
    )
    resolver = RotatingTargetResolver(first, second)

    with pytest.raises(NaturalSqlError) as captured:
        _prepare(resolver)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_MISMATCH
    assert resolver.calls == 2


def test_route_rotation_after_confirmation_rejects_before_compilation() -> None:
    registry = target_bound_registry()
    current = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(current)
    preparation, registry, tokens = _prepare(resolver)
    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=FixedClock(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=registry.load().scope,
    ).execute(preparation, _confirmation(preparation))
    resolver.target = execution_target(
        workspace_id=current.workspace_id,
        route_revision=2,
        marker="rotated-after-confirmation",
    )
    compiler = CountingCompiler()

    with pytest.raises(NaturalSqlError) as captured:
        GenerateGovernedCopyableSql(
            registry=registry,
            compiler=compiler,
            guard=SqlGlotPolicyGuard(),
            renderer=PostgresCopyableSqlRenderer(),
            limits=ResolutionLimits(),
            target_resolver=resolver,
            require_target_binding=True,
            semantic_gate=current_semantic_gate(),
            semantic_scope=registry.load().scope,
        ).execute(confirmed)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_MISMATCH
    assert compiler.calls == 0


def test_managed_mode_rejects_registry_v1_without_touching_a_resolver() -> None:
    registry = build_semantic_registry().load()
    target = execution_target(workspace_id=registry.scope.workspace_id)
    resolver = MutableTargetResolver(target)
    language = DeterministicAdvancedLanguageAdapter()

    with pytest.raises(NaturalSqlError) as captured:
        PrepareNaturalSqlPreview(
            registry=build_semantic_registry(),
            mentions=language,
            retrieval=RegistryWideAdvancedSemanticIndex(),
            interpreter=language,
            preview_tokens=HmacAdvancedQueryPreviewTokens(KEY),
            clock=FixedClock(),
            nonces=FixedNonce(),
            target_resolver=resolver,
            require_target_binding=True,
            semantic_gate=current_semantic_gate(),
            semantic_scope=build_semantic_registry().load().scope,
        ).execute(
            AdvancedNaturalLanguageInput(
                text=M32_SIMPLE_PRODUCTS_QUESTION_ES,
                language=UserLanguage.SPANISH,
            )
        )

    assert captured.value.code is NaturalSqlErrorCode.TARGET_REQUIRED
    assert resolver.calls == []


def test_cross_connection_target_substitution_fails_before_preview_token() -> None:
    registry = target_bound_registry()
    cross_connection = execution_target(
        workspace_id=registry.load().scope.workspace_id,
        connection_id=CatalogConnectionId("warehouse-secondary"),
    )
    resolver = MutableTargetResolver(cross_connection)

    with pytest.raises(NaturalSqlError) as captured:
        _prepare(resolver)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_MISMATCH


def test_disabled_target_fails_closed_with_sanitized_error() -> None:
    registry = target_bound_registry()
    target = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(
        target,
        error=ConnectorTargetError(
            ConnectorTargetErrorCode.ROUTE_DISABLED,
            "synthetic private route detail",
        ),
    )

    with pytest.raises(NaturalSqlError) as captured:
        _prepare(resolver)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_UNAVAILABLE
    assert "private route detail" not in str(captured.value)


def test_ineligible_m26_gate_blocks_before_provider_or_target_resolution() -> None:
    registry = target_bound_registry()
    target = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(target)
    language = CountingLanguage(DeterministicAdvancedLanguageAdapter())
    gate_port = BlockedSemanticGate()

    with pytest.raises(NaturalSqlError) as captured:
        PrepareNaturalSqlPreview(
            registry=registry,
            mentions=language,
            retrieval=RegistryWideAdvancedSemanticIndex(),
            interpreter=language,
            preview_tokens=HmacAdvancedQueryPreviewTokens(KEY),
            clock=FixedClock(),
            nonces=FixedNonce(),
            target_resolver=resolver,
            require_target_binding=True,
            semantic_gate=AssertSemanticContextCurrent(gate_port),
            semantic_scope=registry.load().scope,
        ).execute(
            AdvancedNaturalLanguageInput(
                text=M32_SIMPLE_PRODUCTS_QUESTION_ES,
                language=UserLanguage.SPANISH,
            )
        )

    assert captured.value.code is NaturalSqlErrorCode.STALE_CONTEXT
    assert gate_port.calls == 1
    assert language.extraction_calls == 0
    assert language.interpretation_calls == 0
    assert resolver.calls == []


def test_unexpected_resolver_failure_is_sanitized_before_provider_access() -> None:
    resolver = UnexpectedTargetResolver()

    with pytest.raises(NaturalSqlError) as captured:
        _prepare(resolver)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_UNAVAILABLE
    assert "private resolver detail" not in str(captured.value)
    assert resolver.calls == 1


def test_route_rotation_after_artifact_purges_on_revalidation_before_compilation() -> None:
    registry = target_bound_registry()
    current = execution_target(workspace_id=registry.load().scope.workspace_id)
    resolver = MutableTargetResolver(current)
    preparation, registry, tokens = _prepare(resolver)
    confirmed = ConfirmNaturalSqlPreview(
        registry=registry,
        preview_tokens=tokens,
        clock=FixedClock(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=registry.load().scope,
    ).execute(preparation, _confirmation(preparation))
    compiler = CountingCompiler()
    generator = GenerateGovernedCopyableSql(
        registry=registry,
        compiler=compiler,
        guard=SqlGlotPolicyGuard(),
        renderer=PostgresCopyableSqlRenderer(),
        limits=ResolutionLimits(),
        target_resolver=resolver,
        require_target_binding=True,
        semantic_gate=current_semantic_gate(),
        semantic_scope=registry.load().scope,
    )
    generator.execute(confirmed)
    resolver.target = execution_target(
        workspace_id=current.workspace_id,
        route_revision=2,
        marker="rotated-after-artifact",
    )

    with pytest.raises(NaturalSqlError) as captured:
        generator.execute(confirmed)

    assert captured.value.code is NaturalSqlErrorCode.TARGET_MISMATCH
    assert compiler.calls == 1
