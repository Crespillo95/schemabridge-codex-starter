from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from schemabridge.adapters.identity.local import LocalDemoPrincipalFactory
from schemabridge.domain.identity import AuthenticationMethod, IdentityRole

NOW = datetime(2026, 7, 23, 14, 0, tzinfo=UTC)


def test_local_demo_identity_is_stable_opaque_and_current() -> None:
    factory = LocalDemoPrincipalFactory(
        workspace="public-judge",
        subject="operator-one",
        roles=frozenset({IdentityRole.ANALYST, IdentityRole.PUBLISHER}),
    )

    principal = factory.create(now=NOW)
    later = factory.create(now=NOW + timedelta(minutes=1))

    assert principal.actor_id == later.actor_id
    assert principal.workspace_id == later.workspace_id
    assert principal.authentication_method is AuthenticationMethod.LOCAL_DEMO
    assert principal.roles == frozenset({IdentityRole.ANALYST, IdentityRole.PUBLISHER})
    assert principal.is_current(NOW)
    assert "public-judge" not in repr(principal)
    other = LocalDemoPrincipalFactory(
        workspace="public-judge",
        subject="operator-two",
        roles=frozenset({IdentityRole.ANALYST}),
    ).create(now=NOW)
    assert other.actor_id != principal.actor_id
    assert other.workspace_id == principal.workspace_id


def test_local_demo_identity_rejects_invalid_configuration_or_clock() -> None:
    with pytest.raises(ValueError, match="workspace"):
        LocalDemoPrincipalFactory(workspace=" ", subject="operator", roles=frozenset())
    with pytest.raises(ValueError, match="subject"):
        LocalDemoPrincipalFactory(workspace="demo", subject=" ", roles=frozenset())
    with pytest.raises(ValueError, match="ttl"):
        LocalDemoPrincipalFactory(
            workspace="demo",
            subject="operator",
            roles=frozenset(),
            session_ttl=timedelta(0),
        )
    with pytest.raises(ValueError, match="timezone"):
        LocalDemoPrincipalFactory(
            workspace="demo",
            subject="operator",
            roles=frozenset(),
        ).create(now=NOW.replace(tzinfo=None))
