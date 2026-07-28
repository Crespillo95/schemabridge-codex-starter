from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import scripts.benchmark_catalog_scale as scale_harness
from scripts.benchmark_catalog_scale import (
    LARGE_ASSET_COUNT,
    MAX_FULL_REFRESH_SECONDS,
    MAX_HEAP_DELTA_BYTES,
    MAX_RSS_DELTA_BYTES,
    MIN_LARGE_FIELD_COUNT,
    SMALL_ASSET_COUNT,
    LazySyntheticScaleReader,
    ScaleHarnessError,
    ScalePage,
    ScaleReader,
    ScaleReaderMetadata,
    build_report,
    collect_operated_evidence,
    load_reader,
    main,
    percentile,
    render_markdown,
    run_load,
    sanitize_index_plan_evidence,
    traverse_inventory,
    write_report,
)

MEASURED_AT = datetime(2026, 7, 23, 22, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def correctness_report() -> dict[str, object]:
    return build_report(
        LazySyntheticScaleReader(),
        mode="correctness",
        measured_at=MEASURED_AT,
    )


@pytest.mark.scale
def test_correctness_report_covers_both_cardinalities_and_three_page_sizes(
    correctness_report: dict[str, object],
) -> None:
    report = correctness_report
    assert report["format_version"] == 2
    assert report["production_slo"] is False
    assert report["measured_at"] == MEASURED_AT.isoformat()
    assert report["evidence_profile"] == "synthetic-preflight"
    assert report["preflight_passed"] is True
    assert report["acceptance_passed"] is None
    assert report["acceptance_status"] == "not_run_synthetic_preflight"
    configuration = report["configuration"]
    assert isinstance(configuration, dict)
    assert configuration["asset_cases"] == {
        "small": SMALL_ASSET_COUNT,
        "large": LARGE_ASSET_COUNT,
    }
    assert configuration["page_sizes"] == [1, 17, 50]
    assert configuration["minimum_large_field_count"] == MIN_LARGE_FIELD_COUNT
    assert configuration["maximum_full_refresh_seconds"] == MAX_FULL_REFRESH_SECONDS

    correctness = report["correctness"]
    assert isinstance(correctness, list)
    assert [
        (
            item["case"],
            item["page_size"],
            item["asset_count"],
            item["page_count"],
        )
        for item in correctness
    ] == [
        ("small", 1, 10, 10),
        ("small", 17, 10, 1),
        ("small", 50, 10, 1),
        ("large", 1, 5_434, 5_434),
        ("large", 17, 5_434, 320),
        ("large", 50, 5_434, 109),
    ]
    assert all(item["maximum_materialized_items"] <= item["page_size"] for item in correctness)
    assert all(item["maximum_rows_read"] <= item["page_size"] + 1 for item in correctness)
    assert all(item["passed"] is True for item in correctness)

    memory = report["memory"]
    assert isinstance(memory, dict)
    assert memory["heap_regression_budget_bytes"] == MAX_HEAP_DELTA_BYTES
    assert memory["rss_regression_budget_bytes"] == MAX_RSS_DELTA_BYTES
    assert memory["production_slo"] is False
    assert memory["large"]["materialized_items"] == 50
    assert report["load"] == {
        "status": "not_run_in_correctness_mode",
        "passed": True,
    }
    assert report["index_plan"] == {
        "status": "not_run_in_correctness_mode",
        "passed": True,
        "sql_recorded": False,
        "parameters_recorded": False,
    }
    operated = report["operated_postgres"]
    assert isinstance(operated, dict)
    assert operated["status"] == "not_run_in_correctness_mode"
    assert operated["passed"] is True

    backend = report["backend"]
    assert isinstance(backend, dict)
    assert backend["indexed_database_read"] is False
    assert backend["pool_max_size_per_process"] is None
    environment = report["environment"]
    assert isinstance(environment, dict)
    assert environment["hostname_recorded"] is False
    assert environment["environment_variables_recorded"] is False

    encoded = json.dumps(report, sort_keys=True).casefold()
    for forbidden in (
        "openai_api_key",
        "datahub_gms_token",
        "database_url",
        "postgresql://",
        "password",
        "workspace-small",
        "tenant-small",
        "credential_binding",
    ):
        assert forbidden not in encoded
    markdown = render_markdown(report)
    assert "not a production slo" in markdown.casefold()
    assert "scale preflight report" in markdown.casefold()
    assert "not_run_synthetic_preflight" in markdown
    assert "indexed postgresql read: `false`" in markdown.casefold()


@pytest.mark.scale
def test_load_harness_records_latency_concurrency_pool_wait_and_zero_errors() -> None:
    result = run_load(
        LazySyntheticScaleReader(),
        read_count=64,
        concurrency=4,
        page_size=17,
    )

    assert result["read_count"] == 64
    assert result["concurrency"] == 4
    assert result["error_count"] == 0
    assert result["zero_unexpected_errors"] is True
    assert cast(int, result["maximum_rows_read"]) <= 18
    assert cast(int, result["maximum_materialized_items"]) <= 17
    assert result["passed"] is True
    latency = result["latency_milliseconds"]
    assert isinstance(latency, dict)
    assert 0 <= latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["maximum"]
    assert result["pool_wait_milliseconds"] == {
        "status": "not_available_for_backend",
        "observation_count": 0,
        "p95": None,
        "p99": None,
    }
    budgets = result["regression_budgets"]
    assert isinstance(budgets, dict)
    assert budgets["production_slo"] is False


def test_traversal_rejects_duplicate_or_omitted_identity_without_collecting_inventory() -> None:
    class _BrokenReader:
        @property
        def metadata(self) -> ScaleReaderMetadata:
            return ScaleReaderMetadata(
                label="broken-reader",
                backend_kind="synthetic-callable",
                evidence_profile="synthetic-preflight",
                indexed_database_read=False,
                pool_max_size=None,
                replica_count=1,
                cache_context="cold",
            )

        def read_page(
            self,
            case: str,
            *,
            page_size: int,
            cursor: str | None,
        ) -> ScalePage:
            del case, page_size
            if cursor is None:
                return ScalePage(
                    item_keys=("synthetic-asset-00000000",),
                    next_cursor="second-page",
                    rows_read=1,
                )
            return ScalePage(
                item_keys=("synthetic-asset-00000000",),
                next_cursor=None,
                rows_read=1,
            )

    with pytest.raises(ScaleHarnessError, match="order or identity"):
        traverse_inventory(
            cast(ScaleReader, _BrokenReader()),
            case="small",
            expected_count=2,
            page_size=1,
        )


def test_reader_factory_is_explicit_and_validated() -> None:
    default = load_reader(None)
    explicit = load_reader("scripts.benchmark_catalog_scale:create_synthetic_reader")

    assert default.metadata == explicit.metadata
    with pytest.raises(ScaleHarnessError, match="reference"):
        load_reader("scripts.benchmark_catalog_scale")
    with pytest.raises(ScaleHarnessError, match="unavailable"):
        load_reader("scripts.missing_scale_reader:create")


def test_cli_failure_does_not_emit_factory_or_exception_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "postgresql://private-user:private-password@internal/control"

    exit_code = main(
        [
            "--reader-factory",
            "scripts.missing_scale_reader:create",
            "--output-json",
            str(tmp_path / "scale.json"),
            "--output-markdown",
            str(tmp_path / "scale.md"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "Catalog scale evidence failed safely.\n"
    assert secret not in captured.err
    assert not (tmp_path / "scale.json").exists()


def test_report_writer_creates_json_and_markdown_without_runtime_paths(
    tmp_path: Path,
    correctness_report: dict[str, object],
) -> None:
    report = correctness_report
    json_path = tmp_path / "scale.json"
    markdown_path = tmp_path / "scale.md"

    write_report(
        report,
        json_path=json_path,
        markdown_path=markdown_path,
    )

    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# M25 catalog scale preflight report\n")
    assert str(tmp_path) not in markdown


def test_percentile_uses_deterministic_nearest_rank() -> None:
    observations = [4.0, 1.0, 5.0, 2.0, 3.0]

    assert percentile(observations, 0.50) == 3.0
    assert percentile(observations, 0.95) == 5.0
    assert percentile(observations, 0.99) == 5.0
    with pytest.raises(ScaleHarnessError):
        percentile([], 0.95)


def test_index_plan_evidence_is_closed_and_discards_protected_factory_fields() -> None:
    evidence = sanitize_index_plan_evidence(
        {
            "status": "reviewed_postgres_index_plan",
            "passed": True,
            "sql_recorded": False,
            "parameters_recorded": False,
            "planner_control": "default_postgres_planner",
            "field_probe_field_count": 64,
            "expected_indexes": {
                "assets": "catalog_assets_keyset_idx",
                "fields": "catalog_fields_keyset_idx",
            },
            "index_used": {"assets": True, "fields": True},
            "node_types": {
                "assets": ["Limit", "Index Scan"],
                "fields": ["Limit", "Index Scan"],
            },
            "forbidden_nodes": [],
            "plan_digests": {"assets": "a" * 64, "fields": "b" * 64},
            "sql": "SELECT protected",
            "workspace_id": "workspace-private",
        },
        evidence_profile="postgres-acceptance",
    )

    encoded = json.dumps(evidence, sort_keys=True)
    assert evidence["passed"] is True
    assert "SELECT protected" not in encoded
    assert "workspace-private" not in encoded


def test_only_canonical_postgres_benchmark_can_pass_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PostgresReader:
        @property
        def metadata(self) -> ScaleReaderMetadata:
            return ScaleReaderMetadata(
                label="postgres-catalog-v4",
                backend_kind="postgresql-keyset",
                evidence_profile="postgres-acceptance",
                indexed_database_read=True,
                pool_max_size=16,
                replica_count=1,
                cache_context="warm-after-correctness",
            )

        def explain_index_plan(self) -> dict[str, object]:
            return {
                "status": "reviewed_postgres_index_plan",
                "passed": True,
                "sql_recorded": False,
                "parameters_recorded": False,
                "planner_control": "default_postgres_planner",
                "field_probe_field_count": 64,
                "expected_indexes": {
                    "assets": "catalog_assets_keyset_idx",
                    "fields": "catalog_fields_keyset_idx",
                },
                "index_used": {"assets": True, "fields": True},
                "node_types": {
                    "assets": ["Limit", "Index Scan"],
                    "fields": ["Limit", "Index Scan"],
                },
                "forbidden_nodes": [],
                "plan_digests": {"assets": "a" * 64, "fields": "b" * 64},
            }

    monkeypatch.setattr(
        scale_harness,
        "run_correctness",
        lambda _reader: ([{"passed": True}], {"passed": True}),
    )
    monkeypatch.setattr(
        scale_harness,
        "run_load",
        lambda _reader, **_arguments: {"passed": True},
    )
    monkeypatch.setattr(
        scale_harness,
        "collect_operated_evidence",
        lambda _reader, **_arguments: {
            "status": "measured",
            "passed": True,
            "multi_connection_observed": True,
        },
    )

    canonical = build_report(cast(ScaleReader, _PostgresReader()), mode="benchmark")
    noncanonical = build_report(
        cast(ScaleReader, _PostgresReader()),
        mode="benchmark",
        read_count=4,
        concurrency=2,
    )

    assert canonical["preflight_passed"] is None
    assert canonical["acceptance_passed"] is True
    assert canonical["acceptance_status"] == "passed"
    assert canonical["passed"] is True
    assert noncanonical["acceptance_passed"] is False
    assert noncanonical["acceptance_status"] == "failed_noncanonical_configuration"
    assert noncanonical["passed"] is False


class _EvidenceResult:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...]:
        return self._row


class _EvidenceConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self.parameter_counts: list[int] = []
        self.statements: list[str] = []

    def execute(
        self,
        _statement: object,
        parameters: tuple[object, ...] = (),
    ) -> _EvidenceResult:
        self.parameter_counts.append(len(parameters))
        self.statements.append(str(_statement))
        return _EvidenceResult(self._rows.pop(0))


class _EvidenceContext:
    def __init__(self, connection: _EvidenceConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _EvidenceConnection:
        return self._connection

    def __exit__(self, *_arguments: object) -> None:
        return None


class _EvidenceProvider:
    def __init__(self, connection: _EvidenceConnection) -> None:
        self._connection = connection

    def connection(self) -> _EvidenceContext:
        return _EvidenceContext(self._connection)


def test_operated_postgres_evidence_is_bounded_sanitized_and_acceptance_complete() -> None:
    connection = _EvidenceConnection(
        [
            (160013,),
            (2, LARGE_ASSET_COUNT, 40_743, 110, 10.572819, True),
        ]
    )
    reader = type(
        "_Reader",
        (),
        {
            "connection_provider": _EvidenceProvider(connection),
            "cases": {
                "large": type(
                    "_Case",
                    (),
                    {"workspace_id": "protected-workspace"},
                )()
            },
        },
    )()

    evidence = collect_operated_evidence(
        reader,
        evidence_profile="postgres-acceptance",
        mode="benchmark",
    )

    assert evidence == {
        "status": "measured",
        "measurement_basis": "durable-active-generation-aggregate",
        "postgresql_version": "16.13",
        "active_connection_count": 2,
        "multi_connection_observed": True,
        "asset_count": LARGE_ASSET_COUNT,
        "field_count": 40_743,
        "minimum_field_count": MIN_LARGE_FIELD_COUNT,
        "refresh": {
            "mode": "full",
            "complete": True,
            "persisted_page_count": 110,
            "request_to_completion_seconds": 10.572819,
            "regression_budget_seconds": MAX_FULL_REFRESH_SECONDS,
        },
        "identifiers_recorded": False,
        "sql_recorded": False,
        "parameters_recorded": False,
        "timestamps_recorded": False,
        "passed": True,
    }
    assert connection.parameter_counts == [0, 1]
    assert "max(" in connection.statements[1]
    assert "refresh.completed_at - refresh.requested_at" in connection.statements[1]
    assert "max(refresh.completed_at) - min(refresh.requested_at)" not in connection.statements[1]
    encoded = json.dumps(evidence, sort_keys=True)
    assert "protected-workspace" not in encoded
    assert "SELECT" not in encoded


@pytest.mark.parametrize(
    ("population", "expected_status"),
    [
        ((1, LARGE_ASSET_COUNT, 39_999, 109, 10.0, True), "measured"),
        ((1, LARGE_ASSET_COUNT, 40_743, 109, 60.001, True), "measured"),
        ((1, LARGE_ASSET_COUNT, 40_743, 109, 10.0, False), "measured"),
    ],
)
def test_operated_postgres_evidence_fails_closed_when_required_fact_misses_budget(
    population: tuple[object, ...],
    expected_status: str,
) -> None:
    connection = _EvidenceConnection([(160013,), population])
    reader = type(
        "_Reader",
        (),
        {
            "connection_provider": _EvidenceProvider(connection),
            "cases": {
                "large": type(
                    "_Case",
                    (),
                    {"workspace_id": "protected-workspace"},
                )()
            },
        },
    )()

    evidence = collect_operated_evidence(
        reader,
        evidence_profile="postgres-acceptance",
        mode="benchmark",
    )

    assert evidence["status"] == expected_status
    assert evidence["passed"] is False


def test_postgres_acceptance_fails_when_operated_evidence_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PostgresReader:
        @property
        def metadata(self) -> ScaleReaderMetadata:
            return ScaleReaderMetadata(
                label="postgres-catalog-v4",
                backend_kind="postgresql-keyset",
                evidence_profile="postgres-acceptance",
                indexed_database_read=True,
                pool_max_size=16,
                replica_count=1,
                cache_context="warm-after-correctness",
            )

        def explain_index_plan(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        scale_harness,
        "run_correctness",
        lambda _reader: ([{"passed": True}], {"passed": True}),
    )
    monkeypatch.setattr(
        scale_harness,
        "run_load",
        lambda _reader, **_arguments: {"passed": True},
    )
    monkeypatch.setattr(
        scale_harness,
        "sanitize_index_plan_evidence",
        lambda *_arguments, **_keywords: {"passed": True},
    )

    report = build_report(cast(ScaleReader, _PostgresReader()), mode="benchmark")

    assert report["regression_passed"] is True
    assert report["acceptance_passed"] is False
    assert report["acceptance_status"] == "failed_operated_evidence"
    assert report["passed"] is False
    operated = cast(dict[str, object], report["operated_postgres"])
    assert operated["status"] == "unavailable"
