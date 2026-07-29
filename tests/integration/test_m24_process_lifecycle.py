"""Real-process proof for M24 startup, restart, fencing, and graceful shutdown."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.integration.connector_target_support import (
    ensure_catalog_connector_target,
    ensure_compatible_catalog_generation,
)

from schemabridge.adapters.catalog.postgres_inventory import (
    PostgresCatalogConnectionStore,
)
from schemabridge.adapters.catalog.postgres_refresh import (
    PostgresCatalogRefreshStore,
)
from schemabridge.adapters.connectors.postgres_routing import (
    PostgresExecutionTargetResolver,
)
from schemabridge.adapters.control_plane.postgres_jobs import (
    PostgresBackgroundJobStore,
)
from schemabridge.adapters.control_plane.postgres_migrations import (
    PostgresControlPlaneMigrator,
)
from schemabridge.domain.background_jobs import (
    BackgroundJob,
    JobAuthorization,
    JobExecutionTargetRef,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRegistration,
    CatalogRefreshCommand,
    CatalogRefreshId,
    CatalogRefreshMode,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations/control_plane"
ADMIN_DSN = "postgresql://postgres:local-only-not-a-secret@127.0.0.1:55434/postgres"
API_TOKEN_SENTINEL = "m24-process-token-7Yv!4nQ2zR8cK5pL0sD6wX9a"
INVENTORY_CURSOR_KEY_SENTINEL = "m25-process-inventory-cursor-key-6Yq2vN8wR4kD9sH3cJ7pT5xB"
SOURCE_PASSWORD_SENTINEL = "m24-source-password-must-not-appear"
DATAHUB_TOKEN_SENTINEL = "m25-datahub-token-must-not-appear"
API_MODULE = "schemabridge.entrypoints.http.main"
WORKER_MODULE = "schemabridge.entrypoints.worker.main"
CATALOG_MODULE = "schemabridge.entrypoints.catalog.main"
_LEASE_PROCESS_PROGRAM = """
import json
import os
from datetime import timedelta
from threading import Event

from schemabridge.adapters.control_plane.postgres_jobs import PostgresBackgroundJobStore
from schemabridge.application.ports.background_jobs import JobStoreError, JobStoreErrorCode


def emit(**payload):
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


try:
    action = os.environ["SCHEMABRIDGE_TEST_LEASE_ACTION"]
    expected_job_id = os.environ["SCHEMABRIDGE_TEST_JOB_ID"]
    worker_id = os.environ["SCHEMABRIDGE_TEST_WORKER_ID"]
    lease_token = os.environ["SCHEMABRIDGE_TEST_LEASE_TOKEN"]
    fencing_token = int(os.environ["SCHEMABRIDGE_TEST_FENCING_TOKEN"])
    lease_duration = timedelta(
        seconds=float(os.environ["SCHEMABRIDGE_TEST_LEASE_SECONDS"])
    )
    store = PostgresBackgroundJobStore(
        os.environ["SCHEMABRIDGE_TEST_WORKER_DSN"],
        application_name="schemabridge-control-worker",
    )
    if action in {"claim-and-wait", "claim-once"}:
        claimed = store.claim_next(
            worker_id=worker_id,
            lease_token=lease_token,
            lease_duration=lease_duration,
        )
        if claimed is None:
            emit(outcome="idle")
        else:
            if claimed.id != expected_job_id or claimed.lease is None:
                raise RuntimeError("unexpected claimed job")
            emit(
                attempt_count=claimed.attempt_count,
                fencing_token=claimed.lease.fencing_token,
                outcome="claimed",
            )
            if action == "claim-and-wait":
                Event().wait()
    elif action == "heartbeat":
        try:
            current = store.heartbeat(
                expected_job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
                lease_duration=lease_duration,
            )
        except JobStoreError as error:
            if error.code is not JobStoreErrorCode.LEASE_CONFLICT:
                raise
            emit(outcome="lease_conflict")
        else:
            if current.lease is None:
                raise RuntimeError("heartbeat returned no lease")
            emit(fencing_token=current.lease.fencing_token, outcome="heartbeat")
    else:
        raise RuntimeError("unsupported lease process action")
except Exception as error:
    emit(error_type=type(error).__name__, outcome="error")
    raise SystemExit(70) from None
