"""Explicit approved fixtures for the M03 north-star and guard demonstrations."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyFinding,
    SqlPolicyGuardPort,
    SqlPolicyViolation,
)
from schemabridge.domain.concepts import LogicalFieldRef
from schemabridge.domain.decisions import ApprovalStatus
from schemabridge.domain.fields import PhysicalDatasetRef, PhysicalFieldRef
from schemabridge.domain.joins import (
    Cardinality,
    FanoutPolicy,
    JoinContract,
    JoinType,
    NormalizedJoinKey,
)
from schemabridge.domain.plans import (
    AggregateExpression,
    AllowedAsset,
    ApprovedJoin,
    ColumnExpression,
    DatasetScan,
    DateGrainExpression,
    FilterPredicate,
    JoinPredicate,
    MappedExpression,
    OrderItem,
    OutputAlias,
    ParameterValue,
    QueryPlan,
    QueryPolicy,
    RelationAlias,
    SelectItem,
)
from schemabridge.domain.requests import (
    DateGrain,
    FilterOperator,
    MetricOperation,
    SortDirection,
)
from schemabridge.domain.transformations import (
    CastIntegerToStringStep,
    MapValuesStep,
    RejectInvalidStep,
    StripLeadingZerosStep,
    TransformationPlan,
    TrimStep,
    ValidateFiniteStep,
    ValidateIntegralStep,
    ValidateRegexStep,
    ValueMapEntry,
)


def _column(relation: str, field: str) -> ColumnExpression:
    return ColumnExpression(
        relation=RelationAlias(relation),
        field=PhysicalFieldRef(field),
    )


def build_north_star_query_plan() -> QueryPlan:
    """Return the explicit approved plan; no discovery or inference occurs here."""

    customers = DatasetScan(
        dataset=PhysicalDatasetRef("crm.customers"),
        alias=RelationAlias("c"),
    )
    holders = DatasetScan(
        dataset=PhysicalDatasetRef("bank.account_holders"),
        alias=RelationAlias("h"),
    )

    customer_key = MappedExpression(
        source=_column("c", "crm.customers.customer_id"),
        transformation_plan=TransformationPlan(
            steps=(
                TrimStep(),
                ValidateRegexStep(pattern=r"^[0-9]+$"),
                StripLeadingZerosStep(),
                RejectInvalidStep(),
            )
        ),
    )
    holder_key = MappedExpression(
        source=_column("h", "bank.account_holders.gf_customer_id"),
        transformation_plan=TransformationPlan(
            steps=(
                ValidateFiniteStep(),
                ValidateIntegralStep(),
                CastIntegerToStringStep(),
                RejectInvalidStep(),
            )
        ),
    )
    normalized_role = MappedExpression(
        source=_column("h", "bank.account_holders.holder_type"),
        transformation_plan=TransformationPlan(
            steps=(
                MapValuesStep(
                    entries=(
                        ValueMapEntry(source="SECONDARY", target="SECONDARY"),
                        ValueMapEntry(source="2", target="SECONDARY"),
                        ValueMapEntry(source="CO_HOLDER", target="SECONDARY"),
                        ValueMapEntry(source="PRIMARY", target="PRIMARY"),
                    )
                ),
                RejectInvalidStep(),
            )
        ),
    )
    registration_day = DateGrainExpression(
        source=_column("c", "crm.customers.registration_date"),
        grain=DateGrain.DAY,
    )
    contract = JoinContract(
        id="customer_to_account_holder",
        left_key=NormalizedJoinKey(
            logical_field=LogicalFieldRef("Customer.customer_key"),
            physical_field=customer_key.source.field,
            transformation_plan=customer_key.transformation_plan,
        ),
        right_key=NormalizedJoinKey(
            logical_field=LogicalFieldRef("AccountHolder.customer_key"),
            physical_field=holder_key.source.field,
            transformation_plan=holder_key.transformation_plan,
        ),
        cardinality=Cardinality.ONE_TO_MANY,
        default_join_type=JoinType.INNER,
        fanout_policy=FanoutPolicy.REQUIRE_DISTINCT_FOR_LEFT_ENTITY_METRICS,
        status=ApprovalStatus.APPROVED,
        version=1,
        evidence=("normalized_value_overlap", "matching_business_definition"),
        risks=("duplicate_holder_relationships_can_multiply_customer_rows",),
        approval_decision_id="m03_explicit_fixture_approval",
    )

    return QueryPlan(
        root_scan=customers,
        joins=(
            ApprovedJoin(
                contract=contract,
                right_scan=holders,
                on=JoinPredicate(left=customer_key, right=holder_key),
            ),
        ),
        projections=(
            SelectItem(
                expression=registration_day,
                alias=OutputAlias("registration_date"),
            ),
            SelectItem(
                expression=AggregateExpression(
                    operation=MetricOperation.COUNT_DISTINCT,
                    source=customer_key,
                ),
                alias=OutputAlias("secondary_holder_customers"),
            ),
        ),
        filters=(
            FilterPredicate(
                expression=normalized_role,
                operator=FilterOperator.EQUALS,
                values=(ParameterValue(value="SECONDARY"),),
            ),
        ),
        group_by=(registration_day,),
        order_by=(OrderItem(expression=registration_day, direction=SortDirection.ASC),),
        limit=None,
    )


def build_demo_query_policy(
    *,
    max_preview_rows: int = 500,
    statement_timeout_ms: int = 5_000,
) -> QueryPolicy:
    """Return the synthetic database allowlist used by M03 demos and tests."""

    approved_join_contracts = tuple(join.contract for join in build_north_star_query_plan().joins)
    return QueryPolicy(
        assets=(
            AllowedAsset(
                dataset=PhysicalDatasetRef("crm.customers"),
                columns=(
                    "customer_id",
                    "registration_date",
                    "country_cd",
                    "customer_status",
                ),
            ),
            AllowedAsset(
                dataset=PhysicalDatasetRef("legacy.client_master"),
                columns=("client_no", "created_dt", "country", "status_code"),
            ),
            AllowedAsset(
                dataset=PhysicalDatasetRef("bank.accounts"),
                columns=(
                    "account_number",
                    "opening_date",
                    "account_status",
                    "current_balance",
                ),
            ),
            AllowedAsset(
                dataset=PhysicalDatasetRef("bank.account_holders"),
                columns=(
                    "holder_link_id",
                    "account_number",
                    "gf_customer_id",
                    "holder_type",
                    "relationship_start_date",
                    "relationship_end_date",
                ),
            ),
            AllowedAsset(
                dataset=PhysicalDatasetRef("reporting.customer_accounts"),
                columns=(
                    "report_date",
                    "customer_key_text",
                    "account_number",
                    "holder_role_normalized",
                ),
            ),
        ),
        approved_join_contracts=approved_join_contracts,
        max_tables=3,
        max_preview_rows=max_preview_rows,
        statement_timeout_ms=statement_timeout_ms,
    )


@dataclass(frozen=True, slots=True)
class GuardDemoCase:
    id: str
    sql: str


@dataclass(frozen=True, slots=True)
class GuardDemoResult:
    id: str
    findings: tuple[SqlPolicyFinding, ...]


class GuardDemoInvariantError(RuntimeError):
    """A malicious demonstration case unexpectedly passed the guard."""


_GUARD_DEMO_CASES = (
    GuardDemoCase(
        id="statement_smuggling",
        sql="SELECT c.customer_id FROM crm.customers AS c LIMIT 1; DROP TABLE crm.customers",
    ),
    GuardDemoCase(
        id="destructive_cte",
        sql=(
            "WITH deleted AS (DELETE FROM crm.customers RETURNING customer_id) "
            "SELECT customer_id FROM deleted LIMIT 1"
        ),
    ),
    GuardDemoCase(
        id="cartesian_join",
        sql=(
            "SELECT c.customer_id FROM crm.customers AS c "
            "JOIN bank.account_holders AS h ON TRUE LIMIT 10"
        ),
    ),
)


def run_guard_demo(
    guard: SqlPolicyGuardPort,
    policy: QueryPolicy,
) -> tuple[GuardDemoResult, ...]:
    """Run three fixed malicious examples without executing any SQL."""

    results: list[GuardDemoResult] = []
    for case in _GUARD_DEMO_CASES:
        try:
            guard.validate(
                CompiledQuery(sql=case.sql, parameters=(), effective_limit=1),
                policy,
            )
        except SqlPolicyViolation as error:
            results.append(GuardDemoResult(id=case.id, findings=error.findings))
        else:
            raise GuardDemoInvariantError(f"guard demo case unexpectedly passed: {case.id}")
    return tuple(results)
