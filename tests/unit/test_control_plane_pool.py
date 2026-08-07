from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolTimeout, TooManyRequests

from schemabridge.adapters.control_plane.postgres_pool import (
    ControlPoolSettings,
    ControlPoolUnavailable,
    PostgresControlPool,
    _Pool,
)


class _FakeConnection:
    pass


class _FakePool:
    def __init__(self) -> None:
        self.closed = True
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls: list[float] = []
        self.acquisition_timeouts: list[float | None] = []
        self.open_error: Exception | None = None
        self.connection_error: Exception | None = None
        self.connection_value = _FakeConnection()

    def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls.append((wait, timeout))
        if self.open_error is not None:
            raise self.open_error
        self.closed = False

    def close(self, *, timeout: float) -> None:
        self.close_calls.append(timeout)
        self.closed = True

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Any]:
        self.acquisition_timeouts.append(timeout)
        if self.connection_error is not None:
            raise self.connection_error
        yield self.connection_value

    def get_stats(self) -> dict[str, int]:
        return {"pool_size": 3, "pool_available": 2, "requests_waiting": 0}


def _settings() -> ControlPoolSettings:
    return ControlPoolSettings(
        dsn="postgresql://example.invalid/control",
        application_name="schemabridge-control-api",
        min_size=1,
        max_size=8,
        max_waiting=16,
        acquisition_timeout_seconds=0.25,
        startup_timeout_seconds=3,
    )


def _pool(fake: _FakePool) -> PostgresControlPool:
    return PostgresControlPool(_settings(), _pool=cast(_Pool, fake))


def test_pool_settings_are_secret_safe_and_bounded() -> None:
    settings = _settings()

    assert "example.invalid" not in repr(settings)
    assert settings.max_size == 8
    with pytest.raises(ValueError, match="size"):
        ControlPoolSettings(
            dsn="postgresql://example.invalid/control",
            application_name="schemabridge-control-api",
            min_size=2,
            max_size=1,
            max_waiting=1,
            acquisition_timeout_seconds=1,
            startup_timeout_seconds=1,
        )
    with pytest.raises(ValueError, match="application name"):
        ControlPoolSettings(
            dsn="postgresql://example.invalid/control",
            application_name="unbounded-client",
            min_size=1,
            max_size=1,
            max_waiting=1,
            acquisition_timeout_seconds=1,
            startup_timeout_seconds=1,
        )


def test_pool_requires_explicit_start_and_closes_idempotently() -> None:
    fake = _FakePool()
    pool = _pool(fake)

    with (
        pytest.raises(ControlPoolUnavailable, match="pool is unavailable"),
        pool.connection(),
    ):
        raise AssertionError("unreachable")

    pool.open()
    pool.open()
    assert fake.open_calls == [(True, 3)]

    with pool.connection() as connection:
        assert cast(object, connection) is fake.connection_value
    assert fake.acquisition_timeouts == [0.25]
    assert pool.stats()["pool_size"] == 3

    pool.close()
    pool.close()
    assert fake.close_calls == [5.0]


@pytest.mark.parametrize("error", [PoolTimeout("full"), TooManyRequests("queued")])
def test_pool_saturation_is_one_sanitized_operational_failure(error: Exception) -> None:
    fake = _FakePool()
    pool = _pool(fake)
    pool.open()
    fake.connection_error = error

    with pytest.raises(ControlPoolUnavailable) as captured, pool.connection():
        raise AssertionError("unreachable")

    assert str(captured.value) == "control database pool is unavailable"
    assert "full" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert isinstance(captured.value, psycopg.OperationalError)


def test_pool_startup_failure_does_not_mark_it_ready() -> None:
    fake = _FakePool()
    fake.open_error = PoolTimeout("secret endpoint")
    pool = _pool(fake)

    with pytest.raises(ControlPoolUnavailable) as captured:
        pool.open()

    assert not pool.started
    assert str(captured.value) == "control database pool is unavailable"
    assert "secret endpoint" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_upstream_pool_logger_is_sanitized_before_any_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _pool(_FakePool())
    caplog.set_level(logging.WARNING, logger="psycopg.pool")

    logging.getLogger("psycopg.pool").warning(
        "error connecting user=%s host=%s password=%s",
        "schemabridge_observer",
        "private-control.example",
        "super-secret",
    )

    assert "control_pool_internal_event" in caplog.text
    assert "schemabridge_observer" not in caplog.text
    assert "private-control.example" not in caplog.text
    assert "super-secret" not in caplog.text
