"""Focused M13 tests for recipe approval, fingerprints, staleness, and reuse."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from schemabridge.adapters.datahub.query_recipes import (
    DataHubQueryRecipeAdapter,
    DataHubQueryRecipeConfig,
    _current_document_urn,
)
from schemabridge.adapters.datahub.recorded import RecordedCatalogAdapter
from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.planning.recorded import RecordedSemanticPlanningContext
from schemabridge.adapters.recipes.sqlite import SqliteQueryRecipeRepository
from schemabridge.adapters.requests.recorded_context import RecordedRequestContextAdapter
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.adapters.storage.workflows import SqliteWorkflowDraftStore
from schemabridge.adapters.workflows.fake import SqliteFakeWorkflowPublisher
from schemabridge.adapters.workflows.system import SystemWorkflowClock
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    PlanSemanticRequest,
    PrepareGovernedRequest,
)
from schemabridge.application.intent_resolution import ResolveNaturalLanguageIntent
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.application.query_execution import QueryPreviewResult, ValidatedQuery
from schemabridge.application.query_recipes import (
    AssessQueryRecipeReuse,
    PrepareQueryRecipe,
    PublishQueryRecipe,
    source_schema_fingerprint,
)
from schemabridge.application.semantic_change import AssertSemanticContextCurrent
from schemabridge.application.workflow_orchestration import AgentWorkflowOrchestrator
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import IntentAlternativeId, UserLanguage
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.recipes import (
    QueryRecipe,
    RecipePublicationApproval,
    RecipePublicationConfirmation,
    RecipePublicationStatus,
    RecipeReuseStatus,
    RecipeStalenessCode,
)
from schemabridge.domain.resolution import (
    RejectedSourceRecord,
    RejectedSourceReport,
    RejectionCheck,
    ResolutionLimits,
    ResolvedSemanticPlan,
    SourceRejectionCode,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.semantic_change import (
    SemanticContextGateAssessment,
    SemanticPlanDependencies,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope
from schemabridge.domain.workflows import (
    AgentWorkflowDraft,
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowOperation,
    WorkflowTraceStatus,
)

ROOT = Path(__file__).resolve().parents[2]
TEXT = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta."
DATASETS = (
    PhysicalDatasetRef("crm.customers"),
    PhysicalDatasetRef("bank.account_holders"),
)


@dataclass(slots=True)
class CountingPreview:
    calls: int = 0

    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        self.calls += 1
        return QueryPreviewResult(
            columns=("registration_date", "secondary_holder_customers"),
            rows=(("2026-01-10", 2), ("2026-01-11", 1), ("2026-01-12", 1)),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=query.statement_timeout_ms,
            truncated=False,
        )


class EmptyRejectionReporter:
    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        assert should_continue is None or should_continue()
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


class TruncatedRejectionReporter:
    def inspect(
        self,
        checks: tuple[RejectionCheck, ...],
        *,
        statement_timeout_ms: int,
        should_continue: Callable[[], bool] | None = None,
    ) -> RejectedSourceReport:
        assert should_continue is None or should_continue()
        assert checks
        records = tuple(
            RejectedSourceRecord(
                logical_field=checks[index % len(checks)].logical_field,
                physical_field=checks[index % len(checks)].physical_field,
                source_value=f"synthetic-private-rejection-{index + 1}",
                code=code,
                reason="Synthetic bounded recipe rejection sample.",
            )
            for index, code in enumerate(
                (
                    SourceRejectionCode.NULL_JOIN_KEY,
                    SourceRejectionCode.NON_FINITE_IDENTIFIER,
                )
            )
        )
        return RejectedSourceReport(
            inspected_fields=tuple(check.physical_field for check in checks),
            records=records,
            total_records=25_001,
            truncated=True,
            database_user="schemabridge_reader",
            transaction_read_only=True,
            statement_timeout_ms=statement_timeout_ms,
        )


class UnavailableSemanticGate:
    def assess(
        self,
        dependencies: SemanticPlanDependencies,
    ) -> SemanticContextGateAssessment:
        raise RuntimeError("synthetic semantic evidence outage")


def _build(
    path: Path,
    *,
    recipes: SqliteQueryRecipeRepository | None = None,
    preview: CountingPreview | None = None,
    reporter: EmptyRejectionReporter | TruncatedRejectionReporter | None = None,
) -> AgentWorkflowOrchestrator:
    logical = ROOT / "demo/ground_truth/approved_logical_context.yml"
    prepare = PrepareGovernedRequest(
        planner=PlanSemanticRequest(
            RecordedSemanticPlanningContext(
                logical,
                ROOT / "demo/ground_truth/planning_mappings.yml",
                ROOT / "demo/ground_truth/join_contracts.yml",
            ),
            ResolutionLimits(),
        ),
        compiler=PostgresQueryCompiler(),
        guard=SqlGlotPolicyGuard(),
    )
    return AgentWorkflowOrchestrator(
        store=SqliteWorkflowDraftStore(path),
        clock=SystemWorkflowClock(),
        catalog=RecordedCatalogAdapter(ROOT / "demo/datahub/catalog_snapshot.json"),
        intent=ResolveNaturalLanguageIntent(
            parser=FakeIntentParser(),
            context=RecordedRequestContextAdapter(logical),
            adapter_label="fake:typed-intent-only",
        ),
        prepare=prepare,
        execute=ExecuteGovernedRequest(
            prepare=prepare,
            executor=preview or CountingPreview(),
            rejection_reporter=reporter or EmptyRejectionReporter(),
        ),
        publisher=SqliteFakeWorkflowPublisher(path),
        audit_store=SqlitePublicationAuditStore(path),
        recipe_assessor=AssessQueryRecipeReuse(recipes) if recipes is not None else None,
    )


def _start(orchestrator: AgentWorkflowOrchestrator, workflow_id: str) -> AgentWorkflowDraft:
    return orchestrator.start(
        StartWorkflowCommand(
            id=workflow_id,
            text=TEXT,
            language=UserLanguage.SPANISH,
            datasets=DATASETS,
        )
    )


def _resolve(
    orchestrator: AgentWorkflowOrchestrator,
    draft: AgentWorkflowDraft,
) -> AgentWorkflowDraft:
    assert draft.intent is not None
    return orchestrator.decide_intent(
        draft.id,
        IntentWorkflowDecision(
            actor="operator@example.test",
            interpretation_fingerprint=draft.intent.interpretation_fingerprint,
            selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
        ),
    )


def _execute(
    orchestrator: AgentWorkflowOrchestrator,
    draft: AgentWorkflowDraft,
) -> AgentWorkflowDraft:
    assert draft.plan_fingerprint is not None
    return orchestrator.decide_execution(
        draft.id,
        ExecutionWorkflowDecision(
            actor="operator@example.test",
            plan_fingerprint=draft.plan_fingerprint,
            action=WorkflowDecisionAction.APPROVE,
        ),
    )


def _approval(recipe: QueryRecipe) -> RecipePublicationApproval:
    return RecipePublicationApproval(
        id=f"recipe-approval-{recipe.version}",
        recipe_id=recipe.id,
        recipe_version=recipe.version,
        recipe_fingerprint=recipe.fingerprint,
        actor="operator@example.test",
        approved_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        confirmation=RecipePublicationConfirmation.PUBLISH_VALIDATED_QUERY_RECIPE,
    )


def test_datahub_recipe_current_identity_is_namespaced_by_tenant_scope() -> None:
    intent = "a" * 64

    first = _current_document_urn(intent, scope_fingerprint="b" * 64)
    second = _current_document_urn(intent, scope_fingerprint="c" * 64)

    assert first != second
    assert "b" * 32 in first
    assert "c" * 32 in second


def test_recipe_requires_completed_execution_and_exact_explicit_approval(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    paused = _start(orchestrator, "recipe-before-execution")

    with pytest.raises(RecipeError) as incomplete:
        PrepareQueryRecipe(repository).execute(paused)
    assert incomplete.value.code is RecipeErrorCode.INVALID_WORKFLOW

    completed = _execute(orchestrator, _resolve(orchestrator, paused))
    recipe = PrepareQueryRecipe(repository).execute(completed)
    assert completed.validated_request is not None
    assert recipe.normalized_intent == completed.validated_request.request
    assert recipe.mapping_versions
    assert recipe.join_versions
    assert recipe.validation.transaction_read_only is True
    assert "sql" not in recipe.__class__.model_fields

    wrong = _approval(recipe).model_copy(update={"recipe_fingerprint": "0" * 64})
    with pytest.raises(RecipeError) as mismatch:
        PublishQueryRecipe(repository, SqlitePublicationAuditStore(database)).execute(recipe, wrong)
    assert mismatch.value.code is RecipeErrorCode.APPROVAL_MISMATCH


def test_truncated_workflow_rejections_produce_one_valid_exact_total_recipe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "truncated-recipe.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database, reporter=TruncatedRejectionReporter())
    completed = _execute(
        orchestrator,
        _resolve(
            orchestrator,
            _start(orchestrator, "recipe-truncated-rejections"),
        ),
    )

    recipe = PrepareQueryRecipe(repository).execute(completed)

    assert recipe.validation.rejected_count == 25_001
    assert recipe.validation.rejection_codes == (
        "null_join_key",
        "non_finite_identifier",
    )
    assert recipe.validation.sampled_rejected_count == 2
    assert recipe.validation.unclassified_rejection_count == 24_999
    assert recipe.validation.rejection_counts_complete is False
    assert recipe.validation.rejection_truncated is True
    assert recipe.validation.truncated is True
    assert QueryRecipe.model_validate_json(recipe.model_dump_json()) == recipe
    assert "synthetic-private-rejection" not in recipe.model_dump_json()


def test_recipe_round_trip_publication_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-published")),
    )
    recipe = PrepareQueryRecipe(repository).execute(completed)
    assert QueryRecipe.model_validate_json(recipe.model_dump_json()) == recipe

    publisher = PublishQueryRecipe(repository, SqlitePublicationAuditStore(database))
    approval = _approval(recipe)
    first = publisher.execute(recipe, approval)
    second = publisher.execute(recipe, approval)
    assert first.status is RecipePublicationStatus.CREATED
    assert second.status is RecipePublicationStatus.ALREADY_CURRENT

    restarted = SqliteQueryRecipeRepository(database)
    published = restarted.find_current(recipe.intent_fingerprint)
    assert published is not None
    assert published.recipe == recipe
    assert published.versioned_document_urn.startswith("fake://query-recipe/")
    records = SqlitePublicationAuditStore(database).list_for_approval(approval.id)
    assert len(records) == 4
    assert {record.actor for record in records} == {approval.actor}
    assert {record.new_fingerprint for record in records} == {recipe.fingerprint}


def test_datahub_recipe_current_marker_requires_its_versioned_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "recipes.db"
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-version-required")),
    )
    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(database)).execute(completed)
    versioned_urn = f"urn:li:document:schemabridge-query-recipe-{recipe.intent_fingerprint[:32]}-v{recipe.version}"
    current_document = SimpleNamespace(
        customProperties={
            "schemabridge.queryRecipe": recipe.model_dump_json(),
            "schemabridge.recipeFingerprint": recipe.fingerprint,
            "schemabridge.approvalId": _approval(recipe).id,
            "schemabridge.publishedAt": _approval(recipe).approved_at.isoformat(),
            "schemabridge.versionedDocumentUrn": versioned_urn,
        },
        relatedAssets=tuple(SimpleNamespace(asset=urn) for urn in recipe.linked_asset_urns),
    )

    class MissingVersionGraph:
        def get_aspect(self, urn: str, _aspect: object) -> object | None:
            return current_document if urn.endswith("-current") else None

    writer = DataHubQueryRecipeAdapter(
        DataHubQueryRecipeConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=MissingVersionGraph()))

    with pytest.raises(RecipeError) as raised:
        writer.publish(recipe, _approval(recipe))

    assert raised.value.code is RecipeErrorCode.INVALID_RECORDED_RECIPE


def test_datahub_recipe_rejects_different_content_at_immutable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_database = tmp_path / "first.db"
    first_orchestrator = _build(first_database)
    first_completed = _execute(
        first_orchestrator,
        _resolve(first_orchestrator, _start(first_orchestrator, "recipe-version-first")),
    )
    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(first_database)).execute(
        first_completed
    )

    second_database = tmp_path / "second.db"
    second_orchestrator = _build(second_database)
    second_completed = _execute(
        second_orchestrator,
        _resolve(second_orchestrator, _start(second_orchestrator, "recipe-version-second")),
    )
    conflicting_recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(second_database)).execute(
        second_completed
    )
    assert conflicting_recipe.intent_fingerprint == recipe.intent_fingerprint
    assert conflicting_recipe.version == recipe.version
    assert conflicting_recipe.fingerprint != recipe.fingerprint

    version_document = SimpleNamespace(
        customProperties={
            "schemabridge.queryRecipe": conflicting_recipe.model_dump_json(),
            "schemabridge.recipeFingerprint": conflicting_recipe.fingerprint,
        },
        relatedAssets=tuple(
            SimpleNamespace(asset=urn) for urn in conflicting_recipe.linked_asset_urns
        ),
    )

    class ConflictingVersionGraph:
        def get_aspect(self, urn: str, _aspect: object) -> object | None:
            return None if urn.endswith("-current") else version_document

    writer = DataHubQueryRecipeAdapter(
        DataHubQueryRecipeConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(
        writer,
        "_client",
        lambda: SimpleNamespace(_graph=ConflictingVersionGraph()),
    )

    with pytest.raises(RecipeError) as raised:
        writer.publish(recipe, _approval(recipe))

    assert raised.value.code is RecipeErrorCode.VERSION_CONFLICT


def test_datahub_recipe_requires_version_post_write_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "recipe-readback.db"
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-version-readback")),
    )
    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(database)).execute(completed)
    writer = DataHubQueryRecipeAdapter(
        DataHubQueryRecipeConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(
        writer,
        "find_current",
        lambda _intent, **_kwargs: None,
    )
    monkeypatch.setattr(writer, "_client", object)
    monkeypatch.setattr(writer, "_version_fingerprint", lambda _client, _target: None)
    monkeypatch.setattr(writer, "_upsert_document", lambda *_args, **_kwargs: None)

    result = writer.publish(recipe, _approval(recipe))

    assert result.status is RecipePublicationStatus.PARTIAL_FAILURE
    by_operation = {record.operation: record for record in result.audit_records}
    assert by_operation["versioned_document"].outcome is PublicationAuditOutcome.FAILED
    assert by_operation["current_marker"].outcome is PublicationAuditOutcome.NOT_ATTEMPTED


def test_datahub_recipe_readback_error_after_current_write_is_auditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "recipe-current-readback.db"
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-current-readback")),
    )
    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(database)).execute(completed)
    writer = DataHubQueryRecipeAdapter(
        DataHubQueryRecipeConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    current_reads = 0

    def find_current(_intent: str, **_kwargs: object) -> object | None:
        nonlocal current_reads
        current_reads += 1
        if current_reads == 1:
            return None
        raise RecipeError(RecipeErrorCode.CATALOG_UNAVAILABLE, "read-back unavailable")

    version_reads = iter((None, recipe.fingerprint))
    monkeypatch.setattr(writer, "find_current", find_current)
    monkeypatch.setattr(writer, "_client", object)
    monkeypatch.setattr(
        writer,
        "_version_fingerprint",
        lambda _client, _target: next(version_reads),
    )
    monkeypatch.setattr(writer, "_upsert_document", lambda *_args, **_kwargs: None)

    result = writer.publish(recipe, _approval(recipe))

    assert result.status is RecipePublicationStatus.PARTIAL_FAILURE
    by_operation = {record.operation: record for record in result.audit_records}
    assert by_operation["versioned_document"].outcome is PublicationAuditOutcome.SUCCEEDED
    assert by_operation["current_marker"].outcome is PublicationAuditOutcome.FAILED


def test_datahub_recipe_rejects_tampered_embedded_target_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "recipe-tampered-audit.db"
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-tampered-audit")),
    )
    recipe = PrepareQueryRecipe(SqliteQueryRecipeRepository(database)).execute(completed)
    approval = _approval(recipe)
    current_urn = (
        f"urn:li:document:schemabridge-query-recipe-{recipe.intent_fingerprint[:32]}-current"
    )
    versioned_urn = f"urn:li:document:schemabridge-query-recipe-{recipe.intent_fingerprint[:32]}-v{recipe.version}"
    tampered_audits = (
        PublicationTargetAuditRecord(
            family=PublicationFamily.JOIN,
            operation="versioned_document",
            target=versioned_urn,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            new_fingerprint=recipe.fingerprint,
            outcome=PublicationAuditOutcome.SUCCEEDED,
        ),
        PublicationTargetAuditRecord(
            family=PublicationFamily.RECIPE,
            operation="current_marker",
            target=current_urn,
            approval_id=approval.id,
            actor=approval.actor,
            approved_at=approval.approved_at,
            new_fingerprint=recipe.fingerprint,
            outcome=PublicationAuditOutcome.SUCCEEDED,
        ),
    )
    assets = tuple(SimpleNamespace(asset=urn) for urn in recipe.linked_asset_urns)
    current_document = SimpleNamespace(
        customProperties={
            "schemabridge.queryRecipe": recipe.model_dump_json(),
            "schemabridge.recipeFingerprint": recipe.fingerprint,
            "schemabridge.approvalId": approval.id,
            "schemabridge.approvedBy": approval.actor,
            "schemabridge.publishedAt": approval.approved_at.isoformat(),
            "schemabridge.versionedDocumentUrn": versioned_urn,
            "schemabridge.publicationAudit": json.dumps(
                [record.model_dump(mode="json") for record in tampered_audits]
            ),
        },
        relatedAssets=assets,
    )
    versioned_document = SimpleNamespace(
        customProperties={
            "schemabridge.queryRecipe": recipe.model_dump_json(),
            "schemabridge.recipeFingerprint": recipe.fingerprint,
        },
        relatedAssets=assets,
    )

    class TamperedAuditGraph:
        def get_aspect(self, urn: str, _aspect: object) -> object:
            return current_document if urn == current_urn else versioned_document

    writer = DataHubQueryRecipeAdapter(
        DataHubQueryRecipeConfig(
            server="http://datahub.invalid",
            token="not-logged",
            actor_urn="urn:li:corpuser:schemabridge",
        )
    )
    monkeypatch.setattr(writer, "_verify_runtime_identity", lambda: None)
    monkeypatch.setattr(writer, "_client", lambda: SimpleNamespace(_graph=TamperedAuditGraph()))

    with pytest.raises(RecipeError) as raised:
        writer.find_current(recipe.intent_fingerprint)

    assert raised.value.code is RecipeErrorCode.INVALID_RECORDED_RECIPE


def test_recipe_reuse_is_compatible_only_for_current_context(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-current-context")),
    )
    recipe = PrepareQueryRecipe(repository).execute(completed)
    PublishQueryRecipe(repository, SqlitePublicationAuditStore(database)).execute(
        recipe, _approval(recipe)
    )
    assert completed.validated_request is not None
    assert completed.resolved_plan is not None
    assert completed.plan_fingerprint is not None
    assert completed.query_fingerprint is not None

    assessor = AssessQueryRecipeReuse(SqliteQueryRecipeRepository(database))
    reusable = assessor.execute(
        validated_request=completed.validated_request,
        resolved_plan=completed.resolved_plan,
        plan_fingerprint=completed.plan_fingerprint,
        query_fingerprint=completed.query_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint(completed),
    )
    assert reusable.status is RecipeReuseStatus.REUSABLE
    assert reusable.provenance_document_urn is not None
    assert reusable.revalidated is True

    payload = completed.resolved_plan.model_dump(mode="python")
    payload["selected_mappings"][0]["mapping"]["version"] += 1
    changed = ResolvedSemanticPlan.model_validate(payload)
    stale = assessor.execute(
        validated_request=completed.validated_request,
        resolved_plan=changed,
        plan_fingerprint=resolved_semantic_plan_fingerprint(changed),
        query_fingerprint=completed.query_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint(completed),
    )
    assert stale.status is RecipeReuseStatus.STALE
    assert RecipeStalenessCode.MAPPING_VERSION_CHANGED in stale.reasons
    assert RecipeStalenessCode.PLAN_CHANGED in stale.reasons


def test_recipe_reuse_marks_semantic_evidence_unavailability_distinctly_stale(
    tmp_path: Path,
) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    orchestrator = _build(database)
    completed = _execute(
        orchestrator,
        _resolve(orchestrator, _start(orchestrator, "recipe-semantic-evidence")),
    )
    recipe = PrepareQueryRecipe(repository).execute(completed)
    PublishQueryRecipe(repository, SqlitePublicationAuditStore(database)).execute(
        recipe, _approval(recipe)
    )
    assert completed.validated_request is not None
    assert completed.resolved_plan is not None
    assert completed.query_fingerprint is not None
    active_plan = completed.resolved_plan.model_copy(
        update={
            "activation_generation": 9,
            "active_pointer_fingerprint": "b" * 64,
        }
    )

    assessment = AssessQueryRecipeReuse(
        repository,
        semantic_gate=AssertSemanticContextCurrent(UnavailableSemanticGate()),
        semantic_scope=SemanticRegistryScope(
            workspace_id="recipe-tests",
            catalog_scope="synthetic-demo",
            registry_id="legacy_m10",
        ),
    ).execute(
        validated_request=completed.validated_request,
        resolved_plan=active_plan,
        plan_fingerprint=resolved_semantic_plan_fingerprint(active_plan),
        query_fingerprint=completed.query_fingerprint,
        source_schema_fingerprint=source_schema_fingerprint(completed),
    )

    assert assessment.status is RecipeReuseStatus.STALE
    assert RecipeStalenessCode.SEMANTIC_EVIDENCE_CHANGED in assessment.reasons
    assert assessment.recipe_fingerprint == recipe.fingerprint
    assert assessment.provenance_document_urn is not None


def test_new_workflow_reuses_provenance_but_revalidates_and_executes_once(tmp_path: Path) -> None:
    database = tmp_path / "recipes.db"
    repository = SqliteQueryRecipeRepository(database)
    first = _build(database)
    completed = _execute(first, _resolve(first, _start(first, "recipe-first-process")))
    recipe = PrepareQueryRecipe(repository).execute(completed)
    PublishQueryRecipe(repository, SqlitePublicationAuditStore(database)).execute(
        recipe, _approval(recipe)
    )

    preview = CountingPreview()
    restarted = _build(
        database,
        recipes=SqliteQueryRecipeRepository(database),
        preview=preview,
    )
    resolved = _resolve(restarted, _start(restarted, "recipe-second-process"))
    assert resolved.recipe_reuse is not None
    assert resolved.recipe_reuse.status is RecipeReuseStatus.REUSABLE
    assert resolved.recipe_reuse.recipe_fingerprint == recipe.fingerprint
    assert any(
        event.operation is WorkflowOperation.QUERY_RECIPE_LOOKUP
        and event.status is WorkflowTraceStatus.SUCCEEDED
        for event in resolved.trace
    )
    executed = _execute(restarted, resolved)
    assert executed.execution is not None
    assert preview.calls == 1
    assert (
        sum(
            event.operation is WorkflowOperation.SQL_VALIDATION
            and event.status is WorkflowTraceStatus.SUCCEEDED
            for event in executed.trace
        )
        == 2
    )


def test_recipe_application_has_no_concrete_adapter_imports() -> None:
    source = (ROOT / "src/schemabridge/application/query_recipes.py").read_text(encoding="utf-8")

    assert "schemabridge.adapters" not in source
    assert "datahub" not in source.casefold()
    assert "sqlite" not in source.casefold()


def test_generated_synthetic_recipe_artifact_is_a_valid_sql_free_round_trip() -> None:
    payload = yaml.safe_load((ROOT / "examples/query-recipe-secondary-holders.yml").read_text())
    recipe = QueryRecipe.model_validate(payload)

    assert recipe.validation.policy_status == "accepted"
    assert recipe.validation.row_count == 3
    assert set(recipe.model_dump()) == set(QueryRecipe.model_fields)
    assert "sql" not in recipe.__class__.model_fields
