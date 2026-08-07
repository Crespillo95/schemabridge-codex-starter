from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from schemabridge.domain import connectors as connector_domain
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    MAX_COST_DECIMAL_PLACES,
    MAX_ESTIMATED_ROOT_ROWS,
    MAX_EXPLAIN_RESPONSE_BYTES,
    MAX_EXPLAIN_TIMEOUT_MS,
    MAX_PLAN_DEPTH,
    MAX_PLAN_NODES,
    MAX_PLAN_WIDTH,
    MAX_QUERY_TOTAL_COST,
    MAX_ROUTE_REVISION,
    POSTGRES_TYPE_CONTRACT_FINGERPRINT,
    POSTGRES_TYPE_CONTRACT_VERSION,
    GovernedExecutionTarget,
    PostgresNativeTypeKind,
    PostgresNativeTypeNormalization,
    QueryCostAssessment,
    QueryCostBudget,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceConnectorKind,
    SourceDialect,
    governed_execution_target_fingerprint,
    normalize_postgres_native_type,
    postgres_type_contract_fingerprint,
    query_cost_assessment_fingerprint,
    query_cost_budget_fingerprint,
)
from schemabridge.domain.semantic_registry import PhysicalValueType

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _budget(**updates: object) -> QueryCostBudget:
    values: dict[str, object] = {
        "explain_timeout_ms": 1_000,
        "max_response_bytes": 64 * 1_024,
        "max_total_cost": Decimal("10000"),
        "max_estimated_rows": 100_000,
        "max_plan_nodes": 1_000,
        "max_plan_depth": 32,
        "max_plan_width": 16_384,
    }
    values.update(updates)
    return QueryCostBudget.model_validate(values)


def _target(**updates: object) -> GovernedExecutionTarget:
    budget = updates.pop("cost_budget", _budget())
    assert isinstance(budget, QueryCostBudget)
    values: dict[str, object] = {
        "workspace_id": "tenant_a",
        "connection_id": CatalogConnectionId("warehouse"),
        "connector_kind": SourceConnectorKind.POSTGRESQL,
        "dialect": SourceDialect.POSTGRESQL,
        "route_revision": 7,
        "route_fingerprint": SHA_A,
        "expected_reader": "schemabridge_reader",
        "source_identity_fingerprint": SHA_D,
        "catalog_identity_fingerprint": SHA_E,
        "type_contract_fingerprint": postgres_type_contract_fingerprint(),
        "cost_budget": budget,
        "cost_budget_fingerprint": budget.fingerprint,
    }
    values.update(updates)
    return GovernedExecutionTarget.model_validate(values)


def _assessment(**updates: object) -> QueryCostAssessment:
    budget = updates.pop("cost_budget", _budget())
    assert isinstance(budget, QueryCostBudget)
    values: dict[str, object] = {
        "decision": QueryCostDecision.ACCEPTED,
        "rejection_codes": (),
        "total_cost": Decimal("9999"),
        "estimated_root_rows": 99_999,
        "plan_width": 16_000,
        "plan_node_count": 999,
        "plan_depth": 31,
        "response_bytes": 60 * 1_024,
        "observed_reader": "schemabridge_reader",
        "read_only": True,
        "explain_timeout_ms": 1_000,
        "target_fingerprint": SHA_C,
        "budget_fingerprint": budget.fingerprint,
        "cost_budget": budget,
    }
    values.update(updates)
    return QueryCostAssessment.model_validate(values)


def test_only_postgresql_has_an_executable_connector_and_dialect() -> None:
    assert tuple(SourceConnectorKind) == (SourceConnectorKind.POSTGRESQL,)
    assert tuple(SourceDialect) == (SourceDialect.POSTGRESQL,)
    assert SourceConnectorKind.POSTGRESQL.value == "postgresql"
    assert SourceDialect.POSTGRESQL.value == "postgresql"

    payload = _target().model_dump(mode="json")
    payload["connector_kind"] = "mysql"
    with pytest.raises(ValidationError, match="Input should be 'postgresql'"):
        GovernedExecutionTarget.model_validate(payload)

    payload = _target().model_dump(mode="json")
    payload["dialect"] = "snowflake"
    with pytest.raises(ValidationError, match="Input should be 'postgresql'"):
        GovernedExecutionTarget.model_validate(payload)


