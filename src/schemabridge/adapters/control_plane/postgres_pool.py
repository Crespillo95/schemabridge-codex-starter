"""Lifecycle-managed, bounded PostgreSQL pools for control-plane processes."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

_APPLICATION_NAME = re.compile(
    r"^schemabridge-control-(?:api|worker|catalog|runtime|reconciler|migrator)$"
)


class ControlPoolUnavailable(psycopg.OperationalError):
    """Sanitized capacity or lifecycle failure at the pool boundary."""


@dataclass(frozen=True, slots=True)
class ControlPoolSettings:
    """Validated process-local pool envelope.

    ``max_size`` is a per-process limit. Deployment capacity must also account for the
    configured replica count.
    """

    dsn: str = field(repr=False)
    application_name: str
    min_size: int
    max_size: int
    max_waiting: int
    acquisition_timeout_seconds: float
    startup_timeout_seconds: float
    close_timeout_seconds: float = 5.0
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 5_000
    max_idle_seconds: float = 300.0
    max_lifetime_seconds: float = 1_800.0

    def __post_init__(self) -> None:
        if not self.dsn.strip():
            raise ValueError("control pool DSN must not be blank")
        if _APPLICATION_NAME.fullmatch(self.application_name) is None:
            raise ValueError("control pool application name is invalid")
        if not 1 <= self.min_size <= self.max_size <= 64:
            raise ValueError("control pool size is invalid")
        if not 1 <= self.max_waiting <= 10_000:
            raise ValueError("control pool waiter limit is invalid")
        if not 0.05 <= self.acquisition_timeout_seconds <= 30:
            raise ValueError("control pool acquisition timeout is invalid")
        if not 1 <= self.startup_timeout_seconds <= 60:
            raise ValueError("control pool startup timeout is invalid")
        if not 0.1 <= self.close_timeout_seconds <= 30:
            raise ValueError("control pool close timeout is invalid")
        if not 1 <= self.connect_timeout_seconds <= 30:
            raise ValueError("control pool connect timeout is invalid")
        if not 100 <= self.statement_timeout_ms <= 60_000:
            raise ValueError("control pool statement timeout is invalid")
        if not 1 <= self.max_idle_seconds <= 3_600:
            raise ValueError("control pool maximum idle time is invalid")
        if not 60 <= self.max_lifetime_seconds <= 86_400:
            raise ValueError("control pool maximum lifetime is invalid")


class _Pool(Protocol):
    @property
    def closed(self) -> bool: ...

    def open(self, *, wait: bool, timeout: float) -> None: ...

    def close(self, *, timeout: float) -> None: ...

    def connection(
        self,
        timeout: float | None = None,
    ) -> AbstractContextManager[psycopg.Connection[Any]]: ...

    def get_stats(self) -> dict[str, int]: ...


class PostgresControlPool:
    """Explicitly opened pool shared by one API, worker, or indexer process."""

    def __init__(
        self,
        settings: ControlPoolSettings,
        *,
        _pool: _Pool | None = None,
    ) -> None:
        self._settings = settings
        self._started = False
        self._pool: _Pool = _pool or ConnectionPool(
            conninfo=settings.dsn,
            kwargs={
                "application_name": settings.application_name,
                "connect_timeout": settings.connect_timeout_seconds,
            },
            min_size=settings.min_size,
            max_size=settings.max_size,
            open=False,
            configure=self._configure_connection,
            check=ConnectionPool.check_connection,
            timeout=settings.acquisition_timeout_seconds,
            max_waiting=settings.max_waiting,
            max_idle=settings.max_idle_seconds,
            max_lifetime=settings.max_lifetime_seconds,
            reconnect_timeout=settings.startup_timeout_seconds,
            name=settings.application_name,
        )

    @property
    def settings(self) -> ControlPoolSettings:
        return self._settings

    @property
    def started(self) -> bool:
        return self._started and not self._pool.closed

    def open(self) -> None:
        """Start the pool and fail startup unless its minimum is ready."""

        if self.started:
            return
        try:
            self._pool.open(
                wait=True,
                timeout=self._settings.startup_timeout_seconds,
            )
        except (PoolTimeout, PoolClosed, TooManyRequests) as error:
            self._started = False
            raise ControlPoolUnavailable("control database pool is unavailable") from error
        self._started = True

    def close(self) -> None:
        """Release background workers and pooled connections idempotently."""

        if self._pool.closed:
            self._started = False
            return
        self._pool.close(timeout=self._settings.close_timeout_seconds)
        self._started = False

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """Acquire one bounded connection or expose one sanitized failure."""

        if not self.started:
            raise ControlPoolUnavailable("control database pool is unavailable")
        try:
            with self._pool.connection(
                timeout=self._settings.acquisition_timeout_seconds,
            ) as connection:
                yield connection
        except (PoolTimeout, PoolClosed, TooManyRequests) as error:
            raise ControlPoolUnavailable("control database pool is unavailable") from error

    def stats(self) -> Mapping[str, int]:
        """Return numeric pool metrics without connection material."""

        return dict(self._pool.get_stats())

    def _configure_connection(self, connection: psycopg.Connection[Any]) -> None:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{self._settings.statement_timeout_ms}ms",),
        )
        connection.commit()

    def __enter__(self) -> PostgresControlPool:
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
