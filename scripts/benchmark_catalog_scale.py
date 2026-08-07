"""Reproducible local scale evidence for the bounded catalog page contract.

The default lazy reader produces synthetic preflight evidence only. PostgreSQL acceptance must
select an explicit ``--reader-factory package.module:factory``. The factory takes no arguments,
obtains credentials from protected environment configuration, and returns an object matching
``ScaleReader``. Reports never include connection identifiers, credentials, environment values,
hostnames, exception text, SQL, query parameters, or source values.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import math
import os
import platform
import re
import resource
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from schemabridge.adapters.catalog.synthetic_source import (
    LazySyntheticCatalogSource,
    SyntheticCatalogSpecification,
)
from schemabridge.domain.catalog_inventory import (
    CatalogConnectionId,
    CatalogConnectionKind,
    CatalogConnectionRoute,
    CatalogConnectionStatus,
    CatalogRefreshMode,
)

FORMAT_VERSION = 2
SMALL_ASSET_COUNT = 10
LARGE_ASSET_COUNT = 5_434
MIN_LARGE_FIELD_COUNT = 40_000
PAGE_SIZES = (1, 17, 50)
DEFAULT_READ_COUNT = 5_000
DEFAULT_CONCURRENCY = 16
DEFAULT_LOAD_PAGE_SIZE = 17
MAX_HEAP_DELTA_BYTES = 16 * 1024 * 1024
MAX_RSS_DELTA_BYTES = 64 * 1024 * 1024
MAX_P95_MILLISECONDS = 250.0
MAX_P99_MILLISECONDS = 500.0
MAX_FULL_REFRESH_SECONDS = 60.0
_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,99}$")
_FACTORY_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}:[A-Za-z_][A-Za-z0-9_]{0,99}$")
_PLAN_NODE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYNTHETIC_ASSET_KEY = re.compile(r"^synthetic-asset-([0-9]{8})$")
_EXPECTED_PLAN_INDEXES = {
    "assets": "catalog_assets_keyset_idx",
    "fields": "catalog_fields_keyset_idx",
}


class ScaleHarnessError(RuntimeError):
    """A stable scale-contract failure without protected detail."""


@dataclass(frozen=True, slots=True)
class ScaleReaderMetadata:
    """Sanitized reader facts allowed in a public local-evidence report."""

    label: str
    backend_kind: str
    evidence_profile: str
    indexed_database_read: bool
    pool_max_size: int | None
    replica_count: int
    cache_context: str

    def __post_init__(self) -> None:
        for value in (
            self.label,
            self.backend_kind,
            self.evidence_profile,
            self.cache_context,
        ):
            if _SAFE_LABEL.fullmatch(value) is None:
                raise ScaleHarnessError("scale reader metadata is invalid")
        if self.evidence_profile not in {
            "synthetic-preflight",
            "postgres-acceptance",
        }:
            raise ScaleHarnessError("scale reader evidence profile is invalid")
        if self.indexed_database_read != (self.evidence_profile == "postgres-acceptance"):
            raise ScaleHarnessError("scale reader evidence profile is inconsistent")
        if self.pool_max_size is not None and not 1 <= self.pool_max_size <= 10_000:
            raise ScaleHarnessError("scale reader pool metadata is invalid")
        if not 1 <= self.replica_count <= 1_000:
            raise ScaleHarnessError("scale reader replica metadata is invalid")


@dataclass(frozen=True, slots=True)
class ScalePage:
    """One bounded page returned by either the synthetic or future indexed reader."""

    item_keys: tuple[str, ...]
    next_cursor: str | None
    rows_read: int
    pool_wait_milliseconds: float | None = None

    def __post_init__(self) -> None:
        if len(self.item_keys) > 50:
            raise ScaleHarnessError("scale reader materialized more than fifty items")
        if not 0 <= self.rows_read <= 51:
            raise ScaleHarnessError("scale reader exceeded the bounded row contract")
        if self.rows_read < len(self.item_keys):
            raise ScaleHarnessError("scale reader row accounting is invalid")
        if self.next_cursor is not None and (
            not self.next_cursor
            or len(self.next_cursor.encode("utf-8")) > 1_024
            or any(ord(character) < 32 or ord(character) == 127 for character in self.next_cursor)
        ):
            raise ScaleHarnessError("scale reader cursor is invalid")
        if self.pool_wait_milliseconds is not None and (
            not math.isfinite(self.pool_wait_milliseconds) or self.pool_wait_milliseconds < 0
        ):
            raise ScaleHarnessError("scale reader pool-wait observation is invalid")


class ScaleReader(Protocol):
    """Thread-safe bounded page reader used by correctness and load harnesses."""

    @property
    def metadata(self) -> ScaleReaderMetadata:
        """Return only sanitized backend and pool facts."""

    def read_page(
        self,
        case: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> ScalePage:
        """Read one keyset/scroll page for the named synthetic scale case."""

    def explain_index_plan(self) -> Mapping[str, object]:
        """Return sanitized reviewed-plan evidence without SQL or parameters."""

    def close(self) -> None:
        """Release reader resources idempotently."""


class LazySyntheticScaleReader:
    """Adapt the existing lazy source to the generic scale-reader contract."""

    _SMALL = CatalogConnectionId("tenant-small-primary")
    _LARGE = CatalogConnectionId("tenant-large-primary")

    def __init__(self) -> None:
        self._source = LazySyntheticCatalogSource(
            {
                self._SMALL: SyntheticCatalogSpecification(asset_count=SMALL_ASSET_COUNT),
                self._LARGE: SyntheticCatalogSpecification(asset_count=LARGE_ASSET_COUNT),
            }
        )
        self._routes = {
            "small": self._route("workspace-small", self._SMALL),
            "large": self._route("workspace-large", self._LARGE),
        }

    @property
    def metadata(self) -> ScaleReaderMetadata:
        return ScaleReaderMetadata(
            label="lazy-synthetic-v2",
            backend_kind="synthetic-callable",
            evidence_profile="synthetic-preflight",
            indexed_database_read=False,
            pool_max_size=None,
            replica_count=1,
            cache_context="stateless-generated-pages",
        )

    def read_page(
        self,
        case: str,
        *,
        page_size: int,
        cursor: str | None,
    ) -> ScalePage:
        route = self._routes.get(case)
        if route is None:
            raise ScaleHarnessError("unknown scale case")
        page = self._source.read_page(
            route,
            mode=CatalogRefreshMode.FULL,
            checkpoint=cursor,
            page_size=page_size,
        )
        item_keys: list[str] = []
        for change in page.changes:
            if change.asset is None:
                raise ScaleHarnessError("synthetic scale page is malformed")
            item_keys.append(change.asset.asset_id.root)
        return ScalePage(
            item_keys=tuple(item_keys),
            next_cursor=page.next_checkpoint,
            rows_read=len(item_keys),
        )

    def explain_index_plan(self) -> Mapping[str, object]:
        return {
            "status": "not_applicable_synthetic_preflight",
            "passed": True,
            "sql_recorded": False,
            "parameters_recorded": False,
        }

    def close(self) -> None:
        return None

    @staticmethod
    def _route(
        workspace_id: str,
        connection_id: CatalogConnectionId,
    ) -> CatalogConnectionRoute:
        return CatalogConnectionRoute(
            workspace_id=workspace_id,
            connection_id=connection_id,
            kind=CatalogConnectionKind.SYNTHETIC,
            environment="PROD",
            catalog_scope="synthetic-scale",
            status=CatalogConnectionStatus.ENABLED,
        )


def create_synthetic_reader() -> ScaleReader:
    """Public no-argument factory used by the default CLI and tests."""

    return LazySyntheticScaleReader()


def load_reader(reference: str | None) -> ScaleReader:
    """Load one explicit no-argument reader factory without reporting its internals."""

    if reference is None:
        return create_synthetic_reader()
    if _FACTORY_REFERENCE.fullmatch(reference) is None:
        raise ScaleHarnessError("scale reader factory reference is invalid")
    module_name, attribute_name = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name)
        reader = cast(Callable[[], object], factory)()
        candidate = cast(ScaleReader, reader)
        metadata = candidate.metadata
        ScaleReaderMetadata(
            label=metadata.label,
            backend_kind=metadata.backend_kind,
            evidence_profile=metadata.evidence_profile,
            indexed_database_read=metadata.indexed_database_read,
            pool_max_size=metadata.pool_max_size,
            replica_count=metadata.replica_count,
            cache_context=metadata.cache_context,
        )
        read_page = candidate.read_page
        explain_index_plan = candidate.explain_index_plan
        close = candidate.close
    except Exception as error:
        raise ScaleHarnessError("scale reader factory is unavailable") from error
    if not callable(read_page) or not callable(explain_index_plan) or not callable(close):
        raise ScaleHarnessError("scale reader factory contract is invalid")
    return candidate


def run_correctness(
    reader: ScaleReader,
    *,
    case_counts: Mapping[str, int] | None = None,
    page_sizes: Sequence[int] = PAGE_SIZES,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Traverse each case exactly once per page size without retaining its inventory."""

    counts = dict(case_counts or {"small": SMALL_ASSET_COUNT, "large": LARGE_ASSET_COUNT})
    if counts != {"small": SMALL_ASSET_COUNT, "large": LARGE_ASSET_COUNT}:
        raise ScaleHarnessError(
            "scale correctness cases must remain ten and five-thousand-four-hundred-thirty-four"
        )
    normalized_sizes = tuple(page_sizes)
    if normalized_sizes != PAGE_SIZES:
        raise ScaleHarnessError(
            "scale correctness page sizes must remain one, seventeen, and fifty"
        )

    memory = measure_memory_comparison(reader)
    results: list[dict[str, object]] = []
    traversal_digests: dict[str, str] = {}
    for case in ("small", "large"):
        for page_size in normalized_sizes:
            result = traverse_inventory(
                reader,
                case=case,
                expected_count=counts[case],
                page_size=page_size,
            )
            digest = cast(str, result["inventory_digest"])
            previous = traversal_digests.setdefault(case, digest)
            if digest != previous:
                raise ScaleHarnessError("scale traversal order changed across page sizes")
            results.append(result)
    return results, memory