@pytest.mark.parametrize(
    ("native_type", "expected"),
    (
        ("VARCHAR(42)", PhysicalValueType.STRING),
        ("character varying ( 200 )", PhysicalValueType.STRING),
        ("pg_catalog.int8", PhysicalValueType.INTEGER),
        ("smallserial", PhysicalValueType.INTEGER),
        ("NUMERIC(38, 0)", PhysicalValueType.DECIMAL),
        ("money", PhysicalValueType.DECIMAL),
        ("double precision", PhysicalValueType.FLOAT),
        ("float(24)", PhysicalValueType.FLOAT),
        ("BOOL", PhysicalValueType.BOOLEAN),
        ("date", PhysicalValueType.DATE),
        ("TIMESTAMP(6) WITH TIME ZONE", PhysicalValueType.TIMESTAMP),
        ("timestamp without time zone", PhysicalValueType.TIMESTAMP),
        ("timestamptz", PhysicalValueType.TIMESTAMP),
        ("uuid", PhysicalValueType.STRING),
    ),
)
def test_postgres_scalar_aliases_normalize_deterministically(
    native_type: str,
    expected: PhysicalValueType,
) -> None:
    result = normalize_postgres_native_type(native_type)

    assert result.contract_version == POSTGRES_TYPE_CONTRACT_VERSION
    assert result.contract_fingerprint == postgres_type_contract_fingerprint()
    assert result.native_type == native_type
    assert result.kind is PostgresNativeTypeKind.SCALAR
    assert result.normalized_type is expected
    assert result.executable is True


@pytest.mark.parametrize(
    ("native_type", "expected"),
    (
        ("bytea", PhysicalValueType.BINARY),
        ("bit varying(64)", PhysicalValueType.BINARY),
        ("json", PhysicalValueType.STRUCT),
        ("jsonb", PhysicalValueType.STRUCT),
        ("hstore", PhysicalValueType.STRUCT),
    ),
)
def test_postgres_binary_and_struct_types_are_explicit_non_executable_evidence(
    native_type: str,
    expected: PhysicalValueType,
) -> None:
    result = normalize_postgres_native_type(native_type)

    assert result.kind is PostgresNativeTypeKind.SCALAR
    assert result.normalized_type is expected
    assert result.executable is False


@pytest.mark.parametrize(
    "native_type",
    (
        "integer[]",
        "character varying(20)[][]",
        "_int4",
        "ARRAY",
    ),
)
def test_postgres_arrays_are_explicit_and_never_silently_scalarized(
    native_type: str,
) -> None:
    result = normalize_postgres_native_type(native_type)

    assert result.kind is PostgresNativeTypeKind.ARRAY
    assert result.normalized_type is PhysicalValueType.ARRAY
    assert result.executable is False


def test_postgres_domain_requires_an_authoritative_base_type() -> None:
    scalar = normalize_postgres_native_type(
        "customer_identifier",
        domain_base_native_type="varchar(32)",
    )
    structured = normalize_postgres_native_type(
        "event_payload",
        domain_base_native_type="jsonb",
    )
    unavailable = normalize_postgres_native_type("customer_identifier")

    assert scalar.kind is PostgresNativeTypeKind.DOMAIN
    assert scalar.normalized_type is PhysicalValueType.STRING
    assert scalar.executable is True
    assert structured.kind is PostgresNativeTypeKind.DOMAIN
    assert structured.normalized_type is PhysicalValueType.STRUCT
    assert structured.executable is False
    assert unavailable.kind is PostgresNativeTypeKind.UNKNOWN
    assert unavailable.normalized_type is PhysicalValueType.UNKNOWN
    assert unavailable.executable is False


