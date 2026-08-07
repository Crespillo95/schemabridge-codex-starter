"""Non-executable physical-field discovery over the current PostgreSQL catalog."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql
from pydantic import ValidationError

from schemabridge.adapters.storage.postgres import (
    ControlConnectionProvider,
    _ControlDatabase,
)
from schemabridge.application.ports.query_studio import (
    QueryStudioPortError,
    QueryStudioPortErrorCode,
)
from schemabridge.domain.catalog_inventory import (
    CatalogAssetId,
    CatalogAssetLocator,
    CatalogConnectionId,
    CatalogFieldLocator,
)
from schemabridge.domain.query_studio import (
    PhysicalDiscoveryCandidate,
    PhysicalDiscoveryCardinality,
    PhysicalDiscoveryCursor,
    PhysicalFieldDiscoveryPage,
    PhysicalFieldDiscoveryRequest,
    query_studio_fingerprint,
)
from schemabridge.domain.semantic_registry import SemanticRegistryScope

DEFAULT_PHYSICAL_DISCOVERY_CURSOR_TTL = timedelta(minutes=15)

_FORMAT_VERSION = 1
_MAXIMUM_CURSOR_BYTES = 1_024
_MAXIMUM_CURSOR_TTL = timedelta(minutes=15)
_MINIMUM_SIGNING_KEY_BYTES = 32
_MINIMUM_SIGNING_KEY_DISTINCT_BYTES = 8
_SIGNATURE_CONTEXT = b"schemabridge-physical-discovery-cursor-v1\x00"
_SCOPE_CONTEXT = b"schemabridge-physical-discovery-scope-v1\x00"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONNECTION_ID = re.compile(r"^[a-z][a-z0-9_-]{2,199}$")
_PAYLOAD_KEYS = frozenset({"c", "e", "g", "i", "p", "q", "r", "v", "w"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _PhysicalDiscoveryPosition:
    catalog_vector_fingerprint: str
    score: int
    connection_id: str
    asset_key: str
    field_key: str


class SignedPhysicalDiscoveryCursorCodec:
    """Authenticate a short-lived physical keyset without exposing tenant scope."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        ttl: timedelta = DEFAULT_PHYSICAL_DISCOVERY_CURSOR_TTL,
    ) -> None:
        if (
            not isinstance(signing_key, bytes)
            or len(signing_key) < _MINIMUM_SIGNING_KEY_BYTES
            or len(set(signing_key)) < _MINIMUM_SIGNING_KEY_DISTINCT_BYTES
        ):
            raise ValueError("physical discovery cursor signing key is invalid")
        ttl_microseconds = _timedelta_microseconds(ttl)
        if ttl_microseconds <= 0 or ttl_microseconds > _timedelta_microseconds(_MAXIMUM_CURSOR_TTL):
            raise ValueError("physical discovery cursor TTL is invalid")
        self._signing_key = signing_key
        self._ttl_microseconds = ttl_microseconds

    def encode(
        self,
        *,
        request: PhysicalFieldDiscoveryRequest,
        catalog_vector_fingerprint: str,
        score: int,
        connection_id: str,
        asset_key: str,
        field_key: str,
        issued_at: datetime,
    ) -> PhysicalDiscoveryCursor:
        """Issue one canonical cursor bound to scope, query, snapshot, and final key."""

        issued = _datetime_microseconds(issued_at)
        _validate_position(
            catalog_vector_fingerprint=catalog_vector_fingerprint,
            score=score,
            connection_id=connection_id,
            asset_key=asset_key,
            field_key=field_key,
        )
        payload: dict[str, object] = {
            "c": self._scope_digest("catalog_scope", request.scope.catalog_scope),
            "e": issued + self._ttl_microseconds,
            "g": catalog_vector_fingerprint,
            "i": issued,
            "p": [score, connection_id, asset_key, field_key],
            "q": _request_fingerprint(request),
            "r": self._scope_digest("registry_id", request.scope.registry_id),
            "v": _FORMAT_VERSION,
            "w": self._scope_digest("workspace", request.scope.workspace_id),
        }
        try:
            payload_bytes = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeError) as error:
            raise _cursor_unavailable() from error
        signature = hmac.new(
            self._signing_key,
            _SIGNATURE_CONTEXT + payload_bytes,
            hashlib.sha256,
        ).digest()
        token = f"{_base64url_encode(payload_bytes)}.{_base64url_encode(signature)}"
        if len(token.encode("ascii")) > _MAXIMUM_CURSOR_BYTES:
            raise _cursor_unavailable()
        return PhysicalDiscoveryCursor(token)

    def decode(
        self,
        *,
        cursor: PhysicalDiscoveryCursor,
        request: PhysicalFieldDiscoveryRequest,
        at: datetime,
    ) -> _PhysicalDiscoveryPosition:
        """Verify canonical form, signature, lifetime, request, and complete scope."""

        try:
            observed = _datetime_microseconds(at)
            payload_bytes = self._verified_payload_bytes(cursor.root)
            payload = _canonical_payload(payload_bytes)
            if set(payload) != _PAYLOAD_KEYS:
                raise ValueError("physical discovery cursor payload shape is invalid")
            version = _exact_int(payload["v"])
            issued = _exact_int(payload["i"])
            expires = _exact_int(payload["e"])
            vector = _exact_sha256(payload["g"])
            query_fingerprint = _exact_sha256(payload["q"])
            workspace_digest = _exact_digest(payload["w"])
            catalog_scope_digest = _exact_digest(payload["c"])
            registry_digest = _exact_digest(payload["r"])
            position = payload["p"]
            if (
                version != _FORMAT_VERSION
                or issued < 0
                or expires - issued != self._ttl_microseconds
                or observed < issued
                or observed >= expires
                or not isinstance(position, list)
                or len(position) != 4
            ):
                raise ValueError("physical discovery cursor payload is invalid")
            score = _exact_int(position[0])
            connection_id = _exact_string(position[1])
            asset_key = _exact_string(position[2])
            field_key = _exact_string(position[3])
            if not all(
                (
                    hmac.compare_digest(
                        workspace_digest,
                        self._scope_digest("workspace", request.scope.workspace_id),
                    ),
                    hmac.compare_digest(
                        catalog_scope_digest,
                        self._scope_digest(
                            "catalog_scope",
                            request.scope.catalog_scope,
                        ),
                    ),
                    hmac.compare_digest(
                        registry_digest,
                        self._scope_digest("registry_id", request.scope.registry_id),
                    ),
                    hmac.compare_digest(query_fingerprint, _request_fingerprint(request)),
                )
            ):
                raise ValueError("physical discovery cursor scope is invalid")
            _validate_position(
                catalog_vector_fingerprint=vector,
                score=score,
                connection_id=connection_id,
                asset_key=asset_key,
                field_key=field_key,
            )
            return _PhysicalDiscoveryPosition(
                catalog_vector_fingerprint=vector,
                score=score,
                connection_id=connection_id,
                asset_key=asset_key,
                field_key=field_key,
            )
        except (
            binascii.Error,
            OverflowError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as error:
            raise _cursor_unavailable() from error

    def _verified_payload_bytes(self, token: str) -> bytes:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > _MAXIMUM_CURSOR_BYTES
            or _TOKEN.fullmatch(token) is None
        ):
            raise ValueError("physical discovery cursor token is invalid")
        payload_segment, signature_segment = token.split(".", maxsplit=1)
        payload_bytes = _base64url_decode(payload_segment)
        supplied_signature = _base64url_decode(signature_segment)
        expected_signature = hmac.new(
            self._signing_key,
            _SIGNATURE_CONTEXT + payload_bytes,
            hashlib.sha256,
        ).digest()
        if len(supplied_signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
            supplied_signature,
            expected_signature,
        ):
            raise ValueError("physical discovery cursor signature is invalid")
        return payload_bytes

    def _scope_digest(self, kind: str, value: str) -> str:
        kind_bytes = kind.encode("utf-8")
        value_bytes = value.encode("utf-8")
        framed = (
            _SCOPE_CONTEXT
            + len(kind_bytes).to_bytes(2, "big")
            + kind_bytes
            + len(value_bytes).to_bytes(4, "big")
            + value_bytes
        )
        return _base64url_encode(hmac.new(self._signing_key, framed, hashlib.sha256).digest())


