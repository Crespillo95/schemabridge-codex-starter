"""Closed transformation algebra and deterministic identifier normalization."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from schemabridge.domain._base import FrozenDomainModel


class IdentityStep(FrozenDomainModel):
    operation: Literal["identity"] = "identity"


class TrimStep(FrozenDomainModel):
    operation: Literal["trim"] = "trim"


class EmptyToNullStep(FrozenDomainModel):
    operation: Literal["empty_to_null"] = "empty_to_null"


class ValidateRegexStep(FrozenDomainModel):
    operation: Literal["validate_regex"] = "validate_regex"
    pattern: str = Field(min_length=1, max_length=256)

    @field_validator("pattern")
    @classmethod
    def pattern_must_compile(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError("pattern must be a valid regular expression") from error
        return value


class ValidateFiniteStep(FrozenDomainModel):
    operation: Literal["validate_finite"] = "validate_finite"


class ValidateIntegralStep(FrozenDomainModel):
    operation: Literal["validate_integral"] = "validate_integral"


class StripLeadingZerosStep(FrozenDomainModel):
    operation: Literal["strip_leading_zeros"] = "strip_leading_zeros"


class PadLeftStep(FrozenDomainModel):
    operation: Literal["pad_left"] = "pad_left"
    length: int = Field(ge=1)
    fill_character: Literal["0"] = "0"


class CastIntegerToStringStep(FrozenDomainModel):
    operation: Literal["cast_integer_to_string"] = "cast_integer_to_string"


class CastTimestampToDateStep(FrozenDomainModel):
    operation: Literal["cast_timestamp_to_date"] = "cast_timestamp_to_date"


class ParseDateStep(FrozenDomainModel):
    operation: Literal["parse_date"] = "parse_date"
    format: str = Field(min_length=1, max_length=64)


class NormalizeDecimalScaleStep(FrozenDomainModel):
    operation: Literal["normalize_decimal_scale"] = "normalize_decimal_scale"
    scale: int = Field(ge=0, le=18)


TransformationScalar: TypeAlias = str | int | float | bool | None


class ValueMapEntry(FrozenDomainModel):
    source: TransformationScalar
    target: TransformationScalar


class MapValuesStep(FrozenDomainModel):
    operation: Literal["map_values"] = "map_values"
    entries: tuple[ValueMapEntry, ...] = Field(min_length=1)


class PreserveNullStep(FrozenDomainModel):
    operation: Literal["preserve_null"] = "preserve_null"


class RejectInvalidStep(FrozenDomainModel):
    operation: Literal["reject_invalid"] = "reject_invalid"


TransformationStep: TypeAlias = Annotated[
    IdentityStep
    | TrimStep
    | EmptyToNullStep
    | ValidateRegexStep
    | ValidateFiniteStep
    | ValidateIntegralStep
    | StripLeadingZerosStep
    | PadLeftStep
    | CastIntegerToStringStep
    | CastTimestampToDateStep
    | ParseDateStep
    | NormalizeDecimalScaleStep
    | MapValuesStep
    | PreserveNullStep
    | RejectInvalidStep,
    Field(discriminator="operation"),
]


class TransformationPlan(FrozenDomainModel):
    """Serializable sequence from the fixed MVP operation set."""

    version: Literal[1] = 1
    steps: tuple[TransformationStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def leading_zero_operation_must_be_unambiguous(self) -> TransformationPlan:
        operations = {step.operation for step in self.steps}
        if "strip_leading_zeros" in operations and "pad_left" in operations:
            raise ValueError("a plan cannot both strip and pad leading zeros")
        return self


class NullPolicy(StrEnum):
    """How a source NULL is represented by identifier normalization."""

    PRESERVE = "preserve"
    REJECT = "reject"


class EmptyStringPolicy(StrEnum):
    """Whether an empty string is invalid or explicitly converted to NULL."""

    REJECT = "reject"
    AS_NULL = "as_null"


class LeadingZeroPolicy(StrEnum):
    """Explicit canonical treatment of leading zeros."""

    PRESERVE = "preserve"
    STRIP = "strip"
    PAD_TO_LENGTH = "pad_to_length"


class IdentifierNormalizationPlan(FrozenDomainModel):
    """Complete configuration for the pure identifier interpreter."""

    version: Literal[1] = 1
    null_policy: NullPolicy
    leading_zero_policy: LeadingZeroPolicy
    pad_to_length: int | None = Field(default=None, ge=1)
    trim_whitespace: bool = True
    empty_string_policy: EmptyStringPolicy = EmptyStringPolicy.REJECT

    @model_validator(mode="after")
    def padding_configuration_must_be_explicit(self) -> IdentifierNormalizationPlan:
        requires_length = self.leading_zero_policy is LeadingZeroPolicy.PAD_TO_LENGTH
        if requires_length and self.pad_to_length is None:
            raise ValueError("pad_to_length is required for pad_to_length policy")
        if not requires_length and self.pad_to_length is not None:
            raise ValueError("pad_to_length is only valid for pad_to_length policy")
        return self


class IdentifierSourceKind(StrEnum):
    NULL = "null"
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    UNSUPPORTED = "unsupported"


class NormalizationRejectionCode(StrEnum):
    """Stable machine-readable identifier rejection categories."""

    NULL_NOT_ALLOWED = "null_not_allowed"
    EMPTY_IDENTIFIER = "empty_identifier"
    BOOLEAN_IDENTIFIER = "boolean_identifier"
    NEGATIVE_IDENTIFIER = "negative_identifier"
    NON_FINITE_IDENTIFIER = "non_finite_identifier"
    NON_INTEGRAL_IDENTIFIER = "non_integral_identifier"
    UNSAFE_FLOAT_IDENTIFIER = "unsafe_float_identifier"
    MALFORMED_IDENTIFIER = "malformed_identifier"
    IDENTIFIER_TOO_LONG = "identifier_too_long"
    UNSUPPORTED_IDENTIFIER_TYPE = "unsupported_identifier_type"


class NormalizationPlanInvariantError(ValueError):
    """Raised only when validation was bypassed while constructing a plan."""


class NormalizationAccepted(FrozenDomainModel):
    status: Literal["accepted"] = "accepted"
    canonical_value: str | None
    source_kind: IdentifierSourceKind


class NormalizationRejected(FrozenDomainModel):
    status: Literal["rejected"] = "rejected"
    code: NormalizationRejectionCode
    reason: str = Field(min_length=1)
    source_kind: IdentifierSourceKind


NormalizationOutcome: TypeAlias = Annotated[
    NormalizationAccepted | NormalizationRejected,
    Field(discriminator="status"),
]
IdentifierInput: TypeAlias = str | int | float | Decimal | None

_MAX_SAFE_FLOAT_INTEGER = 9_007_199_254_740_991
_ASCII_DIGITS = re.compile(r"^[0-9]+$")
_NEGATIVE_DIGITS = re.compile(r"^-[0-9]+$")
_REJECTION_REASONS: Mapping[NormalizationRejectionCode, str] = MappingProxyType(
    {
        NormalizationRejectionCode.NULL_NOT_ALLOWED: (
            "NULL identifiers are not allowed by this plan."
        ),
        NormalizationRejectionCode.EMPTY_IDENTIFIER: (
            "The identifier is empty after normalization."
        ),
        NormalizationRejectionCode.BOOLEAN_IDENTIFIER: "Booleans are not valid identifiers.",
        NormalizationRejectionCode.NEGATIVE_IDENTIFIER: "Negative identifiers are not supported.",
        NormalizationRejectionCode.NON_FINITE_IDENTIFIER: (
            "The identifier must be finite; NaN and infinity are rejected."
        ),
        NormalizationRejectionCode.NON_INTEGRAL_IDENTIFIER: (
            "The identifier has a fractional component and cannot be truncated."
        ),
        NormalizationRejectionCode.UNSAFE_FLOAT_IDENTIFIER: (
            "The float exceeds the exact integer range and may already have lost precision."
        ),
        NormalizationRejectionCode.MALFORMED_IDENTIFIER: (
            "The identifier must contain ASCII decimal digits only."
        ),
        NormalizationRejectionCode.IDENTIFIER_TOO_LONG: (
            "The identifier exceeds the configured canonical padding length."
        ),
        NormalizationRejectionCode.UNSUPPORTED_IDENTIFIER_TYPE: (
            "The identifier type is not supported by the normalization plan."
        ),
    }
)


def _reject(
    code: NormalizationRejectionCode,
    source_kind: IdentifierSourceKind,
) -> NormalizationRejected:
    return NormalizationRejected(
        code=code,
        reason=_REJECTION_REASONS[code],
        source_kind=source_kind,
    )


def _normalize_null(
    plan: IdentifierNormalizationPlan,
    source_kind: IdentifierSourceKind = IdentifierSourceKind.NULL,
) -> NormalizationOutcome:
    if plan.null_policy is NullPolicy.PRESERVE:
        return NormalizationAccepted(canonical_value=None, source_kind=source_kind)
    return _reject(NormalizationRejectionCode.NULL_NOT_ALLOWED, source_kind)


def _digits_from_value(
    value: object,
) -> tuple[str | None, IdentifierSourceKind, NormalizationRejected | None]:
    if isinstance(value, bool):
        kind = IdentifierSourceKind.BOOLEAN
        return None, kind, _reject(NormalizationRejectionCode.BOOLEAN_IDENTIFIER, kind)

    if isinstance(value, str):
        return value, IdentifierSourceKind.STRING, None

    if isinstance(value, int):
        kind = IdentifierSourceKind.INTEGER
        if value < 0:
            return None, kind, _reject(NormalizationRejectionCode.NEGATIVE_IDENTIFIER, kind)
        return str(value), kind, None

    if isinstance(value, float):
        kind = IdentifierSourceKind.FLOAT
        if not math.isfinite(value):
            return None, kind, _reject(NormalizationRejectionCode.NON_FINITE_IDENTIFIER, kind)
        if not value.is_integer():
            return None, kind, _reject(NormalizationRejectionCode.NON_INTEGRAL_IDENTIFIER, kind)
        if value < 0:
            return None, kind, _reject(NormalizationRejectionCode.NEGATIVE_IDENTIFIER, kind)
        if abs(value) > _MAX_SAFE_FLOAT_INTEGER:
            return None, kind, _reject(NormalizationRejectionCode.UNSAFE_FLOAT_IDENTIFIER, kind)
        return str(int(value)), kind, None

    if isinstance(value, Decimal):
        kind = IdentifierSourceKind.DECIMAL
        if not value.is_finite():
            return None, kind, _reject(NormalizationRejectionCode.NON_FINITE_IDENTIFIER, kind)
        if value != value.to_integral_value():
            return None, kind, _reject(NormalizationRejectionCode.NON_INTEGRAL_IDENTIFIER, kind)
        if value < 0:
            return None, kind, _reject(NormalizationRejectionCode.NEGATIVE_IDENTIFIER, kind)
        return str(int(value)), kind, None

    kind = IdentifierSourceKind.UNSUPPORTED
    return None, kind, _reject(NormalizationRejectionCode.UNSUPPORTED_IDENTIFIER_TYPE, kind)


def normalize_identifier(
    value: IdentifierInput | object,
    plan: IdentifierNormalizationPlan,
) -> NormalizationOutcome:
    """Normalize an identifier without I/O, truncation, or implicit zero policy."""

    if value is None:
        return _normalize_null(plan)

    digits, source_kind, rejection = _digits_from_value(value)
    if rejection is not None:
        return rejection
    if digits is None:
        return _reject(NormalizationRejectionCode.UNSUPPORTED_IDENTIFIER_TYPE, source_kind)

    if source_kind is IdentifierSourceKind.STRING:
        if plan.trim_whitespace:
            digits = digits.strip()
        if not digits:
            if plan.empty_string_policy is EmptyStringPolicy.AS_NULL:
                return _normalize_null(plan, source_kind)
            return _reject(NormalizationRejectionCode.EMPTY_IDENTIFIER, source_kind)
        if _NEGATIVE_DIGITS.fullmatch(digits) is not None:
            return _reject(NormalizationRejectionCode.NEGATIVE_IDENTIFIER, source_kind)

    if _ASCII_DIGITS.fullmatch(digits) is None:
        return _reject(NormalizationRejectionCode.MALFORMED_IDENTIFIER, source_kind)

    if plan.leading_zero_policy is LeadingZeroPolicy.PRESERVE:
        canonical = digits
    else:
        canonical = digits.lstrip("0") or "0"
        if plan.leading_zero_policy is LeadingZeroPolicy.PAD_TO_LENGTH:
            if plan.pad_to_length is None:
                raise NormalizationPlanInvariantError(
                    "validated padding plan is missing pad_to_length"
                )
            if len(canonical) > plan.pad_to_length:
                return _reject(NormalizationRejectionCode.IDENTIFIER_TOO_LONG, source_kind)
            canonical = canonical.zfill(plan.pad_to_length)

    return NormalizationAccepted(canonical_value=canonical, source_kind=source_kind)
