from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from schemabridge.application.api_capacity import (
    AdmitAuthenticatedApiRequest,
    ApiCapacityError,
    ApiCapacityErrorCode,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    TenantCapacityPort,
)
from schemabridge.domain.catalog_inventory import CapacityAdmission, CapacityResource
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)

NOW = datetime(2026, 7, 23, 22, 0, tzinfo=UTC)


class _Capacity:
    def __init__(self, *, allowed: bool = True, fail: bool = False) -> None:
        self.allowed = allowed
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def admit_api_request(
        self,
        workspace_id: str,
        actor_id: str,
    ) -> CapacityAdmission:
        self.calls.append((workspace_id, actor_id))
        if self.fail:
            raise CatalogInventoryError(
                CatalogInventoryErrorCode.UNAVAILABLE,
                "database detail must not cross the use case",
            )
        return CapacityAdmission(
            workspace_id=workspace_id,
            resource=CapacityResource.API_REQUEST,
            allowed=self.allowed,
            used=3,
            limit=3,
            retry_after_seconds=None if self.allowed else 17,
        )

    def load_policy(self, workspace_id: str) -> None:
        del workspace_id
        return None

    def inspect(self, workspace_id: str) -> None:
        del workspace_id
        return None


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        actor_id="actor_analyst",
        workspace_id="workspace_alpha",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def test_authenticated_admission_uses_exact_workspace_and_actor() -> None:
    capacity = _Capacity()
    use_case = AdmitAuthenticatedApiRequest(
        capacity=cast(TenantCapacityPort, capacity),
    )

    decision = use_case.execute(_principal())

    assert decision.allowed
    assert capacity.calls == [("workspace_alpha", "actor_analyst")]


def test_rate_denial_preserves_only_bounded_retry_metadata() -> None:
    use_case = AdmitAuthenticatedApiRequest(
        capacity=cast(TenantCapacityPort, _Capacity(allowed=False)),
    )

    with pytest.raises(ApiCapacityError) as denied:
        use_case.execute(_principal())

    assert denied.value.code is ApiCapacityErrorCode.RATE_LIMITED
    assert denied.value.retry_after_seconds == 17


def test_capacity_outage_is_sanitized() -> None:
    use_case = AdmitAuthenticatedApiRequest(
        capacity=cast(TenantCapacityPort, _Capacity(fail=True)),
    )

    with pytest.raises(ApiCapacityError) as unavailable:
        use_case.execute(_principal())

    assert unavailable.value.code is ApiCapacityErrorCode.UNAVAILABLE
    assert "database detail" not in str(unavailable.value)
