"""Lease-bound dynamic PostgreSQL evidence for semantic join profiling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from schemabridge.adapters.connectors.local_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
    ResolvedPostgresSecret,
)
from schemabridge.adapters.connectors.postgres_profile_routing import (
    ProfilePostgresConnectorRoute,
)
from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
)
from schemabridge.adapters.postgres.relationships import (
    PsycopgRelationshipEvidenceAdapter,
)
from schemabridge.application.connectors import (
    ConnectorTargetError,
    ConnectorTargetErrorCode,
)
from schemabridge.application.ports.relationships import (
    RelationshipErrorCode,
    RelationshipWorkflowError,
)
from schemabridge.application.ports.semantic_profile_jobs import (
    SemanticJoinProfileEvidencePort,
    SemanticJoinProfileRouteContext,
    SemanticJoinProfileSourceCancelled,
)
from schemabridge.domain.connectors import SourceDialect
from schemabridge.domain.joins import RelationshipProfile
from schemabridge.domain.semantic_profile_jobs import SemanticJoinProfileProposal


class _ProfileRouteReader(Protocol):
    def load(
        self,
        context: SemanticJoinProfileRouteContext,
    ) -> ProfilePostgresConnectorRoute: ...


class _ProfileSecretResolver(Protocol):
    def resolve_postgres_route(
        self,
        reference: OpaqueConnectorSecretRef,
        *,
        dialect: SourceDialect,
        expected_reader: str,
    ) -> ResolvedPostgresSecret: ...


@dataclass(frozen=True, slots=True)
class RoutedSemanticJoinProfileEvidenceFactory:
    """Create one evidence port bound to an exact live profile-job lease."""

    route_reader: _ProfileRouteReader = field(repr=False)
    secret_resolver: _ProfileSecretResolver = field(repr=False)
    statement_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if not 100 <= self.statement_timeout_ms <= 60_000:
            raise ValueError("semantic profile statement timeout is outside its bounded range")

    def for_claim(
        self,
        context: SemanticJoinProfileRouteContext,
        *,
        should_continue: Callable[[], bool],
    ) -> SemanticJoinProfileEvidencePort:
        return _LeaseBoundSemanticJoinProfileEvidence(
            route_reader=self.route_reader,
            secret_resolver=self.secret_resolver,
            context=context,
            should_continue=should_continue,
            statement_timeout_ms=self.statement_timeout_ms,
        )


@dataclass(frozen=True, slots=True)
class _LeaseBoundSemanticJoinProfileEvidence:
    route_reader: _ProfileRouteReader = field(repr=False)
    secret_resolver: _ProfileSecretResolver = field(repr=False)
    context: SemanticJoinProfileRouteContext = field(repr=False)
    should_continue: Callable[[], bool] = field(repr=False)
    statement_timeout_ms: int

    def profile_bound(
        self,
        proposal: SemanticJoinProfileProposal,
    ) -> RelationshipProfile:
        checked = SemanticJoinProfileProposal.model_validate(proposal.model_dump(mode="json"))
        if checked.connection_id != self.context.execution_target.connection_id:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                "relationship proposal targets another connector route",
            )
        _require_continue(self.should_continue)
        try:
            route = self.route_reader.load(self.context)
            _require_continue(self.should_continue)
            secret = self.secret_resolver.resolve_postgres_route(
                route.secret_reference,
                dialect=route.dialect,
                expected_reader=route.expected_reader,
            )
            _require_continue(self.should_continue)
            return PsycopgRelationshipEvidenceAdapter(
                secret.dsn,
                (checked.proposal,),
                expected_user=secret.expected_reader,
                expected_source_identity_fingerprint=route.source_identity_fingerprint,
                statement_timeout_ms=self.statement_timeout_ms,
            ).profile(
                checked.proposal,
                should_continue=self.should_continue,
            )
        except SemanticJoinProfileSourceCancelled:
            raise
        except PostgresSourceIdentityMismatchError:
            raise RelationshipWorkflowError(
                RelationshipErrorCode.EVIDENCE_NOT_ALLOWED,
                "semantic profile connector route is unavailable",
            ) from None
        except ConnectorTargetError as error:
            raise _route_failure(error.code) from None
        except ConnectorSecretResolutionError as error:
            code = (
                RelationshipErrorCode.EVIDENCE_UNAVAILABLE
                if error.code is ConnectorSecretErrorCode.UNAVAILABLE
                else RelationshipErrorCode.EVIDENCE_NOT_ALLOWED
            )
            raise RelationshipWorkflowError(
                code,
                "semantic profile connector secret is unavailable",
            ) from None


def _require_continue(should_continue: Callable[[], bool]) -> None:
    try:
        allowed = should_continue()
    except SemanticJoinProfileSourceCancelled:
        raise
    except Exception:
        raise SemanticJoinProfileSourceCancelled(
            "semantic profile source operation was cooperatively cancelled"
        ) from None
    if allowed is not True:
        raise SemanticJoinProfileSourceCancelled(
            "semantic profile source operation was cooperatively cancelled"
        )


def _route_failure(code: ConnectorTargetErrorCode) -> RelationshipWorkflowError:
    evidence_code = (
        RelationshipErrorCode.EVIDENCE_UNAVAILABLE
        if code
        in {
            ConnectorTargetErrorCode.UNAVAILABLE,
            ConnectorTargetErrorCode.SECRET_UNAVAILABLE,
        }
        else RelationshipErrorCode.EVIDENCE_NOT_ALLOWED
    )
    return RelationshipWorkflowError(
        evidence_code,
        "semantic profile connector route is unavailable",
    )


__all__ = ["RoutedSemanticJoinProfileEvidenceFactory"]
