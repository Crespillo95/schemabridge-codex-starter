"""Boundary tests for the pure identifier transformation interpreter."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from schemabridge.domain.transformations import (
    EmptyStringPolicy,
    IdentifierNormalizationPlan,
    LeadingZeroPolicy,
    NormalizationAccepted,
    NormalizationRejected,
    NormalizationRejectionCode,
    NullPolicy,
    TransformationPlan,
    normalize_identifier,
)


def strip_plan(**overrides: object) -> IdentifierNormalizationPlan:
    values: dict[str, object] = {
        "null_policy": NullPolicy.REJECT,
        "leading_zero_policy": LeadingZeroPolicy.STRIP,
    }
    values.update(overrides)
    return IdentifierNormalizationPlan.model_validate(values)


@pytest.mark.parametrize(
    ("source", "expected", "source_kind"),
    [
        ("00000000123", "123", "string"),
        (123, "123", "integer"),
        (123.0, "123", "float"),
        (Decimal("123.0"), "123", "decimal"),
        ("0000", "0", "string"),
        ("\u2003000123\u2003", "123", "string"),
        (10**100, "1" + ("0" * 100), "integer"),
        (float(2**53 - 1), str(2**53 - 1), "float"),
    ],
)
def test_strip_policy_accepts_exact_integral_identifiers(
    source: object,
    expected: str,
    source_kind: str,
) -> None:
    outcome = normalize_identifier(source, strip_plan())

    assert isinstance(outcome, NormalizationAccepted)
    assert outcome.canonical_value == expected
    assert outcome.source_kind.value == source_kind


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (123.5, NormalizationRejectionCode.NON_INTEGRAL_IDENTIFIER),
        (Decimal("123.5"), NormalizationRejectionCode.NON_INTEGRAL_IDENTIFIER),
        (float("nan"), NormalizationRejectionCode.NON_FINITE_IDENTIFIER),
        (float("inf"), NormalizationRejectionCode.NON_FINITE_IDENTIFIER),
        (float("-inf"), NormalizationRejectionCode.NON_FINITE_IDENTIFIER),
        (True, NormalizationRejectionCode.BOOLEAN_IDENTIFIER),
        (False, NormalizationRejectionCode.BOOLEAN_IDENTIFIER),
        ("12x3", NormalizationRejectionCode.MALFORMED_IDENTIFIER),
        ("\uff11\uff12\uff13", NormalizationRejectionCode.MALFORMED_IDENTIFIER),
        ("", NormalizationRejectionCode.EMPTY_IDENTIFIER),
        ("   ", NormalizationRejectionCode.EMPTY_IDENTIFIER),
        (-1, NormalizationRejectionCode.NEGATIVE_IDENTIFIER),
        ("-1", NormalizationRejectionCode.NEGATIVE_IDENTIFIER),
        (float(2**53), NormalizationRejectionCode.UNSAFE_FLOAT_IDENTIFIER),
        ([123], NormalizationRejectionCode.UNSUPPORTED_IDENTIFIER_TYPE),
    ],
)
def test_invalid_identifiers_are_rejected_without_repair(
    source: object,
    code: NormalizationRejectionCode,
) -> None:
    outcome = normalize_identifier(source, strip_plan())

    assert isinstance(outcome, NormalizationRejected)
    assert outcome.code is code
    assert outcome.reason


def test_null_is_preserved_or_rejected_according_to_plan() -> None:
    preserved = normalize_identifier(
        None,
        strip_plan(null_policy=NullPolicy.PRESERVE),
    )
    rejected = normalize_identifier(None, strip_plan())

    assert isinstance(preserved, NormalizationAccepted)
    assert preserved.canonical_value is None
    assert isinstance(rejected, NormalizationRejected)
    assert rejected.code is NormalizationRejectionCode.NULL_NOT_ALLOWED


def test_empty_string_to_null_is_explicit_and_uses_null_policy() -> None:
    outcome = normalize_identifier(
        "\u2003",
        strip_plan(
            null_policy=NullPolicy.PRESERVE,
            empty_string_policy=EmptyStringPolicy.AS_NULL,
        ),
    )

    assert isinstance(outcome, NormalizationAccepted)
    assert outcome.canonical_value is None
    assert outcome.source_kind.value == "string"


def test_preserve_policy_keeps_source_string_zeros() -> None:
    plan = IdentifierNormalizationPlan(
        null_policy=NullPolicy.REJECT,
        leading_zero_policy=LeadingZeroPolicy.PRESERVE,
    )

    outcome = normalize_identifier("000123", plan)

    assert isinstance(outcome, NormalizationAccepted)
    assert outcome.canonical_value == "000123"


@pytest.mark.parametrize(
    ("source", "expected"),
    [("123", "00000123"), ("000123", "00000123"), ("0000", "00000000")],
)
def test_pad_policy_produces_exact_canonical_width(source: str, expected: str) -> None:
    plan = IdentifierNormalizationPlan(
        null_policy=NullPolicy.REJECT,
        leading_zero_policy=LeadingZeroPolicy.PAD_TO_LENGTH,
        pad_to_length=8,
    )

    outcome = normalize_identifier(source, plan)

    assert isinstance(outcome, NormalizationAccepted)
    assert outcome.canonical_value == expected


def test_pad_policy_rejects_overflow() -> None:
    plan = IdentifierNormalizationPlan(
        null_policy=NullPolicy.REJECT,
        leading_zero_policy=LeadingZeroPolicy.PAD_TO_LENGTH,
        pad_to_length=3,
    )

    outcome = normalize_identifier("1234", plan)

    assert isinstance(outcome, NormalizationRejected)
    assert outcome.code is NormalizationRejectionCode.IDENTIFIER_TOO_LONG


@pytest.mark.parametrize(
    "values",
    [
        {
            "null_policy": "reject",
            "leading_zero_policy": "pad_to_length",
        },
        {
            "null_policy": "reject",
            "leading_zero_policy": "strip",
            "pad_to_length": 8,
        },
    ],
)
def test_padding_configuration_cannot_be_implicit(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IdentifierNormalizationPlan.model_validate(values)


def test_transformation_algebra_is_closed_and_unambiguous() -> None:
    valid = TransformationPlan.model_validate(
        {
            "version": 1,
            "steps": [
                {"operation": "trim"},
                {"operation": "validate_regex", "pattern": "^[0-9]+$"},
                {"operation": "strip_leading_zeros"},
                {"operation": "reject_invalid"},
            ],
        }
    )

    assert [step.operation for step in valid.steps] == [
        "trim",
        "validate_regex",
        "strip_leading_zeros",
        "reject_invalid",
    ]
    with pytest.raises(ValidationError):
        TransformationPlan.model_validate(
            {"version": 1, "steps": [{"operation": "python_callback", "code": "pass"}]}
        )
    with pytest.raises(ValidationError):
        TransformationPlan.model_validate(
            {
                "version": 1,
                "steps": [
                    {"operation": "strip_leading_zeros"},
                    {"operation": "pad_left", "length": 8},
                ],
            }
        )


@pytest.mark.parametrize(
    "step",
    [
        {"operation": "identity"},
        {"operation": "trim"},
        {"operation": "empty_to_null"},
        {"operation": "validate_regex", "pattern": "^[0-9]+$"},
        {"operation": "validate_finite"},
        {"operation": "validate_integral"},
        {"operation": "strip_leading_zeros"},
        {"operation": "pad_left", "length": 8},
        {"operation": "cast_integer_to_string"},
        {"operation": "cast_timestamp_to_date"},
        {"operation": "parse_date", "format": "%Y-%m-%d"},
        {"operation": "normalize_decimal_scale", "scale": 2},
        {
            "operation": "map_values",
            "entries": [{"source": "2", "target": "SECONDARY"}],
        },
        {"operation": "preserve_null"},
        {"operation": "reject_invalid"},
    ],
)
def test_every_named_transformation_step_is_serializable(step: dict[str, object]) -> None:
    plan = TransformationPlan.model_validate({"steps": [step]})

    dumped_step = plan.model_dump(mode="json")["steps"][0]
    assert dumped_step["operation"] == step["operation"]
    assert TransformationPlan.model_validate(plan.model_dump(mode="json")) == plan


def test_normalization_plan_is_immutable() -> None:
    plan = strip_plan()

    with pytest.raises(ValidationError):
        plan.null_policy = NullPolicy.PRESERVE
