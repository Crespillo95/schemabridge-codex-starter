from __future__ import annotations

import copy
import importlib.util
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from schemabridge.config import Settings

ROOT = Path(__file__).resolve().parents[2]
M29_ROOT = ROOT / "deploy" / "kubernetes" / "m29"
BASE = M29_ROOT / "base"
PRODUCTION = M29_ROOT / "overlays" / "production"
VALIDATOR_PATH = M29_ROOT / "validate_rendered.py"
ZERO_DIGEST = "0" * 64
VALID_DIGEST = "a" * 64
COMPONENTS = {
    "web",
    "api",
    "worker",
    "catalog",
    "profile",
    "reconciler",
    "migrator",
    "backup",
    "observer",
}
RUNTIME_COMPONENTS = {
    "web",
    "api",
    "worker",
    "catalog",
    "profile",
    "reconciler",
    "observer",
}
REMOTE_SECRET_COMPONENTS = {"web", "worker", "catalog", "profile"}
REGISTRY_SECRET_COMPONENTS = {"web", "worker", "profile", "reconciler"}
REMOTE_IDENTITY_COMPONENTS = REMOTE_SECRET_COMPONENTS | REGISTRY_SECRET_COMPONENTS
BACKGROUND_METRICS_COMPONENTS = {"worker", "catalog", "profile", "reconciler"}
BASE_RESOURCES = (
    "namespace.yaml",
    "serviceaccounts.yaml",
    "resource-governance.yaml",
    "runtime-config.yaml",
    "workloads.yaml",
    "backup-cronjob.yaml",
    "services.yaml",
    "disruption-budgets.yaml",
    "network-policies.yaml",
    "observer-contract.yaml",
    "prometheus-rules.yaml",
)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m29_manifest_validator", VALIDATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


VALIDATOR = _load_validator()


def _template_text() -> str:
    resources = tuple((BASE / name).read_text(encoding="utf-8") for name in BASE_RESOURCES)
    ingress = (PRODUCTION / "ingress.yaml").read_text(encoding="utf-8")
    return "\n---\n".join((*resources, ingress))


def _rendered_text() -> str:
    text = _template_text()
    replacements = (
        ("secret-manager.example.invalid", "vault.prod.example.com"),
        ("identity.example.invalid", "identity.prod.example.com"),
        ("registry.invalid", "registry.example.com"),
        ("app.example.invalid", "app.schemabridge.example.com"),
        ("api.example.invalid", "api.schemabridge.example.com"),
        ("replace-with-kv-v2-mount", "schemabridge-kv"),
        ("replace-with-registry-id", "governed_enterprise"),
        ("replace-with-catalog-scope", "production"),
        ("replace-with-oidc-provider", "corporate-oidc"),
        ("replace-with-web-oidc-audience", "schemabridge-web"),
        ("replace-with-api-oidc-audience", "schemabridge-api"),
        ("replace-with-approved-tenant", "approved-enterprise"),
        ("replace-with-web-preflight-role", "sb-web-preflight"),
        ("replace-with-execution-role", "sb-execution"),
        ("replace-with-catalog-role", "sb-catalog"),
        ("replace-with-profile-role", "sb-profile"),
        ("replace-with-web-registry-role", "sb-web-registry"),
        ("replace-with-worker-registry-role", "sb-worker-registry"),
        ("replace-with-profile-registry-role", "sb-profile-registry"),
        ("replace-with-reconciler-registry-role", "sb-reconciler-registry"),
        ("replace-with-registry-reader-binding", "registry.reader.primary"),
        ("replace-with-web-runtime-secret-version", "schemabridge-external-web-v17"),
        ("replace-with-api-runtime-secret-version", "schemabridge-external-api-v17"),
        ("replace-with-worker-runtime-secret-version", "schemabridge-external-worker-v17"),
        ("replace-with-catalog-runtime-secret-version", "schemabridge-external-catalog-v17"),
        ("replace-with-profile-runtime-secret-version", "schemabridge-external-profile-v17"),
        (
            "replace-with-reconciler-runtime-secret-version",
            "schemabridge-external-reconciler-v17",
        ),
        (
            "replace-with-observer-runtime-secret-version",
            "schemabridge-external-observer-v17",
        ),
        (
            "replace-with-backup-runtime-secret-version",
            "schemabridge-external-backup-v17",
        ),
        ("replace-with-managed-web-tls-certificate", "sb-web-tls"),
        ("replace-with-managed-api-tls-certificate", "sb-api-tls"),
        (
            "REPLACE_WITH_APPROVED_CA_PEM",
            "-----BEGIN CERTIFICATE-----\n"
            "    MIIBsyntheticproductioncontract\n"
            "    -----END CERTIFICATE-----",
        ),
        (ZERO_DIGEST, VALID_DIGEST),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _documents() -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in VALIDATOR.validate_rendered_text(_rendered_text())]


def _resource(
    documents: list[dict[str, Any]],
    kind: str,
    name: str,
) -> dict[str, Any]:
    return next(
        item for item in documents if item["kind"] == kind and item["metadata"]["name"] == name
    )


def _deployment(
    documents: list[dict[str, Any]],
    component: str,
) -> dict[str, Any]:
    return _resource(documents, "Deployment", f"schemabridge-{component}")


