"""Ports and use cases for compilation, independent guarding, and preview."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from schemabridge.domain.plans import ParameterScalar, QueryPlan, QueryPolicy


class QueryCompilationError(RuntimeError):
    """A restricted query plan cannot be compiled safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SqlRejectionCode(StrEnum):
    PARSE_ERROR = "parse_error"
    COMMENTS_FORBIDDEN = "comments_forbidden"
    MULTIPLE_STATEMENTS = "multiple_statements"
    NON_READ_ONLY_STATEMENT = "non_read_only_statement"
    FORBIDDEN_STATEMENT = "forbidden_statement"
    SELECT_INTO = "select_into"
    RECURSIVE_CTE = "recursive_cte"
    UNKNOWN_ASSET = "unknown_asset"
    REPEATED_ASSET = "repeated_asset"
    DUPLICATE_ALIAS = "duplicate_alias"
    UNKNOWN_COLUMN = "unknown_column"
    WILDCARD_PROJECTION = "wildcard_projection"
    CARTESIAN_JOIN = "cartesian_join"
    MISSING_JOIN_PREDICATE = "missing_join_predicate"
    TOO_MANY_TABLES = "too_many_tables"
    MISSING_PREVIEW_LIMIT = "missing_preview_limit"
    INVALID_PREVIEW_LIMIT = "invalid_preview_limit"
    UNSAFE_FUNCTION = "unsafe_function"
    PARAMETER_MISMATCH = "parameter_mismatch"


@dataclass(frozen=True, slots=True)
class SqlPolicyFinding:
    code: SqlRejectionCode
    message: str


class SqlPolicyViolation(RuntimeError):
    """Final SQL failed at least one independent guard policy."""

    def __init__(self, findings: tuple[SqlPolicyFinding, ...]) -> None:
        self.findings = findings
        summary = ", ".join(finding.code.value for finding in findings)
        super().__init__(f"SQL policy rejected the query: {summary}")


class QueryPreviewError(RuntimeError):
    """Base error for bounded preview execution."""


class QueryPreviewUnavailableError(QueryPreviewError):
    """The preview database could not execute the request."""


class QueryPreviewTimeoutError(QueryPreviewError):
    """The preview exceeded its configured statement timeout."""


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """Parameterized PostgreSQL produced by a compiler adapter."""

    sql: str
    parameters: tuple[ParameterScalar, ...]
    effective_limit: int


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    """Final SQL accepted by an independent policy guard."""

    sql: str
    parameters: tuple[ParameterScalar, ...]
    max_rows: int
    statement_timeout_ms: int


@dataclass(frozen=True, slots=True)
class QueryPreviewResult:
    """Bounded rows plus independently observed connection safety facts."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    database_user: str
    transaction_read_only: bool
    statement_timeout_ms: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "database_user": self.database_user,
            "transaction_read_only": self.transaction_read_only,
            "statement_timeout_ms": self.statement_timeout_ms,
            "truncated": self.truncated,
        }


class QueryCompilerPort(Protocol):
    def compile(self, plan: QueryPlan, *, max_preview_rows: int) -> CompiledQuery:
        """Compile restricted IR into parameterized SQL."""


class SqlPolicyGuardPort(Protocol):
    def validate(self, query: CompiledQuery, policy: QueryPolicy) -> ValidatedQuery:
        """Reparse and independently validate final SQL."""


class QueryPreviewPort(Protocol):
    def execute(self, query: ValidatedQuery) -> QueryPreviewResult:
        """Execute one guarded query through a bounded read-only transaction."""


@dataclass(frozen=True, slots=True)
class PrepareQuery:
    """Compiler-to-guard orchestration; no compiled SQL bypass is available here."""

    compiler: QueryCompilerPort
    guard: SqlPolicyGuardPort
    policy: QueryPolicy

    def execute(self, plan: QueryPlan) -> ValidatedQuery:
        compiled = self.compiler.compile(plan, max_preview_rows=self.policy.max_preview_rows)
        return self.guard.validate(compiled, self.policy)


@dataclass(frozen=True, slots=True)
class PreviewQuery:
    """Prepare and execute one bounded query."""

    prepare: PrepareQuery
    executor: QueryPreviewPort

    def execute(self, plan: QueryPlan) -> QueryPreviewResult:
        return self.executor.execute(self.prepare.execute(plan))
