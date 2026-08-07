from __future__ import annotations

from pathlib import Path

import yaml
from scripts import m27_browser_acceptance_runtime

from schemabridge.adapters.query_studio.recorded_catalog_stream import (
    RECORDED_CATALOG_CONNECTION_ID,
)

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> dict[str, object]:
    value = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_query_studio_corpus_covers_every_governed_mapping_in_two_languages() -> None:
    registry = _load("demo/ground_truth/registries/synthetic_enterprise.yml")
    corpus = _load("demo/ground_truth/query_studio_matching.yml")

    mapping_set = registry["mapping_set"]
    assert isinstance(mapping_set, dict)
    mappings = mapping_set["mappings"]
    positives = corpus["positive_mappings"]
    assert isinstance(mappings, list)
    assert isinstance(positives, list)

    expected = {
        (
            item["mapping"]["logical_field"],
            "warehouse-primary",
            item["mapping"]["physical_field"],
        )
        for item in mappings
    }
    observed = {
        (
            item["logical_field"],
            item["connection_id"],
            item["physical_field"],
        )
        for item in positives
    }

    assert len(mappings) == 31
    assert observed == expected
    assert len(observed) == len(positives)
    assert all(
        set(item["descriptions"]) == {"es", "en"}
        and all(
            isinstance(text, str) and 3 <= len(text.encode("utf-8")) <= 300
            for text in item["descriptions"].values()
        )
        for item in positives
    )


def test_corpus_and_recorded_adapter_share_the_browser_connection_identity() -> None:
    corpus = _load("demo/ground_truth/query_studio_matching.yml")
    positives = corpus["positive_mappings"]
    assert isinstance(positives, list)

    assert RECORDED_CATALOG_CONNECTION_ID == (m27_browser_acceptance_runtime.PRIMARY_CONNECTION_ID)
    assert {item["connection_id"] for item in positives} == {
        m27_browser_acceptance_runtime.PRIMARY_CONNECTION_ID
    }
    assert m27_browser_acceptance_runtime.SECONDARY_CONNECTION_ID == "warehouse-shadow"


def test_query_studio_corpus_has_balanced_negatives_and_bounded_adversarial_cases() -> None:
    corpus = _load("demo/ground_truth/query_studio_matching.yml")
    positives = corpus["positive_mappings"]
    negatives = corpus["true_negatives"]
    adversarial = corpus["adversarial_inputs"]
    critical = corpus["critical_ambiguities"]
    queries = corpus["core_queries"]
    assert isinstance(positives, list)
    assert isinstance(negatives, list)
    assert isinstance(adversarial, list)
    assert isinstance(critical, list)
    assert isinstance(queries, list)

    assert len(negatives) >= len(positives)
    assert len({item["id"] for item in negatives}) == len(negatives)
    assert len(adversarial) >= 10
    assert {item["expected"] for item in critical} == {"ambiguous"}
    assert len(queries) == 5
    assert max(len(item["expected_models"]) for item in queries) == 3
    assert max(len(item["expected_joins"]) for item in queries) == 2
    assert all(len(item["text"].encode("utf-8")) <= 8_192 for item in negatives + adversarial)


def test_query_studio_corpus_is_explicitly_synthetic_and_has_no_live_secret_material() -> None:
    path = ROOT / "demo/ground_truth/query_studio_matching.yml"
    text = path.read_text(encoding="utf-8")

    assert "Synthetic" in text
    assert "proprietary data" in text
    assert "OPENAI_API_KEY" not in text
    assert "sk-proj-" not in text
    assert "BEGIN PRIVATE KEY" not in text
