#!/usr/bin/env python3
"""Secret-free HTTP smoke test for a local or public Streamlit deployment."""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Deployment base URL")
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    arguments = parser.parse_args()
    base_url = arguments.url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        parser.error("--url must start with http:// or https://")

    last_error = "not attempted"
    for attempt in range(1, arguments.attempts + 1):
        try:
            health = _read(f"{base_url}/_stcore/health")
            root = _read(f"{base_url}/")
            if health.strip() != b"ok":
                raise RuntimeError("health response was not 'ok'")
            if b"streamlit" not in root.lower():
                raise RuntimeError("root response was not a Streamlit application")
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            last_error = str(error)
            if attempt < arguments.attempts:
                time.sleep(arguments.interval_seconds)
                continue
            print(
                f"deployment smoke failed after {attempt} attempts: {last_error}", file=sys.stderr
            )
            return 1
        print(f"deployment smoke passed: {base_url}")
        return 0
    return 1


def _read(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "schemabridge-smoke/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read(1_000_000)


if __name__ == "__main__":
    raise SystemExit(main())
