"""Download, verify, and minimally harden the pinned DataHub quickstart compose file."""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

import yaml


class QuickstartPreparationError(RuntimeError):
    """The pinned upstream quickstart cannot be prepared safely."""


def prepare_compose(source: bytes, *, expected_sha256: str) -> str:
    """Verify upstream bytes and enable only the M04 environment controls."""

    observed_sha256 = hashlib.sha256(source).hexdigest()
    if observed_sha256 != expected_sha256:
        raise QuickstartPreparationError(
            "pinned DataHub quickstart checksum mismatch; refusing unreviewed compose input"
        )

    payload = yaml.safe_load(source)
    if not isinstance(payload, dict):
        raise QuickstartPreparationError("DataHub quickstart compose must be a mapping")
    services = payload.get("services")
    if not isinstance(services, dict):
        raise QuickstartPreparationError("DataHub quickstart compose has no services mapping")
    gms = services.get("datahub-gms-quickstart")
    if not isinstance(gms, dict):
        raise QuickstartPreparationError("pinned DataHub GMS service is missing")
    environment = gms.get("environment")
    if not isinstance(environment, dict):
        raise QuickstartPreparationError("pinned DataHub GMS environment is missing")

    environment["LOGICAL_MODELS_ENABLED"] = "true"
    environment["METADATA_SERVICE_AUTH_ENABLED"] = "true"

    for service in services.values():
        if not isinstance(service, dict):
            continue
        ports = service.get("ports")
        if not isinstance(ports, list):
            continue
        for port in ports:
            if isinstance(port, dict) and "published" in port:
                port["host_ip"] = "127.0.0.1"

    rendered = yaml.safe_dump(payload, sort_keys=False)
    return (
        "# Generated from the checksum-pinned DataHub v1.6.0 official quickstart.\n"
        "# Do not edit; rerun scripts/prepare_datahub_quickstart.py.\n"
        f"{rendered}"
    )


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "SchemaBridge-M04/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = prepare_compose(download(args.url), expected_sha256=args.sha256)
    output.write_text(rendered, encoding="utf-8")
    print(f"Prepared pinned DataHub quickstart at {output}")


if __name__ == "__main__":
    main()
