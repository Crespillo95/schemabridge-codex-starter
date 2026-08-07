"""Application contracts for transient, standalone SQL copy artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal, Protocol

from schemabridge.application.query_execution import (
    CompiledQuery,
    SqlPolicyGuardPort,
    ValidatedQuery,
)
from schemabridge.domain.connectors import GovernedExecutionTarget, SourceDialect
from schemabridge.domain.plans import QueryPolicy


@dataclass(frozen=True, slots=True)
class CopyableSqlArtifact:
    """Transient presentation SQL; it is deliberately not an executor input."""

    sql: str = field(repr=False)
    dialect: SourceDialect
    plan_version: Literal[1, 2]
    request_fingerprint: str
    plan_fingerprint: str
    sha256: str
    target_fingerprint: str | None = None
    executed: bool = field(default=False, init=False)


class CopyableSqlRendererPort(Protocol):
    def render(
        self,
        query: ValidatedQuery,
        *,
        request_fingerprint: str,
        plan_fingerprint: str,
    ) -> CopyableSqlArtifact:
        """Render typed bindings as standalone dialect-specific SQL."""


@dataclass(frozen=True, slots=True)
class BuildCopyableSql:
    """Render presentation SQL and independently guard the literalized result."""

    renderer: CopyableSqlRendererPort
    guard: SqlPolicyGuardPort
    policy: QueryPolicy

    def execute(
        self,
        query: ValidatedQuery,
        *,
        request_fingerprint: str,
        plan_fingerprint: str,
        target: GovernedExecutionTarget | None = None,
    ) -> CopyableSqlArtifact:
        artifact = self.renderer.render(
            query,
            request_fingerprint=request_fingerprint,
            plan_fingerprint=plan_fingerprint,
        )
        if (
            artifact.dialect is not query.dialect
            or artifact.plan_version != query.plan_version
            or artifact.request_fingerprint != request_fingerprint
            or artifact.plan_fingerprint != plan_fingerprint
            or artifact.target_fingerprint != query.target_fingerprint
            or artifact.sha256 != hashlib.sha256(artifact.sql.encode("utf-8")).hexdigest()
        ):
            raise RuntimeError(
                "copyable SQL renderer returned metadata outside the requested artifact"
            )
        reguarded = self.guard.validate(
            CompiledQuery(
                sql=artifact.sql,
                parameters=(),
                effective_limit=query.max_rows,
                dialect=artifact.dialect,
                target_fingerprint=artifact.target_fingerprint,
                plan_version=artifact.plan_version,
            ),
            self.policy,
            target=target,
        )
        if reguarded.sql != artifact.sql or reguarded.parameters:
            raise RuntimeError("copyable SQL guard changed the immutable presentation artifact")
        return artifact
