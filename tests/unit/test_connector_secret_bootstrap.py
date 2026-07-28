from __future__ import annotations

from pathlib import Path

import pytest

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.application.postgres_health import DatabaseConfigurationError
from schemabridge.bootstrap import (
    _build_connector_secret_resolver,
    _connector_secret_directory,
)
from schemabridge.config import Settings
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.workflows import fingerprint_payload


def _target() -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=65_536,
        max_total_cost="100",
        max_estimated_rows=1_000,
        max_plan_nodes=100,
        max_plan_depth=16,
        max_plan_width=4_096,
    )
    return GovernedExecutionTarget(
        workspace_id="workspace-secret-bootstrap",
        connection_id=CatalogConnectionId("warehouse-primary"),
        connector_kind=SourceConnectorKind.POSTGRESQL,
        dialect=SourceDialect.POSTGRESQL,
        route_revision=1,
        route_fingerprint=fingerprint_payload({"route": "secret-bootstrap"}),
        expected_reader="schemabridge_reader",
        source_identity_fingerprint=fingerprint_payload({"source": "secret-bootstrap"}),
        catalog_identity_fingerprint=fingerprint_payload({"catalog": "secret-bootstrap"}),
        type_contract_fingerprint=postgres_type_contract_fingerprint(),
        cost_budget=budget,
        cost_budget_fingerprint=budget.fingerprint,
    )


def test_composition_preserves_a_secret_directory_symlink_for_fail_closed_open(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real-secrets"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-secrets"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    settings = Settings.model_validate(
        {"SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": str(linked_directory)}
    )

    resolver = _build_connector_secret_resolver(
        settings,
        repository_root=tmp_path.resolve(),
    )

    assert resolver.secret_directory == linked_directory
    with pytest.raises(ConnectorSecretResolutionError) as captured:
        resolver.resolve(OpaqueConnectorSecretRef("execution.route.alpha"), _target())
    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    assert str(linked_directory) not in str(captured.value)


def test_composition_rejects_parent_traversal_without_canonicalizing_it(
    tmp_path: Path,
) -> None:
    settings = Settings.model_validate(
        {"SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": "../private-secrets"}
    )

    with pytest.raises(DatabaseConfigurationError, match="absolute lexical path"):
        _connector_secret_directory(
            settings,
            repository_root=tmp_path.resolve(),
        )