def traverse_inventory(
    reader: ScaleReader,
    *,
    case: str,
    expected_count: int,
    page_size: int,
) -> dict[str, object]:
    """Prove ordered exact traversal with constant harness memory."""

    if case not in {"small", "large"} or expected_count < 1:
        raise ScaleHarnessError("scale traversal case is invalid")
    if not 1 <= page_size <= 50:
        raise ScaleHarnessError("scale traversal page size is invalid")
    cursor: str | None = None
    page_count = 0
    item_count = 0
    maximum_rows_read = 0
    maximum_materialized_items = 0
    digest = hashlib.sha256()
    seen = bytearray(math.ceil(expected_count / 8))
    started = time.perf_counter_ns()

    while True:
        page = reader.read_page(case, page_size=page_size, cursor=cursor)
        page_count += 1
        maximum_rows_read = max(maximum_rows_read, page.rows_read)
        maximum_materialized_items = max(
            maximum_materialized_items,
            len(page.item_keys),
        )
        if len(page.item_keys) > page_size or page.rows_read > page_size + 1:
            raise ScaleHarnessError("scale page exceeded its requested bound")
        if not page.item_keys:
            raise ScaleHarnessError("scale traversal returned an empty intermediate page")
        for item_key in page.item_keys:
            matched = _SYNTHETIC_ASSET_KEY.fullmatch(item_key)
            if matched is None:
                raise ScaleHarnessError("scale traversal order or identity changed")
            identity_index = int(matched.group(1))
            if identity_index >= expected_count:
                raise ScaleHarnessError("scale traversal order or identity changed")
            byte_index, bit_index = divmod(identity_index, 8)
            mask = 1 << bit_index
            if seen[byte_index] & mask:
                raise ScaleHarnessError("scale traversal order or identity changed")
            seen[byte_index] |= mask
            digest.update(item_key.encode("utf-8"))
            digest.update(b"\n")
            item_count += 1
            if item_count > expected_count:
                raise ScaleHarnessError("scale traversal returned excess items")
        if page.next_cursor is None:
            break
        if page.next_cursor == cursor:
            raise ScaleHarnessError("scale traversal cursor did not advance")
        cursor = page.next_cursor

    elapsed_milliseconds = (time.perf_counter_ns() - started) / 1_000_000
    expected_pages = math.ceil(expected_count / page_size)
    if item_count != expected_count or page_count != expected_pages:
        raise ScaleHarnessError("scale traversal omitted inventory")
    return {
        "case": case,
        "asset_count": item_count,
        "page_size": page_size,
        "page_count": page_count,
        "maximum_rows_read": maximum_rows_read,
        "maximum_materialized_items": maximum_materialized_items,
        "inventory_digest": digest.hexdigest(),
        "elapsed_milliseconds": round(elapsed_milliseconds, 3),
        "passed": True,
    }