@pytest.mark.parametrize(
    "native_type",
    (
        None,
        "USER-DEFINED",
        "time with time zone",
        "interval",
        "int4range",
        "proprietary_identifier",
    ),
)
def test_unknown_postgres_types_remain_explicit_non_executable_evidence(
    native_type: str | None,
) -> None:
    result = normalize_postgres_native_type(native_type)

    assert result.kind is PostgresNativeTypeKind.UNKNOWN
    assert result.normalized_type is PhysicalValueType.UNKNOWN
    assert result.executable is False


@pytest.mark.parametrize(
    "native_type",
    (
        "",
        " varchar",
        "varchar ",
        "varchar\nDROP TYPE",
        "x" * 201,
        "😀" * 101,
    ),
)
def test_postgres_type_normalization_rejects_unbounded_or_control_text(
    native_type: str,
) -> None:
    with pytest.raises(ValueError, match="bounded inert text"):
        normalize_postgres_native_type(native_type)


def test_postgres_type_contract_is_stable_bounded_and_self_validating() -> None:
    connector_domain._validate_postgres_type_contract_definition()
    fingerprint = postgres_type_contract_fingerprint()
    result = normalize_postgres_native_type("bigint")

    assert len(fingerprint) == 64
    assert set(fingerprint) <= set("0123456789abcdef")
    assert fingerprint == POSTGRES_TYPE_CONTRACT_FINGERPRINT
    assert POSTGRES_TYPE_CONTRACT_VERSION == 1
    assert result.contract_fingerprint == fingerprint
    with pytest.raises(ValidationError, match="type contract identity is unsupported"):
        PostgresNativeTypeNormalization(
            contract_fingerprint=SHA_A,
            native_type="bigint",
            kind=PostgresNativeTypeKind.SCALAR,
            normalized_type=PhysicalValueType.INTEGER,
            executable=True,
        )


def test_postgres_type_contract_lookup_does_not_rehash_each_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_rehashed(_payload: object) -> str:
        raise AssertionError("the prevalidated type contract was rehashed")

    monkeypatch.setattr(connector_domain, "_fingerprint", fail_if_rehashed)

    assert postgres_type_contract_fingerprint() == POSTGRES_TYPE_CONTRACT_FINGERPRINT
    assert normalize_postgres_native_type("varchar").contract_fingerprint == (
        POSTGRES_TYPE_CONTRACT_FINGERPRINT
    )


def test_postgres_type_contract_rejects_tampered_native_evidence() -> None:
    fingerprint = postgres_type_contract_fingerprint()

    with pytest.raises(ValidationError, match="does not match native evidence"):
        PostgresNativeTypeNormalization(
            contract_fingerprint=fingerprint,
            native_type="jsonb",
            kind=PostgresNativeTypeKind.SCALAR,
            normalized_type=PhysicalValueType.STRING,
            executable=True,
        )
    with pytest.raises(ValidationError, match="does not match its base type"):
        PostgresNativeTypeNormalization(
            contract_fingerprint=fingerprint,
            native_type="customer_identifier",
            domain_base_native_type="bigint",
            kind=PostgresNativeTypeKind.DOMAIN,
            normalized_type=PhysicalValueType.STRING,
            executable=True,
        )
    with pytest.raises(ValidationError, match="non-executable"):
        PostgresNativeTypeNormalization(
            contract_fingerprint=fingerprint,
            native_type="private_type",
            kind=PostgresNativeTypeKind.UNKNOWN,
            normalized_type=PhysicalValueType.UNKNOWN,
            executable=True,
        )


