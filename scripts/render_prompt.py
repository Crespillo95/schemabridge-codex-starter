"""Print a paste-ready Codex milestone prompt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def resolve_prompt(root: Path, milestone: str) -> Path:
    """Resolve exactly one prompt by milestone prefix."""

    normalized = milestone.upper()
    if not normalized.startswith("M"):
        normalized = f"M{normalized}"
    matches = sorted((root / "prompts").glob(f"{normalized}_*.md"))
    if len(matches) != 1:
        names = ", ".join(path.name for path in matches) or "none"
        raise ValueError(f"Expected one prompt for {normalized}; found: {names}")
    return matches[0]


def main() -> int:
    """CLI entrypoint."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("milestone", help="Milestone ID, for example M00 or 10")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root; defaults to the parent of scripts/.",
    )
    args = parser.parse_args()

    try:
        path = resolve_prompt(args.root, args.milestone)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    print(path.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
