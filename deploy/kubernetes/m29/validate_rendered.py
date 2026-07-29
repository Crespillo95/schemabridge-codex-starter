#!/usr/bin/env python3
"""Fail-closed static validation for one rendered M29 Kubernetes bundle."""

from __future__ import annotations

import json
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
NAMESPACE: Final = "schemabridge-system"
REQUIRED_SECRET_OBJECTS: Final = 9
SECRET_OBJECT_QUOTA: Final = 12
ZERO_DIGEST: Final = "0" * 64
IMAGE_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
KV_MOUNT_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
OPAQUE_BINDING_PATTERN: Final = re.compile(r"^[a-z][a-z0-9._:-]{2,199}$")
EXTERNAL_SECRET_NAME_PATTERN: Final = re.compile(
    r"^schemabridge-external-(web|api|worker|catalog|profile|reconciler|observer)"
    r"-v[1-9][0-9]*$"
)
PLACEHOLDER_PATTERNS: Final = (
    re.compile(r"\.invalid\b", re.IGNORECASE),
    re.compile(r"replace[-_]with", re.IGNORECASE),
    re.compile(r"REPLACE_", re.IGNORECASE),
    re.compile(rf"sha256:{ZERO_DIGEST}"),
)
RUNTIME_COMPONENTS: Final = frozenset(
    {"web", "api", "worker", "catalog", "profile", "reconciler", "observer"}
)
REMOTE_SECRET_COMPONENTS: Final = frozenset({"web", "worker", "catalog", "profile"})
REGISTRY_SECRET_COMPONENTS: Final = frozenset({"web", "worker", "profile", "reconciler"})
REMOTE_IDENTITY_COMPONENTS: Final = frozenset(
    {*REMOTE_SECRET_COMPONENTS, *REGISTRY_SECRET_COMPONENTS}
)
BACKGROUND_METRICS_COMPONENTS: Final = frozenset({"worker", "catalog", "profile", "reconciler"})
PROCESS_METRICS_COMPONENTS: Final = frozenset({"api", *BACKGROUND_METRICS_COMPONENTS})
SCRAPED_METRICS_COMPONENTS: Final = frozenset({"observer", *PROCESS_METRICS_COMPONENTS})
IDENTITY_COMPONENTS: Final = frozenset(
    {
        *RUNTIME_COMPONENTS,
        "migrator",
        "backup",
        "observer",
    }
)
PRIVATE_COMPONENTS: Final = frozenset({"migrator", "backup"})
EXPECTED_CAPABILITIES: Final = {
    "web": {"secret-manager", "oidc", "control-runtime", "datahub-registry"},
    "api": {"oidc", "control-api"},
    "worker": {
        "secret-manager",
        "control-worker",
        "source-execution",
        "datahub-registry",
    },
    "catalog": {"secret-manager", "control-catalog", "datahub-catalog"},
    "profile": {"secret-manager", "control-worker", "source-profile", "datahub-registry"},
    "reconciler": {"secret-manager", "control-reconciler", "datahub-registry"},
    "migrator": {"secret-manager", "control-migrator"},
    "backup": {"secret-manager", "control-backup", "backup-store"},
    "observer": {"control-observer"},
}
EXPECTED_COMMANDS: Final = {
    "web": ["streamlit"],
    "api": ["schemabridge-api"],
    "worker": ["schemabridge-worker"],
    "catalog": ["schemabridge-catalog"],
    "profile": ["schemabridge-semantic-profile-worker"],
    "reconciler": ["schemabridge-semantic-reconciler"],
    "observer": ["schemabridge-observer"],
}
EXPECTED_SECRET_ENV: Final = {
    "web": {
        "SCHEMABRIDGE_CONTROL_DATABASE_URL": "control-runtime-dsn",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": "control-audit-signing-key",
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": "identity-migration-key",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": "pseudonymization-key",
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": "query-studio-signing-key",
    },
    "api": {
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": "control-api-dsn",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": "pseudonymization-key",
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": "inventory-cursor-signing-key",
    },
    "worker": {
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": "control-worker-dsn",
    },
    "catalog": {
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": "control-catalog-dsn",
    },
    "profile": {
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": "control-worker-dsn",
    },
    "reconciler": {
        "SCHEMABRIDGE_CONTROL_RECONCILER_DATABASE_URL": "control-reconciler-dsn",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": "control-audit-signing-key",
    },
    "observer": {
        "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL": "control-observer-dsn",
    },
}
EXPECTED_FIELD_ENV: Final = {
    "web": {},
    "api": {},
    "worker": {"SCHEMABRIDGE_WORKER_ID": "metadata.name"},
    "catalog": {"SCHEMABRIDGE_CATALOG_INDEXER_ID": "metadata.name"},
    "profile": {"SCHEMABRIDGE_WORKER_ID": "metadata.name"},
    "reconciler": {"SCHEMABRIDGE_SEMANTIC_RECONCILER_ID": "metadata.name"},
    "observer": {},
}
COMMON_MANAGED_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_ENVIRONMENT",
        "SCHEMABRIDGE_COMPONENT",
        "SCHEMABRIDGE_AUTH_MODE",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE",
        "SCHEMABRIDGE_CATALOG_MODE",
        "SCHEMABRIDGE_REGISTRY_MODE",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_ID",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_CATALOG_SCOPE",
        "SCHEMABRIDGE_QUERY_STUDIO_AI_MODE",
    }
)
OIDC_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_OIDC_PROVIDER",
        "SCHEMABRIDGE_OIDC_ISSUER",
        "SCHEMABRIDGE_OIDC_AUDIENCE",
        "SCHEMABRIDGE_OIDC_ALLOWED_GROUPS",
        "SCHEMABRIDGE_OIDC_ALLOWED_TENANTS",
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY_VERSION",
    }
)
REMOTE_COMPONENT_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE",
        "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY",
    }
)
REGISTRY_SECRET_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION",
    }
)
PROCESS_METRICS_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_PROCESS_METRICS_BIND_HOST",
        "SCHEMABRIDGE_PROCESS_METRICS_PORT",
        "SCHEMABRIDGE_PROCESS_METRICS_MAX_RESPONSE_BYTES",
    }
)
OBSERVER_CONFIG_KEYS: Final = frozenset(
    {
        "SCHEMABRIDGE_ENVIRONMENT",
        "SCHEMABRIDGE_COMPONENT",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION",
        "SCHEMABRIDGE_LOG_LEVEL",
        "SCHEMABRIDGE_OBSERVER_BIND_HOST",
        "SCHEMABRIDGE_OBSERVER_PORT",
        "SCHEMABRIDGE_OBSERVER_LIMIT_CONCURRENCY",
        "SCHEMABRIDGE_OBSERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "SCHEMABRIDGE_OBSERVER_SNAPSHOT_TIMEOUT_MS",
        "SCHEMABRIDGE_OBSERVER_MAX_METRICS_RESPONSE_BYTES",
        "SCHEMABRIDGE_CONTROL_POOL_MIN_SIZE",
        "SCHEMABRIDGE_CONTROL_POOL_MAX_SIZE",
        "SCHEMABRIDGE_CONTROL_POOL_MAX_WAITING",
        "SCHEMABRIDGE_CONTROL_POOL_ACQUISITION_TIMEOUT_SECONDS",
        "SCHEMABRIDGE_CONTROL_POOL_STARTUP_TIMEOUT_SECONDS",
        "SCHEMABRIDGE_CONTROL_POOL_CLOSE_TIMEOUT_SECONDS",
        "SCHEMABRIDGE_CONTROL_POOL_MAX_IDLE_SECONDS",
        "SCHEMABRIDGE_CONTROL_POOL_MAX_LIFETIME_SECONDS",
    }
)
EXPECTED_COMPONENT_CONFIG_KEYS: Final = {
    "web": COMMON_MANAGED_CONFIG_KEYS
    | OIDC_CONFIG_KEYS
    | REMOTE_COMPONENT_CONFIG_KEYS
    | REGISTRY_SECRET_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION",
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY_VERSION",
        "SCHEMABRIDGE_PUBLICATION_MODE",
        "SCHEMABRIDGE_JUDGE_EXECUTION",
    },
    "api": COMMON_MANAGED_CONFIG_KEYS
    | OIDC_CONFIG_KEYS
    | PROCESS_METRICS_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_API_BIND_HOST",
        "SCHEMABRIDGE_API_PORT",
        "SCHEMABRIDGE_API_ALLOWED_HOSTS",
        "SCHEMABRIDGE_API_DOCS_ENABLED",
        "SCHEMABRIDGE_API_OIDC_JWKS_URL",
    },
    "worker": COMMON_MANAGED_CONFIG_KEYS
    | REMOTE_COMPONENT_CONFIG_KEYS
    | REGISTRY_SECRET_CONFIG_KEYS
    | PROCESS_METRICS_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE",
        "SCHEMABRIDGE_WORKER_LEASE_SECONDS",
        "SCHEMABRIDGE_WORKER_HEARTBEAT_SECONDS",
        "SCHEMABRIDGE_WORKER_POLL_INTERVAL_MS",
    },
    "catalog": COMMON_MANAGED_CONFIG_KEYS
    | REMOTE_COMPONENT_CONFIG_KEYS
    | PROCESS_METRICS_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_CATALOG_POLL_INTERVAL_MS",
        "SCHEMABRIDGE_CATALOG_LEASE_SECONDS",
        "SCHEMABRIDGE_CATALOG_PAGE_SIZE",
        "SCHEMABRIDGE_CATALOG_SOURCE_TIMEOUT_SECONDS",
        "SCHEMABRIDGE_CATALOG_MAX_RESPONSE_BYTES",
    },
    "profile": COMMON_MANAGED_CONFIG_KEYS
    | REMOTE_COMPONENT_CONFIG_KEYS
    | REGISTRY_SECRET_CONFIG_KEYS
    | PROCESS_METRICS_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_WORKER_IDENTITY_LINEAGE_MODE",
        "SCHEMABRIDGE_WORKER_LEASE_SECONDS",
        "SCHEMABRIDGE_WORKER_HEARTBEAT_SECONDS",
        "SCHEMABRIDGE_WORKER_POLL_INTERVAL_MS",
        "SCHEMABRIDGE_SEMANTIC_PROFILE_MAX_ATTEMPTS",
        "SCHEMABRIDGE_SEMANTIC_PROFILE_RETENTION_DAYS",
        "SCHEMABRIDGE_SEMANTIC_PROFILE_MAINTENANCE_BATCH_SIZE",
    },
    "reconciler": COMMON_MANAGED_CONFIG_KEYS
    | REGISTRY_SECRET_CONFIG_KEYS
    | PROCESS_METRICS_CONFIG_KEYS
    | {
        "SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION",
        "SCHEMABRIDGE_SEMANTIC_RECONCILER_LEASE_SECONDS",
        "SCHEMABRIDGE_SEMANTIC_RECONCILER_POLL_INTERVAL_MS",
        "SCHEMABRIDGE_SEMANTIC_RECONCILER_RETENTION_DAYS",
        "SCHEMABRIDGE_SEMANTIC_RECONCILER_MAINTENANCE_BATCH_SIZE",
    },
    "observer": OBSERVER_CONFIG_KEYS,
}
RBAC_KINDS: Final = frozenset({"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})
WORKLOAD_KINDS: Final = frozenset(
    {"Pod", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"}
)
ALLOWED_API_VERSIONS: Final = {
    "Namespace": "v1",
    "ServiceAccount": "v1",
    "ConfigMap": "v1",
    "ResourceQuota": "v1",
    "LimitRange": "v1",
    "Deployment": "apps/v1",
    "Service": "v1",
    "PodDisruptionBudget": "policy/v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "ServiceMonitor": "monitoring.coreos.com/v1",
    "Ingress": "networking.k8s.io/v1",
}


class ManifestValidationError(ValueError):
    """One or more safe, stable manifest validation failures."""

    def __init__(self, codes: Iterable[str]) -> None:
        self.codes = tuple(sorted(set(codes)))
        super().__init__("m29 deployment manifest is invalid")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ManifestValidationError(("duplicate_yaml_key",))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_documents(text: str) -> tuple[dict[str, Any], ...]:
    """Load duplicate-free Kubernetes YAML documents."""

    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError(("manifest_oversized",))
    try:
        loaded = tuple(yaml.load_all(text, Loader=_UniqueKeyLoader))
    except ManifestValidationError:
        raise
    except (TypeError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestValidationError(("malformed_yaml",)) from exc
    if not loaded or any(not isinstance(item, dict) for item in loaded):
        raise ManifestValidationError(("invalid_document",))
    return loaded


def _resources(
    documents: Sequence[Mapping[str, Any]],
    kind: str,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(item for item in documents if item.get("kind") == kind)


def _component(resource: Mapping[str, Any]) -> str | None:
    metadata = resource.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    labels = metadata.get("labels")
    if not isinstance(labels, Mapping):
        return None
    value = labels.get("app.kubernetes.io/component")
    return value if isinstance(value, str) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _check_namespace(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    namespaces = _resources(documents, "Namespace")
    if len(namespaces) != 1:
        errors.add("namespace_count")
        return
    metadata = _mapping(namespaces[0].get("metadata"))
    if metadata.get("name") != NAMESPACE:
        errors.add("namespace_name")
    labels = _mapping(metadata.get("labels"))
    for mode in ("enforce", "audit", "warn"):
        if labels.get(f"pod-security.kubernetes.io/{mode}") != "restricted":
            errors.add("namespace_pod_security")

    cluster_scoped = {"Namespace", "ClusterRole", "ClusterRoleBinding"}
    for document in documents:
        if document.get("kind") in cluster_scoped:
            continue
        metadata = _mapping(document.get("metadata"))
        if metadata.get("namespace") != NAMESPACE:
            errors.add("resource_namespace")


def _check_resource_identity(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    identities: set[tuple[str, str, str, str]] = set()
    for document in documents:
        api_version = document.get("apiVersion")
        kind = document.get("kind")
        metadata = _mapping(document.get("metadata"))
        name = metadata.get("name")
        namespace = metadata.get("namespace", "")
        if (
            not isinstance(api_version, str)
            or not api_version
            or not isinstance(kind, str)
            or not kind
            or not isinstance(name, str)
            or SAFE_NAME_PATTERN.fullmatch(name) is None
            or not isinstance(namespace, str)
            or metadata.get("generateName") is not None
        ):
            errors.add("resource_identity")
            continue
        if ALLOWED_API_VERSIONS.get(kind) != api_version:
            errors.add("resource_api_contract")
        identity = (api_version, kind, namespace, name)
        if identity in identities:
            errors.add("duplicate_resource")
        identities.add(identity)


def _check_service_accounts(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    accounts = _resources(documents, "ServiceAccount")
    components = {_component(item) for item in accounts}
    names = {
        _mapping(item.get("metadata")).get("name")
        for item in accounts
        if isinstance(_mapping(item.get("metadata")).get("name"), str)
    }
    expected_names = {f"schemabridge-{component}" for component in IDENTITY_COMPONENTS}
    if components != IDENTITY_COMPONENTS or names != expected_names:
        errors.add("service_account_set")
    if len(accounts) != len(IDENTITY_COMPONENTS):
        errors.add("service_account_count")
    for account in accounts:
        if account.get("automountServiceAccountToken") is not False:
            errors.add("service_account_automount")
    if "default" in names:
        errors.add("default_service_account")


def _check_container_security(
    component: str,
    container: Mapping[str, Any],
    errors: set[str],
) -> None:
    image = container.get("image")
    if not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
        errors.add("mutable_image")
    elif image.endswith(ZERO_DIGEST) or ".invalid/" in image:
        errors.add("placeholder_image")
    if container.get("imagePullPolicy") not in {"IfNotPresent", "Always"}:
        errors.add("image_pull_policy")
    if container.get("command") != EXPECTED_COMMANDS[component]:
        errors.add("unknown_workload_command")
    if component == "web" and _sequence(container.get("args")) != [
        "run",
        "streamlit_app.py",
        "--server.address=0.0.0.0",
        "--server.port=7860",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
    ]:
        errors.add("unknown_workload_command")
    security = _mapping(container.get("securityContext"))
    if (
        security.get("runAsNonRoot") is not True
        or security.get("runAsUser") != 10001
        or security.get("runAsGroup") != 10001
        or security.get("allowPrivilegeEscalation") is not False
        or security.get("readOnlyRootFilesystem") is not True
        or _mapping(security.get("capabilities")).get("drop") != ["ALL"]
        or security.get("privileged", False) is not False
    ):
        errors.add("container_security")
    resources = _mapping(container.get("resources"))
    for boundary in ("requests", "limits"):
        values = _mapping(resources.get(boundary))
        if not {"cpu", "memory", "ephemeral-storage"} <= set(values):
            errors.add("resource_bounds")
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        if not _mapping(container.get(probe)):
            errors.add("missing_probe")
    pre_stop = _mapping(_mapping(container.get("lifecycle")).get("preStop"))
    if not pre_stop:
        errors.add("missing_graceful_drain")


def _check_identity_projection(
    component: str,
    pod: Mapping[str, Any],
    container: Mapping[str, Any],
    errors: set[str],
) -> None:
    volumes = {
        item.get("name"): item
        for item in _sequence(pod.get("volumes"))
        if isinstance(item, Mapping)
    }
    expected_volumes = {"trust-bundle", "runtime-tmp"}
    if component in REMOTE_IDENTITY_COMPONENTS:
        expected_volumes.add("workload-identity")
    if component == "web":
        expected_volumes.add("streamlit-auth")
    if set(volumes) != expected_volumes:
        errors.add("workload_volume_set")
        return
    if component in REMOTE_IDENTITY_COMPONENTS:
        identity = _mapping(volumes["workload-identity"])
        projected = _mapping(identity.get("projected"))
        if projected.get("defaultMode") != 0o400:
            errors.add("identity_token_mode")
        sources = _sequence(projected.get("sources"))
        if len(sources) != 1:
            errors.add("identity_token_projection")
        else:
            token = _mapping(_mapping(sources[0]).get("serviceAccountToken"))
            if (
                token.get("path") != "token"
                or token.get("audience") != "schemabridge-secret-manager"
                or token.get("expirationSeconds") != 600
            ):
                errors.add("identity_token_projection")
    if component == "web":
        auth_secret = _mapping(volumes["streamlit-auth"].get("secret"))
        environment_secret_names = {
            _mapping(_mapping(_mapping(item).get("valueFrom")).get("secretKeyRef")).get("name")
            for item in _sequence(container.get("env"))
            if _mapping(_mapping(item).get("valueFrom")).get("secretKeyRef") is not None
        }
        if (
            set(auth_secret) != {"secretName", "defaultMode", "items"}
            or auth_secret.get("secretName") not in environment_secret_names
            or auth_secret.get("defaultMode") != 0o440
            or _sequence(auth_secret.get("items"))
            != [{"key": "streamlit-secrets.toml", "path": "secrets.toml"}]
        ):
            errors.add("streamlit_auth_secret")
    trust = _mapping(volumes["trust-bundle"])
    if _mapping(trust.get("configMap")).get("name") != "schemabridge-trust-bundle":
        errors.add("trust_bundle_mount")
    runtime_tmp = _mapping(volumes["runtime-tmp"])
    empty_dir = _mapping(runtime_tmp.get("emptyDir"))
    if empty_dir.get("medium") != "Memory" or not empty_dir.get("sizeLimit"):
        errors.add("writable_volume_bounds")
    mounts = {
        item.get("name"): item
        for item in _sequence(container.get("volumeMounts"))
        if isinstance(item, Mapping)
    }
    if set(mounts) != set(volumes):
        errors.add("workload_mount_set")
    else:
        private_mounts = {"trust-bundle"}
        if component in REMOTE_IDENTITY_COMPONENTS:
            private_mounts.add("workload-identity")
        if component == "web":
            private_mounts.add("streamlit-auth")
        for name in private_mounts:
            if _mapping(mounts[name]).get("readOnly") is not True:
                errors.add("private_mount_not_read_only")
        if component == "web" and _mapping(mounts["streamlit-auth"]).get("mountPath") != (
            "/opt/schemabridge/.streamlit"
        ):
            errors.add("streamlit_auth_secret")


def _check_api_probe_host_contract(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    configs = {
        _mapping(item.get("metadata")).get("name"): item
        for item in _resources(documents, "ConfigMap")
    }
    deployments = {_component(item): item for item in _resources(documents, "Deployment")}
    raw_hosts = _mapping(_mapping(configs.get("schemabridge-api-config")).get("data")).get(
        "SCHEMABRIDGE_API_ALLOWED_HOSTS"
    )
    try:
        hosts = json.loads(raw_hosts) if isinstance(raw_hosts, str) else None
    except (TypeError, ValueError):
        hosts = None
    pod = _mapping(_mapping(_mapping(deployments.get("api")).get("spec")).get("template")).get(
        "spec"
    )
    containers = _sequence(_mapping(pod).get("containers"))
    if (
        not isinstance(hosts, list)
        or len(hosts) != 1
        or not isinstance(hosts[0], str)
        or len(containers) != 1
    ):
        errors.add("api_probe_host_contract")
        return
    container = _mapping(containers[0])
    expected_paths = {
        "startupProbe": "/health/ready",
        "readinessProbe": "/health/ready",
        "livenessProbe": "/health/live",
    }
    for probe, path in expected_paths.items():
        http_get = _mapping(_mapping(container.get(probe)).get("httpGet"))
        if (
            http_get.get("path") != path
            or http_get.get("port") != "http"
            or http_get.get("scheme") != "HTTP"
            or _sequence(http_get.get("httpHeaders")) != [{"name": "Host", "value": hosts[0]}]
        ):
            errors.add("api_probe_host_contract")


def _check_runtime_environment(
    component: str,
    container: Mapping[str, Any],
    errors: set[str],
) -> None:
    expected_secrets = EXPECTED_SECRET_ENV[component]
    expected_fields = EXPECTED_FIELD_ENV[component]
    expected_names = set(expected_secrets) | set(expected_fields)
    environment = _sequence(container.get("env"))
    entries = {
        _mapping(item).get("name"): _mapping(item)
        for item in environment
        if isinstance(_mapping(item).get("name"), str)
    }
    if len(entries) != len(environment) or set(entries) != expected_names:
        errors.add("runtime_environment_set")
        return

    external_secret_names: set[str] = set()
    expected_secret_name = re.compile(
        rf"^schemabridge-external-{re.escape(component)}-v[1-9][0-9]*$"
    )
    for variable, key in expected_secrets.items():
        entry = entries[variable]
        if set(entry) != {"name", "valueFrom"}:
            errors.add("runtime_secret_ref")
            continue
        value_from = _mapping(entry.get("valueFrom"))
        if set(value_from) != {"secretKeyRef"}:
            errors.add("runtime_secret_ref")
            continue
        reference = _mapping(value_from.get("secretKeyRef"))
        secret_name = reference.get("name")
        if (
            set(reference) != {"name", "key"}
            or reference.get("key") != key
            or not isinstance(secret_name, str)
            or EXTERNAL_SECRET_NAME_PATTERN.fullmatch(secret_name) is None
            or expected_secret_name.fullmatch(secret_name) is None
        ):
            errors.add("runtime_secret_ref")
            continue
        external_secret_names.add(secret_name)
    if len(external_secret_names) != 1:
        errors.add("runtime_secret_ref")

    for variable, field_path in expected_fields.items():
        entry = entries[variable]
        if set(entry) != {"name", "valueFrom"} or _mapping(entry.get("valueFrom")) != {
            "fieldRef": {"fieldPath": field_path}
        }:
            errors.add("runtime_field_ref")


def _check_deployments(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    deployments = _resources(documents, "Deployment")
    components = {_component(item) for item in deployments}
    if components != RUNTIME_COMPONENTS or len(deployments) != len(RUNTIME_COMPONENTS):
        errors.add("runtime_deployment_set")
    for deployment in deployments:
        component = _component(deployment)
        if component not in RUNTIME_COMPONENTS:
            errors.add("runtime_component")
            continue
        spec = _mapping(deployment.get("spec"))
        if not isinstance(spec.get("replicas"), int) or spec["replicas"] < 2:
            errors.add("replica_floor")
        rolling = _mapping(_mapping(spec.get("strategy")).get("rollingUpdate"))
        if rolling.get("maxUnavailable") != 0:
            errors.add("rolling_availability")
        template = _mapping(spec.get("template"))
        template_metadata = _mapping(template.get("metadata"))
        annotations = _mapping(template_metadata.get("annotations"))
        expected_annotations: Mapping[str, str] = (
            {
                "schemabridge.io/secret-capability": {
                    "web": "preflight",
                    "worker": "execution",
                    "catalog": "catalog",
                    "profile": "profile",
                    "reconciler": "registry",
                }[component]
            }
            if component in REMOTE_IDENTITY_COMPONENTS
            else {}
        )
        if annotations != expected_annotations:
            errors.add("remote_identity_annotation")
        pod = _mapping(template.get("spec"))
        if (
            pod.get("serviceAccountName") != f"schemabridge-{component}"
            or pod.get("serviceAccountName") == "default"
            or pod.get("automountServiceAccountToken") is not False
            or pod.get("enableServiceLinks") is not False
            or pod.get("hostNetwork") is not False
            or pod.get("hostPID") is not False
            or pod.get("hostIPC") is not False
        ):
            errors.add("pod_identity_boundary")
        if (
            not isinstance(pod.get("terminationGracePeriodSeconds"), int)
            or pod["terminationGracePeriodSeconds"] < 60
        ):
            errors.add("termination_grace")
        pod_security = _mapping(pod.get("securityContext"))
        if (
            pod_security.get("runAsNonRoot") is not True
            or pod_security.get("runAsUser") != 10001
            or pod_security.get("runAsGroup") != 10001
            or pod_security.get("fsGroup") != 10001
            or _mapping(pod_security.get("seccompProfile")).get("type") != "RuntimeDefault"
        ):
            errors.add("pod_security")
        spread = {
            _mapping(item).get("topologyKey")
            for item in _sequence(pod.get("topologySpreadConstraints"))
        }
        if spread != {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}:
            errors.add("topology_spread")
        containers = _sequence(pod.get("containers"))
        if len(containers) != 1 or not isinstance(containers[0], Mapping):
            errors.add("container_count")
            continue
        container = containers[0]
        _check_container_security(component, container, errors)
        _check_identity_projection(component, pod, container, errors)
        _check_runtime_environment(component, container, errors)
        env_from_entries = _sequence(container.get("envFrom"))
        env_from = {
            _mapping(_mapping(item).get("configMapRef")).get("name") for item in env_from_entries
        }
        expected_env_from = {f"schemabridge-{component}-config"}
        if component in REMOTE_IDENTITY_COMPONENTS:
            expected_env_from.add("schemabridge-remote-secret-config")
        if (
            env_from != expected_env_from
            or len(env_from_entries) != len(expected_env_from)
            or any(
                set(_mapping(item)) != {"configMapRef"}
                or set(_mapping(_mapping(item).get("configMapRef"))) != {"name"}
                for item in env_from_entries
            )
        ):
            errors.add("runtime_config_boundary")
        if pod.get("initContainers"):
            errors.add("unexpected_init_container")


def _check_config(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    config_maps = {
        _mapping(item.get("metadata")).get("name"): item
        for item in _resources(documents, "ConfigMap")
    }
    expected_config_maps = {
        "schemabridge-remote-secret-config",
        "schemabridge-trust-bundle",
        "schemabridge-observer-contract",
        *(f"schemabridge-{component}-config" for component in RUNTIME_COMPONENTS),
    }
    if set(config_maps) != expected_config_maps:
        errors.add("config_map_set")
    remote = _mapping(_mapping(config_maps.get("schemabridge-remote-secret-config")).get("data"))
    required = {
        "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
        "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": ("/var/run/secrets/schemabridge/trust/ca.crt"),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": (
            "/var/run/secrets/schemabridge/identity/token"
        ),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": ("/var/run/secrets/schemabridge/identity"),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": ("schemabridge-secret-manager"),
    }
    if any(remote.get(key) != value for key, value in required.items()):
        errors.add("remote_secret_config")
    if set(remote) != {
        *required,
        "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL",
        "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT",
    }:
        errors.add("remote_secret_config")
    provider_url = remote.get("SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL")
    if not isinstance(provider_url, str):
        errors.add("remote_secret_url")
    else:
        parsed = urlsplit(provider_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.hostname.endswith(".invalid")
        ):
            errors.add("remote_secret_url")
    mount = remote.get("SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT")
    if not isinstance(mount, str) or KV_MOUNT_PATTERN.fullmatch(mount) is None:
        errors.add("remote_secret_mount")
    roles: set[str] = set()
    registry_bindings: set[str] = set()
    registry_versions: set[str] = set()
    for component in RUNTIME_COMPONENTS:
        data = _mapping(_mapping(config_maps.get(f"schemabridge-{component}-config")).get("data"))
        if set(data) != EXPECTED_COMPONENT_CONFIG_KEYS[component]:
            errors.add("component_config")
        if component == "observer":
            expected_observer_config = {
                "SCHEMABRIDGE_ENVIRONMENT": "production",
                "SCHEMABRIDGE_COMPONENT": "observer",
                "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
                "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA": "schemabridge_control",
                "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION": "11",
                "SCHEMABRIDGE_LOG_LEVEL": "INFO",
                "SCHEMABRIDGE_OBSERVER_BIND_HOST": "0.0.0.0",
                "SCHEMABRIDGE_OBSERVER_PORT": "9464",
                "SCHEMABRIDGE_OBSERVER_LIMIT_CONCURRENCY": "20",
                "SCHEMABRIDGE_OBSERVER_GRACEFUL_SHUTDOWN_SECONDS": "20",
                "SCHEMABRIDGE_OBSERVER_SNAPSHOT_TIMEOUT_MS": "2000",
                "SCHEMABRIDGE_OBSERVER_MAX_METRICS_RESPONSE_BYTES": "65536",
                "SCHEMABRIDGE_CONTROL_POOL_MIN_SIZE": "1",
                "SCHEMABRIDGE_CONTROL_POOL_MAX_SIZE": "8",
                "SCHEMABRIDGE_CONTROL_POOL_MAX_WAITING": "32",
                "SCHEMABRIDGE_CONTROL_POOL_ACQUISITION_TIMEOUT_SECONDS": "2",
                "SCHEMABRIDGE_CONTROL_POOL_STARTUP_TIMEOUT_SECONDS": "10",
                "SCHEMABRIDGE_CONTROL_POOL_CLOSE_TIMEOUT_SECONDS": "5",
                "SCHEMABRIDGE_CONTROL_POOL_MAX_IDLE_SECONDS": "300",
                "SCHEMABRIDGE_CONTROL_POOL_MAX_LIFETIME_SECONDS": "1800",
            }
            if data != expected_observer_config:
                errors.add("component_config")
            continue
        expected_runtime_component = "worker" if component == "profile" else component
        if (
            data.get("SCHEMABRIDGE_ENVIRONMENT") != "production"
            or data.get("SCHEMABRIDGE_COMPONENT") != expected_runtime_component
            or data.get("SCHEMABRIDGE_AUTH_MODE")
            != ("oidc" if component in {"web", "api"} else "local-demo")
            or data.get("SCHEMABRIDGE_CONTROL_PLANE_MODE") != "postgres"
            or data.get("SCHEMABRIDGE_CATALOG_MODE") != "live"
            or data.get("SCHEMABRIDGE_REGISTRY_MODE") != "live"
            or data.get("SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION") != "active"
            or data.get("SCHEMABRIDGE_QUERY_STUDIO_AI_MODE") != "disabled"
            or (
                component == "web"
                and (
                    data.get("SCHEMABRIDGE_PUBLICATION_MODE") != "disabled"
                    or data.get("SCHEMABRIDGE_JUDGE_EXECUTION") != "disabled"
                )
            )
        ):
            errors.add("component_config")
        if component in PROCESS_METRICS_COMPONENTS and any(
            data.get(key) != value
            for key, value in {
                "SCHEMABRIDGE_PROCESS_METRICS_BIND_HOST": "0.0.0.0",
                "SCHEMABRIDGE_PROCESS_METRICS_PORT": "9464",
                "SCHEMABRIDGE_PROCESS_METRICS_MAX_RESPONSE_BYTES": "65536",
            }.items()
        ):
            errors.add("process_metrics_config")
        if component in REMOTE_SECRET_COMPONENTS:
            role = data.get("SCHEMABRIDGE_CONNECTOR_SECRET_ROLE")
            if (
                not isinstance(role, str)
                or SAFE_NAME_PATTERN.fullmatch(role) is None
                or role in roles
            ):
                errors.add("remote_secret_role")
            else:
                roles.add(role)
            expected_capability = {
                "web": "preflight",
                "worker": "execution",
                "catalog": "catalog",
                "profile": "profile",
            }[component]
            if data.get("SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY") != expected_capability:
                errors.add("remote_secret_capability")
        elif {
            "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE",
            "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY",
        } & set(data):
            errors.add("remote_secret_forbidden_component")
        if component in REGISTRY_SECRET_COMPONENTS:
            registry_role = data.get("SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE")
            binding = data.get("SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF")
            version = data.get("SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION")
            if (
                not isinstance(registry_role, str)
                or SAFE_NAME_PATTERN.fullmatch(registry_role) is None
                or registry_role in roles
            ):
                errors.add("registry_secret_role")
            else:
                roles.add(registry_role)
            if not isinstance(binding, str) or OPAQUE_BINDING_PATTERN.fullmatch(binding) is None:
                errors.add("registry_secret_binding")
            else:
                registry_bindings.add(binding)
            if (
                not isinstance(version, str)
                or not version.isascii()
                or not version.isdigit()
                or int(version) < 1
            ):
                errors.add("registry_secret_version")
            else:
                registry_versions.add(version)
        elif REGISTRY_SECRET_CONFIG_KEYS & set(data):
            errors.add("registry_secret_forbidden_component")
    if len(registry_bindings) != 1:
        errors.add("registry_secret_binding")
    if len(registry_versions) != 1:
        errors.add("registry_secret_version")
    trust = _mapping(_mapping(config_maps.get("schemabridge-trust-bundle")).get("data"))
    ca_pem = trust.get("ca.crt")
    if (
        set(trust) != {"ca.crt"}
        or not isinstance(ca_pem, str)
        or not (
            ca_pem.strip().startswith("-----BEGIN CERTIFICATE-----")
            and ca_pem.strip().endswith("-----END CERTIFICATE-----")
        )
    ):
        errors.add("trust_bundle")


def _check_network_policies(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    policies = _resources(documents, "NetworkPolicy")
    default_deny = tuple(
        item
        for item in policies
        if _mapping(item.get("metadata")).get("name") == "default-deny-all"
    )
    if len(default_deny) != 1:
        errors.add("default_deny")
    else:
        spec = _mapping(default_deny[0].get("spec"))
        if (
            spec.get("podSelector") != {}
            or set(_sequence(spec.get("policyTypes"))) != {"Ingress", "Egress"}
            or spec.get("ingress") != []
            or spec.get("egress") != []
        ):
            errors.add("default_deny")
    component_policies = {
        _component(item): item
        for item in policies
        if _mapping(item.get("metadata")).get("name") != "default-deny-all"
    }
    if (
        set(component_policies) != IDENTITY_COMPONENTS
        or len(policies) != len(IDENTITY_COMPONENTS) + 1
    ):
        errors.add("component_network_policy_set")
    for component in IDENTITY_COMPONENTS:
        policy = component_policies.get(component)
        if policy is None:
            continue
        spec = _mapping(policy.get("spec"))
        if _mapping(_mapping(spec.get("podSelector")).get("matchLabels")) != {
            "app.kubernetes.io/component": component
        }:
            errors.add("component_network_selector")
        if set(_sequence(spec.get("policyTypes"))) != {"Ingress", "Egress"}:
            errors.add("network_policy_types")
        ingress = _sequence(spec.get("ingress"))
        if component in PRIVATE_COMPONENTS and ingress != []:
            errors.add("private_workload_ingress")
        expected_ingress = {
            "web": {"ingress-controller"},
            "api": {"ingress-controller", "web", "prometheus"},
            "observer": {"prometheus"},
            **{
                metrics_component: {"prometheus"}
                for metrics_component in BACKGROUND_METRICS_COMPONENTS
            },
        }.get(component, set())
        observed_ingress: set[str] = set()
        for rule in ingress:
            rule_map = _mapping(rule)
            peers = _sequence(rule_map.get("from"))
            ports = {
                (_mapping(port).get("protocol"), _mapping(port).get("port"))
                for port in _sequence(rule_map.get("ports"))
            }
            if not peers or not ports:
                errors.add("unbounded_ingress")
            for peer in peers:
                peer_map = _mapping(peer)
                namespace_labels = _mapping(
                    _mapping(peer_map.get("namespaceSelector")).get("matchLabels")
                )
                pod_labels = _mapping(_mapping(peer_map.get("podSelector")).get("matchLabels"))
                identity: str | None = None
                if (
                    namespace_labels == {"kubernetes.io/metadata.name": "ingress-nginx"}
                    and pod_labels == {"app.kubernetes.io/component": "controller"}
                    and ports
                    == {
                        ("TCP", 7860 if component == "web" else 8520),
                    }
                ):
                    identity = "ingress-controller"
                elif (
                    namespace_labels == {}
                    and pod_labels == {"app.kubernetes.io/component": "web"}
                    and ports == {("TCP", 8520)}
                ):
                    identity = "web"
                elif (
                    component in SCRAPED_METRICS_COMPONENTS
                    and namespace_labels == {"kubernetes.io/metadata.name": "observability"}
                    and pod_labels == {"app.kubernetes.io/name": "prometheus"}
                    and ports == {("TCP", 9464)}
                ):
                    identity = "prometheus"
                if identity is None:
                    errors.add("unapproved_ingress_peer")
                else:
                    observed_ingress.add(identity)
        if observed_ingress != expected_ingress:
            errors.add("component_ingress")
        egress = _sequence(spec.get("egress"))
        has_dns = False
        has_telemetry = False
        capabilities: set[str] = set()
        for rule in egress:
            rule_map = _mapping(rule)
            peers = _sequence(rule_map.get("to"))
            if not peers:
                errors.add("unbounded_egress")
            ports = {
                (_mapping(port).get("protocol"), _mapping(port).get("port"))
                for port in _sequence(rule_map.get("ports"))
            }
            if not ports:
                errors.add("unbounded_egress")
            for peer in peers:
                peer_map = _mapping(peer)
                if "ipBlock" in peer_map:
                    errors.add("raw_ip_egress")
                namespace_labels = _mapping(
                    _mapping(peer_map.get("namespaceSelector")).get("matchLabels")
                )
                pod_labels = _mapping(_mapping(peer_map.get("podSelector")).get("matchLabels"))
                if (
                    namespace_labels.get("kubernetes.io/metadata.name") == "kube-system"
                    and pod_labels.get("k8s-app") == "kube-dns"
                    and {("UDP", 53), ("TCP", 53)} <= ports
                ):
                    has_dns = True
                if (
                    namespace_labels.get("kubernetes.io/metadata.name") == "observability"
                    and pod_labels.get("app.kubernetes.io/name") == "opentelemetry-collector"
                    and ("TCP", 4317) in ports
                ):
                    has_telemetry = True
                capability = pod_labels.get("schemabridge.io/egress-capability")
                recognized_peer = False
                if isinstance(capability, str):
                    if namespace_labels.get("schemabridge.io/egress-plane") != "true" or ports != {
                        ("TCP", 443)
                    }:
                        errors.add("egress_broker_contract")
                    capabilities.add(capability)
                    recognized_peer = (
                        namespace_labels == {"schemabridge.io/egress-plane": "true"}
                        and pod_labels == {"schemabridge.io/egress-capability": capability}
                        and ports == {("TCP", 443)}
                    )
                elif (
                    (
                        namespace_labels == {}
                        and pod_labels == {"app.kubernetes.io/component": "api"}
                        and ports == {("TCP", 8520)}
                        and component == "web"
                    )
                    or (
                        has_dns
                        and namespace_labels == {"kubernetes.io/metadata.name": "kube-system"}
                        and pod_labels == {"k8s-app": "kube-dns"}
                        and ports == {("TCP", 53), ("UDP", 53)}
                    )
                    or (
                        has_telemetry
                        and namespace_labels == {"kubernetes.io/metadata.name": "observability"}
                        and pod_labels == {"app.kubernetes.io/name": "opentelemetry-collector"}
                        and ports == {("TCP", 4317)}
                    )
                ):
                    recognized_peer = True
                if not recognized_peer:
                    errors.add("unapproved_egress_peer")
        if not has_dns:
            errors.add("dns_egress")
        if component != "observer" and not has_telemetry:
            errors.add("telemetry_egress")
        if capabilities != EXPECTED_CAPABILITIES[component]:
            errors.add("capability_egress")
        if component == "observer" and "secret-manager" in capabilities:
            errors.add("observer_secret_access")


def _check_ingress_and_services(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    services = _resources(documents, "Service")
    service_names = {_mapping(item.get("metadata")).get("name") for item in services}
    if service_names != {
        "schemabridge-web",
        "schemabridge-api",
        "schemabridge-observer",
        "schemabridge-worker",
        "schemabridge-catalog",
        "schemabridge-profile",
        "schemabridge-reconciler",
    }:
        errors.add("service_set")
    for service in services:
        spec = _mapping(service.get("spec"))
        if spec.get("type") != "ClusterIP" or spec.get("externalIPs") or spec.get("loadBalancerIP"):
            errors.add("public_service")
        for port in _sequence(spec.get("ports")):
            if _mapping(port).get("nodePort") is not None:
                errors.add("public_service")

    ingresses = _resources(documents, "Ingress")
    if {_mapping(item.get("metadata")).get("name") for item in ingresses} != {
        "schemabridge-web",
        "schemabridge-api",
    }:
        errors.add("ingress_set")
    for ingress in ingresses:
        metadata = _mapping(ingress.get("metadata"))
        annotations = _mapping(metadata.get("annotations"))
        required_annotations = {
            "nginx.ingress.kubernetes.io/ssl-redirect": "true",
            "nginx.ingress.kubernetes.io/force-ssl-redirect": "true",
            "nginx.ingress.kubernetes.io/hsts": "true",
            "nginx.ingress.kubernetes.io/hsts-include-subdomains": "true",
        }
        if any(annotations.get(key) != value for key, value in required_annotations.items()):
            errors.add("ingress_tls_policy")
        try:
            hsts_age = int(str(annotations.get("nginx.ingress.kubernetes.io/hsts-max-age")))
        except ValueError:
            hsts_age = 0
        if hsts_age < 31_536_000:
            errors.add("ingress_tls_policy")
        spec = _mapping(ingress.get("spec"))
        rules = _sequence(spec.get("rules"))
        tls = _sequence(spec.get("tls"))
        rule_hosts = {
            _mapping(rule).get("host")
            for rule in rules
            if isinstance(_mapping(rule).get("host"), str)
        }
        tls_hosts: set[str] = set()
        for item in tls:
            item_map = _mapping(item)
            secret_name = item_map.get("secretName")
            if not isinstance(secret_name, str) or SAFE_NAME_PATTERN.fullmatch(secret_name) is None:
                errors.add("ingress_tls_secret")
            tls_hosts.update(
                host for host in _sequence(item_map.get("hosts")) if isinstance(host, str)
            )
        if not rule_hosts or rule_hosts != tls_hosts:
            errors.add("ingress_tls_hosts")
        for host in rule_hosts:
            if (
                not isinstance(host, str)
                or host.startswith("*.")
                or host.endswith(".invalid")
                or "." not in host
            ):
                errors.add("ingress_host")


def _check_resilience(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    budgets = _resources(documents, "PodDisruptionBudget")
    if {_component(item) for item in budgets} != RUNTIME_COMPONENTS or len(budgets) != len(
        RUNTIME_COMPONENTS
    ):
        errors.add("disruption_budget_set")
    for budget in budgets:
        spec = _mapping(budget.get("spec"))
        if spec.get("minAvailable") != 1:
            errors.add("disruption_budget")
    quotas = _resources(documents, "ResourceQuota")
    limit_ranges = _resources(documents, "LimitRange")
    quota_hard = _mapping(_mapping(quotas[0].get("spec")).get("hard")) if len(quotas) == 1 else {}
    if (
        len(quotas) != 1
        or len(limit_ranges) != 1
        or not {
            "pods",
            "requests.cpu",
            "requests.memory",
            "limits.cpu",
            "limits.memory",
        }
        <= set(quota_hard)
    ):
        errors.add("namespace_resource_bounds")
    if (
        quota_hard.get("secrets") != str(SECRET_OBJECT_QUOTA)
        or SECRET_OBJECT_QUOTA - REQUIRED_SECRET_OBJECTS != 3
    ):
        errors.add("namespace_secret_quota")


def _check_observer_contract(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    observers = tuple(
        item
        for item in _resources(documents, "ConfigMap")
        if _mapping(item.get("metadata")).get("name") == "schemabridge-observer-contract"
    )
    monitors = {
        _mapping(item.get("metadata")).get("name"): item
        for item in _resources(documents, "ServiceMonitor")
    }
    services = tuple(
        item
        for item in _resources(documents, "Service")
        if _mapping(item.get("metadata")).get("name") == "schemabridge-observer"
    )
    deployments = tuple(
        item
        for item in _resources(documents, "Deployment")
        if _mapping(item.get("metadata")).get("name") == "schemabridge-observer"
    )
    api_services = tuple(
        item
        for item in _resources(documents, "Service")
        if _mapping(item.get("metadata")).get("name") == "schemabridge-api"
    )
    api_deployments = tuple(
        item
        for item in _resources(documents, "Deployment")
        if _mapping(item.get("metadata")).get("name") == "schemabridge-api"
    )
    if (
        len(observers) != 1
        or set(monitors)
        != {
            "schemabridge-observer",
            "schemabridge-api",
            *(f"schemabridge-{component}" for component in BACKGROUND_METRICS_COMPONENTS),
        }
        or len(services) != 1
        or len(deployments) != 1
        or len(api_services) != 1
        or len(api_deployments) != 1
    ):
        errors.add("observer_contract")
        return
    data = _mapping(observers[0].get("data"))
    if (
        data.get("contract-version") != "2"
        or data.get("ownership") != "schemabridge-runtime"
        or data.get("executable-workload-included") != "true"
        or data.get("required-service-account") != "schemabridge-observer"
        or data.get("required-pod-label") != "app.kubernetes.io/name=schemabridge-observer"
        or data.get("metrics-path") != "/metrics"
        or data.get("metrics-port") != "9464"
        or data.get("required-network-label") != "app.kubernetes.io/component=observer"
    ):
        errors.add("observer_contract")

    service_spec = _mapping(services[0].get("spec"))
    service_selector = _mapping(service_spec.get("selector"))
    service_ports = _sequence(service_spec.get("ports"))
    pod_labels = _mapping(
        _mapping(_mapping(deployments[0].get("spec")).get("template")).get("metadata")
    )
    pod_labels = _mapping(pod_labels.get("labels"))
    if (
        service_selector != {"app.kubernetes.io/name": "schemabridge-observer"}
        or any(pod_labels.get(key) != value for key, value in service_selector.items())
        or len(service_ports) != 1
        or _mapping(service_ports[0])
        != {
            "name": "metrics",
            "port": 9464,
            "targetPort": "metrics",
            "protocol": "TCP",
        }
    ):
        errors.add("observer_service_contract")

    pod_spec = _mapping(_mapping(_mapping(deployments[0].get("spec")).get("template")).get("spec"))
    containers = _sequence(pod_spec.get("containers"))
    container_ports = (
        _sequence(_mapping(containers[0]).get("ports")) if len(containers) == 1 else ()
    )
    if len(container_ports) != 1 or _mapping(container_ports[0]) != {
        "name": "metrics",
        "containerPort": 9464,
        "protocol": "TCP",
    }:
        errors.add("observer_service_contract")

    monitor_spec = _mapping(monitors["schemabridge-observer"].get("spec"))
    endpoints = _sequence(monitor_spec.get("endpoints"))
    if (
        monitor_spec.get("jobLabel") != "app.kubernetes.io/name"
        or _mapping(_mapping(monitor_spec.get("selector")).get("matchLabels"))
        != {"app.kubernetes.io/name": "schemabridge-observer"}
        or _sequence(_mapping(monitor_spec.get("namespaceSelector")).get("matchNames"))
        != ["schemabridge-system"]
        or len(endpoints) != 1
        or _mapping(endpoints[0])
        != {
            "port": "metrics",
            "path": "/metrics",
            "interval": "30s",
            "scrapeTimeout": "10s",
            "scheme": "http",
        }
    ):
        errors.add("observer_monitor_contract")

    api_service_spec = _mapping(api_services[0].get("spec"))
    api_service_selector = _mapping(api_service_spec.get("selector"))
    api_service_ports = _sequence(api_service_spec.get("ports"))
    api_pod_labels = _mapping(
        _mapping(_mapping(api_deployments[0].get("spec")).get("template")).get("metadata")
    )
    api_pod_labels = _mapping(api_pod_labels.get("labels"))
    api_pod_spec = _mapping(
        _mapping(_mapping(api_deployments[0].get("spec")).get("template")).get("spec")
    )
    api_containers = _sequence(api_pod_spec.get("containers"))
    api_container_ports = (
        _sequence(_mapping(api_containers[0]).get("ports")) if len(api_containers) == 1 else ()
    )
    if (
        api_service_selector != {"app.kubernetes.io/name": "schemabridge-api"}
        or any(api_pod_labels.get(key) != value for key, value in api_service_selector.items())
        or api_service_ports
        != [
            {
                "name": "http",
                "port": 8520,
                "targetPort": "http",
                "protocol": "TCP",
            },
            {
                "name": "metrics",
                "port": 9464,
                "targetPort": "metrics",
                "protocol": "TCP",
            },
        ]
        or api_container_ports
        != [
            {"name": "http", "containerPort": 8520, "protocol": "TCP"},
            {"name": "metrics", "containerPort": 9464, "protocol": "TCP"},
        ]
    ):
        errors.add("api_monitor_contract")

    api_monitor_spec = _mapping(monitors["schemabridge-api"].get("spec"))
    api_endpoints = _sequence(api_monitor_spec.get("endpoints"))
    if (
        api_monitor_spec.get("jobLabel") != "app.kubernetes.io/name"
        or _mapping(_mapping(api_monitor_spec.get("selector")).get("matchLabels"))
        != {"app.kubernetes.io/name": "schemabridge-api"}
        or _sequence(_mapping(api_monitor_spec.get("namespaceSelector")).get("matchNames"))
        != ["schemabridge-system"]
        or len(api_endpoints) != 1
        or _mapping(api_endpoints[0])
        != {
            "port": "metrics",
            "path": "/metrics",
            "interval": "30s",
            "scrapeTimeout": "10s",
            "scheme": "http",
        }
    ):
        errors.add("api_monitor_contract")

    for component in BACKGROUND_METRICS_COMPONENTS:
        resource_name = f"schemabridge-{component}"
        component_services = tuple(
            item
            for item in _resources(documents, "Service")
            if _mapping(item.get("metadata")).get("name") == resource_name
        )
        component_deployments = tuple(
            item
            for item in _resources(documents, "Deployment")
            if _mapping(item.get("metadata")).get("name") == resource_name
        )
        if len(component_services) != 1 or len(component_deployments) != 1:
            errors.add("process_metrics_service_contract")
            continue

        service_metadata = _mapping(component_services[0].get("metadata"))
        service_spec = _mapping(component_services[0].get("spec"))
        service_selector = _mapping(service_spec.get("selector"))
        service_ports = _sequence(service_spec.get("ports"))
        deployment_template = _mapping(
            _mapping(component_deployments[0].get("spec")).get("template")
        )
        pod_labels = _mapping(_mapping(deployment_template.get("metadata")).get("labels"))
        pod_spec = _mapping(deployment_template.get("spec"))
        containers = _sequence(pod_spec.get("containers"))
        container_ports = (
            _sequence(_mapping(containers[0]).get("ports")) if len(containers) == 1 else ()
        )
        if (
            _mapping(service_metadata.get("labels")).get("app.kubernetes.io/name") != resource_name
            or service_selector != {"app.kubernetes.io/name": resource_name}
            or any(pod_labels.get(key) != value for key, value in service_selector.items())
            or len(service_ports) != 1
            or _mapping(service_ports[0])
            != {
                "name": "metrics",
                "port": 9464,
                "targetPort": "metrics",
                "protocol": "TCP",
            }
            or len(container_ports) != 1
            or _mapping(container_ports[0])
            != {
                "name": "metrics",
                "containerPort": 9464,
                "protocol": "TCP",
            }
        ):
            errors.add("process_metrics_service_contract")

        process_monitor_spec = _mapping(monitors[resource_name].get("spec"))
        process_endpoints = _sequence(process_monitor_spec.get("endpoints"))
        if (
            process_monitor_spec.get("jobLabel") != "app.kubernetes.io/name"
            or _mapping(_mapping(process_monitor_spec.get("selector")).get("matchLabels"))
            != {"app.kubernetes.io/name": resource_name}
            or _sequence(_mapping(process_monitor_spec.get("namespaceSelector")).get("matchNames"))
            != ["schemabridge-system"]
            or len(process_endpoints) != 1
            or _mapping(process_endpoints[0])
            != {
                "port": "metrics",
                "path": "/metrics",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "scheme": "http",
            }
        ):
            errors.add("process_metrics_monitor_contract")


def _check_forbidden_surfaces(
    documents: Sequence[Mapping[str, Any]],
    errors: set[str],
) -> None:
    kinds = {item.get("kind") for item in documents}
    if "Secret" in kinds:
        errors.add("embedded_secret")
    if kinds & RBAC_KINDS:
        errors.add("application_kubernetes_rbac")
    if kinds & WORKLOAD_KINDS:
        errors.add("unsupported_workload_kind")
    if "Kustomization" in kinds:
        errors.add("unrendered_kustomization")
    serialized = yaml.safe_dump_all(documents, sort_keys=True)
    forbidden_terms = (
        "secretRef:",
        "hostPath:",
        "OPENAI_API_KEY",
        "DATAHUB_GMS_TOKEN",
        "postgresql://",
        "postgres://",
    )
    if any(term in serialized for term in forbidden_terms):
        errors.add("embedded_private_material")
    forbidden_mapping_keys = {"secretRef", "hostPath"}

    def contains_forbidden_key(value: Any) -> bool:
        if isinstance(value, Mapping):
            return bool(forbidden_mapping_keys & set(value)) or any(
                contains_forbidden_key(item) for item in value.values()
            )
        if isinstance(value, Sequence) and not isinstance(value, str):
            return any(contains_forbidden_key(item) for item in value)
        return False

    if contains_forbidden_key(documents):
        errors.add("embedded_private_material")

    def count_mapping_key(value: Any, key: str) -> int:
        if isinstance(value, Mapping):
            return int(key in value) + sum(count_mapping_key(item, key) for item in value.values())
        if isinstance(value, Sequence) and not isinstance(value, str):
            return sum(count_mapping_key(item, key) for item in value)
        return 0

    if count_mapping_key(documents, "secret") != 1:
        errors.add("embedded_private_material")

    def count_secret_key_refs(value: Any) -> int:
        if isinstance(value, Mapping):
            return int("secretKeyRef" in value) + sum(
                count_secret_key_refs(item) for item in value.values()
            )
        if isinstance(value, Sequence) and not isinstance(value, str):
            return sum(count_secret_key_refs(item) for item in value)
        return 0

    expected_reference_count = sum(len(values) for values in EXPECTED_SECRET_ENV.values())
    if count_secret_key_refs(documents) != expected_reference_count:
        errors.add("runtime_secret_ref")


def validate_documents(
    documents: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the closed M29 deployment contract or raise safe codes."""

    errors: set[str] = set()
    _check_resource_identity(documents, errors)
    _check_namespace(documents, errors)
    _check_service_accounts(documents, errors)
    _check_deployments(documents, errors)
    _check_config(documents, errors)
    _check_api_probe_host_contract(documents, errors)
    _check_network_policies(documents, errors)
    _check_ingress_and_services(documents, errors)
    _check_resilience(documents, errors)
    _check_observer_contract(documents, errors)
    _check_forbidden_surfaces(documents, errors)
    if errors:
        raise ManifestValidationError(errors)


def validate_rendered_text(text: str) -> tuple[dict[str, Any], ...]:
    """Reject placeholders, then validate the rendered resource set."""

    placeholder_codes = {
        "unresolved_placeholder" for pattern in PLACEHOLDER_PATTERNS if pattern.search(text)
    }
    if placeholder_codes:
        raise ManifestValidationError(placeholder_codes)
    documents = load_documents(text)
    validate_documents(documents)
    return documents


def _read_manifest(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManifestValidationError(("manifest_unavailable",)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ManifestValidationError(("manifest_not_regular",))
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise ManifestValidationError(("manifest_oversized",))
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManifestValidationError(("manifest_unavailable",)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    """Validate one rendered file without printing its path or contents."""

    args = tuple(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: validate_rendered.py RENDERED_MANIFEST", file=sys.stderr)
        return 2
    try:
        documents = validate_rendered_text(_read_manifest(Path(args[0])))
    except ManifestValidationError as exc:
        for code in exc.codes:
            print(f"m29_manifest_invalid:{code}", file=sys.stderr)
        return 1
    print(f"m29_manifest_valid resources={len(documents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
