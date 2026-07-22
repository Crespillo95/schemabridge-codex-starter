"""SchemaBridge command-line interface."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer
from rich.console import Console
from rich.table import Table

from schemabridge import __version__
from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.application.candidate_engine import CandidateGenerationError
from schemabridge.application.doctor import run_doctor
from schemabridge.application.guided_requests import (
    GuidedRequestCase,
    GuidedRequestValidationError,
    build_demo_guided_input,
)
from schemabridge.application.intent_resolution import IntentConfirmationError
from schemabridge.application.join_demo import (
    build_join_review_draft,
    build_north_star_join_proposals,
)
from schemabridge.application.normalization_demo import run_normalization_demo
from schemabridge.application.ports.catalog import CatalogReadError
from schemabridge.application.ports.evaluation import EvaluationError
from schemabridge.application.ports.intents import IntentParserError
from schemabridge.application.ports.planning import PlanningPortError
from schemabridge.application.ports.recipes import RecipeError
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.requests import RequestWorkflowError
from schemabridge.application.ports.reviews import ReviewErrorCode, ReviewWorkflowError
from schemabridge.application.ports.workflows import WorkflowError, WorkflowErrorCode
from schemabridge.application.postgres_health import (
    DatabaseConfigurationError,
    DatabaseHealthError,
)
from schemabridge.application.query_demo import (
    build_demo_query_policy,
    build_north_star_query_plan,
    run_guard_demo,
)
from schemabridge.application.query_execution import (
    QueryCompilationError,
    QueryPreviewError,
    SqlPolicyViolation,
)
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.bootstrap import (
    build_agent_workflow_orchestrator,
    build_candidate_evaluator,
    build_candidate_generator,
    build_catalog_inspector,
    build_evaluation_report_writer,
    build_evaluation_runner,
    build_governed_request_executor,
    build_governed_request_preparer,
    build_guided_request_builder,
    build_guided_request_submitter,
    build_join_context_loader,
    build_join_discoverer,
    build_join_publication_preparer,
    build_join_publisher,
    build_join_review_decider,
    build_join_review_inspector,
    build_join_review_start,
    build_natural_language_intent_resolver,
    build_postgres_health_check,
    build_publication_preparer,
    build_published_context_reader,
    build_query_preparer,
    build_query_previewer,
    build_query_recipe_preparer,
    build_query_recipe_publisher,
    build_request_draft_loader,
    build_request_draft_saver,
    build_review_decider,
    build_review_editor,
    build_review_inspector,
    build_review_publisher,
    build_review_start,
    build_semantic_request_planner,
    build_sql_guard,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.intents import (
    IntentAlternativeId,
    IntentConfirmation,
    UserLanguage,
)
from schemabridge.domain.join_reviews import (
    JoinPublicationApproval,
    JoinPublicationConfirmation,
)
from schemabridge.domain.recipes import (
    RecipePublicationApproval,
    RecipePublicationConfirmation,
)
from schemabridge.domain.request_context import validated_analytical_request_fingerprint
from schemabridge.domain.resolution import (
    SemanticResolutionError,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.reviews import (
    ModelDescriptionEdit,
    PublicationApproval,
    PublicationConfirmation,
)
from schemabridge.domain.validation import (
    ValidationFinding,
    ValidationResult,
    ValidationSeverity,
)
from schemabridge.domain.workflows import (
    ExecutionWorkflowDecision,
    IntentWorkflowDecision,
    PublicationWorkflowDecision,
    RetryWorkflowDecision,
    StartWorkflowCommand,
    WorkflowDecisionAction,
    WorkflowPublicationConfirmation,
)


class CatalogAdapterChoice(StrEnum):
    """Explicit catalog source; live failures never fall back to recorded metadata."""

    LIVE = "live"
    RECORDED = "recorded"


class WriteAdapterChoice(StrEnum):
    """Live DataHub writes and inert fake writes are always visibly distinct."""

    LIVE = "live"
    FAKE = "fake"


class JoinDecisionChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class IntentAdapterChoice(StrEnum):
    FAKE = "fake"
    LIVE = "live"


class WorkflowActionChoice(StrEnum):
    START = "start"
    SHOW = "show"
    CONFIRM_INTENT = "confirm-intent"
    APPROVE_EXECUTION = "approve-execution"
    DECLINE_EXECUTION = "decline-execution"
    RETRY = "retry"
    PUBLISH = "publish"
    SKIP_PUBLICATION = "skip-publication"


app = typer.Typer(
    name="schemabridge",
    help="Governed semantic query agent built on DataHub.",
    no_args_is_help=True,
)
console = Console()


@app.command("review-init")
def review_init(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create the deterministic Customer review locally without catalog writes."""

    try:
        draft = build_review_start().execute(build_customer_review_draft())
    except ReviewWorkflowError as error:
        _review_failure(error, json_output)
    payload = {"ok": True, "draft": draft.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Review {draft.id} initialized at revision {draft.revision}.")


@app.command("review-show")
def review_show(
    draft_id: Annotated[str, typer.Argument()] = "customer-canonical",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect the local draft, immutable decisions, and publication attempts."""

    try:
        snapshot = build_review_inspector().execute(draft_id)
    except ReviewWorkflowError as error:
        _review_failure(error, json_output)
    payload = {
        "ok": True,
        "draft": snapshot.draft.model_dump(mode="json"),
        "decisions": [decision.model_dump(mode="json") for decision in snapshot.decisions],
        "publications": [result.model_dump(mode="json") for result in snapshot.publications],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(
        f"Review {snapshot.draft.id}: revision={snapshot.draft.revision}; "
        f"status={snapshot.draft.logical_model.status.value}"
    )
    table = Table(title="Canonical mapping review")
    table.add_column("Target")
    table.add_column("Confidence")
    table.add_column("Status")
    for item in snapshot.draft.mappings:
        mapping = item.mapping
        table.add_row(
            f"{mapping.physical_field.root} -> {mapping.logical_field.root}",
            f"{mapping.confidence.root:.3f}",
            mapping.status.value,
        )
    console.print(table)


@app.command("review-decide")
def review_decide(
    target: Annotated[str, typer.Argument(help="Exact physical->logical mapping target.")],
    action: Annotated[
        DecisionAction,
        typer.Option("--action", help="Explicit approve, reject, or mark-different action."),
    ],
    revision: Annotated[int, typer.Option("--revision", min=1)],
    actor: Annotated[str, typer.Option("--actor")],
    rationale: Annotated[str, typer.Option("--rationale")],
    draft_id: Annotated[str, typer.Option("--draft-id")] = "customer-canonical",
    different_concept: Annotated[str | None, typer.Option("--different-concept")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Record one explicit mapping decision; confidence cannot invoke this command."""

    from schemabridge.domain.concepts import LogicalFieldRef

    try:
        snapshot = build_review_decider().execute(
            draft_id,
            target,
            action,
            expected_revision=revision,
            actor=actor,
            decided_at=datetime.now(UTC),
            rationale=rationale,
            different_concept=(
                LogicalFieldRef(different_concept) if different_concept is not None else None
            ),
        )
    except (ReviewWorkflowError, ValueError) as error:
        if isinstance(error, ReviewWorkflowError):
            _review_failure(error, json_output)
        _review_failure(
            ReviewWorkflowError(
                code=ReviewErrorCode.INVALID_TRANSITION,
                message="different concept must be a model-qualified inert field",
            ),
            json_output,
        )
    payload = {
        "ok": True,
        "revision": snapshot.draft.revision,
        "decision": snapshot.decisions[-1].model_dump(mode="json"),
        "status": snapshot.draft.logical_model.status.value,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Decision recorded at revision {snapshot.draft.revision}; "
            f"review status={snapshot.draft.logical_model.status.value}."
        )


@app.command("review-publish")
def review_publish(
    actor: Annotated[str, typer.Option("--actor")],
    confirmation: Annotated[
        PublicationConfirmation,
        typer.Option("--confirm", help="Exact approval phrase for this payload."),
    ],
    adapter: Annotated[WriteAdapterChoice, typer.Option("--adapter")] = WriteAdapterChoice.LIVE,
    draft_id: Annotated[str, typer.Option("--draft-id")] = "customer-canonical",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Publish only the currently approved, fingerprinted payload to the selected adapter."""

    try:
        publication = build_publication_preparer().execute(draft_id)
        approval = PublicationApproval(
            id=f"{actor}-{draft_id}-v{publication.draft_version}",
            draft_id=draft_id,
            draft_version=publication.draft_version,
            payload_fingerprint=publication.fingerprint,
            actor=actor,
            approved_at=datetime.now(UTC),
            decision_ids=tuple(
                decision.id
                for decision in publication.decisions
                if decision.action is DecisionAction.APPROVE
            ),
            confirmation=confirmation,
        )
        result = build_review_publisher(adapter.value).execute(draft_id, approval)
    except ReviewWorkflowError as error:
        _review_failure(error, json_output)
    payload = {"ok": True, "adapter": adapter.value, **result.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Publication {result.status.value} via {adapter.value}; "
            f"fingerprint={result.fingerprint}."
        )


@app.command("review-edit-description")
def review_edit_description(
    description: Annotated[str, typer.Option("--description")],
    revision: Annotated[int, typer.Option("--revision", min=1)],
    actor: Annotated[str, typer.Option("--actor")],
    rationale: Annotated[str, typer.Option("--rationale")],
    draft_id: Annotated[str, typer.Option("--draft-id")] = "customer-canonical",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Edit the model description and explicitly return affected mappings to review."""

    try:
        snapshot = build_review_editor().execute(
            draft_id,
            expected_revision=revision,
            actor=actor,
            decided_at=datetime.now(UTC),
            rationale=rationale,
            edit=ModelDescriptionEdit(description=description),
        )
    except (ReviewWorkflowError, ValueError) as error:
        if isinstance(error, ReviewWorkflowError):
            _review_failure(error, json_output)
        _review_failure(
            ReviewWorkflowError(
                ReviewErrorCode.INVALID_TRANSITION,
                "model description must not be blank",
            ),
            json_output,
        )
    payload = {
        "ok": True,
        "revision": snapshot.draft.revision,
        "status": snapshot.draft.logical_model.status.value,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Description edited at revision {snapshot.draft.revision}; mappings need review."
        )


@app.command("review-published")
def review_published(
    draft_id: Annotated[str, typer.Option("--draft-id")] = "customer-canonical",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the current canonical context back from live DataHub."""

    try:
        context = build_published_context_reader("live").execute(draft_id)
    except ReviewWorkflowError as error:
        _review_failure(error, json_output)
    if context is None:
        _review_failure(
            ReviewWorkflowError(
                ReviewErrorCode.NOT_FOUND,
                "approved canonical context is not current in DataHub",
            ),
            json_output,
        )
    payload = {"ok": True, "context": context.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Published context {context.logical_model_urn}; fingerprint={context.fingerprint}."
        )


@app.command("join-discover")
def join_discover(
    catalog: Annotated[CatalogAdapterChoice, typer.Option("--catalog")] = CatalogAdapterChoice.LIVE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Profile the two explicit north-star relationships through read-only adapters."""

    try:
        report = build_join_discoverer(catalog.value).execute(build_north_star_join_proposals())
    except (RelationshipWorkflowError, DatabaseConfigurationError) as error:
        _join_failure(error, json_output)
    payload = {
        "ok": True,
        "catalog_source": report.catalog_source,
        "candidates": [candidate.model_dump(mode="json") for candidate in report.candidates],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(title="Governed join candidates")
    table.add_column("Contract")
    table.add_column("Cardinality")
    table.add_column("Confidence")
    table.add_column("Recommendation")
    for candidate in report.candidates:
        table.add_row(
            candidate.proposal.id,
            candidate.cardinality.cardinality.value,
            f"{candidate.confidence.root:.3f}",
            candidate.recommendation.value,
        )
        if candidate.fanout_warning:
            table.add_row("fanout warning", candidate.fanout_warning, "", "")
    console.print(table)


@app.command("join-review-init")
def join_review_init(
    catalog: Annotated[CatalogAdapterChoice, typer.Option("--catalog")] = CatalogAdapterChoice.LIVE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Discover and persist pending join candidates locally; no approval or write occurs."""

    try:
        report = build_join_discoverer(catalog.value).execute(build_north_star_join_proposals())
        draft = build_join_review_start().execute(build_join_review_draft(report))
    except (RelationshipWorkflowError, DatabaseConfigurationError) as error:
        _join_failure(error, json_output)
    payload = {
        "ok": True,
        "catalog_source": report.catalog_source,
        "draft": draft.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Join review {draft.id} initialized at revision {draft.revision}.")


@app.command("join-review-show")
def join_review_show(
    draft_id: Annotated[str, typer.Option("--draft-id")] = "north-star-joins",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect candidate evidence, decisions, contracts, and publication attempts."""

    try:
        snapshot = build_join_review_inspector().execute(draft_id)
    except RelationshipWorkflowError as error:
        _join_failure(error, json_output)
    payload = {
        "ok": True,
        "draft": snapshot.draft.model_dump(mode="json"),
        "decisions": [decision.model_dump(mode="json") for decision in snapshot.decisions],
        "publications": [result.model_dump(mode="json") for result in snapshot.publications],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Join review {snapshot.draft.id}: revision={snapshot.draft.revision}")
    for item in snapshot.draft.joins:
        console.print(
            f"{item.candidate.proposal.id}: {item.status.value}; "
            f"{item.candidate.cardinality.cardinality.value}"
        )
        if item.candidate.fanout_warning:
            console.print(f"WARNING: {item.candidate.fanout_warning}", style="yellow")


@app.command("join-review-decide")
def join_review_decide(
    proposal_id: Annotated[str, typer.Argument()],
    action: Annotated[JoinDecisionChoice, typer.Option("--action")],
    revision: Annotated[int, typer.Option("--revision", min=1)],
    actor: Annotated[str, typer.Option("--actor")],
    rationale: Annotated[str, typer.Option("--rationale")],
    draft_id: Annotated[str, typer.Option("--draft-id")] = "north-star-joins",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Explicitly approve or reject one join; confidence never invokes this command."""

    try:
        snapshot = build_join_review_decider().execute(
            draft_id,
            proposal_id,
            DecisionAction(action.value),
            expected_revision=revision,
            actor=actor,
            decided_at=datetime.now(UTC),
            rationale=rationale,
        )
    except (RelationshipWorkflowError, ValueError) as error:
        if isinstance(error, RelationshipWorkflowError):
            _join_failure(error, json_output)
        _join_failure(
            RelationshipWorkflowError(
                RelationshipErrorCode.INVALID_TRANSITION,
                "invalid join decision",
            ),
            json_output,
        )
    payload = {
        "ok": True,
        "revision": snapshot.draft.revision,
        "decision": snapshot.decisions[-1].model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Join decision recorded at revision {snapshot.draft.revision}.")


@app.command("join-publish")
def join_publish(
    actor: Annotated[str, typer.Option("--actor")],
    confirmation: Annotated[
        JoinPublicationConfirmation,
        typer.Option("--confirm", help="Exact approval phrase for this join payload."),
    ],
    adapter: Annotated[WriteAdapterChoice, typer.Option("--adapter")] = WriteAdapterChoice.LIVE,
    draft_id: Annotated[str, typer.Option("--draft-id")] = "north-star-joins",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Publish only the exact approved contract set to the selected adapter."""

    try:
        publication = build_join_publication_preparer().execute(draft_id)
        approval = JoinPublicationApproval(
            id=f"{actor}-{draft_id}-v{publication.draft_version}",
            draft_id=draft_id,
            draft_version=publication.draft_version,
            payload_fingerprint=publication.fingerprint,
            actor=actor,
            approved_at=datetime.now(UTC),
            decision_ids=tuple(decision.id for decision in publication.decisions),
            confirmation=confirmation,
        )
        result = build_join_publisher(adapter.value).execute(draft_id, approval)
    except RelationshipWorkflowError as error:
        _join_failure(error, json_output)
    payload = {"ok": True, "adapter": adapter.value, **result.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Join publication {result.status.value} via {adapter.value}; "
            f"fingerprint={result.fingerprint}."
        )


@app.command("join-published")
def join_published(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Load current contracts from DataHub without consulting the local review store."""

    try:
        context = build_join_context_loader("live").execute()
    except RelationshipWorkflowError as error:
        _join_failure(error, json_output)
    if context is None:
        _join_failure(
            RelationshipWorkflowError(
                RelationshipErrorCode.NOT_FOUND,
                "approved join context is not current in DataHub",
            ),
            json_output,
        )
    payload = {"ok": True, "context": context.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Loaded {len(context.contract_set.contracts)} contracts from "
            f"{context.document_urn}; fingerprint={context.fingerprint}."
        )


def _join_failure(
    error: RelationshipWorkflowError | DatabaseConfigurationError,
    json_output: bool,
) -> Never:
    code = (
        error.code.value if isinstance(error, RelationshipWorkflowError) else "configuration_error"
    )
    payload = {"ok": False, "code": code, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Join operation failed: {code}", style="red")
    raise typer.Exit(code=1)


def _review_failure(error: ReviewWorkflowError, json_output: bool) -> Never:
    payload = {"ok": False, "code": error.code.value, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Review operation failed: {error.code.value}", style="red")
    raise typer.Exit(code=1)


def _request_failure(
    error: GuidedRequestValidationError | RequestWorkflowError,
    json_output: bool,
) -> Never:
    if isinstance(error, GuidedRequestValidationError):
        findings = [finding.model_dump(mode="json") for finding in error.result.findings]
        code = error.result.findings[0].code if error.result.findings else "request_invalid"
    else:
        findings = []
        code = error.code.value
    payload = {"ok": False, "code": code, "error": str(error), "findings": findings}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Request builder rejected the input: {code}", style="red")
        console.print(str(error))
    raise typer.Exit(code=1)


def _recipe_failure(
    error: RecipeError | DatabaseConfigurationError | ValueError,
    json_output: bool,
) -> Never:
    code = error.code.value if isinstance(error, RecipeError) else "recipe_configuration_error"
    payload = {"ok": False, "code": code, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Query-recipe operation failed: {code}", style="red")
    raise typer.Exit(code=1)


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Verify the local starter environment."""

    report = run_doctor(Path.cwd())
    if json_output:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        table = Table(title="SchemaBridge doctor")
        table.add_column("Check")
        table.add_column("Required")
        table.add_column("Status")
        table.add_column("Detail")
        for check in report.checks:
            table.add_row(
                check.name,
                "yes" if check.required else "no",
                "PASS" if check.ok else "FAIL",
                check.detail,
            )
        console.print(table)

    if not report.is_healthy:
        raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the package version."""

    typer.echo(__version__)


@app.command("evaluate")
def evaluate(
    output: Annotated[
        Path,
        typer.Option("--output", help="Repository-local deterministic JSON report path."),
    ] = Path("reports/evaluation.json"),
    markdown: Annotated[
        Path,
        typer.Option("--markdown", help="Repository-local judge-readable Markdown path."),
    ] = Path("examples/evaluation-report.md"),
    intent_adapter: Annotated[
        IntentAdapterChoice,
        typer.Option(
            "--intent-adapter",
            help="Keep the key-free deterministic run or add a separate live-LLM intent run.",
        ),
    ] = IntentAdapterChoice.FAKE,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Also emit the complete report on stdout."),
    ] = False,
) -> None:
    """Measure the versioned synthetic release candidate without quality overclaiming."""

    try:
        report = build_evaluation_runner(
            include_live_llm=intent_adapter is IntentAdapterChoice.LIVE,
        ).execute_evaluation()
        build_evaluation_report_writer().write(report, output, markdown)
    except (DatabaseConfigurationError, EvaluationError, IntentParserError) as error:
        code = getattr(error, "code", "evaluation_configuration_failed")
        code_value = code.value if isinstance(code, StrEnum) else str(code)
        payload = {"ok": False, "code": code_value, "error": str(error)}
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(f"Evaluation failed: {code_value}", style="red")
        raise typer.Exit(code=1) from error

    payload = {"ok": report.successful, **report.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(
            f"Evaluation {'PASS' if report.successful else 'FAIL'}; "
            f"JSON={output}; Markdown={markdown}",
            style="green" if report.successful else "red",
        )
        for run in report.runs:
            console.print(f"{run.mode.value}: {run.status.value} ({run.adapter})")
    if not report.successful:
        raise typer.Exit(code=1)


@app.command("normalize-demo")
def normalize_demo(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Demonstrate deterministic identifier normalization and rejections."""

    report = run_normalization_demo()
    if json_output:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return

    table = Table(title="SchemaBridge identifier normalization")
    table.add_column("Case")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Canonical / rejection")
    for result in report.results:
        outcome = result.outcome
        if outcome.status == "accepted":
            detail = "NULL" if outcome.canonical_value is None else outcome.canonical_value
        else:
            detail = f"{outcome.code.value}: {outcome.reason}"
        table.add_row(result.case, result.source_value, outcome.status, detail)
    console.print(table)


@app.command("query-demo")
def query_demo(
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Run the guarded preview against DATABASE_URL."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Compile, guard, and optionally preview the approved north-star plan."""

    plan = build_north_star_query_plan()
    policy = build_demo_query_policy()
    try:
        prepared = build_query_preparer(policy).execute(plan)
        preview = build_query_previewer(policy).execute(plan) if execute else None
    except (
        DatabaseHealthError,
        QueryCompilationError,
        SqlPolicyViolation,
        QueryPreviewError,
    ) as error:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(error)}, indent=2))
        else:
            console.print(f"Query demo failed: {error}", style="red")
        raise typer.Exit(code=1) from error

    payload: dict[str, object] = {
        "ok": True,
        "plan": plan.model_dump(mode="json"),
        "sql": prepared.sql,
        "parameters": list(prepared.parameters),
        "effective_limit": prepared.max_rows,
        "preview": preview.as_dict() if preview is not None else None,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    console.print("Approved north-star PostgreSQL", style="bold")
    console.print(prepared.sql)
    console.print(f"Parameters: {list(prepared.parameters)!r}")
    console.print(f"Effective preview limit: {prepared.max_rows}")
    if preview is not None:
        table = Table(title="SchemaBridge guarded preview")
        for column in preview.columns:
            table.add_column(column)
        for row in preview.rows:
            table.add_row(*(str(value) for value in row))
        console.print(table)
        console.print(
            f"Executor: {preview.database_user}; read-only: "
            f"{str(preview.transaction_read_only).lower()}; "
            f"timeout: {preview.statement_timeout_ms} ms"
        )


@app.command("request-demo")
def request_demo(
    case: Annotated[
        GuidedRequestCase,
        typer.Option(
            "--case",
            help="Build north-star, relationship-count, or Customer-only control intent.",
        ),
    ] = GuidedRequestCase.NORTH_STAR,
    metric_operation: Annotated[
        str | None,
        typer.Option("--metric-operation", help="Override the guided metric enum value."),
    ] = None,
    grain: Annotated[
        str | None,
        typer.Option("--grain", help="Override the guided date-grain enum value."),
    ] = None,
    filter_field: Annotated[
        str | None,
        typer.Option("--filter-field", help="Override the selected approved logical filter field."),
    ] = None,
    save_draft: Annotated[
        str | None,
        typer.Option("--save-draft", help="Save or idempotently reuse a local typed draft."),
    ] = None,
    load_draft: Annotated[
        str | None,
        typer.Option("--load-draft", help="Reload and revalidate a local typed draft."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Build a logical request from approved choices; physical planning is deferred."""

    if save_draft is not None and load_draft is not None:
        _request_failure(
            GuidedRequestValidationError(
                ValidationResult(
                    findings=(
                        ValidationFinding(
                            code="conflicting_draft_options",
                            severity=ValidationSeverity.ERROR,
                            message="Choose either --save-draft or --load-draft, not both.",
                            path=(),
                        ),
                    )
                )
            ),
            json_output,
        )

    builder = build_guided_request_builder()
    draft = None
    try:
        if load_draft is not None:
            loaded = build_request_draft_loader().execute(load_draft)
            validated = loaded.validated_request
            draft = loaded.draft
        else:
            validated = builder.execute(
                build_demo_guided_input(
                    case,
                    metric_operation=metric_operation,
                    grain=grain,
                    filter_field=filter_field,
                )
            )
            if save_draft is not None:
                draft = build_request_draft_saver().execute(
                    save_draft,
                    validated.request,
                )
        submission = build_guided_request_submitter().execute(validated)
        options = builder.options()
    except (GuidedRequestValidationError, RequestWorkflowError) as error:
        _request_failure(error, json_output)

    payload: dict[str, object] = {
        "ok": True,
        "case": case.value if load_draft is None else "reloaded-draft",
        "available_options": options.as_dict(),
        **submission.as_dict(),
        "draft": draft.model_dump(mode="json") if draft is not None else None,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"Approved context: {submission.summary.context_source}", style="bold")
    console.print(
        f"Primary entity: {submission.summary.primary_entity}; limit: {submission.summary.limit}"
    )
    table = Table(title="Validated analytical request")
    table.add_column("Role")
    table.add_column("Selection")
    for selection in submission.summary.dimensions:
        table.add_row("dimension", selection)
    for selection in submission.summary.metrics:
        table.add_row("metric", selection)
    for selection in submission.summary.filters:
        table.add_row("filter", selection)
    for selection in submission.summary.order_by:
        table.add_row("order", selection)
    console.print(table)
    console.print(f"Required logical models: {list(submission.summary.required_models)}")
    console.print(f"Approved join contracts: {list(submission.summary.join_contracts)}")
    console.print(
        "Planning handoff: "
        f"{submission.planning.status} via {submission.planning.adapter}; "
        "physical resolution and SQL execution are not part of M09."
    )
    if draft is not None:
        console.print(f"Local draft: {draft.id} revision {draft.revision}")


@app.command("intent-demo")
def intent_demo(
    text: Annotated[
        str,
        typer.Argument(help="Untrusted business-language request to interpret."),
    ] = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta.",
    language: Annotated[
        UserLanguage,
        typer.Option("--language", help="Current user language for interpretation."),
    ] = UserLanguage.SPANISH,
    adapter: Annotated[
        IntentAdapterChoice,
        typer.Option("--adapter", help="Explicit key-free fake or configured live adapter."),
    ] = IntentAdapterChoice.FAKE,
    confirm: Annotated[
        IntentAlternativeId | None,
        typer.Option("--confirm", help="Confirm one exact alternative shown in the preview."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Interpret natural language as typed intent; never compile or execute SQL."""

    try:
        resolver = build_natural_language_intent_resolver(adapter.value)
        preview = resolver.preview(text, language)
        payload: dict[str, object] = {
            "ok": True,
            "mode": "natural-language-intent",
            "executed": False,
            "preview": preview.as_dict(),
            "confirmation": None,
        }
        if confirm is not None:
            validated = resolver.confirm(
                preview,
                IntentConfirmation(
                    interpretation_fingerprint=preview.interpretation_fingerprint,
                    selected_alternative=confirm,
                ),
            )
            resolved_plan = build_semantic_request_planner().execute(validated)
            payload["confirmation"] = {
                "selected_alternative": confirm.value,
                "request": validated.request.model_dump(mode="json"),
                "request_fingerprint": validated_analytical_request_fingerprint(validated),
                "plan_fingerprint": resolved_semantic_plan_fingerprint(resolved_plan),
                "planned": True,
                "compiled": False,
                "executed": False,
            }
    except (
        IntentConfirmationError,
        IntentParserError,
        PlanningPortError,
        RequestWorkflowError,
        SemanticResolutionError,
    ) as error:
        code = getattr(error, "code", "intent_resolution_failed")
        code_value = code.value if isinstance(code, StrEnum) else str(code)
        failure = {"ok": False, "code": code_value, "error": str(error), "executed": False}
        if json_output:
            typer.echo(json.dumps(failure, indent=2))
        else:
            console.print(f"Intent resolution failed: {code_value}: {error}", style="red")
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Intent adapter: {preview.adapter}", style="bold")
    console.print(f"Interpretation: {preview.interpretation_fingerprint}")
    console.print(f"Ambiguities: {[item.value for item in preview.ambiguities]}")
    table = Table(title="Explicit interpretation alternatives")
    table.add_column("Choice")
    table.add_column("Available")
    table.add_column("Meaning")
    for alternative in preview.alternatives:
        table.add_row(
            alternative.id.value,
            "yes" if alternative.available else "no",
            alternative.rationale,
        )
    console.print(table)
    if preview.findings:
        for finding in preview.findings:
            console.print(f"{finding.severity.value}: {finding.code}: {finding.message}")
    if confirm is None:
        console.print("No confirmation supplied; semantic planning was not invoked.")
    else:
        confirmation = payload["confirmation"]
        assert isinstance(confirmation, dict)
        console.print(
            f"Confirmed typed request; plan fingerprint={confirmation['plan_fingerprint']}. "
            "SQL compilation and execution were not invoked."
        )


@app.command("workflow-demo")
def workflow_demo(
    action: Annotated[
        WorkflowActionChoice,
        typer.Option("--action", help="Explicit workflow transition to perform."),
    ] = WorkflowActionChoice.SHOW,
    workflow_id: Annotated[
        str,
        typer.Option("--workflow-id", help="Durable local workflow identifier."),
    ] = "north-star-workflow",
    actor: Annotated[
        str,
        typer.Option("--actor", help="Identity recorded for typed human decisions."),
    ] = "local-operator",
    catalog: Annotated[
        CatalogAdapterChoice,
        typer.Option("--catalog", help="Explicit live or sanitized recorded catalog source."),
    ] = CatalogAdapterChoice.RECORDED,
    publication_adapter: Annotated[
        WriteAdapterChoice,
        typer.Option(
            "--publication-adapter",
            help="Explicit live approval-gated DataHub or fake local publication adapter.",
        ),
    ] = WriteAdapterChoice.FAKE,
    text: Annotated[
        str,
        typer.Option("--text", help="Untrusted business-language request used only at start."),
    ] = "Agrupa por fecha de registro todos los clientes que sean segundo titular de una cuenta.",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable durable workflow state."),
    ] = False,
) -> None:
    """Start, resume, decide, retry, or inspect the durable governed workflow."""

    try:
        orchestrator = build_agent_workflow_orchestrator(
            catalog.value,
            publication_kind=publication_adapter.value,
        )
        if action is WorkflowActionChoice.START:
            draft = orchestrator.start(
                StartWorkflowCommand(
                    id=workflow_id,
                    text=text,
                    language=UserLanguage.SPANISH,
                    datasets=(
                        PhysicalDatasetRef("crm.customers"),
                        PhysicalDatasetRef("bank.account_holders"),
                    ),
                )
            )
        else:
            draft = orchestrator.resume(workflow_id)
            if action is WorkflowActionChoice.CONFIRM_INTENT:
                if draft.intent is None:
                    raise WorkflowError(
                        code=WorkflowErrorCode.INVALID_TRANSITION,
                        message="workflow has no typed interpretation to confirm",
                    )
                draft = orchestrator.decide_intent(
                    workflow_id,
                    IntentWorkflowDecision(
                        actor=actor,
                        interpretation_fingerprint=draft.intent.interpretation_fingerprint,
                        selected_alternative=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
                    ),
                )
            elif action in {
                WorkflowActionChoice.APPROVE_EXECUTION,
                WorkflowActionChoice.DECLINE_EXECUTION,
            }:
                if draft.plan_fingerprint is None:
                    raise WorkflowError(
                        code=WorkflowErrorCode.INVALID_TRANSITION,
                        message="workflow has no validated plan decision to bind",
                    )
                draft = orchestrator.decide_execution(
                    workflow_id,
                    ExecutionWorkflowDecision(
                        actor=actor,
                        plan_fingerprint=draft.plan_fingerprint,
                        action=(
                            WorkflowDecisionAction.APPROVE
                            if action is WorkflowActionChoice.APPROVE_EXECUTION
                            else WorkflowDecisionAction.DECLINE
                        ),
                    ),
                )
            elif action is WorkflowActionChoice.RETRY:
                if draft.failure is None:
                    raise WorkflowError(
                        code=WorkflowErrorCode.RETRY_NOT_ALLOWED,
                        message="workflow has no typed failure to retry",
                    )
                draft = orchestrator.retry(
                    workflow_id,
                    RetryWorkflowDecision(
                        actor=actor,
                        failure_fingerprint=draft.failure.fingerprint,
                        operation=draft.failure.operation,
                    ),
                )
            elif action in {
                WorkflowActionChoice.PUBLISH,
                WorkflowActionChoice.SKIP_PUBLICATION,
            }:
                if draft.publication_proposal is None:
                    raise WorkflowError(
                        code=WorkflowErrorCode.INVALID_TRANSITION,
                        message="workflow has no publication proposal",
                    )
                publishing = action is WorkflowActionChoice.PUBLISH
                draft = orchestrator.decide_publication(
                    workflow_id,
                    PublicationWorkflowDecision(
                        actor=actor,
                        proposal_fingerprint=draft.publication_proposal.fingerprint,
                        action=(
                            WorkflowDecisionAction.PUBLISH
                            if publishing
                            else WorkflowDecisionAction.SKIP
                        ),
                        confirmation=(
                            WorkflowPublicationConfirmation.PUBLISH_EXECUTION_CONTEXT
                            if publishing
                            else None
                        ),
                    ),
                )
        payload = {
            "ok": True,
            "publication_adapter": (
                "live:datahub-approved-context-document"
                if publication_adapter is WriteAdapterChoice.LIVE
                else "fake:local-idempotency-only"
            ),
            "draft": draft.model_dump(mode="json"),
        }
    except (DatabaseConfigurationError, WorkflowError, ValueError) as error:
        code = getattr(error, "code", "workflow_command_failed")
        code_value = code.value if isinstance(code, StrEnum) else str(code)
        failure = {"ok": False, "code": code_value, "error": str(error)}
        if json_output:
            typer.echo(json.dumps(failure, indent=2))
        else:
            console.print(f"Workflow command failed: {code_value}: {error}", style="red")
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    console.print(
        f"Workflow {draft.id}: stage={draft.stage.value}; revision={draft.revision}",
        style="bold",
    )
    if draft.checkpoint is not None:
        console.print(f"Checkpoint: {draft.checkpoint.kind.value} ({draft.checkpoint.reason})")
    if draft.failure is not None:
        console.print(
            f"Failure: {draft.failure.code}; retryable={draft.failure.retryable}",
            style="red",
        )
    if draft.execution is not None:
        console.print(
            f"Preview rows={len(draft.execution.rows)}; "
            f"rejections={draft.execution.rejected_count}; "
            f"reader={draft.execution.database_user}"
        )
    if draft.recipe_reuse is not None:
        console.print(
            f"Recipe reuse: {draft.recipe_reuse.status.value}; "
            f"provenance={draft.recipe_reuse.provenance_document_urn or 'none'}; "
            f"revalidated={draft.recipe_reuse.revalidated}."
        )
    table = Table(title="Observable workflow trace")
    table.add_column("#")
    table.add_column("Stage")
    table.add_column("Operation")
    table.add_column("Status")
    for event in draft.trace:
        table.add_row(
            str(event.sequence),
            event.stage.value,
            event.operation.value,
            event.status.value,
        )
    console.print(table)
    console.print(f"Publication adapter: {payload['publication_adapter']}.")


@app.command("recipe-show")
def recipe_show(
    workflow_id: Annotated[
        str,
        typer.Option("--workflow-id", help="Completed durable workflow to review."),
    ] = "north-star-workflow",
    adapter: Annotated[
        WriteAdapterChoice,
        typer.Option("--adapter", help="Explicit live DataHub or persistent fake repository."),
    ] = WriteAdapterChoice.FAKE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare and display a SQL-free recipe; this command never publishes it."""

    try:
        recipe = build_query_recipe_preparer(adapter.value).execute(workflow_id)
    except (DatabaseConfigurationError, RecipeError, ValueError) as error:
        _recipe_failure(error, json_output)
    payload = {
        "ok": True,
        "adapter": adapter.value,
        "published": False,
        "recipe": recipe.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    console.print(f"Recipe {recipe.id} v{recipe.version}", style="bold")
    console.print(f"Fingerprint: {recipe.fingerprint}")
    console.print("Review only: no DataHub write was performed.")


@app.command("recipe-publish")
def recipe_publish(
    actor: Annotated[str, typer.Option("--actor")],
    confirmation: Annotated[
        RecipePublicationConfirmation,
        typer.Option("--confirm", help="Exact approval phrase for the reviewed recipe."),
    ],
    workflow_id: Annotated[
        str,
        typer.Option("--workflow-id", help="Completed durable workflow to publish."),
    ] = "north-star-workflow",
    adapter: Annotated[
        WriteAdapterChoice,
        typer.Option("--adapter", help="Explicit live DataHub or persistent fake repository."),
    ] = WriteAdapterChoice.LIVE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Publish one exact executed recipe after typed review and explicit approval."""

    try:
        recipe = build_query_recipe_preparer(adapter.value).execute(workflow_id)
        approval = RecipePublicationApproval(
            id=f"{actor}-{recipe.id}-v{recipe.version}",
            recipe_id=recipe.id,
            recipe_version=recipe.version,
            recipe_fingerprint=recipe.fingerprint,
            actor=actor,
            approved_at=datetime.now(UTC),
            confirmation=confirmation,
        )
        result = build_query_recipe_publisher(adapter.value).execute(recipe, approval)
    except (DatabaseConfigurationError, RecipeError, ValueError) as error:
        _recipe_failure(error, json_output)
    payload = {
        "ok": result.failure_code is None,
        "adapter": adapter.value,
        "recipe": recipe.model_dump(mode="json"),
        "approval": approval.model_dump(mode="json"),
        "publication": result.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        if result.failure_code is not None:
            raise typer.Exit(code=1)
        return
    console.print(
        f"Recipe publication {result.status.value} via {adapter.value}; "
        f"document={result.current_document_urn}."
    )
    if result.failure_code is not None:
        raise typer.Exit(code=1)


@app.command("governed-demo")
def governed_demo(
    case: Annotated[
        GuidedRequestCase,
        typer.Option("--case", help="Choose a fixed typed guided-request demonstration."),
    ] = GuidedRequestCase.NORTH_STAR,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Run the guarded bounded preview and rejection report."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Resolve approved context, compile, guard, and optionally execute a guided request."""

    try:
        validated = build_guided_request_builder().execute(build_demo_guided_input(case))
        if execute:
            result = build_governed_request_executor().execute(validated)
            payload: dict[str, object] = {
                "ok": True,
                "case": case.value,
                "executed": True,
                **result.as_dict(),
            }
        else:
            prepared = build_governed_request_preparer().execute(validated)
            payload = {
                "ok": True,
                "case": case.value,
                "executed": False,
                "resolved_plan": prepared.resolved_plan.model_dump(mode="json"),
                "sql": prepared.query.sql,
                "parameters": list(prepared.query.parameters),
                "policy": {
                    "status": prepared.policy_status,
                    "findings": [],
                },
                "preview": None,
                "rejected_sources": None,
            }
    except (
        DatabaseConfigurationError,
        GuidedRequestValidationError,
        PlanningPortError,
        QueryCompilationError,
        QueryPreviewError,
        RequestWorkflowError,
        SemanticResolutionError,
        SqlPolicyViolation,
    ) as error:
        code = getattr(error, "code", "governed_request_failed")
        code_value = code.value if isinstance(code, StrEnum) else str(code)
        failure = {"ok": False, "code": code_value, "error": str(error)}
        if json_output:
            typer.echo(json.dumps(failure, indent=2))
        else:
            console.print(f"Governed request failed: {code_value}: {error}", style="red")
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    plan = payload["resolved_plan"]
    assert isinstance(plan, dict)
    console.print(
        f"Planning context: {plan['context_source']} v{plan['context_version']}",
        style="bold",
    )
    console.print(f"SQL policy: {payload['policy']}")
    console.print(str(payload["sql"]))
    console.print(f"Parameters: {payload['parameters']}")
    if execute:
        preview = payload["preview"]
        rejected = payload["rejected_sources"]
        assert isinstance(preview, dict)
        assert isinstance(rejected, dict)
        columns = preview["columns"]
        rows = preview["rows"]
        assert isinstance(columns, list)
        assert isinstance(rows, list)
        result_table = Table(title="Governed preview rows")
        for column in columns:
            result_table.add_column(str(column))
        for row in rows:
            assert isinstance(row, list)
            result_table.add_row(*(str(value) for value in row))
        console.print(result_table)

        records = rejected["records"]
        assert isinstance(records, list)
        rejection_table = Table(title="Rejected join-key sources")
        rejection_table.add_column("Physical field")
        rejection_table.add_column("Source value")
        rejection_table.add_column("Code")
        for record in records:
            assert isinstance(record, dict)
            rejection_table.add_row(
                str(record["physical_field"]),
                "NULL" if record["source_value"] is None else str(record["source_value"]),
                str(record["code"]),
            )
        console.print(rejection_table)


@app.command("sql-guard-demo")
def sql_guard_demo(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Reject three fixed malicious SQL examples without executing them."""

    policy = build_demo_query_policy()
    results = run_guard_demo(build_sql_guard(), policy)
    payload = [
        {
            "case": result.id,
            "findings": [
                {"code": finding.code.value, "message": finding.message}
                for finding in result.findings
            ],
        }
        for result in results
    ]
    if json_output:
        typer.echo(json.dumps({"ok": True, "results": payload}, indent=2))
        return

    table = Table(title="SchemaBridge SQL guard security cases")
    table.add_column("Case")
    table.add_column("Rejection codes")
    for item in payload:
        findings = item["findings"]
        assert isinstance(findings, list)
        table.add_row(
            str(item["case"]),
            ", ".join(str(finding["code"]) for finding in findings),
        )
    console.print(table)


@app.command("postgres-health")
def postgres_health(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Verify the configured PostgreSQL reader and safety settings."""

    try:
        report = build_postgres_health_check().execute()
    except DatabaseHealthError as error:
        if json_output:
            typer.echo(json.dumps({"healthy": False, "error": str(error)}, indent=2))
        else:
            console.print(f"PostgreSQL health check failed: {error}", style="red")
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        table = Table(title="SchemaBridge PostgreSQL health")
        table.add_column("Check")
        table.add_column("Value")
        table.add_row("Status", "PASS" if report.is_ready else "FAIL")
        table.add_row("Backend", report.details.backend)
        table.add_row("Server", report.details.server_version)
        table.add_row("Database", report.details.database)
        table.add_row("User", report.details.user)
        table.add_row(
            "Role default read-only",
            "yes" if report.details.default_transaction_read_only else "no",
        )
        table.add_row(
            "Health transaction read-only",
            "yes" if report.details.transaction_read_only else "no",
        )
        table.add_row("Statement timeout", f"{report.details.statement_timeout_ms} ms")
        table.add_row("Findings", ", ".join(report.findings) or "none")
        console.print(table)

    if not report.is_ready:
        raise typer.Exit(code=1)


@app.command("catalog-inspect")
def catalog_inspect(
    dataset: Annotated[
        str,
        typer.Argument(help="Schema-qualified synthetic dataset, for example crm.customers."),
    ],
    adapter: Annotated[
        CatalogAdapterChoice,
        typer.Option("--adapter", help="Catalog source: live DataHub MCP or sanitized recording."),
    ] = CatalogAdapterChoice.LIVE,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Inspect bounded catalog metadata without semantic scoring or writes."""

    try:
        dataset_ref = PhysicalDatasetRef(dataset)
        report = build_catalog_inspector(adapter.value).execute(dataset_ref)
    except ValueError as error:
        payload = {"ok": False, "code": "invalid_dataset", "error": str(error)}
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(f"Catalog inspection failed: {payload['code']}", style="red")
        raise typer.Exit(code=2) from error
    except CatalogReadError as error:
        payload = {
            "ok": False,
            "code": error.code.value,
            "operation": error.operation,
            "error": str(error),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(
                f"Catalog inspection failed: {error.code.value} ({error.operation})",
                style="red",
            )
        raise typer.Exit(code=1) from error

    payload = {"ok": True, **report.as_dict()}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"Catalog source: {report.source}", style="bold")
    console.print(f"Asset: {report.asset.dataset} ({report.asset.urn})")
    if report.asset.description:
        console.print(report.asset.description)
    table = Table(title="Schema fields")
    table.add_column("Field")
    table.add_column("Native type")
    table.add_column("Description")
    for field in report.fields:
        table.add_row(
            ".".join(field.field_path),
            field.native_type or "missing",
            field.description or "missing",
        )
    console.print(table)
    console.print(
        "Evidence: "
        f"upstream={report.upstream.status.value}; "
        f"downstream={report.downstream.status.value}; "
        f"query_context={report.query_context.status.value}"
    )


@app.command("candidates")
def candidates(
    concept: Annotated[
        str,
        typer.Option("--concept", help="Model-qualified logical field to evaluate."),
    ],
    adapter: Annotated[
        CatalogAdapterChoice,
        typer.Option("--adapter", help="Catalog source: live DataHub MCP or sanitized recording."),
    ] = CatalogAdapterChoice.LIVE,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Rank explainable physical-field candidates without approving them."""

    if concept != "Customer.customer_key":
        error_payload = {
            "ok": False,
            "code": "unsupported_demo_concept",
            "error": "M06 demo supports only Customer.customer_key",
        }
        if json_output:
            typer.echo(json.dumps(error_payload, indent=2))
        else:
            console.print(error_payload["error"], style="red")
        raise typer.Exit(code=2)

    try:
        report = build_candidate_generator(adapter.value).execute(build_customer_key_concept())
        metrics = (
            build_candidate_evaluator().execute()
            if adapter is CatalogAdapterChoice.RECORDED
            else None
        )
    except (CatalogReadError, CandidateGenerationError, ValueError) as error:
        code = (
            error.code.value
            if isinstance(error, CatalogReadError)
            else "candidate_generation_failed"
        )
        failure_payload = {"ok": False, "code": code, "error": str(error)}
        if json_output:
            typer.echo(json.dumps(failure_payload, indent=2))
        else:
            console.print(f"Candidate generation failed: {code}", style="red")
        raise typer.Exit(code=1) from error

    payload: dict[str, object] = {"ok": True, **report.as_dict()}
    if metrics is not None:
        payload["evaluation_fixture_metrics"] = metrics.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"Catalog source: {report.catalog_source}", style="bold")
    console.print(f"Evidence source: {report.evidence_source}")
    console.print(
        "Retrieval: "
        f"query={report.asset_query!r}; assets={report.assets_considered}; "
        f"fields={report.fields_scanned}; blocked={report.fields_blocked_in}; "
        f"truncated={str(report.retrieval_truncated).lower()}"
    )
    summary = Table(title=f"Candidates for {report.concept.id}")
    summary.add_column("Rank")
    summary.add_column("Physical field")
    summary.add_column("Confidence")
    summary.add_column("Recommendation")
    summary.add_column("Status")
    summary.add_column("Risks")
    for rank, candidate in enumerate(report.candidates, start=1):
        summary.add_row(
            str(rank),
            candidate.physical_field.root,
            f"{candidate.confidence.root:.3f}",
            candidate.recommendation.value,
            candidate.status.value,
            ", ".join(candidate.risks) or "none",
        )
    console.print(summary)

    for rank, candidate in enumerate(report.candidates, start=1):
        details = Table(title=f"#{rank} {candidate.physical_field.root} — signal breakdown")
        details.add_column("Signal")
        details.add_column("Weight")
        details.add_column("Raw")
        details.add_column("Contribution")
        details.add_column("Evidence / missing reason")
        for signal in candidate.score_breakdown:
            details.add_row(
                signal.signal.value,
                f"{signal.weight:.3f}",
                f"{signal.raw_score:.3f}",
                f"{signal.weighted_score:.3f}",
                signal.detail or signal.missing_reason or "missing",
            )
        console.print(details)
        console.print(
            "Suggested transformations: "
            + ", ".join(step.operation for step in candidate.suggested_transformation_plan.steps)
        )

    if metrics is not None:
        console.print("Synthetic fixture metrics (not production evidence)", style="bold")
        console.print(
            f"precision={metrics.precision:.3f}; recall={metrics.recall:.3f}; "
            f"f1={metrics.f1:.3f}; top-k={metrics.top_k_recall}"
        )
        console.print(f"false positives: {list(metrics.false_positives)}")
        console.print(f"false negatives: {list(metrics.false_negatives)}")


if __name__ == "__main__":
    app()