def _container(deployment: Mapping[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return cast(dict[str, Any], containers[0])


def _backup_cronjob(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return _resource(documents, "CronJob", "schemabridge-backup")


def _backup_container(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return _container(_backup_cronjob(documents)["spec"]["jobTemplate"])


def _synthetic_secret_value(component: str, variable: str) -> str:
    if variable.endswith("DATABASE_URL"):
        role = f"schemabridge_{component}"
        return (
            f"postgresql://{role}:synthetic-control-password@control.prod.example.com/"
            "schemabridge_control?sslmode=verify-full&"
            "sslrootcert=/var/run/secrets/schemabridge/trust/ca.crt"
        )
    return {
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": (
            "audit-signing-key-with-diverse-bytes-1234567890"
        ),
        "SCHEMABRIDGE_IDENTITY_MIGRATION_KEY": (
            "identity-migration-key-with-diverse-bytes-9876543210"
        ),
        "SCHEMABRIDGE_PSEUDONYMIZATION_KEY": ("pseudonymization-key-with-diverse-bytes-1357902468"),
        "SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY": (
            "query-studio-signing-key-with-diverse-bytes-8642097531"
        ),
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": (
            "inventory-cursor-key-with-diverse-bytes-2468135790"
        ),
    }[variable]


def _settings_environment(
    documents: list[dict[str, Any]],
    component: str,
) -> dict[str, str]:
    container = _container(_deployment(documents, component))
    environment: dict[str, str] = {}
    for source in container["envFrom"]:
        config_name = source["configMapRef"]["name"]
        environment.update(_resource(documents, "ConfigMap", config_name)["data"])
    for item in container["env"]:
        variable = item["name"]
        value_from = item["valueFrom"]
        if "secretKeyRef" in value_from:
            environment[variable] = _synthetic_secret_value(component, variable)
        else:
            assert value_from == {"fieldRef": {"fieldPath": "metadata.name"}}
            environment[variable] = f"schemabridge-{component}-pod-0"
    return environment


def _expect_error(
    documents: list[dict[str, Any]],
    code: str,
) -> None:
    with pytest.raises(VALIDATOR.ManifestValidationError) as error:
        VALIDATOR.validate_documents(documents)
    assert code in error.value.codes


def test_template_is_intentionally_blocked_until_every_operator_value_is_set() -> None:
    with pytest.raises(VALIDATOR.ManifestValidationError) as error:
        VALIDATOR.validate_rendered_text(_template_text())

    assert error.value.codes == ("unresolved_placeholder",)
    assert ".invalid" in _template_text()
    assert f"sha256:{ZERO_DIGEST}" in _template_text()


def test_complete_rendered_production_contract_passes_the_fail_closed_validator() -> None:
    documents = VALIDATOR.validate_rendered_text(_rendered_text())

    assert len(documents) == 63
    assert {item["kind"] for item in documents} >= {
        "Namespace",
        "ServiceAccount",
        "Deployment",
        "CronJob",
        "Service",
        "Ingress",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "ResourceQuota",
        "LimitRange",
        "ServiceMonitor",
        "PrometheusRule",
    }


def test_kustomizations_reference_only_existing_bounded_resources() -> None:
    base = yaml.safe_load((BASE / "kustomization.yaml").read_text(encoding="utf-8"))
    production = yaml.safe_load((PRODUCTION / "kustomization.yaml").read_text(encoding="utf-8"))

    assert tuple(base["resources"]) == BASE_RESOURCES
    assert all((BASE / path).is_file() for path in base["resources"])
    assert production["resources"] == ["../../base", "ingress.yaml"]
    assert (PRODUCTION / "ingress.yaml").is_file()
    assert {item["name"] for item in production["images"]} == {
        "registry.invalid/schemabridge/runtime"
    }
    assert all(item["digest"] == f"sha256:{ZERO_DIGEST}" for item in production["images"])


def test_each_capability_has_one_non_default_service_account_and_no_kubernetes_rbac() -> None:
    documents = _documents()
    accounts = [item for item in documents if item["kind"] == "ServiceAccount"]

    assert {
        item["metadata"]["labels"]["app.kubernetes.io/component"] for item in accounts
    } == COMPONENTS
    assert {item["metadata"]["name"] for item in accounts} == {
        f"schemabridge-{component}" for component in COMPONENTS
    }
    assert all(item["automountServiceAccountToken"] is False for item in accounts)
    assert not {
        "Secret",
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
    } & {item["kind"] for item in documents}


def test_only_existing_runtime_commands_are_deployed_and_observer_is_executable() -> None:
    documents = _documents()
    deployments = [item for item in documents if item["kind"] == "Deployment"]
    commands = {
        item["metadata"]["labels"]["app.kubernetes.io/component"]: _container(item)["command"]
        for item in deployments
    }

    assert commands == {
        "web": ["schemabridge-web"],
        "api": ["schemabridge-api"],
        "worker": ["schemabridge-worker"],
        "catalog": ["schemabridge-catalog"],
        "profile": ["schemabridge-semantic-profile-worker"],
        "reconciler": ["schemabridge-semantic-reconciler"],
        "observer": ["schemabridge-observer"],
    }
    assert (
        _container(_deployment(documents, "web"))["image"]
        == (_container(_deployment(documents, "api"))["image"])
    )
    assert "args" not in _container(_deployment(documents, "web"))
    assert not {"backup", "migrator"} & set(commands)
    observer = _resource(
        documents,
        "ConfigMap",
        "schemabridge-observer-contract",
    )
    assert observer["data"]["executable-workload-included"] == "true"
    assert _resource(documents, "ServiceMonitor", "schemabridge-observer")
    assert _resource(documents, "ServiceMonitor", "schemabridge-api")


def test_backup_is_the_only_exact_batch_workload_and_uses_one_external_secret_and_pvc() -> None:
    documents = _documents()
    cronjob = _backup_cronjob(documents)
    spec = cronjob["spec"]
    job_spec = spec["jobTemplate"]["spec"]
    pod = job_spec["template"]["spec"]
    container = _backup_container(documents)

    assert cronjob["apiVersion"] == "batch/v1"
    assert cronjob["metadata"] == {
        "name": "schemabridge-backup",
        "namespace": "schemabridge-system",
        "labels": {
            "app.kubernetes.io/name": "schemabridge-backup",
            "app.kubernetes.io/part-of": "schemabridge",
            "app.kubernetes.io/component": "backup",
        },
    }
    assert {
        "schedule": spec["schedule"],
        "timeZone": spec["timeZone"],
        "concurrencyPolicy": spec["concurrencyPolicy"],
        "suspend": spec["suspend"],
        "startingDeadlineSeconds": spec["startingDeadlineSeconds"],
        "successfulJobsHistoryLimit": spec["successfulJobsHistoryLimit"],
        "failedJobsHistoryLimit": spec["failedJobsHistoryLimit"],
    } == {
        "schedule": "0 * * * *",
        "timeZone": "Etc/UTC",
        "concurrencyPolicy": "Forbid",
        "suspend": False,
        "startingDeadlineSeconds": 900,
        "successfulJobsHistoryLimit": 3,
        "failedJobsHistoryLimit": 3,
    }
    assert {
        "backoffLimit": job_spec["backoffLimit"],
        "activeDeadlineSeconds": job_spec["activeDeadlineSeconds"],
        "ttlSecondsAfterFinished": job_spec["ttlSecondsAfterFinished"],
    } == {
        "backoffLimit": 1,
        "activeDeadlineSeconds": 1800,
        "ttlSecondsAfterFinished": 86400,
    }
    assert pod["serviceAccountName"] == "schemabridge-backup"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["command"] == ["schemabridge-backup"]
    assert container["args"] == [
        "--destination",
        "/var/lib/schemabridge/backups/hourly",
    ]
    assert {container["image"]} == {
        _container(deployment)["image"]
        for deployment in documents
        if deployment["kind"] == "Deployment"
    }
    secret_entries = {
        item["name"]: item["valueFrom"]["secretKeyRef"]
        for item in container["env"]
        if "valueFrom" in item
    }
    assert {
        variable: reference["key"] for variable, reference in secret_entries.items()
    } == VALIDATOR.EXPECTED_SECRET_ENV["backup"]
    assert {reference["name"] for reference in secret_entries.values()} == {
        "schemabridge-external-backup-v17"
    }
    backup_volume = next(item for item in pod["volumes"] if item["name"] == "backup-store")
    assert backup_volume == {
        "name": "backup-store",
        "persistentVolumeClaim": {"claimName": "schemabridge-backup-store-v1"},
    }
    assert not {
        "Pod",
        "ReplicaSet",
        "StatefulSet",
        "DaemonSet",
        "Job",
    } & {item["kind"] for item in documents}


def test_prometheus_rule_is_the_exact_active_alert_contract() -> None:
    documents = _documents()
    rule = _resource(documents, "PrometheusRule", "schemabridge-active-alerts")
    canonical = yaml.safe_load(
        (ROOT / "deploy" / "observability" / "alert-rules.yaml").read_text(encoding="utf-8")
    )

    assert rule["apiVersion"] == "monitoring.coreos.com/v1"
    assert rule["metadata"] == {
        "name": "schemabridge-active-alerts",
        "namespace": "schemabridge-system",
        "labels": {
            "app.kubernetes.io/name": "schemabridge-active-alerts",
            "app.kubernetes.io/part-of": "schemabridge",
            "app.kubernetes.io/component": "observability",
        },
    }
    assert rule["spec"] == {"groups": canonical["groups"]}


def test_web_oidc_secret_is_projected_from_its_exact_versioned_runtime_secret() -> None:
    documents = _documents()
    deployment = _deployment(documents, "web")
    pod = deployment["spec"]["template"]["spec"]
    container = _container(deployment)
    volumes = {item["name"]: item for item in pod["volumes"]}
    mounts = {item["name"]: item for item in container["volumeMounts"]}

    assert volumes["streamlit-auth"] == {
        "name": "streamlit-auth",
        "secret": {
            "secretName": "schemabridge-external-web-v17",
            "defaultMode": 0o440,
            "items": [{"key": "streamlit-secrets.toml", "path": "secrets.toml"}],
        },
    }
    assert mounts["streamlit-auth"] == {
        "name": "streamlit-auth",
        "mountPath": "/opt/schemabridge/.streamlit",
        "readOnly": True,
    }
    assert pod["securityContext"]["fsGroup"] == 10001


def test_web_startup_and_readiness_repeat_real_preflight_but_liveness_is_process_only() -> None:
    container = _container(_deployment(_documents(), "web"))

    for probe_name in ("startupProbe", "readinessProbe"):
        assert container[probe_name]["exec"] == {"command": ["schemabridge-web", "--probe-ready"]}
        assert "httpGet" not in container[probe_name]
    assert container["livenessProbe"]["httpGet"] == {
        "path": "/_stcore/health",
        "port": "http",
        "scheme": "HTTP",
    }
    assert "exec" not in container["livenessProbe"]


def test_api_health_probes_use_the_exact_allowlisted_host() -> None:
    documents = _documents()
    container = _container(_deployment(documents, "api"))
    expected_paths = {
        "startupProbe": "/health/ready",
        "readinessProbe": "/health/ready",
        "livenessProbe": "/health/live",
    }
    for probe, path in expected_paths.items():
        assert container[probe]["httpGet"] == {
            "path": path,
            "port": "http",
            "scheme": "HTTP",
            "httpHeaders": [{"name": "Host", "value": "api.schemabridge.example.com"}],
        }


def test_observer_runtime_is_minimal_tls_bound_and_scrapeable() -> None:
    documents = _documents()
    config = _resource(documents, "ConfigMap", "schemabridge-observer-config")["data"]
    deployment = _deployment(documents, "observer")
    pod = deployment["spec"]["template"]["spec"]
    container = _container(deployment)

    assert config == {
        "SCHEMABRIDGE_ENVIRONMENT": "production",
        "SCHEMABRIDGE_COMPONENT": "observer",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA": "schemabridge_control",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION": "12",
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
    assert container["command"] == ["schemabridge-observer"]
    assert container["env"] == [
        {
            "name": "SCHEMABRIDGE_CONTROL_OBSERVER_DATABASE_URL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "schemabridge-external-observer-v17",
                    "key": "control-observer-dsn",
                }
            },
        }
    ]
    assert container["ports"] == [{"name": "metrics", "containerPort": 9464, "protocol": "TCP"}]
    assert container["startupProbe"]["httpGet"]["path"] == "/health/ready"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health/live"
    assert {item["name"] for item in pod["volumes"]} == {
        "trust-bundle",
        "runtime-tmp",
    }

    environment = _settings_environment(documents, "observer")
    with patch.dict(os.environ, environment, clear=True):
        settings = Settings(_env_file=None)
    assert settings.control_observer_database_url is not None
    assert settings.control_observer_database_url.get_secret_value().startswith(
        "postgresql://schemabridge_observer:"
    )
    assert settings.connector_secret_provider_url is None
    assert settings.workload_identity_token_file is None
    assert settings.openai_api_key is None
    assert settings.datahub_gms_token is None

    service = _resource(documents, "Service", "schemabridge-observer")
    monitor = _resource(documents, "ServiceMonitor", "schemabridge-observer")
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "schemabridge-observer"}
    assert service["spec"]["ports"] == [
        {
            "name": "metrics",
            "port": 9464,
            "targetPort": "metrics",
            "protocol": "TCP",
        }
    ]
    assert monitor["spec"]["endpoints"][0]["path"] == "/metrics"
    assert monitor["spec"]["endpoints"][0]["port"] == "metrics"
    assert monitor["spec"]["jobLabel"] == "app.kubernetes.io/name"

    policy = _resource(documents, "NetworkPolicy", "schemabridge-observer")
    assert policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "observability"}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 9464}],
        }
    ]
    serialized_policy = yaml.safe_dump(policy)
    assert "secret-manager" not in serialized_policy
    assert "opentelemetry-collector" not in serialized_policy
    assert "siem-export" not in serialized_policy
    assert "control-observer" in serialized_policy


