from __future__ import annotations

import base64
import json
import os
import ssl
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

import schemabridge.adapters.connectors.remote_secrets as remote_secret_module
from schemabridge.adapters.connectors.remote_secrets import (
    ConnectorSecretCapability,
    ProjectedServiceAccountIdentity,
    UrllibRemoteSecretTransport,
    VaultKvV2ConnectorSecretResolver,
)
from schemabridge.application.ports.connector_secrets import (
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
)
from schemabridge.domain.catalog_inventory import CatalogConnectionId
from schemabridge.domain.connectors import (
    GovernedExecutionTarget,
    QueryCostBudget,
    SourceConnectorKind,
    SourceDialect,
    postgres_type_contract_fingerprint,
)
from schemabridge.domain.workflows import fingerprint_payload

REFERENCE = "execution.tenant-a.primary-v7"
PROVIDER_SECRET_VERSION = 19
WORKLOAD_TOKEN = "header.payload.signature"
CLIENT_TOKEN = "hvs.synthetic-client-token-0000000000000000"
TLS_DSN = (
    "postgresql://schemabridge_reader:synthetic-password@source.example.test:5432/analytics"
    "?sslmode=verify-full"
    "&sslrootcert=%2Fvar%2Frun%2Fsecrets%2Fschemabridge%2Ftrust%2Fsource-ca.crt"
)


@dataclass(slots=True)
class _Identity:
    token: str = field(repr=False, default=WORKLOAD_TOKEN)
    error: ConnectorSecretResolutionError | None = field(default=None, repr=False)
    calls: int = 0

    def read(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


@dataclass(slots=True)
class _Transport:
    responses: list[bytes]
    error: ConnectorSecretResolutionError | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: object,
        body: bytes | None,
        ca_bundle: Path,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "ca_bundle": ca_bundle,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _target(**updates: object) -> GovernedExecutionTarget:
    budget = QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=64 * 1_024,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )
    values: dict[str, object] = {
        "workspace_id": "tenant_a",
        "connection_id": CatalogConnectionId("warehouse"),
        "connector_kind": SourceConnectorKind.POSTGRESQL,
        "dialect": SourceDialect.POSTGRESQL,
        "route_revision": 7,
        "route_fingerprint": "a" * 64,
        "expected_reader": "schemabridge_reader",
        "source_identity_fingerprint": fingerprint_payload({"source": "tenant-a"}),
        "catalog_identity_fingerprint": fingerprint_payload({"catalog": "tenant-a"}),
        "type_contract_fingerprint": postgres_type_contract_fingerprint(),
        "cost_budget": budget,
        "cost_budget_fingerprint": budget.fingerprint,
    }
    values.update(updates)
    return GovernedExecutionTarget.model_validate(values)


def _login_response(**updates: object) -> bytes:
    auth: dict[str, object] = {
        "client_token": CLIENT_TOKEN,
        "lease_duration": 600,
        "renewable": False,
    }
    auth.update(updates)
    return json.dumps({"auth": auth}, sort_keys=True).encode()


def _secret_document(**updates: object) -> dict[str, object]:
    document: dict[str, object] = {
        "format_version": 1,
        "dialect": "postgresql",
        "expected_reader": "schemabridge_reader",
        "dsn": TLS_DSN,
    }
    document.update(updates)
    return document


def _secret_response(
    *,
    version: int = PROVIDER_SECRET_VERSION,
    document: dict[str, object] | None = None,
    destroyed: bool = False,
    deletion_time: str = "",
) -> bytes:
    return json.dumps(
        {
            "data": {
                "data": document or _secret_document(),
                "metadata": {
                    "deletion_time": deletion_time,
                    "destroyed": destroyed,
                    "version": version,
                },
            }
        },
        sort_keys=True,
    ).encode()


