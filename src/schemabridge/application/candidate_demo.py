"""Explicit M06 demo concept; this is not a production concept registry."""

from schemabridge.domain.concepts import CanonicalField, CanonicalType, LogicalFieldRef
from schemabridge.domain.transformations import (
    EmptyStringPolicy,
    IdentifierNormalizationPlan,
    LeadingZeroPolicy,
    NullPolicy,
)


def build_customer_key_concept() -> CanonicalField:
    """Return the north-star Customer key definition used by the recorded demo."""

    return CanonicalField(
        id=LogicalFieldRef("Customer.customer_key"),
        canonical_name="customer_key",
        canonical_type=CanonicalType.STRING,
        definition="Stable identifier for a customer across source systems.",
        format_policy=IdentifierNormalizationPlan(
            null_policy=NullPolicy.PRESERVE,
            leading_zero_policy=LeadingZeroPolicy.STRIP,
            empty_string_policy=EmptyStringPolicy.REJECT,
        ),
    )
