"""Unit coverage for the exact v9 catalog connector route reader."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, cast

import psycopg
import pytest

from schemabridge.adapters.catalog.postgres_connector_routing import (
    PostgresCatalogConnectorRouteReader,
)
from schemabridge.application.ports.catalog_inventory import (
    CatalogInventoryError,
    CatalogInventoryErrorCode,
    ManagedCatalogConnectorRoute,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogRefreshId,
)

CONTROL_DSN = "postgresql://private-control.invalid/control"
WORKSPACE_ID = "workspace_catalog_route"
CONNECTION_ID = CatalogConnectionId("connection_catalog_route")
REFRESH_ID = CatalogRefreshId("refresh_catalog_route")
INDEXER_ID = "catalog:indexer-route"
CAPABILITY = "catalog-route-capability-" + ("x" * 48)
TARGET_FINGERPRINT = "a" * 64
CATALOG_IDENTITY_FINGERPRINT = "b" * 64
PRIVATE_BINDING = "vault:datahub:catalog-route"


class _Composable(Protocol):
    def as_string(self) -> str: ...


@dataclass
class _Cursor:
    rows: Sequence[tuple[object, ...]]

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        return self.rows


@dataclass
class _Connection:
    rows: Sequence[tuple[object, ...]]
    database_error: psycopg.Error | None = None
    statements: list[tuple[str, Sequence[object] | None]] = field(default_factory=list)

    def execute(
        self,
        query: object,
        params: Sequence[object] | None = None,
    ) -> _Cursor:
        rendered = query if isinstance(query, str) else cast(_Composable, query).as_string()
        normalized = " ".join(rendered.split())
        self.statements.append((normalized, params))
        if normalized == "SET TRANSACTION READ ONLY":
            return _Cursor(())
        if self.database_error is not None:
            raise self.database_error
        return _Cursor(self.rows)

    @contextmanager
    def transaction(self) -> Iterator[object]:
        yield object()


@dataclass
class _Provider:
    value: _Connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.value


def _row(**updates: object) -> tuple[object, ...]:
    values: list[object] = [
        WORKSPACE_ID,
        CONNECTION_ID.root,
        CatalogConnectionKind.DATAHUB_GRAPHQL.value,
        "PROD",
        "synthetic-demo",
        "urn:li:dataPlatformInstance:(urn:li:dataPlatform:postgres,warehouse-a)",
        CATALOG_IDENTITY_FINGERPRINT,
        3,
        7,
        TARGET_FINGERPRINT,
        PRIVATE_BINDING,
    ]
    positions = {
        "workspace_id": 0,
        "connection_id": 1,
        "kind": 2,
        "environment": 3,
        "catalog_scope": 4,
        "platform_instance": 5,
        "catalog_identity_fingerprint": 6,
        "contract_version": 7,
        "route_revision": 8,
        "target_fingerprint": 9,
        "credential_binding_ref": 10,
    }
    for key, value in updates.items():
        values[positions[key]] = value
    return tuple(values)


def _reader(
    rows: Sequence[tuple[object, ...]],
    *,
    error: psycopg.Error | None = None,
) -> tuple[PostgresCatalogConnectorRouteReader, _Connection]:
    connection = _Connection(rows=rows, database_error=error)
    return (
        PostgresCatalogConnectorRouteReader(
            dsn=CONTROL_DSN,
            connection_provider=_Provider(connection),  # type: ignore[arg-type]
        ),
        connection,
    )


def _load(
    reader: PostgresCatalogConnectorRouteReader,
) -> ManagedCatalogConnectorRoute | None:
    return reader.load_route(
        WORKSPACE_ID,
        CONNECTION_ID,
        refresh_id=REFRESH_ID,
        indexer_id=INDEXER_ID,
        lease_capability=CAPABILITY,
        fencing_token=11,
    )


def test_catalog_route_reader_binds_current_target_and_exact_live_lease() -> None:
    reader, connection = _reader([_row()])

    managed = _load(reader)

    assert managed is not None
    route = managed.route
    assert route.workspace_id == WORKSPACE_ID
    assert route.connection_id == CONNECTION_ID
    assert route.contract_version == 3
    assert route.route_revision == 7
    assert route.target_fingerprint == TARGET_FINGERPRINT
    assert route.catalog_identity_fingerprint == CATALOG_IDENTITY_FINGERPRINT
    assert route.platform_instance is not None
    assert managed.credential_binding_ref == PRIVATE_BINDING
    query, params = connection.statements[0]
    assert "load_current_connector_target" in query
    assert "load_owned_catalog_connector_route" in query
    assert params == (
        WORKSPACE_ID,
        CONNECTION_ID.root,
        WORKSPACE_ID,
        CONNECTION_ID.root,
        REFRESH_ID.root,
        INDEXER_ID,
        CAPABILITY,
        11,
    )
    rendered = repr(reader) + repr(managed) + repr(route)
    assert CONTROL_DSN not in rendered
    assert CAPABILITY not in rendered
    assert PRIVATE_BINDING not in rendered


def test_catalog_route_reader_returns_none_when_current_lease_route_is_absent() -> None:
    reader, _ = _reader(())

    assert _load(reader) is None


def test_catalog_route_reader_preserves_explicitly_absent_platform_instance() -> None:
    reader, _ = _reader([_row(platform_instance=None)])

    managed = _load(reader)

    assert managed is not None
    assert managed.route.platform_instance is None


@pytest.mark.parametrize(
    "row",
    [
        _row(workspace_id="workspace_other"),
        _row(connection_id="connection_other"),
        _row(contract_version=0),
        _row(route_revision=True),
        _row(target_fingerprint="not-a-fingerprint"),
        _row(catalog_identity_fingerprint="not-a-fingerprint"),
        _row(platform_instance=" "),
        _row(credential_binding_ref="https://secret.invalid"),
        _row()[:-1],
        (*_row(), "extra"),
    ],
)
def test_catalog_route_reader_rejects_malformed_or_cross_scope_rows(
    row: tuple[object, ...],
) -> None:
    reader, _ = _reader([row])

    with pytest.raises(CatalogInventoryError) as raised:
        _load(reader)

    assert raised.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE
    assert str(raised.value) == "catalog connector route response is invalid"
    assert PRIVATE_BINDING not in str(raised.value)


def test_catalog_route_reader_rejects_duplicate_rows_and_sanitizes_database_errors() -> None:
    duplicate_reader, _ = _reader([_row(), _row()])
    unavailable_reader, _ = _reader([_row()], error=psycopg.OperationalError("private dsn"))

    with pytest.raises(CatalogInventoryError) as duplicate:
        _load(duplicate_reader)
    with pytest.raises(CatalogInventoryError) as unavailable:
        _load(unavailable_reader)

    assert duplicate.value.code is CatalogInventoryErrorCode.INVALID_RESPONSE
    assert unavailable.value.code is CatalogInventoryErrorCode.UNAVAILABLE
    assert "private dsn" not in str(unavailable.value)