def test_cost_budget_is_bounded_immutable_and_canonically_fingerprinted() -> None:
    budget = _budget(max_total_cost=Decimal("10000.000000"))
    equivalent = _budget(max_total_cost="1E+4")

    assert budget.max_total_cost == Decimal("10000")
    assert budget.max_total_cost.as_tuple() == Decimal("10000").as_tuple()
    assert budget.fingerprint == equivalent.fingerprint
    assert budget.fingerprint == query_cost_budget_fingerprint(budget)
    assert len(budget.fingerprint) == 64
    assert set(budget.fingerprint) <= set("0123456789abcdef")

    with pytest.raises(ValidationError, match="Instance is frozen"):
        budget.max_plan_depth = 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryCostBudget.model_validate(
            {
                **budget.model_dump(mode="json"),
                "raw_plan": {"Plan": {"Relation Name": "private_table"}},
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("explain_timeout_ms", 0),
        ("explain_timeout_ms", MAX_EXPLAIN_TIMEOUT_MS + 1),
        ("max_response_bytes", 0),
        ("max_response_bytes", MAX_EXPLAIN_RESPONSE_BYTES + 1),
        ("max_estimated_rows", -1),
        ("max_estimated_rows", MAX_ESTIMATED_ROOT_ROWS + 1),
        ("max_plan_nodes", 0),
        ("max_plan_nodes", MAX_PLAN_NODES + 1),
        ("max_plan_depth", 0),
        ("max_plan_depth", MAX_PLAN_DEPTH + 1),
        ("max_plan_width", -1),
        ("max_plan_width", MAX_PLAN_WIDTH + 1),
    ),
)
def test_cost_budget_rejects_count_and_size_values_outside_bounds(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValidationError):
        _budget(**{field: value})


@pytest.mark.parametrize("value", ("1", 1.0, True))
def test_cost_budget_rejects_coerced_integer_bounds(value: object) -> None:
    with pytest.raises(ValidationError):
        _budget(explain_timeout_ms=value)


@pytest.mark.parametrize(
    "value",
    (
        Decimal("-0.000001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        MAX_QUERY_TOTAL_COST + 1,
        Decimal(1).scaleb(-(MAX_COST_DECIMAL_PLACES + 1)),
        True,
        " 1",
        "",
    ),
)
def test_cost_budget_rejects_unsafe_or_noncanonical_decimal_values(value: object) -> None:
    with pytest.raises(ValidationError):
        _budget(max_total_cost=value)


def test_cost_budget_accepts_closed_boundary_values() -> None:
    budget = _budget(
        explain_timeout_ms=MAX_EXPLAIN_TIMEOUT_MS,
        max_response_bytes=MAX_EXPLAIN_RESPONSE_BYTES,
        max_total_cost=MAX_QUERY_TOTAL_COST,
        max_estimated_rows=MAX_ESTIMATED_ROOT_ROWS,
        max_plan_nodes=MAX_PLAN_NODES,
        max_plan_depth=MAX_PLAN_DEPTH,
        max_plan_width=MAX_PLAN_WIDTH,
    )

    assert budget.max_total_cost == MAX_QUERY_TOTAL_COST
    assert budget.max_plan_depth == MAX_PLAN_DEPTH


def test_budget_fingerprint_changes_for_every_governed_bound() -> None:
    baseline = _budget()
    variants = (
        _budget(explain_timeout_ms=1_001),
        _budget(max_response_bytes=65 * 1_024),
        _budget(max_total_cost=Decimal("10000.1")),
        _budget(max_estimated_rows=100_001),
        _budget(max_plan_nodes=1_001),
        _budget(max_plan_depth=33),
        _budget(max_plan_width=16_385),
    )

    assert len({baseline.fingerprint, *(item.fingerprint for item in variants)}) == 8


def test_execution_target_is_public_immutable_and_contains_no_secret_surface() -> None:
    target = _target()

    assert set(GovernedExecutionTarget.model_fields) == {
        "version",
        "workspace_id",
        "connection_id",
        "connector_kind",
        "dialect",
        "route_revision",
        "route_fingerprint",
        "expected_reader",
        "source_identity_fingerprint",
        "catalog_identity_fingerprint",
        "type_contract_fingerprint",
        "cost_budget",
        "cost_budget_fingerprint",
    }
    serialized = json.dumps(target.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "dsn",
        "database_url",
        "endpoint",
        "password",
        "token",
        "secret",
        "binding_ref",
        "credential",
    ):
        assert forbidden not in serialized.casefold()
        assert forbidden not in repr(target).casefold()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        target.route_revision = 8
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GovernedExecutionTarget.model_validate(
            {
                **target.model_dump(mode="json"),
                "dsn": "postgresql://reader:password@source/private",
            }
        )


def test_execution_target_requires_exact_budget_fingerprint() -> None:
    with pytest.raises(ValidationError, match="cost budget fingerprint does not match"):
        _target(cost_budget_fingerprint=SHA_C)


def test_execution_target_rejects_well_formed_but_unsupported_type_contract() -> None:
    with pytest.raises(ValidationError, match="type contract identity is unsupported"):
        _target(type_contract_fingerprint=SHA_B)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_id", "Tenant A"),
        ("workspace_id", "ab"),
        ("route_revision", 0),
        ("route_revision", MAX_ROUTE_REVISION + 1),
        ("route_fingerprint", "A" * 64),
        ("route_fingerprint", "a" * 63),
        ("expected_reader", "postgresql://reader@source/database"),
        ("expected_reader", "Reader"),
        ("source_identity_fingerprint", "D" * 64),
        ("catalog_identity_fingerprint", "e" * 63),
        ("type_contract_fingerprint", "not-a-fingerprint"),
        ("cost_budget_fingerprint", "b" * 63),
    ),
)
def test_execution_target_rejects_unbounded_or_noncanonical_public_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        _target(**{field: value})


