#!/usr/bin/env python3
"""Remove DataHub credential representations from streamed CLI output."""

from __future__ import annotations

import os
import re
import sys


def redact(text: str, token: str) -> str:
    """Redact both a full token and DataHub's prefix/suffix masked representation."""

    sanitized = text.replace(token, "***REDACTED***") if token else text
    return re.sub(r"(?i)(with token:\s*)\S+", r"\1***REDACTED***", sanitized)


def main() -> None:
    token = os.environ.get("DATAHUB_GMS_TOKEN", "")
    for line in sys.stdin:
        sys.stdout.write(redact(line, token))


if __name__ == "__main__":
    main()
