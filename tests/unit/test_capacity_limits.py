"""Unit tests for data-driven tenant capacity and local scale evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from schemabridge.domain.catalog_inventory import (
    CapacityAdmission,
    CapacityResource,
    CatalogConnectionId,
    CatalogScaleReport,
    TenantCapacityPolicy,
    TenantCapacitySnapshot,
    TenantCapacityUsage,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _policy(
    *,
    workspace_id: str = "workspace_large",
    version: int = 1,
    connection_limit: int = 25,
    asset_limit: int = 10_000,
    field_limit: int = 100_000,
) -> TenantCapacityPolicy:
    return TenantCapacityPolicy(
        workspace_id=workspace_id,
        version=version,
        connection_limit=connection_limit,
        asset_limit=asset_limit,
        field_limit=field_limit,
        api_requests_per_minute=1_000,
        nonterminal_job_limit=100,
        updated_by="actor_platform_admin",
        updated_at=NOW,
    )


def _usage(
    *,
    workspace_id: str = "workspace_large",
    connections: int = 10,
    assets: int = 5_434,
    fields: int = 21_736,
    jobs: int = 2,
) -> TenantCapacityUsage:
    return TenantCapacityUsage(
        workspace_id=workspace_id,
        connection_count=connections,
        asset_count=assets,
        field_count=fields,
        nonterminal_job_count=jobs,
        observed_at=NOW,
    )


def test_capacity_accepts_small_and_large_tenants_without_code_total() -> None:
    small = TenantCapacitySnapshot(
        policy=_policy(
            workspace_id="workspace_small",
            connection_limit=10,
            asset_limit=10,
            field_limit=100,
        ),
        usage=_usage(
            workspace_id="workspace_small",
            connections=2,
            assets=10,
            fields=40,
        ),
    )
    large = TenantCapacitySnapshot(policy=_policy(), usage=_usage())

    assert small.usage.asset_count == 10
    assert large.usage.asset_count == 5_434
    assert not small.over_capacity
    assert not large.over_capacity


def test_lowered_durable_policy_can_report_existing_overage_without_data_loss() -> None:
    before = TenantCapacitySnapshot(policy=_policy(), usage=_usage())
    after = TenantCapacitySnapshot(
        policy=_policy(version=2, asset_limit=5_000, field_limit=20_000),
        usage=_usage(),
    )

    assert before.over_capacity is False
    assert after.over_capacity is True
    assert after.usage.asset_count == before.usage.asset_count
    assert after.policy.version == 2


def test_capacity_policy_is_exactly_tenant_scoped() -> None:
    with pytest.raises(ValidationError, match="workspace"):
        TenantCapacitySnapshot(
            policy=_policy(workspace_id="workspace_alpha"),
            usage=_usage(workspace_id="workspace_beta"),
        )


def test_capacity_policy_rejects_zero_negative_and_unbounded_values() -> None:
    for changes in (
        {"connection_limit": 0},
        {"asset_limit": -1},
        {"field_limit": 1_000_000_001},
        {"api_requests_per_minute": 0},
        {"nonterminal_job_limit": 0},
    ):
        payload = _policy().model_dump(mode="python")
        payload.update(changes)
        with pytest.raises(ValidationError):
            TenantCapacityPolicy.model_validate(payload)


def test_rate_admission_represents_fourth_request_denial_with_retry_after() -> None:
    first_three = CapacityAdmission(
        workspace_id="workspace_alpha",
        resource=CapacityResource.API_REQUEST,
        allowed=True,
        used=3,
        limit=3,
    )
    fourth = CapacityAdmission(
        workspace_id="workspace_alpha",
        resource=CapacityResource.API_REQUEST,
        allowed=False,
        used=3,
        limit=3,
        retry_after_seconds=42,
    )

    assert first_three.allowed
    assert fourth.allowed is False
    assert fourth.retry_after_seconds == 42


def test_capacity_admission_rejects_incoherent_counts_or_retry_metadata() -> None:
    with pytest.raises(ValidationError, match="reached"):
        CapacityAdmission(
            workspace_id="workspace_alpha",
            resource=CapacityResource.EXECUTION_JOB,
            allowed=False,
            used=4,
            limit=5,
        )
    with pytest.raises(ValidationError, match="retry"):
        CapacityAdmission(
            workspace_id="workspace_alpha",
            resource=CapacityResource.API_REQUEST,
            allowed=False,
            used=3,
            limit=3,
        )
    with pytest.raises(ValidationError, match="cannot request"):
        CapacityAdmission(
            workspace_id="workspace_alpha",
            resource=CapacityResource.API_REQUEST,
            allowed=True,
            used=1,
            limit=3,
            retry_after_seconds=1,
        )


def test_scale_report_records_5434_in_109_pages_and_remains_local_evidence() -> None:
    report = CatalogScaleReport(
        workspace_id="workspace_large",
        connection_id=CatalogConnectionId("connection_primary"),
        platform="macOS arm64; CPython 3.13",
        asset_count=5_434,
        field_count=21_736,
        page_size=50,
        page_count=109,
        maximum_rows_read=51,
        elapsed_milliseconds=12_500,
        heap_delta_bytes=8 * 1024 * 1024,
        rss_delta_bytes=32 * 1024 * 1024,
        measured_at=NOW,
    )

    assert report.asset_count == 5_434
    assert report.page_count == 109
    assert report.maximum_rows_read == 51
    assert report.production_slo is False

    payload = report.model_dump(mode="python")
    payload["production_slo"] = True
    with pytest.raises(ValidationError, match="production SLO"):
        CatalogScaleReport.model_validate(payload)


def test_scale_report_rejects_inventory_total_as_page_size() -> None:
    with pytest.raises(ValidationError):
        CatalogScaleReport(
            workspace_id="workspace_large",
            connection_id=CatalogConnectionId("connection_primary"),
            platform="synthetic",
            asset_count=5_434,
            field_count=21_736,
            page_size=5_434,
            page_count=1,
            maximum_rows_read=5_434,
            elapsed_milliseconds=1,
            heap_delta_bytes=1,
            rss_delta_bytes=1,
            measured_at=NOW,
        )
