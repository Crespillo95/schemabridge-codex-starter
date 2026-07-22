#!/usr/bin/env python3
"""Provision the dedicated least-privilege local DataHub semantic-context writer."""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DISPLAY_NAME = "SchemaBridge Approved Writer"
DESCRIPTION = (
    "M07/M08/M12/M13 writer reachable only through explicit SchemaBridge publication approval."
)
TOKEN_NAME = "schemabridge-m07-writer"
GRAPHQL_URL = "http://127.0.0.1:8080/api/graphql"
LOCAL_DIR = Path(".local/datahub")
WRITER_ENV_PATH = LOCAL_DIR / "writer.env"
AUDIT_PATH = LOCAL_DIR / "writer-provision-audit.json"
PLATFORM_POLICY_NAME = "SchemaBridge Writer - Governed Entity Types"
METADATA_POLICY_NAME = "SchemaBridge Writer - Synthetic Canonical Assets"
DOCUMENT_POLICY_NAME = "SchemaBridge Writer - Versioned Decision Documents"
PLATFORM_PRIVILEGES = (
    "MANAGE_GLOSSARIES",
    "MANAGE_STRUCTURED_PROPERTIES",
    "VIEW_STRUCTURED_PROPERTIES_PAGE",
    "MANAGE_DOCUMENTS",
)
# The pinned v1.6.0 GMS authorizer cache refresh is 120 seconds. Poll just beyond
# that window while still failing immediately on actor or privilege expansion.
POLICY_PROPAGATION_ATTEMPTS = 31
POLICY_PROPAGATION_DELAY_SECONDS = 5.0
PHYSICAL_DATASETS = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.legacy.client_master,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.bank.account_holders,PROD)",
    "urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.bank.accounts,PROD)",
)
LOGICAL_DATASET = "urn:li:dataset:(urn:li:dataPlatform:logical,schemabridge.Customer,PROD)"
GOVERNANCE_ENTITIES = (
    "urn:li:structuredProperty:io.schemabridge.decisionRef",
    "urn:li:glossaryTerm:SchemaBridge.Customer.customer_key",
    "urn:li:glossaryTerm:SchemaBridge.Customer.registration_date",
    "urn:li:document:schemabridge-customer-canonical-v5",
    "urn:li:document:schemabridge-join-contracts-current",
)
SCHEMA_FIELDS = (
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD),customer_id)",
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.crm.customers,PROD),registration_date)",
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.legacy.client_master,PROD),client_no)",
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:postgres,schemabridge.bank.account_holders,PROD),gf_customer_id)",
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:logical,schemabridge.Customer,PROD),customer_key)",
    "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:logical,schemabridge.Customer,PROD),registration_date)",
)


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
    except urllib.error.HTTPError as error:
        raise SystemExit(f"DataHub GraphQL request failed with HTTP {error.code}.") from error
    if not isinstance(payload, dict) or payload.get("errors"):
        raise SystemExit("DataHub GraphQL request returned an error.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("DataHub GraphQL response did not contain data.")
    return data


def _admin_token() -> str:
    try:
        value = yaml.safe_load((Path.home() / ".datahubenv").read_text())["gms"]["token"]
    except (FileNotFoundError, KeyError, TypeError) as error:
        raise SystemExit(
            "DataHub admin token is unavailable; run make datahub-init-admin."
        ) from error
    if not isinstance(value, str) or not value:
        raise SystemExit("DataHub admin token is empty; run make datahub-init-admin.")
    return value


def _service_account(admin_token: str) -> tuple[str, str]:
    query = """
    query FindServiceAccount($input: ListServiceAccountsInput!) {
      listServiceAccounts(input: $input) {
        serviceAccounts { urn displayName }
      }
    }
    """
    accounts = _graphql(
        admin_token,
        query,
        {"input": {"start": 0, "count": 100, "query": DISPLAY_NAME}},
    )["listServiceAccounts"]["serviceAccounts"]
    for account in accounts:
        if account.get("displayName") == DISPLAY_NAME:
            return str(account["urn"]), "reused"
    mutation = """
    mutation CreateServiceAccount($input: CreateServiceAccountInput!) {
      createServiceAccount(input: $input) { urn }
    }
    """
    created = _graphql(
        admin_token,
        mutation,
        {"input": {"displayName": DISPLAY_NAME, "description": DESCRIPTION}},
    )
    return str(created["createServiceAccount"]["urn"]), "created"


def _policy_input(
    policy_type: str,
    name: str,
    actor_urn: str,
    privileges: tuple[str, ...],
    resources: tuple[str, ...] | None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": policy_type,
        "name": name,
        "state": "ACTIVE",
        "description": DESCRIPTION,
        "privileges": list(privileges),
        "actors": {
            "users": [actor_urn],
            "groups": [],
            "resourceOwners": False,
            "resourceOwnersTypes": [],
            "allUsers": False,
            "allGroups": False,
        },
    }
    if resources is not None:
        value["resources"] = {"resources": list(resources), "allResources": False}
    return value


def _upsert_policy(
    admin_token: str,
    name: str,
    policy_input: dict[str, Any],
) -> tuple[str, str]:
    query = """
    query FindPolicy($input: ListPoliciesInput!) {
      listPolicies(input: $input) { policies { urn name } }
    }
    """
    policies = _graphql(
        admin_token,
        query,
        {"input": {"start": 0, "count": 100, "query": name}},
    )["listPolicies"]["policies"]
    for policy in policies:
        if policy.get("name") == name:
            mutation = """
            mutation UpdatePolicy($urn: String!, $input: PolicyUpdateInput!) {
              updatePolicy(urn: $urn, input: $input)
            }
            """
            urn = str(policy["urn"])
            _graphql(admin_token, mutation, {"urn": urn, "input": policy_input})
            return urn, "updated"
    mutation = """
    mutation CreatePolicy($input: PolicyUpdateInput!) { createPolicy(input: $input) }
    """
    urn = _graphql(admin_token, mutation, {"input": policy_input})["createPolicy"]
    if not isinstance(urn, str) or not urn:
        raise SystemExit("DataHub did not return a policy URN.")
    return urn, "created"


def _existing_token(actor_urn: str) -> str | None:
    if not WRITER_ENV_PATH.is_file():
        return None
    values: dict[str, str] = {}
    for line in WRITER_ENV_PATH.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    token = values.get("DATAHUB_GMS_TOKEN")
    if not token:
        return None
    try:
        me = _graphql(token, "query { me { corpUser { urn } } }", {})["me"]
    except SystemExit:
        return None
    return token if me["corpUser"]["urn"] == actor_urn else None


def _create_token(admin_token: str, actor_urn: str) -> str:
    mutation = """
    mutation CreateAccessToken($input: CreateAccessTokenInput!) {
      createAccessToken(input: $input) { accessToken }
    }
    """
    token = _graphql(
        admin_token,
        mutation,
        {
            "input": {
                "type": "SERVICE_ACCOUNT",
                "actorUrn": actor_urn,
                "duration": "ONE_MONTH",
                "name": TOKEN_NAME,
                "description": DESCRIPTION,
            }
        },
    )["createAccessToken"]["accessToken"]
    if not isinstance(token, str) or not token:
        raise SystemExit("DataHub did not return a writer service-account token.")
    return token


def _verify_identity(
    token: str,
    actor_urn: str,
    *,
    attempts: int = POLICY_PROPAGATION_ATTEMPTS,
    delay_seconds: float = POLICY_PROPAGATION_DELAY_SECONDS,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    query = """
    query VerifyWriter {
      me {
        corpUser { urn }
        platformPrivileges {
          managePolicies manageIdentities manageIngestion manageSecrets manageTokens
          manageServiceAccounts generatePersonalAccessTokens
          manageGlossaries manageDocuments manageStructuredProperties
          viewStructuredPropertiesPage
        }
      }
    }
    """
    expected_true = {
        # Granted by the stock local "all users" token policy, not by either
        # SchemaBridge writer policy. The writer never calls token mutations.
        "generatePersonalAccessTokens",
        "manageGlossaries",
        "manageDocuments",
        "manageStructuredProperties",
        "viewStructuredPropertiesPage",
    }
    granted: set[str] = set()
    for attempt in range(attempts):
        me = _graphql(token, query, {})["me"]
        if me["corpUser"]["urn"] != actor_urn:
            raise SystemExit("DataHub writer token actor does not match the service account.")
        privileges = me["platformPrivileges"]
        granted = {name for name, value in privileges.items() if value}
        unexpected = granted - expected_true
        if unexpected:
            raise SystemExit(
                f"DataHub writer has unexpected platform privileges: {sorted(unexpected)}."
            )
        if granted == expected_true:
            return
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise SystemExit(
        "DataHub writer platform privileges did not converge to the bounded policy: "
        f"expected={sorted(expected_true)}, granted={sorted(granted)}."
    )


def _write_runtime(
    token: str,
    actor_urn: str,
    account_action: str,
    token_action: str,
    policy_actions: dict[str, str],
) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    WRITER_ENV_PATH.write_text(
        "\n".join(
            (
                "DATAHUB_GMS_URL=http://127.0.0.1:8080",
                f"DATAHUB_GMS_TOKEN={token}",
                f"DATAHUB_WRITER_ACTOR_URN={actor_urn}",
                "",
            )
        )
    )
    WRITER_ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    AUDIT_PATH.write_text(
        json.dumps(
            {
                "actor_urn": actor_urn,
                "service_account": account_action,
                "token": token_action,
                "token_duration": "ONE_MONTH",
                "token_value_recorded": False,
                "policies": policy_actions,
                "metadata_assets": [
                    *PHYSICAL_DATASETS,
                    LOGICAL_DATASET,
                    *GOVERNANCE_ENTITIES,
                    *SCHEMA_FIELDS,
                ],
                "timestamp_utc": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> None:
    admin_token = _admin_token()
    actor_urn, account_action = _service_account(admin_token)
    _, platform_action = _upsert_policy(
        admin_token,
        PLATFORM_POLICY_NAME,
        _policy_input(
            "PLATFORM",
            PLATFORM_POLICY_NAME,
            actor_urn,
            PLATFORM_PRIVILEGES,
            None,
        ),
    )
    _, metadata_action = _upsert_policy(
        admin_token,
        METADATA_POLICY_NAME,
        _policy_input(
            "METADATA",
            METADATA_POLICY_NAME,
            actor_urn,
            ("EDIT_ENTITY",),
            (
                *PHYSICAL_DATASETS,
                LOGICAL_DATASET,
                *GOVERNANCE_ENTITIES,
                *SCHEMA_FIELDS,
            ),
        ),
    )
    document_policy = _policy_input(
        "METADATA",
        DOCUMENT_POLICY_NAME,
        actor_urn,
        ("EDIT_ENTITY",),
        (),
    )
    document_policy["resources"] = {
        "allResources": False,
        "filter": {
            "criteria": [
                {
                    "field": "RESOURCE_URN",
                    "values": [
                        "urn:li:document:schemabridge-customer-canonical-v",
                        "urn:li:document:schemabridge-join-contracts-",
                        "urn:li:document:schemabridge-workflow-",
                        "urn:li:document:schemabridge-query-recipe-",
                    ],
                    "condition": "STARTS_WITH",
                }
            ]
        },
    }
    _, document_action = _upsert_policy(
        admin_token,
        DOCUMENT_POLICY_NAME,
        document_policy,
    )
    token = _existing_token(actor_urn)
    if token is None:
        token = _create_token(admin_token, actor_urn)
        token_action = "created"
    else:
        token_action = "reused"
    _verify_identity(token, actor_urn)
    _write_runtime(
        token,
        actor_urn,
        account_action,
        token_action,
        {
            PLATFORM_POLICY_NAME: platform_action,
            METADATA_POLICY_NAME: metadata_action,
            DOCUMENT_POLICY_NAME: document_action,
        },
    )
    print(
        f"DataHub writer identity {account_action}; token {token_action}; "
        "bounded policies active; runtime secret stored with mode 0600."
    )


if __name__ == "__main__":
    os.umask(0o077)
    main()
