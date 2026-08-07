from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

import pytest
from scripts.check_demo_seed import (
    SeedVerificationError,
    canonical_sha256,
    canonical_value,
    first_difference,
    snapshot_fingerprint,
    verify_snapshot,
)


def test_canonical_hash_is_type_aware_and_stable_across_mapping_order() -> None:
    left = {
        "decimal": Decimal("10.50"),
        "integer": 10,
        "text": "10",
        "date": date(2026, 1, 1),
        "timestamp": datetime(2026, 1, 1, 8, 30),
    }
    right = dict(reversed(tuple(left.items())))

    assert canonical_sha256((left,)) == canonical_sha256((right,))
    assert canonical_sha256(((Decimal("10.50"),),)) != canonical_sha256((("10.50",),))
    assert canonical_sha256(((10,),)) != canonical_sha256((("10",),))


def test_canonical_float_handles_non_finite_values_without_json_nan() -> None:
    assert canonical_value(float("nan")) == {"type": "float", "value": "NaN"}
    assert canonical_value(float("inf")) == {"type": "float", "value": "Infinity"}
    assert canonical_value(float("-inf")) == {"type": "float", "value": "-Infinity"}
    assert canonical_value(-0.0) == {"type": "float", "value": "0"}
    assert math.isnan(float("nan"))


def test_snapshot_fingerprint_excludes_only_its_own_digest() -> None:
    snapshot = {"format_version": 1, "tables": {"demo.table": {"row_count": 1}}}
    fingerprint = snapshot_fingerprint(snapshot)

    assert snapshot_fingerprint({**snapshot, "global_sha256": "ignored"}) == fingerprint
    assert snapshot_fingerprint({**snapshot, "format_version": 2}) != fingerprint


def test_verifier_reports_first_nested_drift_without_database_details() -> None:
    expected = {
        "tables": {
            "sales.orders": {
                "row_count": 60,
                "data_sha256": "a" * 64,
            }
        }
    }
    actual = {
        "tables": {
            "sales.orders": {
                "row_count": 61,
                "data_sha256": "b" * 64,
            }
        }
    }

    assert first_difference(expected, actual) == (
        f"$.tables.sales.orders.data_sha256: expected {'a' * 64!r}, observed {'b' * 64!r}"
    )
    with pytest.raises(SeedVerificationError, match="data_sha256"):
        verify_snapshot(expected, actual)


def test_verifier_rejects_missing_or_unexpected_inventory_keys() -> None:
    with pytest.raises(SeedVerificationError, match="missing keys"):
        verify_snapshot(
            {"tables": {"commerce.products": {"row_count": 40}}},
            {"tables": {}},
        )