def measure_memory_comparison(reader: ScaleReader) -> dict[str, object]:
    """Compare one maximum-size page for 10 and 5,434 without a full collection."""

    rss_before = peak_rss_bytes()
    small = _measure_page_heap(reader, "small")
    rss_after_small = peak_rss_bytes()
    large = _measure_page_heap(reader, "large")
    rss_after_large = peak_rss_bytes()
    heap_delta = max(0, large["heap_peak_bytes"] - small["heap_peak_bytes"])
    rss_delta = max(0, rss_after_large - rss_after_small)
    passed = heap_delta <= MAX_HEAP_DELTA_BYTES and rss_delta <= MAX_RSS_DELTA_BYTES
    return {
        "method": "single-bounded-page-comparison",
        "page_size": 50,
        "small": small,
        "large": large,
        "heap_delta_large_minus_small_bytes": heap_delta,
        "rss_peak_before_bytes": rss_before,
        "rss_peak_after_small_bytes": rss_after_small,
        "rss_peak_after_large_bytes": rss_after_large,
        "rss_delta_large_after_small_bytes": rss_delta,
        "heap_regression_budget_bytes": MAX_HEAP_DELTA_BYTES,
        "rss_regression_budget_bytes": MAX_RSS_DELTA_BYTES,
        "passed": passed,
        "production_slo": False,
    }


def _measure_page_heap(reader: ScaleReader, case: str) -> dict[str, int]:
    gc.collect()
    tracemalloc.start()
    try:
        heap_before = tracemalloc.get_traced_memory()[0]
        page = reader.read_page(case, page_size=50, cursor=None)
        _current, heap_peak = tracemalloc.get_traced_memory()
        materialized_items = len(page.item_keys)
        rows_read = page.rows_read
    finally:
        tracemalloc.stop()
    return {
        "heap_peak_bytes": max(0, heap_peak - heap_before),
        "materialized_items": materialized_items,
        "rows_read": rows_read,
    }


def run_load(
    reader: ScaleReader,
    *,
    read_count: int = DEFAULT_READ_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
    page_size: int = DEFAULT_LOAD_PAGE_SIZE,
) -> dict[str, object]:
    """Run independent bounded cursor chains at the declared local concurrency."""

    if not 1 <= read_count <= 100_000:
        raise ScaleHarnessError("scale read count is invalid")
    if not 1 <= concurrency <= 128 or concurrency > read_count:
        raise ScaleHarnessError("scale concurrency is invalid")
    if not 1 <= page_size <= 50:
        raise ScaleHarnessError("scale load page size is invalid")

    quotient, remainder = divmod(read_count, concurrency)
    worker_counts = tuple(
        quotient + (1 if worker_index < remainder else 0) for worker_index in range(concurrency)
    )
    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="catalog-scale",
    ) as executor:
        warmup_barrier = threading.Barrier(concurrency)
        warmup_futures = tuple(
            executor.submit(
                _load_warmup_worker,
                reader,
                page_size=page_size,
                barrier=warmup_barrier,
            )
            for _worker_index in range(concurrency)
        )
        for future in warmup_futures:
            future.result()

        barrier = threading.Barrier(concurrency)
        started = time.perf_counter_ns()
        futures = tuple(
            executor.submit(
                _load_worker,
                reader,
                iterations=iterations,
                page_size=page_size,
                barrier=barrier,
            )
            for iterations in worker_counts
        )
        worker_results = tuple(future.result() for future in futures)
        elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000

    latencies = [
        latency
        for result in worker_results
        for latency in cast(list[float], result["latencies_milliseconds"])
    ]
    pool_waits = [
        wait
        for result in worker_results
        for wait in cast(list[float], result["pool_waits_milliseconds"])
    ]
    error_count = sum(cast(int, result["error_count"]) for result in worker_results)
    maximum_rows_read = max(cast(int, result["maximum_rows_read"]) for result in worker_results)
    maximum_materialized_items = max(
        cast(int, result["maximum_materialized_items"]) for result in worker_results
    )
    if len(latencies) != read_count:
        raise ScaleHarnessError("scale load did not account for every read")
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    latency_observations = {
        "count": len(latencies),
        "percentile_method": "nearest-rank",
        "over_p95_budget_count": sum(latency > MAX_P95_MILLISECONDS for latency in latencies),
        "over_p99_budget_count": sum(latency > MAX_P99_MILLISECONDS for latency in latencies),
    }
    regression_checks = _load_regression_checks(
        error_count=error_count,
        p95_milliseconds=p95,
        p99_milliseconds=p99,
        maximum_rows_read=maximum_rows_read,
        maximum_materialized_items=maximum_materialized_items,
        page_size=page_size,
    )
    passed = all(regression_checks.values())
    return {
        "read_count": read_count,
        "warmup_read_count": concurrency,
        "warmup_included_in_latency": False,
        "concurrency": concurrency,
        "page_size": page_size,
        "error_count": error_count,
        "zero_unexpected_errors": error_count == 0,
        "maximum_rows_read": maximum_rows_read,
        "maximum_materialized_items": maximum_materialized_items,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "reads_per_second": round(read_count / elapsed_seconds, 3),
        "latency_observations": latency_observations,
        "latency_milliseconds": {
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "maximum": round(max(latencies), 3),
        },
        "pool_wait_milliseconds": (
            {
                "status": "measured",
                "observation_count": len(pool_waits),
                "p95": round(percentile(pool_waits, 0.95), 3),
                "p99": round(percentile(pool_waits, 0.99), 3),
            }
            if len(pool_waits) == read_count
            else {
                "status": "not_available_for_backend",
                "observation_count": len(pool_waits),
                "p95": None,
                "p99": None,
            }
        ),
        "regression_budgets": {
            "p95_milliseconds": MAX_P95_MILLISECONDS,
            "p99_milliseconds": MAX_P99_MILLISECONDS,
            "production_slo": False,
        },
        "regression_checks": regression_checks,
        "passed": passed,
    }


