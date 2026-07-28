"""Plan or explicitly run the bounded nano-first Query Studio evaluation."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
from typing import TYPE_CHECKING

from schemabridge.adapters.evaluation.query_studio_live import (
    NANO_FIRST_MODEL_ORDER,
    OFFICIAL_STANDARD_PRICING,
    EvaluationCandidate,
    EvaluationCandidateAuthorization,
    NanoFirstQualificationCampaignReport,
    evaluate_qualified_nano_first_query_studio,
    external_runtime_admission_is_aligned,
    load_query_studio_qualification_campaign_report,
    plan_nano_first_query_studio_qualification,
    require_fresh_query_studio_qualification_history,
    validate_openai_sdk_version,
    write_query_studio_qualification_campaign_report,
)

if TYPE_CHECKING:
    from schemabridge.bootstrap import QueryStudioRuntimeServices


_CANONICAL_LIVE_HISTORY_DIRECTORY = Path("reports/m27-query-studio-live-history")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan the synthetic nano-first evaluation. Provider calls require "
            "the explicit --execute-live switch."
        )
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("reports/m27-query-studio-live-evaluation.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("reports/m27-query-studio-live-evaluation.md"),
    )
    parser.add_argument(
        "--history-directory",
        type=Path,
        default=_CANONICAL_LIVE_HISTORY_DIRECTORY,
        help="append-only content-addressed campaign snapshots",
    )
    parser.add_argument(
        "--resume-json",
        type=Path,
        help="retained awaiting campaign to resume after an exact policy revision",
    )
    parser.add_argument("--repetitions", type=int, default=3, choices=(3,))
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="explicitly authorize the configured live runtime to call its provider",
    )
    arguments = parser.parse_args()

    if not arguments.execute_live:
        plan = plan_nano_first_query_studio_qualification(
            arguments.repository_root,
            repetitions=arguments.repetitions,
        )
        print("M27 nano-first qualification plan (no provider call performed)")
        for index, model in enumerate(NANO_FIRST_MODEL_ORDER, start=1):
            pricing = OFFICIAL_STANDARD_PRICING[model]
            print(
                f"{index}. {model}: "
                f"input={pricing.input_eur_per_million}/1M "
                f"output={pricing.output_eur_per_million}/1M"
            )
        print(
            "Qualification: "
            f"{plan.qualification_cases_per_model} cases / "
            f"{plan.qualification_base_provider_attempts_per_model} base attempts per model"
        )
        print(
            "Full corpus: every qualifier until first full PASS / "
            f"{plan.full_corpus_base_provider_attempts} base attempts per run / "
            f"{plan.full_corpus_runs_maximum} runs maximum"
        )
        print(
            "Worst-case base attempts: "
            f"{plan.worst_case_base_provider_attempts}/"
            f"{plan.limits.max_provider_attempts} "
            f"(headroom {plan.provider_attempt_headroom})"
        )
        print(
            "Per-stage fail-closed reservation: "
            f"input<={plan.stage_input_reservation_maximum} "
            f"(campaign headroom {plan.input_headroom_after_maximum_stage}); "
            f"output/reasoning<={plan.stage_output_reservation_maximum} "
            f"(campaign headroom {plan.output_headroom_after_maximum_stage})"
        )
        print(
            "Most expensive admitted stage: "
            f"EUR {plan.maximum_stage_cost_eur} "
            f"(campaign headroom EUR {plan.cost_headroom_after_maximum_stage_eur})"
        )
        print("Caps: 180 attempts, 250000 input tokens, 40000 output/reasoning tokens, EUR 1.00")
        return 0

    failure_phase = "configuration"
    try:
        canonical_history_directory = _require_canonical_live_history_directory(
            arguments.repository_root,
            arguments.history_directory,
        )
        evidence_signing_key, evidence_key_version = _live_evidence_signing_material()
        failure_phase = "history_pre_egress"
        if arguments.resume_json is None:
            require_fresh_query_studio_qualification_history(
                arguments.repository_root,
                history_directory=canonical_history_directory,
                signing_key=evidence_signing_key,
                signing_key_version=evidence_key_version,
            )
        failure_phase = "resume_load"
        prior_report = (
            None
            if arguments.resume_json is None
            else load_query_studio_qualification_campaign_report(
                arguments.repository_root,
                arguments.resume_json,
                signing_key=evidence_signing_key,
                signing_key_version=evidence_key_version,
                history_directory=canonical_history_directory,
            )
        )
        failure_phase = "provider_campaign"
        report = _run_live(
            arguments.repository_root,
            repetitions=arguments.repetitions,
            prior_report=prior_report,
        )
        failure_phase = "evidence_write"
        write_query_studio_qualification_campaign_report(
            report,
            arguments.repository_root,
            json_path=arguments.json,
            markdown_path=arguments.markdown,
            history_directory=canonical_history_directory,
            signing_key=evidence_signing_key,
            signing_key_version=evidence_key_version,
        )
    except Exception as error:  # sanitized CLI boundary: never render payloads or settings
        print(f"M27 live evaluation failed closed ({type(error).__name__}) phase={failure_phase}")
        return 2

    qualification = report.qualification_reports[-1]
    current_full = next(
        (
            item
            for item in reversed(report.full_evaluations)
            if item.model_snapshot == qualification.model_snapshot
            and item.policy_version == qualification.policy_version
        ),
        None,
    )
    if current_full is None:
        print(
            "M27 nano-first "
            f"{report.evaluation_outcome.upper()}: "
            f"model={qualification.model_snapshot} "
            f"policy_version={qualification.policy_version or '-'} "
            f"qualification={qualification.evaluation_outcome} "
            f"attempts={report.usage.provider_attempts} "
            f"cost_eur={report.usage.calculated_cost_eur} "
            f"next_model={report.next_required_model or '-'}"
        )
    else:
        candidate = current_full.report
        metrics = candidate.metrics
        print(
            "M27 nano-first "
            f"{'PASS' if candidate.passed else candidate.evaluation_outcome.upper()}: "
            f"model={candidate.model_snapshot} "
            f"policy_version={current_full.policy_version} "
            f"top1={metrics.top_1_accuracy:.6f} "
            f"top3={metrics.top_3_recall:.6f} "
            f"mrr={metrics.mean_reciprocal_rank:.6f} "
            f"recall20={metrics.recall_at_20:.6f} "
            f"specificity={metrics.no_match_specificity:.6f} "
            f"ambiguity={metrics.ambiguity_recall:.6f} "
            f"core={metrics.exact_core_success_rate:.6f} "
            f"attempts={report.usage.provider_attempts} "
            f"cost_eur={report.usage.calculated_cost_eur}"
        )
    return 0 if report.selected_model is not None else 1


def _require_canonical_live_history_directory(
    repository_root: Path,
    requested_history_directory: Path,
) -> Path:
    """Pin live evidence to the repository-owned append-only history."""

    root = repository_root.resolve()
    canonical = (root / _CANONICAL_LIVE_HISTORY_DIRECTORY).resolve()
    requested = requested_history_directory
    if not requested.is_absolute():
        requested = root / requested
    if requested.resolve() != canonical:
        raise ValueError("live history directory must be the canonical repository path")
    return canonical


def _live_evidence_signing_material() -> tuple[bytes, str]:
    """Load the owner-only control key only for signed live evidence."""

    from schemabridge.config import get_settings

    settings = get_settings()
    if settings.control_audit_signing_key is None:
        raise RuntimeError("live evaluation evidence signing is unavailable")
    return (
        settings.control_audit_signing_key.get_secret_value().encode("utf-8"),
        settings.control_audit_key_version,
    )


def _run_live(
    repository_root: Path,
    *,
    repetitions: int,
    prior_report: NanoFirstQualificationCampaignReport | None = None,
) -> NanoFirstQualificationCampaignReport:
    """Evaluate only the exact model/configuration authorized for this tenant."""

    from schemabridge.bootstrap import (
        build_query_studio_runtime,
        build_streamlit_principal,
    )
    from schemabridge.config import get_settings

    root = repository_root.resolve()
    base_settings = get_settings()
    principal = build_streamlit_principal(settings=base_settings)
    fake_settings = base_settings.model_copy(update={"query_studio_ai_mode": "fake"})
    fake_baseline = build_query_studio_runtime(
        principal=principal,
        repository_root=root,
        settings=fake_settings,
    )
    live_settings = base_settings.model_copy(update={"query_studio_ai_mode": "live"})
    live_runtime = build_query_studio_runtime(
        principal=principal,
        repository_root=root,
        settings=live_settings,
    )
    model = live_runtime.configuration.model_snapshot
    if model not in NANO_FIRST_MODEL_ORDER:
        raise ValueError("configured live evaluation model is outside the reviewed order")
    authorization = _load_live_authorization(live_runtime)
    openai_sdk_version = _installed_openai_sdk_version()

    return evaluate_qualified_nano_first_query_studio(
        root,
        fake_baseline=fake_baseline,
        candidates=(
            EvaluationCandidate(
                model_snapshot=model,
                openai_sdk_version=openai_sdk_version,
                runtime_factory=lambda: live_runtime,
                pricing=OFFICIAL_STANDARD_PRICING[model],
                authorization=authorization,
            ),
        ),
        candidate_order=NANO_FIRST_MODEL_ORDER,
        repetitions=repetitions,
        prior_report=prior_report,
    )


def _installed_openai_sdk_version() -> str:
    """Resolve and validate the exact installed provider SDK without importing it."""

    try:
        observed = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError("required OpenAI SDK distribution is unavailable") from error
    return validate_openai_sdk_version(observed)


def _load_live_authorization(
    runtime: QueryStudioRuntimeServices,
) -> EvaluationCandidateAuthorization:
    """Read one sanitized policy snapshot; durable admission rechecks it per call."""

    from schemabridge.adapters.control_plane.postgres_query_studio_ai import (
        PostgresQueryStudioAiControl,
    )
    from schemabridge.application.ports.query_studio_ai_control import AiControlError
    from schemabridge.application.query_studio_ai_admission import (
        AdmittedQueryStudioIntent,
    )

    prepare = runtime.prepare_natural
    if prepare is None or not external_runtime_admission_is_aligned(runtime):
        return EvaluationCandidateAuthorization(status="policy_unavailable")
    interpreter = prepare.interpreter
    if type(interpreter) is not AdmittedQueryStudioIntent:
        return EvaluationCandidateAuthorization(status="policy_unavailable")
    control = interpreter.control
    if not isinstance(control, PostgresQueryStudioAiControl):
        return EvaluationCandidateAuthorization(status="policy_unavailable")
    try:
        policy = control.load_policy(interpreter.workspace_id)
    except AiControlError:
        policy = None
    return EvaluationCandidateAuthorization.from_policy(
        workspace_id=interpreter.workspace_id,
        policy=policy,
    )


if __name__ == "__main__":
    raise SystemExit(main())
