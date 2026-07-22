"""Shared foundations for immutable domain values."""

from pydantic import BaseModel, ConfigDict


class FrozenDomainModel(BaseModel):
    """Base configuration for deterministic domain models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )
