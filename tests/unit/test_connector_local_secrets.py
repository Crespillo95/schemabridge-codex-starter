from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from schemabridge.adapters.connectors import local_secrets
from schemabridge.adapters.connectors.local_secrets import (
    MAX_CONNECTOR_SECRET_BYTES,
    ConnectorSecretErrorCode,
    ConnectorSecretResolutionError,
    OpaqueConnectorSecretRef,
    OwnerOnlyConnectorSecretResolver,
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

ROUTE_FINGERPRINT = "a" * 64
SOURCE_IDENTITY_FINGERPRINT = fingerprint_payload({"source": "tenant-a-primary"})
CATALOG_IDENTITY_FINGERPRINT = fingerprint_payload({"catalog": "tenant-a-primary"})
TYPE_FINGERPRINT = postgres_type_contract_fingerprint()
REFERENCE = "execution.tenant-a.primary-v1"
DSN = "postgresql://schemabridge_reader:synthetic-password@source.example.test:5432/analytics"


def _budget() -> QueryCostBudget:
    return QueryCostBudget(
        explain_timeout_ms=1_000,
        max_response_bytes=64 * 1_024,
        max_total_cost=Decimal("10000"),
        max_estimated_rows=100_000,
        max_plan_nodes=1_000,
        max_plan_depth=32,
        max_plan_width=16_384,
    )


def _target(**updates: object) -> GovernedExecutionTarget:
    budget = _budget()
    values: dict[str, object] = {
        "workspace_id": "tenant_a",
        "connection_id": CatalogConnectionId("warehouse"),
        "connector_kind": SourceConnectorKind.POSTGRESQL,
        "dialect": SourceDialect.POSTGRESQL,
        "route_revision": 1,
        "route_fingerprint": ROUTE_FINGERPRINT,
        "expected_reader": "schemabridge_reader",
        "source_identity_fingerprint": SOURCE_IDENTITY_FINGERPRINT,
        "catalog_identity_fingerprint": CATALOG_IDENTITY_FINGERPRINT,
        "type_contract_fingerprint": TYPE_FINGERPRINT,
        "cost_budget": budget,
        "cost_budget_fingerprint": budget.fingerprint,
    }
    values.update(updates)
    return GovernedExecutionTarget.model_validate(values)


def _document(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "format_version": 1,
        "dialect": "postgresql",
        "expected_reader": "schemabridge_reader",
        "dsn": DSN,
    }
    values.update(updates)
    return values


def _secret_directory(tmp_path: Path) -> Path:
    directory = (tmp_path / "connector-secrets").resolve()
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def _secret_path(directory: Path, reference: str = REFERENCE) -> Path:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _write_secret(
    directory: Path,
    *,
    document: dict[str, object] | None = None,
    raw: bytes | None = None,
    mode: int = 0o600,
) -> Path:
    path = _secret_path(directory)
    payload = (
        raw
        if raw is not None
        else json.dumps(document or _document(), sort_keys=True).encode("utf-8")
    )
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def _resolver(directory: Path) -> OwnerOnlyConnectorSecretResolver:
    return OwnerOnlyConnectorSecretResolver(directory)


def _assert_sanitized(
    error: ConnectorSecretResolutionError,
    *,
    directory: Path,
) -> None:
    rendered = f"{error} {error!r}".casefold()
    for protected in (
        REFERENCE,
        str(directory),
        DSN,
        "schemabridge_reader",
        "source.example.test",
        "synthetic-password",
    ):
        assert protected.casefold() not in rendered
    assert error.__cause__ is None


def test_owner_only_resolver_uses_sha256_reference_filename_and_returns_private_value(
    tmp_path: Path,
) -> None:
    directory = _secret_directory(tmp_path)
    path = _write_secret(directory)
    reference = OpaqueConnectorSecretRef(REFERENCE)

    secret = _resolver(directory).resolve(reference, _target())

    assert path.name == reference.filename
    assert REFERENCE not in path.name
    assert secret.dsn == DSN
    assert secret.expected_reader == "schemabridge_reader"
    assert secret.dialect is SourceDialect.POSTGRESQL
    rendered = repr(secret)
    assert DSN not in rendered
    assert "schemabridge_reader" not in rendered
    assert "source.example.test" not in rendered
    assert "synthetic-password" not in rendered
    assert not hasattr(secret, "model_dump")
    assert DSN not in json.dumps(secret, default=repr)


def test_owner_only_resolver_accepts_an_exact_lease_validated_postgres_route(
    tmp_path: Path,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory)

    secret = _resolver(directory).resolve_postgres_route(
        OpaqueConnectorSecretRef(REFERENCE),
        dialect=SourceDialect.POSTGRESQL,
        expected_reader="schemabridge_reader",
    )

    assert secret.dsn == DSN
    assert secret.expected_reader == "schemabridge_reader"
    assert DSN not in repr(secret)
    assert REFERENCE not in repr(secret)


def test_lease_validated_postgres_route_still_requires_exact_secret_metadata(
    tmp_path: Path,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve_postgres_route(
            OpaqueConnectorSecretRef(REFERENCE),
            dialect=SourceDialect.POSTGRESQL,
            expected_reader="other_reader",
        )

    assert captured.value.code is ConnectorSecretErrorCode.TARGET_MISMATCH
    _assert_sanitized(captured.value, directory=directory)


def test_reference_and_resolver_representations_hide_private_values(tmp_path: Path) -> None:
    directory = _secret_directory(tmp_path)
    reference = OpaqueConnectorSecretRef(REFERENCE)
    resolver = _resolver(directory)

    assert REFERENCE not in repr(reference)
    assert str(directory) not in repr(resolver)


@pytest.mark.parametrize(
    "reference",
    (
        "",
        "ab",
        "../secret",
        "execution/secret",
        "postgresql://source/private",
        "reader@source",
        "UPPERCASE",
        "a" * 201,
    ),
)
def test_opaque_reference_rejects_traversal_and_credential_shapes(reference: str) -> None:
    with pytest.raises(ConnectorSecretResolutionError) as captured:
        OpaqueConnectorSecretRef(reference)

    assert captured.value.code is ConnectorSecretErrorCode.UNAVAILABLE
    assert str(captured.value) == "connector secret is unavailable"


def test_resolver_requires_an_absolute_bounded_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configuration is invalid"):
        OwnerOnlyConnectorSecretResolver(Path("relative/secrets"))
    with pytest.raises(ValueError, match="configuration is invalid"):
        OwnerOnlyConnectorSecretResolver(
            tmp_path.resolve(),
            max_document_bytes=MAX_CONNECTOR_SECRET_BYTES + 1,
        )


@pytest.mark.parametrize("mode", (0o755, 0o750, 0o710, 0o600))
def test_secret_directory_must_be_exact_owner_only_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory)
    directory.chmod(mode)

    try:
        with pytest.raises(ConnectorSecretResolutionError) as captured:
            _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())
    finally:
        directory.chmod(0o700)

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _assert_sanitized(captured.value, directory=directory)


