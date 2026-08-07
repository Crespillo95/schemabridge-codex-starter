"""Framework-free contract for validating browser authentication configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

BrowserRuntimeProfile: TypeAlias = Literal[
    "development",
    "hosted-demo",
    "staging",
    "production",
]


class BrowserAuthConfigurationError(RuntimeError):
    """Sanitized rejection of private browser authentication configuration."""


@dataclass(frozen=True, slots=True)
class BrowserOidcRequirements:
    """Non-secret OIDC facts an adapter must bind to private configuration."""

    profile: BrowserRuntimeProfile
    provider: str
    audience: str
    issuer: str


@dataclass(frozen=True, slots=True)
class ValidatedBrowserAuthConfiguration:
    """Public evidence that the expected browser provider configuration is coherent."""

    provider: str


class BrowserAuthConfigurationValidatorPort(Protocol):
    """Validate private browser auth material against exact non-secret requirements."""

    def validate(
        self,
        secrets: Mapping[str, object],
        requirements: BrowserOidcRequirements,
    ) -> ValidatedBrowserAuthConfiguration:
        """Return non-secret validation evidence or one sanitized rejection."""
