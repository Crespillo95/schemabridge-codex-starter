#!/usr/bin/env python3
"""Verify the configured live DataHub semantic registry without exposing its payload."""

from __future__ import annotations

import json

from schemabridge.application.ports.planning import PlanningPortError
from schemabridge.bootstrap import build_semantic_registry
from schemabridge.config import get_settings
from schemabridge.domain.semantic_registry import semantic_registry_decision_ids

EXPECTED_COUNTS = {
    "models": 7,
    "mappings": 31,
    "joins": 5,
    "decisions": 37,
}


def main() -> None:
    settings = get_settings()
    if settings.registry_mode != "live":
        raise SystemExit("DataHub registry check requires SCHEMABRIDGE_REGISTRY_MODE=live.")
    try:
        scoped = build_semantic_registry(settings=settings).load()
    except PlanningPortError as error:
        raise SystemExit(f"DataHub registry check failed: {error.code.value}.") from error
    registry = scoped.registry
    observed = {
        "models": len(registry.logical_context.models),
        "mappings": len(registry.mapping_set.mappings),
        "joins": len(registry.join_contracts.contracts),
        "decisions": len(semantic_registry_decision_ids(registry)),
    }
    if observed != EXPECTED_COUNTS or not registry.source.startswith("datahub:"):
        raise SystemExit("DataHub registry check failed: unexpected governed registry shape.")
    print(
        json.dumps(
            {
                "ok": True,
                "registry_id": registry.registry_id,
                "version": registry.version,
                "catalog_scope": registry.catalog_scope,
                "source": registry.source,
                "fingerprint": registry.fingerprint,
                **observed,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