def test_secret_directory_symlink_is_rejected(tmp_path: Path) -> None:
    real_directory = _secret_directory(tmp_path)
    _write_secret(real_directory)
    linked_directory = tmp_path / "linked-secrets"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(linked_directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _assert_sanitized(captured.value, directory=linked_directory)


@pytest.mark.parametrize("mode", (0o400, 0o640, 0o700, 0o660, 0o604))
def test_secret_file_requires_exact_0600_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory, mode=mode)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _assert_sanitized(captured.value, directory=directory)


def test_secret_file_must_have_the_effective_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory)
    actual_uid = os.geteuid()
    monkeypatch.setattr(local_secrets.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _assert_sanitized(captured.value, directory=directory)


def test_secret_file_cannot_be_a_symlink_or_directory(tmp_path: Path) -> None:
    directory = _secret_directory(tmp_path)
    target = directory / "actual.json"
    target.write_text(json.dumps(_document()), encoding="utf-8")
    target.chmod(0o600)
    _secret_path(directory).symlink_to(target)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())
    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _secret_path(directory).unlink()
    _secret_path(directory).mkdir(mode=0o700)
    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())
    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE


@pytest.mark.parametrize("raw", (b"", b"x" * (MAX_CONNECTOR_SECRET_BYTES + 1)))
def test_secret_file_size_is_bounded(tmp_path: Path, raw: bytes) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory, raw=raw)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE


