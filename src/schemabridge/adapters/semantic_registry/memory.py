"""In-memory governed registry for unit tests and explicit synthetic seams."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.domain.semantic_registry import (
    GovernedSemanticRegistrySnapshot,
    ScopedSemanticRegistrySnapshot,
    SemanticRegistryScope,
)


@dataclass(slots=True)
class InMemoryGovernedSemanticRegistry:
    registry: GovernedSemanticRegistrySnapshot
    _scope: SemanticRegistryScope
    activation_generation: int | None = None
    active_pointer_fingerprint: str | None = None
    loads: int = 0

    @property
    def scope(self) -> SemanticRegistryScope:
        return self._scope

    def load(self) -> ScopedSemanticRegistrySnapshot:
        self.loads += 1
        return ScopedSemanticRegistrySnapshot(
            scope=self._scope,
            registry=self.registry,
            activation_generation=self.activation_generation,
            active_pointer_fingerprint=self.active_pointer_fingerprint,
        )
