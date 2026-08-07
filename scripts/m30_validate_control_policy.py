"""Bind an external M30 control policy; never accept a control or authorize campaign I/O."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from schemabridge.application.ports.production_evidence import ProductionEvidenceError
from schemabridge.bootstrap import (
    build_m30_control_policy_report_writer,
    build_m30_control_policy_validator,
)
from schemabridge.domain.production_campaign_receipts import M30ControlPolicy

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-attestation-bundle", type=Path)
    parser.add_argument("--control-policy", type=Path)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--schema", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.schema:
        print(json.dumps(M30ControlPolicy.model_json_schema(), indent=2, sort_keys=True))
        return 0
    if (
        arguments.manifest is None
        or arguments.manifest_attestation_bundle is None
        or arguments.control_policy is None
    ):
        print(
            "M30 control-policy validation failed closed: manifest, attestation bundle and "
            "control policy are required"
        )
        return 3
    root = arguments.repository_root.resolve()
    output = arguments.output_directory or root / ".local/m30/control-policy"
    try:
        report = build_m30_control_policy_validator(
            repository_root=root,
            manifest_path=arguments.manifest,
            attestation_bundle_path=arguments.manifest_attestation_bundle,
            control_policy_path=arguments.control_policy,
        ).execute()
        json_path, markdown_path = build_m30_control_policy_report_writer(
            repository_root=root
        ).write(report, output)
    except ProductionEvidenceError as error:
        print(f"M30 control-policy validation failed closed: {error.code.value}")
        return 3
    print(
        f"M30 Phase 1b policy {report.state.value}: "
        f"policy_bound={str(report.policy_bound_to_authenticated_manifest).lower()} "
        f"external_controls_passed={report.external_controls_passed} "
        f"campaign_executable={str(report.campaign_executable).lower()} "
        f"release_decision={report.release_decision.value}"
    )
    print(f"Artifacts written: {json_path.name}, {markdown_path.name}")
    return 0 if report.policy_bound_to_authenticated_manifest else 2


if __name__ == "__main__":
    raise SystemExit(main())