@dataclass(frozen=True, slots=True)
class PostgresPhysicalFieldDiscovery:
    """Read global current physical fields without granting semantic authority."""

    dsn: str = field(repr=False)
    cursors: SignedPhysicalDiscoveryCursorCodec = field(repr=False)
    schema: str = "schemabridge_control"
    application_name: str = "schemabridge-control-runtime"
    connection_provider: ControlConnectionProvider | None = field(default=None, repr=False)
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC),
        repr=False,
        compare=False,
    )
    _database: _ControlDatabase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_database",
            _ControlDatabase(
                self.dsn,
                self.schema,
                application_name=self.application_name,
                connection_provider=self.connection_provider,
            ),
        )

    @classmethod
    def from_signing_key(
        cls,
        *,
        dsn: str,
        cursor_signing_key: bytes,
        schema: str = "schemabridge_control",
        application_name: str = "schemabridge-control-runtime",
        connection_provider: ControlConnectionProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> PostgresPhysicalFieldDiscovery:
        """Construct the adapter without accepting or retaining a textual secret."""

        return cls(
            dsn=dsn,
            cursors=SignedPhysicalDiscoveryCursorCodec(signing_key=cursor_signing_key),
            schema=schema,
            application_name=application_name,
            connection_provider=connection_provider,
            clock=clock,
        )

    def search(self, request: PhysicalFieldDiscoveryRequest) -> PhysicalFieldDiscoveryPage:
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "physical discovery clock is invalid",
            )
        try:
            position = (
                None
                if request.cursor is None
                else self.cursors.decode(
                    cursor=request.cursor,
                    request=request,
                    at=observed_at,
                )
            )
            load_scope = self._load_scope_statement()
            search = self._search_statement()
            scope_values = (
                request.scope.workspace_id,
                request.scope.catalog_scope,
                request.scope.registry_id,
            )
            with self._database.connect() as connection, connection.transaction():
                initial_scope = _scope_from_row(
                    connection.execute(load_scope, scope_values).fetchone()
                )
                if position is not None and position.catalog_vector_fingerprint != initial_scope[0]:
                    raise QueryStudioPortError(
                        QueryStudioPortErrorCode.SCOPE_CHANGED,
                        "physical discovery catalog snapshot changed",
                    )
                rows = connection.execute(
                    search,
                    (
                        *scope_values,
                        initial_scope[0],
                        "" if request.query is None else request.query.root,
                        request.page_size,
                        None if position is None else position.score,
                        None if position is None else position.connection_id,
                        None if position is None else position.asset_key,
                        None if position is None else position.field_key,
                    ),
                ).fetchall()
                confirmed_scope = _scope_from_row(
                    connection.execute(load_scope, scope_values).fetchone()
                )
                if confirmed_scope != initial_scope:
                    raise QueryStudioPortError(
                        QueryStudioPortErrorCode.SCOPE_CHANGED,
                        "physical discovery catalog snapshot changed during retrieval",
                    )
            if len(rows) > request.page_size + 1 or any(
                len(row) != 15 or str(row[13]) != initial_scope[0] for row in rows
            ):
                raise ValueError("physical discovery escaped its snapshot or row bound")
            items = tuple(
                _candidate_from_row(row, workspace_id=request.scope.workspace_id)
                for row in rows[: request.page_size]
            )
            next_cursor = (
                self._cursor_from_row(
                    request=request,
                    row=rows[request.page_size - 1],
                    issued_at=observed_at,
                )
                if len(rows) == request.page_size + 1
                else None
            )
            return PhysicalFieldDiscoveryPage(
                items=items,
                page_size=request.page_size,
                next_cursor=next_cursor,
            )
        except QueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError, IndexError) as error:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "physical discovery response is invalid",
            ) from error
        except psycopg.Error as error:
            code = (
                QueryStudioPortErrorCode.SCOPE_CHANGED
                if error.sqlstate == "40001"
                else (
                    QueryStudioPortErrorCode.INVALID_RESPONSE
                    if error.sqlstate in {"22003", "22023"}
                    else QueryStudioPortErrorCode.RESOURCE_UNAVAILABLE
                )
            )
            raise QueryStudioPortError(
                code,
                "physical discovery is unavailable",
            ) from error

    def inspect_cardinality(
        self,
        scope: SemanticRegistryScope,
    ) -> PhysicalDiscoveryCardinality:
        """Load only current aggregate counts through the reviewed runtime function."""

        try:
            values = (scope.workspace_id, scope.catalog_scope, scope.registry_id)
            with self._database.connect() as connection, connection.transaction():
                snapshot = _scope_from_row(
                    connection.execute(self._load_scope_statement(), values).fetchone()
                )
            return PhysicalDiscoveryCardinality(
                scope=scope,
                catalog_generation_vector_fingerprint=snapshot[0],
                connection_count=snapshot[1],
                asset_count=snapshot[2],
                field_count=snapshot[3],
            )
        except QueryStudioPortError:
            raise
        except (ValidationError, TypeError, ValueError, IndexError) as error:
            raise QueryStudioPortError(
                QueryStudioPortErrorCode.INVALID_RESPONSE,
                "physical discovery cardinality response is invalid",
            ) from error
        except psycopg.Error as error:
            code = (
                QueryStudioPortErrorCode.INVALID_RESPONSE
                if error.sqlstate in {"22003", "22023"}
                else QueryStudioPortErrorCode.RESOURCE_UNAVAILABLE
            )
            raise QueryStudioPortError(
                code,
                "physical discovery cardinality is unavailable",
            ) from error

    def _cursor_from_row(
        self,
        *,
        request: PhysicalFieldDiscoveryRequest,
        row: Sequence[Any],
        issued_at: datetime,
    ) -> PhysicalDiscoveryCursor:
        return self.cursors.encode(
            request=request,
            catalog_vector_fingerprint=str(row[13]),
            score=int(row[14]),
            connection_id=str(row[0]),
            asset_key=str(row[3]),
            field_key=str(row[7]),
            issued_at=issued_at,
        )

    def _load_scope_statement(self) -> sql.Composed:
        return sql.SQL(
            """
            SELECT *
            FROM {}.load_physical_discovery_scope(
                %s::varchar, %s::varchar, %s::varchar
            )
            """
        ).format(sql.Identifier(self.schema))

    def _search_statement(self) -> sql.Composed:
        return sql.SQL(
            """
            SELECT *
            FROM {}.discover_physical_fields(
                %s::varchar, %s::varchar, %s::varchar, %s,
                %s::varchar, %s::integer, %s::integer, %s::varchar, %s, %s
            )
            """
        ).format(sql.Identifier(self.schema))


