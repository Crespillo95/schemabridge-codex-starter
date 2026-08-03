"""Authenticate an exact M30 manifest without adjudicating campaign evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from schemabridge.application.ports.production_evidence import (
    LoadedM30CampaignManifest,
    M30CampaignContractPort,
    M30CampaignManifestAuthenticationPort,
    M30CampaignManifestPort,
    M30ClockPort,
    ProductionEvidenceError,
    ProductionEvidenceErrorCode,
)
from schemabridge.application.production_readiness import AssessM30Readiness
from schemabridge.domain.production_campaign import (
    M30_GITHUB_ATTESTATION_PREDICATE,
    M30_GITHUB_CLI_EXECUTABLE_SHA256,
    M30_GITHUB_CLI_VERSION,
    M30_GITHUB_OIDC_ISSUER,
    M30_GITHUB_REPOSITORY,
    M30_GITHUB_SIGNER_WORKFLOW,
    M30AuthenticatedManifest,
    M30CampaignBlockReason,
    M30CampaignManifest,
    M30ManifestAuthenticationReport,
    M30ManifestAuthenticationState,
    manifest_matches_contract,
)
from schemabridge.domain.production_readiness import (
    M30CampaignContract,
    M30EvidenceClass,
    M30GateStatus,
    M30ReadinessReport,
    M30ReleaseDecision,
)


@dataclass(frozen=True, slots=True)
class M30CampaignManifestValidation:
    """Safe pre-attestation result; validity never implies authenticity or execution authority."""

    preflight: M30ReadinessReport
    loaded: LoadedM30CampaignManifest | None
    blocking_reasons: tuple[M30CampaignBlockReason, ...]


@dataclass(frozen=True, slots=True)
class ValidateM30CampaignManifest:
    readiness: AssessM30Readiness
    contract_loader: M30CampaignContractPort
    manifest_loader: M30CampaignManifestPort

    def execute(self, *, verified_at: datetime) -> M30CampaignManifestValidation:
        if verified_at.tzinfo is None or verified_at.utcoffset() != timedelta(0):
            raise ValueError("M30 campaign verification time must be timezone-aware UTC")
        contract_before = self.contract_loader.load()
        preflight = self.readiness.execute()
        contract_after = self.contract_loader.load()
        if contract_before.fingerprint() != contract_after.fingerprint():
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_INVALID,
                "M30 campaign contract changed during validation",
            )
        if not repository_ready_for_campaign(preflight):
            return M30CampaignManifestValidation(
                preflight=preflight,
                loaded=None,
                blocking_reasons=(M30CampaignBlockReason.CANDIDATE_NOT_READY,),
            )
        loaded = self.manifest_loader.load()
        return M30CampaignManifestValidation(
            preflight=preflight,
            loaded=loaded,
            blocking_reasons=manifest_block_reasons(
                preflight=preflight,
                contract=contract_after,
                loaded=loaded,
                verified_at=verified_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthenticateM30CampaignManifest:
    """Authenticate frozen inputs; all 24 material controls remain unadjudicated."""

    validator: ValidateM30CampaignManifest
    authenticator: M30CampaignManifestAuthenticationPort
    clock: M30ClockPort

    def execute(self) -> M30ManifestAuthenticationReport:
        verified_at = _clock_now(self.clock)
        validation = self.validator.execute(verified_at=verified_at)
        if validation.blocking_reasons:
            return _blocked_report(
                preflight=validation.preflight,
                reasons=validation.blocking_reasons,
                campaign_id=(
                    validation.loaded.manifest.campaign_id
                    if validation.loaded is not None
                    else None
                ),
                manifest_sha256=(
                    validation.loaded.raw_sha256 if validation.loaded is not None else None
                ),
            )
        loaded = validation.loaded
        if loaded is None:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_INVALID,
                "M30 validation returned no authenticated subject",
            )
        manifest = loaded.manifest
        authentication = _revalidate_authentication(
            self.authenticator.authenticate(loaded, verified_at=verified_at)
        )
        expected_ref = f"refs/tags/v{validation.preflight.candidate.package_version}"
        if (
            authentication.manifest_sha256 != loaded.raw_sha256
            or authentication.source_repository != M30_GITHUB_REPOSITORY
            or authentication.source_revision != validation.preflight.candidate.revision
            or authentication.source_ref != expected_ref
            or authentication.signer_workflow != M30_GITHUB_SIGNER_WORKFLOW
            or authentication.signer_digest != validation.preflight.candidate.revision
            or authentication.oidc_issuer != M30_GITHUB_OIDC_ISSUER
            or authentication.predicate_type != M30_GITHUB_ATTESTATION_PREDICATE
            or authentication.github_cli_version != M30_GITHUB_CLI_VERSION
            or authentication.github_cli_executable_sha256
            != M30_GITHUB_CLI_EXECUTABLE_SHA256[authentication.github_cli_platform]
            or authentication.github_hosted_runner_required is not True
            or authentication.trusted_timestamp_verified is not True
            or authentication.verified_at != verified_at
        ):
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_AUTHENTICATION_FAILED,
                "M30 manifest authentication facts differ from the exact candidate",
            )
        completed_at = _clock_now(self.clock)
        if completed_at < verified_at:
            raise ProductionEvidenceError(
                ProductionEvidenceErrorCode.MANIFEST_AUTHENTICATION_FAILED,
                "M30 authentication clock moved backwards",
            )
        completion_reasons = manifest_validity_block_reasons(
            manifest=manifest,
            verified_at=completed_at,
        )
        if completion_reasons:
            return _blocked_report(
                preflight=validation.preflight,
                reasons=completion_reasons,
                campaign_id=manifest.campaign_id,
                manifest_sha256=loaded.raw_sha256,
            )
        return M30ManifestAuthenticationReport(
            schema_version=1,
            milestone="M30",
            phase="1a",
            preflight=validation.preflight,
            state=M30ManifestAuthenticationState.AUTHENTICATED,
            blocking_reasons=(),
            campaign_id=manifest.campaign_id,
            manifest_sha256=loaded.raw_sha256,
            authentication=authentication,
            campaign_executable=False,
            release_decision=M30ReleaseDecision.NO_GO,
            workflow_attested_manifest_authenticated=True,
            external_controls_passed=0,
            external_controls_remaining=24,
            synthetic_evidence_accepted_as_operated=False,
            database_calls=0,
            datahub_writes=0,
            source_writes=0,
        )


def repository_ready_for_campaign(preflight: M30ReadinessReport) -> bool:
    """Return whether every repository-only gate is exact and passed."""

    return all(
        gate.status is M30GateStatus.PASSED
        for gate in preflight.gates
        if gate.evidence_class is M30EvidenceClass.REPOSITORY_CONTRACT
    )


def manifest_block_reasons(
    *,
    preflight: M30ReadinessReport,
    contract: M30CampaignContract,
    loaded: LoadedM30CampaignManifest,
    verified_at: datetime,
) -> tuple[M30CampaignBlockReason, ...]:
    """Compare untrusted frozen inputs before invoking the external authenticator."""

    manifest = loaded.manifest
    reasons: list[M30CampaignBlockReason] = []
    if manifest.candidate != preflight.candidate or not manifest_matches_contract(
        manifest, contract
    ):
        reasons.append(M30CampaignBlockReason.MANIFEST_SUBJECT_MISMATCH)
    reasons.extend(
        manifest_validity_block_reasons(
            manifest=manifest,
            verified_at=verified_at,
        )
    )
    return tuple(sorted(set(reasons), key=lambda item: item.value))


def manifest_validity_block_reasons(
    *,
    manifest: M30CampaignManifest,
    verified_at: datetime,
) -> tuple[M30CampaignBlockReason, ...]:
    """Return time-window failures at the supplied trusted UTC instant."""

    reasons: list[M30CampaignBlockReason] = []
    if verified_at < manifest.not_before:
        reasons.append(M30CampaignBlockReason.MANIFEST_NOT_YET_VALID)
    if verified_at >= manifest.expires_at:
        reasons.append(M30CampaignBlockReason.MANIFEST_EXPIRED)
    return tuple(reasons)


def _clock_now(clock: M30ClockPort) -> datetime:
    try:
        observed = clock.now()
        if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
            raise ValueError("clock did not return timezone-aware UTC")
        return observed.astimezone(UTC)
    except (AttributeError, TypeError, ValueError) as error:
        raise ProductionEvidenceError(
            ProductionEvidenceErrorCode.MANIFEST_AUTHENTICATION_FAILED,
            "M30 authentication clock is invalid",
        ) from error


def _revalidate_authentication(value: M30AuthenticatedManifest) -> M30AuthenticatedManifest:
    try:
        payload = value.model_dump(mode="python")
        if not isinstance(payload, dict):
            raise TypeError("authentication facts are not a dictionary")
        return M30AuthenticatedManifest.model_validate(dict(payload))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProductionEvidenceError(
            ProductionEvidenceErrorCode.MANIFEST_AUTHENTICATION_FAILED,
            "M30 manifest authentication facts are invalid",
        ) from error


def _blocked_report(
    *,
    preflight: M30ReadinessReport,
    reasons: tuple[M30CampaignBlockReason, ...],
    campaign_id: str | None = None,
    manifest_sha256: str | None = None,
) -> M30ManifestAuthenticationReport:
    return M30ManifestAuthenticationReport(
        schema_version=1,
        milestone="M30",
        phase="1a",
        preflight=preflight,
        state=M30ManifestAuthenticationState.BLOCKED,
        blocking_reasons=reasons,
        campaign_id=campaign_id,
        manifest_sha256=manifest_sha256,
        authentication=None,
        campaign_executable=False,
        release_decision=M30ReleaseDecision.NO_GO,
        workflow_attested_manifest_authenticated=False,
        external_controls_passed=0,
        external_controls_remaining=24,
        synthetic_evidence_accepted_as_operated=False,
        database_calls=0,
        datahub_writes=0,
        source_writes=0,
    )


__all__ = [
    "AuthenticateM30CampaignManifest",
    "M30CampaignManifestValidation",
    "ValidateM30CampaignManifest",
    "manifest_block_reasons",
    "manifest_validity_block_reasons",
    "repository_ready_for_campaign",
]
