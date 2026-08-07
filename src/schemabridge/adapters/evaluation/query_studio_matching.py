"""Reproducible deterministic evaluation for governed Query Studio retrieval."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemabridge.adapters.evaluation.yaml_loader import load_unique_yaml
from schemabridge.adapters.query_studio.recorded import (
    RECORDED_MATCHER_VERSION,
    RecordedGovernedBindingFactsSearch,
)
from schemabridge.adapters.semantic_registry.recorded import (
    RecordedGovernedSemanticRegistry,
)
from schemabridge.application.query_studio import (
    RegistryAwareGovernedFieldSearch,
    SearchGovernedFields,
)
from schemabridge.domain.query_studio import (
    DescriptionQuery,
    GovernedFieldSearchRequest,
)
from schemabridge.domain.semantic_registry import (
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)

_CORPUS_RELATIVE_PATH = Path("demo/ground_truth/query_studio_matching.yml")
_MANIFEST_RELATIVE_PATH = Path("demo/ground_truth/registries/manifest.yml")
_CATALOG_RELATIVE_PATH = Path("demo/datahub/catalog_snapshot.json")
_EVALUATION_WORKSPACE = "query-studio-deterministic-evaluation"
_PAGE_SIZES = (1, 17, 50)
_TOP_1_THRESHOLD = 0.85
_TOP_3_THRESHOLD = 1.0
_MRR_THRESHOLD = 0.90
_RECALL_AT_20_THRESHOLD = 1.0
_SPECIFICITY_THRESHOLD = 1.0
_AMBIGUITY_RECALL_THRESHOLD = 1.0


class _CorpusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Descriptions(_CorpusModel):
    es: str = Field(min_length=3, max_length=300)
    en: str = Field(min_length=3, max_length=300)


class _PositiveMapping(_CorpusModel):
    id: str = Field(min_length=3, max_length=100)
    logical_field: str = Field(min_length=3, max_length=200)
    connection_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9_-]{2,199}$",
    )
    physical_field: str = Field(min_length=3, max_length=300)
    descriptions: _Descriptions


class _NegativeCase(_CorpusModel):
    id: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=3, max_length=2_000)


class _AmbiguityCase(_CorpusModel):
    id: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=3, max_length=2_000)
    expected: Literal["ambiguous"]


class _AdversarialCase(_CorpusModel):
    id: str = Field(min_length=3, max_length=100)
    text: str = Field(min_length=3, max_length=2_000)
    expected: str = Field(min_length=3, max_length=100)


class _CoreQueryCase(_CorpusModel):
    id: str = Field(min_length=3, max_length=100)
    language: Literal["es", "en"]
    text: str = Field(min_length=3, max_length=2_000)
    expected_models: tuple[str, ...] = Field(min_length=1, max_length=3)
    expected_joins: tuple[str, ...] = Field(max_length=2)


class _MatchingCorpus(_CorpusModel):
    version: Literal[1]
    fixture_notice: str = Field(min_length=20, max_length=1_000)
    positive_mappings: tuple[_PositiveMapping, ...] = Field(min_length=1, max_length=1_000)
    true_negatives: tuple[_NegativeCase, ...] = Field(min_length=1, max_length=1_000)
    critical_ambiguities: tuple[_AmbiguityCase, ...] = Field(
        min_length=1,
        max_length=1_000,
    )
    adversarial_inputs: tuple[_AdversarialCase, ...] = Field(
        min_length=1,
        max_length=1_000,
    )
    core_queries: tuple[_CoreQueryCase, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def case_identities_are_unique(self) -> _MatchingCorpus:
        for rows in (
            self.positive_mappings,
            self.true_negatives,
            self.critical_ambiguities,
            self.adversarial_inputs,
            self.core_queries,
        ):
            identities = tuple(item.id for item in rows)
            if len(identities) != len(set(identities)):
                raise ValueError("query studio corpus case ids must be unique")
        return self


class QueryStudioRetrievalMetrics(_CorpusModel):
    """Exact deterministic retrieval measurements over the synthetic corpus."""

    positive_cases: int = Field(ge=1)
    top_1_correct: int = Field(ge=0)
    top_1_accuracy: float = Field(ge=0.0, le=1.0)
    top_3_correct: int = Field(ge=0)
    top_3_recall: float = Field(ge=0.0, le=1.0)
    recall_at_20_correct: int = Field(ge=0)
    recall_at_20: float = Field(ge=0.0, le=1.0)
    reciprocal_rank_sum: float = Field(ge=0.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    negative_cases: int = Field(ge=1)
    negative_no_match_correct: int = Field(ge=0)
    no_match_specificity: float = Field(ge=0.0, le=1.0)
    ambiguity_cases: int = Field(ge=1)
    ambiguity_correct: int = Field(ge=0)
    ambiguity_recall: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def visible_ratios_are_consistent(self) -> QueryStudioRetrievalMetrics:
        ratios = (
            (self.top_1_correct, self.positive_cases, self.top_1_accuracy),
            (self.top_3_correct, self.positive_cases, self.top_3_recall),
            (self.recall_at_20_correct, self.positive_cases, self.recall_at_20),
            (
                self.negative_no_match_correct,
                self.negative_cases,
                self.no_match_specificity,
            ),
            (self.ambiguity_correct, self.ambiguity_cases, self.ambiguity_recall),
        )
        if any(
            numerator > denominator
            or not math.isclose(value, numerator / denominator, abs_tol=1e-12)
            for numerator, denominator, value in ratios
        ):
            raise ValueError("query studio evaluation ratio is inconsistent")
        if not math.isclose(
            self.mean_reciprocal_rank,
            self.reciprocal_rank_sum / self.positive_cases,
            abs_tol=1e-12,
        ):
            raise ValueError("query studio reciprocal-rank metric is inconsistent")
        return self


class QueryStudioDeterministicEvaluationReport(_CorpusModel):
    """Bounded, secret-free evidence for one deterministic retrieval run."""

    schema_version: Literal[1] = 1
    fixture_notice: str = Field(min_length=20, max_length=1_000)
    adapter: Literal["recorded_governed_binding_facts"] = "recorded_governed_binding_facts"
    matcher_version: str = Field(min_length=3, max_length=80)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    governed_mapping_count: int = Field(ge=1, le=1_000)
    catalog_asset_count: int = Field(ge=0)
    keyset_page_sizes: tuple[int, ...] = Field(min_length=1, max_length=10)
    keyset_traversal_counts: tuple[int, ...] = Field(min_length=1, max_length=10)
    keyset_stable: bool
    deterministic_replay_stable: bool
    score_order: Literal["integer_score_desc_logical_field_asc_binding_id_asc"] = (
        "integer_score_desc_logical_field_asc_binding_id_asc"
    )
    tie_behavior: Literal["equal_scores_remain_visible_and_use_stable_identity_order"] = (
        "equal_scores_remain_visible_and_use_stable_identity_order"
    )
    metrics: QueryStudioRetrievalMetrics
    top_1_errors: tuple[str, ...] = Field(max_length=1_000)
    false_negatives_at_20: tuple[str, ...] = Field(max_length=1_000)
    false_positives: tuple[str, ...] = Field(max_length=1_000)
    ambiguity_misses: tuple[str, ...] = Field(max_length=1_000)
    ungoverned_executable_results: tuple[str, ...] = Field(max_length=1_000)
    provider_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0
    calculated_cost_eur: float = Field(default=0.0, ge=0.0, le=0.0)
    passed: bool

    @model_validator(mode="after")
    def pass_flag_matches_complete_gate(self) -> QueryStudioDeterministicEvaluationReport:
        expected = (
            self.keyset_stable
            and self.deterministic_replay_stable
            and self.metrics.top_1_accuracy >= _TOP_1_THRESHOLD
            and self.metrics.top_3_recall >= _TOP_3_THRESHOLD
            and self.metrics.mean_reciprocal_rank >= _MRR_THRESHOLD
            and self.metrics.recall_at_20 >= _RECALL_AT_20_THRESHOLD
            and self.metrics.no_match_specificity >= _SPECIFICITY_THRESHOLD
            and self.metrics.ambiguity_recall >= _AMBIGUITY_RECALL_THRESHOLD
            and not self.false_negatives_at_20
            and not self.false_positives
            and not self.ambiguity_misses
            and not self.ungoverned_executable_results
        )
        if self.passed is not expected:
            raise ValueError("query studio evaluation pass flag is inconsistent")
        return self

    def json_bytes(self) -> bytes:
        """Return canonical human-readable JSON bytes for checked-in evidence."""

        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def markdown(self) -> str:
        """Render the same facts without adding non-reproducible timestamps."""

        metrics = self.metrics
        status = "PASS" if self.passed else "FAIL"
        return "\n".join(
            (
                "# M27 deterministic Query Studio retrieval",
                "",
                f"- Status: **{status}**",
                f"- Matcher: `{self.matcher_version}`",
                f"- Corpus SHA-256: `{self.corpus_sha256}`",
                f"- Catalog SHA-256: `{self.catalog_sha256}`",
                f"- Registry fingerprint: `{self.registry_fingerprint}`",
                (
                    "- Scope: synthetic recorded governed retrieval only; "
                    "no provider or source request was made."
                ),
                "",
                "| Metric | Result | Gate |",
                "|---|---:|---:|",
                (
                    f"| Top-1 accuracy | {metrics.top_1_correct}/"
                    f"{metrics.positive_cases} ({metrics.top_1_accuracy:.6f}) | >= 0.85 |"
                ),
                (
                    f"| Top-3 recall | {metrics.top_3_correct}/"
                    f"{metrics.positive_cases} ({metrics.top_3_recall:.6f}) | 1.00 |"
                ),
                (
                    f"| Recall@20 | {metrics.recall_at_20_correct}/"
                    f"{metrics.positive_cases} ({metrics.recall_at_20:.6f}) | 1.00 |"
                ),
                (f"| Mean reciprocal rank | {metrics.mean_reciprocal_rank:.6f} | >= 0.90 |"),
                (
                    f"| No-match specificity | {metrics.negative_no_match_correct}/"
                    f"{metrics.negative_cases} ({metrics.no_match_specificity:.6f}) | 1.00 |"
                ),
                (
                    f"| Critical ambiguity recall | {metrics.ambiguity_correct}/"
                    f"{metrics.ambiguity_cases} ({metrics.ambiguity_recall:.6f}) | 1.00 |"
                ),
                "",
                "## Boundedness and safety",
                "",
                (
                    f"- Governed mappings traversed: {self.governed_mapping_count}; "
                    f"page sizes: {list(self.keyset_page_sizes)}; "
                    f"counts: {list(self.keyset_traversal_counts)}."
                ),
                f"- Stable keyset and replay order: {self.keyset_stable and self.deterministic_replay_stable}.",
                f"- Ungoverned executable results: {len(self.ungoverned_executable_results)}.",
                f"- False positives: {len(self.false_positives)}.",
                f"- False negatives at 20: {len(self.false_negatives_at_20)}.",
                f"- Ambiguity misses: {len(self.ambiguity_misses)}.",
                "- Provider calls/tokens/cost: 0 / 0 / EUR 0.00.",
                "",
                "> " + self.fixture_notice,
                "",
            )
        )


@dataclass(frozen=True, slots=True)
class _StaticRegistry:
    value: ScopedSemanticRegistrySnapshot

    @property
    def scope(self) -> SemanticRegistryScope:
        return self.value.scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        return self.value


@dataclass(frozen=True, slots=True)
class _SearchStack:
    registry: ScopedSemanticRegistrySnapshot
    service: SearchGovernedFields


def evaluate_recorded_query_studio_matching(
    repository_root: Path,
    *,
    catalog_snapshot_path: Path | None = None,
) -> QueryStudioDeterministicEvaluationReport:
    """Evaluate every labelled governed description, negative, ambiguity, and keyset."""

    root = repository_root.resolve()
    corpus_path = root / _CORPUS_RELATIVE_PATH
    manifest_path = root / _MANIFEST_RELATIVE_PATH
    catalog_path = (
        catalog_snapshot_path.resolve()
        if catalog_snapshot_path is not None
        else root / _CATALOG_RELATIVE_PATH
    )
    corpus_bytes = corpus_path.read_bytes()
    corpus = _MatchingCorpus.model_validate(load_unique_yaml(corpus_bytes.decode("utf-8")))
    stack = _build_stack(manifest_path, catalog_path)
    governed_pairs = {
        (
            governed.mapping.logical_field.root,
            governed.mapping.physical_field.root,
        )
        for governed in stack.registry.registry.mapping_set.mappings
    }
    labelled_identities = {
        (item.logical_field, item.connection_id, item.physical_field)
        for item in corpus.positive_mappings
    }
    if {
        (logical_field, physical_field)
        for logical_field, _connection_id, physical_field in labelled_identities
    } != governed_pairs:
        raise ValueError("query studio labels do not exactly cover the governed mapping set")

    baseline_identities: tuple[str, ...] | None = None
    traversal_counts: list[int] = []
    keyset_stable = True
    all_observed_identities: set[tuple[str, str, str]] = set()
    for page_size in _PAGE_SIZES:
        identities, observed_identities = _traverse(stack, page_size)
        traversal_counts.append(len(identities))
        all_observed_identities.update(observed_identities)
        if len(identities) != len(governed_pairs) or len(identities) != len(set(identities)):
            keyset_stable = False
        if baseline_identities is None:
            baseline_identities = identities
        elif identities != baseline_identities:
            keyset_stable = False

    ranks: list[int | None] = []
    top_1_errors: list[str] = []
    false_negatives: list[str] = []
    deterministic_replay_stable = True
    for positive in corpus.positive_mappings:
        expected = (
            positive.logical_field,
            positive.connection_id,
            positive.physical_field,
        )
        for language, text in (
            ("es", positive.descriptions.es),
            ("en", positive.descriptions.en),
        ):
            first = _ranked_search(stack, text)
            second = _ranked_search(stack, text)
            if first != second:
                deterministic_replay_stable = False
            ranked_identities = tuple(identity for identity, _score in first)
            all_observed_identities.update(ranked_identities)
            rank = ranked_identities.index(expected) + 1 if expected in ranked_identities else None
            ranks.append(rank)
            case_id = f"{positive.id}:{language}"
            if rank != 1:
                actual = ranked_identities[0] if ranked_identities else None
                top_1_errors.append(f"{case_id}:expected={expected}:actual={actual}")
            if rank is None or rank > 20:
                false_negatives.append(f"{case_id}:expected={expected}:rank={rank}")

    false_positives: list[str] = []
    for negative in corpus.true_negatives:
        ranked = _ranked_search(stack, negative.text)
        all_observed_identities.update(identity for identity, _score in ranked)
        if ranked:
            false_positives.append(f"{negative.id}:results={len(ranked)}")

    ambiguity_misses: list[str] = []
    ambiguity_correct = 0
    for ambiguity in corpus.critical_ambiguities:
        ranked = _ranked_search(stack, ambiguity.text)
        all_observed_identities.update(identity for identity, _score in ranked)
        if len(ranked) >= 2:
            ambiguity_correct += 1
        else:
            ambiguity_misses.append(f"{ambiguity.id}:alternatives={len(ranked)}")

    positive_count = len(ranks)
    top_1_correct = sum(rank == 1 for rank in ranks)
    top_3_correct = sum(rank is not None and rank <= 3 for rank in ranks)
    recall_at_20_correct = sum(rank is not None and rank <= 20 for rank in ranks)
    reciprocal_rank_sum = sum(0.0 if rank is None else 1.0 / rank for rank in ranks)
    negative_correct = len(corpus.true_negatives) - len(false_positives)
    metrics = QueryStudioRetrievalMetrics(
        positive_cases=positive_count,
        top_1_correct=top_1_correct,
        top_1_accuracy=top_1_correct / positive_count,
        top_3_correct=top_3_correct,
        top_3_recall=top_3_correct / positive_count,
        recall_at_20_correct=recall_at_20_correct,
        recall_at_20=recall_at_20_correct / positive_count,
        reciprocal_rank_sum=reciprocal_rank_sum,
        mean_reciprocal_rank=reciprocal_rank_sum / positive_count,
        negative_cases=len(corpus.true_negatives),
        negative_no_match_correct=negative_correct,
        no_match_specificity=negative_correct / len(corpus.true_negatives),
        ambiguity_cases=len(corpus.critical_ambiguities),
        ambiguity_correct=ambiguity_correct,
        ambiguity_recall=ambiguity_correct / len(corpus.critical_ambiguities),
    )
    ungoverned = tuple(
        f"{logical_field}@{connection_id}->{physical_field}"
        for logical_field, connection_id, physical_field in sorted(
            all_observed_identities - labelled_identities
        )
    )
    passed = (
        keyset_stable
        and deterministic_replay_stable
        and metrics.top_1_accuracy >= _TOP_1_THRESHOLD
        and metrics.top_3_recall >= _TOP_3_THRESHOLD
        and metrics.mean_reciprocal_rank >= _MRR_THRESHOLD
        and metrics.recall_at_20 >= _RECALL_AT_20_THRESHOLD
        and metrics.no_match_specificity >= _SPECIFICITY_THRESHOLD
        and metrics.ambiguity_recall >= _AMBIGUITY_RECALL_THRESHOLD
        and not false_negatives
        and not false_positives
        and not ambiguity_misses
        and not ungoverned
    )
    return QueryStudioDeterministicEvaluationReport(
        fixture_notice=corpus.fixture_notice,
        matcher_version=RECORDED_MATCHER_VERSION,
        corpus_sha256=hashlib.sha256(corpus_bytes).hexdigest(),
        catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        registry_fingerprint=stack.registry.registry.fingerprint,
        governed_mapping_count=len(governed_pairs),
        catalog_asset_count=_catalog_asset_count(catalog_path),
        keyset_page_sizes=_PAGE_SIZES,
        keyset_traversal_counts=tuple(traversal_counts),
        keyset_stable=keyset_stable,
        deterministic_replay_stable=deterministic_replay_stable,
        metrics=metrics,
        top_1_errors=tuple(top_1_errors),
        false_negatives_at_20=tuple(false_negatives),
        false_positives=tuple(false_positives),
        ambiguity_misses=tuple(ambiguity_misses),
        ungoverned_executable_results=ungoverned,
        passed=passed,
    )


def write_query_studio_matching_report(
    report: QueryStudioDeterministicEvaluationReport,
    repository_root: Path,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Write report files only beneath the explicit repository root."""

    root = repository_root.resolve()
    resolved_json = _safe_output(root, json_path, ".json")
    resolved_markdown = _safe_output(root, markdown_path, ".md")
    resolved_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_markdown.parent.mkdir(parents=True, exist_ok=True)
    resolved_json.write_bytes(report.json_bytes())
    resolved_markdown.write_text(report.markdown(), encoding="utf-8")