"""


@dataclass(frozen=True)
class _DatabaseUrls:
    database: str
    migrator: str
    api: str
    worker: str
    catalog: str


@dataclass(frozen=True)
class _LifecycleDatabases:
    current: _DatabaseUrls
    behind: _DatabaseUrls


@pytest.fixture(scope="module")
def lifecycle_databases() -> Iterator[_LifecycleDatabases]:
    """Create isolated current and intentionally-behind control databases."""

    admin_dsn = os.environ.get("SCHEMABRIDGE_TEST_CONTROL_ADMIN_DATABASE_URL", ADMIN_DSN)
    current = _database_urls(f"schemabridge_lifecycle_current_{uuid4().hex[:12]}")
    behind = _database_urls(f"schemabridge_lifecycle_behind_{uuid4().hex[:12]}")
    created: list[str] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            for urls in (current, behind):
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER schemabridge_migrator").format(
                        sql.Identifier(urls.database)
                    )
                )
                created.append(urls.database)
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(urls.database)
                    )
                )
                connection.execute(
                    sql.SQL(
                        """
                        GRANT CONNECT ON DATABASE {} TO
                            schemabridge_migrator,
                            schemabridge_api,
                            schemabridge_worker,
                            schemabridge_catalog
                        """
                    ).format(sql.Identifier(urls.database))
                )

        current_result = PostgresControlPlaneMigrator(
            current.migrator,
            MIGRATIONS,
        ).migrate()
        assert current_result.inspection.current_version == 11

        with tempfile.TemporaryDirectory(prefix="schemabridge-m24-behind-") as directory:
            behind_migrations = Path(directory)
            shutil.copyfile(
                MIGRATIONS / "0001_initial_control_plane.sql",
                behind_migrations / "0001_initial_control_plane.sql",
            )
            behind_result = PostgresControlPlaneMigrator(
                behind.migrator,
                behind_migrations,
            ).migrate()
        assert behind_result.inspection.current_version == 1

        # Version 2 normally grants these two read-only preflight capabilities. Granting
        # only them lets both service identities observe that version 1 is behind without
        # granting any version-2 queue capability or applying any migration.
        with psycopg.connect(behind.migrator) as connection:
            connection.execute(
                """
                GRANT USAGE ON SCHEMA schemabridge_control
                    TO schemabridge_api, schemabridge_worker, schemabridge_catalog
                """
            )
            connection.execute(
                """
                GRANT SELECT ON schemabridge_control.schema_migrations
                    TO schemabridge_api, schemabridge_worker, schemabridge_catalog
                """
            )

        yield _LifecycleDatabases(current=current, behind=behind)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            for database in reversed(created):
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )


def test_api_worker_and_catalog_fail_closed_on_behind_schema_without_auto_migration(
    lifecycle_databases: _LifecycleDatabases,
) -> None:
    """All three real service entrypoints reject v1 without auto-migration."""

    urls = lifecycle_databases.behind
    api_result = _run_process(
        API_MODULE,
        _api_environment(urls, port=_free_loopback_port()),
        timeout=15,
    )
    worker_result = _run_process(
        WORKER_MODULE,
        _worker_environment(urls),
        timeout=15,
    )
    catalog_result = _run_process(
        CATALOG_MODULE,
        _catalog_environment(urls),
        arguments=("--probe-ready",),
        timeout=15,
    )

    assert api_result.returncode == 1
    assert worker_result.returncode == 1
    assert catalog_result.returncode == 1
    for service, output in (
        ("api", api_result.stdout),
        ("worker", worker_result.stdout),
        ("catalog", catalog_result.stdout),
    ):
        assert _runtime_event_contracts(output) == (
            (service, "service.health", "started", None),
            (service, "service.health", "failed", "internal_failure"),
        )
    assert "Traceback" not in api_result.stdout
    assert "Traceback" not in worker_result.stdout
    assert "Traceback" not in catalog_result.stdout
    _assert_sanitized(api_result.stdout, urls)
    _assert_sanitized(worker_result.stdout, urls)
    _assert_sanitized(catalog_result.stdout, urls)

    with psycopg.connect(urls.migrator) as connection:
        history = connection.execute(
            """
            SELECT version, name
            FROM schemabridge_control.schema_migrations
            ORDER BY version
            """
        ).fetchall()
        jobs_relation = connection.execute(
            "SELECT to_regclass('schemabridge_control.execution_jobs')"
        ).fetchone()
    assert history == [(1, "initial_control_plane")]
    assert jobs_relation == (None,)


def test_worker_os_process_crash_reclaims_lease_and_fences_stale_owner(
    lifecycle_databases: _LifecycleDatabases,
) -> None:
    """PostgreSQL survives a hard process crash and fences stale lease credentials."""

    urls = lifecycle_databases.current
    job = _seed_job(urls)
    PostgresBackgroundJobStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).submit(job)

    first_token = "m24-first-crashed-worker-capability-" + ("a" * 40)
    first_process = _start_lease_process(
        urls,
        action="claim-and-wait",
        job_id=job.id,
        worker_id="worker-before-crash",
        lease_token=first_token,
        fencing_token=0,
        lease_seconds=4,
    )
    first_prefix = ""
    try:
        first_prefix = _wait_for_output(
            first_process,
            marker='"outcome":"claimed"',
            timeout=15,
        )
        first_process.kill()
        first_output = first_prefix + first_process.communicate(timeout=5)[0]
    finally:
        first_output = _ensure_stopped(
            first_process,
            locals().get("first_output", first_prefix),
        )
    assert first_process.returncode == -signal.SIGKILL
    first_payload = _process_payload(first_output)
    assert first_payload["outcome"] == "claimed"
    assert first_payload["attempt_count"] == 1
    first_fence = first_payload["fencing_token"]
    assert isinstance(first_fence, int)
    _assert_lease_process_sanitized(first_output, urls, first_token)
    assert _persisted_lease_coordinates(urls, job.id) == (
        "leased",
        "worker-before-crash",
        first_fence,
    )

    before_expiry_token = "m24-too-early-capability-" + ("b" * 40)  # gitleaks:allow -- fixture
    before_expiry = _run_lease_process(
        urls,
        action="claim-once",
        job_id=job.id,
        worker_id="worker-restarted-too-early",
        lease_token=before_expiry_token,
        fencing_token=0,
        lease_seconds=2,
        timeout=15,
    )
    assert before_expiry.returncode == 0, _safe_process_summary(before_expiry.stdout)
    assert _process_payload(before_expiry.stdout) == {"outcome": "idle"}
    _assert_lease_process_sanitized(before_expiry.stdout, urls, before_expiry_token)

    remaining = _lease_seconds_remaining(urls, job.id)
    if remaining > 0:
        time.sleep(remaining + 0.2)
    restarted_token = "m24-restarted-worker-capability-" + ("c" * 40)
    # Keep the recovered owner's lease comfortably beyond the two instrumented
    # subprocess startups below. This assertion is about fencing the stale
    # capability; the first 4-second lease already proves expiry and reclaim.
    restarted_lease_seconds = 60
    restarted = _run_lease_process(
        urls,
        action="claim-once",
        job_id=job.id,
        worker_id="worker-after-restart",
        lease_token=restarted_token,
        fencing_token=0,
        lease_seconds=restarted_lease_seconds,
        timeout=15,
    )
    assert restarted.returncode == 0, _safe_process_summary(restarted.stdout)
    restarted_payload = _process_payload(restarted.stdout)
    assert restarted_payload["outcome"] == "claimed"
    assert restarted_payload["attempt_count"] == 2
    restarted_fence = restarted_payload["fencing_token"]
    assert restarted_fence == first_fence + 1
    _assert_lease_process_sanitized(restarted.stdout, urls, restarted_token)

    stale = _run_lease_process(
        urls,
        action="heartbeat",
        job_id=job.id,
        worker_id="worker-before-crash",
        lease_token=first_token,
        fencing_token=first_fence,
        lease_seconds=2,
        timeout=15,
    )
    assert stale.returncode == 0, _safe_process_summary(stale.stdout)
    assert _process_payload(stale.stdout) == {"outcome": "lease_conflict"}
    _assert_lease_process_sanitized(stale.stdout, urls, first_token)

    current = _run_lease_process(
        urls,
        action="heartbeat",
        job_id=job.id,
        worker_id="worker-after-restart",
        lease_token=restarted_token,
        fencing_token=restarted_fence,
        lease_seconds=restarted_lease_seconds,
        timeout=15,
    )
    assert current.returncode == 0, _safe_process_summary(current.stdout)
    assert _process_payload(current.stdout) == {
        "fencing_token": restarted_fence,
        "outcome": "heartbeat",
    }
    _assert_lease_process_sanitized(current.stdout, urls, restarted_token)


def test_api_worker_and_catalog_start_independently_and_exit_cleanly_on_sigterm(
    lifecycle_databases: _LifecycleDatabases,
) -> None:
    """Real API, worker, and catalog subprocesses start alone and honor SIGTERM."""

    urls = lifecycle_databases.current
    api_port = _free_loopback_port()
    api_process = _start_process(API_MODULE, _api_environment(urls, port=api_port))
    try:
        status, payload, headers = _wait_for_http(
            api_process,
            port=api_port,
            path="/health/ready",
            timeout=15,
        )
        assert status == 200
        assert payload == b'{"status":"ready"}'
        assert "server" not in headers
        api_process.send_signal(signal.SIGTERM)
        api_output = api_process.communicate(timeout=15)[0]
    finally:
        api_output = _ensure_stopped(api_process, locals().get("api_output", ""))
    # Uvicorn completes shutdown and then re-raises the captured signal after restoring
    # the prior handler, so the process status truthfully remains SIGTERM.
    assert api_process.returncode == -signal.SIGTERM
    api_events = _runtime_events(api_output)
    assert _runtime_event_contract(api_events[0]) == (
        "api",
        "service.health",
        "started",
        None,
    )
    assert _runtime_event_contract(api_events[-1]) == (
        "api",
        "runtime.log",
        "succeeded",
        None,
    )
    assert sum(event["event"] == "runtime.log" for event in api_events) >= 6
    assert all(
        event["service"] == "api"
        and event["event"] in {"service.health", "runtime.log"}
        and event["outcome"] in {"started", "succeeded"}
        for event in api_events
    )
    assert "Traceback" not in api_output
    _assert_sanitized(api_output, urls)

    worker_process = _start_process(WORKER_MODULE, _worker_environment(urls))
    worker_prefix = ""
    try:
        worker_prefix = _wait_for_output(
            worker_process,
            marker='"event":"service.health","outcome":"succeeded"',
            timeout=15,
        )
        # The readiness event precedes installation of the process-owned signal
        # handlers by only the exporter open. One bounded poll interval proves the
        # long-running loop, rather than racing SIGTERM against that setup boundary.
        time.sleep(0.1)
        worker_process.send_signal(signal.SIGTERM)
        worker_output = worker_prefix + worker_process.communicate(timeout=15)[0]
    finally:
        worker_output = _ensure_stopped(
            worker_process,
            locals().get("worker_output", worker_prefix),
        )
    assert worker_process.returncode == 0
    assert _runtime_event_contracts(worker_output) == (
        ("worker", "service.health", "started", None),
        ("worker", "service.health", "succeeded", None),
    )
    assert "Traceback" not in worker_output
    _assert_sanitized(worker_output, urls)

    catalog_process = _start_process(CATALOG_MODULE, _catalog_environment(urls))
    catalog_prefix = ""
    try:
        catalog_prefix = _wait_for_output(
            catalog_process,
            marker='"event":"catalog.refresh","outcome":"succeeded"',
            timeout=15,
        )
        catalog_process.send_signal(signal.SIGTERM)
        catalog_output = catalog_prefix + catalog_process.communicate(timeout=15)[0]
    finally:
        catalog_output = _ensure_stopped(
            catalog_process,
            locals().get("catalog_output", catalog_prefix),
        )
    assert catalog_process.returncode == 0
    catalog_events = _runtime_events(catalog_output)
    assert tuple(map(_runtime_event_contract, catalog_events[:2])) == (
        ("catalog", "service.health", "started", None),
        ("catalog", "service.health", "succeeded", None),
    )
    assert all(
        _runtime_event_contract(event) == ("catalog", "catalog.refresh", "succeeded", None)
        and event["jobs_completed"] == 0
        and event["jobs_failed"] == 0
        and event["records_processed"] == 0
        for event in catalog_events[2:]
    )
    assert len(catalog_events) >= 3
    assert "Traceback" not in catalog_output
    _assert_sanitized(catalog_output, urls)


def test_catalog_sigterm_commits_at_most_the_active_page_and_keeps_refresh_resumable(
    lifecycle_databases: _LifecycleDatabases,
) -> None:
    """A real non-idle indexer exits at a committed multipage checkpoint."""

    urls = lifecycle_databases.current
    connection_id, refresh_id = _seed_catalog_refresh(urls, asset_count=5_434)
    catalog_process = _start_process(
        CATALOG_MODULE,
        _catalog_environment(
            urls,
            synthetic_assets={connection_id.root: 5_434},
            page_size=1,
        ),
    )
    catalog_output = ""
    try:
        pages_before_signal = _wait_for_catalog_progress(
            urls,
            refresh_id,
            timeout=15,
        )
        catalog_process.send_signal(signal.SIGTERM)
        catalog_output = catalog_process.communicate(timeout=15)[0]
    finally:
        catalog_output = _ensure_stopped(catalog_process, catalog_output)

    assert catalog_process.returncode == 0
    catalog_events = _runtime_events(catalog_output)
    assert tuple(map(_runtime_event_contract, catalog_events[:2])) == (
        ("catalog", "service.health", "started", None),
        ("catalog", "service.health", "succeeded", None),
    )
    assert _runtime_event_contract(catalog_events[-1]) == (
        "catalog",
        "catalog.refresh",
        "cancelled",
        "operation_cancelled",
    )
    assert catalog_events[-1]["jobs_completed"] == 0
    assert catalog_events[-1]["jobs_failed"] == 0
    assert isinstance(catalog_events[-1]["records_processed"], int)
    assert catalog_events[-1]["records_processed"] > 0
    assert "Traceback" not in catalog_output
    _assert_sanitized(catalog_output, urls)
    status, pages_after_signal, source_complete, active_generation = _catalog_refresh_coordinates(
        urls, refresh_id
    )
    assert status == "staging"
    assert pages_before_signal <= pages_after_signal < 5_434
    assert not source_complete
    assert active_generation is None


def _database_urls(database: str) -> _DatabaseUrls:
    return _DatabaseUrls(
        database=database,
        migrator=_role_dsn("schemabridge_migrator", database),
        api=_role_dsn("schemabridge_api", database),
        worker=_role_dsn("schemabridge_worker", database),
        catalog=_role_dsn("schemabridge_catalog", database),
    )


def _role_dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{role}@127.0.0.1:55434/{database}"


def _base_environment() -> dict[str, str]:
    environment = {
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "SCHEMABRIDGE_LOG_LEVEL": "INFO",
    }
    for name in ("PATH", "SYSTEMROOT", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _api_environment(urls: _DatabaseUrls, *, port: int) -> dict[str, str]:
    return {
        **_base_environment(),
        "SCHEMABRIDGE_COMPONENT": "api",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_API_DATABASE_URL": urls.api,
        "SCHEMABRIDGE_API_LOCAL_BEARER_TOKEN": API_TOKEN_SENTINEL,
        "SCHEMABRIDGE_INVENTORY_CURSOR_SIGNING_KEY": (INVENTORY_CURSOR_KEY_SENTINEL),
        "SCHEMABRIDGE_API_BIND_HOST": "127.0.0.1",
        "SCHEMABRIDGE_API_PORT": str(port),
    }


def _worker_environment(urls: _DatabaseUrls) -> dict[str, str]:
    return {
        **_base_environment(),
        "SCHEMABRIDGE_COMPONENT": "worker",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_WORKER_DATABASE_URL": urls.worker,
        "SCHEMABRIDGE_REGISTRY_MODE": "live",
        "SCHEMABRIDGE_SEMANTIC_REGISTRY_SELECTION": "active",
        "SCHEMABRIDGE_ALLOW_LOCAL_LIVE_READS": "true",
        "SCHEMABRIDGE_WORKER_POLL_INTERVAL_MS": "50",
    }


def _catalog_environment(
    urls: _DatabaseUrls,
    *,
    synthetic_assets: Mapping[str, int] | None = None,
    page_size: int | None = None,
) -> dict[str, str]:
    environment = {
        **_base_environment(),
        "SCHEMABRIDGE_COMPONENT": "catalog",
        "SCHEMABRIDGE_CONTROL_PLANE_MODE": "postgres",
        "SCHEMABRIDGE_CONTROL_CATALOG_DATABASE_URL": urls.catalog,
        "SCHEMABRIDGE_CONNECTOR_SECRET_DIRECTORY": str(ROOT / ".local/test-catalog-connectors"),
        "SCHEMABRIDGE_CATALOG_POLL_INTERVAL_MS": "100",
    }
    if synthetic_assets is not None:
        environment["SCHEMABRIDGE_CATALOG_SYNTHETIC_ASSET_COUNTS"] = json.dumps(
            synthetic_assets,
            sort_keys=True,
            separators=(",", ":"),
        )
    if page_size is not None:
        environment["SCHEMABRIDGE_CATALOG_PAGE_SIZE"] = str(page_size)
    return environment


def _lease_process_environment(
    urls: _DatabaseUrls,
    *,
    action: str,
    job_id: str,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    lease_seconds: float,
) -> dict[str, str]:
    return {
        **_base_environment(),
        "SCHEMABRIDGE_TEST_LEASE_ACTION": action,
        "SCHEMABRIDGE_TEST_WORKER_DSN": urls.worker,
        "SCHEMABRIDGE_TEST_JOB_ID": job_id,
        "SCHEMABRIDGE_TEST_WORKER_ID": worker_id,
        "SCHEMABRIDGE_TEST_LEASE_TOKEN": lease_token,
        "SCHEMABRIDGE_TEST_FENCING_TOKEN": str(fencing_token),
        "SCHEMABRIDGE_TEST_LEASE_SECONDS": str(lease_seconds),
    }


def _run_lease_process(
    urls: _DatabaseUrls,
    *,
    action: str,
    job_id: str,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    lease_seconds: float,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-c", _LEASE_PROCESS_PROGRAM),
        cwd=ROOT,
        env=_lease_process_environment(
            urls,
            action=action,
            job_id=job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=timeout,
    )


def _start_lease_process(
    urls: _DatabaseUrls,
    *,
    action: str,
    job_id: str,
    worker_id: str,
    lease_token: str,
    fencing_token: int,
    lease_seconds: float,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        (sys.executable, "-c", _LEASE_PROCESS_PROGRAM),
        cwd=ROOT,
        env=_lease_process_environment(
            urls,
            action=action,
            job_id=job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_process(
    module: str,
    environment: Mapping[str, str],
    *,
    arguments: tuple[str, ...] = (),
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", module, *arguments),
        cwd=ROOT,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=timeout,
    )


def _start_process(
    module: str,
    environment: Mapping[str, str],
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        (sys.executable, "-m", module),
        cwd=ROOT,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_http(
    process: subprocess.Popen[str],
    *,
    port: int,
    path: str,
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.communicate(timeout=1)[0]
            pytest.fail(f"API exited before readiness: {_safe_process_summary(output)}")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", path, headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            payload = response.read()
            headers = {name.casefold(): value for name, value in response.getheaders()}
            return response.status, payload, headers
        except OSError:
            time.sleep(0.05)
        finally:
            connection.close()
    pytest.fail("API did not become ready before the bounded timeout")


def _wait_for_output(
    process: subprocess.Popen[str],
    *,
    marker: str,
    timeout: float,
) -> str:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    output: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output.append(process.communicate(timeout=1)[0])
            pytest.fail(f"process exited before startup: {_safe_process_summary(''.join(output))}")
        ready, _, _ = select.select((process.stdout,), (), (), 0.1)
        if not ready:
            continue
        line = process.stdout.readline()
        output.append(line)
        if marker in line:
            return "".join(output)
    pytest.fail("process did not report the expected event before the bounded timeout")


def _ensure_stopped(process: subprocess.Popen[str], output: str) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            output += process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output += process.communicate(timeout=5)[0]
    elif process.stdout is not None and not process.stdout.closed:
        output += process.communicate(timeout=1)[0]
    return output


def _safe_process_summary(output: str) -> str:
    lines = [line for line in output.splitlines() if "error_type" in line]
    return lines[-1] if lines else "no sanitized process status"


def _runtime_events(output: str) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line in output.splitlines():
        if not line:
            continue
        raw: object = json.loads(line)
        assert isinstance(raw, dict)
        assert all(isinstance(key, str) for key in raw)
        event = {str(key): value for key, value in raw.items()}
        assert event["schema_version"] == "schemabridge.telemetry.v1"
        assert isinstance(event["timestamp"], str)
        assert isinstance(event["duration_ms"], int)
        events.append(event)
    assert events
    return tuple(events)


def _runtime_event_contract(event: Mapping[str, object]) -> tuple[object, object, object, object]:
    return (
        event["service"],
        event["event"],
        event["outcome"],
        event.get("error_code"),
    )


def _runtime_event_contracts(
    output: str,
) -> tuple[tuple[object, object, object, object], ...]:
    return tuple(_runtime_event_contract(event) for event in _runtime_events(output))


def _assert_sanitized(output: str, urls: _DatabaseUrls) -> None:
    for forbidden in (
        API_TOKEN_SENTINEL,
        INVENTORY_CURSOR_KEY_SENTINEL,
        SOURCE_PASSWORD_SENTINEL,
        DATAHUB_TOKEN_SENTINEL,
        urls.api,
        urls.worker,
        urls.catalog,
        "schemabridge_api:schemabridge_api",
        "schemabridge_worker:schemabridge_worker",
        "schemabridge_catalog:schemabridge_catalog",
        "OPENAI_API_KEY",
        "DATAHUB_GMS_TOKEN",
    ):
        assert forbidden not in output


def _assert_lease_process_sanitized(
    output: str,
    urls: _DatabaseUrls,
    *lease_tokens: str,
) -> None:
    _assert_sanitized(output, urls)
    assert "Traceback" not in output
    for lease_token in lease_tokens:
        assert lease_token not in output


def _process_payload(output: str) -> dict[str, object]:
    lines = [line for line in output.splitlines() if line]
    assert lines
    raw: object = json.loads(lines[-1])
    assert isinstance(raw, dict)
    assert all(isinstance(key, str) for key in raw)
    return {str(key): value for key, value in raw.items()}


def _lease_seconds_remaining(urls: _DatabaseUrls, job_id: str) -> float:
    with psycopg.connect(urls.migrator) as connection:
        row = connection.execute(
            """
            SELECT EXTRACT(EPOCH FROM (lease_expires_at - clock_timestamp()))::double precision
            FROM schemabridge_control.execution_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    assert row is not None
    assert isinstance(row[0], float)
    return max(0.0, row[0])


