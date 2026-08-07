"""Stable, session-only Streamlit state contracts."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ui_view_models import UiResult


@dataclass(frozen=True, slots=True)
class TransientExecutionResult:
    """Row payload retained only in one authenticated browser session."""

    actor_id: str
    workspace_id: str
    workflow_id: str
    revision: int
    registry_fingerprint: str
    activation_generation: int | None
    active_pointer_fingerprint: str | None
    result: UiResult
