"""Typed fail-closed process wrapper for the Streamlit web runtime."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from schemabridge.bootstrap import WebProcessRuntime, build_web_process_runtime

_ExecProcess = Callable[[str, list[str]], object]
_SAFE_FAILURE = (
    "web_runtime_unavailable: configuration, authentication, schema, or process launch failed"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemabridge-web",
        description="Preflight and start the governed Streamlit web runtime.",
    )
    parser.add_argument(
        "--probe-ready",
        action="store_true",
        help="Repeat managed configuration, authentication, and schema checks, then exit.",
    )
    return parser


def command(
    argv: Sequence[str] | None = None,
    *,
    runtime: WebProcessRuntime | None = None,
    exec_process: _ExecProcess = os.execvp,
) -> int:
    """Preflight once, or replace this process with the fixed Streamlit command."""

    arguments = _parser().parse_args(argv)
    try:
        resolved = runtime or build_web_process_runtime()
        if arguments.probe_ready:
            resolved.require_probe_ready()
            return 0
        resolved.require_ready()
        executable = resolved.streamlit_argv[0]
        exec_process(executable, list(resolved.streamlit_argv))
    except Exception:
        print(_SAFE_FAILURE, file=sys.stderr)
        return 1
    print(_SAFE_FAILURE, file=sys.stderr)
    return 1


def main() -> None:
    raise SystemExit(command())


if __name__ == "__main__":
    main()
