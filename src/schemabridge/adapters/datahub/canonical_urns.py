"""Deterministic DataHub identities for the bounded synthetic M07 publication."""

LOGICAL_CUSTOMER_URN = "urn:li:dataset:(urn:li:dataPlatform:logical,schemabridge.Customer,PROD)"
DECISION_PROPERTY_URN = "urn:li:structuredProperty:io.schemabridge.decisionRef"
CUSTOMER_KEY_TERM_URN = "urn:li:glossaryTerm:SchemaBridge.Customer.customer_key"
REGISTRATION_DATE_TERM_URN = "urn:li:glossaryTerm:SchemaBridge.Customer.registration_date"

__all__ = [
    "CUSTOMER_KEY_TERM_URN",
    "DECISION_PROPERTY_URN",
    "LOGICAL_CUSTOMER_URN",
    "REGISTRATION_DATE_TERM_URN",
]
