"""Generate deterministic M27 governed-field retrieval evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from schemabridge.adapters.evaluation.query_studio_matching import (
    evaluate_recorded_query_studio_matching,
    write_query_studio_matching_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate synthetic deterministic Query Studio matching."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("reports/m27-query-studio-deterministic-evaluation.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("reports/m27-query-studio-deterministic-evaluation.md"),
    )
    arguments = parser.parse_args()
    report = evaluate_recorded_query_studio_matching(arguments.repository_root)
    write_query_studio_matching_report(
        report,
        arguments.repository_root,
        json_path=arguments.json,
        markdown_path=arguments.markdown,
    )
    metrics = report.metrics
    print(
        "M27 deterministic retrieval "
        f"{'PASS' if report.passed else 'FAIL'}: "
        f"top1={metrics.top_1_accuracy:.6f} "
        f"top3={metrics.top_3_recall:.6f} "
        f"mrr={metrics.mean_reciprocal_rank:.6f} "
        f"recall20={metrics.recall_at_20:.6f} "
        f"specificity={metrics.no_match_specificity:.6f} "
        f"ambiguity={metrics.ambiguity_recall:.6f}"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
