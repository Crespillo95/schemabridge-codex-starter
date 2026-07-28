from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

import schemabridge.adapters.evaluation.query_studio_live as live_evaluation
from schemabridge.bootstrap import QueryStudioRuntimeServices, build_query_studio_runtime
from schemabridge.config import Settings
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
HISTORICAL_CORPUS_SHA256 = "6bb72e6efd4801bd5042e15a29616d634118d62e5f8a83b2f19d4691b1502ece"
FROZEN_COMPOSITE_CORPUS_SHA256 = "a03d96b6ce0ed7172ffcf6a074f0d755dc62f2709c10fdce7607e1989614b1ba"
CORPUS_RELATIVE_PATH = Path("demo/ground_truth/query_studio_matching.yml")
HOLDOUT_RELATIVE_PATH = Path("demo/ground_truth/query_studio_live_holdout_v1.yml")


def _runtime() -> QueryStudioRuntimeServices:
    principal = AuthenticatedPrincipal(
        actor_id="query-studio-live-holdout-test",
        workspace_id="query-studio-deterministic-evaluation",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    settings = Settings(
        _env_file=None,
        OPENAI_API_KEY=None,
        DATABASE_URL=None,
        SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="fake",
        SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
            "query-studio-live-holdout-signing-key-with-diversity-2026"
        ),
    )
    return build_query_studio_runtime(
        principal=principal,
        repository_root=ROOT,
        settings=settings,
    )


def _copy_live_fixtures(tmp_path: Path) -> None:
    for relative_path in (CORPUS_RELATIVE_PATH, HOLDOUT_RELATIVE_PATH):
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative_path).read_bytes())


