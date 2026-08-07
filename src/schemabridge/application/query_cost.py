"""Validation and stable failures for governed query-cost preflight."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from schemabridge.application.connectors import ConnectorTargetError
from schemabridge.application.ports.query_cost import QueryCostPreflightPort
from schemabridge.application.query_execution import ValidatedQuery
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceDialect,
)


class QueryCostErrorCode(StrEnum):
    """Stable non-disclosing application failures."""

    REJECTED = "query_cost_rejected"
    TIMEOUT = "query_cost_timeout"
    UNAVAILABLE = "query_cost_unavailable"
    INVALID = "query_cost_invalid"


class QueryCostError(RuntimeError):
    """A cost preflight failed or rejected the exact query."""

    def __init__(
        self,
        code: QueryCostErrorCode,
        message: str,
        *,
        assessment: QueryCostAssessment | None = None,
    ) -> None:
        self.code = code
        self.assessment = assessment
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AssessGovernedQueryCost:
    """Call the connector port and distrust every returned binding."""

    preflight: QueryCostPreflightPort

    def execute(
        self,
        query: ValidatedQuery,
        target: GovernedExecutionTarget,
    ) -> QueryCostAssessment:
        if (
            query.dialect is not SourceDialect.POSTGRESQL
            or query.dialect is not target.dialect
            or query.target_fingerprint != target.fingerprint
        ):
            raise QueryCostError(
                QueryCostErrorCode.INVALID,
                "query cost preflight does not match the governed execution target",
            )
        try:
            assessment = self.preflight.assess(query, target)
        except (ConnectorTargetError, QueryCostError):
            raise
        except Exception as error:
            raise QueryCostError(
                QueryCostErrorCode.UNAVAILABLE,
                "query cost preflight is unavailable",
            ) from error
        if type(assessment) is not QueryCostAssessment:
            raise QueryCostError(
                QueryCostErrorCode.INVALID,
                "query cost preflight returned invalid evidence",
            )
        if (
            assessment.target_fingerprint != target.fingerprint
            or assessment.budget_fingerprint != target.cost_budget_fingerprint
            or assessment.cost_budget != target.cost_budget
            or assessment.explain_timeout_ms > target.cost_budget.explain_timeout_ms
        ):
            raise QueryCostError(
                QueryCostErrorCode.INVALID,
                "query cost preflight returned mismatched evidence",
            )
        return assessment

    @staticmethod
    def require_accepted(assessment: QueryCostAssessment) -> None:
        """Fail closed while retaining the sanitized assessment for presentation."""

        if assessment.decision is QueryCostDecision.ACCEPTED:
            return
        codes = set(assessment.rejection_codes)
        if QueryCostRejectionCode.TIMEOUT in codes:
            code = QueryCostErrorCode.TIMEOUT
        elif QueryCostRejectionCode.UNAVAILABLE in codes:
            code = QueryCostErrorCode.UNAVAILABLE
        elif (
            QueryCostRejectionCode.INVALID in codes
            or QueryCostRejectionCode.TARGET_FINGERPRINT_MISMATCH in codes
            or QueryCostRejectionCode.BUDGET_FINGERPRINT_MISMATCH in codes
        ):
            code = QueryCostErrorCode.INVALID
        else:
            code = QueryCostErrorCode.REJECTED
        raise QueryCostError(
            code,
            "query cost preflight rejected the governed query",
            assessment=assessment,
        )