def test_execution_target_fingerprint_is_deterministic_and_binds_every_public_fact() -> None:
    baseline = _target()
    alternate_budget = _budget(max_total_cost=Decimal("10001"))
    variants = (
        _target(workspace_id="tenant_b"),
        _target(connection_id=CatalogConnectionId("warehouse_b")),
        _target(route_revision=8),
        _target(route_fingerprint=SHA_C),
        _target(expected_reader="tenant_reader"),
        _target(source_identity_fingerprint=SHA_C),
        _target(catalog_identity_fingerprint=SHA_C),
        _target(
            cost_budget=alternate_budget,
            cost_budget_fingerprint=alternate_budget.fingerprint,
        ),
    )

    assert baseline.fingerprint == governed_execution_target_fingerprint(baseline)
    assert len({baseline.fingerprint, *(item.fingerprint for item in variants)}) == 9
    assert _target().fingerprint == baseline.fingerprint


def test_target_accepts_closed_revision_and_reader_boundaries() -> None:
    target = _target(
        route_revision=MAX_ROUTE_REVISION,
        expected_reader="_" + ("a" * 62),
    )

    assert target.route_revision == MAX_ROUTE_REVISION
    assert len(target.expected_reader) == 63


def test_query_cost_decision_and_rejection_codes_are_closed() -> None:
    assert tuple(QueryCostDecision) == (
        QueryCostDecision.ACCEPTED,
        QueryCostDecision.REJECTED,
    )
    assert tuple(QueryCostRejectionCode) == (
        QueryCostRejectionCode.TOTAL_COST_EXCEEDED,
        QueryCostRejectionCode.ESTIMATED_ROWS_EXCEEDED,
        QueryCostRejectionCode.PLAN_WIDTH_EXCEEDED,
        QueryCostRejectionCode.PLAN_NODES_EXCEEDED,
        QueryCostRejectionCode.PLAN_DEPTH_EXCEEDED,
        QueryCostRejectionCode.RESPONSE_BYTES_EXCEEDED,
        QueryCostRejectionCode.TIMEOUT,
        QueryCostRejectionCode.UNAVAILABLE,
        QueryCostRejectionCode.INVALID,
        QueryCostRejectionCode.TARGET_FINGERPRINT_MISMATCH,
        QueryCostRejectionCode.BUDGET_FINGERPRINT_MISMATCH,
    )


def test_accepted_cost_assessment_is_complete_immutable_and_canonical() -> None:
    assessment = _assessment(total_cost=Decimal("9999.000000"))
    equivalent = _assessment(total_cost="9.999E+3")

    assert assessment.total_cost == Decimal("9999")
    assert assessment.rejection_codes == ()
    assert assessment.fingerprint == equivalent.fingerprint
    assert assessment.fingerprint == query_cost_assessment_fingerprint(assessment)
    assert len(assessment.fingerprint) == 64
    assert set(assessment.fingerprint) <= set("0123456789abcdef")
    with pytest.raises(ValidationError, match="Instance is frozen"):
        assessment.decision = QueryCostDecision.REJECTED