def _scope_from_row(row: Sequence[Any] | None) -> tuple[str, int, int, int]:
    if row is None or len(row) != 4:
        raise ValueError("physical discovery scope is unavailable")
    fingerprint = str(row[0])
    connection_count = int(row[1])
    asset_count = int(row[2])
    field_count = int(row[3])
    if (
        _SHA256.fullmatch(fingerprint) is None
        or connection_count < 0
        or asset_count < 0
        or field_count < 0
    ):
        raise ValueError("physical discovery scope is invalid")
    return fingerprint, connection_count, asset_count, field_count


def _candidate_from_row(
    row: Sequence[Any],
    *,
    workspace_id: str,
) -> PhysicalDiscoveryCandidate:
    if len(row) != 15:
        raise ValueError("physical discovery row has an invalid shape")
    field_path = tuple(str(value) for value in row[8])
    if not field_path:
        raise ValueError("physical discovery field path is empty")
    return PhysicalDiscoveryCandidate(
        locator=CatalogFieldLocator(
            asset=CatalogAssetLocator(
                workspace_id=workspace_id,
                connection_id=CatalogConnectionId(str(row[0])),
                asset_id=CatalogAssetId(str(row[4])),
            ),
            field_path=field_path,
        ),
        generation=int(row[1]),
        asset_qualified_name=str(row[5]),
        native_type=None if row[10] is None else str(row[10]),
        definition=None if row[11] is None else str(row[11]),
        metadata_fingerprint=str(row[12]),
    )