def _with_extra_key(
    raw: bytes,
    *,
    path: tuple[str, ...],
    key: str = "unsupported_provider_field",
) -> bytes:
    document = json.loads(raw)
    selected = document
    for part in path:
        assert isinstance(selected, dict)
        selected = selected[part]
    assert isinstance(selected, dict)
    selected[key] = "private-provider-value"
    return json.dumps(document, sort_keys=True).encode()


def _ca_bundle(tmp_path: Path) -> Path:
    path = (tmp_path / "provider-ca.crt").resolve()
    path.write_text("synthetic test trust anchor", encoding="utf-8")
    path.chmod(0o600)
    return path


def _resolver(
    tmp_path: Path,
    *,
    identity: _Identity | None = None,
    transport: _Transport | None = None,
) -> tuple[VaultKvV2ConnectorSecretResolver, _Identity, _Transport]:
    selected_identity = identity or _Identity()
    selected_transport = transport or _Transport([_login_response(), _secret_response()])
    resolver = VaultKvV2ConnectorSecretResolver(
        server="https://secrets.example.test",
        role="schemabridge-worker",
        kv_mount="tenant-connectors",
        capability=ConnectorSecretCapability.EXECUTION,
        identity=selected_identity,
        ca_bundle=_ca_bundle(tmp_path),
        transport=selected_transport,
    )
    return resolver, selected_identity, selected_transport


def _reference(
    provider_secret_version: int = PROVIDER_SECRET_VERSION,
) -> OpaqueConnectorSecretRef:
    return OpaqueConnectorSecretRef(
        REFERENCE,
        provider_secret_version=provider_secret_version,
    )


def _assert_sanitized(error: BaseException) -> None:
    rendered = f"{error} {error!r}".casefold()
    for protected in (
        REFERENCE,
        WORKLOAD_TOKEN,
        CLIENT_TOKEN,
        TLS_DSN,
        "synthetic-password",
        "source.example.test",
        "secrets.example.test",
        "tenant-connectors",
        "schemabridge-worker",
    ):
        assert protected.casefold() not in rendered


def test_remote_resolver_authenticates_once_and_reads_only_exact_hashed_version(
    tmp_path: Path,
) -> None:
    resolver, identity, transport = _resolver(tmp_path)
    reference = _reference()

    secret = resolver.resolve(reference, _target())

    assert identity.calls == 1
    assert secret.dsn == TLS_DSN
    assert secret.expected_reader == "schemabridge_reader"
    assert TLS_DSN not in repr(secret)
    assert REFERENCE not in repr(resolver)
    assert CLIENT_TOKEN not in repr(resolver)
    assert len(transport.calls) == 2
    login, read = transport.calls
    assert login["method"] == "POST"
    assert login["url"] == "https://secrets.example.test/v1/auth/jwt/login"
    login_body = login["body"]
    assert isinstance(login_body, bytes)
    assert WORKLOAD_TOKEN in login_body.decode()
    assert read["method"] == "GET"
    assert read["url"] == (
        "https://secrets.example.test/v1/tenant-connectors/data/schemabridge/"
        f"execution/{reference.digest}?version={PROVIDER_SECRET_VERSION}"
    )
    assert REFERENCE not in str(read)
    assert read["body"] is None
    headers = read["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Vault-Token"] == CLIENT_TOKEN


