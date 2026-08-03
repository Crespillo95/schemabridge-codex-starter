"""Bounded periodic heartbeat supervision for registry publication operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Lock, Thread
from typing import TypeVar, cast

from schemabridge.application.ports.registry_publication import (
    RegistryPublicationHeartbeatSupervisorError,
    RegistryPublicationJobWorkerStorePort,
)
from schemabridge.domain.registry_publication_jobs import RegistryPublicationJob

OperationResultT = TypeVar("OperationResultT")


@dataclass(frozen=True, slots=True)
class ThreadedRegistryPublicationHeartbeatSupervisor:
    job_store: RegistryPublicationJobWorkerStorePort
    join_timeout: timedelta = timedelta(seconds=10)

    def __post_init__(self) -> None:
        if not timedelta(milliseconds=10) <= self.join_timeout <= timedelta(minutes=1):
            raise ValueError("publication heartbeat join timeout is outside its bound")

    def run(
        self,
        *,
        claim: RegistryPublicationJob,
        worker_id: str,
        lease_capability: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], OperationResultT],
    ) -> tuple[OperationResultT, RegistryPublicationJob]:
        lease = claim.lease
        if lease is None:
            raise ValueError("publication heartbeat requires an active lease")
        if (
            heartbeat_interval < timedelta(milliseconds=10)
            or heartbeat_interval * 2 >= lease_duration
        ):
            raise ValueError("publication heartbeat interval is unsafe")

        stopped = Event()
        lock = Lock()
        latest = [claim]
        failures: list[BaseException] = []

        def heartbeat_loop() -> None:
            while not stopped.wait(heartbeat_interval.total_seconds()):
                try:
                    refreshed = self.job_store.heartbeat(
                        claim.id,
                        worker_id=worker_id,
                        lease_capability=lease_capability,
                        fencing_token=lease.fencing_token,
                        lease_duration=lease_duration,
                    )
                except BaseException as error:
                    with lock:
                        failures.append(error)
                    return
                with lock:
                    latest[0] = refreshed

        thread = Thread(
            target=heartbeat_loop,
            name="schemabridge-registry-publication-heartbeat",
            daemon=True,
        )
        thread.start()
        operation_error: BaseException | None = None
        operation_value: OperationResultT | None = None
        try:
            operation_value = operation()
        except BaseException as error:
            operation_error = error
        finally:
            stopped.set()
            thread.join(self.join_timeout.total_seconds())

        if thread.is_alive():
            raise RegistryPublicationHeartbeatSupervisorError(
                "The registry publication heartbeat did not stop safely."
            )
        with lock:
            prior_failure = failures[0] if failures else None
        if prior_failure is not None:
            raise RegistryPublicationHeartbeatSupervisorError(
                "The registry publication heartbeat was lost."
            ) from None
        try:
            final_claim = self.job_store.heartbeat(
                claim.id,
                worker_id=worker_id,
                lease_capability=lease_capability,
                fencing_token=lease.fencing_token,
                lease_duration=lease_duration,
            )
        except BaseException:
            raise RegistryPublicationHeartbeatSupervisorError(
                "The registry publication heartbeat was lost."
            ) from None
        if operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)
        return cast(OperationResultT, operation_value), final_claim


__all__ = ["ThreadedRegistryPublicationHeartbeatSupervisor"]