def test_api_metrics_scrape_uses_a_stable_job_and_one_closed_ingress_peer() -> None:
    documents = _documents()
    monitor = _resource(documents, "ServiceMonitor", "schemabridge-api")
    service = _resource(documents, "Service", "schemabridge-api")
    policy = _resource(documents, "NetworkPolicy", "schemabridge-api")

    assert monitor["spec"] == {
        "jobLabel": "app.kubernetes.io/name",
        "selector": {"matchLabels": {"app.kubernetes.io/name": "schemabridge-api"}},
        "namespaceSelector": {"matchNames": ["schemabridge-system"]},
        "endpoints": [
            {
                "port": "metrics",
                "path": "/metrics",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "scheme": "http",
            }
        ],
    }
    assert service["metadata"]["labels"]["app.kubernetes.io/name"] == ("schemabridge-api")
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "schemabridge-api"}
    assert service["spec"]["ports"] == [
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
    assert _container(_deployment(documents, "api"))["ports"] == [
        {"name": "http", "containerPort": 8520, "protocol": "TCP"},
        {"name": "metrics", "containerPort": 9464, "protocol": "TCP"},
    ]
    prometheus_rules = [
        rule
        for rule in policy["spec"]["ingress"]
        if any(
            peer.get("namespaceSelector", {}).get("matchLabels", {})
            == {"kubernetes.io/metadata.name": "observability"}
            and peer.get("podSelector", {}).get("matchLabels", {})
            == {"app.kubernetes.io/name": "prometheus"}
            for peer in rule["from"]
        )
    ]
    assert prometheus_rules == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "observability"}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 9464}],
        }
    ]
    environment = _settings_environment(documents, "api")
    with patch.dict(os.environ, environment, clear=True):
        settings = Settings(_env_file=None)
    assert settings.process_metrics_bind_host == "0.0.0.0"
    assert settings.process_metrics_port == 9464
    assert settings.process_metrics_max_response_bytes == 65_536
    assert {item["metadata"]["name"] for item in documents if item["kind"] == "ServiceMonitor"} == {
        "schemabridge-api",
        "schemabridge-observer",
        *(f"schemabridge-{component}" for component in BACKGROUND_METRICS_COMPONENTS),
    }