def _build_stack(manifest_path: Path, catalog_path: Path) -> _SearchStack:
    scope = SemanticRegistryScope(
        workspace_id=_EVALUATION_WORKSPACE,
        catalog_scope="synthetic-demo",
        registry_id="synthetic_enterprise",
    )
    registry = RecordedGovernedSemanticRegistry(manifest_path, scope).load()
    static_registry = _StaticRegistry(registry)
    facts = RecordedGovernedBindingFactsSearch(registry, catalog_path)
    search = RegistryAwareGovernedFieldSearch(registry=static_registry, facts=facts)
    return _SearchStack(
        registry=registry,
        service=SearchGovernedFields(registry=static_registry, search=search),
    )


def _traverse(
    stack: _SearchStack,
    page_size: int,
) -> tuple[tuple[str, ...], set[tuple[str, str, str]]]:
    identities: list[str] = []
    observed_fields: set[tuple[str, str, str]] = set()
    request = GovernedFieldSearchRequest(
        scope=stack.registry.scope,
        page_size=page_size,
    )
    seen_keys: set[tuple[object, ...]] = set()
    while True:
        page = stack.service.execute(request)
        identities.extend(item.binding_id for item in page.items)
        observed_fields.update(
            (
                item.logical_field.root,
                item.locator.asset.connection_id.root,
                item.physical_field.root,
            )
            for item in page.items
        )
        if page.next_key is None:
            return tuple(identities), observed_fields
        key = page.next_key.sort_tuple
        if key in seen_keys:
            raise ValueError("query studio keyset did not advance")
        seen_keys.add(key)
        request = GovernedFieldSearchRequest(
            scope=stack.registry.scope,
            page_size=page_size,
            after=page.next_key,
            expected_scope=page.scope,
        )


def _ranked_search(
    stack: _SearchStack,
    text: str,
) -> tuple[tuple[tuple[str, str, str], int], ...]:
    page = stack.service.execute(
        GovernedFieldSearchRequest(
            scope=stack.registry.scope,
            query=DescriptionQuery(text),
            page_size=50,
        )
    )
    return tuple(
        (
            (
                item.logical_field.root,
                item.locator.asset.connection_id.root,
                item.physical_field.root,
            ),
            item.signals.total,
        )
        for item in page.items
    )


def _catalog_asset_count(path: Path) -> int:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("query studio catalog root must be an object")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError("query studio catalog assets must be a list")
    return len(assets)


def _safe_output(root: Path, configured: Path, suffix: str) -> Path:
    candidate = configured if configured.is_absolute() else root / configured
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved.suffix != suffix:
        raise ValueError("query studio evaluation output must remain inside the repository")
    return resolved


__all__ = [
    "QueryStudioDeterministicEvaluationReport",
    "QueryStudioRetrievalMetrics",
    "evaluate_recorded_query_studio_matching",
    "write_query_studio_matching_report",
]
