from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy" / "kubernetes" / "m24-runtime.yaml"


def _documents() -> tuple[dict[str, Any], ...]:
    loaded = tuple(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    assert all(isinstance(item, dict) for item in loaded)
    return loaded


def _deployment(name: str) -> dict[str, Any]:
    return next(
        item
        for item in _documents()
        if item["kind"] == "Deployment" and item["metadata"]["name"] == name
    )


def _config_map(name: str) -> dict[str, Any]:
    return next(
        item
        for item in _documents()
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == name
    )


def _container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _init_container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["initContainers"]
    assert len(containers) == 1
    return containers[0]


def test_managed_runtime_components_are_separate_hardened_deployments() -> None:
    documents = _documents()
    assert len(documents) == 11
    assert all(item["kind"] != "Secret" for item in documents)

    api = _deployment("schemabridge-api")
    worker = _deployment("schemabridge-worker")
    catalog = _deployment("schemabridge-catalog")
    reconciler = _deployment("schemabridge-semantic-reconciler")
    profile_worker = _deployment("schemabridge-semantic-profile-worker")
    api_pod = api["spec"]["template"]["spec"]
    worker_pod = worker["spec"]["template"]["spec"]
    catalog_pod = catalog["spec"]["template"]["spec"]
    reconciler_pod = reconciler["spec"]["template"]["spec"]
    profile_worker_pod = profile_worker["spec"]["template"]["spec"]
    api_container = _container(api)
    worker_container = _container(worker)
    catalog_container = _container(catalog)
    reconciler_container = _container(reconciler)
    profile_worker_container = _container(profile_worker)

    assert api_container["command"] == ["schemabridge-api"]
    assert worker_container["command"] == ["schemabridge-worker"]
    assert catalog_container["command"] == ["schemabridge-catalog"]
    assert reconciler_container["command"] == ["schemabridge-semantic-reconciler"]
    assert profile_worker_container["command"] == ["schemabridge-semantic-profile-worker"]
    assert api_pod["terminationGracePeriodSeconds"] >= 30
    assert worker_pod["terminationGracePeriodSeconds"] >= 120
    assert catalog_pod["terminationGracePeriodSeconds"] >= 120
    assert reconciler_pod["terminationGracePeriodSeconds"] >= 120
    assert profile_worker_pod["terminationGracePeriodSeconds"] >= 120
    for pod in (
        api_pod,
        worker_pod,
        catalog_pod,
        reconciler_pod,
        profile_worker_pod,
    ):
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
    assert "initContainers" not in api_pod
    assert len(worker_pod["initContainers"]) == 1
    assert len(catalog_pod["initContainers"]) == 1
    assert len(reconciler_pod["initContainers"]) == 1
    assert len(profile_worker_pod["initContainers"]) == 1
    assert api_container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert api_container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert worker_container["startupProbe"]["exec"]["command"] == [
        "schemabridge-worker",
        "--probe-ready",
    ]
    assert worker_container["readinessProbe"]["exec"]["command"] == [
        "schemabridge-worker",
        "--probe-ready",
    ]
    assert worker_container["livenessProbe"]["exec"]["command"] == [
        "/bin/sh",
        "-ec",
        "kill -0 1",
    ]
    assert catalog_container["startupProbe"]["exec"]["command"] == [
        "schemabridge-catalog",
        "--probe-ready",
    ]
    assert catalog_container["readinessProbe"]["exec"]["command"] == [
        "schemabridge-catalog",
        "--probe-ready",
    ]
    assert catalog_container["livenessProbe"]["exec"]["command"] == [
        "/bin/sh",
        "-ec",
        "kill -0 1",
    ]
    assert reconciler_container["startupProbe"]["exec"]["command"] == [
        "schemabridge-semantic-reconciler",
        "--probe-ready",
    ]
    assert reconciler_container["readinessProbe"]["exec"]["command"] == [
        "schemabridge-semantic-reconciler",
        "--probe-ready",
    ]
    assert reconciler_container["livenessProbe"]["exec"]["command"] == [
        "/bin/sh",
        "-ec",
        "kill -0 1",
    ]
    assert profile_worker_container["startupProbe"]["exec"]["command"] == [
        "schemabridge-semantic-profile-worker",
        "--probe-ready",
    ]
    assert profile_worker_container["readinessProbe"]["exec"]["command"] == [
        "schemabridge-semantic-profile-worker",
        "--probe-ready",
    ]
    assert profile_worker_container["livenessProbe"]["exec"]["command"] == [
        "/bin/sh",
        "-ec",
        "kill -0 1",
    ]
    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert "httpGet" not in worker_container[probe_name]
        assert "httpGet" not in catalog_container[probe_name]
        assert "httpGet" not in reconciler_container[probe_name]
        assert "httpGet" not in profile_worker_container[probe_name]
    for container in (
        api_container,
        worker_container,
        catalog_container,
        reconciler_container,
        profile_worker_container,
    ):
        security = container["securityContext"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]
        assert container["resources"]["requests"]
        assert container["resources"]["limits"]


def test_all_runtime_mounts_reference_one_declared_volume() -> None:
    deployments = tuple(item for item in _documents() if item["kind"] == "Deployment")

    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        volume_names = tuple(item["name"] for item in pod.get("volumes", ()))
        assert len(volume_names) == len(set(volume_names))
        for container in (*pod["containers"], *pod.get("initContainers", ())):
            mount_names = tuple(item["name"] for item in container.get("volumeMounts", ()))
            assert len(mount_names) == len(set(mount_names))
            assert set(mount_names) <= set(volume_names)


def test_runtime_secret_references_are_component_scoped() -> None:
    api_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in _container(_deployment("schemabridge-api"))["env"]
    }
    worker_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in _container(_deployment("schemabridge-worker"))["env"]
        if "secretKeyRef" in item["valueFrom"]
    }
    catalog_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in _container(_deployment("schemabridge-catalog"))["env"]
        if "secretKeyRef" in item["valueFrom"]
    }
    reconciler_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in _container(_deployment("schemabridge-semantic-reconciler"))["env"]
        if "secretKeyRef" in item["valueFrom"]
    }
    profile_worker_env = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in _container(_deployment("schemabridge-semantic-profile-worker"))["env"]
        if "secretKeyRef" in item["valueFrom"]
    }

    assert api_env == {
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": "schemabridge-api-secrets",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": "schemabridge-api-secrets",
    }
    assert worker_env == {
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": "schemabridge-worker-secrets",
    }
    assert catalog_env == {
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": "schemabridge-catalog-secrets",
    }
    assert reconciler_env == {
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
            "schemabridge-semantic-reconciler-secrets"
        ),
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": ("schemabridge-semantic-reconciler-secrets"),
    }
    assert profile_worker_env == {
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
            "schemabridge-semantic-profile-worker-secrets"
        ),
    }
    assert not set(api_env) & {"DATABASE_URL", "OPENAI_API_KEY", "DATAHUB_GMS_TOKEN"}
    assert not set(worker_env) & {
        "DATABASE_URL",
        "DATAHUB_GMS_TOKEN",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
    }
    assert not set(catalog_env) & {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
    }
    assert not set(reconciler_env) & {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "DATAHUB_GMS_TOKEN",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL",
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY",
    }
    assert not set(profile_worker_env) & {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "DATAHUB_GMS_TOKEN",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
    }


