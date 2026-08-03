"""Materialize the offline M30 candidate-readiness NO-GO report."""

from __future__ import annotations

import argparse
from pathlib import Path

from schemabridge.application.ports.production_evidence import ProductionEvidenceError
from schemabridge.bootstrap import (
    build_m30_readiness_assessor,
    build_m30_readiness_report_writer,
)

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write the expected blocked preparation report and return zero without changing it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    output = (
        arguments.output_directory
        if arguments.output_directory is not None
        else root / ".local/m30"
    )
    try:
        report = build_m30_readiness_assessor(repository_root=root).execute()
        json_path, markdown_path = build_m30_readiness_report_writer(repository_root=root).write(
            report, output
        )
    except ProductionEvidenceError as error:
        print(f"M30 preflight failed closed: {error.code.value}")
        return 3
    print(
        f"M30 preflight {report.preflight_state.value}: "
        f"campaign_executable={str(report.campaign_executable).lower()} "
        f"release_decision={report.release_decision.value}"
    )
    print(f"Artifacts written: {json_path.name}, {markdown_path.name}")
    return 0 if report.campaign_executable or arguments.report_only else 2


if __name__ == "__main__":
    raise SystemExit(main())
