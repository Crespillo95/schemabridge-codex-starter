"""Synthetic M30 Phase-1a manifest fixtures shared by tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from schemabridge.adapters.evaluation.m30_readiness import (
    FileM30CampaignContract,
    GitM30CandidateIdentity,
)
from schemabridge.domain.production_campaign import (
    M30_ADVANCED_FAMILIES,
    M30_RESPONSIBLE_ROLE_BY_EVIDENCE_CLASS,
    M30_SIMPLE_FAMILIES,
    M30BrowserVersion,
    M30CampaignArtifactSet,
    M30CampaignControlAssignment,
    M30CampaignCorpusFreeze,
    M30CampaignCorpusSlice,
    M30CampaignManifest,
    M30CampaignOwner,
    M30CampaignOwnerRole,
    M30CampaignProviderFreeze,
    M30CampaignRisk,
    M30CampaignTargetFreeze,
)
from schemabridge.domain.production_readiness import (
    M30_LANGUAGE_MINIMUMS,
    M30_REQUIRED_CONTROL_SPECS,
    M30_SUPPORTED_FAMILIES,
    M30_UNSUPPORTED_FAMILIES,
    M30CaseClass,
)

VERIFICATION_TIME = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def build_manifest(
    repository: Path,
    *,
    not_before: datetime | None = None,
    expires_at: datetime | None = None,
) -> M30CampaignManifest:
    contract = FileM30CampaignContract(repository).load()
    candidate = GitM30CandidateIdentity(repository).inspect(contract)
    return M30CampaignManifest(
        schema_version=1,
        kind="schemabridge.m30.campaign-manifest",
        campaign_id="campaign:m30:2026-08-03:001",
        source_repository="Crespillo95/schemabridge-codex-starter",
        trust_policy_id="github-actions-m30-v1",
        issued_at=VERIFICATION_TIME - timedelta(hours=1),
        not_before=not_before or VERIFICATION_TIME - timedelta(minutes=5),
        expires_at=expires_at or VERIFICATION_TIME + timedelta(days=7),
        contract_sha256=contract.fingerprint(),
        candidate=candidate,
        candidate_sku=contract.candidate_sku,
        artifacts=M30CampaignArtifactSet(
            wheel_sha256="1" * 64,
            runtime_image_digest="sha256:" + "2" * 64,
            wheel_sbom_sha256="3" * 64,
            runtime_image_sbom_sha256="4" * 64,
            provenance_sha256="5" * 64,
            frozen_requirements_sha256="6" * 64,
        ),
        provider=M30CampaignProviderFreeze(
            provider_id="provider:synthetic-independent",
            model_snapshot="model:held-out-v1",
            configuration_sha256="7" * 64,
            prompt_bundle_sha256="8" * 64,
            parameters_sha256="9" * 64,
        ),
        target=M30CampaignTargetFreeze(
            environment_id="target:isolated-postgresql-001",
            region="eu-test-1",
            environment_sha256="a" * 64,
            postgresql_version="16.14",
            datahub_version="1.3.0",
            browsers=tuple(
                M30BrowserVersion(browser=name, version="2026.08")
                for name in ("chrome", "edge", "firefox", "safari")
            ),
            mobile_viewport="390x844",
            iam_policy_sha256="b" * 64,
            network_policy_sha256="c" * 64,
            secret_versions_sha256="d" * 64,
            observability_routes_sha256="e" * 64,
        ),
        corpus=M30CampaignCorpusFreeze(
            corpus_manifest_sha256="f" * 64,
            case_ids_sha256="0" * 64,
            hidden_answer_key_sha256="1" * 64,
            oracle_dataset_sha256="2" * 64,
            slices=_corpus_slices(),
        ),
        owners=tuple(
            M30CampaignOwner(
                role=role,
                authority_id=f"authority:{role.value}",
                approval_key_fingerprint=f"{position:x}" * 64,
            )
            for position, role in enumerate(M30CampaignOwnerRole, start=1)
        ),
        supported_families=M30_SUPPORTED_FAMILIES,
        unsupported_families=M30_UNSUPPORTED_FAMILIES,
        controls=tuple(
            M30CampaignControlAssignment(
                code=code,
                evidence_class=evidence_class,
                responsible_authority=(
                    f"authority:{M30_RESPONSIBLE_ROLE_BY_EVIDENCE_CLASS[evidence_class].value}"
                ),
                evidence_artifact_kind=f"artifact:{code}",
            )
            for code, (evidence_class, _label) in sorted(M30_REQUIRED_CONTROL_SPECS.items())
        ),
    )


def write_manifest(path: Path, manifest: M30CampaignManifest) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest.canonical_bytes())
    return path


def _corpus_slices() -> tuple[M30CampaignCorpusSlice, ...]:
    families_by_class = {
        M30CaseClass.SUPPORTED_SIMPLE: M30_SIMPLE_FAMILIES,
        M30CaseClass.SUPPORTED_ADVANCED: tuple(
            (family, risk)
            for family in M30_ADVANCED_FAMILIES
            for risk in (
                M30CampaignRisk.STANDARD,
                M30CampaignRisk.HIGH,
                M30CampaignRisk.CRITICAL,
            )
        ),
        M30CaseClass.AMBIGUOUS: (("cross_family", M30CampaignRisk.HIGH),),
        M30CaseClass.UNSUPPORTED: tuple(
            (family, M30CampaignRisk.HIGH) for family in M30_UNSUPPORTED_FAMILIES
        ),
        M30CaseClass.ADVERSARIAL_SECURITY: (("security", M30CampaignRisk.CRITICAL),),
    }
    rows: list[M30CampaignCorpusSlice] = []
    for case_class, raw_families in families_by_class.items():
        families = tuple(
            (item, M30CampaignRisk.STANDARD) if isinstance(item, str) else item
            for item in raw_families
        )
        spanish_total, english_total = M30_LANGUAGE_MINIMUMS[case_class]
        spanish_counts = _distributed(spanish_total, len(families))
        english_counts = _distributed(english_total, len(families))
        for (family, risk), spanish, english in zip(
            families,
            spanish_counts,
            english_counts,
            strict=True,
        ):
            rows.append(
                M30CampaignCorpusSlice(
                    case_class=case_class,
                    family=family,
                    risk=risk,
                    spanish_cases=spanish,
                    english_cases=english,
                )
            )
    return tuple(
        sorted(rows, key=lambda item: (item.case_class.value, item.family, item.risk.value))
    )


def _distributed(total: int, buckets: int) -> tuple[int, ...]:
    quotient, remainder = divmod(total, buckets)
    return tuple(quotient + (1 if position < remainder else 0) for position in range(buckets))


__all__ = ["VERIFICATION_TIME", "build_manifest", "write_manifest"]
