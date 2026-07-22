#!/usr/bin/env python3
"""Provision a dedicated local DataHub service-account token for read-only MCP."""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DISPLAY_NAME = "SchemaBridge MCP Reader"
DESCRIPTION = "Dedicated read-only identity for the local SchemaBridge M04 MCP connection."
TOKEN_NAME = "schemabridge-m04-mcp"
GRAPHQL_URL = "http://127.0.0.1:8080/api/graphql"
LOCAL_DIR = Path(".local/datahub")
MCP_ENV_PATH = LOCAL_DIR / "mcp.env"
AUDIT_PATH = LOCAL_DIR / "provision-audit.json"


def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"DataHub GraphQL request failed with HTTP {exc.code}.") from exc
    if not isinstance(payload, dict) or payload.get("errors"):
        raise SystemExit("DataHub GraphQL request returned an error.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("DataHub GraphQL response did not contain data.")
    return data


def _admin_token() -> str:
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


def _existing_runtime_token(actor_urn: str) -> str | None:
    if not MCP_ENV_PATH.is_file():
        return None
    values: dict[str, str] = {}
    for line in MCP_ENV_PATH.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    token = values.get("DATAHUB_GMS_TOKEN")
    if not token:
        return None
    try:
        data = _graphql(token, "query { me { corpUser { urn } } }", {})
    except SystemExit:
        return None
    me = data.get("me")
    if not isinstance(me, dict):
        return None
    corp_user = me.get("corpUser")
    if not isinstance(corp_user, dict) or corp_user.get("urn") != actor_urn:
        return None
    return token


def _service_account_urn(admin_token: str) -> tuple[str, str]:
    query = """
    query FindServiceAccount($input: ListServiceAccountsInput!) {
      listServiceAccounts(input: $input) {
        serviceAccounts { urn displayName description }
      }
    }
    """
    result = _graphql(
        admin_token,
        query,
        {"input": {"start": 0, "count": 100, "query": DISPLAY_NAME}},
    )
    accounts = result["listServiceAccounts"]["serviceAccounts"]
    for account in accounts:
        if account.get("displayName") == DISPLAY_NAME:
            return str(account["urn"]), "reused"

    mutation = """
    mutation CreateServiceAccount($input: CreateServiceAccountInput!) {
      createServiceAccount(input: $input) { urn displayName description }
    }
    """
    result = _graphql(
        admin_token,
        mutation,
        {"input": {"displayName": DISPLAY_NAME, "description": DESCRIPTION}},
    )
    return str(result["createServiceAccount"]["urn"]), "created"


def _create_service_token(admin_token: str, actor_urn: str) -> str:
    mutation = """
    mutation CreateAccessToken($input: CreateAccessTokenInput!) {
      createAccessToken(input: $input) { accessToken }
    }
    """
    result = _graphql(
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
    )
    token = result["createAccessToken"]["accessToken"]
    if not isinstance(token, str) or not token:
        raise SystemExit("DataHub did not return a service-account token.")
    return token


def _verify_read_only_identity(token: str, actor_urn: str) -> None:
    query = """
    query VerifyMcpIdentity {
      me {
        corpUser { urn }
        platformPrivileges {
          managePolicies
          manageIdentities
          manageIngestion
          manageSecrets
          manageTokens
          manageGlossaries
          manageDocuments
          createTags
          manageTags
          manageStructuredProperties
          manageServiceAccounts
        }
      }
    }
    """
    me = _graphql(token, query, {})["me"]
    if me["corpUser"]["urn"] != actor_urn:
        raise SystemExit("DataHub service token actor does not match the MCP service account.")
    granted = [name for name, value in me["platformPrivileges"].items() if value]
    if granted:
        raise SystemExit(f"DataHub MCP identity has unexpected platform privileges: {granted}")


def _write_runtime(token: str, actor_urn: str, account_action: str, token_action: str) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        (
            "DATAHUB_GMS_URL=http://127.0.0.1:8080",
            f"DATAHUB_GMS_TOKEN={token}",
            "TOOLS_IS_MUTATION_ENABLED=false",
            "SAVE_DOCUMENT_TOOL_ENABLED=false",
            "DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED=true",
            "",
        )
    )
    MCP_ENV_PATH.write_text(content)
    MCP_ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    audit = {
        "actor_urn": actor_urn,
        "service_account": account_action,
        "token": token_action,
        "token_duration": "ONE_MONTH",
        "token_value_recorded": False,
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    AUDIT_PATH.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")


def main() -> None:
    admin_token = _admin_token()
    actor_urn, account_action = _service_account_urn(admin_token)
    existing_token = _existing_runtime_token(actor_urn)
    if existing_token is None:
        service_token = _create_service_token(admin_token, actor_urn)
        token_action = "created"
    else:
        service_token = existing_token
        token_action = "reused"
    _verify_read_only_identity(service_token, actor_urn)
    _write_runtime(service_token, actor_urn, account_action, token_action)
    print(
        f"DataHub MCP identity {account_action}; token {token_action}; "
        "runtime secret stored with mode 0600."
    )


if __name__ == "__main__":
    os.umask(0o077)
    main()
