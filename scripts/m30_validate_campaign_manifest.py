"""Validate M30 frozen inputs before attestation; this is not authentication or release."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from schemabridge.application.ports.production_evidence import ProductionEvidenceError
from schemabridge.bootstrap import build_m30_campaign_manifest_validator
from schemabridge.domain.production_campaign import M30CampaignManifest

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--print-json-schema", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.print_json_schema:
        if arguments.manifest is not None:
            print("--print-json-schema cannot be combined with --manifest")
            return 3
        print(json.dumps(M30CampaignManifest.model_json_schema(), indent=2, sort_keys=True))
        return 0
    if arguments.manifest is None:
        print("M30 campaign manifest validation failed closed: --manifest is required")
        return 3
    root = arguments.repository_root.resolve()
    try:
        validation = build_m30_campaign_manifest_validator(
            repository_root=root,
            manifest_path=arguments.manifest,
        ).execute(verified_at=datetime.now(UTC))
    except ProductionEvidenceError as error:
        print(f"M30 campaign manifest validation failed closed: {error.code.value}")
        return 3
    if validation.blocking_reasons:
        print(
            "M30 campaign manifest validation blocked: "
            + ",".join(item.value for item in validation.blocking_reasons)
        )
        return 2
    if validation.loaded is None:
        print("M30 campaign manifest validation failed closed: m30_manifest_invalid")
        return 3
    print(
        "M30 campaign manifest is canonical and candidate-bound but not authenticated: "
        f"sha256={validation.loaded.raw_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
