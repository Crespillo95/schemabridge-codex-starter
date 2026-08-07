"""DataHub-only M22 semantic-registry planning and execution proof."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from tests.unit.test_registry_publication_v2 import (
    NOW as M34_NOW,
)
from tests.unit.test_registry_publication_v2 import (
    OPAQUE_ORDERS_URN,
    _authorization,
    _proposal,
)

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubGovernedSemanticRegistry,
    DataHubHttpRegistryReadClient,
    DataHubObservedSemanticRegistryPublisher,
    DataHubRegistryReadConfig,
    DataHubRegistryWriteConfig,
    DataHubWriterRegistryVersionReader,
)
from schemabridge.adapters.storage.publication_audit import SqlitePublicationAuditStore
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    GovernedQueryResult,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedDimensionInput,
    GuidedFilterInput,
    GuidedMetricInput,
    GuidedOrderInput,
    GuidedRequestCase,
    GuidedRequestInput,
    build_demo_guided_input,
)
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.bootstrap import (
    build_governed_request_executor,
    build_governed_request_preparer,
    build_semantic_registry,
    build_streamlit_principal,
)
from schemabridge.config import Settings
from schemabridge.domain.publication_audit import (
    PublicationAuditOutcome,
    PublicationFamily,
    PublicationTargetAuditRecord,
)
from schemabridge.domain.registry_control import RegistryVersionTrust
from schemabridge.domain.registry_publication import assemble_publishable_registry_version
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    datahub_registry_document_urn,
    semantic_registry_decision_ids,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
MCP_CREDENTIALS = ROOT / ".local/datahub/mcp.env"
WRITER_CREDENTIALS = ROOT / ".local/datahub/writer.env"
PUBLISHER_CREDENTIALS = ROOT / ".local/datahub/registry-publisher.env"
_M34_V2_PROPERTIES = {
    "schemabridge.registryFormatVersion",
    "schemabridge.registryId",
    "schemabridge.registryVersion",
    "schemabridge.registryCatalogScope",
    "schemabridge.registryWorkspaceDigest",
    "schemabridge.registryFingerprint",
    "schemabridge.registryDecisionIds",
    "schemabridge.registrySnapshot",
    "schemabridge.registryPublicationCandidate",
    "schemabridge.registryPublicationAuthorization",
    "schemabridge.registryPublicationAudit",
}


@pytest.fixture
def live_settings(reader_dsn: str, tmp_path: Path) -> Settings:
    """Select live DataHub and PostgreSQL while making the manifest unavailable."""

    if not MCP_CREDENTIALS.is_file():
        pytest.skip("DataHub reader credentials are absent; run make datahub-provision-mcp")
    return Settings.model_validate(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_CATALOG_MODE": "live",
            "SCHEMABRIDGE_REGISTRY_MODE": "live",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "live",
            "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": True,
            "SCHEMABRIDGE_LOCAL_WORKSPACE": "local-demo",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH": MCP_CREDENTIALS,
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_MANIFEST_PATH": (
                tmp_path / "manifest-must-not-be-read.yml"
            ),
            "DATABASE_URL": reader_dsn,
            "POSTGRES_READER_USER": "schemabridge_reader",
            "SCHEMABRIDGE_STATEMENT_TIMEOUT_MS": 5_000,
            "SCHEMABRIDGE_MAX_QUERY_ROWS": 500,
            "SCHEMABRIDGE_MAX_QUERY_TABLES": 3,
        }
    )


def _load_fresh_live_registry(
    settings: Settings,
) -> tuple[DataHubGovernedSemanticRegistry, ScopedSemanticRegistrySnapshot]:
    registry = build_semantic_registry(
        repository_root=ROOT,
        settings=settings,
    )
    assert isinstance(registry, DataHubGovernedSemanticRegistry)
    try:
        snapshot = registry.load()
    except PlanningPortError as error:
        if error.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE:
            pytest.skip("Local DataHub is unavailable; run make datahub-start")
        raise
    return registry, snapshot


def _require_postgres(reader_dsn: str) -> None:
    try:
        with (
            psycopg.connect(reader_dsn, connect_timeout=3) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
            observed = cursor.fetchone()
    except psycopg.OperationalError:
        pytest.skip("Local synthetic PostgreSQL is unavailable; run make demo-start")
    assert observed == ("schemabridge_reader", "on")


def _live_runtime(
    settings: Settings,
) -> tuple[BuildGuidedRequest, ExecuteGovernedRequest]:
    registry, _ = _load_fresh_live_registry(settings)
    prepare = build_governed_request_preparer(
        repository_root=ROOT,
        settings=settings,
        registry=registry,
    )
    return (
        BuildGuidedRequest(registry),
        build_governed_request_executor(
            execution_kind="live",
            repository_root=ROOT,
            settings=settings,
            prepare=prepare,
        ),
    )


def _assert_guarded_read_only_result(result: GovernedQueryResult) -> None:
    assert result.policy_status == "accepted"
    assert result.policy_findings == ()
    assert result.sql.lstrip().startswith("SELECT")
    assert result.preview.database_user == "schemabridge_reader"
    assert result.preview.transaction_read_only is True
    assert result.preview.statement_timeout_ms == 5_000
    assert result.rejected_sources.database_user == "schemabridge_reader"
    assert result.rejected_sources.transaction_read_only is True
    assert result.rejected_sources.statement_timeout_ms == 5_000
    assert result.resolved_plan.context_source.startswith("datahub:")
    assert result.resolved_plan.query_policy.max_tables == 3
    assert len(result.resolved_plan.query_plan.joins) <= 2


def test_fresh_live_adapter_reconstructs_complete_read_only_registry_without_manifest(
    live_settings: Settings,
) -> None:
    first, first_snapshot = _load_fresh_live_registry(live_settings)
    fresh, fresh_snapshot = _load_fresh_live_registry(live_settings)
    registry = fresh_snapshot.registry

    assert first is not fresh
    assert first.loads == fresh.loads == 1
    assert first_snapshot.registry.fingerprint == registry.fingerprint
    assert registry.registry_id == "synthetic_enterprise"
    assert registry.version == 1
    assert registry.catalog_scope == "synthetic-demo"
    assert registry.source.startswith("datahub:schemabridge-semantic-registry-")
    assert len(registry.logical_context.models) == 7
    assert len(registry.mapping_set.mappings) == 31
    assert len(registry.join_contracts.contracts) == 5
    assert len(semantic_registry_decision_ids(registry)) == 37
    assert len(registry.fingerprint) == 64
    assert (
        fresh_snapshot.scope.workspace_id
        == build_streamlit_principal(settings=live_settings).workspace_id
    )
    assert not live_settings.semantic_registry_manifest_path.exists()

    reader = DataHubHttpRegistryReadClient(fresh.config.server, fresh.config.token)
    assert not hasattr(reader, "upsert_document")
    identity = reader.identity(
        datahub_registry_document_urn(fresh.scope, fresh.version),
    )
    assert identity.granted_target_edit_privileges == frozenset()
    assert identity.granted_platform_mutation_privileges <= frozenset(
        {"generatePersonalAccessTokens"}
    )


def test_registry_publish_cli_replay_is_idempotent_across_fresh_processes(
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    if not WRITER_CREDENTIALS.is_file():
        pytest.skip("DataHub writer credentials are absent; run make datahub-provision-writer")
    _, snapshot = _load_fresh_live_registry(live_settings)
    audit_path = tmp_path / "fresh-process-audit.sqlite3"
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.update(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_CATALOG_MODE": "recorded",
            "SCHEMABRIDGE_REGISTRY_MODE": "recorded",
            "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
            "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
            "SCHEMABRIDGE_DRAFT_STORE_PATH": str(audit_path),
            "SCHEMABRIDGE_LOCAL_WORKSPACE": "local-demo",
            # Exercise a distinct authenticated operator so this isolated empty ledger does not
            # reissue the existing operator's stable approval ID with a different timestamp.
            "SCHEMABRIDGE_LOCAL_SUBJECT": "integration-registry-replay-operator",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_MANIFEST_PATH": (
                "demo/ground_truth/registries/manifest.yml"
            ),
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_VERSION": "1",
        }
    )
    command = (
        str(ROOT / ".venv/bin/schemabridge"),
        "registry-publish",
        "--fingerprint",
        snapshot.registry.fingerprint,
        "--confirm",
        "publish-approved-registry-version",
        "--json",
    )

    outputs: list[dict[str, object]] = []
    for _ in range(2):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(json.loads(completed.stdout))

    assert [output["status"] for output in outputs] == [
        "already_current",
        "already_current",
    ]
    assert outputs[0]["approval_id"] == outputs[1]["approval_id"]
    first_audit = outputs[0]["audit_record"]
    second_audit = outputs[1]["audit_record"]
    assert isinstance(first_audit, dict)
    assert isinstance(second_audit, dict)
    assert first_audit["approved_at"] == second_audit["approved_at"]
    records = SqlitePublicationAuditStore(audit_path).list_for_approval(
        str(outputs[0]["approval_id"])
    )
    assert [record.outcome for record in records] == [
        PublicationAuditOutcome.NOT_ATTEMPTED,
        PublicationAuditOutcome.ALREADY_CURRENT,
        PublicationAuditOutcome.ALREADY_CURRENT,
    ]


def test_live_datahub_v2_publication_has_exact_opaque_authority_and_immutable_readback() -> None:
    if not PUBLISHER_CREDENTIALS.is_file() or not MCP_CREDENTIALS.is_file():
        pytest.skip("DataHub reader or document-only M34 publisher credentials are absent")
    writer_config = DataHubRegistryWriteConfig.from_env_file(PUBLISHER_CREDENTIALS)
    reader_config = DataHubRegistryReadConfig.from_env_file(MCP_CREDENTIALS)
    scope = SemanticRegistryScope(
        workspace_id="workspace-m34-datahub-integration",
        catalog_scope="postgres.m34-integration",
        registry_id="m34_datahub_integration",
    )
    proposal = _proposal(
        scope=scope,
        proposal_id="proposal-m34-datahub-integration-v1",
        draft_id="draft-m34-datahub-integration-v1",
        model_decision="decision-model-m34-datahub-integration-v1",
        mapping_decision="decision-mapping-m34-datahub-integration-v1",
        observed_urn=OPAQUE_ORDERS_URN,
        asset_id=OPAQUE_ORDERS_URN,
    )
    candidate = assemble_publishable_registry_version(proposal, base=None)
    authorization = _authorization(
        candidate,
        actor="publisher-m34-datahub-integration",
        approved_at=M34_NOW,
    )

    publisher = DataHubObservedSemanticRegistryPublisher(config=writer_config)
    try:
        first = publisher.publish(candidate, authorization, observed_at=M34_NOW)
    except RegistryPublicationError as error:
        if error.code is RegistryPublicationErrorCode.CATALOG_UNAVAILABLE:
            pytest.skip("Local DataHub is unavailable; run make datahub-start")
        raise

    assert first.status in {"published", "already_current"}
    assert first.receipt is not None
    assert first.receipt.observed_authorization_id == authorization.id
    assert first.receipt.related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert candidate.registry.physical_bindings[0].observed_datahub_asset_urn == OPAQUE_ORDERS_URN
    assert "sales.orders" not in OPAQUE_ORDERS_URN

    independent = DataHubObservedSemanticRegistryPublisher(config=writer_config).observe(
        candidate,
        authorization,
        observed_at=M34_NOW + timedelta(seconds=1),
    )
    assert independent.status == "already_current"
    assert independent.receipt is not None
    assert independent.receipt.observed_authorization_id == authorization.id
    assert independent.receipt.related_asset_urns == (OPAQUE_ORDERS_URN,)

    strict = DataHubWriterRegistryVersionReader(config=writer_config).load_version(scope, 1)
    assert strict.trust is RegistryVersionTrust.STRICT
    assert strict.publication_approval_id == authorization.id
    assert strict.snapshot.registry == candidate.registry
    try:
        reader_snapshot = DataHubGovernedSemanticRegistry(
            config=reader_config,
            _scope=scope,
            version=1,
        ).load()
    except PlanningPortError as error:
        if error.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE:
            pytest.skip("Local DataHub is unavailable; run make datahub-start")
        raise
    assert reader_snapshot.registry == candidate.registry

    client = DataHubHttpRegistryReadClient(writer_config.server, writer_config.token)
    before_retry = client.get_document(candidate.target)
    assert before_retry is not None
    assert before_retry.removed is False
    assert before_retry.related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert set(before_retry.custom_properties) == _M34_V2_PROPERTIES
    properties = before_retry.custom_properties
    assert json.loads(properties["schemabridge.registrySnapshot"]) == (
        candidate.registry.model_dump(mode="json")
    )
    assert json.loads(properties["schemabridge.registryPublicationCandidate"]) == (
        candidate.manifest.model_dump(mode="json")
    )
    assert json.loads(properties["schemabridge.registryPublicationAuthorization"]) == (
        authorization.model_dump(mode="json")
    )
    audit = PublicationTargetAuditRecord.model_validate(
        json.loads(properties["schemabridge.registryPublicationAudit"])
    )
    assert audit.family is PublicationFamily.REGISTRY
    assert audit.operation == "versioned_document_v2"
    assert audit.target == candidate.target
    assert audit.approval_id == authorization.id
    assert audit.actor == authorization.actor_id
    assert audit.approved_at == authorization.approved_at
    assert audit.new_fingerprint == candidate.registry.fingerprint
    assert audit.outcome is PublicationAuditOutcome.SUCCEEDED
    assert audit.decision_ids == candidate.active_decision_ids
    assert audit.reason_code is None

    retry = DataHubObservedSemanticRegistryPublisher(config=writer_config).publish(
        candidate,
        authorization,
        observed_at=M34_NOW + timedelta(seconds=2),
    )
    assert retry.status == "already_current"
    assert retry.receipt is not None
    assert retry.receipt.observed_authorization_id == authorization.id
    assert client.get_document(candidate.target) == before_retry

    conflicting_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,opaque-conflict-4d61,PROD)"
    conflicting = assemble_publishable_registry_version(
        _proposal(
            scope=scope,
            model_id="Invoice",
            logical_field="Invoice.invoice_id",
            physical_field="billing.invoices.invoice_id",
            proposal_id="proposal-m34-datahub-conflict-v1",
            draft_id="draft-m34-datahub-conflict-v1",
            model_decision="decision-model-m34-datahub-conflict-v1",
            mapping_decision="decision-mapping-m34-datahub-conflict-v1",
            observed_urn=conflicting_urn,
            asset_id=conflicting_urn,
        ),
        base=None,
    )
    conflicting_authorization = _authorization(
        conflicting,
        actor="publisher-m34-datahub-integration",
        approved_at=M34_NOW,
    )
    with pytest.raises(RegistryPublicationError) as conflict:
        DataHubObservedSemanticRegistryPublisher(config=writer_config).publish(
            conflicting,
            conflicting_authorization,
            observed_at=M34_NOW + timedelta(seconds=3),
        )
    assert conflict.value.code is RegistryPublicationErrorCode.CONFLICT
    assert client.get_document(candidate.target) == before_retry


def test_datahub_only_north_star_is_guarded_and_executes_read_only(
    live_settings: Settings,
    reader_dsn: str,
) -> None:
    _require_postgres(reader_dsn)
    builder, executor = _live_runtime(live_settings)

    result = executor.execute(
        builder.execute(build_demo_guided_input(GuidedRequestCase.NORTH_STAR))
    )

    assert [contract.id for contract in result.resolved_plan.selected_contracts] == [
        "customer_to_account_holder"
    ]
    assert result.preview.columns == ("registration_date", "secondary_holder_customers")
    assert result.preview.rows == (
        (date(2026, 1, 1), 2),
        (date(2026, 1, 2), 1),
        (date(2026, 1, 3), 1),
    )
    assert [(item.source_value, item.code.value) for item in result.rejected_sources.records] == [
        ("127.5", "non_integral_identifier"),
        ("NaN", "non_finite_identifier"),
        (None, "null_join_key"),
    ]
    _assert_guarded_read_only_result(result)
    assert not live_settings.semantic_registry_manifest_path.exists()


def test_datahub_only_commerce_is_guarded_and_executes_read_only(
    live_settings: Settings,
    reader_dsn: str,
) -> None:
    _require_postgres(reader_dsn)
    builder, executor = _live_runtime(live_settings)
    request = builder.execute(
        GuidedRequestInput(
            primary_entity="SaleLine",
            dimensions=(
                GuidedDimensionInput("Product.category"),
                GuidedDimensionInput("SalesOrder.ordered_at", "month"),
            ),
            metrics=(
                GuidedMetricInput("sum", "SaleLine.net_amount", "net_revenue"),
                GuidedMetricInput("sum", "SaleLine.quantity", "units"),
            ),
            filters=(
                GuidedFilterInput(
                    "SalesOrder.order_status",
                    "equals",
                    "COMPLETED",
                ),
            ),
            order_by=(
                GuidedOrderInput("Product.category"),
                GuidedOrderInput("SalesOrder.ordered_at"),
            ),
            limit=100,
        )
    )

    result = executor.execute(request)

    assert [contract.id for contract in result.resolved_plan.selected_contracts] == [
        "product_to_sale_line",
        "sales_order_to_sale_line",
    ]
    assert result.preview.rows == (
        ("BOOKS", date(2026, 1, 1), Decimal("1496.45"), 25),
        ("BOOKS", date(2026, 2, 1), Decimal("988.00"), 16),
        ("ELECTRONICS", date(2026, 1, 1), Decimal("938.65"), 23),
        ("ELECTRONICS", date(2026, 2, 1), Decimal("440.85"), 7),
        ("HOME", date(2026, 1, 1), Decimal("529.70"), 13),
        ("HOME", date(2026, 2, 1), Decimal("637.80"), 12),
        ("SPORTS", date(2026, 1, 1), Decimal("988.80"), 16),
        ("SPORTS", date(2026, 2, 1), Decimal("852.00"), 11),
    )
    assert [(item.source_value, item.code.value) for item in result.rejected_sources.records] == [
        ("-3", "negative_identifier"),
        (None, "null_join_key"),
        ("", "malformed_identifier"),
        ("-1001", "negative_identifier"),
        ("bad-1060", "malformed_identifier"),
        (None, "null_join_key"),
    ]
    _assert_guarded_read_only_result(result)
    assert not live_settings.semantic_registry_manifest_path.exists()
