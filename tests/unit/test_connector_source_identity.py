from __future__ import annotations

import pytest

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
    postgres_source_identity_fingerprint,
    require_postgres_source_identity,
)


def _fingerprint() -> str:
    return postgres_source_identity_fingerprint(
        server_address="127.0.0.1",
        server_port=5432,
        database="tenant_a",
        user="schemabridge_reader",
    )


def test_postgres_source_identity_is_canonical_and_sensitive_to_every_coordinate() -> None:
    expected = _fingerprint()

    assert expected == _fingerprint()
    assert expected == postgres_source_identity_fingerprint(
        server_address="127.0.0.1/32",
        server_port=5432,
        database="tenant_a",
        user="schemabridge_reader",
    )
    assert len(expected) == 64
    assert expected != postgres_source_identity_fingerprint(
        server_address="127.0.0.2",
        server_port=5432,
        database="tenant_a",
        user="schemabridge_reader",
    )
    assert expected != postgres_source_identity_fingerprint(
        server_address="127.0.0.1",
        server_port=5433,
        database="tenant_a",
        user="schemabridge_reader",
    )
    assert expected != postgres_source_identity_fingerprint(
        server_address="127.0.0.1",
        server_port=5432,
        database="tenant_b",
        user="schemabridge_reader",
    )
    assert expected != postgres_source_identity_fingerprint(
        server_address="127.0.0.1",
        server_port=5432,
        database="tenant_a",
        user="another_reader",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("server_address", "host\nleak"),
        ("server_address", "127.0.0.1/24"),
        ("server_address", "fe80::1%en0/128"),
        ("server_address", "local_socket"),
        ("server_port", True),
        ("server_port", 0),
        ("server_port", 65_536),
        ("database", ""),
        ("database", "d" * 64),
        ("user", "u" * 64),
    ),
)
def test_postgres_source_identity_rejects_unbounded_or_ambiguous_values(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "database": "tenant_a",
        "user": "schemabridge_reader",
    }
    payload[field] = value

    with pytest.raises(ValueError, match="identity is invalid"):
        postgres_source_identity_fingerprint(**payload)  # type: ignore[arg-type]


def test_require_postgres_source_identity_accepts_only_the_exact_observation() -> None:
    require_postgres_source_identity(
        expected_fingerprint=_fingerprint(),
        server_address="127.0.0.1",
        server_port=5432,
        database="tenant_a",
        user="schemabridge_reader",
    )

    with pytest.raises(PostgresSourceIdentityMismatchError) as raised:
        require_postgres_source_identity(
            expected_fingerprint=_fingerprint(),
            server_address="127.0.0.1",
            server_port=5432,
            database="tenant_b",
            user="schemabridge_reader",
        )

    assert "tenant_a" not in str(raised.value)
    assert "tenant_b" not in str(raised.value)
    assert "127.0.0.1" not in str(raised.value)