def _request_fingerprint(request: PhysicalFieldDiscoveryRequest) -> str:
    return query_studio_fingerprint(
        {
            "catalog_scope": request.scope.catalog_scope,
            "lane": "physical_discovery",
            "query": None if request.query is None else request.query.root,
            "registry_id": request.scope.registry_id,
            "version": 1,
            "workspace_id": request.scope.workspace_id,
        }
    )


def _validate_position(
    *,
    catalog_vector_fingerprint: str,
    score: int,
    connection_id: str,
    asset_key: str,
    field_key: str,
) -> None:
    if (
        _SHA256.fullmatch(catalog_vector_fingerprint) is None
        or type(score) is not int
        or not 0 <= score <= 5_000
        or _CONNECTION_ID.fullmatch(connection_id) is None
        or _SHA256.fullmatch(asset_key) is None
        or _SHA256.fullmatch(field_key) is None
    ):
        raise ValueError("physical discovery keyset position is invalid")


def _cursor_unavailable() -> ValueError:
    return ValueError("physical discovery cursor is unavailable")


def _datetime_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("physical discovery cursor time must include a timezone")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _timedelta_microseconds(value: timedelta) -> int:
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("physical discovery cursor base64 is invalid")
    padded = value + ("=" * (-len(value) % 4))
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    if _base64url_encode(decoded) != value:
        raise ValueError("physical discovery cursor base64 is not canonical")
    return decoded


def _canonical_payload(payload_bytes: bytes) -> Mapping[str, object]:
    payload = json.loads(payload_bytes.decode("ascii"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("physical discovery cursor payload is invalid")
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not hmac.compare_digest(payload_bytes, canonical):
        raise ValueError("physical discovery cursor payload is not canonical")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("physical discovery cursor contains duplicate keys")
        result[key] = value
    return result


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("physical discovery cursor integer is invalid")
    return value


def _exact_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("physical discovery cursor string is invalid")
    return value


def _exact_sha256(value: object) -> str:
    candidate = _exact_string(value)
    if _SHA256.fullmatch(candidate) is None:
        raise ValueError("physical discovery cursor fingerprint is invalid")
    return candidate


def _exact_digest(value: object) -> str:
    candidate = _exact_string(value)
    if _DIGEST.fullmatch(candidate) is None:
        raise ValueError("physical discovery cursor digest is invalid")
    return candidate


__all__ = [
    "DEFAULT_PHYSICAL_DISCOVERY_CURSOR_TTL",
    "PostgresPhysicalFieldDiscovery",
    "SignedPhysicalDiscoveryCursorCodec",
]
