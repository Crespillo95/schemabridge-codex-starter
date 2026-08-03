"""Bind one external M30 control policy to an authenticated manifest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from schemabridge.application.ports.production_evidence import (
    M30CampaignManifestPort,
    M30ClockPort,
    M30ControlPolicyPort,
    ProductionEvidenceError,
    ProductionEvidenceErrorCode,
)
from schemabridge.application.production_campaign import AuthenticateM30CampaignManifest
from schemabridge.domain.production_campaign import M30ManifestAuthenticationState
from schemabridge.domain.production_campaign_receipts import (
    M30ControlPolicyBlockReason,
    M30ControlPolicyValidationReport,
    M30ControlPolicyValidationState,
    control_policy_matches_manifest,
)
from schemabridge.domain.production_readiness import M30ReleaseDecision


@dataclass(frozen=True, slots=True)
class ValidateM30ControlPolicy:
    """Validate policy binding without authenticating or adjudicating a control receipt."""

    manifest_authenticator: AuthenticateM30CampaignManifest
    manifest_loader: M30CampaignManifestPort
    policy_loader: M30ControlPolicyPort
    clock: M30ClockPort

    def execute(self) -> M30ControlPolicyValidationReport:
        authentication = self.manifest_authenticator.execute()
        authentication_sha256 = authentication.fingerprint()
        if authentication.state is not M30ManifestAuthenticationState.AUTHENTICATED:
            observed_at = _clock_now(self.clock)
            return _blocked_report(
                reasons=(M30ControlPolicyBlockReason.MANIFEST_NOT_AUTHENTICATED,),
                campaign_id=authentication.campaign_id,
                manifest_sha256=authentication.manifest_sha256,
                manifest_authentication_sha256=authentication_sha256,
                control_policy_id=authentication.control_policy_id,
                control_policy_sha256=authentication.control_policy_sha256,
                observed_at=observed_at,
            )
        loaded_manifest = self.manifest_loader.load()
        manifest = loaded_manifest.manifest
        if (
            loaded_manifest.raw_sha256 != authentication.manifest_sha256
            or manifest.campaign_id != authentication.campaign_id
            or manifest.control_policy_id != authentication.control_policy_id
            or manifest.control_policy_sha256 != authentication.control_policy_sha256
        ):
            observed_at = _clock_now(self.clock)
            return _blocked_report(
                reasons=(M30ControlPolicyBlockReason.MANIFEST_CHANGED,),
                campaign_id=authentication.campaign_id,
                manifest_sha256=authentication.manifest_sha256,
                manifest_authentication_sha256=authentication_sha256,
                control_policy_id=authentication.control_policy_id,
                control_policy_sha256=authentication.control_policy_sha256,
                observed_at=observed_at,
            )
        loaded_policy = self.policy_loader.load()
        policy = loaded_policy.policy
        observed_at = _clock_now(self.clock)
        reasons: list[M30ControlPolicyBlockReason] = []
        if loaded_policy.raw_sha256 != policy.fingerprint() or not control_policy_matches_manifest(
            policy, manifest
        ):
            reasons.append(M30ControlPolicyBlockReason.POLICY_SUBJECT_MISMATCH)
        if observed_at < policy.not_before:
            reasons.append(M30ControlPolicyBlockReason.POLICY_NOT_YET_VALID)
        if observed_at >= policy.expires_at:
            reasons.append(M30ControlPolicyBlockReason.POLICY_EXPIRED)
        if observed_at < authentication.completed_at:
            reasons.append(M30ControlPolicyBlockReason.CLOCK_ROLLBACK)
        if reasons:
            return _blocked_report(
                reasons=tuple(sorted(set(reasons), key=lambda item: item.value)),
                campaign_id=manifest.campaign_id,
                manifest_sha256=loaded_manifest.raw_sha256,
                manifest_authentication_sha256=authentication_sha256,
                control_policy_id=manifest.control_policy_id,
                control_policy_sha256=manifest.control_policy_sha256,
                observed_at=observed_at,
                policy_not_before=policy.not_before,
                policy_expires_at=policy.expires_at,
            )
        return M30ControlPolicyValidationReport(
            schema_version=2,
            milestone="M30",
            phase="1b-policy",
            state=M30ControlPolicyValidationState.VALIDATED,
            blocking_reasons=(),
            campaign_id=manifest.campaign_id,
            manifest_sha256=loaded_manifest.raw_sha256,
            manifest_authentication_sha256=authentication_sha256,
            control_policy_id=policy.policy_id,
            control_policy_sha256=loaded_policy.raw_sha256,
            observed_at=observed_at,
            policy_not_before=policy.not_before,
            policy_expires_at=policy.expires_at,
            policy_bound_to_authenticated_manifest=True,
            control_dag_validated=True,
            external_policy_trust_authenticated=False,
            receipt_authentication_enabled=False,
            implemented_adjudicators=1,
            admitted_unadjudicated_controls=23,
            external_controls_passed=0,
            external_controls_remaining=24,
            campaign_executable=False,
            release_decision=M30ReleaseDecision.NO_GO,
            production_release_authorized=False,
            provider_calls=0,
            source_reads=0,
            source_writes=0,
            target_calls=0,
            corpus_reads=0,
            datahub_writes=0,
        )


def _clock_now(clock: M30ClockPort) -> datetime:
    try:
        observed = clock.now()
        if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
            raise ValueError("clock did not return timezone-aware UTC")
        return observed.astimezone(UTC)
    except (AttributeError, TypeError, ValueError) as error:
        raise ProductionEvidenceError(
            ProductionEvidenceErrorCode.CONTROL_POLICY_INVALID,
            "M30 control-policy clock is invalid",
        ) from error


def _blocked_report(
    *,
    reasons: tuple[M30ControlPolicyBlockReason, ...],
    campaign_id: str | None,
    manifest_sha256: str | None,
    manifest_authentication_sha256: str | None,
    control_policy_id: str | None,
    control_policy_sha256: str | None,
    observed_at: datetime,
    policy_not_before: datetime | None = None,
    policy_expires_at: datetime | None = None,
) -> M30ControlPolicyValidationReport:
    return M30ControlPolicyValidationReport(
        schema_version=2,
        milestone="M30",
        phase="1b-policy",
        state=M30ControlPolicyValidationState.BLOCKED,
        blocking_reasons=reasons,
        campaign_id=campaign_id,
        manifest_sha256=manifest_sha256,
        manifest_authentication_sha256=manifest_authentication_sha256,
        control_policy_id=control_policy_id,
        control_policy_sha256=control_policy_sha256,
        observed_at=observed_at,
        policy_not_before=policy_not_before,
        policy_expires_at=policy_expires_at,
        policy_bound_to_authenticated_manifest=False,
        control_dag_validated=False,
        external_policy_trust_authenticated=False,
        receipt_authentication_enabled=False,
        implemented_adjudicators=1,
        admitted_unadjudicated_controls=23,
        external_controls_passed=0,
        external_controls_remaining=24,
        campaign_executable=False,
        release_decision=M30ReleaseDecision.NO_GO,
        production_release_authorized=False,
        provider_calls=0,
        source_reads=0,
        source_writes=0,
        target_calls=0,
        corpus_reads=0,
        datahub_writes=0,
    )


__all__ = ["ValidateM30ControlPolicy"]
