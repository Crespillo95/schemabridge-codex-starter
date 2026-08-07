"""Least-privilege PostgreSQL adapter for Query Studio AI policy and admission."""

from __future__ import annotations

import re
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
    AiAdmissionOutcome,
    AiAttemptReservation,
    AiAttemptReservationRequest,
    AiAttemptSettlement,
    AiAttemptSettlementRequest,
    AiAttemptStatus,
    AiControlError,
    AiControlErrorCode,
    AiSettlementOutcome,
    TenantAiPolicySnapshot,
)
from schemabridge.domain.query_studio import ProviderStage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,199}$")
_RESERVATION_ID = re.compile(r"^air_[0-9a-f]{64}$")
_AUDIT_ID = re.compile(r"^aia_[0-9a-f]{64}$")
_MODELS = {
    "gpt-5-nano-2025-08-07",
    "gpt-5.4-nano-2026-03-17",
    "gpt-5.6-luna",
}
_REGIONS = {"global", "eu", "us"}


@dataclass(frozen=True, slots=True)
class PostgresQueryStudioAiControl:
    """Invoke only reviewed SECURITY DEFINER functions; never read control tables."""

    dsn: str = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
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

    def load_policy(self, workspace_id: str) -> TenantAiPolicySnapshot | None:
        _safe_id(workspace_id, "workspace")
        statement = sql.SQL("SELECT * FROM {}.load_tenant_ai_policy(%s::varchar)").format(
            sql.Identifier(self.schema)
        )
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(statement, (workspace_id,)).fetchone()
            return None if row is None else _policy_from_row(row)
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("tenant AI policy response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "tenant AI policy read failed") from error

    def reserve(self, request: AiAttemptReservationRequest) -> AiAttemptReservation:
        _validate_reservation_request(request)
        statement = sql.SQL(
            """
            SELECT *
            FROM {}.reserve_ai_provider_attempt(
                %s::varchar, %s::varchar, %s, %s::varchar,
                %s::smallint, %s, %s, %s,
                %s, %s, %s::integer, %s::integer,
                %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(
                    statement,
                    (
                        request.workspace_id,
                        request.request_id,
                        request.actor_digest,
                        request.stage.value,
                        request.attempt_number,
                        request.idempotency_digest,
                        request.request_fingerprint,
                        request.semantic_scope_fingerprint,
                        request.semantic_payload_fingerprint,
                        request.configuration_fingerprint,
                        request.estimated_input_tokens,
                        request.estimated_output_tokens,
                        request.lease_capability,
                    ),
                ).fetchone()
            if row is None:
                raise ValueError("AI admission returned no outcome")
            return _reservation_from_row(row)
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("AI admission response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "AI admission failed") from error

    def settle(self, request: AiAttemptSettlementRequest) -> AiAttemptSettlement:
        _validate_settlement_request(request)
        statement = sql.SQL(
            """
            SELECT *
            FROM {}.settle_ai_provider_attempt(
                %s::varchar, %s::varchar, %s::varchar, %s::bigint,
                %s::varchar, %s::integer, %s::integer, %s::integer
            )
            """
        ).format(sql.Identifier(self.schema))
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(
                    statement,
                    (
                        request.workspace_id,
                        request.reservation_id,
                        request.lease_capability,
                        request.fencing_token,
                        request.outcome.value,
                        request.observed_input_tokens,
                        request.observed_output_tokens,
                        request.duration_ms,
                    ),
                ).fetchone()
            if row is None:
                raise ValueError("AI settlement returned no outcome")
            settlement = _settlement_from_row(row)
            _validate_settlement_response(settlement, request)
            return settlement
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("AI settlement response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "AI settlement failed") from error

    def expire(self, workspace_id: str, *, limit: int = 100) -> int:
        _safe_id(workspace_id, "workspace")
        if not 1 <= limit <= 1_000:
            raise ValueError("AI expiry limit is invalid")
        statement = sql.SQL(
            """
            SELECT {}.expire_ai_provider_attempts(
                %s::varchar, %s::integer
            )
            """
        ).format(sql.Identifier(self.schema))
        try:
            with self._database.connect() as connection, connection.transaction():
                row = connection.execute(statement, (workspace_id, limit)).fetchone()
            if row is None or len(row) != 1 or not 0 <= int(row[0]) <= limit:
                raise ValueError("AI expiry response is invalid")
            return int(row[0])
        except AiControlError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise _invalid_response("AI expiry response is invalid") from error
        except psycopg.Error as error:
            raise _database_error(error, "AI reservation expiry failed") from error


def _policy_from_row(row: Sequence[Any]) -> TenantAiPolicySnapshot:
    if len(row) != 13:
        raise ValueError("tenant AI policy row has an invalid shape")
    model = str(row[3])
    region = str(row[4])
    fingerprint = str(row[5])
    updated_at = row[12]
    if (
        model not in _MODELS
        or region not in _REGIONS
        or _SHA256.fullmatch(fingerprint) is None
        or not isinstance(updated_at, datetime)
    ):
        raise ValueError("tenant AI policy row contains invalid facts")
    result = TenantAiPolicySnapshot(
        version=int(row[0]),
        external_ai_enabled=_strict_bool(row[1]),
        provider_governance_accepted=_strict_bool(row[2]),
        model_snapshot=model,
        endpoint_region=region,
        configuration_fingerprint=fingerprint,
        requests_per_minute=int(row[6]),
        daily_input_token_limit=int(row[7]),
        daily_output_token_limit=int(row[8]),
        concurrent_attempt_limit=int(row[9]),
        reservation_lease_seconds=int(row[10]),
        audit_retention_seconds=int(row[11]),
        updated_at=updated_at,
    )
    if (
        result.version < 1
        or not 1 <= result.requests_per_minute <= 10_000
        or not 1 <= result.daily_input_token_limit <= 1_000_000_000
        or not 1 <= result.daily_output_token_limit <= 1_000_000_000
        or not 1 <= result.concurrent_attempt_limit <= 1_000
        or not 10 <= result.reservation_lease_seconds <= 300
        or not 2_592_000 <= result.audit_retention_seconds <= 315_360_000
        or (result.external_ai_enabled and not result.provider_governance_accepted)
    ):
        raise ValueError("tenant AI policy row violates its bounded contract")
    return result


def _reservation_from_row(row: Sequence[Any]) -> AiAttemptReservation:
    if len(row) != 10:
        raise ValueError("AI admission row has an invalid shape")
    outcome = AiAdmissionOutcome(str(row[0]))
    replayed = _strict_bool(row[1])
    reservation_id = None if row[2] is None else str(row[2])
    status = None if row[3] is None else AiAttemptStatus(str(row[3]))
    fence = None if row[4] is None else int(row[4])
    lease_expires_at = row[5]
    policy_version = int(row[6])
    model = None if row[7] is None else str(row[7])
    region = None if row[8] is None else str(row[8])
    fingerprint = None if row[9] is None else str(row[9])
    admitted = outcome in {AiAdmissionOutcome.RESERVED, AiAdmissionOutcome.REPLAYED}
    if admitted:
        if (
            _RESERVATION_ID.fullmatch(reservation_id or "") is None
            or status is None
            or fence is None
            or fence < 1
            or ((status is AiAttemptStatus.RESERVED) != isinstance(lease_expires_at, datetime))
            or (outcome is AiAdmissionOutcome.RESERVED and status is not AiAttemptStatus.RESERVED)
            or model not in _MODELS
            or region not in _REGIONS
            or _SHA256.fullmatch(fingerprint or "") is None
            or policy_version < 1
            or replayed is not (outcome is AiAdmissionOutcome.REPLAYED)
        ):
            raise ValueError("admitted AI reservation is incomplete")
    elif any(value is not None for value in (reservation_id, status, fence, lease_expires_at)):
        raise ValueError("denied AI admission exposed reservation state")
    elif replayed:
        raise ValueError("denied AI admission cannot be a replay")
    elif (
        policy_version < 0
        or (model is not None and model not in _MODELS)
        or (region is not None and region not in _REGIONS)
        or (fingerprint is not None and _SHA256.fullmatch(fingerprint) is None)
    ):
        raise ValueError("denied AI admission returned invalid policy facts")
    return AiAttemptReservation(
        outcome=outcome,
        replayed=replayed,
        reservation_id=reservation_id,
        status=status,
        fencing_token=fence,
        lease_expires_at=lease_expires_at,
        policy_version=policy_version,
        model_snapshot=model,
        endpoint_region=region,
        configuration_fingerprint=fingerprint,
    )


def _settlement_from_row(row: Sequence[Any]) -> AiAttemptSettlement:
    if len(row) != 8:
        raise ValueError("AI settlement row has an invalid shape")
    reservation_id = str(row[0])
    status = AiAttemptStatus(str(row[2]))
    outcome = AiSettlementOutcome(str(row[3]))
    settled_at = row[6]
    audit_id = str(row[7])
    if (
        _RESERVATION_ID.fullmatch(reservation_id) is None
        or status is not AiAttemptStatus.SETTLED
        or outcome is AiSettlementOutcome.EXPIRED_CRASH
        or not isinstance(settled_at, datetime)
        or _AUDIT_ID.fullmatch(audit_id) is None
        or not 0 <= int(row[4]) <= 1_000_000_000
        or not 0 <= int(row[5]) <= 1_000_000_000
    ):
        raise ValueError("AI settlement row contains invalid facts")
    return AiAttemptSettlement(
        reservation_id=reservation_id,
        replayed=_strict_bool(row[1]),
        status=status,
        outcome=outcome,
        charged_input_tokens=int(row[4]),
        charged_output_tokens=int(row[5]),
        settled_at=settled_at,
        audit_id=audit_id,
    )


def _validate_reservation_request(request: AiAttemptReservationRequest) -> None:
    _safe_id(request.workspace_id, "workspace")
    _safe_id(request.request_id, "request")
    _digest(request.actor_digest, "actor")
    _digest(request.idempotency_digest, "idempotency")
    _digest(request.request_fingerprint, "request fingerprint")
    _digest(request.semantic_scope_fingerprint, "semantic scope")
    _digest(request.semantic_payload_fingerprint, "semantic payload")
    _digest(request.configuration_fingerprint, "configuration")
    if (
        not isinstance(request.stage, ProviderStage)
        or type(request.attempt_number) is not int
        or not 1 <= request.attempt_number <= 2
        or not 1 <= request.estimated_input_tokens <= 1_000_000
        or not 1 <= request.estimated_output_tokens <= 1_000_000
    ):
        raise ValueError("AI reservation bounds are invalid")
    _capability(request.lease_capability)


def _validate_settlement_request(request: AiAttemptSettlementRequest) -> None:
    _safe_id(request.workspace_id, "workspace")
    if _RESERVATION_ID.fullmatch(request.reservation_id) is None:
        raise ValueError("AI reservation id is invalid")
    _capability(request.lease_capability)
    if (
        not isinstance(request.outcome, AiSettlementOutcome)
        or request.fencing_token < 1
        or not 0 <= request.duration_ms <= 3_600_000
        or not 1 <= request.reserved_input_tokens <= 1_000_000
        or not 1 <= request.reserved_output_tokens <= 1_000_000
        or (request.observed_input_tokens is None) != (request.observed_output_tokens is None)
        or (
            request.outcome is AiSettlementOutcome.SUCCEEDED
            and request.observed_input_tokens is None
        )
        or (
            request.outcome is not AiSettlementOutcome.SUCCEEDED
            and request.observed_input_tokens is not None
        )
        or request.outcome is AiSettlementOutcome.EXPIRED_CRASH
    ):
        raise ValueError("AI settlement bounds are invalid")
    for value in (request.observed_input_tokens, request.observed_output_tokens):
        if value is not None and not 0 <= value <= 1_000_000_000:
            raise ValueError("AI settlement usage is invalid")
    if request.observed_input_tokens is not None and (
        request.observed_input_tokens > request.reserved_input_tokens
        or request.observed_output_tokens is None
        or request.observed_output_tokens > request.reserved_output_tokens
    ):
        raise ValueError("AI settlement usage exceeds its reservation")


def _validate_settlement_response(
    settlement: AiAttemptSettlement,
    request: AiAttemptSettlementRequest,
) -> None:
    expected_input = (
        request.observed_input_tokens
        if request.outcome is AiSettlementOutcome.SUCCEEDED
        else request.reserved_input_tokens
    )
    expected_output = (
        request.observed_output_tokens
        if request.outcome is AiSettlementOutcome.SUCCEEDED
        else request.reserved_output_tokens
    )
    if (
        settlement.reservation_id != request.reservation_id
        or settlement.replayed is not False
        or settlement.status is not AiAttemptStatus.SETTLED
        or settlement.outcome is not request.outcome
        or settlement.charged_input_tokens != expected_input
        or settlement.charged_output_tokens != expected_output
    ):
        raise ValueError("AI settlement response does not match its exact request")


def _safe_id(value: str, label: str) -> None:
    if _SAFE_ID.fullmatch(value) is None or len(value.encode()) > 200:
        raise ValueError(f"AI {label} id is invalid")


def _digest(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"AI {label} digest is invalid")


def _capability(value: str) -> None:
    if not 32 <= len(value) <= 256 or len(value.encode()) > 1_024:
        raise ValueError("AI lease capability is invalid")


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("AI response boolean is invalid")
    return value


def _invalid_response(message: str) -> AiControlError:
    return AiControlError(AiControlErrorCode.INVALID_RESPONSE, message)


def _database_error(error: psycopg.Error, message: str) -> AiControlError:
    code = {
        "23505": AiControlErrorCode.IDEMPOTENCY_CONFLICT,
        "02000": AiControlErrorCode.RESERVATION_UNAVAILABLE,
        "55000": AiControlErrorCode.LEASE_STALE,
        "22023": AiControlErrorCode.INVALID_RESPONSE,
        "22003": AiControlErrorCode.INVALID_RESPONSE,
    }.get(error.sqlstate or "", AiControlErrorCode.RESOURCE_UNAVAILABLE)
    return AiControlError(code, message)


__all__ = ["PostgresQueryStudioAiControl"]
