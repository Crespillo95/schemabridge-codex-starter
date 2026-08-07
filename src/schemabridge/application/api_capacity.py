"""Application boundary for distributed authenticated API admission."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    TenantCapacityPort,
)
from schemabridge.domain.catalog_inventory import CapacityAdmission
from schemabridge.domain.identity import AuthenticatedPrincipal


class ApiCapacityErrorCode(StrEnum):
    """Stable HTTP-safe outcomes for the distributed admission boundary."""

    RATE_LIMITED = "api_rate_limited"
    UNAVAILABLE = "api_capacity_unavailable"


class ApiCapacityError(RuntimeError):
    """A rate decision failed without exposing persistence details."""

    def __init__(
        self,
        code: ApiCapacityErrorCode,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AdmitAuthenticatedApiRequest:
    """Apply one workspace/principal admission after successful authentication."""

    capacity: TenantCapacityPort

    def execute(self, principal: AuthenticatedPrincipal) -> CapacityAdmission:
        try:
            decision = self.capacity.admit_api_request(
                principal.workspace_id,
                principal.actor_id,
            )
        except (CatalogInventoryError, TypeError, ValueError) as error:
            raise ApiCapacityError(
                ApiCapacityErrorCode.UNAVAILABLE,
                "API capacity admission is unavailable",
            ) from error
        if not decision.allowed:
            raise ApiCapacityError(
                ApiCapacityErrorCode.RATE_LIMITED,
                "authenticated API request rate is exhausted",
                retry_after_seconds=decision.retry_after_seconds,
            )
        return decision


__all__ = [
    "AdmitAuthenticatedApiRequest",
    "ApiCapacityError",
    "ApiCapacityErrorCode",
]
