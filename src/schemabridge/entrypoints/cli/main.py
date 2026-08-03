"""SchemaBridge command-line interface."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never

import typer
from pydantic import ValidationError
from pydantic_settings import SettingsError
from rich.console import Console
from rich.table import Table

from schemabridge import __version__
from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.application.candidate_engine import CandidateGenerationError
from schemabridge.application.catalog_inventory import CatalogUseCaseError
from schemabridge.application.doctor import run_doctor
from schemabridge.application.guided_requests import (
    GuidedRequestCase,
    GuidedRequestValidationError,
    build_demo_guided_input,
)
from schemabridge.application.identity_rotation import (
    IdentityRotationError,
    IdentityRotationErrorCode,
)
from schemabridge.application.intent_resolution import IntentConfirmationError
from schemabridge.application.join_demo import (
    build_join_review_draft,
    build_north_star_join_proposals,
)
from schemabridge.application.legacy_import import (
    ApproveLegacyControlPlaneImport,
    LegacyImportError,
    LegacyImportErrorCode,
)
from schemabridge.application.natural_sql import (
    NaturalSqlError,
    NaturalSqlErrorCode,
    NaturalSqlPreparation,
)
from schemabridge.application.normalization_demo import run_normalization_demo
from schemabridge.application.ports.advanced_query_studio import (
    AdvancedQueryStudioPortError,
)
from schemabridge.application.ports.catalog import CatalogReadError
from schemabridge.application.ports.control_plane_migrations import (
    ControlPlaneMigrationError,
)
from schemabridge.application.ports.control_plane_operations import (
    ControlPlaneOperationError,
)
from schemabridge.application.ports.evaluation import EvaluationError
from schemabridge.application.ports.identity_evidence import (
    IdentityEvidenceEnvelopePort,
    IdentityEvidenceError,
)
from schemabridge.application.ports.intents import IntentParserError
from schemabridge.application.ports.planning import (
    PlanningPortError,
    RegistryPublicationError,
)
from schemabridge.application.ports.recipes import RecipeError, RecipeErrorCode
from schemabridge.application.ports.registry_control import (
    RegistryControlError,
    RegistryControlErrorCode,
)
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
from schemabridge.application.registry_control import (
    PrepareRegistryActivationApproval,
    PrepareRegistryReconciliationApproval,
)
from schemabridge.application.review_demo import build_customer_review_draft
from schemabridge.application.workflow_orchestration import workflow_recovery_operation
from schemabridge.bootstrap import (
    NaturalSqlRuntimeServices,
    build_agent_workflow_orchestrator,
    build_candidate_evaluator,
    build_candidate_generator,
    build_catalog_inspector,
    build_control_plane_backup,
    build_control_plane_migrator,
    build_control_plane_restore,
    build_evaluation_report_writer,
    build_evaluation_runner,
    build_governed_request_executor,
    build_governed_request_preparer,
    build_guided_request_builder,
    build_guided_request_submitter,
    build_identity_evidence_reader,
    build_identity_rotation_services,
    build_join_context_loader,
    build_join_discoverer,
    build_join_publication_preparer,
    build_join_publisher,
    build_join_review_decider,
    build_join_review_inspector,
    build_join_review_start,
    build_legacy_control_plane_import,
    build_natural_language_intent_resolver,
    build_natural_sql_runtime,
    build_postgres_health_check,
    build_publication_preparer,
    build_published_context_reader,
    build_query_preparer,
    build_query_previewer,
    build_query_recipe_migration_preparer,
    build_query_recipe_migration_publisher,
    build_query_recipe_preparer,
    build_query_recipe_publisher,
    build_recorded_registry_publication_source,
    build_registry_activation_committer,
    build_registry_activation_preparer,
    build_registry_control_store,
    build_registry_reconciliation_inspector,
    build_registry_reconciliation_repairer,
    build_registry_rollback_preparer,
    build_request_draft_loader,
    build_request_draft_saver,
    build_review_decider,
    build_review_editor,
    build_review_inspector,
    build_review_publisher,
    build_review_start,
    build_semantic_registry,
    build_semantic_registry_publication_approval_preparer,
    build_semantic_registry_scope,
    build_semantic_registry_version_publisher,
    build_semantic_request_planner,
    build_source_control_database_separation,
    build_sql_guard,
    build_streamlit_principal,
    build_tenant_capacity_policy_operator,
    resolve_control_operator_actor,
    resolve_runtime_profile,
)
from schemabridge.domain.advanced_query_studio import (
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
    AdvancedQueryConfirmationAction,
)
from schemabridge.domain.advanced_requests import (
    AdvancedAnalyticalRequest,
    BooleanOperator,
    LogicalBooleanPredicate,
    OutputBooleanPredicate,
)
from schemabridge.domain.catalog_inventory import (
    TenantCapacityPolicyChange,
    TenantCapacityPolicyConfirmation,
)
from schemabridge.domain.decisions import DecisionAction
from schemabridge.domain.fields import PhysicalDatasetRef
from schemabridge.domain.identity_rotation import (
    IdentityInitializationConfirmation,
    IdentityRotationConfirmation,
    IdentityRotationPlan,
)
from schemabridge.domain.intents import (
    IntentAlternativeId,
    IntentConfirmation,
    UserLanguage,
)
from schemabridge.domain.join_reviews import (
    JoinPublicationApproval,
    JoinPublicationConfirmation,
)
from schemabridge.domain.legacy_import import LegacyImportConfirmation
from schemabridge.domain.recipes import (
    RecipeMigrationProposal,
    RecipePublicationApproval,
    RecipePublicationConfirmation,
)
from schemabridge.domain.registry_control import (
    RegistryActivationConfirmation,
    RegistryReconciliationConfirmation,
)
from schemabridge.domain.request_context import validated_analytical_request_fingerprint
from schemabridge.domain.requests import AnalyticalRequest, Filter
from schemabridge.domain.resolution import (
    SemanticResolutionError,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.reviews import (
    ModelDescriptionEdit,
    PublicationApproval,
    PublicationConfirmation,
)
from schemabridge.domain.semantic_registry import (
    RegistryPublicationConfirmation,
    datahub_registry_document_urn,
    prepare_datahub_registry_version,
    semantic_registry_decision_ids,
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
    RECOVER = "recover"
    PUBLISH = "publish"
    SKIP_PUBLICATION = "skip-publication"


app = typer.Typer(
    name="schemabridge",
    help="Governed semantic query agent built on DataHub.",
    no_args_is_help=True,
)
control_plane_app = typer.Typer(
    help="Explicit PostgreSQL control-plane migration and read-only status commands.",
    no_args_is_help=True,
)
registry_control_app = typer.Typer(
    help="Prepare and commit exact approval-gated registry transitions.",
    no_args_is_help=True,
)
registry_reconcile_app = typer.Typer(
    help="Inspect and explicitly repair the DataHub active-pointer projection.",
    no_args_is_help=True,
)
legacy_import_app = typer.Typer(
    help="Inspect and approval-gate one offline legacy SQLite import.",
    no_args_is_help=True,
)
recipe_migration_app = typer.Typer(
    help="Prepare and publish one exact stale query-recipe migration.",
    no_args_is_help=True,
)
identity_control_app = typer.Typer(
    help="Inspect and approval-gate verified opaque identity initialization or rotation.",
    no_args_is_help=True,
)
capacity_policy_app = typer.Typer(
    help="Create or revise tenant capacity through optimistic explicit approval.",
    no_args_is_help=True,
)
app.add_typer(control_plane_app, name="control-plane")
control_plane_app.add_typer(registry_control_app, name="registry")
control_plane_app.add_typer(registry_reconcile_app, name="reconcile")
control_plane_app.add_typer(legacy_import_app, name="legacy-import")
control_plane_app.add_typer(recipe_migration_app, name="recipe-migration")
control_plane_app.add_typer(identity_control_app, name="identity")
control_plane_app.add_typer(capacity_policy_app, name="capacity")
console = Console()


@app.callback()
def enforce_authenticated_production_entrypoint(context: typer.Context) -> None:
    """Disable the legacy caller-identified CLI in managed deployments."""

    try:
        profile = resolve_runtime_profile()
    except (SettingsError, ValidationError):
        typer.echo(
            "cli_runtime_configuration_invalid: typed runtime configuration was rejected",
            err=True,
        )
        raise typer.Exit(code=2) from None
    if profile in {"staging", "production"} and context.invoked_subcommand != "control-plane":
        typer.echo(
            "cli_authentication_required: the legacy CLI is disabled in managed deployments",
            err=True,
        )
        raise typer.Exit(code=2)


@control_plane_app.command("migrate")
def control_plane_migrate(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Explicitly apply every reviewed pending migration with the migrator credential."""

    try:
        result = build_control_plane_migrator(credential_kind="migrator").migrate()
    except (ControlPlaneMigrationError, DatabaseConfigurationError) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "current_version": result.inspection.current_version,
        "expected_version": result.inspection.expected_version,
        "applied_versions": list(result.applied_versions),
        "already_current": result.already_current,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        applied = ", ".join(str(version) for version in result.applied_versions) or "none"
        console.print(
            "Control-plane migration complete: "
            f"version={result.inspection.current_version}; applied={applied}."
        )


