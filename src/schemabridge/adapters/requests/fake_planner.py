"""Deterministic M09 planner seam that performs no physical resolution."""

from __future__ import annotations

from schemabridge.application.ports.requests import RequestPlanningAcknowledgement
from schemabridge.domain.request_context import (
    ValidatedAnalyticalRequest,
    validated_analytical_request_fingerprint,
)


class FakeRequestPlanner:
    """Capture validated requests and acknowledge that M10 planning is deliberately deferred."""

    def __init__(self) -> None:
        self.accepted_requests: list[ValidatedAnalyticalRequest] = []

    def accept(
        self,
        request: ValidatedAnalyticalRequest,
    ) -> RequestPlanningAcknowledgement:
        self.accepted_requests.append(request)
        return RequestPlanningAcknowledgement(
            adapter="fake:no-physical-resolution",
            request_fingerprint=validated_analytical_request_fingerprint(request),
            status="accepted_for_future_planning",
        )