def _load_regression_checks(
    *,
    error_count: int,
    p95_milliseconds: float,
    p99_milliseconds: float,
    maximum_rows_read: int,
    maximum_materialized_items: int,
    page_size: int,
) -> dict[str, bool]:
    """Evaluate each load gate independently from wall-clock collection."""

    return {
        "zero_unexpected_errors": error_count == 0,
        "p95_within_budget": p95_milliseconds <= MAX_P95_MILLISECONDS,
        "p99_within_budget": p99_milliseconds <= MAX_P99_MILLISECONDS,
        "rows_within_page_bound": maximum_rows_read <= page_size + 1,
        "materialized_items_within_page_bound": maximum_materialized_items <= page_size,
    }


def _load_worker(
    reader: ScaleReader,
    *,
    iterations: int,
    page_size: int,
    barrier: threading.Barrier,
) -> dict[str, object]:
    cursor: str | None = None
    latencies: list[float] = []
    pool_waits: list[float] = []
    errors = 0
    maximum_rows_read = 0
    maximum_materialized_items = 0
    barrier.wait(timeout=30)
    for _index in range(iterations):
        read_started = time.perf_counter_ns()
        try:
            page = reader.read_page("large", page_size=page_size, cursor=cursor)
            maximum_rows_read = max(maximum_rows_read, page.rows_read)
            maximum_materialized_items = max(
                maximum_materialized_items,
                len(page.item_keys),
            )
            if not _valid_load_page(page, page_size=page_size, cursor=cursor):
                raise ScaleHarnessError("scale load page contract failed")
            if page.pool_wait_milliseconds is not None:
                pool_waits.append(page.pool_wait_milliseconds)
            cursor = page.next_cursor
        except Exception:
            errors += 1
            cursor = None
        latencies.append((time.perf_counter_ns() - read_started) / 1_000_000)
    return {
        "latencies_milliseconds": latencies,
        "pool_waits_milliseconds": pool_waits,
        "error_count": errors,
        "maximum_rows_read": maximum_rows_read,
        "maximum_materialized_items": maximum_materialized_items,
    }


def _load_warmup_worker(
    reader: ScaleReader,
    *,
    page_size: int,
    barrier: threading.Barrier,
) -> None:
    """Precondition one real read on each executor worker without measuring it."""

    barrier.wait(timeout=30)
    try:
        page = reader.read_page("large", page_size=page_size, cursor=None)
    except Exception as error:
        raise ScaleHarnessError("scale load warmup read failed") from error
    if not _valid_load_page(page, page_size=page_size, cursor=None):
        raise ScaleHarnessError("scale load warmup page contract failed")