@pytest.mark.parametrize(
    "field",
    (
        "total_cost",
        "estimated_root_rows",
        "plan_width",
        "plan_node_count",
        "plan_depth",
        "response_bytes",
        "observed_reader",
        "read_only",
    ),
)
def test_accepted_cost_assessment_requires_every_observed_fact(field: str) -> None:
    with pytest.raises(ValidationError, match="requires complete observed facts"):
        _assessment(**{field: None})


def test_accepted_cost_assessment_requires_read_only_and_no_rejection_codes() -> None:
    with pytest.raises(ValidationError, match="requires a read-only transaction"):
        _assessment(read_only=False)
    with pytest.raises(ValidationError, match="cannot contain rejection codes"):
        _assessment(rejection_codes=(QueryCostRejectionCode.INVALID,))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("total_cost", Decimal("10000.000001")),
        ("estimated_root_rows", 100_001),
        ("plan_width", 16_385),
        ("plan_node_count", 1_001),
        ("plan_depth", 33),
        ("response_bytes", (64 * 1_024) + 1),
        ("explain_timeout_ms", 1_001),
    ),
)
def test_accepted_cost_assessment_rejects_every_metric_above_budget(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match="exceeds its governed budget"):
        _assessment(**{field: value})


def test_accepted_cost_assessment_accepts_exact_budget_boundaries() -> None:
    budget = _budget()
    assessment = _assessment(
        cost_budget=budget,
        total_cost=budget.max_total_cost,
        estimated_root_rows=budget.max_estimated_rows,
        plan_width=budget.max_plan_width,
        plan_node_count=budget.max_plan_nodes,
        plan_depth=budget.max_plan_depth,
        response_bytes=budget.max_response_bytes,
        explain_timeout_ms=budget.explain_timeout_ms,
    )

    assert assessment.decision is QueryCostDecision.ACCEPTED


def test_rejected_cost_assessment_requires_a_reason_and_allows_no_plan_metrics() -> None:
    with pytest.raises(ValidationError, match="requires a rejection code"):
        _assessment(
            decision=QueryCostDecision.REJECTED,
            rejection_codes=(),
            total_cost=None,
            estimated_root_rows=None,
            plan_width=None,
            plan_node_count=None,
            plan_depth=None,
            response_bytes=None,
            observed_reader=None,
            read_only=None,
        )

    for code in (
        QueryCostRejectionCode.TIMEOUT,
        QueryCostRejectionCode.UNAVAILABLE,
        QueryCostRejectionCode.INVALID,
        QueryCostRejectionCode.TARGET_FINGERPRINT_MISMATCH,
        QueryCostRejectionCode.BUDGET_FINGERPRINT_MISMATCH,
    ):
        assessment = _assessment(
            decision=QueryCostDecision.REJECTED,
            rejection_codes=(code,),
            total_cost=None,
            estimated_root_rows=None,
            plan_width=None,
            plan_node_count=None,
            plan_depth=None,
            response_bytes=None,
            observed_reader=None,
            read_only=None,
        )
        assert assessment.decision is QueryCostDecision.REJECTED
        assert assessment.rejection_codes == (code,)


def test_rejected_cost_assessment_can_retain_sanitized_over_budget_metrics() -> None:
    assessment = _assessment(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=(QueryCostRejectionCode.TOTAL_COST_EXCEEDED,),
        total_cost=Decimal("10000.000001"),
    )

    assert assessment.total_cost == Decimal("10000.000001")
    assert assessment.rejection_codes == (QueryCostRejectionCode.TOTAL_COST_EXCEEDED,)


