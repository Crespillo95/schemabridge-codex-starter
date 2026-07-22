"""Deterministic synthetic Customer review fixture for the M07 operator workflow."""

from schemabridge.application.candidate_demo import build_customer_key_concept
from schemabridge.domain.concepts import (
    CanonicalField,
    CanonicalType,
    LogicalFieldRef,
    LogicalModel,
    LogicalModelRef,
)
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.mappings import ColumnMapping, ConfidenceScore
from schemabridge.domain.reviews import CanonicalReviewDraft, ReviewedMapping
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    IdentityStep,
    PreserveNullStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TransformationPlan,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
)

CUSTOMER_REVIEW_ID = "customer-canonical"


def build_customer_review_draft() -> CanonicalReviewDraft:
    """Return the four explicit north-star candidates requiring human review."""

    customer_key = build_customer_key_concept()
    registration_date = CanonicalField(
        id=LogicalFieldRef("Customer.registration_date"),
        canonical_name="registration_date",
        canonical_type=CanonicalType.DATE,
        definition="Calendar date on which the customer registration became effective.",
    )
    fields = (customer_key, registration_date)
    mappings = (
        _mapping(
            "Customer.customer_key",
            "crm.customers.customer_id",
            0.781,
            (
                "normalized name and description identify the CRM customer key",
                "normalized overlap matches the bounded synthetic evidence",
            ),
            (),
            TransformationPlan(
                steps=(
                    PreserveNullStep(),
                    TrimStep(),
                    ValidateRegexStep(pattern=r"^[0-9]+$"),
                    StripLeadingZerosStep(),
                    RejectInvalidStep(),
                )
            ),
        ),
        _mapping(
            "Customer.customer_key",
            "legacy.client_master.client_no",
            0.768,
            (
                "description identifies the legacy customer number",
                "normalized overlap matches the bounded synthetic evidence",
            ),
            (),
            TransformationPlan(
                steps=(
                    PreserveNullStep(),
                    CastIntegerToStringStep(),
                    StripLeadingZerosStep(),
                    RejectInvalidStep(),
                )
            ),
        ),
        _mapping(
            "Customer.customer_key",
            "bank.account_holders.gf_customer_id",
            0.638,
            (
                "description identifies the bank global customer identifier",
                "integral finite values overlap after deterministic normalization",
            ),
            (
                "floating-point storage can lose identifier precision",
                "non-finite and fractional values require explicit rejection",
            ),
            TransformationPlan(
                steps=(
                    PreserveNullStep(),
                    ValidateFiniteStep(),
                    ValidateIntegralStep(),
                    CastIntegerToStringStep(),
                    StripLeadingZerosStep(),
                    RejectInvalidStep(),
                )
            ),
        ),
        _mapping(
            "Customer.registration_date",
            "crm.customers.registration_date",
            1.0,
            ("the typed CRM registration date is the approved north-star source",),
            (),
            TransformationPlan(steps=(IdentityStep(),)),
        ),
    )
    return CanonicalReviewDraft(
        id=CUSTOMER_REVIEW_ID,
        logical_model=LogicalModel(
            id=LogicalModelRef("Customer"),
            name="Customer",
            description="Governed canonical Customer context for the synthetic demo sources.",
            fields=tuple(field.id for field in fields),
            status=ApprovalStatus.NEEDS_REVIEW,
            version=1,
        ),
        fields=fields,
        mappings=mappings,
    )


def _mapping(
    logical_field: str,
    physical_field: str,
    confidence: float,
    evidence: tuple[str, ...],
    risks: tuple[str, ...],
    transformation_plan: TransformationPlan,
) -> ReviewedMapping:
    return ReviewedMapping(
        mapping=ColumnMapping(
            logical_field=LogicalFieldRef(logical_field),
            physical_field=PhysicalFieldRef(physical_field),
            confidence=ConfidenceScore(confidence),
            status=ApprovalStatus.NEEDS_REVIEW,
            evidence=evidence,
            risks=risks,
            transformation_plan=transformation_plan,
        )
    )