def test_background_metrics_exporters_have_exact_settings_services_and_ingress() -> None:
    documents = _documents()
    expected_metrics_config = {
        "SCHEMABRIDGE_PROCESS_METRICS_BIND_HOST": "0.0.0.0",
        "SCHEMABRIDGE_PROCESS_METRICS_PORT": "9464",
        "SCHEMABRIDGE_PROCESS_METRICS_MAX_RESPONSE_BYTES": "65536",
    }

    for component in BACKGROUND_METRICS_COMPONENTS:
        resource_name = f"schemabridge-{component}"
        config = _resource(
            documents,
            "ConfigMap",
            f"schemabridge-{component}-config",
        )["data"]
        deployment = _deployment(documents, component)
        container = _container(deployment)
        service = _resource(documents, "Service", resource_name)
        monitor = _resource(documents, "ServiceMonitor", resource_name)
        policy = _resource(documents, "NetworkPolicy", resource_name)

        assert {key: config[key] for key in expected_metrics_config} == expected_metrics_config
        assert container["ports"] == [{"name": "metrics", "containerPort": 9464, "protocol": "TCP"}]
        assert "exec" in container["startupProbe"]
        assert "exec" in container["readinessProbe"]
        assert service["spec"] == {
            "type": "ClusterIP",
            "selector": {"app.kubernetes.io/name": resource_name},
            "ports": [
                {
                    "name": "metrics",
                    "port": 9464,
                    "targetPort": "metrics",
                    "protocol": "TCP",
                }
            ],
        }
        assert monitor["spec"] == {
            "jobLabel": "app.kubernetes.io/name",
            "selector": {"matchLabels": {"app.kubernetes.io/name": resource_name}},
            "namespaceSelector": {"matchNames": ["schemabridge-system"]},
            "endpoints": [
                {
                    "port": "metrics",
                    "path": "/metrics",
                    "interval": "30s",
                    "scrapeTimeout": "10s",
                    "scheme": "http",
                }
            ],
        }
        assert policy["spec"]["ingress"] == [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "observability"}
                        },
                        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                    }
                ],
                "ports": [{"protocol": "TCP", "port": 9464}],
            }
        ]

        environment = _settings_environment(documents, component)
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings(_env_file=None)
        assert settings.process_metrics_bind_host == "0.0.0.0"
        assert settings.process_metrics_port == 9464
        assert settings.process_metrics_max_response_bytes == 65_536


def test_runtime_pods_are_digest_pinned_hardened_and_mount_control_trust() -> None:
    documents = _documents()
    for component in RUNTIME_COMPONENTS:
        deployment = _deployment(documents, component)
        pod = deployment["spec"]["template"]["spec"]
        container = _container(deployment)
        security = container["securityContext"]
        volumes = {item["name"]: item for item in pod["volumes"]}
        mounts = {item["name"]: item for item in container["volumeMounts"]}

        assert deployment["spec"]["replicas"] >= 2
        assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        assert pod["serviceAccountName"] == f"schemabridge-{component}"
        assert pod["automountServiceAccountToken"] is False
        assert pod["enableServiceLinks"] is False
        assert pod["hostNetwork"] is pod["hostPID"] is pod["hostIPC"] is False
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        assert {item["topologyKey"] for item in pod["topologySpreadConstraints"]} == {
            "kubernetes.io/hostname",
            "topology.kubernetes.io/zone",
        }
        assert container["image"].endswith(f"@sha256:{VALID_DIGEST}")
        assert security["runAsNonRoot"] is True
        assert security["runAsUser"] == security["runAsGroup"] == 10001
        assert security["allowPrivilegeEscalation"] is False
        assert security["readOnlyRootFilesystem"] is True
        assert security["capabilities"] == {"drop": ["ALL"]}
        assert set(container["resources"]["requests"]) == {
            "cpu",
            "memory",
            "ephemeral-storage",
        }
        assert set(container["resources"]["limits"]) == {
            "cpu",
            "memory",
            "ephemeral-storage",
        }
        assert volumes["trust-bundle"]["configMap"]["name"] == "schemabridge-trust-bundle"
        assert mounts["trust-bundle"] == {
            "name": "trust-bundle",
            "mountPath": "/var/run/secrets/schemabridge/trust",
            "readOnly": True,
        }


def test_only_connector_secret_readers_receive_short_lived_projected_identity() -> None:
    documents = _documents()
    for component in RUNTIME_COMPONENTS:
        deployment = _deployment(documents, component)
        pod = deployment["spec"]["template"]["spec"]
        container = _container(deployment)
        volumes = {item["name"]: item for item in pod["volumes"]}
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        env_from = {item["configMapRef"]["name"] for item in container.get("envFrom", ())}

        if component not in REMOTE_IDENTITY_COMPONENTS:
            assert "workload-identity" not in volumes
            assert "workload-identity" not in mounts
            assert "schemabridge-remote-secret-config" not in env_from
            assert "annotations" not in deployment["spec"]["template"]["metadata"]
            continue

        identity = volumes["workload-identity"]["projected"]
        token = identity["sources"][0]["serviceAccountToken"]
        assert identity["defaultMode"] == 0o400
        assert token == {
            "path": "token",
            "audience": "schemabridge-secret-manager",
            "expirationSeconds": 600,
        }
        assert mounts["workload-identity"]["readOnly"] is True
        assert "schemabridge-remote-secret-config" in env_from
        assert deployment["spec"]["template"]["metadata"]["annotations"] == {
            "schemabridge.io/secret-capability": {
                "web": "preflight",
                "worker": "execution",
                "catalog": "catalog",
                "profile": "profile",
                "reconciler": "registry",
            }[component]
        }


