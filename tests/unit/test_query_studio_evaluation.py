from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import schemabridge.adapters.evaluation.query_studio_matching as matching_evaluation
from schemabridge.adapters.evaluation.query_studio_matching import (
    evaluate_recorded_query_studio_matching,
    write_query_studio_matching_report,
)
from schemabridge.adapters.evaluation.yaml_loader import load_unique_yaml

ROOT = Path(__file__).resolve().parents[2]


def test_deterministic_query_studio_evaluation_meets_every_retrieval_gate() -> None:
    report = evaluate_recorded_query_studio_matching(ROOT)
    metrics = report.metrics

    assert report.passed is True
    assert report.governed_mapping_count == 31
    assert report.keyset_page_sizes == (1, 17, 50)
    assert report.keyset_traversal_counts == (31, 31, 31)
    assert report.keyset_stable is True
    assert report.deterministic_replay_stable is True
    assert metrics.positive_cases == 62
    assert metrics.top_1_accuracy >= 0.85
    assert metrics.top_3_recall == 1.0
    assert metrics.mean_reciprocal_rank >= 0.90
    assert metrics.recall_at_20 == 1.0
    assert metrics.negative_cases == 31
    assert metrics.no_match_specificity == 1.0
    assert metrics.ambiguity_cases == 6
    assert metrics.ambiguity_recall == 1.0
    assert report.false_negatives_at_20 == ()
    assert report.false_positives == ()
    assert report.ambiguity_misses == ()
    assert report.ungoverned_executable_results == ()
    assert report.provider_calls == 0
    assert report.input_tokens == 0
    assert report.output_tokens == 0
    assert report.calculated_cost_eur == 0.0


def test_v45_field_match_regressions_rank_on_the_canonical_browser_connection() -> None:
    corpus_path = ROOT / "demo/ground_truth/query_studio_matching.yml"
    corpus = matching_evaluation._MatchingCorpus.model_validate(
        load_unique_yaml(corpus_path.read_text(encoding="utf-8"))
    )
    stack = matching_evaluation._build_stack(
        ROOT / "demo/ground_truth/registries/manifest.yml",
        ROOT / "demo/datahub/catalog_snapshot.json",
    )
    cases = {
        item.id: item
        for item in corpus.positive_mappings
        if item.id in {"product-active", "order-total"}
    }

    assert set(cases) == {"product-active", "order-total"}
    for case in cases.values():
        expected = (
            case.logical_field,
            "warehouse-primary",
            case.physical_field,
        )
        ranked = tuple(
            identity
            for identity, _score in matching_evaluation._ranked_search(
                stack,
                case.descriptions.es,
            )
        )
        assert expected in ranked[:3]


def test_deterministic_report_is_byte_stable_and_round_trips(tmp_path: Path) -> None:
    first = evaluate_recorded_query_studio_matching(ROOT)
    second = evaluate_recorded_query_studio_matching(ROOT)

    assert first == second
    assert first.json_bytes() == second.json_bytes()
    assert first.markdown() == second.markdown()

    json_path = tmp_path / "m27-evaluation.json"
    markdown_path = tmp_path / "m27-evaluation.md"
    write_query_studio_matching_report(
        first,
        tmp_path,
        json_path=json_path,
        markdown_path=markdown_path,
    )

    assert json.loads(json_path.read_text(encoding="utf-8")) == first.model_dump(mode="json")
    assert markdown_path.read_text(encoding="utf-8") == first.markdown()


def test_5434_asset_catalog_does_not_cap_governed_results_or_promote_decoys(
    tmp_path: Path,
) -> None:
    catalog_path = ROOT / "demo/datahub/catalog_snapshot.json"
    payload = cast(
        dict[str, object],
        json.loads(catalog_path.read_text(encoding="utf-8")),
    )
    raw_assets = payload["assets"]
    assert isinstance(raw_assets, list)
    existing_count = len(raw_assets)
    for index in range(existing_count, 5_434):
        raw_assets.append(
            {
                "dataset": f"ungoverned_{index:04d}.table_{index:04d}",
                "description": "Synthetic ungoverned decoy table.",
                "fields": [
                    {
                        "field_path": "customer_id",
                        "native_type": "text",
                        "description": "Synthetic homonymous field without governed evidence.",
                        "nullable": True,
                        "is_part_of_key": False,
                        "tags": ["synthetic", "ungoverned"],
                        "glossary_terms": [],
                    }
                ],
            }
        )
    scaled_catalog = tmp_path / "catalog-5434.json"
    scaled_catalog.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    report = evaluate_recorded_query_studio_matching(
        ROOT,
        catalog_snapshot_path=scaled_catalog,
    )

    assert report.passed is True
    assert report.catalog_asset_count == 5_434
    assert report.governed_mapping_count == 31
    assert report.keyset_traversal_counts == (31, 31, 31)
    assert report.metrics.recall_at_20 == 1.0
    assert report.metrics.no_match_specificity == 1.0
    assert report.ungoverned_executable_results == ()


@pytest.mark.parametrize(
    ("json_path", "markdown_path"),
    [
        (Path("../outside.json"), Path("inside.md")),
        (Path("inside.json"), Path("../outside.md")),
        (Path("inside.txt"), Path("inside.md")),
        (Path("inside.json"), Path("inside.txt")),
    ],
)
def test_report_writer_rejects_out_of_scope_or_wrong_suffix_outputs(
    tmp_path: Path,
    json_path: Path,
    markdown_path: Path,
) -> None:
    report = evaluate_recorded_query_studio_matching(ROOT)

    with pytest.raises(ValueError, match="must remain inside"):
        write_query_studio_matching_report(
            report,
            tmp_path,
            json_path=json_path,
            markdown_path=markdown_path,
        )