def test_cost_rejection_codes_must_be_unique_and_sorted() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        _assessment(
            decision=QueryCostDecision.REJECTED,
            rejection_codes=(
                QueryCostRejectionCode.TIMEOUT,
                QueryCostRejectionCode.INVALID,
            ),
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        _assessment(
            decision=QueryCostDecision.REJECTED,
            rejection_codes=(
                QueryCostRejectionCode.INVALID,
                QueryCostRejectionCode.INVALID,
            ),
        )

    assessment = _assessment(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=tuple(sorted(QueryCostRejectionCode, key=lambda item: item.value)),
    )
    assert len(assessment.rejection_codes) == len(QueryCostRejectionCode)


def test_cost_assessment_requires_the_exact_budget_fingerprint() -> None:
    with pytest.raises(ValidationError, match="budget fingerprint does not match"):
        _assessment(budget_fingerprint=SHA_A)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("total_cost", Decimal("-0.000001")),
        ("total_cost", Decimal("NaN")),
        ("estimated_root_rows", -1),
        ("estimated_root_rows", "1"),
        ("plan_width", -1),
        ("plan_node_count", 0),
        ("plan_depth", 0),
        ("response_bytes", 0),
        ("read_only", "true"),
        ("explain_timeout_ms", 0),
        ("observed_reader", "postgresql://reader@source/database"),
        ("target_fingerprint", "C" * 64),
        ("budget_fingerprint", "c" * 63),
    ),
)
def test_cost_assessment_rejects_unbounded_or_noncanonical_facts(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        _assessment(**{field: value})


@pytest.mark.parametrize(
    "forbidden_field",
    ("raw_plan", "sql", "parameters", "relation_names", "index_names", "topology"),
)
def test_cost_assessment_has_no_raw_plan_sql_parameter_or_topology_surface(
    forbidden_field: str,
) -> None:
    assessment = _assessment()
    serialized = assessment.model_dump(mode="json")

    assert "cost_budget" not in serialized
    assert "QueryCostBudget" not in repr(assessment)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryCostAssessment.model_validate(
            {
                **assessment.model_dump(mode="json"),
                "cost_budget": assessment.cost_budget,
                forbidden_field: "private source detail",
            }
        )


def test_cost_assessment_public_surface_is_sanitized_and_explicit() -> None:
    assessment = _assessment()

    assert set(QueryCostAssessment.model_fields) == {
        "version",
        "decision",
        "rejection_codes",
        "total_cost",
        "estimated_root_rows",
        "plan_width",
        "plan_node_count",
        "plan_depth",
        "response_bytes",
        "observed_reader",
        "read_only",
        "explain_timeout_ms",
        "target_fingerprint",
        "budget_fingerprint",
        "cost_budget",
    }
    serialized = json.dumps(assessment.model_dump(mode="json"), sort_keys=True).casefold()
    for forbidden in (
        "raw_plan",
        "relation",
        "index_name",
        "sql",
        "parameter",
        "topology",
        "dsn",
        "endpoint",
        "password",
        "secret",
        "credential",
    ):
        assert forbidden not in serialized
        assert forbidden not in repr(assessment).casefold()


def test_cost_assessment_fingerprint_binds_every_sanitized_fact() -> None:
    baseline = _assessment()
    alternate_budget = _budget(max_total_cost=Decimal("10001"))
    variants = (
        _assessment(total_cost=Decimal("9999.1")),
        _assessment(estimated_root_rows=99_998),
        _assessment(plan_width=15_999),
        _assessment(plan_node_count=998),
        _assessment(plan_depth=30),
        _assessment(response_bytes=(60 * 1_024) - 1),
        _assessment(observed_reader="tenant_reader"),
        _assessment(explain_timeout_ms=999),
        _assessment(target_fingerprint=SHA_A),
        _assessment(
            cost_budget=alternate_budget,
            budget_fingerprint=alternate_budget.fingerprint,
        ),
        _assessment(
            decision=QueryCostDecision.REJECTED,
            rejection_codes=(QueryCostRejectionCode.INVALID,),
        ),
    )

    assert len({baseline.fingerprint, *(item.fingerprint for item in variants)}) == 12