def test_remote_secret_configuration_is_https_and_capability_scoped() -> None:
    documents = _documents()
    remote = _resource(
        documents,
        "ConfigMap",
        "schemabridge-remote-secret-config",
    )["data"]
    component_configs = {
        component: _resource(
            documents,
            "ConfigMap",
            f"schemabridge-{component}-config",
        )["data"]
        for component in RUNTIME_COMPONENTS
    }

    assert remote == {
        "SCHEMABRIDGE_CONNECTOR_SECRET_MODE": "remote",
        "SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL": ("https://vault.prod.example.com"),
        "SCHEMABRIDGE_CONNECTOR_SECRET_KV_MOUNT": "schemabridge-kv",
        "SCHEMABRIDGE_CONNECTOR_SECRET_CA_BUNDLE": ("/var/run/secrets/schemabridge/trust/ca.crt"),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_TOKEN_FILE": (
            "/var/run/secrets/schemabridge/identity/token"
        ),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_ROOT": ("/var/run/secrets/schemabridge/identity"),
        "SCHEMABRIDGE_WORKLOAD_IDENTITY_AUDIENCE": ("schemabridge-secret-manager"),
    }
    roles = {
        component_configs[component]["SCHEMABRIDGE_CONNECTOR_SECRET_ROLE"]
        for component in REMOTE_SECRET_COMPONENTS
    }
    assert len(roles) == len(REMOTE_SECRET_COMPONENTS)
    assert {
        component: component_configs[component]["SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY"]
        for component in REMOTE_SECRET_COMPONENTS
    } == {
        "web": "preflight",
        "worker": "execution",
        "catalog": "catalog",
        "profile": "profile",
    }
    registry_roles = {
        component_configs[component]["SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE"]
        for component in REGISTRY_SECRET_COMPONENTS
    }
    assert len(registry_roles) == len(REGISTRY_SECRET_COMPONENTS)
    assert not roles & registry_roles
    assert {
        component_configs[component]["SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF"]
        for component in REGISTRY_SECRET_COMPONENTS
    } == {"registry.reader.primary"}
    assert {
        component_configs[component]["SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_VERSION"]
        for component in REGISTRY_SECRET_COMPONENTS
    } == {"17"}
    for component in {"api", "observer"}:
        assert "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE" not in component_configs[component]
        assert "SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY" not in component_configs[component]
    serialized = yaml.safe_dump_all(documents)
    assert "secretKeyRef:" in serialized
    assert "secretRef:" not in serialized
    assert "postgresql://" not in serialized
    assert "synthetic-control-password" not in serialized
    assert "OPENAI_API_KEY" not in serialized


def test_external_runtime_secret_references_are_exact_separated_and_content_free() -> None:
    documents = _documents()
    assert not [item for item in documents if item["kind"] == "Secret"]
    secret_names: set[str] = set()
    reference_count = 0
    secret_components = {*RUNTIME_COMPONENTS, "backup"}

    for component in secret_components:
        environment = (
            _backup_container(documents)["env"]
            if component == "backup"
            else _container(_deployment(documents, component))["env"]
        )
        secret_entries = {
            item["name"]: item["valueFrom"]["secretKeyRef"]
            for item in environment
            if "valueFrom" in item and "secretKeyRef" in item["valueFrom"]
        }
        assert {
            variable: reference["key"] for variable, reference in secret_entries.items()
        } == VALIDATOR.EXPECTED_SECRET_ENV[component]
        component_names = {reference["name"] for reference in secret_entries.values()}
        assert component_names == {f"schemabridge-external-{component}-v17"}
        secret_names.update(component_names)
        reference_count += len(secret_entries)
        assert all(set(reference) == {"name", "key"} for reference in secret_entries.values())

    assert reference_count == 16
    assert len(secret_names) == len(secret_components)
    rendered = _rendered_text()
    for component in secret_components:
        for variable in VALIDATOR.EXPECTED_SECRET_ENV[component]:
            assert _synthetic_secret_value(component, variable) not in rendered


def test_each_final_config_map_composes_with_settings_and_only_synthetic_test_secrets() -> None:
    documents = _documents()
    settings_by_component: dict[str, Settings] = {}

    for component in RUNTIME_COMPONENTS:
        environment = _settings_environment(documents, component)
        with patch.dict(os.environ, environment, clear=True):
            settings_by_component[component] = Settings(_env_file=None)

    assert {
        component: settings.runtime_component
        for component, settings in settings_by_component.items()
    } == {
        "web": "web",
        "api": "api",
        "worker": "worker",
        "catalog": "catalog",
        "profile": "worker",
        "reconciler": "reconciler",
        "observer": "observer",
    }
    for component, settings in settings_by_component.items():
        assert settings.environment == "production"
        assert settings.control_plane_kind == "postgres"
        assert settings.registry_kind == "live"
        assert settings.semantic_registry_selection == "active"
        assert settings.query_studio_ai_mode == "disabled"
        if component in REMOTE_IDENTITY_COMPONENTS:
            assert settings.connector_secret_mode == "remote"
            if component in REMOTE_SECRET_COMPONENTS:
                assert (
                    settings.connector_secret_capability
                    == {
                        "web": "preflight",
                        "worker": "execution",
                        "catalog": "catalog",
                        "profile": "profile",
                    }[component]
                )
            else:
                assert settings.connector_secret_capability is None
            assert settings.workload_identity_token_file == Path(
                "/var/run/secrets/schemabridge/identity/token"
            )
            if component in REGISTRY_SECRET_COMPONENTS:
                assert settings.semantic_registry_secret_binding_ref == ("registry.reader.primary")
                assert settings.semantic_registry_secret_version == 17
        else:
            assert settings.connector_secret_mode == "local"
            assert settings.connector_secret_capability is None
            assert settings.workload_identity_token_file is None


def test_runtime_field_identity_and_secret_references_cannot_cross_components() -> None:
    documents = _documents()
    expected_fields = {
        "web": {},
        "api": {},
        "worker": {"SCHEMABRIDGE_WORKER_ID": "metadata.name"},
        "catalog": {"SCHEMABRIDGE_CATALOG_INDEXER_ID": "metadata.name"},
        "profile": {"SCHEMABRIDGE_WORKER_ID": "metadata.name"},
        "reconciler": {"SCHEMABRIDGE_SEMANTIC_RECONCILER_ID": "metadata.name"},
        "observer": {},
    }
    for component in RUNTIME_COMPONENTS:
        environment = _container(_deployment(documents, component))["env"]
        field_entries = {
            item["name"]: item["valueFrom"]["fieldRef"]["fieldPath"]
            for item in environment
            if "fieldRef" in item["valueFrom"]
        }
        assert field_entries == expected_fields[component]


