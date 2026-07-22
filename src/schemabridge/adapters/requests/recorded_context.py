"""Explicit synthetic approved logical context for offline guided requests."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from schemabridge.application.ports.requests import (
    RequestWorkflowError,
    RequestWorkflowErrorCode,
)
from schemabridge.domain.request_context import ApprovedLogicalContext


class RecordedRequestContextAdapter:
    """Load a labeled YAML recording; it is never a hidden fallback for live context."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> ApprovedLogicalContext:
        try:
            payload = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            return ApprovedLogicalContext.model_validate(payload)
        except OSError as error:
            raise RequestWorkflowError(
                RequestWorkflowErrorCode.CONTEXT_UNAVAILABLE,
                "recorded approved logical context is unavailable",
            ) from error
        except (ValidationError, yaml.YAMLError) as error:
            raise RequestWorkflowError(
                RequestWorkflowErrorCode.CONTEXT_INVALID,
                "recorded approved logical context is invalid",
            ) from error