@pytest.mark.parametrize(
    ("responses", "expected"),
    (
        ([json.dumps({"auth": {}}).encode()], ConnectorSecretErrorCode.IDENTITY_REJECTED),
        (
            [_login_response(), _secret_response(version=18)],
            ConnectorSecretErrorCode.VERSION_UNAVAILABLE,
        ),
        (
            [_login_response(), _secret_response(destroyed=True)],
            ConnectorSecretErrorCode.VERSION_UNAVAILABLE,
        ),
        (
            [_login_response(), _secret_response(deletion_time="2026-07-29T00:00:00Z")],
            ConnectorSecretErrorCode.VERSION_UNAVAILABLE,
        ),
        (
            [
                _login_response(),
                _secret_response(document=_secret_document(expected_reader="other_reader")),
            ],
            ConnectorSecretErrorCode.TARGET_MISMATCH,
        ),
        (
            [
                _login_response(),
                _secret_response(
                    document=_secret_document(
                        dsn=(
                            "postgresql://schemabridge_reader:synthetic-password"
                            "@source.example.test:5432/analytics"
                        )
                    )
                ),
            ],
            ConnectorSecretErrorCode.INVALID,
        ),
    ),
)
def test_remote_resolver_fails_closed_with_stable_sanitized_codes(
    tmp_path: Path,
    responses: list[bytes],
    expected: ConnectorSecretErrorCode,
) -> None:
    transport = _Transport(list(responses))
    resolver, _, _ = _resolver(tmp_path, transport=transport)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        resolver.resolve(_reference(), _target())

    assert captured.value.code is expected
    assert captured.value.__cause__ is None
    _assert_sanitized(captured.value)


@pytest.mark.parametrize(
    ("responses", "expected_code", "expected_calls"),
    (
        (
            [_with_extra_key(_login_response(), path=())],
            ConnectorSecretErrorCode.IDENTITY_REJECTED,
            1,
        ),
        (
            [_with_extra_key(_login_response(), path=("auth",))],
            ConnectorSecretErrorCode.IDENTITY_REJECTED,
            1,
        ),
        (
            [
                _login_response(),
                _with_extra_key(_secret_response(), path=()),
            ],
            ConnectorSecretErrorCode.INVALID,
            2,
        ),
        (
            [
                _login_response(),
                _with_extra_key(_secret_response(), path=("data",)),
            ],
            ConnectorSecretErrorCode.INVALID,
            2,
        ),
        (
            [
                _login_response(),
                _with_extra_key(_secret_response(), path=("data", "metadata")),
            ],
            ConnectorSecretErrorCode.INVALID,
            2,
        ),
        (
            [
                _login_response(),
                _secret_response(
                    document=_secret_document(unsupported_provider_field="private-provider-value")
                ),
            ],
            ConnectorSecretErrorCode.INVALID,
            2,
        ),
    ),
)
def test_remote_resolver_rejects_unknown_provider_shapes_without_source_io(
    tmp_path: Path,
    responses: list[bytes],
    expected_code: ConnectorSecretErrorCode,
    expected_calls: int,
) -> None:
    transport = _Transport(list(responses))
    resolver, _, _ = _resolver(tmp_path, transport=transport)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        resolver.resolve(_reference(), _target())

    assert captured.value.code is expected_code
    assert len(transport.calls) == expected_calls
    _assert_sanitized(captured.value)


def test_remote_resolver_accepts_only_documented_standard_vault_metadata(
    tmp_path: Path,
) -> None:
    login = json.loads(_login_response())
    login["request_id"] = "synthetic-request"
    login["mount_type"] = "jwt"
    login["auth"]["accessor"] = "synthetic-accessor"
    login["auth"]["policies"] = ["default"]
    secret_response = json.loads(_secret_response())
    secret_response["request_id"] = "synthetic-request"
    secret_response["data"]["metadata"]["created_time"] = "2026-07-29T00:00:00Z"
    secret_response["data"]["metadata"]["custom_metadata"] = None
    transport = _Transport(
        [
            json.dumps(login, sort_keys=True).encode(),
            json.dumps(secret_response, sort_keys=True).encode(),
        ]
    )
    resolver, _, _ = _resolver(tmp_path, transport=transport)

    secret = resolver.resolve(_reference(), _target(route_revision=700))

    assert secret.expected_reader == "schemabridge_reader"
    assert len(transport.calls) == 2