def test_networking_is_default_deny_and_capability_specific() -> None:
    documents = _documents()
    policies = {
        item["metadata"]["name"]: item for item in documents if item["kind"] == "NetworkPolicy"
    }
    default = policies["default-deny-all"]["spec"]
    expected = VALIDATOR.EXPECTED_CAPABILITIES

    assert default == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    }
    assert set(policies) == {
        "default-deny-all",
        *(f"schemabridge-{component}" for component in COMPONENTS),
    }
    for component in COMPONENTS:
        spec = policies[f"schemabridge-{component}"]["spec"]
        capabilities = {
            peer["podSelector"]["matchLabels"]["schemabridge.io/egress-capability"]
            for rule in spec["egress"]
            for peer in rule["to"]
            if "schemabridge.io/egress-capability"
            in peer.get("podSelector", {}).get("matchLabels", {})
        }
        assert capabilities == expected[component]
        assert {"Ingress", "Egress"} == set(spec["policyTypes"])
    serialized_policies = yaml.safe_dump_all(policies.values())
    assert "opentelemetry-collector" not in serialized_policies
    assert "port: 4317" not in serialized_policies
    for component in {"migrator", "backup"}:
        assert policies[f"schemabridge-{component}"]["spec"]["ingress"] == []
    assert "secret-manager" not in expected["observer"]


def test_tls_ingress_resilience_and_namespace_resource_governance_are_explicit() -> None:
    documents = _documents()
    ingresses = [item for item in documents if item["kind"] == "Ingress"]
    budgets = [item for item in documents if item["kind"] == "PodDisruptionBudget"]
    namespace = _resource(documents, "Namespace", "schemabridge-system")
    quota = _resource(
        documents,
        "ResourceQuota",
        "schemabridge-production-budget",
    )

    assert len(ingresses) == 2
    for ingress in ingresses:
        annotations = ingress["metadata"]["annotations"]
        assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
        assert annotations["nginx.ingress.kubernetes.io/force-ssl-redirect"] == "true"
        assert annotations["nginx.ingress.kubernetes.io/hsts"] == "true"
        assert int(annotations["nginx.ingress.kubernetes.io/hsts-max-age"]) >= 31_536_000
        assert ingress["spec"]["tls"][0]["hosts"] == [ingress["spec"]["rules"][0]["host"]]
    assert {
        item["metadata"]["labels"]["app.kubernetes.io/component"] for item in budgets
    } == RUNTIME_COMPONENTS
    assert all(item["spec"]["minAvailable"] == 1 for item in budgets)
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    assert {"requests.cpu", "requests.memory", "limits.cpu", "limits.memory"} <= set(
        quota["spec"]["hard"]
    )
    assert quota["spec"]["hard"]["persistentvolumeclaims"] == "4"
    secret_quota = int(quota["spec"]["hard"]["secrets"])
    assert secret_quota == 13
    assert len(RUNTIME_COMPONENTS) + len(ingresses) + 1 == VALIDATOR.REQUIRED_SECRET_OBJECTS
    assert secret_quota - VALIDATOR.REQUIRED_SECRET_OBJECTS == 3


Mutation = Callable[[list[dict[str, Any]]], None]


def _unhashable_resource_kind(documents: list[dict[str, Any]]) -> None:
    documents[0]["kind"] = []


def _add_unapproved_job(documents: list[dict[str, Any]]) -> None:
    documents.append(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "unapproved",
                "namespace": "schemabridge-system",
            },
            "spec": {},
        }
    )


def _duplicate_backup_cronjob(documents: list[dict[str, Any]]) -> None:
    duplicate = copy.deepcopy(_backup_cronjob(documents))
    duplicate["metadata"]["name"] = "schemabridge-backup-two"
    documents.append(duplicate)


def _wrong_backup_api_version(documents: list[dict[str, Any]]) -> None:
    _backup_cronjob(documents)["apiVersion"] = "batch/v1beta1"


def _wrong_backup_command(documents: list[dict[str, Any]]) -> None:
    _backup_container(documents)["command"] = ["pg_dump"]


def _alternate_backup_image(documents: list[dict[str, Any]]) -> None:
    _backup_container(documents)["image"] = (
        f"registry.example.com/schemabridge/alternate@sha256:{'b' * 64}"
    )


def _wrong_backup_schedule(documents: list[dict[str, Any]]) -> None:
    _backup_cronjob(documents)["spec"]["schedule"] = "* * * * *"


def _wrong_backup_literal_environment(documents: list[dict[str, Any]]) -> None:
    schema = next(
        item
        for item in _backup_container(documents)["env"]
        if item["name"] == "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION"
    )
    schema["value"] = "11"


def _cross_component_backup_secret(documents: list[dict[str, Any]]) -> None:
    secret_entry = next(
        item
        for item in _backup_container(documents)["env"]
        if item["name"] == "SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL"
    )
    secret_entry["valueFrom"]["secretKeyRef"]["name"] = "schemabridge-external-worker-v17"


def _wrong_backup_pvc(documents: list[dict[str, Any]]) -> None:
    pod = _backup_cronjob(documents)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = next(item for item in pod["volumes"] if item["name"] == "backup-store")
    volume["persistentVolumeClaim"]["claimName"] = "shared-unversioned-backups"


def _privileged_backup_container(documents: list[dict[str, Any]]) -> None:
    _backup_container(documents)["securityContext"]["privileged"] = True


def _backup_sidecar(documents: list[dict[str, Any]]) -> None:
    pod = _backup_cronjob(documents)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    sidecar = copy.deepcopy(_backup_container(documents))
    sidecar["name"] = "sidecar"
    pod["containers"].append(sidecar)


def _backup_service_account_secret(documents: list[dict[str, Any]]) -> None:
    service_account = _resource(
        documents,
        "ServiceAccount",
        "schemabridge-backup",
    )
    service_account["secrets"] = [{"name": "externally-created-long-lived-token"}]


def _remove_pvc_quota(documents: list[dict[str, Any]]) -> None:
    quota = _resource(
        documents,
        "ResourceQuota",
        "schemabridge-production-budget",
    )
    quota["spec"]["hard"].pop("persistentvolumeclaims")


def _drift_prometheus_rule(documents: list[dict[str, Any]]) -> None:
    rule = _resource(documents, "PrometheusRule", "schemabridge-active-alerts")
    rule["spec"]["groups"][0]["rules"][0]["expr"] = "vector(1)"


def _mutable_image(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "worker"))["image"] = (
        "registry.example.com/schemabridge/runtime:latest"
    )


def _default_service_account(documents: list[dict[str, Any]]) -> None:
    _deployment(documents, "worker")["spec"]["template"]["spec"]["serviceAccountName"] = "default"


def _automatic_token_mount(documents: list[dict[str, Any]]) -> None:
    _deployment(documents, "api")["spec"]["template"]["spec"]["automountServiceAccountToken"] = True


