"""Bounded PostgreSQL EXPLAIN cost preflight without query execution."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import psycopg
from psycopg.abc import Buffer
from psycopg.adapt import Loader
from psycopg.pq import Format

from schemabridge.adapters.connectors.source_identity import (
    PostgresSourceIdentityMismatchError,
    require_postgres_source_identity,
)
from schemabridge.adapters.postgres.preview import _postgres_interval_to_milliseconds
from schemabridge.adapters.postgres.transient_errors import is_transient_postgres_error
from schemabridge.application.query_execution import ValidatedQuery
from schemabridge.domain.connectors import (
    MAX_COST_DECIMAL_PLACES,
    MAX_ESTIMATED_ROOT_ROWS,
    MAX_EXPLAIN_RESPONSE_BYTES,
    MAX_PLAN_DEPTH,
    MAX_PLAN_NODES,
    MAX_PLAN_WIDTH,
    MAX_QUERY_TOTAL_COST,
    GovernedExecutionTarget,
    QueryCostAssessment,
    QueryCostDecision,
    QueryCostRejectionCode,
    SourceDialect,
)

_EXPLAIN_PREFIX = (
    "EXPLAIN (FORMAT JSON, COSTS TRUE, ANALYZE FALSE, BUFFERS FALSE, "
    "VERBOSE FALSE, SETTINGS FALSE) "
)


class _ExplainResponseBytesExceeded(ValueError):
    """Internal sentinel for a raw EXPLAIN response rejected before JSON decoding."""


class _BoundedExplainJsonLoader(Loader):
    """Keep EXPLAIN JSON raw and reject it before Python object materialization."""

    format = Format.TEXT

    def load(self, data: Buffer) -> bytes:
        response_bytes = data.nbytes if isinstance(data, memoryview) else len(data)
        if response_bytes < 1:
            raise ValueError("PostgreSQL returned an empty cost plan")
        if response_bytes > MAX_EXPLAIN_RESPONSE_BYTES:
            raise _ExplainResponseBytesExceeded
        return bytes(data)


@dataclass(frozen=True, slots=True)
class _ObservedPlan:
    total_cost: Decimal
    estimated_root_rows: int
    plan_width: int
    plan_node_count: int
    plan_depth: int
    response_bytes: int


@dataclass(frozen=True, slots=True)
class PsycopgQueryCostPreflight:
    """Extract only sanitized planner aggregates under independent source controls."""

    dsn: str = field(repr=False)
    connect_timeout_seconds: int = 3

    def assess(
        self,
        query: ValidatedQuery,
        target: GovernedExecutionTarget,
    ) -> QueryCostAssessment:
        budget = target.cost_budget
        if (
            query.dialect is not SourceDialect.POSTGRESQL
            or target.dialect is not SourceDialect.POSTGRESQL
            or query.target_fingerprint != target.fingerprint
        ):
            return _rejected(
                target,
                QueryCostRejectionCode.TARGET_FINGERPRINT_MISMATCH,
            )
        try:
            with psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout_seconds,
                autocommit=False,
            ) as connection:
                connection.adapters.register_loader("json", _BoundedExplainJsonLoader)
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(budget.explain_timeout_ms),),
                    )
                    cursor.fetchone()
                    cursor.execute(
                        """
                        SELECT
                            current_user,
                            current_setting('transaction_read_only')::BOOLEAN,
                            current_setting('statement_timeout'),
                            COALESCE(inet_server_addr()::TEXT, 'local_socket'),
                            COALESCE(inet_server_port(), 0),
                            current_database()
                        """
                    )
                    safety = cursor.fetchone()
                    if safety is None:
                        connection.rollback()
                        return _rejected(target, QueryCostRejectionCode.INVALID)
                    (
                        reader,
                        read_only,
                        timeout_setting,
                        server_address,
                        server_port,
                        database,
                    ) = cast(tuple[str, bool, str, str, int, str], safety)
                    timeout_ms = _postgres_interval_to_milliseconds(timeout_setting)
                    if (
                        reader != target.expected_reader
                        or read_only is not True
                        or timeout_ms != budget.explain_timeout_ms
                    ):
                        connection.rollback()
                        return _rejected(
                            target,
                            QueryCostRejectionCode.INVALID,
                            observed_reader=reader if isinstance(reader, str) else None,
                            read_only=read_only if isinstance(read_only, bool) else None,
                        )
                    try:
                        require_postgres_source_identity(
                            expected_fingerprint=target.source_identity_fingerprint,
                            server_address=server_address,
                            server_port=server_port,
                            database=database,
                            user=reader,
                        )
                    except PostgresSourceIdentityMismatchError:
                        connection.rollback()
                        raise
                    cursor.execute(f"{_EXPLAIN_PREFIX}{query.sql}", query.parameters)
                    row = cursor.fetchone()
                    if row is None or len(row) != 1 or cursor.fetchone() is not None:
                        connection.rollback()
                        return _rejected(
                            target,
                            QueryCostRejectionCode.INVALID,
                            observed_reader=reader,
                            read_only=read_only,
                        )
                    observed = _parse_cost_plan(
                        row[0],
                        maximum_response_bytes=budget.max_response_bytes,
                    )
                connection.rollback()
        except psycopg.errors.QueryCanceled:
            return _rejected(target, QueryCostRejectionCode.TIMEOUT)
        except PostgresSourceIdentityMismatchError:
            raise
        except _ExplainResponseBytesExceeded:
            return _rejected(target, QueryCostRejectionCode.RESPONSE_BYTES_EXCEEDED)
        except psycopg.Error as error:
            code = (
                QueryCostRejectionCode.UNAVAILABLE
                if is_transient_postgres_error(error)
                else QueryCostRejectionCode.INVALID
            )
            return _rejected(target, code)
        except (TypeError, ValueError, InvalidOperation, OverflowError, RecursionError):
            return _rejected(target, QueryCostRejectionCode.INVALID)

        reasons: set[QueryCostRejectionCode] = set()
        if observed.total_cost > budget.max_total_cost:
            reasons.add(QueryCostRejectionCode.TOTAL_COST_EXCEEDED)
        if observed.estimated_root_rows > budget.max_estimated_rows:
            reasons.add(QueryCostRejectionCode.ESTIMATED_ROWS_EXCEEDED)
        if observed.plan_width > budget.max_plan_width:
            reasons.add(QueryCostRejectionCode.PLAN_WIDTH_EXCEEDED)
        if observed.plan_node_count > budget.max_plan_nodes:
            reasons.add(QueryCostRejectionCode.PLAN_NODES_EXCEEDED)
        if observed.plan_depth > budget.max_plan_depth:
            reasons.add(QueryCostRejectionCode.PLAN_DEPTH_EXCEEDED)
        if observed.response_bytes > budget.max_response_bytes:
            reasons.add(QueryCostRejectionCode.RESPONSE_BYTES_EXCEEDED)
        return QueryCostAssessment(
            decision=(QueryCostDecision.REJECTED if reasons else QueryCostDecision.ACCEPTED),
            rejection_codes=tuple(sorted(reasons, key=lambda item: item.value)),
            total_cost=observed.total_cost,
            estimated_root_rows=observed.estimated_root_rows,
            plan_width=observed.plan_width,
            plan_node_count=observed.plan_node_count,
            plan_depth=observed.plan_depth,
            response_bytes=observed.response_bytes,
            observed_reader=reader,
            read_only=read_only,
            explain_timeout_ms=timeout_ms,
            target_fingerprint=target.fingerprint,
            budget_fingerprint=target.cost_budget_fingerprint,
            cost_budget=budget,
        )


def _parse_cost_plan(
    value: object,
    *,
    maximum_response_bytes: int = MAX_EXPLAIN_RESPONSE_BYTES,
) -> _ObservedPlan:
    if not isinstance(value, bytes | bytearray | memoryview):
        raise TypeError("PostgreSQL cost plan must remain raw until bounded")
    response_bytes = value.nbytes if isinstance(value, memoryview) else len(value)
    if response_bytes < 1:
        raise ValueError("PostgreSQL returned an empty cost plan")
    if response_bytes > MAX_EXPLAIN_RESPONSE_BYTES or response_bytes > maximum_response_bytes:
        raise _ExplainResponseBytesExceeded
    raw = bytes(value) if isinstance(value, memoryview | bytearray) else value
    decoded = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
        parse_float=Decimal,
    )
    if (
        not isinstance(decoded, list)
        or len(decoded) != 1
        or not isinstance(decoded[0], dict)
        or set(decoded[0]) != {"Plan"}
        or not isinstance(decoded[0]["Plan"], dict)
    ):
        raise ValueError("PostgreSQL cost plan has an invalid envelope")
    root = cast(dict[str, Any], decoded[0]["Plan"])
    total_cost = _decimal_metric(root, "Total Cost")
    estimated_root_rows = _integer_metric(root, "Plan Rows", MAX_ESTIMATED_ROOT_ROWS)

    nodes = 0
    maximum_depth = 0
    maximum_width = 0
    stack: list[tuple[dict[str, Any], int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PLAN_NODES or depth > MAX_PLAN_DEPTH:
            raise ValueError("PostgreSQL cost plan exceeds structural hard bounds")
        maximum_depth = max(maximum_depth, depth)
        maximum_width = max(
            maximum_width,
            _integer_metric(node, "Plan Width", MAX_PLAN_WIDTH),
        )
        children = node.get("Plans", ())
        if not isinstance(children, list | tuple):
            raise ValueError("PostgreSQL cost plan children are invalid")
        for child in reversed(children):
            if not isinstance(child, dict):
                raise ValueError("PostgreSQL cost plan node is invalid")
            stack.append((cast(dict[str, Any], child), depth + 1))
    return _ObservedPlan(
        total_cost=total_cost,
        estimated_root_rows=estimated_root_rows,
        plan_width=maximum_width,
        plan_node_count=nodes,
        plan_depth=maximum_depth,
        response_bytes=response_bytes,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("PostgreSQL cost plan contains duplicate keys")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("PostgreSQL cost plan contains a non-finite JSON value")


def _decimal_metric(node: dict[str, Any], key: str) -> Decimal:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise ValueError("PostgreSQL cost metric is invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("PostgreSQL cost metric must be finite")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_QUERY_TOTAL_COST:
        raise ValueError("PostgreSQL cost metric is outside hard bounds")
    exponent = parsed.normalize().as_tuple().exponent
    if not isinstance(exponent, int) or max(0, -exponent) > MAX_COST_DECIMAL_PLACES:
        raise ValueError("PostgreSQL cost metric has excessive decimal precision")
    return parsed


def _integer_metric(node: dict[str, Any], key: str, maximum: int) -> int:
    value = node.get(key)
    if isinstance(value, bool):
        raise ValueError("PostgreSQL count metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("PostgreSQL count metric is invalid") from None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError("PostgreSQL count metric must be a finite integer")
    integer = int(parsed)
    if integer < 0 or integer > maximum:
        raise ValueError("PostgreSQL count metric is outside hard bounds")
    return integer


def _rejected(
    target: GovernedExecutionTarget,
    code: QueryCostRejectionCode,
    *,
    observed_reader: str | None = None,
    read_only: bool | None = None,
) -> QueryCostAssessment:
    return QueryCostAssessment(
        decision=QueryCostDecision.REJECTED,
        rejection_codes=(code,),
        observed_reader=observed_reader,
        read_only=read_only,
        explain_timeout_ms=target.cost_budget.explain_timeout_ms,
        target_fingerprint=target.fingerprint,
        budget_fingerprint=target.cost_budget_fingerprint,
        cost_budget=target.cost_budget,
    )
