"""Migrator-only PostgreSQL adapter for exact tenant external-AI policy revisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.query_studio_ai_control import (
    AiControlError,
    AiControlErrorCode,
    TenantAiPolicyOperatorSnapshot,
    TenantAiPolicyWrite,
)


@dataclass(frozen=True, slots=True)
class PostgresTenantAiPolicyOperator:
    """Use only the reviewed migrator functions and expose no policy table access."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-migrator"
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

    def inspect(self, workspace_id: str) -> TenantAiPolicyOperatorSnapshot | None:
        _validate_workspace(workspace_id)
        statement = self._inspect_statement()
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(statement, (workspace_id,)).fetchone()
            return None if row is None else _snapshot_from_row(row)
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("tenant AI policy inspection response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "tenant AI policy inspection failed") from error

    def apply(self, change: TenantAiPolicyWrite) -> TenantAiPolicyOperatorSnapshot:
        statement = sql.SQL(
            """
            SELECT *
            FROM {}.apply_tenant_ai_policy(
                %s::varchar, %s::bigint, %s::boolean, %s::boolean, %s,
                %s::varchar, %s::varchar, %s, %s, %s::integer,
                %s::bigint, %s::bigint, %s::integer, %s::integer,
                %s::integer, %s::varchar, %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))
        inspect = self._inspect_statement()
        try:
            with self._database.connect() as connection, connection.transaction():
                applied = connection.execute(
                    statement,
                    (
                        change.workspace_id,
                        change.expected_version,
                        change.external_ai_enabled,
                        change.provider_governance_accepted,
                        change.provider_governance_fingerprint,
                        change.model_snapshot,
                        change.endpoint_region,
                        change.endpoint_origin_fingerprint,
                        change.configuration_fingerprint,
                        change.requests_per_minute,
                        change.daily_input_token_limit,
                        change.daily_output_token_limit,
                        change.concurrent_attempt_limit,
                        change.reservation_lease_seconds,
                        change.audit_retention_seconds,
                        change.updated_by,
                        change.confirmation.value,
                    ),
                ).fetchone()
                if applied is None:
                    raise ValueError("tenant AI policy apply returned no outcome")
                row = connection.execute(inspect, (change.workspace_id,)).fetchone()
            if row is None:
                raise ValueError("tenant AI policy apply read-back is absent")
            return _snapshot_from_row(row)
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("tenant AI policy apply response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "tenant AI policy apply failed") from error

    def _inspect_statement(self) -> sql.Composed:
        return sql.SQL("SELECT * FROM {}.inspect_tenant_ai_policy(%s::varchar)").format(
            sql.Identifier(self.schema)
        )


def _snapshot_from_row(row: Sequence[Any]) -> TenantAiPolicyOperatorSnapshot:
    if len(row) != 18:
        raise ValueError("tenant AI policy operator row has an invalid shape")
    accepted_at = row[5]
    updated_at = row[17]
    if accepted_at is not None and not isinstance(accepted_at, datetime):
        raise ValueError("tenant AI policy acceptance time is invalid")
    if not isinstance(updated_at, datetime):
        raise ValueError("tenant AI policy update time is invalid")
    return TenantAiPolicyOperatorSnapshot(
        workspace_id=str(row[0]),
        version=int(row[1]),
        external_ai_enabled=_strict_bool(row[2]),
        provider_governance_accepted=_strict_bool(row[3]),
        provider_governance_fingerprint=None if row[4] is None else str(row[4]),
        provider_governance_accepted_at=accepted_at,
        model_snapshot=str(row[6]),
        endpoint_region=str(row[7]),
        endpoint_origin_fingerprint=str(row[8]),
        configuration_fingerprint=str(row[9]),
        requests_per_minute=int(row[10]),
        daily_input_token_limit=int(row[11]),
        daily_output_token_limit=int(row[12]),
        concurrent_attempt_limit=int(row[13]),
        reservation_lease_seconds=int(row[14]),
        audit_retention_seconds=int(row[15]),
        updated_by=str(row[16]),
        updated_at=updated_at,
    )


def _validate_workspace(workspace_id: str) -> None:
    if (
        not isinstance(workspace_id, str)
        or not 3 <= len(workspace_id) <= 200
        or len(workspace_id.encode("utf-8")) > 200
        or workspace_id[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in workspace_id
        )
    ):
        raise ValueError("tenant AI policy workspace is invalid")


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("tenant AI policy boolean is invalid")
    return value


def _invalid_response(message: str) -> AiControlError:
    return AiControlError(AiControlErrorCode.INVALID_RESPONSE, message)


def _database_error(error: psycopg.Error, message: str) -> AiControlError:
    code = {
        "40001": AiControlErrorCode.IDEMPOTENCY_CONFLICT,
        "22023": AiControlErrorCode.INVALID_RESPONSE,
        "22003": AiControlErrorCode.INVALID_RESPONSE,
        "42501": AiControlErrorCode.RESOURCE_UNAVAILABLE,
    }.get(error.sqlstate or "", AiControlErrorCode.RESOURCE_UNAVAILABLE)
    return AiControlError(code, message)


__all__ = ["PostgresTenantAiPolicyOperator"]