def test_missing_hashed_file_is_sanitized_unavailable(tmp_path: Path) -> None:
    directory = _secret_directory(tmp_path)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNAVAILABLE
    _assert_sanitized(captured.value, directory=directory)


def test_secret_reader_uses_nofollow_for_directory_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory)
    real_open = local_secrets.os.open
    observed_flags: list[int] = []

    def capture_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        observed_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local_secrets.os, "open", capture_open)

    _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert len(observed_flags) == 2
    assert all(flags & os.O_NOFOLLOW for flags in observed_flags)


def test_secret_change_during_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _secret_directory(tmp_path)
    path = _write_secret(directory)
    real_read_exact = local_secrets._read_exact

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        raw = real_read_exact(descriptor, size)
        changed = raw.replace(b"synthetic-password", b"modified--password")
        assert len(changed) == len(raw)
        path.write_bytes(changed)
        path.chmod(0o600)
        return raw

    monkeypatch.setattr(local_secrets, "_read_exact", mutate_after_read)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.UNSAFE
    _assert_sanitized(captured.value, directory=directory)


@pytest.mark.parametrize(
    "raw",
    (
        b"\xff",
        b"{}",
        b'{"format_version":1,"format_version":1}',
        b'{"format_version":1,"dialect":"postgresql",'
        b'"expected_reader":"schemabridge_reader","dsn":"x","extra":"x"}',
        b'{"format_version":"1","dialect":"postgresql",'
        b'"expected_reader":"schemabridge_reader","dsn":"x"}',
    ),
)
def test_secret_document_requires_unique_exact_bounded_json(
    tmp_path: Path,
    raw: bytes,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory, raw=raw)

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.INVALID
    _assert_sanitized(captured.value, directory=directory)


@pytest.mark.parametrize(
    "updates",
    (
        {"dialect": "mysql"},
        {"expected_reader": "different_reader"},
    ),
)
def test_document_dialect_and_expected_reader_must_match_target(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory, document=_document(**updates))

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.TARGET_MISMATCH
    _assert_sanitized(captured.value, directory=directory)


@pytest.mark.parametrize(
    "dsn",
    (
        "postgres://schemabridge_reader:password@source.example.test/analytics",
        "user=schemabridge_reader password=secret host=source.example.test dbname=analytics",
        "postgresql://schemabridge_reader@source.example.test/analytics",
        "postgresql:///analytics",
        "postgresql://schemabridge_reader:password@source.example.test/",
        "postgresql://schemabridge_reader:password@source.example.test/a/b",
        "postgresql://schemabridge_reader:password@source.example.test/analytics#fragment",
        "postgresql://schemabridge_reader:password@source.example.test/analytics?user=other",
        "postgresql://schemabridge_reader:password@source.example.test/analytics?password=other",
        "postgresql://other_reader:password@source.example.test/analytics",
        "postgresql://schemabridge_reader:password@source-a,source-b/analytics",
        "postgresql://schemabridge_reader:password@source.example.test:99999/analytics",
        " postgresql://schemabridge_reader:password@source.example.test/analytics",
    ),
)
def test_postgres_dsn_is_strict_and_matches_expected_reader(
    tmp_path: Path,
    dsn: str,
) -> None:
    directory = _secret_directory(tmp_path)
    _write_secret(directory, document=_document(dsn=dsn))

    with pytest.raises(ConnectorSecretResolutionError) as captured:
        _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert captured.value.code is ConnectorSecretErrorCode.INVALID
    _assert_sanitized(captured.value, directory=directory)


def test_valid_percent_encoded_postgres_dsn_and_safe_libpq_options_are_accepted(
    tmp_path: Path,
) -> None:
    directory = _secret_directory(tmp_path)
    dsn = (
        "postgresql://schemabridge_reader:synthetic%40password@"
        "source.example.test/analytics?sslmode=verify-full&connect_timeout=3"
    )
    _write_secret(directory, document=_document(dsn=dsn))

    secret = _resolver(directory).resolve(OpaqueConnectorSecretRef(REFERENCE), _target())

    assert secret.dsn == dsn