def _persisted_lease_coordinates(
    urls: _DatabaseUrls,
    job_id: str,
) -> tuple[str, str, int]:
    with psycopg.connect(urls.migrator) as connection:
        row = connection.execute(
            """
            SELECT status, lease_owner_id, fencing_token
            FROM schemabridge_control.execution_jobs
            WHERE job_id = %s
            """,
            (job_id,),
        ).fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    assert isinstance(row[1], str)
    assert isinstance(row[2], int)
    return row[0], row[1], row[2]


def _seed_job(urls: _DatabaseUrls) -> BackgroundJob:
    suffix = uuid4().hex
    workspace_id = f"workspace-{suffix}"
    workflow_id = f"workflow-{suffix}"
    owner_actor_id = f"owner-{suffix}"
    submitting_actor_id = f"submitter-{suffix}"
    now = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit, version,
                updated_by, created_at, updated_at
            ) VALUES (%s, 100, 1000000, 10000000, 10000, 1000, 1,
                      'test_platform_admin', %s, %s)
            """,
            (workspace_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.agent_workflow_drafts (
                workspace_id,
                id,
                revision,
                payload,
                execution_row_count,
                execution_preview_fingerprint,
                updated_at
            ) VALUES (%s, %s, 1, %s, NULL, NULL, %s)
            """,
            (workspace_id, workflow_id, Jsonb({"synthetic": True}), now),
        )
        connection.execute(
            """
            INSERT INTO schemabridge_control.workflow_access_grants (
                workspace_id,
                workflow_id,
                owner_actor_id,
                created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (workspace_id, workflow_id, owner_actor_id, now),
        )
    connection_id = CatalogConnectionId(f"connection_job_{suffix}")
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(
        CatalogConnectionRegistration(
            workspace_id=workspace_id,
            connection_id=connection_id,
            display_name="Lifecycle job source",
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="TEST",
            catalog_scope="lifecycle-job",
            requested_by="test_platform_admin",
            requested_at=now,
            idempotency_digest=hashlib.sha256(f"register-job:{suffix}".encode()).hexdigest(),
        )
    )
    target_facts = ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    ensure_compatible_catalog_generation(
        urls.migrator,
        urls.api,
        urls.catalog,
        workspace_id=workspace_id,
        connection_id=connection_id,
        target=target_facts,
    )
    target = PostgresExecutionTargetResolver(
        urls.catalog,
        application_name="schemabridge-control-catalog",
    ).resolve_current(
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    assert target.fingerprint == target_facts.target_fingerprint
    authorization = JobAuthorization.create(
        workspace_id=workspace_id,
        workflow_id=workflow_id,
        workflow_owner_actor_id=owner_actor_id,
        submitting_actor_id=submitting_actor_id,
        expected_workflow_revision=1,
        expected_plan_fingerprint=hashlib.sha256(f"plan-{suffix}".encode()).hexdigest(),
        execution_target=JobExecutionTargetRef.from_target(target),
        authenticated_at=now - timedelta(minutes=1),
        authorized_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
    )
    return BackgroundJob.create(
        id=f"job-{suffix}",
        authorization=authorization,
        idempotency_digest=hashlib.sha256(f"idempotency-{suffix}".encode()).hexdigest(),
        max_attempts=3,
        created_at=now,
    )


def _seed_catalog_refresh(
    urls: _DatabaseUrls,
    *,
    asset_count: int,
) -> tuple[CatalogConnectionId, CatalogRefreshId]:
    suffix = uuid4().hex
    workspace_id = f"workspace_catalog_{suffix}"
    connection_id = CatalogConnectionId(f"connection_{suffix}")
    now = datetime.now(UTC)
    with psycopg.connect(urls.migrator) as connection:
        connection.execute(
            """
            INSERT INTO schemabridge_control.tenant_capacity_policies (
                workspace_id, connection_limit, asset_limit, field_limit,
                api_requests_per_minute, nonterminal_job_limit,
                version, updated_by, created_at, updated_at
            ) VALUES (
                %s, 2, %s, %s, 10000, 100,
                1, 'actor_platform_admin', %s, %s
            )
            """,
            (workspace_id, asset_count + 10, asset_count * 12 + 10, now, now),
        )
    registration = CatalogConnectionRegistration(
        workspace_id=workspace_id,
        connection_id=connection_id,
        display_name="Synthetic lifecycle inventory",
        kind=CatalogConnectionKind.SYNTHETIC,
        environment="PROD",
        catalog_scope="synthetic-lifecycle",
        requested_by="actor_platform_admin",
        requested_at=now,
        idempotency_digest=hashlib.sha256(f"register:{suffix}".encode()).hexdigest(),
    )
    PostgresCatalogConnectionStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).register(registration)
    ensure_catalog_connector_target(
        urls.migrator,
        workspace_id=workspace_id,
        connection_id=connection_id,
    )
    requested = PostgresCatalogRefreshStore(
        urls.api,
        application_name="schemabridge-control-api",
    ).request(
        CatalogRefreshCommand(
            workspace_id=workspace_id,
            connection_id=connection_id,
            mode=CatalogRefreshMode.FULL,
            requested_by="actor_platform_admin",
            requested_at=now,
            idempotency_digest=hashlib.sha256(f"refresh:{suffix}".encode()).hexdigest(),
        )
    )
    return connection_id, requested.refresh.refresh_id


def _wait_for_catalog_progress(
    urls: _DatabaseUrls,
    refresh_id: CatalogRefreshId,
    *,
    timeout: float,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, page_count, source_complete, _active_generation = _catalog_refresh_coordinates(
            urls, refresh_id
        )
        if status == "staging" and page_count >= 1 and not source_complete:
            return page_count
        if status in {"completed", "failed"}:
            pytest.fail(f"catalog refresh became terminal before SIGTERM: {status}")
        time.sleep(0.02)
    pytest.fail("catalog indexer did not commit a source page before the bounded timeout")


def _catalog_refresh_coordinates(
    urls: _DatabaseUrls,
    refresh_id: CatalogRefreshId,
) -> tuple[str, int, bool, int | None]:
    with psycopg.connect(urls.migrator) as connection:
        row = connection.execute(
            """
            SELECT refresh.status,
                   refresh.source_page_number,
                   refresh.source_complete,
                   catalog.active_generation
            FROM schemabridge_control.catalog_refresh_runs AS refresh
            JOIN schemabridge_control.catalog_connections AS catalog
              ON catalog.workspace_id = refresh.workspace_id
             AND catalog.connection_id = refresh.connection_id
            WHERE refresh.refresh_id = %s
            """,
            (refresh_id.root,),
        ).fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    assert isinstance(row[1], int)
    assert isinstance(row[2], bool)
    assert row[3] is None or isinstance(row[3], int)
    return row[0], row[1], row[2], row[3]