def test_remote_resolver_identity_or_provider_failure_performs_no_later_read(
    tmp_path: Path,
) -> None:
    identity_error = ConnectorSecretResolutionError(
        ConnectorSecretErrorCode.IDENTITY_REJECTED,
        "connector secret identity was rejected",
    )
    identity = _Identity(error=identity_error)
    resolver, _, transport = _resolver(tmp_path, identity=identity)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        resolver.resolve(_reference(), _target())

    assert captured.value.code is ConnectorSecretErrorCode.IDENTITY_REJECTED
    assert transport.calls == []
    _assert_sanitized(captured.value)

    provider_error = ConnectorSecretResolutionError(
        ConnectorSecretErrorCode.UNAVAILABLE,
        "connector secret is unavailable",
    )
    provider_transport = _Transport([], error=provider_error)
    resolver, _, _ = _resolver(tmp_path, transport=provider_transport)
    with pytest.raises(ConnectorSecretResolutionError) as provider_capture:
        resolver.resolve(_reference(), _target())
    assert provider_capture.value.code is ConnectorSecretErrorCode.UNAVAILABLE
    assert len(provider_transport.calls) == 1
    _assert_sanitized(provider_capture.value)


def test_remote_route_never_reads_latest_or_an_unbound_version(tmp_path: Path) -> None:
    resolver, identity, transport = _resolver(tmp_path)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        resolver.resolve_postgres_route(
            OpaqueConnectorSecretRef(REFERENCE),
            dialect=SourceDialect.POSTGRESQL,
            expected_reader="schemabridge_reader",
        )

    assert captured.value.code is ConnectorSecretErrorCode.TARGET_MISMATCH
    assert identity.calls == 0
    assert transport.calls == []
    _assert_sanitized(captured.value)

    with pytest.raises(ConnectorSecretResolutionError) as substituted:
        resolver.read_document(_reference(), version=PROVIDER_SECRET_VERSION + 1)

    assert substituted.value.code is ConnectorSecretErrorCode.TARGET_MISMATCH
    assert identity.calls == 0
    assert transport.calls == []


def test_rotation_activates_only_the_new_exact_version_and_rejects_revoked_old_version(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        [
            _login_response(),
            _secret_response(version=19),
            _login_response(),
            _secret_response(version=20),
            _login_response(),
            _secret_response(version=19, destroyed=True),
        ]
    )
    resolver, identity, _ = _resolver(tmp_path, transport=transport)
    version_19 = _reference(19)
    version_20 = _reference(20)

    before = resolver.resolve(version_19, _target(route_revision=70))
    after = resolver.resolve(version_20, _target(route_revision=80))
    with pytest.raises(ConnectorSecretResolutionError) as revoked:
        resolver.resolve(version_19, _target(route_revision=90))

    assert before.expected_reader == after.expected_reader == "schemabridge_reader"
    assert revoked.value.code is ConnectorSecretErrorCode.VERSION_UNAVAILABLE
    assert identity.calls == 3
    read_urls = [str(call["url"]) for call in transport.calls if call["method"] == "GET"]
    assert [url.rsplit("=", 1)[-1] for url in read_urls] == ["19", "20", "19"]
    assert all("version=" in url for url in read_urls)
    assert all("latest" not in url for url in read_urls)
    _assert_sanitized(revoked.value)


@pytest.mark.parametrize(
    "updates",
    (
        {"server": "http://secrets.example.test"},
        {"server": "https://user:password@secrets.example.test"},
        {"server": "https://secrets.example.test/prefix"},
        {"server": "https://secrets.example.test?token=private"},
        {"role": "../worker"},
        {"kv_mount": "tenant/connectors"},
        {"auth_mount": "jwt/path"},
        {"timeout_seconds": 0.01},
        {"max_response_bytes": 1},
    ),
)
def test_remote_resolver_rejects_unsafe_configuration(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "server": "https://secrets.example.test",
        "role": "schemabridge-worker",
        "kv_mount": "tenant-connectors",
        "capability": ConnectorSecretCapability.EXECUTION,
        "identity": _Identity(),
        "ca_bundle": _ca_bundle(tmp_path),
        "transport": _Transport([]),
    }
    values.update(updates)
    with pytest.raises(ValueError, match="configuration is invalid"):
        VaultKvV2ConnectorSecretResolver(**values)  # type: ignore[arg-type]


