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


def main() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    token = _token()
    feature_data = _graphql(
        token,
        "query { appConfig { featureFlags { logicalModelsEnabled } } }",
        {},
    )
    if not feature_data["appConfig"]["featureFlags"]["logicalModelsEnabled"]:
        raise SystemExit("DataHub logical-model UI support is not enabled.")
    for expected in fixture["datasets"]:
        deadline = time.monotonic() + 30
        while True:
            dataset = _dataset(token, expected["urn"])
            profiles = dataset.get("datasetProfiles") or []
            if profiles and profiles[0].get("rowCount") == expected["row_count"]:
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"Missing or unexpected profile for {expected['urn']}.")
            time.sleep(1)
        properties = dataset.get("properties") or {}
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
        "5 datasets, descriptions, schemas, and profiles verified."
    )


if __name__ == "__main__":
    main()
