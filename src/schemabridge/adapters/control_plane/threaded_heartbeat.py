"""Bounded periodic heartbeat supervision for synchronous worker operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Lock, Thread
from typing import TypeVar, cast

from schemabridge.application.ports.background_jobs import (
    BackgroundJobStorePort,
    LeaseHeartbeatRunResult,
    LeaseHeartbeatSupervisorError,
)
from schemabridge.domain.background_jobs import BackgroundJob

OperationResultT = TypeVar("OperationResultT")


@dataclass(frozen=True, slots=True)
class ThreadedLeaseHeartbeatSupervisor:
    """Renew leases on a daemon thread while work remains on the caller thread."""

    job_store: BackgroundJobStorePort
    join_timeout: timedelta = timedelta(seconds=10)

    def __post_init__(self) -> None:
        if not timedelta(milliseconds=10) <= self.join_timeout <= timedelta(minutes=1):
            raise ValueError("heartbeat join timeout is outside the supported bound")

    def run(
        self,
        *,
        claim: BackgroundJob,
        worker_id: str,
        lease_token: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        operation: Callable[[], OperationResultT],
    ) -> LeaseHeartbeatRunResult[OperationResultT]:
        """Run one operation with periodic and final fenced lease renewal."""

        lease = claim.lease
        if lease is None:
            raise ValueError("heartbeat supervision requires an active lease")
        if not timedelta(milliseconds=10) <= heartbeat_interval:
            raise ValueError("heartbeat interval is outside the supported bound")
        if heartbeat_interval * 2 >= lease_duration:
            raise ValueError("heartbeat interval must be less than half the lease duration")

        stopped = Event()
        lock = Lock()
        latest: list[BackgroundJob] = [claim]
        heartbeat_failure: list[Exception] = []

        def heartbeat_loop() -> None:
            while not stopped.wait(heartbeat_interval.total_seconds()):
                try:
                    refreshed = self.job_store.heartbeat(
                        claim.id,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        fencing_token=lease.fencing_token,
                        lease_duration=lease_duration,
                    )
                except Exception as error:
                    with lock:
                        heartbeat_failure.append(error)
                    return
                with lock:
                    latest[0] = refreshed

        thread = Thread(
            target=heartbeat_loop,
            name="schemabridge-lease-heartbeat",
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
            raise LeaseHeartbeatSupervisorError("The worker lease heartbeat did not stop safely.")
        with lock:
            prior_failure = heartbeat_failure[0] if heartbeat_failure else None
        if prior_failure is not None:
            raise LeaseHeartbeatSupervisorError("The worker lease heartbeat was lost.") from None

        try:
            final_claim = self.job_store.heartbeat(
                claim.id,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=lease.fencing_token,
                lease_duration=lease_duration,
            )
        except Exception:
            raise LeaseHeartbeatSupervisorError("The worker lease heartbeat was lost.") from None

        if operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)
        return LeaseHeartbeatRunResult(
            value=cast(OperationResultT, operation_value),
            claim=final_claim,
        )
