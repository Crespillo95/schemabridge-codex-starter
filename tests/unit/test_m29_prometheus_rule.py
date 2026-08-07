from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERT_RULES_PATH = ROOT / "deploy" / "observability" / "alert-rules.yaml"
PROMETHEUS_RULE_PATH = ROOT / "deploy" / "kubernetes" / "m29" / "base" / "prometheus-rules.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_prometheus_rule_delivers_exact_active_alert_contract() -> None:
    alert_contract = _load_yaml(ALERT_RULES_PATH)
    prometheus_rule = _load_yaml(PROMETHEUS_RULE_PATH)

    assert set(alert_contract) == {"groups"}
    assert prometheus_rule["spec"] == {"groups": alert_contract["groups"]}


def test_prometheus_rule_has_closed_cluster_metadata() -> None:
    prometheus_rule = _load_yaml(PROMETHEUS_RULE_PATH)

    assert set(prometheus_rule) == {"apiVersion", "kind", "metadata", "spec"}
    assert prometheus_rule["apiVersion"] == "monitoring.coreos.com/v1"
    assert prometheus_rule["kind"] == "PrometheusRule"
    assert prometheus_rule["metadata"] == {
        "name": "schemabridge-active-alerts",
        "namespace": "schemabridge-system",
        "labels": {
            "app.kubernetes.io/name": "schemabridge-active-alerts",
            "app.kubernetes.io/part-of": "schemabridge",
            "app.kubernetes.io/component": "observability",
        },
    }
