"""Static integrity checks for the generated starter kit."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    """Raise a validation error with a consistent message."""

    raise RuntimeError(message)


def is_versioned_mapping(value: object) -> bool:
    """Return whether a YAML root declares a positive integer schema version."""

    if not isinstance(value, dict):
        return False
    version_key = "version" if "version" in value else "format_version"
    version = value.get(version_key)
    return isinstance(version, int) and not isinstance(version, bool) and version > 0


def main() -> int:
    """Validate configuration syntax, milestone coverage, and YAML fixtures."""

    with (ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    with (ROOT / ".codex/config.toml").open("rb") as stream:
        codex_config = tomllib.load(stream)

    if pyproject["project"]["name"] != "schemabridge":
        fail("Unexpected project name")
    if codex_config.get("model") != "gpt-5.6-sol":
        fail("Codex default model is not gpt-5.6-sol")

    plans = sorted((ROOT / "plans").glob("M[0-1][0-9]_*.md"))
    prompts = sorted((ROOT / "prompts").glob("M[0-1][0-9]_*.md"))
    if len(plans) != 20:
        fail(f"Expected 20 milestone plans, found {len(plans)}")
    if len(prompts) != 20:
        fail(f"Expected 20 milestone prompts, found {len(prompts)}")
    if [p.name.split("_", 1)[0] for p in plans] != [p.name.split("_", 1)[0] for p in prompts]:
        fail("Plan and prompt milestone IDs do not match")

    for path in sorted((ROOT / "demo" / "ground_truth").glob("*.yml")):
        with path.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        if not is_versioned_mapping(loaded):
            fail(f"Invalid ground-truth file: {path.relative_to(ROOT)}")

    required = [
        "START_HERE_ES.md",
        "AGENTS.md",
        "LICENSE",
        "plans/MASTER_PLAN.md",
        "tasks/HANDOFF_TEMPLATE.md",
        ".agents/skills/schemabridge-milestone/SKILL.md",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        fail(f"Missing required starter files: {', '.join(missing)}")

    print("Starter integrity checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
