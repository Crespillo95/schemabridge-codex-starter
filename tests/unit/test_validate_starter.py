"""Regression coverage for the static starter integrity validator."""

from __future__ import annotations

import pytest
from scripts.validate_starter import is_versioned_mapping


@pytest.mark.parametrize("version", [1, 2, 99])
def test_ground_truth_schema_versions_may_advance(version: int) -> None:
    assert is_versioned_mapping({"version": version}) is True


def test_seed_manifest_format_version_is_supported() -> None:
    assert is_versioned_mapping({"format_version": 1}) is True


@pytest.mark.parametrize("version", [None, True, False, 0, -1, 1.0, "2"])
def test_ground_truth_schema_version_must_be_a_positive_integer(version: object) -> None:
    assert is_versioned_mapping({"version": version}) is False


def test_ground_truth_root_must_be_a_mapping() -> None:
    assert is_versioned_mapping([{"version": 1}]) is False
