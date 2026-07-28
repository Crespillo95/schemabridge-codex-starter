"""Port for one independently bounded query-cost preflight."""

from __future__ import annotations

from typing import Protocol

from schemabridge.application.query_execution import ValidatedQuery
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
)


class QueryCostPreflightPort(Protocol):
    """Observe sanitized PostgreSQL planner facts without executing the SELECT."""

    def assess(
        self,
        query: ValidatedQuery,
        target: GovernedExecutionTarget,
    ) -> QueryCostAssessment:
        """Run bounded EXPLAIN without ANALYZE for the exact query and target."""