def _wrong_token_audience(documents: list[dict[str, Any]]) -> None:
    pod = _deployment(documents, "catalog")["spec"]["template"]["spec"]
    identity = next(item for item in pod["volumes"] if item["name"] == "workload-identity")
    identity["projected"]["sources"][0]["serviceAccountToken"]["audience"] = "kubernetes"


def _long_lived_token(documents: list[dict[str, Any]]) -> None:
    pod = _deployment(documents, "profile")["spec"]["template"]["spec"]
    identity = next(item for item in pod["volumes"] if item["name"] == "workload-identity")
    identity["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"] = 86_400


def _host_path(documents: list[dict[str, Any]]) -> None:
    pod = _deployment(documents, "reconciler")["spec"]["template"]["spec"]
    pod["volumes"].append({"name": "host", "hostPath": {"path": "/etc"}})


def _unbounded_egress(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-worker")
    policy["spec"]["egress"].append({})


def _broad_egress_peer(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-worker")
    policy["spec"]["egress"].append(
        {
            "to": [{"namespaceSelector": {}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
    )


def _raw_ip_egress(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-catalog")
    policy["spec"]["egress"].append(
        {
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
    )


def _uncomposed_otlp_egress(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-api")
    policy["spec"]["egress"].append(
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "observability"}
                    },
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "opentelemetry-collector"}
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 4317}],
        }
    )


def _private_ingress(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-migrator")
    policy["spec"]["ingress"] = [{"from": [{"podSelector": {}}]}]


def _broad_api_ingress(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-api")
    policy["spec"]["ingress"].append(
        {
            "from": [{"namespaceSelector": {}}],
            "ports": [{"protocol": "TCP", "port": 8520}],
        }
    )


def _wrong_component_selector(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-profile")
    policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] = "worker"


def _observer_secret_access(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-observer")
    policy["spec"]["egress"].append(
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"schemabridge.io/egress-plane": "true"}},
                    "podSelector": {
                        "matchLabels": {"schemabridge.io/egress-capability": "secret-manager"}
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
    )


def _observer_service_selector_mismatch(documents: list[dict[str, Any]]) -> None:
    service = _resource(documents, "Service", "schemabridge-observer")
    service["spec"]["selector"]["app.kubernetes.io/name"] = "schemabridge-api"


def _observer_split_scrape_namespace(documents: list[dict[str, Any]]) -> None:
    policy = _resource(documents, "NetworkPolicy", "schemabridge-observer")
    policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"][
        "kubernetes.io/metadata.name"
    ] = "monitoring"


def _api_monitor_selector_mismatch(documents: list[dict[str, Any]]) -> None:
    monitor = _resource(documents, "ServiceMonitor", "schemabridge-api")
    monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] = "schemabridge-worker"


def _api_probe_without_allowlisted_host(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "api"))["readinessProbe"]["httpGet"].pop("httpHeaders")


def _web_oidc_secret_name_mismatch(documents: list[dict[str, Any]]) -> None:
    pod = _deployment(documents, "web")["spec"]["template"]["spec"]
    auth = next(item for item in pod["volumes"] if item["name"] == "streamlit-auth")
    auth["secret"]["secretName"] = "schemabridge-external-api-v17"


def _web_readiness_uses_process_only_health(documents: list[dict[str, Any]]) -> None:
    web = _container(_deployment(documents, "web"))
    web["readinessProbe"].pop("exec")
    web["readinessProbe"]["httpGet"] = {
        "path": "/_stcore/health",
        "port": "http",
        "scheme": "HTTP",
    }


def _api_metrics_on_public_port(documents: list[dict[str, Any]]) -> None:
    monitor = _resource(documents, "ServiceMonitor", "schemabridge-api")
    monitor["spec"]["endpoints"][0]["port"] = "http"
    policy = _resource(documents, "NetworkPolicy", "schemabridge-api")
    prometheus_rule = next(
        rule
        for rule in policy["spec"]["ingress"]
        if any(
            peer.get("podSelector", {}).get("matchLabels", {})
            == {"app.kubernetes.io/name": "prometheus"}
            for peer in rule["from"]
        )
    )
    prometheus_rule["ports"] = [{"protocol": "TCP", "port": 8520}]


def _process_metrics_service_mismatch(documents: list[dict[str, Any]]) -> None:
    service = _resource(documents, "Service", "schemabridge-worker")
    service["spec"]["selector"]["app.kubernetes.io/name"] = "schemabridge-catalog"


def _http_secret_provider(documents: list[dict[str, Any]]) -> None:
    config = _resource(
        documents,
        "ConfigMap",
        "schemabridge-remote-secret-config",
    )
    config["data"]["SCHEMABRIDGE_CONNECTOR_SECRET_PROVIDER_URL"] = "http://vault.prod.example.com"


def _duplicate_remote_role(documents: list[dict[str, Any]]) -> None:
    web = _resource(documents, "ConfigMap", "schemabridge-web-config")
    worker = _resource(documents, "ConfigMap", "schemabridge-worker-config")
    worker["data"]["SCHEMABRIDGE_CONNECTOR_SECRET_ROLE"] = web["data"][
        "SCHEMABRIDGE_CONNECTOR_SECRET_ROLE"
    ]


def _direct_secret_environment(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "api"))["env"].append(
        {
            "name": "OPENAI_API_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "forbidden",
                    "key": "openai",
                }
            },
        }
    )


def _remote_config_for_api(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "api"))["envFrom"].append(
        {"configMapRef": {"name": "schemabridge-remote-secret-config"}}
    )


def _connector_capability_for_reconciler(documents: list[dict[str, Any]]) -> None:
    reconciler = _resource(documents, "ConfigMap", "schemabridge-reconciler-config")
    reconciler["data"]["SCHEMABRIDGE_CONNECTOR_SECRET_ROLE"] = "forbidden-reconciler-role"
    reconciler["data"]["SCHEMABRIDGE_CONNECTOR_SECRET_CAPABILITY"] = "profile"


def _missing_projected_identity_for_reconciler(documents: list[dict[str, Any]]) -> None:
    reconciler_pod = _deployment(documents, "reconciler")["spec"]["template"]["spec"]
    reconciler_pod["volumes"] = [
        item for item in reconciler_pod["volumes"] if item["name"] != "workload-identity"
    ]
    container = _container(_deployment(documents, "reconciler"))
    container["volumeMounts"] = [
        item for item in container["volumeMounts"] if item["name"] != "workload-identity"
    ]


def _registry_role_reuses_connector_role(documents: list[dict[str, Any]]) -> None:
    web = _resource(documents, "ConfigMap", "schemabridge-web-config")["data"]
    web["SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_ROLE"] = web["SCHEMABRIDGE_CONNECTOR_SECRET_ROLE"]


def _registry_binding_diverges(documents: list[dict[str, Any]]) -> None:
    reconciler = _resource(
        documents,
        "ConfigMap",
        "schemabridge-reconciler-config",
    )["data"]
    reconciler["SCHEMABRIDGE_SEMANTIC_REGISTRY_SECRET_BINDING_REF"] = "registry.reader.unapproved"


