"""Real-source proof that one connector routes identical tenant queries separately."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from schemabridge.adapters.connectors.local_secrets import (
    OpaqueConnectorSecretRef,
    OwnerOnlyConnectorSecretResolver,
)
from schemabridge.adapters.connectors.routed_postgres import (
    RoutedPostgresQueryConnector,
)
from schemabridge.adapters.connectors.source_identity import (
    postgres_source_identity_fingerprint,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.query_execution import (
    QueryPreviewRejectedError,
    ValidatedQuery,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    QueryCostDecision,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)

pytestmark = pytest.mark.integration

_ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55433/postgres"
_READER_PASSWORD = "schemabridge_reader"
_CONNECTION_ID = CatalogConnectionId("warehouse-primary")


@dataclass(frozen=True, slots=True)
class _TenantSources:
    alpha_database: str
    beta_database: str
    alpha_dsn: str = field(repr=False)
    beta_dsn: str = field(repr=False)


@dataclass
class _ExactRouteLoader:
    references: dict[str, OpaqueConnectorSecretRef] = field(repr=False)
    calls: list[str] = field(default_factory=list)

    def __call__(self, target: GovernedExecutionTarget) -> OpaqueConnectorSecretRef:
        self.calls.append(target.fingerprint)
        try:
            return self.references[target.fingerprint]
        except KeyError:
            raise ConnectorTargetError(
                ConnectorTargetErrorCode.ROUTE_STALE,
                "connector route is stale",
            ) from None


@pytest.fixture()
def tenant_sources() -> Iterator[_TenantSources]:
    suffix = uuid4().hex[:12]
    alpha_database = f"sb_source_alpha_{suffix}"
    beta_database = f"sb_source_beta_{suffix}"
    admin_dsn = os.environ.get("SCHEMABRIDGE_TEST_SOURCE_ADMIN_DATABASE_URL", _ADMIN_DSN)
    created: list[str] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            reader = connection.execute(
                """
                SELECT rolname
                FROM pg_catalog.pg_roles
                WHERE rolname = 'schemabridge_reader'
                """
            ).fetchone()
            if reader is None:
                pytest.fail("the synthetic read-only source role must exist")
            for database in (alpha_database, beta_database):
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER postgres").format(sql.Identifier(database))
                )
                created.append(database)
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(database)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO schemabridge_reader").format(
                        sql.Identifier(database)
                    )
                )

        for database, marker in (
            (alpha_database, "tenant-alpha-only"),
            (beta_database, "tenant-beta-only"),
        ):
            with psycopg.connect(_database_dsn(admin_dsn, database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE public.routing_probe (
                        tenant_marker text PRIMARY KEY
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO public.routing_probe (tenant_marker) VALUES (%s)",
                    (marker,),
                )
                connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                connection.execute("REVOKE ALL ON public.routing_probe FROM PUBLIC")
                connection.execute("GRANT USAGE ON SCHEMA public TO schemabridge_reader")
                connection.execute("GRANT SELECT ON public.routing_probe TO schemabridge_reader")

        yield _TenantSources(
            alpha_database=alpha_database,
            beta_database=beta_database,
            alpha_dsn=_reader_dsn(admin_dsn, alpha_database),
            beta_dsn=_reader_dsn(admin_dsn, beta_database),
        )
    finally:
        if created:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                for database in reversed(created):
                    connection.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(database)
                        )
                    )


def test_identical_connection_table_and_query_route_to_distinct_tenant_databases(
    tenant_sources: _TenantSources,
    tmp_path: Path,
) -> None:
    alpha = _target("tenant-isolation-alpha", tenant_sources.alpha_dsn)
    beta = _target("tenant-isolation-beta", tenant_sources.beta_dsn)
    assert alpha.connection_id == beta.connection_id == _CONNECTION_ID
    assert alpha.fingerprint != beta.fingerprint

    secret_directory = (tmp_path / "connector-secrets").resolve()
    secret_directory.mkdir(mode=0o700)
    secret_directory.chmod(0o700)
    alpha_reference = OpaqueConnectorSecretRef("execution.tenant-alpha.route-v1")
    beta_reference = OpaqueConnectorSecretRef("execution.tenant-beta.route-v1")
    _write_secret(secret_directory, alpha_reference, tenant_sources.alpha_dsn)
    _write_secret(secret_directory, beta_reference, tenant_sources.beta_dsn)

    loader = _ExactRouteLoader(
        {
            alpha.fingerprint: alpha_reference,
            beta.fingerprint: beta_reference,
        }
    )
    connector = RoutedPostgresQueryConnector(
        reference_loader=loader,
        secret_resolver=OwnerOnlyConnectorSecretResolver(secret_directory),
        allowed_fields=frozenset(),
        max_rows_limit=10,
        max_timeout_ms=2_000,
    )
    alpha_query = _query(alpha)
    beta_query = _query(beta)

    assert connector.assess(alpha_query, alpha).decision is QueryCostDecision.ACCEPTED
    alpha_result = connector.execute(alpha_query, target=alpha)
    assert connector.assess(beta_query, beta).decision is QueryCostDecision.ACCEPTED
    beta_result = connector.execute(beta_query, target=beta)

    assert alpha_result.columns == beta_result.columns == ("tenant_marker",)
    assert alpha_result.rows == (("tenant-alpha-only",),)
    assert beta_result.rows == (("tenant-beta-only",),)
    assert alpha_result.database_user == beta_result.database_user == "schemabridge_reader"
    assert alpha_result.transaction_read_only is beta_result.transaction_read_only is True
    assert loader.calls == [
        alpha.fingerprint,
        alpha.fingerprint,
        beta.fingerprint,
        beta.fingerprint,
    ]

    calls_before_mismatch = tuple(loader.calls)
    with pytest.raises(QueryPreviewRejectedError, match="governed target"):
        connector.execute(alpha_query, target=beta)
    assert tuple(loader.calls) == calls_before_mismatch

    _write_secret(secret_directory, alpha_reference, tenant_sources.beta_dsn)
    with pytest.raises(ConnectorTargetError) as retargeted:
        connector.assess(alpha_query, alpha)
    assert retargeted.value.code is ConnectorTargetErrorCode.ROUTE_STALE
    assert tenant_sources.alpha_database not in str(retargeted.value)
    assert tenant_sources.beta_database not in str(retargeted.value)
    assert tenant_sources.alpha_database not in repr(connector)
    assert tenant_sources.beta_database not in repr(connector)


def _target(workspace_id: str, dsn: str) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=2_000,
        max_response_bytes=1_048_576,
        max_total_cost=Decimal("1000000"),
        max_estimated_rows=1_000_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=65_536,
    )
    return GovernedExecutionTarget(
        workspace_id=workspace_id,
        connection_id=_CONNECTION_ID,
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint=hashlib.sha256(f"route:{workspace_id}".encode()).hexdigest(),
        expected_reader="schemabridge_reader",
        source_identity_fingerprint=_observed_source_identity(dsn),
        catalog_identity_fingerprint=hashlib.sha256(f"catalog:{workspace_id}".encode()).hexdigest(),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def _query(target: GovernedExecutionTarget) -> ValidatedQuery:
    return ValidatedQuery(
        sql=(
            "SELECT probe.tenant_marker "
            "FROM public.routing_probe AS probe "
            "ORDER BY probe.tenant_marker "
            "LIMIT 10"
        ),
        parameters=(),
        max_rows=10,
        statement_timeout_ms=2_000,
        dialect=SourceDialect.POSTGRESQL,
        target_fingerprint=target.fingerprint,
    )


def _write_secret(
    directory: Path,
    reference: OpaqueConnectorSecretRef,
    dsn: str,
) -> None:
    path = directory / reference.filename
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "dialect": "postgresql",
                "expected_reader": "schemabridge_reader",
                "dsn": dsn,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _observed_source_identity(dsn: str) -> str:
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
    assert row is not None
    return postgres_source_identity_fingerprint(
        server_address=str(row[0]),
        server_port=int(row[1]),
        database=str(row[2]),
        user=str(row[3]),
    )


def _database_dsn(admin_dsn: str, database: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    parsed["dbname"] = database
    return psycopg.conninfo.make_conninfo("", **parsed)


def _reader_dsn(admin_dsn: str, database: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(admin_dsn)
    host = str(parsed.get("host") or "127.0.0.1")
    port = str(parsed.get("port") or "5432")
    return (
        "postgresql://schemabridge_reader:"
        f"{quote(_READER_PASSWORD, safe='')}@{host}:{port}/{quote(database, safe='')}"
    )