def _load_holdout_payload(tmp_path: Path) -> dict[str, Any]:
    payload = yaml.safe_load((tmp_path / HOLDOUT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_holdout_payload(tmp_path: Path, payload: dict[str, Any]) -> None:
    (tmp_path / HOLDOUT_RELATIVE_PATH).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "plan_version",
    (
        "m27-cheapest-first-campaign-v7",
        "m27-cheapest-first-campaign-v8",
        "m27-cheapest-first-campaign-v9",
        "m27-cheapest-first-campaign-v10",
        "m27-cheapest-first-campaign-v11",
    ),
)
def test_v7_through_v11_load_exactly_two_frozen_paraphrases_per_core_case(
    plan_version: live_evaluation.RetainedQualificationPlanVersion,
) -> None:
    corpus, _digest = live_evaluation._load_synthetic_corpus(
        ROOT,
        plan_version=plan_version,
    )

    assert len(corpus.core_queries) == 5
    assert {len(case.paraphrases) for case in corpus.core_queries} == {2}
    assert (
        len({paraphrase.text for case in corpus.core_queries for paraphrase in case.paraphrases})
        == 10
    )


@pytest.mark.parametrize(
    "plan_version",
    (
        "m27-cheapest-first-campaign-v4",
        "m27-cheapest-first-campaign-v5",
        "m27-cheapest-first-campaign-v6",
    ),
)
def test_retained_plans_load_the_unchanged_historical_corpus(
    plan_version: live_evaluation.RetainedQualificationPlanVersion,
) -> None:
    corpus, digest = live_evaluation._load_synthetic_corpus(
        ROOT,
        plan_version=plan_version,
    )

    assert digest == HISTORICAL_CORPUS_SHA256
    assert len(corpus.core_queries) == 5
    assert all(not case.paraphrases for case in corpus.core_queries)


def test_v8_through_v11_reuse_the_exact_frozen_v7_composite_corpus_digest() -> None:
    v7_corpus, v7_digest = live_evaluation._load_synthetic_corpus(
        ROOT, plan_version="m27-cheapest-first-campaign-v7"
    )
    v8_corpus, v8_digest = live_evaluation._load_synthetic_corpus(
        ROOT, plan_version="m27-cheapest-first-campaign-v8"
    )
    v9_corpus, v9_digest = live_evaluation._load_synthetic_corpus(
        ROOT, plan_version="m27-cheapest-first-campaign-v9"
    )
    v10_corpus, v10_digest = live_evaluation._load_synthetic_corpus(
        ROOT, plan_version="m27-cheapest-first-campaign-v10"
    )
    v11_corpus, v11_digest = live_evaluation._load_synthetic_corpus(
        ROOT, plan_version="m27-cheapest-first-campaign-v11"
    )

    assert (
        v7_digest
        == v8_digest
        == v9_digest
        == v10_digest
        == v11_digest
        == FROZEN_COMPOSITE_CORPUS_SHA256
    )
    assert v7_corpus == v8_corpus == v9_corpus == v10_corpus == v11_corpus


def test_core_repetitions_cycle_original_then_the_two_holdouts() -> None:
    corpus, _digest = live_evaluation._load_synthetic_corpus(
        ROOT,
        plan_version="m27-cheapest-first-campaign-v11",
    )

    for case in corpus.core_queries:
        expected = (
            (case.text, live_evaluation.UserLanguage(case.language)),
            *(
                (
                    paraphrase.text,
                    live_evaluation.UserLanguage(paraphrase.language),
                )
                for paraphrase in case.paraphrases
            ),
        )
        assert tuple(
            live_evaluation._core_repetition_input(case, repetition) for repetition in range(1, 7)
        ) == (*expected, *expected)

    with pytest.raises(ValueError, match="core repetition must be positive"):
        live_evaluation._core_repetition_input(corpus.core_queries[0], 0)


def test_v11_holdout_rejects_duplicate_case_id(tmp_path: Path) -> None:
    _copy_live_fixtures(tmp_path)
    payload = _load_holdout_payload(tmp_path)
    payload["core_paraphrases"][-1]["id"] = payload["core_paraphrases"][0]["id"]
    _write_holdout_payload(tmp_path, payload)

    with pytest.raises(
        ValueError,
        match="live holdout must be synthetic and globally unique",
    ):
        live_evaluation._load_synthetic_corpus(
            tmp_path,
            plan_version="m27-cheapest-first-campaign-v11",
        )


def test_v11_holdout_rejects_missing_core_case(tmp_path: Path) -> None:
    _copy_live_fixtures(tmp_path)
    payload = _load_holdout_payload(tmp_path)
    payload["core_paraphrases"][-1]["id"] = "synthetic-unexpected-core-case"
    _write_holdout_payload(tmp_path, payload)

    with pytest.raises(
        ValueError,
        match="live holdout does not cover the exact core corpus",
    ):
        live_evaluation._load_synthetic_corpus(
            tmp_path,
            plan_version="m27-cheapest-first-campaign-v11",
        )


def test_v11_holdout_text_mutation_changes_composite_digest(tmp_path: Path) -> None:
    _copy_live_fixtures(tmp_path)
    payload = _load_holdout_payload(tmp_path)
    payload["core_paraphrases"][0]["paraphrases"][0]["text"] += " sintético"
    _write_holdout_payload(tmp_path, payload)

    corpus, digest = live_evaluation._load_synthetic_corpus(
        tmp_path,
        plan_version="m27-cheapest-first-campaign-v11",
    )

    assert digest != FROZEN_COMPOSITE_CORPUS_SHA256
    assert corpus.core_queries[0].paraphrases[0].text.endswith(" sintético")


def test_fake_baseline_is_equivalent_across_every_v11_variant() -> None:
    corpus, _digest = live_evaluation._load_synthetic_corpus(
        ROOT,
        plan_version="m27-cheapest-first-campaign-v11",
    )
    runtime = _runtime()
    baseline = live_evaluation._build_fake_baseline(runtime, corpus.core_queries)

    assert set(baseline) == {case.id for case in corpus.core_queries}
    for case in corpus.core_queries:
        observed: set[str] = set()
        for repetition in range(1, 4):
            text, language = live_evaluation._core_repetition_input(
                case,
                repetition,
            )
            preview = live_evaluation._prepare(runtime, text, language)
            confirmed = live_evaluation._confirm(
                runtime,
                text,
                language,
                preview,
            )
            assert confirmed is not None
            observed.add(
                live_evaluation.query_studio_fingerprint(
                    confirmed.validated_request.model_dump(mode="json")
                )
            )
        assert observed == {baseline[case.id].request_fingerprint}