def _cross_component_external_secret(documents: list[dict[str, Any]]) -> None:
    worker_environment = _container(_deployment(documents, "worker"))["env"]
    control = next(
        item
        for item in worker_environment
        if item["name"] == "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL"
    )
    control["valueFrom"]["secretKeyRef"]["name"] = "schemabridge-external-web-v17"


def _wrong_external_secret_key(documents: list[dict[str, Any]]) -> None:
    api_environment = _container(_deployment(documents, "api"))["env"]
    cursor = next(
        item
        for item in api_environment
        if item["name"] == "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY"
    )
    cursor["valueFrom"]["secretKeyRef"]["key"] = "control-api-dsn"


def _embedded_secret_volume(documents: list[dict[str, Any]]) -> None:
    pod = _deployment(documents, "worker")["spec"]["template"]["spec"]
    trust = next(item for item in pod["volumes"] if item["name"] == "trust-bundle")
    trust.pop("configMap")
    trust["secret"] = {"secretName": "forbidden"}


def _remove_tls(documents: list[dict[str, Any]]) -> None:
    _resource(documents, "Ingress", "schemabridge-api")["spec"]["tls"] = []


def _remove_resources(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "api"))["resources"]["limits"].pop("memory")


def _remove_pdb(documents: list[dict[str, Any]]) -> None:
    documents.remove(_resource(documents, "PodDisruptionBudget", "schemabridge-catalog"))


def _insufficient_secret_quota(documents: list[dict[str, Any]]) -> None:
    quota = _resource(
        documents,
        "ResourceQuota",
        "schemabridge-production-budget",
    )
    quota["spec"]["hard"]["secrets"] = "9"


def _invent_binary(documents: list[dict[str, Any]]) -> None:
    _container(_deployment(documents, "web"))["command"] = ["schemabridge-unimplemented-observer"]


def _grant_application_rbac(documents: list[dict[str, Any]]) -> None:
    documents.append(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": "unsafe",
                "namespace": "schemabridge-system",
            },
            "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}],
        }
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (_unhashable_resource_kind, "resource_identity"),
        (_add_unapproved_job, "unsupported_workload_kind"),
        (_duplicate_backup_cronjob, "backup_cronjob_set"),
        (_wrong_backup_api_version, "resource_api_contract"),
        (_wrong_backup_command, "backup_command"),
        (_alternate_backup_image, "workload_image_contract"),
        (_wrong_backup_schedule, "backup_schedule_contract"),
        (_wrong_backup_literal_environment, "backup_runtime_environment"),
        (_cross_component_backup_secret, "backup_runtime_environment"),
        (_wrong_backup_pvc, "backup_storage_contract"),
        (_privileged_backup_container, "backup_container_security"),
        (_backup_sidecar, "backup_container_count"),
        (_backup_service_account_secret, "backup_service_account_contract"),
        (_remove_pvc_quota, "namespace_resource_bounds"),
        (_drift_prometheus_rule, "prometheus_rule_contract"),
        (_mutable_image, "mutable_image"),
        (_default_service_account, "pod_identity_boundary"),
        (_automatic_token_mount, "pod_identity_boundary"),
        (_wrong_token_audience, "identity_token_projection"),
        (_long_lived_token, "identity_token_projection"),
        (_host_path, "workload_volume_set"),
        (_unbounded_egress, "unbounded_egress"),
        (_broad_egress_peer, "unapproved_egress_peer"),
        (_raw_ip_egress, "raw_ip_egress"),
        (_uncomposed_otlp_egress, "unapproved_egress_peer"),
        (_private_ingress, "private_workload_ingress"),
        (_broad_api_ingress, "unapproved_ingress_peer"),
        (_wrong_component_selector, "component_network_selector"),
        (_observer_secret_access, "observer_secret_access"),
        (_observer_service_selector_mismatch, "observer_service_contract"),
        (_observer_split_scrape_namespace, "unapproved_ingress_peer"),
        (_api_monitor_selector_mismatch, "api_monitor_contract"),
        (_api_probe_without_allowlisted_host, "api_probe_host_contract"),
        (_web_oidc_secret_name_mismatch, "streamlit_auth_secret"),
        (_web_readiness_uses_process_only_health, "web_probe_contract"),
        (_api_metrics_on_public_port, "api_monitor_contract"),
        (_process_metrics_service_mismatch, "process_metrics_service_contract"),
        (_http_secret_provider, "remote_secret_url"),
        (_duplicate_remote_role, "remote_secret_role"),
        (_direct_secret_environment, "runtime_environment_set"),
        (_remote_config_for_api, "runtime_config_boundary"),
        (_connector_capability_for_reconciler, "remote_secret_forbidden_component"),
        (_missing_projected_identity_for_reconciler, "workload_volume_set"),
        (_registry_role_reuses_connector_role, "registry_secret_role"),
        (_registry_binding_diverges, "registry_secret_binding"),
        (_cross_component_external_secret, "runtime_secret_ref"),
        (_wrong_external_secret_key, "runtime_secret_ref"),
        (_embedded_secret_volume, "embedded_private_material"),
        (_remove_tls, "ingress_tls_hosts"),
        (_remove_resources, "resource_bounds"),
        (_remove_pdb, "disruption_budget_set"),
        (_insufficient_secret_quota, "namespace_secret_quota"),
        (_invent_binary, "unknown_workload_command"),
        (_grant_application_rbac, "application_kubernetes_rbac"),
    ),
)
def test_validator_rejects_security_and_operability_regressions(
    mutation: Mutation,
    code: str,
) -> None:
    documents = _documents()
    mutation(documents)

    _expect_error(documents, code)


def test_duplicate_yaml_keys_are_rejected_before_resource_validation() -> None:
    duplicate = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: one
  name: two
"""

    with pytest.raises(VALIDATOR.ManifestValidationError) as error:
        VALIDATOR.load_documents(duplicate)

    assert error.value.codes == ("duplicate_yaml_key",)


def test_validator_cli_emits_only_safe_codes_and_never_manifest_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid_path = tmp_path / "rendered.yaml"
    valid_path.write_text(_rendered_text(), encoding="utf-8")
    assert VALIDATOR.main([str(valid_path)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == "m29_manifest_valid resources=63\n"

    sensitive_sentinel = "sensitive-provider-payload-must-not-echo"
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text(
        f"apiVersion: v1\nkind: ConfigMap\n{sensitive_sentinel}: true\n",
        encoding="utf-8",
    )
    assert VALIDATOR.main([str(invalid_path)]) == 1
    output = capsys.readouterr()
    assert "m29_manifest_invalid:" in output.err
    assert sensitive_sentinel not in output.err
    assert str(invalid_path) not in output.err
