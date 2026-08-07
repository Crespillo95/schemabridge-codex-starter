"""Read the exact active strict registry-v2 payload from the control plane.

This API-side adapter deliberately has no DataHub client or mutation capability.  It accepts
only the registry version selected by the authoritative pointer and reconstructs that version
from the already completed, exactly scoped publication job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from psycopg import sql
from pydantic import BaseModel, ValidationError

from schemabridge.adapters.storage.postgres import ControlConnectionProvider, _ControlDatabase
from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryControlErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    GovernedRegistryVersion,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.registry_publication import (
    PublicationReadbackReceipt,
    PublishableRegistryVersion,
    RegistryPublicationAuthorization,
    validate_registry_publication_authorization,
)
from schemabridge.domain.registry_publication_jobs import (
    RegistryPublicationJob,
    RegistryPublicationJobStatus,
    registry_publication_job_id,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
    semantic_registry_decision_ids,
)

_ROW_NAMES = (
    "payload",
    "job_id",
    "workspace_id",
    "catalog_scope",
    "registry_id",
    "target_version",
    "proposal_id",
    "proposal_fingerprint",
    "status",
    "candidate_fingerprint",
    "authorization_id",
    "observed_authorization_id",
)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class PostgresActiveRegistryVersionReader:
    """Serve only the currently active version from an exact activation-ready job."""

    dsn: str = field(repr=False)
    pointers: ActiveRegistryPointerReadPort = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-api"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    def load_version(
        self,
        scope: SemanticRegistryScope,
        version: int,
    ) -> GovernedRegistryVersion:
        """Load only the exact version that remains active for ``scope``."""

        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RegistryControlError(
                RegistryControlErrorCode.VERSION_INVALID,
                "active registry version is invalid",
            )
        validated_scope = _validated_scope(scope)
        pointer = _load_exact_active_pointer(self.pointers, validated_scope, version)
        rows = self._load_activation_ready_rows(validated_scope, pointer)
        governed = _governed_version_from_rows(validated_scope, pointer, rows)

        # The pointer port may use another pooled connection.  Re-read after the payload so a
        # concurrent activation cannot be returned as if the older target were still active.
        observed_pointer = _load_exact_active_pointer(self.pointers, validated_scope, version)
        if observed_pointer != pointer:
            raise _version_unavailable()
        return governed

    def _load_activation_ready_rows(
        self,
        scope: SemanticRegistryScope,
        pointer: ActiveRegistryPointer,
    ) -> tuple[tuple[object, ...], ...]:
        table = self._database.table("registry_publication_jobs")
        query = sql.SQL(
            """
            SELECT payload, job_id, workspace_id, catalog_scope, registry_id,
                   target_version, proposal_id, proposal_fingerprint, status,
                   candidate_fingerprint, authorization_id, observed_authorization_id
            FROM {table}
            WHERE job_id = %s
              AND workspace_id = %s
              AND catalog_scope = %s
              AND registry_id = %s
              AND target_version = %s
              AND status = 'activation_ready'
            ORDER BY job_id
            LIMIT 2
            """
        ).format(table=table)
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    query,
                    (
                        registry_publication_job_id(scope, pointer.registry_version),
                        scope.workspace_id,
                        scope.catalog_scope,
                        scope.registry_id,
                        pointer.registry_version,
                    ),
                ).fetchall()
            return tuple(tuple(row) for row in rows)
        except Exception as error:
            raise _version_unavailable() from error


def _validated_scope(scope: SemanticRegistryScope) -> SemanticRegistryScope:
    try:
        validated = SemanticRegistryScope.model_validate(
            scope.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise RegistryControlError(
            RegistryControlErrorCode.VERSION_INVALID,
            "active registry scope is invalid",
        ) from error
    if validated != scope:
        raise RegistryControlError(
            RegistryControlErrorCode.VERSION_INVALID,
            "active registry scope is invalid",
        )
    return validated


def _load_exact_active_pointer(
    pointers: ActiveRegistryPointerReadPort,
    scope: SemanticRegistryScope,
    version: int,
) -> ActiveRegistryPointer:
    try:
        loaded = pointers.load_active(scope)
    except Exception as error:
        raise _version_unavailable() from error
    if loaded is None:
        raise _version_unavailable()
    try:
        validated = ActiveRegistryPointer.model_validate(
            loaded.model_dump(mode="python", warnings=False)
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise _version_invalid() from error
    if validated != loaded or validated.scope != scope:
        raise _version_invalid()
    if validated.registry_version != version:
        raise _version_unavailable()
    return validated


def _governed_version_from_rows(
    scope: SemanticRegistryScope,
    pointer: ActiveRegistryPointer,
    rows: Sequence[Sequence[object]],
) -> GovernedRegistryVersion:
    if len(rows) == 0:
        raise _version_unavailable()
    if len(rows) != 1 or len(rows[0]) != len(_ROW_NAMES):
        raise _version_invalid()
    values = dict(zip(_ROW_NAMES, rows[0], strict=True))
    try:
        job = RegistryPublicationJob.model_validate(values["payload"])
        candidate = _validated_model(PublishableRegistryVersion, job.candidate)
        authorization = _validated_model(RegistryPublicationAuthorization, job.authorization)
        receipt = _validated_model(PublicationReadbackReceipt, job.receipt)
        validate_registry_publication_authorization(candidate, authorization)
        expected_columns: dict[str, object] = {
            "job_id": job.id,
            "workspace_id": job.scope.workspace_id,
            "catalog_scope": job.scope.catalog_scope,
            "registry_id": job.scope.registry_id,
            "target_version": job.proposal.target_registry_version,
            "proposal_id": job.proposal.id,
            "proposal_fingerprint": job.proposal.fingerprint,
            "status": job.status.value,
            "candidate_fingerprint": candidate.fingerprint,
            "authorization_id": authorization.id,
            "observed_authorization_id": receipt.observed_authorization_id,
        }
        registry = candidate.registry
        if (
            any(values[name] != expected for name, expected in expected_columns.items())
            or job.status is not RegistryPublicationJobStatus.ACTIVATION_READY
            or job.scope != scope
            or candidate.scope != scope
            or authorization.scope != scope
            or receipt.scope != scope
            or registry.format_version != 2
            or registry.version != pointer.registry_version
            or registry.fingerprint != pointer.registry_fingerprint
            or candidate.target != pointer.registry_target
            or authorization.target != pointer.registry_target
            or receipt.target != pointer.registry_target
            or receipt.registry_version != pointer.registry_version
            or receipt.registry_fingerprint != pointer.registry_fingerprint
            or receipt.observed_authorization_id != authorization.id
            or candidate.active_decision_ids != pointer.decision_ids
            or semantic_registry_decision_ids(registry) != pointer.decision_ids
        ):
            raise ValueError("active publication payload differs from its pointer")
        governed = GovernedRegistryVersion(
            snapshot=ScopedSemanticRegistrySnapshot(
                scope=scope,
                registry=registry,
                activation_generation=pointer.generation,
                active_pointer_fingerprint=registry_projection_fingerprint(pointer),
            ),
            publication_approval_id=receipt.observed_authorization_id,
            trust=RegistryVersionTrust.STRICT,
        )
        validated = GovernedRegistryVersion.model_validate(
            governed.model_dump(mode="python", warnings=False)
        )
    except (ValidationError, AttributeError, TypeError, ValueError) as error:
        raise _version_invalid() from error
    if validated != governed:
        raise _version_invalid()
    return validated


def _validated_model(model: type[_ModelT], value: BaseModel | None) -> _ModelT:
    if value is None:
        raise ValueError("activation-ready publication evidence is incomplete")
    validated = model.model_validate(value.model_dump(mode="python", warnings=False))
    if validated != value:
        raise ValueError("activation-ready publication evidence is invalid")
    return validated


def _version_unavailable() -> RegistryControlError:
    return RegistryControlError(
        RegistryControlErrorCode.VERSION_UNAVAILABLE,
        "active registry version is unavailable",
    )


def _version_invalid() -> RegistryControlError:
    return RegistryControlError(
        RegistryControlErrorCode.VERSION_INVALID,
        "active registry version failed validation",
    )


__all__ = ["PostgresActiveRegistryVersionReader"]