def _valid_load_page(page: ScalePage, *, page_size: int, cursor: str | None) -> bool:
    return bool(
        page.item_keys
        and len(page.item_keys) <= page_size
        and page.rows_read <= page_size + 1
        and page.next_cursor != cursor
    )


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values or not 0 < quantile <= 1:
        raise ScaleHarnessError("scale percentile input is invalid")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def build_report(
    reader: ScaleReader,
    *,
    mode: str,
    read_count: int = DEFAULT_READ_COUNT,
    concurrency: int = DEFAULT_CONCURRENCY,
    load_page_size: int = DEFAULT_LOAD_PAGE_SIZE,
    measured_at: datetime | None = None,
) -> dict[str, object]:
    if mode not in {"correctness", "benchmark"}:
        raise ScaleHarnessError("scale report mode is invalid")
    correctness, memory = run_correctness(reader)
    load = (
        run_load(
            reader,
            read_count=read_count,
            concurrency=concurrency,
            page_size=load_page_size,
        )
        if mode == "benchmark"
        else {"status": "not_run_in_correctness_mode", "passed": True}
    )
    metadata = reader.metadata
    index_plan = (
        sanitize_index_plan_evidence(
            reader.explain_index_plan(),
            evidence_profile=metadata.evidence_profile,
        )
        if mode == "benchmark"
        else {
            "status": "not_run_in_correctness_mode",
            "passed": True,
            "sql_recorded": False,
            "parameters_recorded": False,
        }
    )
    operated_evidence = collect_operated_evidence(
        reader,
        evidence_profile=metadata.evidence_profile,
        mode=mode,
    )
    deployment_connection_capacity = (
        metadata.pool_max_size * metadata.replica_count
        if metadata.pool_max_size is not None
        else None
    )
    report: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "evidence_kind": "local_scale_regression",
        "measured_at": (measured_at or datetime.now(UTC)).isoformat(),
        "production_slo": False,
        "mode": mode,
        "evidence_profile": metadata.evidence_profile,
        "backend": {
            "label": metadata.label,
            "kind": metadata.backend_kind,
            "indexed_database_read": metadata.indexed_database_read,
            "cache_context": metadata.cache_context,
            "pool_max_size_per_process": metadata.pool_max_size,
            "replica_count": metadata.replica_count,
            "deployment_connection_capacity": deployment_connection_capacity,
        },
        "environment": environment_facts(),
        "configuration": {
            "asset_cases": {"small": SMALL_ASSET_COUNT, "large": LARGE_ASSET_COUNT},
            "minimum_large_field_count": MIN_LARGE_FIELD_COUNT,
            "page_sizes": list(PAGE_SIZES),
            "load_read_count": read_count if mode == "benchmark" else None,
            "load_concurrency": concurrency if mode == "benchmark" else None,
            "load_page_size": load_page_size if mode == "benchmark" else None,
            "maximum_full_refresh_seconds": MAX_FULL_REFRESH_SECONDS,
        },
        "correctness": correctness,
        "memory": memory,
        "load": load,
        "index_plan": index_plan,
        "operated_postgres": operated_evidence,
        "limitations": [
            "Local regression evidence only; this report is not a production SLO.",
            (
                "The default lazy synthetic backend is not an indexed PostgreSQL read; "
                "its result is preflight only and cannot satisfy PostgreSQL acceptance."
                if not metadata.indexed_database_read
                else "PostgreSQL acceptance remains local regression evidence, not a production SLO."
            ),
            "Peak RSS is process-level and allocator/platform dependent.",
            "No availability, autoscaling, multi-region, or production-traffic claim is made.",
        ],
    }
    regression_passed = (
        bool(memory["passed"])
        and bool(load["passed"])
        and bool(index_plan["passed"])
        and all(bool(result["passed"]) for result in correctness)
    )
    canonical_acceptance_configuration = (
        mode == "benchmark"
        and read_count == DEFAULT_READ_COUNT
        and concurrency == DEFAULT_CONCURRENCY
        and load_page_size == DEFAULT_LOAD_PAGE_SIZE
    )
    if metadata.evidence_profile == "synthetic-preflight":
        preflight_passed: bool | None = regression_passed
        acceptance_passed: bool | None = None
        acceptance_status = "not_run_synthetic_preflight"
        passed = regression_passed
    elif mode != "benchmark":
        preflight_passed = None
        acceptance_passed = None
        acceptance_status = "not_run_in_correctness_mode"
        passed = regression_passed
    elif not canonical_acceptance_configuration:
        preflight_passed = None
        acceptance_passed = False
        acceptance_status = "failed_noncanonical_configuration"
        passed = False
    else:
        preflight_passed = None
        acceptance_passed = regression_passed and bool(operated_evidence["passed"])
        if not regression_passed:
            acceptance_status = "failed_regression"
        elif not bool(operated_evidence["passed"]):
            acceptance_status = "failed_operated_evidence"
        else:
            acceptance_status = "passed"
        passed = acceptance_passed
    if metadata.evidence_profile == "postgres-acceptance":
        if operated_evidence["status"] == "unavailable":
            cast(list[str], report["limitations"]).append(
                "Operated PostgreSQL population, refresh, and version facts were unavailable; "
                "acceptance fails closed until the report is regenerated against its fixture."
            )
        elif (
            operated_evidence["status"] == "measured"
            and operated_evidence["multi_connection_observed"] is False
        ):
            cast(list[str], report["limitations"]).append(
                "This report fixture observed one active connection; the separate isolated "
                "multi-connection integration scenario remains required."
            )
    report["regression_passed"] = regression_passed
    report["preflight_passed"] = preflight_passed
    report["acceptance_passed"] = acceptance_passed
    report["acceptance_status"] = acceptance_status
    report["passed"] = passed
    return report