def test_m28_component_configmaps_do_not_cross_capability_boundaries() -> None:
    worker = _config_map("schemabridge-worker-config")["data"]
    catalog = _config_map("schemabridge-catalog-config")["data"]
    reconciler = _config_map("schemabridge-semantic-reconciler-config")["data"]
    profile_worker = _config_map("schemabridge-semantic-profile-worker-config")["data"]

    assert worker["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] == (
        "/var/run/schemabridge/connectors/execution"
    )
    assert catalog["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] == (
        "/var/run/schemabridge/connectors/catalog"
    )
    assert profile_worker["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] == (
        "/var/run/schemabridge/connectors/profile"
    )
    for routed in (worker, catalog, profile_worker):
        assert not set(routed) & {
            "DATABASE_URL",
            "DATAHUB_GMS_TOKEN",
        }
    assert "DATAHUB_GMS_URL" not in catalog
    assert "SCHEMABRIDGE_CATALOG_DATAHUB_CREDENTIAL_BINDING_REF" not in catalog
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID" not in profile_worker
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID" not in profile_worker

    assert reconciler["SCHEMABRIDGE_COMPONENT"] == "reconciler"
    assert reconciler["SCHEMABRIDGE_AUTH_MODE"] == "local-demo"
    assert reconciler["SCHEMABRIDGE_REGISTRY_MODE"] == "live"
    assert reconciler["SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION"] == "active"
    assert reconciler["SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH"] == (
        "/var/run/schemabridge/datahub/reader.env"
    )
    assert reconciler["SCHEMABRIDGE_CONTROL_OPERATOR_ROLES"] == '["platform_admin"]'
    assert not set(reconciler) & {
        "DATABASE_URL",
        "POSTGRES_READER_USER",
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_LLM_MODEL",
        "DATAHUB_GMS_TOKEN",
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
    }

    assert profile_worker["SCHEMABRIDGE_COMPONENT"] == "worker"
    assert profile_worker["SCHEMABRIDGE_AUTH_MODE"] == "local-demo"
    assert profile_worker["SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE"] == "verified-oidc"
    assert not set(profile_worker) & {
        "OPENAI_API_KEY",
        "SCHEMABRIDGE_LLM_MODEL",
        "DATAHUB_GMS_URL",
        "DATAHUB_GMS_TOKEN",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
        "SCHEMABRIDGE_CONTROL_OPERATOR_ACTOR_ID",
        "SCHEMABRIDGE_CONTROL_OPERATOR_ROLES",
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY",
    }


