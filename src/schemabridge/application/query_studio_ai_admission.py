"""Durable per-attempt admission and accounting for live Query Studio AI calls."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from schemabridge.application.ports.query_studio import (
    DescriptionExpansionPort,
    DescriptionExpansionPreflightPort,
    QueryStudioIntentPort,
    QueryStudioNoncePort,
    QueryStudioPortError,
    QueryStudioPortErrorCode,
    QueryStudioRetryDisposition,
)
from schemabridge.application.ports.query_studio_ai_control import (
    AiAdmissionOutcome,
    AiAttemptReservation,
    AiAttemptReservationRequest,
    AiAttemptSettlement,
    AiAttemptSettlementRequest,
    AiAttemptStatus,
    AiControlError,
    AiProviderAttemptControlPort,
    AiSettlementOutcome,
)
from schemabridge.domain.query_studio import (
    DescriptionExpansionInput,
    DescriptionExpansionResult,
    ProviderConfigurationFacts,
    ProviderOutcomeCode,
    ProviderOutputFailureCategory,
    ProviderStage,
    ProviderUsageFacts,
    QueryStudioInterpretationInput,
    QueryStudioInterpretationResult,
    query_studio_fingerprint,
)

_ResultT = TypeVar(
    "_ResultT",
    DescriptionExpansionResult,
    QueryStudioInterpretationResult,
)
_InputT = TypeVar(
    "_InputT",
    DescriptionExpansionInput,
    QueryStudioInterpretationInput,
)


@dataclass(frozen=True, slots=True)
class _AdmissionContext:
    control: AiProviderAttemptControlPort
    nonces: QueryStudioNoncePort
    workspace_id: str
    actor_digest: str
    semantic_scope_fingerprint: str
    configuration: ProviderConfigurationFacts
    estimated_input_tokens: int
    estimated_output_tokens: int

    def __post_init__(self) -> None:
        if not self.configuration.external_ai:
            raise ValueError("durable AI admission requires an external-AI configuration")
        for value, label in (
            (self.actor_digest, "actor"),
            (self.semantic_scope_fingerprint, "semantic scope"),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"AI admission {label} fingerprint is invalid")
        if not self.workspace_id or len(self.workspace_id.encode("utf-8")) > 200:
            raise ValueError("AI admission workspace is invalid")
        if not 1 <= self.estimated_input_tokens <= 1_000_000:
            raise ValueError("AI admission input estimate is invalid")
        if not 1 <= self.estimated_output_tokens <= 1_000_000:
            raise ValueError("AI admission output estimate is invalid")


@dataclass(frozen=True, slots=True)
class AdmittedDescriptionExpansion:
    """Historical expansion admission retained only for evidence compatibility.

    Production composition uses local expansion and exposes only
    ``AdmittedQueryStudioIntent`` as a supported live admission surface.
    """

    delegate: DescriptionExpansionPort
    preflight: DescriptionExpansionPreflightPort
    control: AiProviderAttemptControlPort
    nonces: QueryStudioNoncePort
    workspace_id: str
    actor_digest: str
    semantic_scope_fingerprint: str
    configuration: ProviderConfigurationFacts
    estimated_input_tokens: int
    estimated_output_tokens: int

    def expand(self, value: DescriptionExpansionInput) -> DescriptionExpansionResult:
        local_expansion = self.preflight.expand_if_local(value)
        if local_expansion is not None:
            return DescriptionExpansionResult(
                expansion=local_expansion,
                usage=None,
            )
        context = self._context()
        return _run_admitted(
            context=context,
            stage=ProviderStage.EXPANSION,
            value=value,
            invoke=self.delegate.expand,
            usage=lambda result: _require_provider_usage(result.usage),
        )

    def _context(self) -> _AdmissionContext:
        return _AdmissionContext(
            control=self.control,
            nonces=self.nonces,
            workspace_id=self.workspace_id,
            actor_digest=self.actor_digest,
            semantic_scope_fingerprint=self.semantic_scope_fingerprint,
            configuration=self.configuration,
            estimated_input_tokens=self.estimated_input_tokens,
            estimated_output_tokens=self.estimated_output_tokens,
        )


@dataclass(frozen=True, slots=True)
class AdmittedQueryStudioIntent:
    """Wrap one live typed-intent port with two independently audited attempts."""

    delegate: QueryStudioIntentPort
    control: AiProviderAttemptControlPort
    nonces: QueryStudioNoncePort
    workspace_id: str
    actor_digest: str
    semantic_scope_fingerprint: str
    configuration: ProviderConfigurationFacts
    estimated_input_tokens: int
    estimated_output_tokens: int

    def interpret(
        self,
        value: QueryStudioInterpretationInput,
    ) -> QueryStudioInterpretationResult:
        context = self._context()
        return _run_admitted(
            context=context,
            stage=ProviderStage.INTERPRETATION,
            value=value,
            invoke=self.delegate.interpret,
            usage=lambda result: result.usage,
        )

    def _context(self) -> _AdmissionContext:
        return _AdmissionContext(
            control=self.control,
            nonces=self.nonces,
            workspace_id=self.workspace_id,
            actor_digest=self.actor_digest,
            semantic_scope_fingerprint=self.semantic_scope_fingerprint,
            configuration=self.configuration,
            estimated_input_tokens=self.estimated_input_tokens,
            estimated_output_tokens=self.estimated_output_tokens,
        )


def _run_admitted(
    *,
    context: _AdmissionContext,
    stage: ProviderStage,
    value: _InputT,
    invoke: Callable[[_InputT], _ResultT],
    usage: Callable[[_ResultT], ProviderUsageFacts],
) -> _ResultT:
    request_fingerprint = query_studio_fingerprint(value.model_dump(mode="json"))
    semantic_payload_fingerprint = _semantic_payload_fingerprint(value)
    request_id = _safe_request_id(context.nonces.new_nonce())
    final_error: QueryStudioPortError | None = None

    for attempt_number in (1, 2):
        capability = context.nonces.new_nonce()
        reservation = _reserve(
            context=context,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            semantic_payload_fingerprint=semantic_payload_fingerprint,
            estimated_input_tokens=context.estimated_input_tokens,
            stage=stage,
            attempt_number=attempt_number,
            capability=capability,
        )
        denied = _denied_error(reservation)
        if denied is not None:
            raise denied
        if (
            reservation.reservation_id is None
            or reservation.fencing_token is None
            or reservation.status is not AiAttemptStatus.RESERVED
        ):
            raise _provider_unavailable()
        if not _reservation_matches_configuration(reservation, context.configuration):
            _settle(
                context=context,
                reservation=reservation,
                capability=capability,
                outcome=AiSettlementOutcome.INVALID_OUTPUT,
                provider_usage=None,
                duration_ms=0,
            )
            raise _provider_unavailable()

        try:
            result = invoke(value)
            provider_usage = usage(result)
            _validate_provider_usage(
                provider_usage,
                context=context,
                stage=stage,
            )
        except QueryStudioPortError as error:
            final_error = error
            _settle(
                context=context,
                reservation=reservation,
                capability=capability,
                outcome=_settlement_outcome(error.code),
                provider_usage=None,
                duration_ms=0,
            )
            if attempt_number == 1 and (
                error.code in _TRANSIENT_PROVIDER_ERRORS
                or error.retry_disposition is QueryStudioRetryDisposition.RETRY_ONCE
            ):
                continue
            raise
        except (TypeError, ValueError):
            _settle(
                context=context,
                reservation=reservation,
                capability=capability,
                outcome=AiSettlementOutcome.INVALID_OUTPUT,
                provider_usage=None,
                duration_ms=0,
            )
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT,
                "the admitted AI adapter returned an invalid result",
                output_failure_category=(ProviderOutputFailureCategory.SCHEMA_VALIDATION),
            ) from None
        _settle(
            context=context,
            reservation=reservation,
            capability=capability,
            outcome=AiSettlementOutcome.SUCCEEDED,
            provider_usage=provider_usage,
            duration_ms=provider_usage.duration_ms,
        )
        return result

    if final_error is not None:
        raise final_error
    raise _provider_unavailable()


def _reserve(
    *,
    context: _AdmissionContext,
    request_id: str,
    request_fingerprint: str,
    semantic_payload_fingerprint: str,
    estimated_input_tokens: int,
    stage: ProviderStage,
    attempt_number: int,
    capability: str,
) -> AiAttemptReservation:
    idempotency_digest = query_studio_fingerprint(
        {
            "version": 1,
            "workspace_id": context.workspace_id,
            "request_id": request_id,
            "stage": stage.value,
            "attempt_number": attempt_number,
            "request_fingerprint": request_fingerprint,
        }
    )
    try:
        return context.control.reserve(
            AiAttemptReservationRequest(
                workspace_id=context.workspace_id,
                request_id=request_id,
                actor_digest=context.actor_digest,
                stage=stage,
                attempt_number=attempt_number,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
                semantic_scope_fingerprint=context.semantic_scope_fingerprint,
                semantic_payload_fingerprint=semantic_payload_fingerprint,
                configuration_fingerprint=context.configuration.fingerprint,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=context.estimated_output_tokens,
                lease_capability=capability,
            )
        )
    except AiControlError:
        raise _provider_unavailable() from None


def _settle(
    *,
    context: _AdmissionContext,
    reservation: AiAttemptReservation,
    capability: str,
    outcome: AiSettlementOutcome,
    provider_usage: ProviderUsageFacts | None,
    duration_ms: int,
) -> None:
    if reservation.reservation_id is None or reservation.fencing_token is None:
        raise _provider_unavailable()
    request = AiAttemptSettlementRequest(
        workspace_id=context.workspace_id,
        reservation_id=reservation.reservation_id,
        fencing_token=reservation.fencing_token,
        outcome=outcome,
        reserved_input_tokens=context.estimated_input_tokens,
        reserved_output_tokens=context.estimated_output_tokens,
        observed_input_tokens=(None if provider_usage is None else provider_usage.input_tokens),
        observed_output_tokens=(None if provider_usage is None else provider_usage.output_tokens),
        duration_ms=duration_ms,
        lease_capability=capability,
    )
    try:
        settlement = context.control.settle(request)
        _validate_exact_settlement(settlement, request)
    except (AiControlError, TypeError, ValueError):
        raise _provider_unavailable() from None


def _validate_exact_settlement(
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
        not isinstance(settlement, AiAttemptSettlement)
        or settlement.reservation_id != request.reservation_id
        or settlement.replayed is not False
        or settlement.status is not AiAttemptStatus.SETTLED
        or settlement.outcome is not request.outcome
        or settlement.charged_input_tokens != expected_input
        or settlement.charged_output_tokens != expected_output
        or not isinstance(settlement.settled_at, datetime)
        or settlement.settled_at.utcoffset() is None
        or len(settlement.audit_id) != 68
        or not settlement.audit_id.startswith("aia_")
        or any(character not in "0123456789abcdef" for character in settlement.audit_id[4:])
    ):
        raise ValueError("AI settlement acknowledgement does not match its exact request")


def _semantic_payload_fingerprint(
    value: DescriptionExpansionInput | QueryStudioInterpretationInput,
) -> str:
    if isinstance(value, QueryStudioInterpretationInput):
        return value.vocabulary.fingerprint
    return value.text.fingerprint


def _require_provider_usage(value: ProviderUsageFacts | None) -> ProviderUsageFacts:
    if value is None:
        raise ValueError("provider delegate returned local-only expansion usage")
    return value


def _validate_provider_usage(
    value: ProviderUsageFacts,
    *,
    context: _AdmissionContext,
    stage: ProviderStage,
) -> None:
    if (
        value.stage is not stage
        or value.model_snapshot != context.configuration.model_snapshot
        or value.configuration_fingerprint != context.configuration.fingerprint
        or value.outcome is not ProviderOutcomeCode.SUCCEEDED
        or value.input_tokens <= 0
        or value.output_tokens <= 0
        or value.input_tokens > context.estimated_input_tokens
        or value.output_tokens > context.estimated_output_tokens
    ):
        raise ValueError("provider usage does not match the admitted reservation")


def _safe_request_id(nonce: str) -> str:
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    return f"airq_{digest[:48]}"


def _reservation_matches_configuration(
    reservation: AiAttemptReservation,
    configuration: ProviderConfigurationFacts,
) -> bool:
    return (
        reservation.configuration_fingerprint == configuration.fingerprint
        and reservation.model_snapshot == configuration.model_snapshot
        and reservation.endpoint_region == configuration.endpoint_region
    )


def _denied_error(
    reservation: AiAttemptReservation,
) -> QueryStudioPortError | None:
    if reservation.outcome in {
        AiAdmissionOutcome.RESERVED,
        AiAdmissionOutcome.REPLAYED,
    }:
        return None
    code = {
        AiAdmissionOutcome.RATE_LIMITED: QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED,
        AiAdmissionOutcome.CONCURRENCY_LIMITED: (QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED),
        AiAdmissionOutcome.QUOTA_EXHAUSTED: (QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED),
        AiAdmissionOutcome.POLICY_DISABLED: (QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE),
        AiAdmissionOutcome.POLICY_MISMATCH: (QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE),
    }[reservation.outcome]
    return QueryStudioPortError(code, "the live AI request was not admitted")


def _settlement_outcome(code: QueryStudioPortErrorCode) -> AiSettlementOutcome:
    return {
        QueryStudioPortErrorCode.PROVIDER_TIMEOUT: AiSettlementOutcome.TIMEOUT,
        QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED: AiSettlementOutcome.RATE_LIMITED,
        QueryStudioPortErrorCode.PROVIDER_QUOTA_EXHAUSTED: (AiSettlementOutcome.RATE_LIMITED),
        QueryStudioPortErrorCode.PROVIDER_REFUSED: AiSettlementOutcome.REFUSAL,
        QueryStudioPortErrorCode.PROVIDER_MISSING_OUTPUT: (AiSettlementOutcome.MISSING_OUTPUT),
        QueryStudioPortErrorCode.PROVIDER_INVALID_OUTPUT: (AiSettlementOutcome.INVALID_OUTPUT),
        QueryStudioPortErrorCode.PROVIDER_MODEL_MISMATCH: (AiSettlementOutcome.INVALID_OUTPUT),
        QueryStudioPortErrorCode.SENSITIVE_INPUT_BLOCKED: (AiSettlementOutcome.INVALID_OUTPUT),
    }.get(code, AiSettlementOutcome.PROVIDER_UNAVAILABLE)


def _provider_unavailable() -> QueryStudioPortError:
    return QueryStudioPortError(
        QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
        "the durable live AI control plane is unavailable",
    )


_TRANSIENT_PROVIDER_ERRORS = frozenset(
    {
        QueryStudioPortErrorCode.PROVIDER_TIMEOUT,
        QueryStudioPortErrorCode.PROVIDER_RATE_LIMITED,
        QueryStudioPortErrorCode.PROVIDER_UNAVAILABLE,
    }
)


__all__ = [
    "AdmittedQueryStudioIntent",
]