def collect_operated_evidence(
    reader: ScaleReader,
    *,
    evidence_profile: str,
    mode: str,
) -> dict[str, object]:
    """Collect bounded, sanitized facts from the configured PostgreSQL acceptance fixture.

    The PostgreSQL scale reader already owns a bounded connection provider and opaque small/large
    case bindings. This harness uses only the large case's workspace as a query parameter and
    reports aggregate counts, duration, pages, and server version. It never returns the workspace,
    connection identities, SQL, query parameters, timestamps, DSN, or exception details.
    """

    if mode != "benchmark":
        return _unmeasured_operated_evidence("not_run_in_correctness_mode", passed=True)
    if evidence_profile == "synthetic-preflight":
        return _unmeasured_operated_evidence("not_applicable_synthetic_preflight", passed=True)
    if evidence_profile != "postgres-acceptance":
        raise ScaleHarnessError("scale reader evidence profile is invalid")

    provider = getattr(reader, "connection_provider", None)
    cases = getattr(reader, "cases", None)
    large_case = cases.get("large") if isinstance(cases, Mapping) else None
    workspace_id = getattr(large_case, "workspace_id", None)
    connection_context = getattr(provider, "connection", None)
    if not isinstance(workspace_id, str) or not workspace_id or not callable(connection_context):
        return _unmeasured_operated_evidence("unavailable", passed=False)

    try:
        with connection_context() as connection:
            version_row = connection.execute(
                "SELECT current_setting('server_version_num')::integer"
            ).fetchone()
            population_row = connection.execute(
                """
                SELECT
                    count(*)::bigint,
                    coalesce(sum(generation.asset_count), 0)::bigint,
                    coalesce(sum(generation.field_count), 0)::bigint,
                    coalesce(sum(refresh.source_page_number), 0)::bigint,
                    max(
                        extract(epoch FROM refresh.completed_at - refresh.requested_at)
                    )::double precision,
                    bool_and(
                        refresh.status = 'completed'
                        AND refresh.refresh_mode = 'full'
                        AND refresh.source_complete
                    )
                FROM schemabridge_control.catalog_connections AS catalog_connection
                JOIN schemabridge_control.catalog_generations AS generation
                  ON generation.workspace_id = catalog_connection.workspace_id
                 AND generation.connection_id = catalog_connection.connection_id
                 AND generation.generation = catalog_connection.active_generation
                JOIN schemabridge_control.catalog_refresh_runs AS refresh
                  ON refresh.workspace_id = generation.workspace_id
                 AND refresh.connection_id = generation.connection_id
                 AND refresh.refresh_id = generation.refresh_id
                WHERE catalog_connection.workspace_id = %s
                  AND catalog_connection.status = 'enabled'
                  AND generation.status = 'completed'
                """,
                (workspace_id,),
            ).fetchone()
    except Exception:
        return _unmeasured_operated_evidence("unavailable", passed=False)

    if (
        not isinstance(version_row, Sequence)
        or isinstance(version_row, (str, bytes))
        or len(version_row) != 1
        or not isinstance(population_row, Sequence)
        or isinstance(population_row, (str, bytes))
        or len(population_row) != 6
    ):
        return _unmeasured_operated_evidence("unavailable", passed=False)
    try:
        version = _postgres_version(int(version_row[0]))
        connection_count = int(population_row[0])
        asset_count = int(population_row[1])
        field_count = int(population_row[2])
        refresh_page_count = int(population_row[3])
        refresh_elapsed_seconds = float(population_row[4])
        full_refresh_complete = population_row[5]
    except (TypeError, ValueError, OverflowError):
        return _unmeasured_operated_evidence("unavailable", passed=False)

    valid = (
        1 <= connection_count <= 10_000
        and 0 <= asset_count <= 100_000_000
        and 0 <= field_count <= 1_000_000_000
        and 1 <= refresh_page_count <= 100_000_000
        and math.isfinite(refresh_elapsed_seconds)
        and refresh_elapsed_seconds >= 0
        and isinstance(full_refresh_complete, bool)
    )
    if not valid:
        return _unmeasured_operated_evidence("unavailable", passed=False)
    passed = (
        asset_count == LARGE_ASSET_COUNT
        and field_count >= MIN_LARGE_FIELD_COUNT
        and refresh_elapsed_seconds <= MAX_FULL_REFRESH_SECONDS
        and full_refresh_complete
    )
    return {
        "status": "measured",
        "measurement_basis": "durable-active-generation-aggregate",
        "postgresql_version": version,
        "active_connection_count": connection_count,
        "multi_connection_observed": connection_count > 1,
        "asset_count": asset_count,
        "field_count": field_count,
        "minimum_field_count": MIN_LARGE_FIELD_COUNT,
        "refresh": {
            "mode": "full",
            "complete": full_refresh_complete,
            "persisted_page_count": refresh_page_count,
            "request_to_completion_seconds": round(refresh_elapsed_seconds, 6),
            "regression_budget_seconds": MAX_FULL_REFRESH_SECONDS,
        },
        "identifiers_recorded": False,
        "sql_recorded": False,
        "parameters_recorded": False,
        "timestamps_recorded": False,
        "passed": passed,
    }


def _unmeasured_operated_evidence(status: str, *, passed: bool) -> dict[str, object]:
    if status not in {
        "not_run_in_correctness_mode",
        "not_applicable_synthetic_preflight",
        "unavailable",
    }:
        raise ScaleHarnessError("operated evidence status is invalid")
    return {
        "status": status,
        "measurement_basis": None,
        "postgresql_version": None,
        "active_connection_count": None,
        "multi_connection_observed": None,
        "asset_count": None,
        "field_count": None,
        "minimum_field_count": MIN_LARGE_FIELD_COUNT,
        "refresh": {
            "mode": None,
            "complete": None,
            "persisted_page_count": None,
            "request_to_completion_seconds": None,
            "regression_budget_seconds": MAX_FULL_REFRESH_SECONDS,
        },
        "identifiers_recorded": False,
        "sql_recorded": False,
        "parameters_recorded": False,
        "timestamps_recorded": False,
        "passed": passed,
    }


def _postgres_version(server_version_num: int) -> str:
    if not 100_000 <= server_version_num <= 9_999_999:
        raise ValueError
    return f"{server_version_num // 10_000}.{server_version_num % 10_000}"