def test_remote_resolver_missing_trust_bundle_has_no_path_bearing_exception_chain(
    tmp_path: Path,
) -> None:
    missing = (tmp_path / "private-provider-name" / "missing-ca.crt").resolve()

    with pytest.raises(ValueError) as captured:
        VaultKvV2ConnectorSecretResolver(
            server="https://secrets.example.test",
            role="schemabridge-worker",
            kv_mount="tenant-connectors",
            capability=ConnectorSecretCapability.EXECUTION,
            identity=_Identity(),
            ca_bundle=missing,
            transport=_Transport([]),
        )

    assert str(captured.value) == "remote connector secret trust bundle is unavailable"
    assert captured.value.__cause__ is None
    assert str(missing) not in f"{captured.value} {captured.value!r}"


def _b64(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt(
    *,
    audience: object = "schemabridge-secret-manager",
    expires: int = 1_700_000_600,
    not_before: int = 1_699_999_995,
) -> str:
    return ".".join(
        (
            _b64({"alg": "RS256", "typ": "JWT"}),
            _b64(
                {
                    "aud": audience,
                    "exp": expires,
                    "iat": 1_700_000_000,
                    "iss": "https://kubernetes.default.svc.cluster.local",
                    "nbf": not_before,
                    "sub": "system:serviceaccount:schemabridge:schemabridge-worker",
                }
            ),
            _b64({"synthetic": "signature"}),
        )
    )


def _projected_identity(
    tmp_path: Path,
    token: str,
) -> ProjectedServiceAccountIdentity:
    root = (tmp_path / "projected").resolve()
    version = root / "..2026_07_29"
    version.mkdir(parents=True)
    target = version / "token"
    target.write_text(token, encoding="ascii")
    target.chmod(0o400)
    current = root / "..data"
    current.symlink_to(version.name)
    projected = root / "token"
    projected.symlink_to("..data/token")
    return ProjectedServiceAccountIdentity(
        token_file=projected,
        mount_root=root,
        audience="schemabridge-secret-manager",
        now_epoch=lambda: 1_700_000_000,
    )


def test_projected_identity_accepts_a_stable_audience_bound_kubernetes_projection(
    tmp_path: Path,
) -> None:
    token = _jwt()
    identity = _projected_identity(tmp_path, token)

    assert identity.read() == token
    assert token not in repr(identity)


@pytest.mark.parametrize(
    "token",
    (
        _jwt(audience="other-audience"),
        _jwt(expires=1_700_000_005),
        _jwt(expires=1_700_003_601),
        _jwt(not_before=1_700_000_006),
        "not-a-jwt",
        " header.payload.signature",
    ),
)
def test_projected_identity_rejects_wrong_audience_time_or_shape(
    tmp_path: Path,
    token: str,
) -> None:
    identity = _projected_identity(tmp_path, token)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        identity.read()

    assert captured.value.code is ConnectorSecretErrorCode.IDENTITY_REJECTED
    assert captured.value.__cause__ is None
    assert token not in str(captured.value)


def test_projected_identity_rejects_writable_or_out_of_root_material(
    tmp_path: Path,
) -> None:
    identity = _projected_identity(tmp_path, _jwt())
    resolved = identity.token_file.resolve(strict=True)
    resolved.chmod(0o622)

    with pytest.raises(ConnectorSecretResolutionError) as writable:
        identity.read()
    assert writable.value.code is ConnectorSecretErrorCode.IDENTITY_REJECTED

    outside = (tmp_path / "outside-token").resolve()
    outside.write_text(_jwt(), encoding="ascii")
    outside.chmod(0o400)
    with pytest.raises(ValueError, match="configuration is invalid"):
        ProjectedServiceAccountIdentity(
            token_file=outside,
            mount_root=identity.mount_root,
            audience="schemabridge-secret-manager",
        )


def test_remote_secret_types_and_errors_hide_every_private_value(tmp_path: Path) -> None:
    resolver, identity, _ = _resolver(tmp_path)
    reference = _reference()

    combined = " ".join(
        (
            repr(resolver),
            repr(identity),
            repr(reference),
        )
    )

    assert REFERENCE not in combined
    assert WORKLOAD_TOKEN not in combined
    assert CLIENT_TOKEN not in combined
    assert TLS_DSN not in combined
    assert "synthetic-password" not in combined
    assert os.fspath(resolver.ca_bundle) not in repr(resolver)


class _ResponseHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _HttpResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = _ResponseHeaders(content_type)
        self._body = body
        self._offset = 0

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _Opener:
    def __init__(
        self,
        response: _HttpResponse | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def open(self, _request: object, *, timeout: float) -> _HttpResponse:
        assert timeout == 1.0
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_urllib_transport_enforces_default_hostname_verification_tls_floor_and_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ssl.create_default_context()
    ca_bundle = _ca_bundle(tmp_path)
    observed_ca: list[str] = []
    opener = _Opener(_HttpResponse(b'{"ok":true}'))

    def create_context(*, cafile: str) -> ssl.SSLContext:
        observed_ca.append(cafile)
        return context

    monkeypatch.setattr(
        "schemabridge.adapters.connectors.remote_secrets.ssl.create_default_context",
        create_context,
    )
    monkeypatch.setattr(remote_secret_module, "build_opener", lambda *_handlers: opener)

    result = UrllibRemoteSecretTransport().request(
        method="GET",
        url="https://secrets.example.test/v1/safe",
        headers={"Accept": "application/json"},
        body=None,
        ca_bundle=ca_bundle,
        timeout_seconds=1.0,
        max_response_bytes=64,
    )

    assert result == b'{"ok":true}'
    assert observed_ca == [str(ca_bundle)]
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert opener.calls == 1


@pytest.mark.parametrize(
    "response",
    (
        _HttpResponse(b"", status=200),
        _HttpResponse(b"{}", status=302),
        _HttpResponse(b"{}", content_type="text/plain"),
        _HttpResponse(b"x" * 66),
    ),
)
def test_urllib_transport_rejects_empty_redirect_non_json_and_oversized_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: _HttpResponse,
) -> None:
    context = ssl.create_default_context()
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.remote_secrets.ssl.create_default_context",
        lambda *, cafile: context,
    )
    monkeypatch.setattr(
        remote_secret_module,
        "build_opener",
        lambda *_handlers: _Opener(response),
    )

    with pytest.raises(ConnectorSecretResolutionError) as raised:
        UrllibRemoteSecretTransport().request(
            method="GET",
            url="https://secrets.example.test/v1/safe",
            headers={},
            body=None,
            ca_bundle=_ca_bundle(tmp_path),
            timeout_seconds=1.0,
            max_response_bytes=64,
        )

    assert raised.value.code is ConnectorSecretErrorCode.UNAVAILABLE
    _assert_sanitized(raised.value)


@pytest.mark.parametrize("failure", (TimeoutError(), OSError("bad CA or hostname")))
def test_urllib_transport_sanitizes_tls_timeout_or_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    context = ssl.create_default_context()
    monkeypatch.setattr(
        "schemabridge.adapters.connectors.remote_secrets.ssl.create_default_context",
        lambda *, cafile: context,
    )
    monkeypatch.setattr(
        remote_secret_module,
        "build_opener",
        lambda *_handlers: _Opener(error=failure),
    )

    with pytest.raises(ConnectorSecretResolutionError) as raised:
        UrllibRemoteSecretTransport().request(
            method="GET",
            url="https://secrets.example.test/v1/safe",
            headers={},
            body=None,
            ca_bundle=_ca_bundle(tmp_path),
            timeout_seconds=1.0,
            max_response_bytes=64,
        )

    assert raised.value.code is ConnectorSecretErrorCode.UNAVAILABLE
    assert raised.value.__cause__ is None
    assert "bad CA or hostname" not in str(raised.value)
