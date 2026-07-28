#!/usr/bin/env python3
"""Prepare and serve secret-free M28 internal-browser acceptance evidence.

``prepare`` creates two temporary synthetic PostgreSQL sources with the same
physical relation and connection ID but different databases, read-only roles,
route revisions, budgets, and aggregate results.  It exercises the real typed
compiler, independent SQL guard, routed connector, EXPLAIN preflight, and
preview, then removes both sources and every connector-secret file before
writing the owner-only public browser state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Final, NoReturn
from urllib.parse import quote

import psycopg
from psycopg import sql

from schemabridge.adapters.connectors.local_secrets import (
    OpaqueConnectorSecretRef,
    OwnerOnlyConnectorSecretResolver,
)
from schemabridge.adapters.connectors.routed_postgres import RoutedPostgresQueryConnector
from schemabridge.adapters.connectors.source_identity import (
    postgres_source_identity_fingerprint,
)
from schemabridge.adapters.sql.compiler import PostgresQueryCompiler
from schemabridge.adapters.sql.guard import SqlGlotPolicyGuard
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.query_execution import (
    QueryPreviewResult,
    ValidatedQuery,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostBudget,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.plans import (
    AggregateExpression,
    AllowedAsset,
    ColumnExpression,
    DatasetScan,
    OutputAlias,
    QueryPlan,
    QueryPolicy,
    RelationAlias,
    SelectItem,
)
from schemabridge.domain.requests import MetricOperation
from schemabridge.domain.workflows import fingerprint_payload
from schemabridge.entrypoints.streamlit.connector_acceptance import (
    M28_BROWSER_RELEASE_REF,
    M28_BROWSER_SCENARIO_ENV,
    M28_BROWSER_STATE_FILE_ENV,
    M28BrowserAcceptanceState,
    M28BrowserScenario,
    M28CostAssessmentEvidence,
    M28CostBudgetEvidence,
    M28ResultEvidence,
    M28ScenarioEvidence,
    is_m28_private_environment_key,
)

ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR: Final = ROOT / ".local/m28-browser-acceptance"
DEFAULT_SOURCE_ADMIN_DSN: Final = "postgresql://postgres:postgres@127.0.0.1:55433/postgres"
STATE_FILE: Final = "state.json"
CLEANUP_CONFIRMATION: Final = "REMOVE M28 BROWSER ACCEPTANCE"
_SETUP_SECRET_DIR: Final = "setup-connector-secrets"
_CONNECTION_ID: Final = CatalogConnectionId("warehouse-primary")
_DATABASE_NAME: Final = re.compile(r"^sb_m28_(?:a|b)_[0-9a-f]{12}$")
_READER_NAME: Final = re.compile(r"^sb_m28_(?:a|b)_reader_[0-9a-f]{8}$")
_MAX_STATE_BYTES: Final = 256 * 1024


class BrowserAcceptanceSetupError(RuntimeError):
    """One bounded setup/runtime failure with no private connector detail."""


@dataclass(frozen=True, slots=True)
class _SourceFixture:
    label: str
    database: str = field(repr=False)
    reader: str = field(repr=False)
    dsn: str = field(repr=False)
    row_count: int
    reference: OpaqueConnectorSecretRef = field(repr=False)


@dataclass
class _ExactRouteLoader:
    references: Mapping[str, OpaqueConnectorSecretRef] = field(repr=False)
    calls: int = 0

    def __call__(self, target: GovernedExecutionTarget) -> OpaqueConnectorSecretRef:
        self.calls += 1
        try:
            return self.references[target.fingerprint]
        except KeyError:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            ) from None


def _write_owner_only(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise BrowserAcceptanceSetupError(
            "An owner-only M28 acceptance file could not be created."
        ) from error


def _read_owner_only(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or not 0 < before.st_size <= maximum
        ):
            raise OSError
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != before.st_size
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise OSError
        return b"".join(chunks)
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "The owner-only M28 browser evidence is unavailable."
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resolve_state_dir(raw: Path) -> Path:
    allowed = (ROOT / ".local").resolve()
    resolved = raw.expanduser().resolve()
    if resolved == allowed or allowed not in resolved.parents:
        raise BrowserAcceptanceSetupError(
            "M28 browser state must be a dedicated directory under .local."
        )
    return resolved


def _prepare(state_dir: Path) -> M28BrowserAcceptanceState:
    resolved = _resolve_state_dir(state_dir)
    try:
        resolved.mkdir(mode=0o700, parents=False, exist_ok=False)
        secret_directory = resolved / _SETUP_SECRET_DIR
        secret_directory.mkdir(mode=0o700, exist_ok=False)
    except OSError as error:
        raise BrowserAcceptanceSetupError(
            "The dedicated M28 browser state directory must not already exist."
        ) from error

    fixtures: tuple[_SourceFixture, ...] = ()
    try:
        fixtures = _create_sources()
        state = _collect_real_evidence(fixtures, secret_directory)
        _drop_sources(fixtures)
        fixtures = ()
        _remove_setup_secret_directory(secret_directory)
        _write_owner_only(resolved / STATE_FILE, state.to_json())
        return state
    except BrowserAcceptanceSetupError:
        if fixtures:
            _drop_sources_best_effort(fixtures)
        _remove_failed_state_best_effort(resolved)
        raise
    except Exception as error:
        if fixtures:
            _drop_sources_best_effort(fixtures)
        _remove_failed_state_best_effort(resolved)
        raise BrowserAcceptanceSetupError(
            "The real M28 connector evidence could not be prepared."
        ) from error


def _create_sources() -> tuple[_SourceFixture, _SourceFixture]:
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_SOURCE_ADMIN_DATABASE_URL",
        DEFAULT_SOURCE_ADMIN_DSN,
    )
    suffix = secrets.token_hex(6)
    reader_suffix = suffix[:8]
    specifications = (
        ("a", 2, "tenant-a"),
        ("b", 3, "tenant-b"),
    )
    created_databases: list[str] = []
    created_roles: list[str] = []
    fixtures: list[_SourceFixture] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            for code, _count, _label in specifications:
                database = f"sb_m28_{code}_{suffix}"
                reader = f"sb_m28_{code}_reader_{reader_suffix}"
                secret_value = secrets.token_urlsafe(32)
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOINHERIT"
                    ).format(
                        sql.Identifier(reader),
                        sql.Literal(secret_value),
                    )
                )
                created_roles.append(reader)
                connection.execute(
                    sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                        sql.Identifier(reader)
                    )
                )
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER postgres").format(sql.Identifier(database))
                )
                created_databases.append(database)
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(database)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database),
                        sql.Identifier(reader),
                    )
                )
                dsn = _reader_dsn(
                    admin_dsn,
                    database=database,
                    reader=reader,
                    secret_value=secret_value,
                )
                fixtures.append(
                    _SourceFixture(
                        label=_label,
                        database=database,
                        reader=reader,
                        dsn=dsn,
                        row_count=_count,
                        reference=OpaqueConnectorSecretRef(f"execution.m28.{_label}.route"),
                    )
                )

        for fixture in fixtures:
            canary = f"m28_canary_{secrets.token_hex(18)}"
            with psycopg.connect(_database_dsn(admin_dsn, fixture.database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE public.routing_probe (
                        tenant_key text PRIMARY KEY,
                        approved_row integer NOT NULL,
                        hidden_canary text NOT NULL
                    )
                    """
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO public.routing_probe
                            (tenant_key, approved_row, hidden_canary)
                        VALUES (%s, %s, %s)
                        """,
                        tuple(
                            (
                                f"row-{index}",
                                index,
                                f"{canary}-{index}",
                            )
                            for index in range(1, fixture.row_count + 1)
                        ),
                    )
                connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                connection.execute("REVOKE ALL ON public.routing_probe FROM PUBLIC")
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        sql.Identifier(fixture.reader)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT SELECT ON public.routing_probe TO {}").format(
                        sql.Identifier(fixture.reader)
                    )
                )
        return fixtures[0], fixtures[1]
    except psycopg.Error as error:
        _drop_created_best_effort(
            admin_dsn,
            databases=tuple(created_databases),
            roles=tuple(created_roles),
        )
        raise BrowserAcceptanceSetupError(
            "The temporary synthetic M28 PostgreSQL sources could not be created."
        ) from error


def _collect_real_evidence(
    fixtures: tuple[_SourceFixture, ...],
    secret_directory: Path,
) -> M28BrowserAcceptanceState:
    if len(fixtures) != 2:
        raise BrowserAcceptanceSetupError("M28 source-pair evidence is incomplete.")
    alpha, beta = fixtures
    _write_connector_secret(secret_directory, alpha.reference, alpha)
    _write_connector_secret(secret_directory, beta.reference, beta)

    alpha_budget = _budget(
        explain_timeout_ms=2_000,
        max_total_cost=Decimal("100000"),
        max_estimated_rows=100_000,
    )
    beta_budget = _budget(
        explain_timeout_ms=1_750,
        max_total_cost=Decimal("90000"),
        max_estimated_rows=90_000,
    )
    alpha_target = _target(alpha, "m28-workspace-alpha", 1, alpha_budget)
    beta_target = _target(beta, "m28-workspace-beta", 7, beta_budget)
    rejected_budget = _budget(
        explain_timeout_ms=alpha_budget.explain_timeout_ms,
        max_total_cost=Decimal("0"),
        max_estimated_rows=alpha_budget.max_estimated_rows,
    )
    rejected_target = _target(alpha, "m28-workspace-alpha", 2, rejected_budget)

    loader = _ExactRouteLoader(
        {
            alpha_target.fingerprint: alpha.reference,
            beta_target.fingerprint: beta.reference,
            rejected_target.fingerprint: alpha.reference,
        }
    )
    connector = RoutedPostgresQueryConnector(
        reference_loader=loader,
        secret_resolver=OwnerOnlyConnectorSecretResolver(secret_directory.resolve()),
        allowed_fields=frozenset(),
        max_rows_limit=10,
        max_timeout_ms=2_000,
    )
    plan = _query_plan()
    policy = _query_policy()
    query_shape_fingerprint = fingerprint_payload(plan.model_dump(mode="json"))
    alpha_query = _validated_query(plan, policy, alpha_target)
    beta_query = _validated_query(plan, policy, beta_target)
    rejected_query = _validated_query(plan, policy, rejected_target)

    alpha_cost = connector.assess(alpha_query, alpha_target)
    alpha_result = connector.execute(alpha_query, target=alpha_target)
    beta_cost = connector.assess(beta_query, beta_target)
    beta_result = connector.execute(beta_query, target=beta_target)
    rejected_cost = connector.assess(rejected_query, rejected_target)
    if (
        alpha_cost.decision is not QueryCostDecision.ACCEPTED
        or beta_cost.decision is not QueryCostDecision.ACCEPTED
        or rejected_cost.decision is not QueryCostDecision.REJECTED
        or QueryCostRejectionCode.TOTAL_COST_EXCEEDED not in rejected_cost.rejection_codes
        or alpha_result.rows != ((alpha.row_count,),)
        or beta_result.rows != ((beta.row_count,),)
        or alpha_result.database_user == beta_result.database_user
    ):
        raise BrowserAcceptanceSetupError(
            "The real M28 routed preflight/preview evidence is incomplete."
        )

    rotation_baseline = connector.assess(alpha_query, alpha_target)
    if rotation_baseline.decision is not QueryCostDecision.ACCEPTED:
        raise BrowserAcceptanceSetupError("The M28 route-rotation baseline was not accepted.")
    _write_connector_secret(
        secret_directory,
        alpha.reference,
        beta,
        replace_existing=True,
    )
    try:
        connector.assess(alpha_query, alpha_target)
    except ConnectorTargetError as error:
        if error.code is not ConnectorTargetErrorCode.ROUTE_STALE:
            raise BrowserAcceptanceSetupError(
                "The M28 route-rotation failure was not sanitized."
            ) from None
    else:
        raise BrowserAcceptanceSetupError(
            "A rotated M28 route redirected an already confirmed target."
        )

    for code in (
        ConnectorTargetErrorCode.ROUTE_DISABLED,
        ConnectorTargetErrorCode.ROUTE_STALE,
        ConnectorTargetErrorCode.UNAVAILABLE,
    ):
        _exercise_closed_route_failure(
            code=code,
            query=alpha_query,
            target=alpha_target,
            secret_directory=secret_directory,
        )

    timeout_assessment = QueryCostAssessment(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=(QueryCostRejectionCode.TIMEOUT,),
        explain_timeout_ms=alpha_budget.explain_timeout_ms,
        target_fingerprint=alpha_target.fingerprint,
        budget_fingerprint=alpha_budget.fingerprint,
        cost_budget=alpha_budget,
    )
    scenarios = (
        _scenario(
            M28BrowserScenario.TENANT_A_ACCEPTED,
            target=alpha_target,
            connection_label="Warehouse primary A",
            assessment=alpha_cost,
            result=alpha_result,
            execution_allowed=True,
            blocker="Accepted preflight; execution requires explicit approval.",
        ),
        _scenario(
            M28BrowserScenario.TENANT_B_ACCEPTED,
            target=beta_target,
            connection_label="Warehouse primary B",
            assessment=beta_cost,
            result=beta_result,
            execution_allowed=True,
            blocker="Accepted preflight; execution requires explicit approval.",
        ),
        _scenario(
            M28BrowserScenario.COST_REJECTED,
            target=rejected_target,
            connection_label="Warehouse primary A",
            assessment=rejected_cost,
            blocker="The governed total-cost budget rejected this plan before preview.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_DISABLED,
            target=alpha_target,
            connection_label="Warehouse primary A",
            preflight_error_code=ConnectorTargetErrorCode.ROUTE_DISABLED.value,
            blocker="The current connector route is disabled; preview was not attempted.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_STALE,
            target=alpha_target,
            connection_label="Warehouse primary A",
            preflight_error_code=ConnectorTargetErrorCode.ROUTE_STALE.value,
            blocker="The confirmed target does not match the current route.",
        ),
        _scenario(
            M28BrowserScenario.ROUTE_UNAVAILABLE,
            target=alpha_target,
            connection_label="Warehouse primary A",
            preflight_error_code=ConnectorTargetErrorCode.UNAVAILABLE.value,
            blocker="The connector target is unavailable; preview was not attempted.",
        ),
        _scenario(
            M28BrowserScenario.EXPLAIN_TIMEOUT,
            target=alpha_target,
            connection_label="Warehouse primary A",
            assessment=timeout_assessment,
            preflight_error_code=QueryCostRejectionCode.TIMEOUT.value,
            blocker="The independent EXPLAIN timeout elapsed before preview.",
        ),
        _scenario(
            M28BrowserScenario.UNSUPPORTED_DIALECT,
            target=alpha_target,
            connection_label="Catalog only source",
            dialect="snowflake",
            connector_kind="catalog_only",
            target_fingerprint=fingerprint_payload(
                {
                    "unsupported_dialect": "snowflake",
                    "connection_id": _CONNECTION_ID.root,
                }
            ),
            preflight_error_code=ConnectorTargetErrorCode.DIALECT_UNSUPPORTED.value,
            blocker="No compiler, guard, preflight, or preview capability exists for this dialect.",
        ),
        _scenario(
            M28BrowserScenario.ROTATED_AFTER_CONFIRMATION,
            target=alpha_target,
            connection_label="Warehouse primary A",
            current_route_revision=2,
            preflight_error_code=ConnectorTargetErrorCode.ROUTE_STALE.value,
            blocker="Route rotation invalidated the confirmed target; a new approval is required.",
        ),
    )
    return M28BrowserAcceptanceState.create(
        real_preflight_calls=4,
        query_shape_fingerprint=query_shape_fingerprint,
        scenarios=scenarios,
    )


def _query_plan() -> QueryPlan:
    alias = RelationAlias("probe")
    source = ColumnExpression(
        relation=alias,
        field=PhysicalFieldRef("public.routing_probe.approved_row"),
    )
    return QueryPlan(
        root_scan=DatasetScan(
            dataset=PhysicalDatasetRef("public.routing_probe"),
            alias=alias,
        ),
        projections=(
            SelectItem(
                expression=AggregateExpression(
                    operation=MetricOperation.COUNT,
                    source=source,
                ),
                alias=OutputAlias("approved_rows"),
            ),
        ),
        limit=10,
    )


def _query_policy() -> QueryPolicy:
    return QueryPolicy(
        assets=(
            AllowedAsset(
                dataset=PhysicalDatasetRef("public.routing_probe"),
                columns=("approved_row",),
            ),
        ),
        max_tables=1,
        max_preview_rows=10,
        statement_timeout_ms=2_000,
    )


def _validated_query(
    plan: QueryPlan,
    policy: QueryPolicy,
    target: GovernedExecutionTarget,
) -> ValidatedQuery:
    compiled = PostgresQueryCompiler().compile(
        plan,
        max_preview_rows=policy.max_preview_rows,
        target=target,
    )
    return SqlGlotPolicyGuard(dialect=SourceDialect.POSTGRESQL).validate(
        compiled,
        policy,
        target=target,
    )


def _budget(
    *,
    explain_timeout_ms: int,
    max_total_cost: Decimal,
    max_estimated_rows: int,
) -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=explain_timeout_ms,
        max_response_bytes=65_536,
        max_total_cost=max_total_cost,
        max_estimated_rows=max_estimated_rows,
        max_plan_nodes=100,
        max_plan_depth=16,
        max_plan_width=4_096,
    )


def _target(
    fixture: _SourceFixture,
    workspace_id: str,
    route_revision: int,
    budget: QueryCostBudget,
) -> GovernedExecutionTarget:
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=_CONNECTION_ID,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=route_revision,
        route_fingerprint=fingerprint_payload(
            {
                "workspace_id": workspace_id,
                "connection_id": _CONNECTION_ID.root,
                "route_revision": route_revision,
            }
        ),
        expected_reader=fixture.reader,
        source_identity_fingerprint=_observed_source_identity(fixture.dsn),
        catalog_identity_fingerprint=fingerprint_payload(
            {
                "workspace_id": workspace_id,
                "catalog_kind": "synthetic-browser-evidence",
            }
        ),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _observed_source_identity(dsn: str) -> str:
    try:
        with psycopg.connect(dsn) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute(
                """
                SELECT
                    COALESCE(inet_server_addr()::TEXT, 'local_socket'),
                    COALESCE(inet_server_port(), 0),
                    current_database(),
                    current_user
                """
            ).fetchone()
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The temporary M28 source identity could not be observed."
        ) from error
    if row is None:
        raise BrowserAcceptanceSetupError("The temporary M28 source identity was unavailable.")
    return postgres_source_identity_fingerprint(
        server_address=str(row[0]),
        server_port=int(row[1]),
        database=str(row[2]),
        user=str(row[3]),
    )


def _exercise_closed_route_failure(
    *,
    code: ConnectorTargetErrorCode,
    query: ValidatedQuery,
    target: GovernedExecutionTarget,
    secret_directory: Path,
) -> None:
    def fail(_target: GovernedExecutionTarget) -> NoReturn:
        raise ConnectorTargetError(code, "connector route is unavailable")

    connector = RoutedPostgresQueryConnector(
        reference_loader=fail,
        secret_resolver=OwnerOnlyConnectorSecretResolver(secret_directory.resolve()),
        allowed_fields=frozenset(),
        max_rows_limit=10,
        max_timeout_ms=2_000,
    )
    try:
        connector.assess(query, target)
    except ConnectorTargetError as error:
        if error.code is code:
            return
    raise BrowserAcceptanceSetupError(
        "A closed M28 route failure did not stop before source preflight."
    )


def _scenario(
    scenario: M28BrowserScenario,
    *,
    target: GovernedExecutionTarget,
    connection_label: str,
    assessment: QueryCostAssessment | None = None,
    result: QueryPreviewResult | None = None,
    execution_allowed: bool = False,
    preflight_error_code: str | None = None,
    current_route_revision: int | None = None,
    dialect: str | None = None,
    connector_kind: str | None = None,
    target_fingerprint: str | None = None,
    blocker: str,
) -> M28ScenarioEvidence:
    workspace_label = (
        "Workspace B" if scenario is M28BrowserScenario.TENANT_B_ACCEPTED else "Workspace A"
    )
    return M28ScenarioEvidence(
        scenario=scenario,
        workspace_label=workspace_label,
        connection_label=connection_label,
        connection_id=target.connection_id.root,
        connector_kind=connector_kind or target.connector_kind.value,
        dialect=dialect or target.dialect.value,
        route_revision=target.route_revision,
        current_route_revision=current_route_revision,
        route_fingerprint=target.route_fingerprint,
        target_fingerprint=target_fingerprint or target.fingerprint,
        type_contract_fingerprint=target.type_contract_fingerprint,
        cost_budget=_public_budget(target.cost_budget),
        cost_assessment=(_public_assessment(assessment) if assessment is not None else None),
        preflight_error_code=preflight_error_code,
        execution_allowed=execution_allowed,
        result=_public_result(result) if result is not None else None,
        blocker=blocker,
    )


def _public_budget(budget: QueryCostBudget) -> M28CostBudgetEvidence:
    return M28CostBudgetEvidence(
        fingerprint=budget.fingerprint,
        explain_timeout_ms=budget.explain_timeout_ms,
        max_response_bytes=budget.max_response_bytes,
        max_total_cost=str(budget.max_total_cost),
        max_estimated_rows=budget.max_estimated_rows,
        max_plan_nodes=budget.max_plan_nodes,
        max_plan_depth=budget.max_plan_depth,
        max_plan_width=budget.max_plan_width,
    )


def _public_assessment(
    assessment: QueryCostAssessment,
) -> M28CostAssessmentEvidence:
    return M28CostAssessmentEvidence(
        decision=assessment.decision,
        rejection_codes=assessment.rejection_codes,
        total_cost=(str(assessment.total_cost) if assessment.total_cost is not None else None),
        estimated_root_rows=assessment.estimated_root_rows,
        plan_width=assessment.plan_width,
        plan_node_count=assessment.plan_node_count,
        plan_depth=assessment.plan_depth,
        response_bytes=assessment.response_bytes,
        read_only=assessment.read_only,
        explain_timeout_ms=assessment.explain_timeout_ms,
        fingerprint=assessment.fingerprint,
    )


def _public_result(result: QueryPreviewResult) -> M28ResultEvidence:
    if result.transaction_read_only is not True or result.truncated:
        raise BrowserAcceptanceSetupError(
            "The M28 aggregate preview did not preserve read-only bounds."
        )
    preview_fingerprint = fingerprint_payload(result.as_dict())
    return M28ResultEvidence(
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        preview_fingerprint=preview_fingerprint,
        database_user=result.database_user,
        transaction_read_only=result.transaction_read_only,
        statement_timeout_ms=result.statement_timeout_ms,
        truncated=result.truncated,
    )


def _write_connector_secret(
    directory: Path,
    reference: OpaqueConnectorSecretRef,
    fixture: _SourceFixture,
    *,
    replace_existing: bool = False,
) -> None:
    path = directory / reference.filename
    if replace_existing:
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise OSError
            path.unlink()
        except OSError as error:
            raise BrowserAcceptanceSetupError(
                "The temporary M28 connector route could not be rotated."
            ) from error
    _write_owner_only(
        path,
        (
            json.dumps(
                {
                    "format_version": 1,
                    "dialect": "postgresql",
                    "expected_reader": fixture.reader,
                    "dsn": fixture.dsn,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        ),
    )


def _drop_sources(fixtures: tuple[_SourceFixture, ...]) -> None:
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_SOURCE_ADMIN_DATABASE_URL",
        DEFAULT_SOURCE_ADMIN_DSN,
    )
    try:
        _drop_created(
            admin_dsn,
            databases=tuple(item.database for item in fixtures),
            roles=tuple(item.reader for item in fixtures),
        )
    except psycopg.Error as error:
        raise BrowserAcceptanceSetupError(
            "The temporary M28 PostgreSQL sources could not be removed."
        ) from error


def _drop_sources_best_effort(fixtures: tuple[_SourceFixture, ...]) -> None:
    admin_dsn = os.environ.get(
        "SCHEMABRIDGE_TEST_SOURCE_ADMIN_DATABASE_URL",
        DEFAULT_SOURCE_ADMIN_DSN,
    )
    _drop_created_best_effort(
        admin_dsn,
        databases=tuple(item.database for item in fixtures),
        roles=tuple(item.reader for item in fixtures),
    )


def _drop_created(
    admin_dsn: str,
    *,
    databases: tuple[str, ...],
    roles: tuple[str, ...],
) -> None:
    _validate_cleanup_identities(databases, roles)
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        for database in reversed(databases):
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
        for role in reversed(roles):
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _drop_created_best_effort(
    admin_dsn: str,
    *,
    databases: tuple[str, ...],
    roles: tuple[str, ...],
) -> None:
    try:
        _drop_created(admin_dsn, databases=databases, roles=roles)
    except (BrowserAcceptanceSetupError, psycopg.Error):
        return


def _validate_cleanup_identities(
    databases: tuple[str, ...],
    roles: tuple[str, ...],
) -> None:
    if (
        not databases
        or len(databases) > 2
        or len(roles) != len(databases)
        or any(_DATABASE_NAME.fullmatch(value) is None for value in databases)
        or any(_READER_NAME.fullmatch(value) is None for value in roles)
    ):
        raise BrowserAcceptanceSetupError("M28 temporary source cleanup identities are invalid.")


def _remove_setup_secret_directory(path: Path) -> None:
    actual = tuple(path.iterdir())
    if len(actual) != 2 or any(
        not item.is_file() or item.suffix != ".json" or stat.S_IMODE(item.stat().st_mode) != 0o600
        for item in actual
    ):
        raise BrowserAcceptanceSetupError(
            "The temporary M28 connector-secret directory is not exact."
        )
    for item in sorted(actual):
        item.unlink()
    path.rmdir()


def _remove_failed_state_best_effort(state_dir: Path) -> None:
    try:
        if not state_dir.exists() or not state_dir.is_dir():
            return
        for item in tuple(state_dir.iterdir()):
            if item.name == _SETUP_SECRET_DIR and item.is_dir():
                children = tuple(item.iterdir())
                if all(child.is_file() for child in children):
                    for child in children:
                        child.unlink()
                    item.rmdir()
            elif item.name == STATE_FILE and item.is_file():
                item.unlink()
        if not tuple(state_dir.iterdir()):
            state_dir.rmdir()
    except OSError:
        return


def _database_dsn(admin_dsn: str, database: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    parsed["dbname"] = database
    return psycopg.conninfo.make_conninfo("", **parsed)


def _reader_dsn(
    admin_dsn: str,
    *,
    database: str,
    reader: str,
    secret_value: str,
) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    host = str(parsed.get("host") or "127.0.0.1")
    port = str(parsed.get("port") or "5432")
    return (
        f"postgresql://{quote(reader, safe='')}:{quote(secret_value, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def _load_state(state_dir: Path) -> tuple[Path, M28BrowserAcceptanceState]:
    resolved = _resolve_state_dir(state_dir)
    raw = _read_owner_only(resolved / STATE_FILE, maximum=_MAX_STATE_BYTES)
    try:
        state = M28BrowserAcceptanceState.from_json(raw)
    except ValueError as error:
        raise BrowserAcceptanceSetupError(
            "The owner-only M28 browser evidence is invalid."
        ) from error
    return resolved, state


def _clean_process_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("LANG", "LC_ALL"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _serve_streamlit(
    state_dir: Path,
    *,
    scenario: M28BrowserScenario,
    port: int = 8510,
) -> NoReturn:
    if not isinstance(scenario, M28BrowserScenario):
        raise BrowserAcceptanceSetupError("M28 browser scenario is invalid.")
    if type(port) is not int or not 1_024 <= port <= 65_535:
        raise BrowserAcceptanceSetupError("M28 browser port is invalid.")
    resolved, _state = _load_state(state_dir)
    executable = ROOT / ".venv/bin/streamlit"
    app = ROOT / "scripts/m28_connector_scenario_app.py"
    environment = _clean_process_environment()
    environment.update(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "development",
            "SCHEMABRIDGE_AUTH_MODE": "local-demo",
            "SCHEMABRIDGE_RELEASE_REF": M28_BROWSER_RELEASE_REF,
            M28_BROWSER_STATE_FILE_ENV: str(resolved / STATE_FILE),
            M28_BROWSER_SCENARIO_ENV: scenario.value,
        }
    )
    if any(is_m28_private_environment_key(key) for key in environment):
        raise BrowserAcceptanceSetupError("M28 browser environment contains a private capability.")
    arguments = (
        str(executable),
        "run",
        str(app),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--server.fileWatcherType",
        "none",
        "--browser.gatherUsageStats",
        "false",
    )
    os.chdir(ROOT)
    os.execve(executable, arguments, environment)


def _cleanup(state_dir: Path, confirmation: str) -> None:
    if confirmation != CLEANUP_CONFIRMATION:
        raise BrowserAcceptanceSetupError("The exact M28 cleanup confirmation is required.")
    resolved, _state = _load_state(state_dir)
    actual = tuple(resolved.iterdir())
    if len(actual) != 1 or actual[0].name != STATE_FILE or not actual[0].is_file():
        raise BrowserAcceptanceSetupError(
            "The M28 state directory contains unexpected material and was retained."
        )
    actual[0].unlink()
    resolved.rmdir()


def _safe_summary(state: M28BrowserAcceptanceState) -> str:
    return json.dumps(
        {
            "evidence_fingerprint": state.evidence_fingerprint,
            "real_preflight_calls": state.real_preflight_calls,
            "real_preview_calls": state.real_preview_calls,
            "scenario_decisions": {
                item.scenario.value: (
                    item.cost_assessment.decision.value
                    if item.cost_assessment is not None
                    else item.preflight_error_code
                )
                for item in state.scenarios
            },
        },
        sort_keys=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the isolated M28 browser-acceptance runtime.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("status")
    streamlit = commands.add_parser("streamlit")
    streamlit.add_argument(
        "--scenario",
        choices=tuple(item.value for item in M28BrowserScenario),
        default=M28BrowserScenario.TENANT_A_ACCEPTED.value,
    )
    streamlit.add_argument("--port", type=int, default=8510)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            print(_safe_summary(_prepare(arguments.state_dir)))
        elif arguments.command == "status":
            _resolved, state = _load_state(arguments.state_dir)
            print(_safe_summary(state))
        elif arguments.command == "streamlit":
            _serve_streamlit(
                arguments.state_dir,
                scenario=M28BrowserScenario(str(arguments.scenario)),
                port=int(arguments.port),
            )
        elif arguments.command == "cleanup":
            _cleanup(arguments.state_dir, str(arguments.confirm))
            print("M28 browser acceptance owner-only public state removed.")
        else:
            raise BrowserAcceptanceSetupError("The M28 browser acceptance command is invalid.")
    except BrowserAcceptanceSetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
