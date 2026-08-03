"""Fail-closed contracts for operation-scoped DataHub registry credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import schemabridge.adapters.semantic_registry.remote_secrets as remote_module
import schemabridge.bootstrap as bootstrap_module
from schemabridge.adapters.connectors.remote_secrets import ConnectorSecretCapability
from schemabridge.adapters.semantic_registry.datahub import (
    DataHubRegistryReadConfig,
    DataHubRegistryWriteConfig,
)
from schemabridge.adapters.semantic_registry.remote_secrets import (
    RemoteDataHubObservedSemanticRegistryPublisher,
    RemoteDataHubQueryRecipeInventory,
    RemoteDataHubRegistryVersionReader,
    VaultKvV2DataHubRegistryCredentialResolver,
    VaultKvV2DataHubRegistryWriterCredentialResolver,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretErrorCode,
    OpaqueConnectorSecretRef,
    connector_secret_error,
)
from schemabridge.application.ports.planning import (
    PlanningPortError,
    PlanningPortErrorCode,
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.bootstrap import (
    build_registry_version_reader,
    build_semantic_dependency_reconciler,
)
from schemabridge.config import Settings
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass
class _Backend:
    document: dict[str, object]
    capability: ConnectorSecretCapability = ConnectorSecretCapability.REGISTRY
    calls: list[tuple[OpaqueConnectorSecretRef, int]] = field(default_factory=list)
    failure: bool = False

    def read_document(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        version: int,
    ) -> dict[str, object]:
        self.calls.append((reference, version))
        if self.failure:
            raise connector_secret_error(ConnectorSecretErrorCode.UNAVAILABLE)
        return dict(self.document)


@dataclass
class _Credentials:
    calls: int = 0

    def resolve(self) -> DataHubRegistryReadConfig:
        self.calls += 1
        return DataHubRegistryReadConfig(
            server="https://datahub.example.test",
            token="operation-scoped-registry-token",
        )


@dataclass
class _WriterCredentials:
    calls: int = 0

    def resolve(self) -> DataHubRegistryWriteConfig:
        self.calls += 1
        return DataHubRegistryWriteConfig(
            server="https://datahub.example.test",
            token="operation-scoped-writer-token",
            actor_urn="urn:li:corpuser:registry-publisher",
        )


def _document() -> dict[str, object]:
    return {
        "format_version": 1,
        "kind": "datahub_registry_reader",
        "server": "https://datahub.example.test/",
        "token": "synthetic-registry-token",
    }


def _resolver(backend: _Backend) -> VaultKvV2DataHubRegistryCredentialResolver:
    return VaultKvV2DataHubRegistryCredentialResolver(
        backend=backend,
        reference=OpaqueConnectorSecretRef("registry.reader.primary"),
        version=73,
    )


def _writer_document() -> dict[str, object]:
    return {
        "format_version": 1,
        "kind": "datahub_registry_writer",
        "server": "https://datahub.example.test/",
        "token": "synthetic-registry-writer-token",
        "actor_urn": "urn:li:corpuser:registry-publisher",
    }


def _writer_resolver(
    backend: _Backend,
) -> VaultKvV2DataHubRegistryWriterCredentialResolver:
    return VaultKvV2DataHubRegistryWriterCredentialResolver(
        backend=backend,
        reference=OpaqueConnectorSecretRef("registry.writer.primary"),
        version=91,
    )


def test_registry_credential_uses_exact_provider_version_and_closed_shape() -> None:
    backend = _Backend(_document())
    resolver = _resolver(backend)

    config = resolver.resolve()

    assert config.server == "https://datahub.example.test"
    assert config.token == "synthetic-registry-token"
    assert len(backend.calls) == 1
    assert backend.calls[0][1] == 73
    representation = repr(resolver)
    assert "registry.reader.primary" not in representation
    assert "synthetic-registry-token" not in representation


@pytest.mark.parametrize(
    "mutation",
    (
        lambda document: document.update({"unexpected": "value"}),
        lambda document: document.update({"format_version": 2}),
        lambda document: document.update({"kind": "datahub_catalog"}),
        lambda document: document.update({"server": "http://datahub.example.test"}),
        lambda document: document.update({"token": "line\nbreak"}),
    ),
)
def test_registry_credential_rejects_unknown_or_unsafe_document(
    mutation: Any,
) -> None:
    document = _document()
    mutation(document)

    with pytest.raises(PlanningPortError) as raised:
        _resolver(_Backend(document)).resolve()

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_INVALID
    assert "registry.reader.primary" not in str(raised.value)
    assert "synthetic-registry-token" not in str(raised.value)


def test_registry_provider_failure_is_sanitized() -> None:
    backend = _Backend(_document(), failure=True)

    with pytest.raises(PlanningPortError) as raised:
        _resolver(backend).resolve()

    assert raised.value.code is PlanningPortErrorCode.CONTEXT_UNAVAILABLE
    assert str(raised.value) == "DataHub registry reader credential is unavailable"


def test_registry_writer_credential_is_exact_versioned_closed_and_sanitized() -> None:
    backend = _Backend(
        _writer_document(),
        capability=ConnectorSecretCapability.REGISTRY_PUBLISHER,
    )
    resolver = _writer_resolver(backend)

    config = resolver.resolve()

    assert config.server == "https://datahub.example.test"
    assert config.actor_urn == "urn:li:corpuser:registry-publisher"
    assert backend.calls[0][1] == 91
    assert "synthetic-registry-writer-token" not in repr(config)
    assert "registry.writer.primary" not in repr(resolver)

    invalid = _writer_document()
    invalid["unexpected"] = "value"
    with pytest.raises(RegistryPublicationError) as malformed:
        _writer_resolver(
            _Backend(invalid, capability=ConnectorSecretCapability.REGISTRY_PUBLISHER)
        ).resolve()
    assert malformed.value.code is RegistryPublicationErrorCode.CATALOG_UNAVAILABLE
    assert "synthetic-registry-writer-token" not in str(malformed.value)

    with pytest.raises(RegistryPublicationError) as unavailable:
        _writer_resolver(
            _Backend(
                _writer_document(),
                capability=ConnectorSecretCapability.REGISTRY_PUBLISHER,
                failure=True,
            )
        ).resolve()
    assert unavailable.value.code is RegistryPublicationErrorCode.CATALOG_UNAVAILABLE
    assert str(unavailable.value) == "DataHub registry writer credential is unavailable"


def test_registry_publisher_resolves_fresh_writer_credentials_per_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = _WriterCredentials()
    tokens: list[str] = []
    expected = object()

    class _Publisher:
        def __init__(self, *, config: DataHubRegistryWriteConfig) -> None:
            tokens.append(config.token)

        def observe(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return expected

        def publish(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return expected

    monkeypatch.setattr(remote_module, "DataHubObservedSemanticRegistryPublisher", _Publisher)
    publisher = RemoteDataHubObservedSemanticRegistryPublisher(credentials)

    assert publisher.observe(object(), object(), observed_at=object()) is expected  # type: ignore[arg-type]
    assert publisher.publish(object(), object(), observed_at=object()) is expected  # type: ignore[arg-type]
    assert credentials.calls == 2
    assert tokens == ["operation-scoped-writer-token"] * 2
    assert "operation-scoped-writer-token" not in repr(publisher)


def test_version_reader_resolves_fresh_credentials_for_each_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = _Credentials()
    observed: list[str] = []
    expected = object()

    class _Reader:
        def __init__(self, *, config: DataHubRegistryReadConfig) -> None:
            observed.append(config.token)

        def load_version(self, scope: SemanticRegistryScope, version: int) -> object:
            del scope, version
            return expected

    monkeypatch.setattr(remote_module, "DataHubRegistryVersionReader", _Reader)
    reader = RemoteDataHubRegistryVersionReader(credentials=credentials)
    scope = SemanticRegistryScope(
        workspace_id="workspace",
        catalog_scope="catalog",
        registry_id="registry",
    )

    assert reader.load_version(scope, 7) is expected
    assert reader.load_version(scope, 7) is expected
    assert credentials.calls == 2
    assert observed == [
        "operation-scoped-registry-token",
        "operation-scoped-registry-token",
    ]
    assert "operation-scoped-registry-token" not in repr(reader)


def _managed_reconciler_settings(tmp_path: Path) -> Settings:
    ca_bundle = tmp_path / "ca.crt"
    ca_bundle.write_text("synthetic trust anchor", encoding="utf-8")
    ca_bundle.chmod(0o444)
    return Settings.model_validate(
        {
            "SCHEMABRIDGE_ENVIRONMENT": "production",
            "SCHEMABRIDGE_COMPONENT": "reconciler",
            "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
                "postgresql://schemabridge_reconciler:synthetic@control.example.test/"
                "control?sslmode=verify-full"
            ),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                "audit-key-with-diverse-bytes-0123456789-abcdefgh"
            ),
            "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
            "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": "https://vault.example.test",
            "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT": "schemabridge",
            "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": ca_bundle,
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": tmp_path / "identity/token",
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": tmp_path / "identity",
            "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": ("schemabridge-secret-manager"),
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE": "reconciler-registry",
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF": ("registry.reader.primary"),
            "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION": 73,
        }
    )


def test_managed_registry_composes_without_local_credential_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = _managed_reconciler_settings(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "require_current_control_plane_schema",
        lambda **_kwargs: None,
    )

    versions = build_registry_version_reader(settings=settings)
    reconciler = build_semantic_dependency_reconciler(settings=settings)

    assert isinstance(versions, RemoteDataHubRegistryVersionReader)
    assert isinstance(reconciler.versions, RemoteDataHubRegistryVersionReader)
    assert isinstance(reconciler.recipes, RemoteDataHubQueryRecipeInventory)
    assert settings.semantic_registry_reader_env_path == Path(".local/datahub/mcp.env")
    assert ".local/datahub/mcp.env" not in repr(versions)
    assert "registry.reader.primary" not in repr(versions)


def test_managed_active_registry_cannot_fall_back_to_local_reader() -> None:
    with pytest.raises(ValidationError, match="managed semantic registry requires remote"):
        Settings.model_validate(
            {
                "SCHEMABRIDGE_ENVIRONMENT": "production",
                "SCHEMABRIDGE_COMPONENT": "reconciler",
                "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
                    "postgresql://schemabridge_reconciler:synthetic@control.example.test/"
                    "control?sslmode=verify-full"
                ),
                "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                    "audit-key-with-diverse-bytes-0123456789-abcdefgh"
                ),
            }
        )


def test_registry_resolver_requires_registry_capability() -> None:
    backend = _Backend(_document(), capability=ConnectorSecretCapability.CATALOG)

    with pytest.raises(ValueError, match="configuration is invalid"):
        _resolver(backend)
