from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.adapters.connectors.remote_secrets import (
    ConnectorSecretCapability,
    VaultKvV2ConnectorSecretResolver,
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


def test_managed_composition_builds_remote_exact_version_resolver(
    tmp_path: Path,
) -> None:
    trust_root = (tmp_path / "trust").resolve()
    trust_root.mkdir()
    ca_bundle = trust_root / "ca.crt"
    ca_bundle.write_text("synthetic trust anchor", encoding="utf-8")
    ca_bundle.chmod(0o600)
    identity_root = (tmp_path / "identity").resolve()
    identity_root.mkdir()
    token_file = identity_root / "token"
    token_file.write_text("read-only-projected-token-placeholder", encoding="utf-8")
    token_file.chmod(0o400)
    with patch.dict("os.environ", {}, clear=True):
        settings = Settings(
            _env_file=None,
            OPENAI_API_KEY=None,
            DATAHUB_GMS_TOKEN=None,
            **{
                "SCHEMABRIDGE_ENVIRONMENT": "production",
                "SCHEMABRIDGE_COMPONENT": "worker",
                "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                    "postgresql://schemabridge_worker:password@control.example.test/control"
                    "?sslmode=verify-full"
                ),
                "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE": "verified-oidc",
                "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
                "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": ("https://secrets.example.test"),
                "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE": "schemabridge-execution",
                "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE": ("schemabridge-registry-reader"),
                "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF": ("registry.reader.primary"),
                "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION": 17,
                "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT": "tenant-connectors",
                "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY": "execution",
                "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": ca_bundle,
                "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": token_file,
                "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": identity_root,
                "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": ("schemabridge-secret-manager"),
            },  # type: ignore[arg-type]
        )

    resolver = _build_connector_secret_resolver(
        settings,
        repository_root=tmp_path.resolve(),
    )

    assert isinstance(resolver, VaultKvV2ConnectorSecretResolver)
    assert resolver.capability is ConnectorSecretCapability.EXECUTION
    rendered = repr(resolver)
    assert "secrets.example.test" not in rendered
    assert "tenant-connectors" not in rendered
    assert str(ca_bundle) not in rendered
    assert str(token_file) not in rendered
