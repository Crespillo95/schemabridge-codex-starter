"""Deterministic identifier-normalization demonstration use case."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemabridge.domain.transformations import (
    IdentifierInput,
    IdentifierNormalizationPlan,
    LeadingZeroPolicy,
    NormalizationOutcome,
    NullPolicy,
    normalize_identifier,
)


@dataclass(frozen=True, slots=True)
class NormalizationDemoResult:
    """One labeled synthetic input and its typed domain outcome."""

    case: str
    source_value: str
    outcome: NormalizationOutcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "source_value": self.source_value,
            "outcome": self.outcome.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NormalizationDemoReport:
    """Complete reproducible CLI demonstration."""

    plan: IdentifierNormalizationPlan
    results: tuple[NormalizationDemoResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="json"),
            "results": [result.as_dict() for result in self.results],
        }


_DEMO_CASES: tuple[tuple[str, str, IdentifierInput], ...] = (
    ("padded_string", '"00000000123"', "00000000123"),
    ("integer", "123", 123),
    ("integral_float", "123.0", 123.0),
    ("all_zero", '"0000"', "0000"),
    ("fractional_float", "123.5", 123.5),
    ("nan", "NaN", float("nan")),
    ("positive_infinity", "Infinity", float("inf")),
    ("negative_infinity", "-Infinity", float("-inf")),
    ("boolean", "true", True),
    ("malformed_string", '"12x3"', "12x3"),
    ("null", "NULL", None),
)


def run_normalization_demo() -> NormalizationDemoReport:
    """Run the fixed synthetic strip-policy boundary set."""

    plan = IdentifierNormalizationPlan(
        null_policy=NullPolicy.REJECT,
        leading_zero_policy=LeadingZeroPolicy.STRIP,
    )
    results = tuple(
        NormalizationDemoResult(
            case=case,
            source_value=source_value,
            outcome=normalize_identifier(value, plan),
        )
        for case, source_value, value in _DEMO_CASES
    )
    return NormalizationDemoReport(plan=plan, results=results)
