"""Strict active-registry base reader for M33 onboarding CAS bindings."""

from __future__ import annotations

from dataclasses import dataclass

from schemabridge.application.ports.registry_control import (
    ActiveRegistryPointerReadPort,
    RegistryControlError,
    RegistryVersionReadPort,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_control import (
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_onboarding import OnboardingRegistryBase
from schemabridge.domain.semantic_registry import SemanticRegistryScope


@dataclass(frozen=True, slots=True)
class AuthoritativeSemanticOnboardingRegistryBaseReader:
    """Project the PostgreSQL CAS pointer without contacting its external registry target."""

    pointers: ActiveRegistryPointerReadPort

    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        try:
            pointer = self.pointers.load_active(scope)
        except RegistryControlError as error:
            raise _unavailable() from error
        if pointer is None:
            return OnboardingRegistryBase()
        try:
            if pointer.scope != scope:
                raise _invalid_response()
            return OnboardingRegistryBase(
                registry_version=pointer.registry_version,
                registry_fingerprint=pointer.registry_fingerprint,
                activation_generation=pointer.generation,
                active_pointer_fingerprint=registry_projection_fingerprint(pointer),
            )
        except SemanticOnboardingPortError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _invalid_response() from error


@dataclass(frozen=True, slots=True)
class StrictSemanticOnboardingRegistryBaseReader:
    pointers: ActiveRegistryPointerReadPort
    versions: RegistryVersionReadPort

    def load(self, scope: SemanticRegistryScope) -> OnboardingRegistryBase:
        try:
            pointer = self.pointers.load_active(scope)
            if pointer is None:
                return OnboardingRegistryBase()
            version = self.versions.load_version(scope, pointer.registry_version)
        except RegistryControlError as error:
            raise _unavailable() from error
        try:
            snapshot = version.snapshot
            if (
                version.trust is not RegistryVersionTrust.STRICT
                or pointer.scope != scope
                or snapshot.scope != scope
                or snapshot.registry.version != pointer.registry_version
                or snapshot.registry.fingerprint != pointer.registry_fingerprint
            ):
                raise _invalid_response()
            return OnboardingRegistryBase(
                registry_version=pointer.registry_version,
                registry_fingerprint=pointer.registry_fingerprint,
                activation_generation=pointer.generation,
                active_pointer_fingerprint=registry_projection_fingerprint(pointer),
            )
        except SemanticOnboardingPortError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _invalid_response() from error


def _unavailable() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.UNAVAILABLE,
        "semantic onboarding registry base is unavailable",
    )


def _invalid_response() -> SemanticOnboardingPortError:
    return SemanticOnboardingPortError(
        SemanticOnboardingPortErrorCode.INVALID_RESPONSE,
        "semantic onboarding registry base is invalid",
    )


__all__ = [
    "AuthoritativeSemanticOnboardingRegistryBaseReader",
    "StrictSemanticOnboardingRegistryBaseReader",
]