def test_m28_configmaps_pass_the_managed_component_preflight() -> None:
    worker_config = _config_map("schemabridge-worker-config")["data"]
    catalog_config = _config_map("schemabridge-catalog-config")["data"]
    reconciler_config = _config_map("schemabridge-semantic-reconciler-config")["data"]
    profile_worker_config = _config_map("schemabridge-semantic-profile-worker-config")["data"]

    with patch.dict(
        os.environ,
        {
            **worker_config,
            "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                "postgresql://schemabridge_worker:secret@control.example.test/"
                "schemabridge_control?sslmode=verify-full"
            ),
        },
        clear=True,
    ):
        worker = Settings(_env_file=None)
    with patch.dict(
        os.environ,
        {
            **catalog_config,
            "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": (
                "postgresql://schemabridge_catalog:secret@control.example.test/"
                "schemabridge_control?sslmode=verify-full"
            ),
        },
        clear=True,
    ):
        catalog = Settings(_env_file=None)
    with patch.dict(
        os.environ,
        {
            **reconciler_config,
            "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": (
                "postgresql://schemabridge_reconciler:secret@control.example.test/"
                "schemabridge_control?sslmode=verify-full"
            ),
            "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
                "semantic-reconciler-audit-key-with-distinct-bytes-1234"
            ),
        },
        clear=True,
    ):
        reconciler = Settings(_env_file=None)
    with patch.dict(
        os.environ,
        {
            **profile_worker_config,
            "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": (
                "postgresql://schemabridge_worker:secret@control.example.test/"
                "schemabridge_control?sslmode=verify-full"
            ),
        },
        clear=True,
    ):
        profile_worker = Settings(_env_file=None)

    assert worker.database_url is None
    assert worker.connector_secret_directory == Path("/var/run/schemabridge/connectors/execution")
    assert catalog.database_url is None
    assert catalog.datahub_gms_token is None
    assert catalog.connector_secret_directory == Path("/var/run/schemabridge/connectors/catalog")
    assert reconciler.runtime_component == "reconciler"
    assert reconciler.database_url is None
    assert reconciler.openai_api_key is None
    assert reconciler.datahub_gms_token is None
    assert profile_worker.runtime_component == "worker"
    assert profile_worker.database_url is None
    assert profile_worker.semantic_profile_source_workspace_id is None
    assert profile_worker.semantic_profile_source_connection_id is None
    assert profile_worker.connector_secret_directory == Path(
        "/var/run/schemabridge/connectors/profile"
    )
    assert profile_worker.control_audit_signing_key is None
    assert profile_worker.openai_api_key is None
    assert profile_worker.datahub_gms_token is None


