#!/usr/bin/env python3
"""Run the production Streamlit app with one isolated M27 acceptance decorator."""

from __future__ import annotations

import os
from pathlib import Path

from schemabridge.bootstrap import QueryStudioRuntimeServices
from schemabridge.config import Settings
from schemabridge.domain.identity import AuthenticatedPrincipal
from schemabridge.domain.query_studio import ConfirmedQueryStudioRequest
from schemabridge.entrypoints.streamlit.query_studio_acceptance import (
    M27BrowserScenario,
    build_m27_browser_scenario_runtime,
    load_m27_browser_scenario,
    render_m27_delayed_query_studio,
)


def main() -> None:
    """Install the decorator before invoking the otherwise unchanged app."""

    scenario = load_m27_browser_scenario(os.environ)

    def scenario_builder(
        *,
        principal: AuthenticatedPrincipal,
        repository_root: Path | None = None,
        settings: Settings | None = None,
    ) -> QueryStudioRuntimeServices:
        return build_m27_browser_scenario_runtime(
            principal=principal,
            scenario=scenario,
            repository_root=repository_root,
            settings=settings,
        )

    from schemabridge.entrypoints.streamlit import app as production_app

    previous_builder = production_app.__dict__["build_query_studio_runtime"]
    previous_renderer = production_app.__dict__["render_dynamic_query_studio"]

    def scenario_renderer(
        runtime: QueryStudioRuntimeServices,
        *,
        can_create: bool,
    ) -> ConfirmedQueryStudioRequest | None:
        return render_m27_delayed_query_studio(
            runtime,
            can_create=can_create,
            delegate=previous_renderer,
        )

    try:
        production_app.__dict__["build_query_studio_runtime"] = scenario_builder
        if scenario is M27BrowserScenario.DELAYED_EXPANSION:
            production_app.__dict__["render_dynamic_query_studio"] = scenario_renderer
        production_app.main()
    finally:
        production_app.__dict__["build_query_studio_runtime"] = previous_builder
        production_app.__dict__["render_dynamic_query_studio"] = previous_renderer


if __name__ == "__main__":
    main()
