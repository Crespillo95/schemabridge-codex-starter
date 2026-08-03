"""Dedicated DataHub v2 publication/read-back contract tests for M34."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from tests.unit.test_registry_publication_v2 import (
    NOW,
    OPAQUE_CUSTOMERS_URN,
    OPAQUE_ORDERS_URN,
    _authorization,
    _FakeDataHubV2Client,
    _proposal,
    _v2_publisher,
)

from schemabridge.adapters.semantic_registry.datahub import (
    DataHubHttpRegistryReadClient,
    DataHubRegistryIdentity,
)
from schemabridge.application.ports.planning import (
    RegistryPublicationError,
    RegistryPublicationErrorCode,
)
from schemabridge.domain.registry_publication import assemble_publishable_registry_version

_PLATFORM_PRIVILEGES = {
    "managePolicies": False,
    "manageIdentities": False,
    "manageIngestion": False,
    "manageSecrets": False,
    "manageTokens": False,
    "manageServiceAccounts": False,
    "generatePersonalAccessTokens": False,
    "manageGlossaries": False,
    "manageDocuments": True,
    "createTags": False,
    "manageTags": False,
    "manageStructuredProperties": False,
}
_TARGET_PRIVILEGES = {
    "canManageEntity": False,
    "canEditProperties": False,
    "canEditDescription": False,
    "canEditTags": False,
    "canEditGlossaryTerms": False,
    "canEditOwners": False,
    "canEditDomains": False,
}


def test_http_writer_identity_accepts_an_explicitly_absent_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: Iterator[dict[str, Any]] = iter(
        (
            {
                "me": {
                    "corpUser": {"urn": "urn:li:corpuser:schemabridge-registry-publisher"},
                    "platformPrivileges": _PLATFORM_PRIVILEGES,
                }
            },
            {
                "document": None,
                "getGrantedPrivileges": {"privileges": []},
            },
        )
    )
    monkeypatch.setattr(
        DataHubHttpRegistryReadClient,
        "_graphql",
        lambda *_args, **_kwargs: next(responses),
    )

    identity = DataHubHttpRegistryReadClient(
        server="http://127.0.0.1:8080",
        token="synthetic-writer-token",
    ).identity("urn:li:document:schemabridge-registry-missing-v1")

    assert identity.actor_urn == "urn:li:corpuser:schemabridge-registry-publisher"
    assert identity.granted_platform_mutation_privileges == frozenset({"manageDocuments"})
    assert identity.granted_target_edit_privileges == frozenset()


def test_http_writer_identity_requires_complete_privileges_for_an_existing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: Iterator[dict[str, Any]] = iter(
        (
            {
                "me": {
                    "corpUser": {"urn": "urn:li:corpuser:schemabridge-registry-publisher"},
                    "platformPrivileges": _PLATFORM_PRIVILEGES,
                }
            },
            {
                "document": {
                    "privileges": {
                        key: value
                        for key, value in _TARGET_PRIVILEGES.items()
                        if key != "canEditDomains"
                    }
                },
                "getGrantedPrivileges": {"privileges": []},
            },
        )
    )
    monkeypatch.setattr(
        DataHubHttpRegistryReadClient,
        "_graphql",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(ValueError, match="target privilege response is incomplete"):
        DataHubHttpRegistryReadClient(
            server="http://127.0.0.1:8080",
            token="synthetic-writer-token",
        ).identity("urn:li:document:schemabridge-registry-existing-v1")


def test_http_writer_identity_rejects_an_ambiguous_target_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: Iterator[dict[str, Any]] = iter(
        (
            {
                "me": {
                    "corpUser": {"urn": "urn:li:corpuser:schemabridge-registry-publisher"},
                    "platformPrivileges": _PLATFORM_PRIVILEGES,
                }
            },
            {"getGrantedPrivileges": {"privileges": []}},
        )
    )
    monkeypatch.setattr(
        DataHubHttpRegistryReadClient,
        "_graphql",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(ValueError, match="target privilege response is incomplete"):
        DataHubHttpRegistryReadClient(
            server="http://127.0.0.1:8080",
            token="synthetic-writer-token",
        ).identity("urn:li:document:schemabridge-registry-ambiguous-v1")


def test_datahub_v2_publishes_only_the_retained_observed_asset_urn() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client()

    result = _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)

    assert result.status == "published"
    assert result.receipt is not None
    assert result.receipt.related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert client.upserts[0].related_asset_urns == (OPAQUE_ORDERS_URN,)
    assert "sales.orders" not in client.upserts[0].related_asset_urns[0]


def test_datahub_v2_rejects_every_residual_platform_mutation_privilege() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client(
        identity_value=DataHubRegistryIdentity(
            actor_urn="urn:li:corpuser:schemabridge-registry-publisher",
            granted_platform_mutation_privileges=frozenset({"manageDocuments", "manageGlossaries"}),
            granted_target_edit_privileges=frozenset(),
        )
    )

    with pytest.raises(RegistryPublicationError) as raised:
        _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)

    assert raised.value.code is RegistryPublicationErrorCode.CATALOG_PERMISSION_DENIED
    assert client.upserts == []


def test_datahub_v2_post_write_tampering_never_becomes_false_success() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    client = _FakeDataHubV2Client(
        after_upsert=lambda document: replace(
            document,
            related_asset_urns=(OPAQUE_CUSTOMERS_URN,),
        )
    )

    result = _v2_publisher(client).publish(candidate, authorization, observed_at=NOW)

    assert result.status == "failed"
    assert result.reason_code == "post_write_verification_failed"
    assert result.receipt is None


def test_datahub_v2_expired_retry_reads_existing_target_but_cannot_create_one() -> None:
    candidate = assemble_publishable_registry_version(_proposal(), base=None)
    authorization = _authorization(candidate)
    populated = _FakeDataHubV2Client()
    publisher = _v2_publisher(populated)
    publisher.publish(candidate, authorization, observed_at=NOW)

    recovered = publisher.publish(
        candidate,
        authorization,
        observed_at=authorization.expires_at + timedelta(seconds=1),
    )

    assert recovered.status == "already_current"
    assert recovered.observed_authorization_id == authorization.id
    assert len(populated.upserts) == 1

    missing = _FakeDataHubV2Client()
    with pytest.raises(RegistryPublicationError) as raised:
        _v2_publisher(missing).publish(
            candidate,
            authorization,
            observed_at=authorization.expires_at + timedelta(seconds=1),
        )
    assert raised.value.code is RegistryPublicationErrorCode.APPROVAL_MISMATCH
    assert missing.upserts == []
