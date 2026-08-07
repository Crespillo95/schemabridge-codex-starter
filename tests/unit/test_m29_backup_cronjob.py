from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from scripts.verify_supply_chain import POSTGRES_CLIENT_APK_MATRIX

ROOT = Path(__file__).resolve().parents[2]
CRONJOB = ROOT / "deploy/kubernetes/m29/base/backup-cronjob.yaml"


def _document() -> dict[str, Any]:
    loaded = yaml.safe_load(CRONJOB.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_backup_cronjob_is_hourly_non_overlapping_and_bounded() -> None:
    document = _document()

    assert document["apiVersion"] == "batch/v1"
    assert document["kind"] == "CronJob"
    assert document["metadata"] == {
        "name": "schemabridge-backup",
        "namespace": "schemabridge-system",
        "labels": {
            "app.kubernetes.io/name": "schemabridge-backup",
            "app.kubernetes.io/part-of": "schemabridge",
            "app.kubernetes.io/component": "backup",
        },
    }
    spec = document["spec"]
    assert spec["schedule"] == "0 * * * *"
    assert spec["timeZone"] == "Etc/UTC"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["suspend"] is False
    assert spec["startingDeadlineSeconds"] == 900
    assert spec["jobTemplate"]["spec"]["backoffLimit"] == 1
    assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 1800


def test_backup_cronjob_uses_only_backup_identity_and_external_immutable_store() -> None:
    document = _document()
    pod = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert pod["serviceAccountName"] == "schemabridge-backup"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert container["command"] == ["schemabridge-backup"]
    assert container["args"] == [
        "--destination",
        "/var/lib/schemabridge/backups/hourly",
    ]
    environment = {item["name"]: item for item in container["env"]}
    assert set(environment) == {
        "SCHEMABRIDGE_ENVIRONMENT",
        "SCHEMABRIDGE_COMPONENT",
        "SCHEMABRIDGE_AUTH_MODE",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA",
        "SCHEMABRIDGE_CONTROL_PLANE_SCHEMA_VERSION",
        "SCHEMABRIDGE_CONTROL_AUDIT_KEY_VERSION",
        "SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY",
    }
    assert environment["SCHEMABRIDGE_COMPONENT"]["value"] == "backup"
    for variable, key in {
        "SCHEMABRIDGE_CONTROL_BACKUP_DATABASE_URL": "control-backup-dsn",
        "SCHEMABRIDGE_CONTROL_AUDIT_SIGNING_KEY": "control-audit-signing-key",
    }.items():
        assert environment[variable]["valueFrom"]["secretKeyRef"] == {
            "name": "replace-with-backup-runtime-secret-version",
            "key": key,
        }
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["backup-store"] == {
        "name": "backup-store",
        "persistentVolumeClaim": {
            "claimName": "schemabridge-backup-store-v1",
        },
    }
    serialized = CRONJOB.read_text(encoding="utf-8")
    assert "SCHEMABRIDGE_CONTROL_MIGRATOR_DATABASE_URL" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "postgresql://" not in serialized


def test_runtime_image_installs_exact_signed_postgres_backup_apks_offline() -> None:
    dockerfile = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")

    assert "FROM postgres:" not in dockerfile
    assert dockerfile.count(" AS postgres-client-apks") == 1
    assert dockerfile.count("ARG TARGETARCH") == 1
    assert dockerfile.count("      amd64) \\") == 1
    assert dockerfile.count("      arm64) \\") == 1
    for packages in POSTGRES_CLIENT_APK_MATRIX.values():
        for _variable, url, digest in packages:
            assert dockerfile.count(url) == 1
            assert dockerfile.count(digest) == 1

    assert dockerfile.count("apk add") == 1
    assert (
        "RUN --mount=from=postgres-client-apks,source=/postgres-client-apks,"
        "target=/postgres-client-apks,ro \\\n"
        "    apk add --no-cache --no-network \\\n"
        "    /postgres-client-apks/libpq-18.4-r0.apk \\\n"
        "    /postgres-client-apks/lz4-libs-1.10.0-r1.apk \\\n"
        "    /postgres-client-apks/postgresql-common-1.3-r0.apk \\\n"
        "    /postgres-client-apks/postgresql16-client-16.14-r0.apk \\\n"
        "    /postgres-client-apks/zstd-libs-1.5.7-r2.apk" in dockerfile
    )
    assert "COPY --from=postgres-client-apks" not in dockerfile
    assert "--allow-untrusted" not in dockerfile
