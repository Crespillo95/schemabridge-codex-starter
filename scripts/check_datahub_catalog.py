#!/usr/bin/env python3
"""Verify the ingested synthetic datasets, descriptions, fields, and profiles."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

FIXTURE_PATH = Path("tests/fixtures/datahub/mcp_catalog.json")
GRAPHQL_URL = "http://127.0.0.1:8080/api/graphql"
DATASET_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge."
DATASET_URN_SUFFIX = ",PROD)"
EXPECTED_DATASETS = frozenset(
    {
        "bank.account_holders",
        "bank.accounts",
        "commerce.products",
        "crm.customers",
        "fulfillment.shipments",
        "legacy.client_master",
        "legacy.item_master",
        "reporting.customer_accounts",
        "sales.order_lines",
        "sales.orders",
        "support.order_cases",
    }
)


def _token() -> str:
    config_path = Path.home() / ".datahubenv"
    try:
        config = yaml.safe_load(config_path.read_text())
        token = config["gms"]["token"]
    except (FileNotFoundError, KeyError, TypeError) as exc:
        raise SystemExit(
            "DataHub admin token is unavailable; run make datahub-init-admin."
        ) from exc
    if not isinstance(token, str) or not token:
        raise SystemExit("DataHub admin token is empty; run make datahub-init-admin.")
    return token


def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"DataHub catalog check failed with HTTP {exc.code}.") from exc
    data = payload.get("data")
    if not isinstance(data, dict) or payload.get("errors"):
        raise SystemExit("DataHub catalog GraphQL check returned an error.")
    return data


def _dataset(token: str, urn: str) -> dict[str, Any]:
    query = """
    query CatalogDataset($urn: String!) {
      dataset(urn: $urn) {
        urn
        properties { name description }
        schemaMetadata { fields { fieldPath description } }
        datasetProfiles(limit: 1) { rowCount columnCount }
      }
    }
    """
    dataset = _graphql(token, query, {"urn": urn}).get("dataset")
    if not isinstance(dataset, dict):
        raise SystemExit(f"DataHub catalog check could not read {urn}.")
    return dataset


def _load_expectations(path: Path = FIXTURE_PATH) -> tuple[dict[str, Any], ...]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        datasets = fixture["datasets"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit("DataHub catalog expectations could not be loaded.") from exc
    if fixture.get("fixture_kind") != "sanitized_mcp_expectation":
        raise SystemExit("DataHub catalog expectation kind is unsupported.")
    if not isinstance(datasets, list) or not all(isinstance(item, dict) for item in datasets):
        raise SystemExit("DataHub catalog expectations contain invalid datasets.")
    expected = tuple(datasets)
    names = tuple(_expectation_name(item) for item in expected)
    if len(set(names)) != len(names) or frozenset(names) != EXPECTED_DATASETS:
        raise SystemExit("DataHub catalog expectations do not name the exact synthetic corpus.")
    return expected


def _expectation_name(item: dict[str, Any]) -> str:
    urn = item.get("urn")
    fields = item.get("fields")
    described_fields = item.get("described_fields")
    row_count = item.get("row_count")
    if (
        not isinstance(urn, str)
        or not urn.startswith(DATASET_URN_PREFIX)
        or not urn.endswith(DATASET_URN_SUFFIX)
        or not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 0
        or not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field for field in fields)
        or len(set(fields)) != len(fields)
        or not isinstance(described_fields, list)
        or any(
            not isinstance(field, str) or not field or field not in fields
            for field in described_fields
        )
        or len(set(described_fields)) != len(described_fields)
    ):
        raise SystemExit("DataHub catalog expectations contain an invalid dataset contract.")
    return urn.removeprefix(DATASET_URN_PREFIX).removesuffix(DATASET_URN_SUFFIX)


def main() -> None:
    expectations = _load_expectations()
    token = _token()
    feature_data = _graphql(
        token,
        "query { appConfig { featureFlags { logicalModelsEnabled } } }",
        {},
    )
    if not feature_data["appConfig"]["featureFlags"]["logicalModelsEnabled"]:
        raise SystemExit("DataHub logical-model UI support is not enabled.")
    for expected in expectations:
        deadline = time.monotonic() + 30
        while True:
            dataset = _dataset(token, expected["urn"])
            profiles = dataset.get("datasetProfiles") or []
            if (
                profiles
                and profiles[0].get("rowCount") == expected["row_count"]
                and profiles[0].get("columnCount") == len(expected["fields"])
            ):
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"Missing or unexpected profile for {expected['urn']}.")
            time.sleep(1)
        properties = dataset.get("properties") or {}
        if dataset.get("urn") != expected["urn"]:
            raise SystemExit(f"Unexpected dataset identity for {expected['urn']}.")
        if not properties.get("description"):
            raise SystemExit(f"Missing table description for {expected['urn']}.")
        fields = {
            field["fieldPath"]: field.get("description")
            for field in (dataset.get("schemaMetadata") or {}).get("fields", [])
        }
        if set(fields) != set(expected["fields"]):
            raise SystemExit(f"Unexpected schema fields for {expected['urn']}.")
        for field_name in expected["described_fields"]:
            if not fields.get(field_name):
                raise SystemExit(f"Missing field description for {expected['urn']}::{field_name}.")
    print(
        "DataHub catalog check passed: logical-model UI enabled; "
        f"{len(expectations)} datasets, descriptions, schemas, and profiles verified."
    )


if __name__ == "__main__":
    main()
