"""Explicit recorded planning context for the bounded synthetic demonstration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from schemabridge.application.ports.planning import PlanningPortError, PlanningPortErrorCode
from schemabridge.domain.joins import JoinContractSet
from schemabridge.domain.request_context import ApprovedLogicalContext
from schemabridge.domain.resolution import (
    GovernedMappingSet,
    SemanticPlanningContext,
)


class RecordedSemanticPlanningContext:
    """Load three labeled YAML fixtures; never fall back from a live catalog implicitly."""

    def __init__(
        self,
        logical_context_path: Path,
        mappings_path: Path,
        join_contracts_path: Path,
    ) -> None:
        self._logical_context_path = logical_context_path
        self._mappings_path = mappings_path
        self._join_contracts_path = join_contracts_path

    def load(self) -> SemanticPlanningContext:
        try:
            logical = ApprovedLogicalContext.model_validate(
                yaml.safe_load(self._logical_context_path.read_text(encoding="utf-8"))
            )
            mappings = GovernedMappingSet.model_validate(
                yaml.safe_load(self._mappings_path.read_text(encoding="utf-8"))
            )
            contracts = JoinContractSet.model_validate(
                yaml.safe_load(self._join_contracts_path.read_text(encoding="utf-8"))
            )
            return SemanticPlanningContext(
                version=1,
                source="recorded:demo/ground_truth/m10",
                logical_context=logical,
                mapping_set=mappings,
                join_contracts=contracts,
            )
        except OSError as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_UNAVAILABLE,
                "recorded governed planning context is unavailable",
            ) from error
        except (ValidationError, yaml.YAMLError, TypeError) as error:
            raise PlanningPortError(
                PlanningPortErrorCode.CONTEXT_INVALID,
                "recorded governed planning context is invalid",
            ) from error