def sanitize_index_plan_evidence(
    evidence: Mapping[str, object],
    *,
    evidence_profile: str,
) -> dict[str, object]:
    """Return one closed public plan-evidence shape and discard all other fields."""

    if not isinstance(evidence, Mapping):
        raise ScaleHarnessError("scale index-plan evidence is invalid")
    if (
        evidence.get("sql_recorded") is not False
        or evidence.get("parameters_recorded") is not False
    ):
        raise ScaleHarnessError("scale index-plan evidence contains protected detail")
    status = evidence.get("status")
    passed = evidence.get("passed")
    if not isinstance(status, str) or not isinstance(passed, bool):
        raise ScaleHarnessError("scale index-plan evidence is invalid")
    if evidence_profile == "synthetic-preflight":
        if status != "not_applicable_synthetic_preflight" or not passed:
            raise ScaleHarnessError("synthetic index-plan evidence is invalid")
        return {
            "status": status,
            "passed": True,
            "sql_recorded": False,
            "parameters_recorded": False,
        }
    if evidence_profile != "postgres-acceptance" or status != "reviewed_postgres_index_plan":
        raise ScaleHarnessError("PostgreSQL index-plan evidence is invalid")

    expected_indexes = evidence.get("expected_indexes")
    index_used = evidence.get("index_used")
    node_types = evidence.get("node_types")
    forbidden_nodes = evidence.get("forbidden_nodes")
    plan_digests = evidence.get("plan_digests")
    planner_control = evidence.get("planner_control")
    field_probe_field_count = evidence.get("field_probe_field_count")
    if (
        not isinstance(expected_indexes, Mapping)
        or dict(expected_indexes) != _EXPECTED_PLAN_INDEXES
        or not isinstance(index_used, Mapping)
        or set(index_used) != set(_EXPECTED_PLAN_INDEXES)
        or any(not isinstance(index_used[name], bool) for name in _EXPECTED_PLAN_INDEXES)
        or not isinstance(node_types, Mapping)
        or set(node_types) != set(_EXPECTED_PLAN_INDEXES)
        or not isinstance(plan_digests, Mapping)
        or set(plan_digests) != set(_EXPECTED_PLAN_INDEXES)
        or not isinstance(forbidden_nodes, Sequence)
        or isinstance(forbidden_nodes, (str, bytes))
        or planner_control != "default_postgres_planner"
        or type(field_probe_field_count) is not int
        or not 64 <= field_probe_field_count <= 1_000
    ):
        raise ScaleHarnessError("PostgreSQL index-plan evidence is invalid")

    sanitized_node_types: dict[str, list[str]] = {}
    sanitized_digests: dict[str, str] = {}
    for resource_name in _EXPECTED_PLAN_INDEXES:
        resource_nodes = node_types[resource_name]
        digest = plan_digests[resource_name]
        if (
            not isinstance(resource_nodes, Sequence)
            or isinstance(resource_nodes, (str, bytes))
            or not 1 <= len(resource_nodes) <= 64
            or any(
                not isinstance(node, str) or _PLAN_NODE.fullmatch(node) is None
                for node in resource_nodes
            )
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise ScaleHarnessError("PostgreSQL index-plan evidence is invalid")
        sanitized_node_types[resource_name] = sorted(set(resource_nodes))
        sanitized_digests[resource_name] = digest
    if any(
        not isinstance(node, str) or _PLAN_NODE.fullmatch(node) is None for node in forbidden_nodes
    ):
        raise ScaleHarnessError("PostgreSQL index-plan evidence is invalid")
    sanitized_forbidden_nodes = sorted(set(cast(Sequence[str], forbidden_nodes)))
    expected_passed = all(bool(index_used[name]) for name in _EXPECTED_PLAN_INDEXES) and not (
        sanitized_forbidden_nodes
    )
    if passed != expected_passed:
        raise ScaleHarnessError("PostgreSQL index-plan evidence is inconsistent")
    return {
        "status": status,
        "passed": passed,
        "sql_recorded": False,
        "parameters_recorded": False,
        "planner_control": "default_postgres_planner",
        "field_probe_field_count": field_probe_field_count,
        "expected_indexes": dict(_EXPECTED_PLAN_INDEXES),
        "index_used": {
            resource_name: bool(index_used[resource_name])
            for resource_name in _EXPECTED_PLAN_INDEXES
        },
        "node_types": sanitized_node_types,
        "forbidden_nodes": sanitized_forbidden_nodes,
        "plan_digests": sanitized_digests,
    }


def environment_facts() -> dict[str, object]:
    return {
        "operating_system": platform.system(),
        "operating_system_release": platform.release(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "logical_cpu_count": os.cpu_count(),
        "physical_memory_bytes": physical_memory_bytes(),
        "rss_measurement": "resource.getrusage.ru_maxrss_peak",
        "hostname_recorded": False,
        "environment_variables_recorded": False,
    }


def physical_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    return pages * page_size


def peak_rss_bytes() -> int:
    observed = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    multiplier = 1 if sys.platform == "darwin" else 1_024
    return max(0, int(observed * multiplier))


def render_markdown(report: Mapping[str, object]) -> str:
    backend = cast(Mapping[str, object], report["backend"])
    environment = cast(Mapping[str, object], report["environment"])
    memory = cast(Mapping[str, object], report["memory"])
    load = cast(Mapping[str, object], report["load"])
    index_plan = cast(Mapping[str, object], report["index_plan"])
    operated = cast(Mapping[str, object], report["operated_postgres"])
    correctness = cast(Sequence[Mapping[str, object]], report["correctness"])
    evidence_profile = cast(str, report["evidence_profile"])
    title = (
        "# M25 catalog scale preflight report"
        if evidence_profile == "synthetic-preflight"
        else "# M25 PostgreSQL catalog scale acceptance report"
    )
    lines = [
        title,
        "",
        "> Local regression evidence only. This is not a production SLO or availability claim.",
        "",
        f"- Measured at: `{report['measured_at']}`",
        f"- Evidence profile: `{evidence_profile}`",
        f"- Synthetic preflight passed: `{report['preflight_passed']}`",
        (
            "- PostgreSQL acceptance: "
            f"`{report['acceptance_status']}` (passed: `{report['acceptance_passed']}`)."
        ),
        f"- Backend: `{backend['label']}` (`{backend['kind']}`)",
        f"- Indexed PostgreSQL read: `{str(backend['indexed_database_read']).lower()}`",
        f"- Cache context: `{backend['cache_context']}`",
        (
            "- Pool max / replicas / deployment capacity: "
            f"`{backend['pool_max_size_per_process']}` / `{backend['replica_count']}` / "
            f"`{backend['deployment_connection_capacity']}`"
        ),
        (
            "- Platform: "
            f"`{environment['operating_system']} {environment['operating_system_release']} "
            f"{environment['machine']}`; Python `{environment['python_version']}`; "
            f"logical CPUs `{environment['logical_cpu_count']}`"
        ),
        "",
        "## Correctness",
        "",
        "| Case | Assets | Page size | Pages | Max rows read | Max materialized | Passed |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for result in correctness:
        lines.append(
            f"| {result['case']} | {result['asset_count']} | {result['page_size']} | "
            f"{result['page_count']} | {result['maximum_rows_read']} | "
            f"{result['maximum_materialized_items']} | {result['passed']} |"
        )
    lines.extend(
        [
            "",
            "## Memory",
            "",
            (
                "- Heap delta, large minus small bounded page: "
                f"`{memory['heap_delta_large_minus_small_bytes']}` bytes "
                f"(budget `{memory['heap_regression_budget_bytes']}`)."
            ),
            (
                "- Peak RSS growth after the large bounded page: "
                f"`{memory['rss_delta_large_after_small_bytes']}` bytes "
                f"(budget `{memory['rss_regression_budget_bytes']}`)."
            ),
            f"- Memory regression result: `{memory['passed']}`.",
            "",
            "## Load",
            "",
        ]
    )
    if load.get("status") == "not_run_in_correctness_mode":
        lines.append("- Not run in correctness mode.")
    else:
        latency = cast(Mapping[str, object], load["latency_milliseconds"])
        raw_latency_observations = load.get("latency_observations")
        latency_observations = (
            cast(Mapping[str, object], raw_latency_observations)
            if isinstance(raw_latency_observations, Mapping)
            else {
                "count": load["read_count"],
                "percentile_method": "nearest-rank",
                "over_p95_budget_count": "not_recorded",
                "over_p99_budget_count": "not_recorded",
            }
        )
        warmup_read_count = load.get("warmup_read_count", "not_recorded")
        warmup_included_in_latency = load.get("warmup_included_in_latency", "not_recorded")
        pool_wait = cast(Mapping[str, object], load["pool_wait_milliseconds"])
        lines.extend(
            [
                (
                    f"- Reads / concurrency / page size: `{load['read_count']}` / "
                    f"`{load['concurrency']}` / `{load['page_size']}`."
                ),
                (
                    "- Unmeasured per-worker preconditioning reads: "
                    f"`{warmup_read_count}`; included in reported latency: "
                    f"`{warmup_included_in_latency}`."
                ),
                f"- Unexpected errors: `{load['error_count']}`.",
                (
                    f"- Latency p50 / p95 / p99 / max: `{latency['p50']}` / "
                    f"`{latency['p95']}` / `{latency['p99']}` / `{latency['maximum']}` ms."
                ),
                (
                    "- Timed observations / percentile method / over p95 budget / over p99 "
                    f"budget: `{latency_observations['count']}` / "
                    f"`{latency_observations['percentile_method']}` / "
                    f"`{latency_observations['over_p95_budget_count']}` / "
                    f"`{latency_observations['over_p99_budget_count']}`."
                ),
                (
                    f"- Pool wait: `{pool_wait['status']}`; observations "
                    f"`{pool_wait['observation_count']}`."
                ),
                f"- Load regression result: `{load['passed']}`.",
            ]
        )
    lines.extend(
        [
            "",
            "## Reviewed index plan",
            "",
            f"- Status: `{index_plan['status']}`.",
            f"- Passed: `{index_plan['passed']}`.",
            (
                "- Planner control: "
                f"`{index_plan.get('planner_control', 'not_applicable')}` "
                "(no planner overrides)."
            ),
            (
                "- Field probe cardinality: "
                f"`{index_plan.get('field_probe_field_count', 'not_applicable')}`."
            ),
            "- SQL recorded: `false`; parameters recorded: `false`.",
            "",
            "## Operated PostgreSQL evidence",
            "",
            f"- Status: `{operated['status']}`.",
            f"- Measurement basis: `{operated['measurement_basis']}`.",
            f"- PostgreSQL version: `{operated['postgresql_version']}`.",
            (
                "- Active connections / multi-connection observed: "
                f"`{operated['active_connection_count']}` / "
                f"`{operated['multi_connection_observed']}`."
            ),
            (
                "- Assets / fields / minimum fields: "
                f"`{operated['asset_count']}` / `{operated['field_count']}` / "
                f"`{operated['minimum_field_count']}`."
            ),
            (
                "- Full refresh pages / maximum request-to-completion / budget: "
                f"`{cast(Mapping[str, object], operated['refresh'])['persisted_page_count']}` / "
                f"`{cast(Mapping[str, object], operated['refresh'])['request_to_completion_seconds']}` "
                "s / "
                f"`{cast(Mapping[str, object], operated['refresh'])['regression_budget_seconds']}` s."
            ),
            f"- Operated evidence result: `{operated['passed']}`.",
            "- Identifiers, SQL, parameters, and timestamps recorded: `false`.",
            "",
            "## Limitations",
            "",
            *(f"- {item}" for item in cast(Sequence[str], report["limitations"])),
            "",
            (
                "Overall required result for this evidence profile: "
                f"**{'PASS' if report['passed'] else 'FAIL'}**"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: Mapping[str, object],
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    json_bytes = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    markdown_bytes = render_markdown(report).encode()
    _atomic_write(json_path, json_bytes)
    _atomic_write(markdown_path, markdown_bytes)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local, non-SLO catalog scale evidence.",
    )
    parser.add_argument(
        "--mode",
        choices=("correctness", "benchmark"),
        default="benchmark",
    )
    parser.add_argument(
        "--reader-factory",
        help=(
            "Optional no-argument package.module:factory. Configure protected PostgreSQL "
            "bindings outside argv; the report records only sanitized reader metadata."
        ),
    )
    parser.add_argument("--read-count", type=int, default=DEFAULT_READ_COUNT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--load-page-size", type=int, default=DEFAULT_LOAD_PAGE_SIZE)
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("reports/m25-scale-report.json"),
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=Path("reports/m25-scale-report.md"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        reader = load_reader(arguments.reader_factory)
        try:
            report = build_report(
                reader,
                mode=arguments.mode,
                read_count=arguments.read_count,
                concurrency=arguments.concurrency,
                load_page_size=arguments.load_page_size,
            )
            write_report(
                report,
                json_path=arguments.output_json,
                markdown_path=arguments.output_markdown,
            )
        finally:
            reader.close()
    except Exception:
        print("Catalog scale evidence failed safely.", file=sys.stderr)
        return 1
    print(
        "Catalog scale evidence written: "
        f"profile={report['evidence_profile']} mode={arguments.mode} "
        f"passed={str(report['passed']).lower()} "
        f"acceptance={report['acceptance_status']} "
        f"json={arguments.output_json} markdown={arguments.output_markdown}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
