"""Shared creation guard for SchemaBridge decision provenance in DataHub."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from schemabridge.adapters.datahub.canonical_urns import DECISION_PROPERTY_URN

GraphQLCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def ensure_decision_property(graphql: GraphQLCall) -> None:
    """Create the bounded provenance property when publication runs before M07 write-back."""
    existing = graphql(
        """
        query Property($urn: String!) {
          structuredProperty(urn: $urn) { urn definition { qualifiedName } }
        }
        """,
        {"urn": DECISION_PROPERTY_URN},
    ).get("structuredProperty")
    if (
        isinstance(existing, dict)
        and isinstance(existing.get("definition"), dict)
        and existing["definition"].get("qualifiedName") == "io.schemabridge.decisionRef"
    ):
        return
    data = graphql(
        """
        mutation CreateDecisionProperty($input: CreateStructuredPropertyInput!) {
          createStructuredProperty(input: $input) { urn }
        }
        """,
        {
            "input": {
                "id": "io.schemabridge.decisionRef",
                "qualifiedName": "io.schemabridge.decisionRef",
                "displayName": "SchemaBridge decision reference",
                "description": "Immutable SchemaBridge decision identifiers and versions.",
                "valueType": "urn:li:dataType:datahub.string",
                "cardinality": "MULTIPLE",
                "entityTypes": [
                    "urn:li:entityType:datahub.dataset",
                    "urn:li:entityType:datahub.glossaryTerm",
                    "urn:li:entityType:datahub.document",
                ],
            }
        },
    )
    created = data.get("createStructuredProperty")
    if not isinstance(created, dict) or created.get("urn") != DECISION_PROPERTY_URN:
        raise ValueError("DataHub returned an unexpected structured-property identity")


__all__ = ["ensure_decision_property"]