@control_plane_app.command("backup")
def control_plane_backup(
    destination: Annotated[
        Path,
        typer.Option(
            "--destination",
            help="Owner-only local directory for the archive and signed manifest.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a transaction-consistent signed backup with the dedicated backup credential."""

    try:
        archive, manifest_path, manifest = build_control_plane_backup().create_backup(destination)
    except (
        ControlPlaneMigrationError,
        ControlPlaneOperationError,
        DatabaseConfigurationError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "archive": str(archive),
        "manifest": str(manifest_path),
        "schema_version": manifest.schema_version,
        "schema_checksum": manifest.schema_checksum,
        "archive_sha256": manifest.archive_sha256,
        "state_sha256": manifest.state_sha256,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Control-plane backup verified: archive={archive}; manifest={manifest_path}; "
            f"state={manifest.state_sha256}."
        )


@control_plane_app.command("restore")
def control_plane_restore(
    archive: Annotated[
        Path,
        typer.Option("--archive", help="Owner-only custom-format backup archive."),
    ],
    manifest: Annotated[
        Path,
        typer.Option("--manifest", help="Signed manifest paired with the archive."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Restore with the secret configured target and emit post-restore verification facts."""

    try:
        verification = build_control_plane_restore().restore_backup(
            archive,
            manifest,
        )
    except (
        ControlPlaneMigrationError,
        ControlPlaneOperationError,
        DatabaseConfigurationError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    payload = {"ok": True, **verification.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Control-plane restore verified: "
            f"schema={verification.schema_version}; state={verification.state_sha256}; "
            f"audit_events={verification.audit_events}."
        )


@control_plane_app.command("check")
def control_plane_check(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify exact schema history independently through every dedicated credential."""

    try:
        inspections = {
            "runtime": build_control_plane_migrator(credential_kind="runtime").require_current(),
            "reconciler": build_control_plane_migrator(
                credential_kind="reconciler"
            ).require_current(),
            "migrator": build_control_plane_migrator(credential_kind="migrator").require_current(),
            "api": build_control_plane_migrator(credential_kind="api").require_current(),
            "worker": build_control_plane_migrator(credential_kind="worker").require_current(),
            "publisher": build_control_plane_migrator(
                credential_kind="publisher"
            ).require_current(),
            "catalog": build_control_plane_migrator(credential_kind="catalog").require_current(),
            "observer": build_control_plane_migrator(credential_kind="observer").require_current(),
            "backup": build_control_plane_migrator(credential_kind="backup").require_current(),
        }
        separation = build_source_control_database_separation().execute()
    except (ControlPlaneMigrationError, DatabaseConfigurationError) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "roles": {
            credential: {
                "current_version": inspection.current_version,
                "expected_version": inspection.expected_version,
                "pending_versions": [item.version for item in inspection.pending],
            }
            for credential, inspection in inspections.items()
        },
        "database_separation": {
            "separate": separation.separate,
            "source_database": separation.source.database,
            "source_user": separation.source.user,
            "control_database": separation.control.database,
            "control_user": separation.control.user,
        },
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        table = Table(title="SchemaBridge control-plane schema")
        table.add_column("Credential")
        table.add_column("Current")
        table.add_column("Expected")
        table.add_column("Pending")
        for credential, inspection in inspections.items():
            table.add_row(
                credential,
                str(inspection.current_version),
                str(inspection.expected_version),
                ", ".join(str(item.version) for item in inspection.pending) or "none",
            )
        console.print(table)
        console.print(
            "Database separation verified by PostgreSQL: "
            f"source={separation.source.database}; control={separation.control.database}."
        )
        console.print("Read-only inspection; no migration or control-state write was performed.")


@control_plane_app.command("status")
def control_plane_status(
    workspace_id: Annotated[
        str,
        typer.Option(
            "--workspace-id",
            help="Exact opaque authenticated workspace identifier to inspect.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the authoritative pointer, history, and pending outbox without DataHub I/O."""

    try:
        build_control_plane_migrator(credential_kind="runtime").require_current()
        scope = build_semantic_registry_scope(workspace_id=workspace_id)
        store = build_registry_control_store()
        active = store.load_active(scope)
        pending = store.load_pending_outbox(scope)
        history = store.list_transitions(scope, limit=100)
    except (
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        RegistryControlError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "scope": scope.model_dump(mode="json"),
        "active_pointer": None if active is None else active.model_dump(mode="json"),
        "pending_outbox": None if pending is None else pending.model_dump(mode="json"),
        "transition_count": len(history),
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        generation = "none" if active is None else str(active.generation)
        version = "none" if active is None else str(active.registry_version)
        console.print(
            f"Control-plane status: generation={generation}; version={version}; "
            f"history={len(history)}; pending={'yes' if pending is not None else 'no'}."
        )


@capacity_policy_app.command("apply")
def tenant_capacity_policy_apply(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque tenant workspace."),
    ],
    expected_version: Annotated[
        int,
        typer.Option(
            "--expected-version",
            help="Use 0 to create; use the current version to revise.",
        ),
    ],
    connection_limit: Annotated[int, typer.Option("--connection-limit")],
    asset_limit: Annotated[int, typer.Option("--asset-limit")],
    field_limit: Annotated[int, typer.Option("--field-limit")],
    api_requests_per_minute: Annotated[
        int,
        typer.Option("--api-requests-per-minute"),
    ],
    nonterminal_job_limit: Annotated[int, typer.Option("--nonterminal-job-limit")],
    generation_retention_seconds: Annotated[
        int,
        typer.Option("--generation-retention-seconds"),
    ],
    confirmation: Annotated[
        TenantCapacityPolicyConfirmation,
        typer.Option("--confirm", help="Exact durable policy-approval phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Apply one version-checked policy and retain an immutable revision record."""

    try:
        resolved_actor = _control_operator_actor(actor, required_role="platform_admin")
        change = TenantCapacityPolicyChange(
            workspace_id=workspace_id,
            expected_version=expected_version,
            connection_limit=connection_limit,
            asset_limit=asset_limit,
            field_limit=field_limit,
            api_requests_per_minute=api_requests_per_minute,
            nonterminal_job_limit=nonterminal_job_limit,
            generation_retention_seconds=generation_retention_seconds,
            updated_by=resolved_actor,
            confirmation=confirmation,
        )
        policy = build_tenant_capacity_policy_operator().execute(change)
    except (
        CatalogUseCaseError,
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        ValidationError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "policy": policy.model_dump(mode="json"),
        "writes_performed": True,
        "immutable_revision_recorded": True,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Tenant capacity policy applied: "
            f"workspace={policy.workspace_id}; version={policy.version}; "
            f"connections={policy.connection_limit}; assets={policy.asset_limit}; "
            f"fields={policy.field_limit}."
        )


@identity_control_app.command("inspect-evidence")
def identity_inspect_evidence(
    evidence_file: Annotated[
        Path,
        typer.Option(
            "--evidence-file",
            help="Owner-only signed envelope containing opaque verified derivations.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify a transient signed envelope and emit only bounded review metadata."""

    try:
        envelope = build_identity_evidence_reader(evidence_file).read()
    except (DatabaseConfigurationError, IdentityEvidenceError, ValueError) as error:
        _identity_failure(error, json_output)
    payload = _identity_evidence_review_payload(envelope)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Identity evidence verified: "
            f"workspace={envelope.workspace_id}; "
            f"owners={len(envelope.derivations)}; "
            f"fingerprint={envelope.payload_fingerprint}."
        )
        console.print("Read-only verification; no claims, tokens, or derivations were emitted.")


@identity_control_app.command("initialize")
def identity_initialize(
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence-file", help="Exact owner-only envelope reviewed earlier."),
    ],
    evidence_fingerprint: Annotated[
        str,
        typer.Option(
            "--evidence-fingerprint",
            help="Exact SHA-256 emitted by identity inspect-evidence.",
        ),
    ],
    approved_at: Annotated[
        str,
        typer.Option(
            "--approved-at",
            help="Exact timezone-aware approval timestamp retained for replay.",
        ),
    ],
    confirmation: Annotated[
        IdentityInitializationConfirmation,
        typer.Option("--confirm", help="Exact identity-initialization approval phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Initialize opaque identity state only from exact approved signed evidence."""

    try:
        envelope = build_identity_evidence_reader(evidence_file).read(
            expected_fingerprint=evidence_fingerprint
        )
        approval_time = _parse_operator_time(approved_at, "identity initialization approval")
        _require_evidence_time(envelope, approval_time)
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        state = build_identity_rotation_services().initialize.execute(
            envelope.derivations,
            evidence_fingerprint=envelope.payload_fingerprint,
            actor=resolved_actor,
            approved_at=approval_time,
            confirmation=confirmation,
        )
    except _IDENTITY_OPERATOR_ERRORS as error:
        _identity_failure(error, json_output)
    payload = {
        "ok": True,
        "evidence_fingerprint": envelope.payload_fingerprint,
        "workspace_id": state.workspace_id,
        "active_key_version": state.active_key_version,
        "revision": state.revision,
        "owner_count": len(state.owner_actor_ids),
        "writes_performed": True,
        "sensitive_evidence_exposed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Verified identity state initialized: "
            f"workspace={state.workspace_id}; key={state.active_key_version}; "
            f"owners={len(state.owner_actor_ids)}."
        )


@identity_control_app.command("prepare")
def identity_prepare(
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence-file", help="Exact owner-only envelope reviewed earlier."),
    ],
    evidence_fingerprint: Annotated[
        str,
        typer.Option("--evidence-fingerprint", help="Exact reviewed evidence SHA-256."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare an opaque all-owner rotation plan without writing control state."""

    try:
        envelope = build_identity_evidence_reader(evidence_file).read(
            expected_fingerprint=evidence_fingerprint
        )
        plan = build_identity_rotation_services().prepare.execute(
            envelope.workspace_id,
            envelope.derivations,
        )
    except _IDENTITY_OPERATOR_ERRORS as error:
        _identity_failure(error, json_output)
    payload = _identity_rotation_plan_payload(plan, writes_performed=False)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Identity rotation prepared: "
            f"{plan.from_key_version}->{plan.to_key_version}; "
            f"bindings={plan.expected_binding_count}; fingerprint={plan.fingerprint}."
        )
        console.print("Read-only review; no identity binding or approval was written.")


@identity_control_app.command("approve")
def identity_approve(
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence-file", help="Exact owner-only envelope reviewed earlier."),
    ],
    evidence_fingerprint: Annotated[
        str,
        typer.Option("--evidence-fingerprint", help="Exact reviewed evidence SHA-256."),
    ],
    plan_fingerprint: Annotated[
        str,
        typer.Option("--plan-fingerprint", help="Exact SHA-256 emitted by identity prepare."),
    ],
    approved_at: Annotated[
        str,
        typer.Option(
            "--approved-at",
            help="Exact timezone-aware approval timestamp retained for replay.",
        ),
    ],
    confirmation: Annotated[
        IdentityRotationConfirmation,
        typer.Option("--confirm", help="Exact identity-rotation approval phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reserve one exact rotation approval without changing active bindings."""

    try:
        envelope = build_identity_evidence_reader(evidence_file).read(
            expected_fingerprint=evidence_fingerprint
        )
        services = build_identity_rotation_services()
        plan = services.prepare.execute(envelope.workspace_id, envelope.derivations)
        _require_identity_plan_fingerprint(plan.fingerprint, plan_fingerprint)
        approval_time = _parse_operator_time(approved_at, "identity rotation approval")
        _require_evidence_time(envelope, approval_time)
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        approved = services.approve.execute(
            plan,
            actor=resolved_actor,
            approved_at=approval_time,
            confirmation=confirmation,
        )
    except _IDENTITY_OPERATOR_ERRORS as error:
        _identity_failure(error, json_output)
    payload = {
        **_identity_rotation_plan_payload(plan, writes_performed=True),
        "approval_id": approved.approval.id,
        "approved_at": approved.approval.approved_at.isoformat(),
        "bindings_changed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Identity rotation approval reserved: plan={plan.id}; approval={approved.approval.id}."
        )


@identity_control_app.command("complete")
def identity_complete(
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence-file", help="Exact owner-only envelope reviewed earlier."),
    ],
    evidence_fingerprint: Annotated[
        str,
        typer.Option("--evidence-fingerprint", help="Exact reviewed evidence SHA-256."),
    ],
    plan_fingerprint: Annotated[
        str,
        typer.Option("--plan-fingerprint", help="Exact approved rotation-plan SHA-256."),
    ],
    approval_id: Annotated[
        str,
        typer.Option("--approval-id", help="Exact durable approval reservation identifier."),
    ],
    completed_at: Annotated[
        str,
        typer.Option(
            "--completed-at",
            help="Exact timezone-aware completion timestamp retained for replay.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Atomically complete only the exact plan and reserved approval."""

    try:
        envelope = build_identity_evidence_reader(evidence_file).read(
            expected_fingerprint=evidence_fingerprint
        )
        services = build_identity_rotation_services()
        plan = services.resolve.execute(
            plan_fingerprint,
            envelope.derivations,
        )
        completion_time = _parse_operator_time(completed_at, "identity rotation completion")
        _require_evidence_time(envelope, completion_time)
        resolved_actor = _control_operator_actor(None, required_role="publisher")
        result = services.complete.execute(
            plan,
            approval_id=approval_id,
            actor=resolved_actor,
            completed_at=completion_time,
        )
    except _IDENTITY_OPERATOR_ERRORS as error:
        _identity_failure(error, json_output)
    payload = {
        **_identity_rotation_plan_payload(plan, writes_performed=True),
        "approval_id": result.completion.approved.approval.id,
        "completion_id": result.completion.id,
        "completed_at": result.completion.completed_at.isoformat(),
        "verified_binding_count": result.completion.verified_binding_count,
        "historical_payloads_rewritten": result.completion.historical_payloads_rewritten,
        "replayed": result.replayed,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Identity rotation completed atomically: "
            f"bindings={result.completion.verified_binding_count}; "
            f"replayed={'yes' if result.replayed else 'no'}; "
            f"completion={result.completion.id}."
        )


@recipe_migration_app.command("prepare")
def recipe_migration_prepare(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque workspace target."),
    ],
    workflow_id: Annotated[
        str,
        typer.Option("--workflow-id", help="Exact completed replacement workflow target."),
    ],
    intent_fingerprint: Annotated[
        str,
        typer.Option("--intent-fingerprint", help="Exact current recipe intent SHA-256."),
    ],
    owner_actor_id: Annotated[
        str,
        typer.Option("--owner-actor-id", help="Exact opaque workflow owner target."),
    ],
    adapter: Annotated[
        WriteAdapterChoice,
        typer.Option("--adapter", help="Explicit live DataHub or persistent fake repository."),
    ] = WriteAdapterChoice.LIVE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare an SQL-free migration proposal without writing any target."""

    try:
        proposal = build_query_recipe_migration_preparer(
            adapter.value,
            workspace_id=workspace_id,
            owner_actor_id=owner_actor_id,
        ).execute(
            workflow_id=workflow_id,
            intent_fingerprint=intent_fingerprint,
        )
    except (DatabaseConfigurationError, RecipeError, ValueError) as error:
        _recipe_failure(error, json_output)
    payload = _recipe_migration_review_payload(
        proposal,
        adapter=adapter,
        writes_performed=False,
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Recipe migration prepared: "
            f"recipe={proposal.replacement.id}; "
            f"v{proposal.historical.recipe.version}->v{proposal.replacement.version}; "
            f"fingerprint={proposal.fingerprint}."
        )
        console.print("Read-only review; no SQL, preview rows, or publication write emitted.")


@recipe_migration_app.command("publish")
def recipe_migration_publish(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque workspace target."),
    ],
    workflow_id: Annotated[
        str,
        typer.Option("--workflow-id", help="Exact completed replacement workflow target."),
    ],
    intent_fingerprint: Annotated[
        str,
        typer.Option("--intent-fingerprint", help="Exact current recipe intent SHA-256."),
    ],
    owner_actor_id: Annotated[
        str,
        typer.Option("--owner-actor-id", help="Exact opaque workflow owner target."),
    ],
    proposal_fingerprint: Annotated[
        str,
        typer.Option(
            "--proposal-fingerprint",
            help="Exact SHA-256 emitted by recipe-migration prepare.",
        ),
    ],
    confirmation: Annotated[
        RecipePublicationConfirmation,
        typer.Option("--confirm", help="Exact recipe publication approval phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    adapter: Annotated[
        WriteAdapterChoice,
        typer.Option("--adapter", help="Explicit live DataHub or persistent fake repository."),
    ] = WriteAdapterChoice.LIVE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reprepare and publish only one unchanged, explicitly approved migration."""

    try:
        proposal = build_query_recipe_migration_preparer(
            adapter.value,
            workspace_id=workspace_id,
            owner_actor_id=owner_actor_id,
        ).execute(
            workflow_id=workflow_id,
            intent_fingerprint=intent_fingerprint,
        )
        if proposal.fingerprint != proposal_fingerprint:
            raise RecipeError(
                RecipeErrorCode.APPROVAL_MISMATCH,
                "recipe migration proposal fingerprint does not match current state",
            )
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        approved_at = datetime.now(UTC)
        replacement = proposal.replacement
        approval = RecipePublicationApproval(
            id=f"{resolved_actor}-{replacement.id}-v{replacement.version}",
            recipe_id=replacement.id,
            recipe_version=replacement.version,
            recipe_fingerprint=replacement.fingerprint,
            actor=resolved_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        result = build_query_recipe_migration_publisher(
            adapter.value,
            workspace_id=workspace_id,
        ).execute(proposal, approval)
    except (DatabaseConfigurationError, RecipeError, ValueError) as error:
        _recipe_failure(error, json_output)
    payload = {
        **_recipe_migration_review_payload(
            proposal,
            adapter=adapter,
            writes_performed=True,
        ),
        "ok": result.failure_code is None,
        "approval_id": approval.id,
        "publication": {
            "status": result.status.value,
            "recipe_fingerprint": result.recipe_fingerprint,
            "current_document_urn": result.current_document_urn,
            "versioned_document_urn": result.versioned_document_urn,
            "failure_code": result.failure_code,
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Recipe migration publication "
            f"{result.status.value}: recipe={proposal.replacement.id}; "
            f"version={proposal.replacement.version}; "
            f"document={result.current_document_urn}."
        )
    if result.failure_code is not None:
        raise typer.Exit(code=1)


@legacy_import_app.command("inspect")
def legacy_import_inspect(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Owner-controlled offline SQLite file; symlinks and active journals are rejected.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reserve a metadata-only dry run without importing or exposing source payloads."""

    try:
        plan = build_legacy_control_plane_import(source).prepare.execute(
            recorded_at=datetime.now(UTC)
        )
    except (
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        LegacyImportError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    reasons: dict[str, int] = {}
    for item in plan.items:
        if item.reason_code is not None:
            reasons[item.reason_code] = reasons.get(item.reason_code, 0) + 1
    payload = {
        "ok": True,
        "import_id": plan.id,
        "source_fingerprint": plan.source_fingerprint,
        "source_schema_fingerprint": plan.source_schema_fingerprint,
        "plan_fingerprint": plan.fingerprint,
        "counts": plan.counts.model_dump(mode="json"),
        "reason_counts": dict(sorted(reasons.items())),
        "dry_run_reserved": True,
        "target_rows_written": 0,
        "source_payloads_exposed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Legacy import dry run reserved: "
            f"items={plan.counts.total}; import={plan.counts.imported}; "
            f"quarantine={plan.counts.quarantined}; skipped={plan.counts.skipped}; "
            f"fingerprint={plan.fingerprint}."
        )


@legacy_import_app.command("apply")
def legacy_import_apply(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="The same owner-controlled offline SQLite file used for inspect.",
        ),
    ],
    plan_fingerprint: Annotated[
        str,
        typer.Option(
            "--plan-fingerprint",
            help="Exact SHA-256 emitted by legacy-import inspect.",
        ),
    ],
    confirmation: Annotated[
        LegacyImportConfirmation,
        typer.Option("--confirm", help="Exact legacy-import approval phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reinspect and atomically apply only the exact approved dry-run plan."""

    try:
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        services = build_legacy_control_plane_import(source)
        plan = services.inspect.execute()
        if plan.fingerprint != plan_fingerprint:
            raise LegacyImportError(
                LegacyImportErrorCode.APPROVAL_MISMATCH,
                "legacy import plan fingerprint does not match the reviewed dry run",
            )
        approved_at = datetime.now(UTC)
        approval = ApproveLegacyControlPlaneImport().execute(
            plan,
            actor=resolved_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        result = services.apply.execute(
            plan,
            approval,
            completed_at=approved_at,
        )
    except (
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        LegacyImportError,
        ValueError,
    ) as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "import_id": plan.id,
        "plan_fingerprint": plan.fingerprint,
        "approval_id": approval.id,
        "status": result.reservation.status.value,
        "counts": result.reservation.counts.model_dump(mode="json"),
        "source_payloads_exposed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Legacy import completed atomically: "
            f"import={result.reservation.counts.imported}; "
            f"quarantine={result.reservation.counts.quarantined}; "
            f"skipped={result.reservation.counts.skipped}; approval={approval.id}."
        )


@registry_control_app.command("prepare-activation")
def registry_prepare_activation(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    target_version: Annotated[
        int,
        typer.Option("--target-version", min=1, help="Exact immutable DataHub registry version."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare one compare-and-swap activation proposal without writing control state."""

    try:
        proposal = build_registry_activation_preparer(workspace_id=workspace_id).execute(
            target_version
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "proposal_fingerprint": proposal.fingerprint,
        "proposal": proposal.model_dump(mode="json"),
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Registry activation prepared: "
            f"target_version={proposal.target_registry_version}; "
            f"expected_generation={proposal.expected_generation}; "
            f"fingerprint={proposal.fingerprint}."
        )


@registry_control_app.command("activate")
def registry_activate(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    target_version: Annotated[
        int,
        typer.Option("--target-version", min=1, help="Exact immutable DataHub registry version."),
    ],
    proposal_fingerprint: Annotated[
        str,
        typer.Option(
            "--proposal-fingerprint",
            help="SHA-256 fingerprint returned by prepare-activation.",
        ),
    ],
    confirmation: Annotated[
        RegistryActivationConfirmation,
        typer.Option("--confirm", help="Exact forward-activation confirmation phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Commit only the exact freshly reconstructed and explicitly approved proposal."""

    try:
        proposal = build_registry_activation_preparer(workspace_id=workspace_id).execute(
            target_version
        )
        _require_exact_registry_fingerprint(
            actual=proposal.fingerprint,
            supplied=proposal_fingerprint,
            subject="activation proposal",
        )
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        approved_at = datetime.now(UTC)
        approval = PrepareRegistryActivationApproval().execute(
            proposal,
            actor=resolved_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        commit = build_registry_activation_committer().execute(
            proposal,
            approval,
            committed_at=approved_at,
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "proposal_fingerprint": proposal.fingerprint,
        "approval_id": approval.id,
        "commit": commit.model_dump(mode="json"),
        "writes_performed": True,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        pointer = commit.transition.active_pointer
        console.print(
            "Registry activated: "
            f"generation={pointer.generation}; version={pointer.registry_version}; "
            f"transition={commit.transition.id}."
        )


@registry_control_app.command("prepare-rollback")
def registry_prepare_rollback(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    transition_id: Annotated[
        str,
        typer.Option(
            "--transition-id",
            help="Previously active immutable transition selected as rollback target.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prepare a monotonic rollback generation without changing history or DataHub."""

    try:
        proposal = build_registry_rollback_preparer(workspace_id=workspace_id).execute(
            transition_id
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "proposal_fingerprint": proposal.fingerprint,
        "proposal": proposal.model_dump(mode="json"),
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Registry rollback prepared: "
            f"target_version={proposal.target_registry_version}; "
            f"next_generation={proposal.expected_generation + 1}; "
            f"fingerprint={proposal.fingerprint}."
        )


@registry_control_app.command("rollback")
def registry_rollback(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    transition_id: Annotated[
        str,
        typer.Option(
            "--transition-id",
            help="Previously active immutable transition selected as rollback target.",
        ),
    ],
    proposal_fingerprint: Annotated[
        str,
        typer.Option(
            "--proposal-fingerprint",
            help="SHA-256 fingerprint returned by prepare-rollback.",
        ),
    ],
    confirmation: Annotated[
        RegistryActivationConfirmation,
        typer.Option("--confirm", help="Exact rollback confirmation phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Commit rollback only as a new approved compare-and-swap generation."""

    try:
        proposal = build_registry_rollback_preparer(workspace_id=workspace_id).execute(
            transition_id
        )
        _require_exact_registry_fingerprint(
            actual=proposal.fingerprint,
            supplied=proposal_fingerprint,
            subject="rollback proposal",
        )
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        approved_at = datetime.now(UTC)
        approval = PrepareRegistryActivationApproval().execute(
            proposal,
            actor=resolved_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        commit = build_registry_activation_committer().execute(
            proposal,
            approval,
            committed_at=approved_at,
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "proposal_fingerprint": proposal.fingerprint,
        "approval_id": approval.id,
        "commit": commit.model_dump(mode="json"),
        "writes_performed": True,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        pointer = commit.transition.active_pointer
        console.print(
            "Registry rolled back through a new generation: "
            f"generation={pointer.generation}; version={pointer.registry_version}; "
            f"transition={commit.transition.id}."
        )


@registry_reconcile_app.command("inspect")
def registry_reconcile_inspect(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect authoritative and projected state without writing PostgreSQL or DataHub."""

    try:
        report = build_registry_reconciliation_inspector(workspace_id=workspace_id).execute(
            inspected_at=datetime.now(UTC)
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "report_fingerprint": report.fingerprint,
        "report": report.model_dump(mode="json"),
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        findings = ", ".join(finding.code.value for finding in report.findings)
        console.print(
            "Registry reconciliation inspected: "
            f"generation={report.active_pointer.generation}; "
            f"findings={findings}; fingerprint={report.fingerprint}."
        )


@registry_reconcile_app.command("repair")
def registry_reconcile_repair(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Exact opaque authenticated workspace identifier."),
    ],
    report_fingerprint: Annotated[
        str,
        typer.Option(
            "--report-fingerprint",
            help="SHA-256 fingerprint returned by reconcile inspect.",
        ),
    ],
    inspected_at: Annotated[
        str,
        typer.Option(
            "--inspected-at",
            help="Exact timezone-aware report timestamp returned by reconcile inspect.",
        ),
    ],
    confirmation: Annotated[
        RegistryReconciliationConfirmation,
        typer.Option("--confirm", help="Exact projection-repair confirmation phrase."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Repair only an unchanged, safe report with an explicit typed approval."""

    try:
        report_time = _parse_registry_report_time(inspected_at)
        report = build_registry_reconciliation_inspector(workspace_id=workspace_id).execute(
            inspected_at=report_time
        )
        _require_exact_registry_fingerprint(
            actual=report.fingerprint,
            supplied=report_fingerprint,
            subject="reconciliation report",
        )
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        approved_at = datetime.now(UTC)
        approval = PrepareRegistryReconciliationApproval().execute(
            report,
            actor=resolved_actor,
            approved_at=approved_at,
            confirmation=confirmation,
        )
        outcome = build_registry_reconciliation_repairer().execute(
            report,
            approval,
            occurred_at=approved_at,
        )
    except _REGISTRY_OPERATOR_ERRORS as error:
        _control_plane_failure(error, json_output)
    payload = {
        "ok": True,
        "report_fingerprint": report.fingerprint,
        "approval_id": approval.id,
        "outcome": outcome.model_dump(mode="json"),
        "writes_performed": True,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            "Registry reconciliation completed: "
            f"generation={outcome.generation}; status={outcome.status.value}; "
            f"transition={outcome.transition_id}."
        )


_REGISTRY_OPERATOR_ERRORS = (
    ControlPlaneMigrationError,
    DatabaseConfigurationError,
    PlanningPortError,
    RegistryControlError,
    RegistryPublicationError,
    ValueError,
)

_IDENTITY_OPERATOR_ERRORS = (
    ControlPlaneMigrationError,
    DatabaseConfigurationError,
    IdentityEvidenceError,
    IdentityRotationError,
    ValueError,
)


def _identity_evidence_review_payload(
    envelope: IdentityEvidenceEnvelopePort,
) -> dict[str, object]:
    from_key_versions = sorted(
        {derivation.previous.key_version for derivation in envelope.derivations}
    )
    to_key_versions = sorted(
        {derivation.current.key_version for derivation in envelope.derivations}
    )
    return {
        "ok": True,
        "workspace_id": envelope.workspace_id,
        "payload_fingerprint": envelope.payload_fingerprint,
        "signature_key_version": envelope.signature_key_version,
        "from_key_versions": from_key_versions,
        "to_key_versions": to_key_versions,
        "owner_count": len(envelope.derivations),
        "expected_binding_count": len(envelope.derivations) + 1,
        "issued_at": envelope.issued_at.isoformat(),
        "expires_at": envelope.expires_at.isoformat(),
        "writes_performed": False,
        "sensitive_evidence_exposed": False,
    }


def _identity_rotation_plan_payload(
    plan: IdentityRotationPlan,
    *,
    writes_performed: bool,
) -> dict[str, object]:
    return {
        "ok": True,
        "plan_id": plan.id,
        "plan_fingerprint": plan.fingerprint,
        "old_workspace_id": plan.old_workspace_id,
        "new_workspace_id": plan.new_workspace_id,
        "from_key_version": plan.from_key_version,
        "to_key_version": plan.to_key_version,
        "expected_state_revision": plan.expected_state_revision,
        "expected_binding_count": plan.expected_binding_count,
        "writes_performed": writes_performed,
        "sensitive_evidence_exposed": False,
    }


def _require_identity_plan_fingerprint(actual: str, supplied: str) -> None:
    if actual != supplied:
        raise IdentityRotationError(
            IdentityRotationErrorCode.APPROVAL_MISMATCH,
            "identity rotation plan fingerprint does not match current state",
        )


def _require_evidence_time(
    envelope: IdentityEvidenceEnvelopePort,
    timestamp: datetime,
) -> None:
    if timestamp < envelope.issued_at or timestamp >= envelope.expires_at:
        raise ValueError("operator timestamp is outside the verified evidence window")


def _parse_operator_time(value: str, subject: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{subject} time must be valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{subject} time must include a timezone")
    return parsed


def _identity_failure(
    error: (
        ControlPlaneMigrationError
        | DatabaseConfigurationError
        | IdentityEvidenceError
        | IdentityRotationError
        | ValueError
    ),
    json_output: bool,
) -> Never:
    raw_code = getattr(error, "code", "identity_control_configuration_error")
    code = raw_code.value if isinstance(raw_code, StrEnum) else str(raw_code)
    payload = {"ok": False, "code": code, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Identity control operation failed: {code}", style="red")
    raise typer.Exit(code=1)


def _control_operator_actor(
    supplied_actor: str | None,
    *,
    required_role: str,
) -> str:
    """Resolve audit identity without trusting a managed command-line assertion."""

    return resolve_control_operator_actor(
        supplied_actor,
        required_role=required_role,
    )


def _require_exact_registry_fingerprint(
    *,
    actual: str,
    supplied: str,
    subject: str,
) -> None:
    if supplied != actual:
        raise RegistryControlError(
            RegistryControlErrorCode.APPROVAL_MISMATCH,
            f"{subject} fingerprint does not match the current exact state",
        )


def _parse_registry_report_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("reconciliation report time must be valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reconciliation report time must include a timezone")
    return parsed


def _control_plane_failure(
    error: (
        ControlPlaneMigrationError
        | ControlPlaneOperationError
        | DatabaseConfigurationError
        | LegacyImportError
        | PlanningPortError
        | RegistryControlError
        | RegistryPublicationError
        | CatalogUseCaseError
        | ValidationError
        | ValueError
    ),
    json_output: bool,
) -> Never:
    raw_code = getattr(error, "code", "control_plane_configuration_error")
    code = raw_code.value if isinstance(raw_code, StrEnum) else str(raw_code)
    payload = {"ok": False, "code": code, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Control-plane operation failed: {code}", style="red")
    raise typer.Exit(code=1)


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


@app.command("registry-prepare")
@registry_control_app.command("prepare-version")
def registry_prepare(
    target_version: Annotated[
        int | None,
        typer.Option(
            "--target-version",
            min=1,
            help="Explicit immutable version to derive from the approved bundle.",
        ),
    ] = None,
    workspace_id: Annotated[
        str | None,
        typer.Option(
            "--workspace-id",
            help="Exact target workspace; required by managed operator composition.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Display the exact workspace-bound immutable payload before approval."""

    try:
        source = build_recorded_registry_publication_source(
            target_version=target_version,
            workspace_id=workspace_id,
        )
        registry = prepare_datahub_registry_version(source.registry, source.scope)
    except (
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        PlanningPortError,
        RegistryControlError,
        ValueError,
    ) as error:
        _registry_failure(error, json_output)
    payload = {
        "ok": True,
        "target": datahub_registry_document_urn(source.scope, registry.version),
        "source": registry.source,
        "registry_id": registry.registry_id,
        "version": registry.version,
        "catalog_scope": registry.catalog_scope,
        "fingerprint": registry.fingerprint,
        "models": len(registry.logical_context.models),
        "mappings": len(registry.mapping_set.mappings),
        "joins": len(registry.join_contracts.contracts),
        "decisions": len(semantic_registry_decision_ids(registry)),
        "writes_performed": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Prepared registry target={payload['target']}; "
            f"{payload['models']}/{payload['mappings']}/{payload['joins']}; "
            f"fingerprint={registry.fingerprint}. No write was performed."
        )


@app.command("registry-publish")
@registry_control_app.command("publish-version")
def registry_publish(
    confirmed_fingerprint: Annotated[
        str,
        typer.Option(
            "--fingerprint",
            help="Exact fingerprint previously emitted by registry-prepare.",
        ),
    ],
    confirmation: Annotated[
        RegistryPublicationConfirmation,
        typer.Option("--confirm", help="Exact approval phrase for this registry version."),
    ],
    target_version: Annotated[
        int | None,
        typer.Option(
            "--target-version",
            min=1,
            help="Explicit immutable version used by registry-prepare.",
        ),
    ] = None,
    workspace_id: Annotated[
        str | None,
        typer.Option(
            "--workspace-id",
            help="Exact target workspace; required by managed operator composition.",
        ),
    ] = None,
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help="Local identity check; managed actor identity comes from trusted configuration.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Publish the exact verified recorded registry as one immutable DataHub version."""

    try:
        resolved_actor = _control_operator_actor(actor, required_role="publisher")
        source = build_recorded_registry_publication_source(
            target_version=target_version,
            workspace_id=workspace_id,
        )
        registry = prepare_datahub_registry_version(source.registry, source.scope)
        approval = build_semantic_registry_publication_approval_preparer(
            workspace_id=workspace_id
        ).execute(
            registry,
            source.scope,
            actor=resolved_actor,
            approved_at=datetime.now(UTC),
            confirmed_fingerprint=confirmed_fingerprint,
            confirmation=confirmation,
        )
        result = build_semantic_registry_version_publisher(workspace_id=workspace_id).execute(
            registry,
            approval,
        )
    except (DatabaseConfigurationError, RegistryPublicationError, ValueError) as error:
        _registry_failure(error, json_output)
    payload = {"ok": result.status.value != "failed", **result.model_dump(mode="json")}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Registry publication {result.status.value}; "
            f"target={result.target}; fingerprint={result.fingerprint}."
        )
    if result.status.value == "failed":
        raise typer.Exit(code=1)


@app.command("registry-show")
def registry_show(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read one configured registry version through its selected adapter with no fallback."""

    try:
        scoped = build_semantic_registry().load()
    except (
        ControlPlaneMigrationError,
        DatabaseConfigurationError,
        PlanningPortError,
        RegistryControlError,
        ValueError,
    ) as error:
        _registry_failure(error, json_output)
    registry = scoped.registry
    payload = {
        "ok": True,
        "source": registry.source,
        "workspace_id": scoped.scope.workspace_id,
        "catalog_scope": registry.catalog_scope,
        "registry_id": registry.registry_id,
        "version": registry.version,
        "fingerprint": registry.fingerprint,
        "models": len(registry.logical_context.models),
        "mappings": len(registry.mapping_set.mappings),
        "joins": len(registry.join_contracts.contracts),
        "decisions": len(semantic_registry_decision_ids(registry)),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"Registry {registry.registry_id} v{registry.version} from {registry.source}; "
            f"{payload['models']}/{payload['mappings']}/{payload['joins']}; "
            f"fingerprint={registry.fingerprint}."
        )


def _registry_failure(
    error: (
        ControlPlaneMigrationError
        | DatabaseConfigurationError
        | PlanningPortError
        | RegistryControlError
        | RegistryPublicationError
        | ValueError
    ),
    json_output: bool,
) -> Never:
    raw_code = getattr(error, "code", "registry_configuration_error")
    code = raw_code.value if isinstance(raw_code, StrEnum) else str(raw_code)
    payload = {"ok": False, "code": code, "error": str(error)}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"Semantic registry operation failed: {code}", style="red")
    raise typer.Exit(code=1)


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


def _recipe_migration_review_payload(
    proposal: RecipeMigrationProposal,
    *,
    adapter: WriteAdapterChoice,
    writes_performed: bool,
) -> dict[str, object]:
    historical = proposal.historical.recipe
    replacement = proposal.replacement
    return {
        "ok": True,
        "adapter": adapter.value,
        "proposal_fingerprint": proposal.fingerprint,
        "intent_fingerprint": historical.intent_fingerprint,
        "historical": {
            "recipe_id": historical.id,
            "version": historical.version,
            "recipe_fingerprint": historical.fingerprint,
            "payload_fingerprint": proposal.historical_payload_fingerprint,
            "snapshot_fingerprint": proposal.historical_snapshot_fingerprint,
        },
        "replacement": {
            "recipe_id": replacement.id,
            "version": replacement.version,
            "recipe_fingerprint": replacement.fingerprint,
            "source_workflow_id": replacement.source_workflow_id,
            "staleness_reasons": [reason.value for reason in proposal.assessment.reasons],
        },
        "writes_performed": writes_performed,
        "sql_exposed": False,
        "preview_rows_exposed": False,
    }


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


def _natural_sql_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _natural_sql_predicate(
    value: LogicalBooleanPredicate | OutputBooleanPredicate,
) -> str:
    if value.kind is BooleanOperator.COMPARISON:
        comparison = value.comparison
        assert comparison is not None
        if isinstance(comparison, Filter):
            subject = comparison.field.root
            target = comparison.value
        else:
            subject = comparison.alias
            if comparison.compare_to_alias is not None:
                return f"{subject} {comparison.operator.value} alias:{comparison.compare_to_alias}"
            target = comparison.value
        if target is None:
            return f"{subject} {comparison.operator.value}"
        return f"{subject} {comparison.operator.value} {_natural_sql_value(target)}"
    rendered = tuple(_natural_sql_predicate(item) for item in value.operands)
    if value.kind is BooleanOperator.NOT:
        return f"NOT ({rendered[0]})"
    separator = f" {value.kind.value.upper()} "
    return "(" + separator.join(rendered) + ")"


def _natural_sql_advanced_rows(
    request: AdvancedAnalyticalRequest,
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = [
        ("Versión / modo", f"v{request.version} / {request.mode.value}"),
        ("Entidad primaria", request.primary_entity.root),
    ]
    if not request.fields:
        rows.append(("Campos", "ninguno"))
    for field_item in request.fields:
        alias = field_item.alias or field_item.field.root.rsplit(".", 1)[-1]
        attributes: list[str] = []
        if field_item.grain is not None:
            attributes.append(f"grain={field_item.grain.value}")
        if field_item.buckets:
            bands = ", ".join(
                (
                    f"{bucket.label}:["
                    f"{bucket.lower if bucket.lower is not None else '-∞'}, "
                    f"{bucket.upper if bucket.upper is not None else '+∞'})"
                )
                for bucket in field_item.buckets
            )
            attributes.append(f"buckets={bands}; else={field_item.else_label}")
        suffix = f" · {'; '.join(attributes)}" if attributes else ""
        rows.append(("Campo", f"{alias} = {field_item.field.root}{suffix}"))
    if not request.metrics:
        rows.append(("Métricas", "ninguna"))
    for metric_item in request.metrics:
        alias = metric_item.alias or (
            metric_item.operation.value
            if metric_item.field is None
            else (f"{metric_item.operation.value}_{metric_item.field.root.rsplit('.', 1)[-1]}")
        )
        source = "*" if metric_item.field is None else metric_item.field.root
        rendered = f"{alias} = {metric_item.operation.value}({source})"
        if metric_item.condition is not None:
            rendered += f" WHERE {_natural_sql_predicate(metric_item.condition)}"
        rows.append(("Métrica", rendered))
    rows.append(
        (
            "WHERE",
            _natural_sql_predicate(request.where) if request.where is not None else "ninguno",
        )
    )
    rows.append(("GROUP BY", ", ".join(request.group_by) or "ninguno"))
    rows.append(
        (
            "HAVING",
            _natural_sql_predicate(request.having) if request.having is not None else "ninguno",
        )
    )
    if not request.windows:
        rows.append(("Ventanas", "ninguna"))
    for window_item in request.windows:
        details = [window_item.operation.value]
        if window_item.source is not None:
            details.append(f"source={window_item.source}")
        if window_item.partition_by:
            details.append(f"partition_by={','.join(window_item.partition_by)}")
        if window_item.order_by:
            details.append(
                "order_by="
                + ",".join(
                    f"{order.alias} {order.direction.value}" for order in window_item.order_by
                )
            )
        for label, bounded_value in (
            ("buckets", window_item.buckets),
            ("offset", window_item.offset),
            ("preceding_rows", window_item.preceding_rows),
        ):
            if bounded_value is not None:
                details.append(f"{label}={bounded_value}")
        rows.append(("Ventana", f"{window_item.alias} = " + " · ".join(details)))
    ranking = tuple(
        item
        for item in request.windows
        if item.operation.value in {"row_number", "rank", "dense_rank", "ntile"}
    )
    if ranking:
        rows.append(
            (
                "Política de empates",
                "; ".join(
                    (
                        f"{item.alias}:{item.operation.value} por "
                        + ", ".join(
                            f"{order.alias} {order.direction.value}" for order in item.order_by
                        )
                    )
                    for item in ranking
                ),
            )
        )
    else:
        rows.append(("Política de empates", "no aplica"))
    rows.append(
        (
            "Filtro de salida",
            (
                _natural_sql_predicate(request.post_filter)
                if request.post_filter is not None
                else "ninguno"
            ),
        )
    )
    rows.append(
        (
            "Orden final",
            ", ".join(f"{item.alias} {item.direction.value}" for item in request.result_order_by)
            or "ninguno",
        )
    )
    rows.extend(
        (
            ("Agrupación", request.grouping.value),
            ("Límite", str(request.limit)),
        )
    )
    return tuple(rows)


def _natural_sql_simple_rows(
    request: AnalyticalRequest,
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = [
        ("Versión / modo", "v1 / aggregate"),
        ("Entidad primaria", request.primary_entity.root),
    ]
    if not request.dimensions:
        rows.append(("Campos", "ninguno"))
    for dimension_item in request.dimensions:
        suffix = (
            f" · grain={dimension_item.grain.value}" if dimension_item.grain is not None else ""
        )
        rows.append(("Campo", f"{dimension_item.field.root}{suffix}"))
    for metric_item in request.metrics:
        alias = metric_item.alias or (
            f"{metric_item.operation.value}_{metric_item.field.root.rsplit('.', 1)[-1]}"
        )
        rows.append(
            (
                "Métrica",
                f"{alias} = {metric_item.operation.value}({metric_item.field.root})",
            )
        )
    rows.append(
        (
            "WHERE",
            " AND ".join(
                (
                    f"{filter_item.field.root} {filter_item.operator.value}"
                    + (
                        ""
                        if filter_item.value is None
                        else f" {_natural_sql_value(filter_item.value)}"
                    )
                )
                for filter_item in request.filters
            )
            or "ninguno",
        )
    )
    rows.append(
        (
            "GROUP BY",
            ", ".join(item.field.root for item in request.dimensions) or "ninguno",
        )
    )
    rows.extend(
        (
            ("HAVING", "ninguno"),
            ("Ventanas", "ninguna"),
            ("Política de empates", "no aplica"),
            ("Filtro de salida", "ninguno"),
            (
                "Orden final",
                ", ".join(
                    f"{order_item.field.root} {order_item.direction.value}"
                    for order_item in request.order_by
                )
                or "ninguno",
            ),
            ("Agrupación", "standard"),
            ("Límite", str(request.limit)),
        )
    )
    return tuple(rows)


def _print_natural_sql_preview(
    preparation: NaturalSqlPreparation,
    *,
    preview_fingerprint: object,
    show_repeat_instruction: bool = True,
) -> None:
    preview = preparation.preview
    assert preview is not None
    request = preview.routed_request
    rows = (
        _natural_sql_advanced_rows(request)
        if isinstance(request, AdvancedAnalyticalRequest)
        else _natural_sql_simple_rows(request)
    )
    console.print("Interpretación gobernada preparada; todavía no existe SQL.", style="bold")
    interpretation = Table(title="Interpretación tipada confirmable")
    interpretation.add_column("Cláusula", style="cyan")
    interpretation.add_column("Valor aprobado")
    for clause, rendered in rows:
        interpretation.add_row(clause, rendered)
    console.print(interpretation)

    lineage = Table(title="Contexto y resolución gobernados")
    lineage.add_column("Elemento", style="cyan")
    lineage.add_column("Valor aprobado")
    lineage.add_row(
        "Modelos lógicos",
        ", ".join(item.id.root for item in preparation.semantic_context.models),
    )
    for semantic_field in preparation.semantic_context.fields:
        lineage.add_row(
            "Campo de contexto",
            (
                f"{semantic_field.id.root} · {semantic_field.canonical_type.value} "
                f"· {semantic_field.role.value}"
            ),
        )
    lineage.add_row(
        "Datasets físicos",
        ", ".join(item.root for item in preview.datasets),
    )
    lineage.add_row(
        "Joins aprobados",
        ", ".join(preview.join_contract_ids) or "ninguno",
    )
    for mapping_review in preview.mapping_reviews:
        lineage.add_row(
            "Mapping aprobado",
            (
                f"{mapping_review.logical_field.root} → {mapping_review.physical_field.root}; "
                f"confianza={mapping_review.confidence:.2f}; "
                f"evidencia={', '.join(mapping_review.evidence)}; "
                f"riesgos={', '.join(mapping_review.risks) or 'ninguno'}"
            ),
        )
    for join_review in preview.join_reviews:
        lineage.add_row(
            "Contrato aprobado",
            (
                f"{join_review.contract_id}; evidencia={', '.join(join_review.evidence)}; "
                f"riesgos={', '.join(join_review.risks) or 'ninguno'}"
            ),
        )
    for assumption in preview.assumptions:
        lineage.add_row(f"Supuesto: {assumption.code}", assumption.message)
    if preview.fanout_mitigations:
        for mitigation in preview.fanout_mitigations:
            lineage.add_row(
                "Fanout",
                (
                    f"{mitigation.contract_id}/{mitigation.metric_alias}: "
                    f"{mitigation.requested_operation.value} → "
                    f"{mitigation.applied_operation.value}; "
                    f"automatic={str(mitigation.automatic).lower()}; {mitigation.reason}"
                ),
            )
    else:
        lineage.add_row("Fanout", "ninguno; no se aplicó mitigación automática")
    console.print(lineage)
    console.print(f"Ruta: {preview.route.value}")
    console.print(f"Preview: {preview_fingerprint}")
    if show_repeat_instruction:
        console.print(
            "Revísala y repite el comando con --confirm-fingerprint <preview> "
            "para generar el SQL autónomo."
        )


def _confirm_natural_sql_preparation(
    runtime: NaturalSqlRuntimeServices,
    preparation: NaturalSqlPreparation,
) -> dict[str, object]:
    preview = preparation.preview
    if preview is None or preparation.token is None:
        raise NaturalSqlError(
            code=NaturalSqlErrorCode.CONFIRMATION_REQUIRED,
            message="natural SQL requires one exact signed preview before confirmation",
        )
    confirmation = AdvancedQueryConfirmation(
        action=AdvancedQueryConfirmationAction.CONFIRM,
        request_digest=preparation.request_digest,
        preview_fingerprint=preview.fingerprint,
        routed_request_fingerprint=preview.routed_request_fingerprint,
        token=preparation.token,
    )
    confirmed = runtime.confirm.execute(preparation, confirmation)
    generated = runtime.generate.execute(confirmed)
    return {
        "ok": True,
        "preview_fingerprint": preview.fingerprint,
        **generated.as_dict(),
    }


@app.command("sql-from-natural")
def sql_from_natural(
    text: Annotated[
        str,
        typer.Argument(help="Business request to convert into governed standalone SQL."),
    ],
    language: Annotated[
        UserLanguage,
        typer.Option("--language", help="Language of the business request."),
    ] = UserLanguage.SPANISH,
    confirm_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--confirm-fingerprint",
            help=(
                "Exact previously reviewed fingerprint. This mode prepares again and "
                "fails closed if the new interpretation differs."
            ),
        ),
    ] = None,
    review_and_confirm: Annotated[
        bool,
        typer.Option(
            "--review-and-confirm",
            help=(
                "Prepare once, print the exact preview, and ask interactively before "
                "generating from that same signed preparation."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Prepare or explicitly confirm copyable SQL; never execute the query."""

    if review_and_confirm and confirm_fingerprint is not None:
        raise typer.BadParameter(
            "--review-and-confirm cannot be combined with --confirm-fingerprint",
            param_hint="--review-and-confirm",
        )
    if review_and_confirm and json_output:
        raise typer.BadParameter(
            "--review-and-confirm is interactive and cannot be combined with --json",
            param_hint="--review-and-confirm",
        )

    payload: dict[str, object]
    try:
        runtime = build_natural_sql_runtime(
            principal=build_streamlit_principal(),
        )
        preparation = runtime.prepare.execute(
            AdvancedNaturalLanguageInput(text=text, language=language)
        )
        if preparation.preview is None:
            payload = {
                "ok": False,
                **preparation.as_dict(),
                "code": "natural_sql_ambiguity",
            }
        elif review_and_confirm:
            _print_natural_sql_preview(
                preparation,
                preview_fingerprint=preparation.preview.fingerprint,
                show_repeat_instruction=False,
            )
            accepted = typer.confirm(
                ("¿Confirmas exactamente este preview firmado y quieres generar el SQL autónomo?"),
                default=False,
            )
            if not accepted:
                console.print(
                    "Confirmación cancelada; no se compiló, generó ni ejecutó SQL.",
                    style="yellow",
                )
                return
            payload = _confirm_natural_sql_preparation(runtime, preparation)
        elif confirm_fingerprint is None:
            payload = {
                "ok": True,
                **preparation.as_dict(),
                "confirmation": {
                    "required": True,
                    "preview_fingerprint": preparation.preview.fingerprint,
                },
            }
        else:
            if confirm_fingerprint != preparation.preview.fingerprint:
                raise NaturalSqlError(
                    code=NaturalSqlErrorCode.CONFIRMATION_MISMATCH,
                    message="confirmed fingerprint differs from the current preview",
                )
            payload = _confirm_natural_sql_preparation(runtime, preparation)
    except (
        AdvancedQueryStudioPortError,
        DatabaseConfigurationError,
        NaturalSqlError,
        QueryCompilationError,
        SemanticResolutionError,
        SqlPolicyViolation,
        ValidationError,
        TypeError,
        ValueError,
    ) as error:
        code = getattr(error, "code", "natural_sql_failed")
        code_value = code.value if isinstance(code, StrEnum) else str(code)
        failure = {
            "ok": False,
            "code": code_value,
            "error": str(error),
            "executed": False,
        }
        if json_output:
            typer.echo(json.dumps(failure, indent=2))
        else:
            console.print(f"Natural SQL failed: {code_value}", style="red")
        raise typer.Exit(code=1) from error

    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        if payload["ok"] is False:
            raise typer.Exit(code=2)
        return
    if payload["ok"] is False:
        ambiguities = payload.get("ambiguities")
        assert isinstance(ambiguities, list)
        console.print(
            "La petición requiere aclaración: " + ", ".join(str(item) for item in ambiguities),
            style="yellow",
        )
        raise typer.Exit(code=2)
    sql = payload.get("sql")
    if isinstance(sql, str):
        console.print(sql)
        console.print(
            f"SHA-256: {payload['sha256']} · executed=false",
            style="green",
        )
        return
    confirmation_payload = payload["confirmation"]
    assert isinstance(confirmation_payload, dict)
    _print_natural_sql_preview(
        preparation,
        preview_fingerprint=confirmation_payload["preview_fingerprint"],
    )


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
    """Start, inspect, recover, decide, or retry the durable governed workflow."""

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
            draft = orchestrator.inspect(workflow_id)
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
            elif action is WorkflowActionChoice.RECOVER:
                recovery_operation = workflow_recovery_operation(draft)
                if recovery_operation is None:
                    raise WorkflowError(
                        code=WorkflowErrorCode.INVALID_TRANSITION,
                        message="workflow has no interrupted transition to recover",
                    )
                draft = orchestrator.recover_interrupted(
                    workflow_id,
                    expected_operation=recovery_operation,
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
