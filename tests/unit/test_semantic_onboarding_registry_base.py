"""Strict M33 binding to the currently active semantic registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from schemabridge.adapters.semantic_onboarding.registry_base import (
    AuthoritativeSemanticOnboardingRegistryBaseReader,
    StrictSemanticOnboardingRegistryBaseReader,
)
from schemabridge.application.ports.semantic_onboarding import (
    SemanticOnboardingPortError,
    SemanticOnboardingPortErrorCode,
)
from schemabridge.domain.registry_control import (
    ActiveRegistryPointer,
    RegistryVersionTrust,
    registry_projection_fingerprint,
)
from schemabridge.domain.semantic_registry import (
    SemanticRegistryScope,
    datahub_registry_document_urn,
)

SCOPE = SemanticRegistryScope(
    workspace_id="workspace-a",
    catalog_scope="postgres.production",
    registry_id="orders_registry",
)
FINGERPRINT = "a" * 64


@dataclass
class _Pointers:
    value: ActiveRegistryPointer | None

    def load_active(self, scope: SemanticRegistryScope) -> ActiveRegistryPointer | None:
        assert scope == SCOPE
        return self.value


@dataclass
class _Versions:
    value: object
    calls: int = 0

    def load_version(self, scope: SemanticRegistryScope, version: int) -> object:
        assert scope == SCOPE
        assert version == 4
        self.calls += 1
        return self.value


def test_absent_pointer_is_an_explicit_empty_base_without_version_lookup() -> None:
    versions = _Versions(value=object())
    reader = StrictSemanticOnboardingRegistryBaseReader(
        pointers=_Pointers(None),
        versions=versions,  # type: ignore[arg-type]
    )

    base = reader.load(SCOPE)

    assert base.registry_version is None
    assert base.next_registry_version == 1
    assert versions.calls == 0


def test_strict_version_is_bound_to_the_exact_active_pointer() -> None:
    pointer = _pointer()
    version = SimpleNamespace(
        trust=RegistryVersionTrust.STRICT,
        snapshot=SimpleNamespace(
            scope=SCOPE,
            registry=SimpleNamespace(version=4, fingerprint=FINGERPRINT),
        ),
    )
    reader = StrictSemanticOnboardingRegistryBaseReader(
        pointers=_Pointers(pointer),
        versions=_Versions(version),  # type: ignore[arg-type]
    )

    base = reader.load(SCOPE)

    assert base.registry_version == 4
    assert base.registry_fingerprint == FINGERPRINT
    assert base.activation_generation == 9
    assert base.active_pointer_fingerprint == registry_projection_fingerprint(pointer)
    assert base.next_registry_version == 5


def test_authoritative_control_pointer_is_a_complete_base_without_datahub_read() -> None:
    pointer = _pointer()
    reader = AuthoritativeSemanticOnboardingRegistryBaseReader(
        pointers=_Pointers(pointer),
    )

    base = reader.load(SCOPE)

    assert base.registry_version == pointer.registry_version
    assert base.registry_fingerprint == pointer.registry_fingerprint
    assert base.activation_generation == pointer.generation
    assert base.active_pointer_fingerprint == registry_projection_fingerprint(pointer)


def test_authoritative_control_pointer_rejects_a_mismatched_scope() -> None:
    other_scope = SemanticRegistryScope(
        workspace_id="workspace-b",
        catalog_scope=SCOPE.catalog_scope,
        registry_id=SCOPE.registry_id,
    )
    pointer = _pointer().model_copy(
        update={
            "scope": other_scope,
            "registry_target": datahub_registry_document_urn(other_scope, 4),
        }
    )
    reader = AuthoritativeSemanticOnboardingRegistryBaseReader(
        pointers=_Pointers(pointer),
    )

    with pytest.raises(SemanticOnboardingPortError) as raised:
        reader.load(SCOPE)

    assert raised.value.code is SemanticOnboardingPortErrorCode.INVALID_RESPONSE


def test_legacy_or_mismatched_registry_state_fails_closed() -> None:
    version = SimpleNamespace(
        trust=RegistryVersionTrust.LEGACY_READ_ONLY,
        snapshot=SimpleNamespace(
            scope=SCOPE,
            registry=SimpleNamespace(version=4, fingerprint=FINGERPRINT),
        ),
    )
    reader = StrictSemanticOnboardingRegistryBaseReader(
        pointers=_Pointers(_pointer()),
        versions=_Versions(version),  # type: ignore[arg-type]
    )

    with pytest.raises(SemanticOnboardingPortError) as raised:
        reader.load(SCOPE)

    assert raised.value.code is SemanticOnboardingPortErrorCode.INVALID_RESPONSE


def _pointer() -> ActiveRegistryPointer:
    return ActiveRegistryPointer(
        scope=SCOPE,
        generation=9,
        registry_version=4,
        registry_fingerprint=FINGERPRINT,
        registry_target=datahub_registry_document_urn(SCOPE, 4),
        transition_id="transition-orders-v4",
        activated_by="platform-admin-a",
        activated_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        decision_ids=("decision-orders-v4",),
    )
