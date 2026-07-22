"""Regression tests for bounded, policy-consistent rejection inspection."""

from __future__ import annotations

from typing import Any

from schemabridge.adapters.postgres.rejections import (
    PsycopgRejectedSourceReporter,
    _inspection_query,
)
from schemabridge.application.join_demo import build_north_star_join_proposals
from schemabridge.domain.resolution import RejectionCheck, SourceRejectionCode


class _Cursor:
    def __init__(self) -> None:
        self.current = ""
        self.fetchmany_sizes: list[int] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, parameters: object = None) -> None:
        del parameters
        self.current = query if isinstance(query, str) else query.as_string()

    def fetchone(self) -> tuple[str, bool, str] | None:
        if "current_user" in self.current:
            return ("schemabridge_reader", True, "5s")
        return None

    def fetchmany(self, size: int) -> list[tuple[str, str, int]]:
        self.fetchmany_sizes.append(size)
        return [
            ("-1", "negative_identifier", 3),
            ("127.5", "non_integral_identifier", 3),
        ][:size]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.read_only = False
        self._cursor = cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def rollback(self) -> None:
        return None


def _check(index: int) -> RejectionCheck:
    key = (
        build_north_star_join_proposals()[0].left_key
        if index == 0
        else build_north_star_join_proposals()[0].right_key
    )
    return RejectionCheck(
        logical_field=key.logical_field,
        physical_field=key.physical_field,
        transformation_plan=key.transformation_plan,
    )


def test_rejection_report_is_bounded_and_reports_exact_total(monkeypatch: Any) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(
        "schemabridge.adapters.postgres.rejections.psycopg.connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )
    reporter = PsycopgRejectedSourceReporter(
        "postgresql://synthetic",
        allowed_fields=frozenset({_check(1).physical_field.root}),
        max_records=1,
    )

    report = reporter.inspect((_check(1),), statement_timeout_ms=5_000)

    assert cursor.fetchmany_sizes == [2]
    assert report.total_records == 3
    assert report.truncated is True
    assert len(report.records) == 1
    assert report.records[0].code is SourceRejectionCode.NEGATIVE_IDENTIFIER


def test_rejection_sql_has_negative_paths_count_and_hard_limit() -> None:
    numeric_sql = _inspection_query(_check(1), limit=11)[0].as_string()
    string_sql = _inspection_query(_check(0), limit=11)[0].as_string()

    assert "WHEN \"gf_customer_id\" < 0 THEN 'negative_identifier'" in numeric_sql
    assert "COUNT(*) OVER () AS total_records" in numeric_sql
    assert "LIMIT 11" in numeric_sql
    assert "~ '^-[0-9]+$' THEN 'negative_identifier'" in string_sql
