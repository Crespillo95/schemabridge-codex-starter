"""Reproducible evaluation across existing governed application use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from schemabridge.application.candidate_engine import EvaluateSemanticCandidates
from schemabridge.application.governed_execution import (
    ExecuteGovernedRequest,
    GovernedPreparedQuery,
    GovernedQueryResult,
    PrepareGovernedRequest,
)
from schemabridge.application.guided_requests import (
    BuildGuidedRequest,
    GuidedRequestCase,
    GuidedRequestValidationError,
    build_demo_guided_input,
)
from schemabridge.application.intent_resolution import (
    IntentConfirmationError,
    ResolveNaturalLanguageIntent,
)
from schemabridge.application.join_discovery import DiscoverJoinCandidates
from schemabridge.application.ports.evaluation import (
    EvaluationGroundTruthPort,
    EvaluationReleaseIdentityPort,
)
from schemabridge.application.ports.intents import IntentParserError
from schemabridge.application.ports.planning import PlanningPortError
from schemabridge.application.ports.relationships import RelationshipWorkflowError
from schemabridge.application.query_execution import (
    CompiledQuery,
    QueryCompilationError,
    QueryPreviewError,
    SqlPolicyGuardPort,
    SqlPolicyViolation,
)
from schemabridge.application.query_recipes import AssessQueryRecipeReuse
from schemabridge.domain.evaluation import (
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationGroundTruth,
    EvaluationGuidedCase,
    EvaluationMetric,
    EvaluationMode,
    EvaluationQueryGroundTruth,
    EvaluationReport,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSection,
    EvaluationSectionStatus,
    normalize_preview_rows,
    normalized_rows_json,
    rate_metric,
)
from schemabridge.domain.intents import IntentConfirmation, UserLanguage
from schemabridge.domain.joins import JoinProposal
from schemabridge.domain.plans import QueryPolicy
from schemabridge.domain.request_context import ValidatedAnalyticalRequest
from schemabridge.domain.resolution import (
    ResolvedSemanticPlan,
    SemanticResolutionError,
    resolved_semantic_plan_fingerprint,
)
from schemabridge.domain.workflows import fingerprint_payload


@dataclass(frozen=True, slots=True)
class _QueryObservation:
    case: EvaluationQueryGroundTruth
    prepared: GovernedPreparedQuery | None
    result: GovernedQueryResult | None
    validated_request: ValidatedAnalyticalRequest | None
    compile_error: str | None = None
    execution_error: str | None = None


@dataclass(frozen=True, slots=True)
class RunReleaseEvaluation:
    """Measure only existing deterministic or explicitly live application paths."""

    ground_truth: EvaluationGroundTruthPort
    release_identity: EvaluationReleaseIdentityPort
    candidates: EvaluateSemanticCandidates
    joins: DiscoverJoinCandidates
    join_proposals: tuple[JoinProposal, ...]
    guided: BuildGuidedRequest
    deterministic_intent: ResolveNaturalLanguageIntent
    prepare: PrepareGovernedRequest
    execute: ExecuteGovernedRequest
    guard: SqlPolicyGuardPort
    safety_policy: QueryPolicy
    recipe_assessor: AssessQueryRecipeReuse
    live_intent: ResolveNaturalLanguageIntent | None = None

    def execute_evaluation(self) -> EvaluationReport:
        truth = self.ground_truth.load()
        observations = self._query_observations(truth)
        deterministic_sections = (
            self._candidate_section(),
            self._join_section(truth, observations),
            self._intent_section(truth, self.deterministic_intent),
            self._query_section(observations),
            self._safety_section(truth),
            self._recipe_section(truth, observations),
        )
        deterministic_failed = any(
            section.status is EvaluationSectionStatus.FAILED for section in deterministic_sections
        )
        deterministic = EvaluationRun(
            mode=EvaluationMode.DETERMINISTIC,
            adapter="recorded fixtures + deterministic fake + read-only PostgreSQL",
            required=True,
            status=(
                EvaluationRunStatus.FAILED
                if deterministic_failed
                else EvaluationRunStatus.COMPLETED
            ),
            sections=deterministic_sections,
        )
        if self.live_intent is None:
            live = EvaluationRun(
                mode=EvaluationMode.LIVE_LLM,
                adapter="live structured-output intent parser",
                required=False,
                status=EvaluationRunStatus.NOT_RUN,
                reason=(
                    "Live LLM evaluation was not requested; deterministic metrics remain "
                    "separate and key-free."
                ),
            )
        else:
            live_section = self._intent_section(truth, self.live_intent)
            live = EvaluationRun(
                mode=EvaluationMode.LIVE_LLM,
                adapter="live structured-output intent parser; no fallback",
                required=True,
                status=(
                    EvaluationRunStatus.FAILED
                    if live_section.status is EvaluationSectionStatus.FAILED
                    else EvaluationRunStatus.COMPLETED
                ),
                sections=(live_section,),
            )
        runs = (deterministic, live)
        return EvaluationReport(
            ground_truth_version=truth.version,
            fixture_fingerprint=truth.fixture_fingerprint,
            fixture_notice=truth.fixture_notice,
            release=self.release_identity.inspect(),
            runs=runs,
            statistical_note=(
                "The fixture is too small for a meaningful confidence interval; every metric "
                "therefore reports raw numerator, denominator, skips, and failures only."
            ),
            successful=all(
                not run.required or run.status is EvaluationRunStatus.COMPLETED for run in runs
            ),
        )

    def _candidate_section(self) -> EvaluationSection:
        metrics = self.candidates.execute()
        positive_count = len(metrics.true_positives) + len(metrics.false_negatives)
        predicted_positive = len(metrics.true_positives) + len(metrics.false_positives)
        precision = rate_metric(
            "candidate_precision",
            len(metrics.true_positives),
            predicted_positive,
            evaluated_cases=metrics.case_count,
            failed_cases=len(metrics.false_positives),
            note="Recommendation-threshold precision on the labeled synthetic fixture.",
        )
        recall = rate_metric(
            "candidate_recall",
            len(metrics.true_positives),
            positive_count,
            evaluated_cases=metrics.case_count,
            failed_cases=len(metrics.false_negatives),
            note="Recommendation-threshold recall on the labeled synthetic fixture.",
        )
        f1 = rate_metric(
            "candidate_f1",
            2 * len(metrics.true_positives),
            (
                2 * len(metrics.true_positives)
                + len(metrics.false_positives)
                + len(metrics.false_negatives)
            ),
            evaluated_cases=metrics.case_count,
            failed_cases=len(metrics.false_positives) + len(metrics.false_negatives),
            note="Exact 2TP / (2TP + FP + FN) count formula.",
        )
        top_k = tuple(
            rate_metric(
                f"candidate_top_{value}_recall",
                score * positive_count,
                positive_count,
                evaluated_cases=metrics.case_count,
                note=f"Positive labels found in the first {value} ranked cases.",
            )
            for value, score in sorted(metrics.top_k_recall.items())
        )
        cases = tuple(
            _candidate_case(case_id, outcome)
            for outcome, identities in (
                ("true_positive", metrics.true_positives),
                ("true_negative", metrics.true_negatives),
                ("false_positive", metrics.false_positives),
                ("false_negative", metrics.false_negatives),
            )
            for case_id in identities
        )
        return EvaluationSection(
            id="semantic_matching",
            label="Semantic candidate matching",
            status=EvaluationSectionStatus.COMPLETED,
            metrics=(precision, recall, f1, *top_k),
            cases=cases,
        )

    def _query_observations(
        self,
        truth: EvaluationGroundTruth,
    ) -> tuple[_QueryObservation, ...]:
        observations: list[_QueryObservation] = []
        for case in truth.queries:
            try:
                validated = self.guided.execute(build_demo_guided_input(_guided_case(case)))
                prepared = self.prepare.execute(validated)
            except (
                GuidedRequestValidationError,
                PlanningPortError,
                QueryCompilationError,
                SemanticResolutionError,
                SqlPolicyViolation,
                ValueError,
            ) as error:
                observations.append(
                    _QueryObservation(
                        case=case,
                        prepared=None,
                        result=None,
                        validated_request=None,
                        compile_error=_error_code(error, "compile_failed"),
                    )
                )
                continue
            try:
                result = self.execute.execute_prepared(prepared)
            except (PlanningPortError, QueryPreviewError, ValueError) as error:
                observations.append(
                    _QueryObservation(
                        case=case,
                        prepared=prepared,
                        result=None,
                        validated_request=validated,
                        execution_error=_error_code(error, "execution_failed"),
                    )
                )
                continue
            observations.append(
                _QueryObservation(
                    case=case,
                    prepared=prepared,
                    result=result,
                    validated_request=validated,
                )
            )
        return tuple(observations)

    def _join_section(
        self,
        truth: EvaluationGroundTruth,
        observations: tuple[_QueryObservation, ...],
    ) -> EvaluationSection:
        cases: list[EvaluationCaseResult] = []
        path_correct = 0
        for observation in observations:
            actual = (
                tuple(item.id for item in observation.prepared.resolved_plan.selected_contracts)
                if observation.prepared is not None
                else ()
            )
            expected_path = observation.case.expected_join_contracts
            passed = observation.prepared is not None and actual == expected_path
            path_correct += int(passed)
            cases.append(
                _case(
                    f"join_path_{observation.case.id}",
                    passed,
                    expected=",".join(expected_path) or "no_join",
                    actual=(
                        ",".join(actual) or "no_join"
                        if observation.prepared is not None
                        else observation.compile_error or "not_available"
                    ),
                    detail="Exact ordered approved join-contract path comparison.",
                )
            )

        expected_cards = {item.id: item.cardinality.value for item in truth.joins}
        cardinality_correct = 0
        try:
            discovered = self.joins.execute(self.join_proposals)
            actual_cards = {
                item.proposal.id: item.cardinality.cardinality.value
                for item in discovered.candidates
            }
            for contract_id, expected_cardinality in expected_cards.items():
                actual_cardinality = actual_cards.get(contract_id, "missing")
                passed = actual_cardinality == expected_cardinality
                cardinality_correct += int(passed)
                cases.append(
                    _case(
                        f"cardinality_{contract_id}",
                        passed,
                        expected=expected_cardinality,
                        actual=actual_cardinality,
                        detail="Cardinality classified from declared and aggregate read evidence.",
                    )
                )
        except (PlanningPortError, RelationshipWorkflowError, ValueError) as error:
            code = _error_code(error, "join_discovery_failed")
            for contract_id, expected_cardinality in expected_cards.items():
                cases.append(
                    _case(
                        f"cardinality_{contract_id}",
                        False,
                        expected=expected_cardinality,
                        actual=code,
                        detail="Join discovery did not produce a comparable classification.",
                    )
                )
        metrics = (
            rate_metric(
                "join_path_accuracy",
                path_correct,
                len(observations),
                evaluated_cases=len(observations),
                failed_cases=len(observations) - path_correct,
                note="Exact query join-path matches, including the no-join control.",
            ),
            rate_metric(
                "join_cardinality_accuracy",
                cardinality_correct,
                len(expected_cards),
                evaluated_cases=len(expected_cards),
                failed_cases=len(expected_cards) - cardinality_correct,
                note="Exact expected cardinality classifications for both governed contracts.",
            ),
        )
        return _section("join_reasoning", "Join path and cardinality", metrics, tuple(cases))

    def _intent_section(
        self,
        truth: EvaluationGroundTruth,
        resolver: ResolveNaturalLanguageIntent,
    ) -> EvaluationSection:
        cases: list[EvaluationCaseResult] = []
        eligible = 0
        correct = 0
        for case in truth.queries:
            if case.intent_alternative is None:
                cases.append(
                    EvaluationCaseResult(
                        id=f"intent_{case.id}",
                        status=EvaluationCaseStatus.SKIPPED,
                        expected="typed request equivalence",
                        actual="not_evaluated",
                        detail=case.intent_skip_reason or "fixture-declared skip",
                        blocking=False,
                    )
                )
                continue
            eligible += 1
            try:
                preview = resolver.preview(case.question, UserLanguage.SPANISH)
                confirmed = resolver.confirm(
                    preview,
                    IntentConfirmation(
                        interpretation_fingerprint=preview.interpretation_fingerprint,
                        selected_alternative=case.intent_alternative,
                    ),
                )
                passed = confirmed.request == case.expected_request
                correct += int(passed)
                cases.append(
                    _case(
                        f"intent_{case.id}",
                        passed,
                        expected=fingerprint_payload(case.expected_request.model_dump(mode="json")),
                        actual=fingerprint_payload(confirmed.request.model_dump(mode="json")),
                        detail="Confirmed typed request fingerprint equivalence; no SQL involved.",
                    )
                )
            except (IntentConfirmationError, IntentParserError, ValueError) as error:
                cases.append(
                    _case(
                        f"intent_{case.id}",
                        False,
                        expected="equivalent_typed_request",
                        actual=_error_code(error, "intent_failed"),
                        detail="Intent parsing or fingerprint-bound confirmation failed.",
                    )
                )
        metric = rate_metric(
            "intent_equivalence",
            correct,
            eligible,
            evaluated_cases=eligible,
            skipped_cases=len(truth.queries) - eligible,
            failed_cases=eligible - correct,
            note="Exact typed-request equality after explicit ambiguity confirmation.",
        )
        return _section(
            "intent_equivalence", "Natural-language intent equivalence", (metric,), tuple(cases)
        )

    def _query_section(
        self,
        observations: tuple[_QueryObservation, ...],
    ) -> EvaluationSection:
        cases: list[EvaluationCaseResult] = []
        compiled = executed = correct_rows = correct_rejections = 0
        for observation in observations:
            case_id = observation.case.id
            compiled_ok = observation.prepared is not None
            compiled += int(compiled_ok)
            cases.append(
                _case(
                    f"compile_{case_id}",
                    compiled_ok,
                    expected="independently_guarded_query",
                    actual="accepted" if compiled_ok else observation.compile_error or "failed",
                    detail="Restricted plan compiled and final SQL passed the independent guard.",
                )
            )
            result = observation.result
            execution_ok = bool(
                result is not None
                and result.preview.database_user == "schemabridge_reader"
                and result.preview.transaction_read_only
                and result.preview.statement_timeout_ms == 5_000
            )
            executed += int(execution_ok)
            cases.append(
                _case(
                    f"execution_{case_id}",
                    execution_ok,
                    expected="schemabridge_reader|read_only=true|timeout_ms=5000",
                    actual=(
                        (
                            f"{result.preview.database_user}|"
                            f"read_only={str(result.preview.transaction_read_only).lower()}|"
                            f"timeout_ms={result.preview.statement_timeout_ms}"
                        )
                        if result is not None
                        else observation.execution_error or "not_executed"
                    ),
                    detail="Bounded preview safety facts observed from PostgreSQL.",
                )
            )
            actual_rows = (
                normalize_preview_rows(result.preview.columns, result.preview.rows)
                if result is not None
                else ()
            )
            rows_ok = result is not None and actual_rows == observation.case.expected_rows
            correct_rows += int(rows_ok)
            cases.append(
                _case(
                    f"result_{case_id}",
                    rows_ok,
                    expected=normalized_rows_json(observation.case.expected_rows),
                    actual=normalized_rows_json(actual_rows),
                    detail="Type-tagged, column-keyed, order-independent normalized row multiset.",
                )
            )
            actual_rejections = (
                tuple(item.code.value for item in result.rejected_sources.records)
                if result is not None
                else ()
            )
            rejections_ok = (
                result is not None
                and actual_rejections == observation.case.expected_rejection_codes
            )
            correct_rejections += int(rejections_ok)
            cases.append(
                _case(
                    f"rejections_{case_id}",
                    rejections_ok,
                    expected=",".join(observation.case.expected_rejection_codes) or "none",
                    actual=",".join(actual_rejections) or "none",
                    detail="Stable rejected-source codes; source values are not retained here.",
                )
            )
        count = len(observations)
        metrics = (
            rate_metric(
                "compile_success",
                compiled,
                count,
                evaluated_cases=count,
                failed_cases=count - compiled,
                note="Cases producing independently guarded final SQL.",
            ),
            rate_metric(
                "execution_success",
                executed,
                count,
                evaluated_cases=count,
                failed_cases=count - executed,
                note="Cases completing with the exact reader, read-only mode, and timeout.",
            ),
            rate_metric(
                "result_correctness",
                correct_rows,
                count,
                evaluated_cases=count,
                failed_cases=count - correct_rows,
                note="Normalized result-row equality; SQL text equality is not used.",
            ),
            rate_metric(
                "source_rejection_correctness",
                correct_rejections,
                count,
                evaluated_cases=count,
                failed_cases=count - correct_rejections,
                note="Exact stable rejection-code sequence, including the zero-rejection control.",
            ),
        )
        return _section(
            "query_execution", "Compilation, execution, and results", metrics, tuple(cases)
        )

    def _safety_section(self, truth: EvaluationGroundTruth) -> EvaluationSection:
        cases: list[EvaluationCaseResult] = []
        correct = 0
        for expected in truth.safety_cases:
            try:
                self.guard.validate(
                    CompiledQuery(
                        sql=expected.sql,
                        parameters=(),
                        effective_limit=expected.effective_limit,
                    ),
                    self.safety_policy,
                )
            except SqlPolicyViolation as error:
                actual = error.findings[0].code.value
                passed = actual == expected.expected_code
            except (QueryCompilationError, ValueError) as error:
                actual = _error_code(error, "guard_failed")
                passed = False
            else:
                actual = "accepted_unsafe_sql"
                passed = False
            correct += int(passed)
            cases.append(
                _case(
                    f"safety_{expected.id}",
                    passed,
                    expected=expected.expected_code,
                    actual=actual,
                    detail="Guard-only evaluation; malicious fixture SQL is never executed.",
                )
            )
        metric = rate_metric(
            "safety_rejection_rate",
            correct,
            len(truth.safety_cases),
            evaluated_cases=len(truth.safety_cases),
            failed_cases=len(truth.safety_cases) - correct,
            note="Malicious cases rejected with the expected first stable guard code.",
        )
        return _section("sql_safety", "Independent SQL policy rejection", (metric,), tuple(cases))

    def _recipe_section(
        self,
        truth: EvaluationGroundTruth,
        observations: tuple[_QueryObservation, ...],
    ) -> EvaluationSection:
        north_star = next(
            (
                item
                for item in observations
                if item.case.guided_case is EvaluationGuidedCase.NORTH_STAR
            ),
            None,
        )
        cases: list[EvaluationCaseResult] = []
        correct = 0
        for expected in truth.recipe_reuse_cases:
            if (
                north_star is None
                or north_star.prepared is None
                or north_star.validated_request is None
            ):
                cases.append(
                    _case(
                        f"recipe_{expected.id}",
                        False,
                        expected=expected.expected_status,
                        actual="governed_plan_unavailable",
                        detail="Recipe compatibility requires the current typed governed plan.",
                    )
                )
                continue
            resolved = _mutate_plan(north_star.prepared.resolved_plan, expected.mutation)
            assessment = self.recipe_assessor.execute(
                validated_request=north_star.validated_request,
                resolved_plan=resolved,
                plan_fingerprint=resolved_semantic_plan_fingerprint(resolved),
                query_fingerprint=_query_fingerprint(north_star.prepared),
                source_schema_fingerprint=truth.recipe.source_schema_fingerprint,
            )
            actual_reasons = tuple(item.value for item in assessment.reasons)
            passed = (
                assessment.status.value == expected.expected_status
                and actual_reasons == expected.expected_reasons
                and assessment.revalidated
            )
            correct += int(passed)
            cases.append(
                _case(
                    f"recipe_{expected.id}",
                    passed,
                    expected=(
                        f"{expected.expected_status}|{','.join(expected.expected_reasons) or 'none'}"
                    ),
                    actual=(f"{assessment.status.value}|{','.join(actual_reasons) or 'none'}"),
                    detail="SQL-free recipe compatibility; current SQL is still replanned and guarded.",
                )
            )
        metric = rate_metric(
            "recipe_reuse_accuracy",
            correct,
            len(truth.recipe_reuse_cases),
            evaluated_cases=len(truth.recipe_reuse_cases),
            failed_cases=len(truth.recipe_reuse_cases) - correct,
            note="Current and stale recipe decisions with exact provenance and reason checks.",
        )
        return _section("recipe_reuse", "Validated query-recipe reuse", (metric,), tuple(cases))


def _candidate_case(case_id: str, outcome: str) -> EvaluationCaseResult:
    expected = "positive" if outcome in {"true_positive", "false_negative"} else "negative"
    actual = "positive" if outcome in {"true_positive", "false_positive"} else "negative"
    return EvaluationCaseResult(
        id=f"candidate_{case_id}",
        status=EvaluationCaseStatus.OBSERVED,
        expected=expected,
        actual=actual,
        detail=outcome,
        blocking=False,
    )


def _guided_case(case: EvaluationQueryGroundTruth) -> GuidedRequestCase:
    return {
        EvaluationGuidedCase.NORTH_STAR: GuidedRequestCase.NORTH_STAR,
        EvaluationGuidedCase.NO_JOIN: GuidedRequestCase.NO_JOIN,
    }[case.guided_case]


def _case(
    case_id: str,
    passed: bool,
    *,
    expected: str,
    actual: str,
    detail: str,
) -> EvaluationCaseResult:
    return EvaluationCaseResult(
        id=case_id,
        status=EvaluationCaseStatus.PASSED if passed else EvaluationCaseStatus.FAILED,
        expected=expected,
        actual=actual,
        detail=detail,
        blocking=True,
    )


def _section(
    section_id: str,
    label: str,
    metrics: tuple[EvaluationMetric, ...],
    cases: tuple[EvaluationCaseResult, ...],
) -> EvaluationSection:
    failed = any(case.blocking and case.status is EvaluationCaseStatus.FAILED for case in cases)
    return EvaluationSection(
        id=section_id,
        label=label,
        status=EvaluationSectionStatus.FAILED if failed else EvaluationSectionStatus.COMPLETED,
        metrics=metrics,
        cases=cases,
    )


def _query_fingerprint(prepared: GovernedPreparedQuery) -> str:
    return fingerprint_payload(
        {
            "sql": prepared.query.sql,
            "parameters": [_workflow_scalar(item) for item in prepared.query.parameters],
            "max_rows": prepared.query.max_rows,
            "statement_timeout_ms": prepared.query.statement_timeout_ms,
        }
    )


def _workflow_scalar(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime | Decimal):
        return str(value)
    raise ValueError("compiled query contains an unsupported fingerprint scalar")


def _mutate_plan(plan: ResolvedSemanticPlan, mutation: str) -> ResolvedSemanticPlan:
    if mutation == "none":
        return plan
    payload = plan.model_dump(mode="python")
    payload["selected_mappings"][0]["mapping"]["version"] += 1
    return ResolvedSemanticPlan.model_validate(payload)


def _error_code(error: Exception, fallback: str) -> str:
    value = getattr(error, "code", fallback)
    return value.value if hasattr(value, "value") else str(value)
