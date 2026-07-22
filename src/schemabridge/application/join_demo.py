"""Explicit synthetic relationship proposals for the M08 workflow."""

from schemabridge.application.join_discovery import JoinDiscoveryReport
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.fields import PhysicalFieldRef
from schemabridge.domain.join_reviews import JoinReviewDraft, ReviewedJoinCandidate
from schemabridge.domain.joins import JoinProposal, NormalizedJoinKey
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    IdentityStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TransformationPlan,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
)

JOIN_REVIEW_ID = "north-star-joins"


def build_north_star_join_proposals() -> tuple[JoinProposal, ...]:
    """Return exactly the two declared north-star relationship hypotheses."""

    return (
        JoinProposal(
            id="customer_to_account_holder",
            left_key=NormalizedJoinKey(
                logical_field=LogicalFieldRef("Customer.customer_key"),
                physical_field=PhysicalFieldRef("crm.customers.customer_id"),
                transformation_plan=TransformationPlan(
                    steps=(
                        TrimStep(),
                        ValidateRegexStep(pattern=r"^[0-9]+$"),
                        StripLeadingZerosStep(),
                        RejectInvalidStep(),
                    )
                ),
            ),
            right_key=NormalizedJoinKey(
                logical_field=LogicalFieldRef("AccountHolder.customer_key"),
                physical_field=PhysicalFieldRef("bank.account_holders.gf_customer_id"),
                transformation_plan=TransformationPlan(
                    steps=(
                        ValidateFiniteStep(),
                        ValidateIntegralStep(),
                        CastIntegerToStringStep(),
                        RejectInvalidStep(),
                    )
                ),
            ),
        ),
        JoinProposal(
            id="account_holder_to_account",
            left_key=NormalizedJoinKey(
                logical_field=LogicalFieldRef("AccountHolder.account_key"),
                physical_field=PhysicalFieldRef("bank.account_holders.account_number"),
                transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
            ),
            right_key=NormalizedJoinKey(
                logical_field=LogicalFieldRef("Account.account_key"),
                physical_field=PhysicalFieldRef("bank.accounts.account_number"),
                transformation_plan=TransformationPlan(steps=(IdentityStep(),)),
            ),
        ),
    )


def build_join_review_draft(report: JoinDiscoveryReport) -> JoinReviewDraft:
    """Turn discovery output into local review state without approving anything."""

    return JoinReviewDraft(
        id=JOIN_REVIEW_ID,
        joins=tuple(ReviewedJoinCandidate(candidate=candidate) for candidate in report.candidates),
    )