def test_each_managed_catalog_replica_uses_its_pod_name_as_lease_owner() -> None:
    catalog = _deployment("schemabridge-catalog")
    catalog_env = {item["name"]: item["valueFrom"] for item in _container(catalog)["env"]}
    catalog_config = _config_map("schemabridge-catalog-config")["data"]

    assert catalog["spec"]["replicas"] > 1
    assert catalog_env["SCHEMABRIDGE_CATALOG_INDEXER_ID"] == {
        "fieldRef": {"fieldPath": "metadata.name"}
    }
    assert "SCHEMABRIDGE_CATALOG_INDEXER_ID" not in catalog_config
    assert catalog_config["SCHEMABRIDGE_COMPONENT"] == "catalog"
    assert catalog_config["SCHEMABRIDGE_CATALOG_MODE"] == "live"
    assert catalog_config["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] == (
        "/var/run/schemabridge/connectors/catalog"
    )
    assert "DATAHUB_GMS_URL" not in catalog_config


def test_each_managed_worker_uses_its_unique_pod_name_as_lease_owner() -> None:
    worker = _deployment("schemabridge-worker")
    worker_env = {item["name"]: item["valueFrom"] for item in _container(worker)["env"]}
    worker_config = _config_map("schemabridge-worker-config")["data"]

    assert worker["spec"]["replicas"] > 1
    assert worker_env["SCHEMABRIDGE_WORKER_ID"] == {"fieldRef": {"fieldPath": "metadata.name"}}
    assert "SCHEMABRIDGE_WORKER_ID" not in worker_config


def test_each_m28_replica_uses_its_pod_name_as_lease_owner() -> None:
    reconciler = _deployment("schemabridge-semantic-reconciler")
    profile_worker = _deployment("schemabridge-semantic-profile-worker")
    reconciler_env = {item["name"]: item["valueFrom"] for item in _container(reconciler)["env"]}
    profile_worker_env = {
        item["name"]: item["valueFrom"] for item in _container(profile_worker)["env"]
    }
    reconciler_config = _config_map("schemabridge-semantic-reconciler-config")["data"]
    profile_worker_config = _config_map("schemabridge-semantic-profile-worker-config")["data"]

    assert reconciler["spec"]["replicas"] > 1
    assert profile_worker["spec"]["replicas"] > 1
    assert reconciler_env["SCHEMABRIDGE_SEMANTIC_RECONCILER_ID"] == {
        "fieldRef": {"fieldPath": "metadata.name"}
    }
    assert profile_worker_env["SCHEMABRIDGE_WORKER_ID"] == {
        "fieldRef": {"fieldPath": "metadata.name"}
    }
    assert "SCHEMABRIDGE_SEMANTIC_RECONCILER_ID" not in reconciler_config
    assert "SCHEMABRIDGE_WORKER_ID" not in profile_worker_config
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_WORKSPACE_ID" not in profile_worker_config
    assert "SCHEMABRIDGE_SEMANTIC_PROFILE_SOURCE_CONNECTION_ID" not in profile_worker_config


def test_managed_worker_enables_verified_lineage_without_oidc_material() -> None:
    api_config = _config_map("schemabridge-api-config")["data"]
    worker_config = _config_map("schemabridge-worker-config")["data"]

    assert worker_config["SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE"] == "verified-oidc"
    assert "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE" not in api_config
    assert not set(worker_config) & {
        "SCHEMABRIDGE_AUTH_MODE",
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_OIDC_AUDIENCE",
        "SCHEMABRIDGE_OIDC_PROVIDER",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS",
        "SCHEMABRIDGE_API_OIDC_JWKS_URL",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY",
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN",
    }


