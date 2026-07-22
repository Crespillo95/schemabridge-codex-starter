#!/usr/bin/env python3
"""Create persistent local-only signing material for authenticated DataHub quickstart."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from pathlib import Path

REQUIRED_KEYS = ("DATAHUB_TOKEN_SERVICE_SIGNING_KEY", "DATAHUB_TOKEN_SERVICE_SALT")


def ensure_secrets(path: Path) -> bool:
    """Create the secret file once and validate existing files without revealing values."""

    if path.exists():
        values = {
            key: value
            for line in path.read_text().splitlines()
            for key, separator, value in (line.partition("="),)
            if separator
        }
        if any(not values.get(key) for key in REQUIRED_KEYS):
            raise SystemExit(f"Existing {path} is missing required signing material.")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"{key}={secrets.token_urlsafe(48)}" for key in REQUIRED_KEYS) + "\n"
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    created = ensure_secrets(args.path)
    action = "Created" if created else "Reused"
    print(f"{action} local DataHub signing material at {args.path}; values not displayed.")


if __name__ == "__main__":
    os.umask(0o077)
    main()
