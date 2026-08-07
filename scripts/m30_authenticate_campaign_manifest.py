"""Authenticate one exact external M30 manifest; never authorize campaign execution or release."""

from __future__ import annotations

import argparse
from pathlib import Path

from schemabridge.application.ports.production_evidence import ProductionEvidenceError
from schemabridge.bootstrap import (
    build_m30_campaign_manifest_authenticator,
    build_m30_manifest_authentication_report_writer,
)

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--attestation-bundle", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    output = arguments.output_directory or root / ".local/m30/manifest-authentication"
    try:
        report = build_m30_campaign_manifest_authenticator(
            repository_root=root,
            manifest_path=arguments.manifest,
            attestation_bundle_path=arguments.attestation_bundle,
        ).execute()
        json_path, markdown_path = build_m30_manifest_authentication_report_writer(
            repository_root=root
        ).write(report, output)
    except ProductionEvidenceError as error:
        print(f"M30 manifest authentication failed closed: {error.code.value}")
        return 3
    print(
        f"M30 Phase 1a {report.state.value}: "
        f"campaign_executable={str(report.campaign_executable).lower()} "
        f"release_decision={report.release_decision.value} "
        f"external_controls_passed={report.external_controls_passed}"
    )
    print(f"Artifacts written: {json_path.name}, {markdown_path.name}")
    return 0 if report.workflow_attested_manifest_authenticated else 2


if __name__ == "__main__":
    raise SystemExit(main())