def test_worker_stages_reader_secret_as_uid_10001_regular_owner_only_file() -> None:
    worker = _deployment("schemabridge-worker")
    pod = worker["spec"]["template"]["spec"]
    worker_container = _container(worker)
    init_container = _init_container(worker)
    config = _config_map("schemabridge-worker-config")["data"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    worker_mounts = {item["name"]: item for item in worker_container["volumeMounts"]}
    init_mounts = {item["name"]: item for item in init_container["volumeMounts"]}

    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["runAsGroup"] == 10001
    assert pod["securityContext"]["fsGroup"] == 10001

    source = volumes["datahub-reader-source"]["secret"]
    assert source == {
        "secretName": "schemabridge-worker-datahub-reader",
        "defaultMode": 0o440,
        "items": [{"key": "reader.env", "path": "reader.env"}],
    }
    assert volumes["datahub-reader-runtime"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "128Ki",
    }

    assert "datahub-reader-source" not in worker_mounts
    assert worker_mounts == {
        "datahub-reader-runtime": {
            "name": "datahub-reader-runtime",
            "mountPath": "/var/run/schemabridge/datahub",
            "readOnly": True,
        },
        "connector-runtime": {
            "name": "connector-runtime",
            "mountPath": "/var/run/schemabridge/connectors",
            "readOnly": True,
        },
    }
    assert config["SCHEMABRIDGE_SEMANTIC_REGISTRY_READER_ENV_PATH"] == (
        "/var/run/schemabridge/datahub/reader.env"
    )

    assert init_container["image"] == worker_container["image"]
    assert init_container["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert init_mounts == {
        "datahub-reader-source": {
            "name": "datahub-reader-source",
            "mountPath": "/var/run/secrets/schemabridge/datahub-source",
            "readOnly": True,
        },
        "datahub-reader-runtime": {
            "name": "datahub-reader-runtime",
            "mountPath": "/var/run/schemabridge/datahub",
        },
        "execution-connector-source": {
            "name": "execution-connector-source",
            "mountPath": "/var/run/secrets/schemabridge/execution-connectors",
            "readOnly": True,
        },
        "connector-runtime": {
            "name": "connector-runtime",
            "mountPath": "/var/run/schemabridge/connectors",
        },
    }
    assert init_container["command"] == ["/bin/sh", "-ec"]
    script = init_container["args"][0]
    assert (
        "cp -- /var/run/secrets/schemabridge/datahub-source/reader.env "
        "/var/run/schemabridge/datahub/reader.env"
    ) in script
    assert "chmod 0600 /var/run/schemabridge/datahub/reader.env" in script
    assert "test ! -L /var/run/schemabridge/datahub/reader.env" in script
    assert 'test "$(stat -c %u /var/run/schemabridge/datahub/reader.env)" -eq 10001' in script
    assert 'test "$(stat -c %a /var/run/schemabridge/datahub/reader.env)" = 600' in script


@pytest.mark.parametrize(
    (
        "deployment_name",
        "config_map_name",
        "directory_name",
        "source_volume_name",
        "source_secret_name",
    ),
    (
        (
            "schemabridge-worker",
            "schemabridge-worker-config",
            "execution",
            "execution-connector-source",
            "schemabridge-worker-connector-secrets",
        ),
        (
            "schemabridge-catalog",
            "schemabridge-catalog-config",
            "catalog",
            "catalog-connector-source",
            "schemabridge-catalog-connector-secrets",
        ),
        (
            "schemabridge-semantic-profile-worker",
            "schemabridge-semantic-profile-worker-config",
            "profile",
            "profile-connector-source",
            "schemabridge-semantic-profile-connector-secrets",
        ),
    ),
)
def test_connector_documents_are_staged_into_separate_owner_only_directories(
    deployment_name: str,
    config_map_name: str,
    directory_name: str,
    source_volume_name: str,
    source_secret_name: str,
) -> None:
    deployment = _deployment(deployment_name)
    pod = deployment["spec"]["template"]["spec"]
    container = _container(deployment)
    init_container = _init_container(deployment)
    config = _config_map(config_map_name)["data"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    container_mounts = {item["name"]: item for item in container["volumeMounts"]}
    init_mounts = {item["name"]: item for item in init_container["volumeMounts"]}
    runtime_directory = f"/var/run/schemabridge/connectors/{directory_name}"
    source_directory = f"/var/run/secrets/schemabridge/{directory_name}-connectors"

    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["runAsGroup"] == 10001
    assert pod["securityContext"]["fsGroup"] == 10001
    assert config["SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY"] == runtime_directory
    assert volumes[source_volume_name]["secret"] == {
        "secretName": source_secret_name,
        "defaultMode": 0o440,
    }
    assert volumes["connector-runtime"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "1Mi",
    }
    assert source_volume_name not in container_mounts
    assert container_mounts["connector-runtime"] == {
        "name": "connector-runtime",
        "mountPath": "/var/run/schemabridge/connectors",
        "readOnly": True,
    }
    assert init_mounts[source_volume_name] == {
        "name": source_volume_name,
        "mountPath": source_directory,
        "readOnly": True,
    }
    assert init_mounts["connector-runtime"] == {
        "name": "connector-runtime",
        "mountPath": "/var/run/schemabridge/connectors",
    }
    assert init_container["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    script = init_container["args"][0]
    assert f"mkdir -p {runtime_directory}" in script
    assert f"chmod 0700 {runtime_directory}" in script
    assert f"test ! -L {runtime_directory}" in script
    assert f"for source in {source_directory}/*.json; do" in script
    assert 'cp -- "$source" "$destination"' in script
    assert 'chmod 0600 "$destination"' in script
    assert 'test ! -L "$destination"' in script
    assert 'test "$(stat -c %u "$destination")" -eq 10001' in script
    assert 'test "$(stat -c %a "$destination")" = 600' in script


def test_reconciler_stages_reader_secret_as_uid_10001_regular_owner_only_file() -> None:
    reconciler = _deployment("schemabridge-semantic-reconciler")
    pod = reconciler["spec"]["template"]["spec"]
    reconciler_container = _container(reconciler)
    init_container = _init_container(reconciler)
    volumes = {item["name"]: item for item in pod["volumes"]}
    reconciler_mounts = {item["name"]: item for item in reconciler_container["volumeMounts"]}
    init_mounts = {item["name"]: item for item in init_container["volumeMounts"]}

    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["runAsGroup"] == 10001
    assert pod["securityContext"]["fsGroup"] == 10001
    assert volumes["datahub-reader-source"]["secret"] == {
        "secretName": "schemabridge-semantic-reconciler-datahub-reader",
        "defaultMode": 0o440,
        "items": [{"key": "reader.env", "path": "reader.env"}],
    }
    assert volumes["datahub-reader-runtime"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "128Ki",
    }
    assert "datahub-reader-source" not in reconciler_mounts
    assert reconciler_mounts == {
        "datahub-reader-runtime": {
            "name": "datahub-reader-runtime",
            "mountPath": "/var/run/schemabridge/datahub",
            "readOnly": True,
        }
    }
    assert init_mounts["datahub-reader-source"]["readOnly"] is True
    assert init_container["securityContext"]["readOnlyRootFilesystem"] is True
    script = init_container["args"][0]
    assert "umask 077" in script
    assert "chmod 0600 /var/run/schemabridge/datahub/reader.env" in script
    assert 'test "$(id -u)" -eq 10001' in script
    assert "test ! -L /var/run/schemabridge/datahub/reader.env" in script
    assert 'test "$(stat -c %u /var/run/schemabridge/datahub/reader.env)" -eq 10001' in script
    assert 'test "$(stat -c %a /var/run/schemabridge/datahub/reader.env)" = 600' in script


def test_runtime_image_contains_schema_preflight_assets_and_non_root_user() -> None:
    dockerfile = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")

    assert "COPY migrations ./migrations" in dockerfile
    assert "'.[api,postgres,sql]'" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["schemabridge-api"]' in dockerfile


def test_m28_runbook_keeps_local_connector_staging_distinct_from_m29_operation() -> None:
    runbook = (ROOT / "deploy" / "kubernetes" / "README.md").read_text(encoding="utf-8")

    for secret_name in (
        "schemabridge-worker-connector-secrets",
        "schemabridge-catalog-connector-secrets",
        "schemabridge-semantic-profile-connector-secrets",
    ):
        assert secret_name in runbook
    assert "`source-reader-dsn`" not in runbook
    assert "`datahub-reader-token`" not in runbook
    assert "not an operated remote secret-manager" in runbook
    assert "remain M29 production gates" in runbook
