#!/usr/bin/env python3
"""Verify the deterministic demo corpus without mutating PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "demo" / "ground_truth" / "seed_manifest.yml"
DEFAULT_DATABASE_URL = (
    "postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
)
CANONICALIZATION = "typed-json-lines-v1"
SCHEMAS = (
    "bank",
    "commerce",
    "crm",
    "fulfillment",
    "legacy",
    "reporting",
    "sales",
    "support",
)


class SeedVerificationError(RuntimeError):
    """Raised when the checked demo corpus differs from the tracked contract."""


@dataclass(frozen=True)
class TableSpec:
    """Static allowlist for one synthetic table and its canonical row order."""

    columns: tuple[str, ...]
    order_by: tuple[str, ...]


TABLES: dict[str, TableSpec] = {
    "bank.account_holders": TableSpec(
        columns=(
            "holder_link_id",
            "account_number",
            "gf_customer_id",
            "holder_type",
            "relationship_start_date",
            "relationship_end_date",
        ),
        order_by=("holder_link_id",),
    ),
    "bank.accounts": TableSpec(
        columns=("account_number", "opening_date", "account_status", "current_balance"),
        order_by=("account_number",),
    ),
    "commerce.products": TableSpec(
        columns=("product_code", "category_code", "unit_price", "is_active", "created_at"),
        order_by=("product_code",),
    ),
    "crm.customers": TableSpec(
        columns=("customer_id", "registration_date", "country_cd", "customer_status"),
        order_by=("customer_id",),
    ),
    "fulfillment.shipments": TableSpec(
        columns=("shipment_id", "order_ref", "status_code", "shipped_at", "delivered_at"),
        order_by=("shipment_id",),
    ),
    "legacy.client_master": TableSpec(
        columns=("client_no", "created_dt", "country", "status_code"),
        order_by=("client_no",),
    ),
    "legacy.item_master": TableSpec(
        columns=(
            "item_no",
            "item_name",
            "category_cd",
            "price_text",
            "active_flag",
            "loaded_at",
        ),
        order_by=("item_no",),
    ),
    "reporting.customer_accounts": TableSpec(
        columns=(
            "report_date",
            "customer_key_text",
            "account_number",
            "holder_role_normalized",
        ),
        order_by=("report_date", "customer_key_text", "account_number"),
    ),
    "sales.order_lines": TableSpec(
        columns=(
            "line_id",
            "order_ref",
            "product_no",
            "quantity",
            "net_amount",
            "discount_amount",
        ),
        order_by=("line_id",),
    ),
    "sales.orders": TableSpec(
        columns=(
            "order_id",
            "ordered_at",
            "status_code",
            "sales_channel",
            "region_code",
            "order_total",
        ),
        order_by=("order_id",),
    ),
    "support.order_cases": TableSpec(
        columns=(
            "case_id",
            "order_id",
            "product_id",
            "customer_id",
            "status",
            "priority",
            "opened_at",
            "closed_at",
            "case_summary",
        ),
        order_by=("case_id",),
    ),
}


def canonical_value(value: Any) -> Any:
    """Return a JSON-safe, type-preserving representation of a database value."""

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": format(value, "f")}
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "NaN"
        elif math.isinf(value):
            rendered = "Infinity" if value > 0 else "-Infinity"
        elif value == 0:
            rendered = "0"
        else:
            rendered = format(value, ".17g")
        return {"type": "float", "value": rendered}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat(timespec="microseconds")}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence):
        return [canonical_value(item) for item in value]
    raise SeedVerificationError(f"Unsupported canonical value type: {type(value).__name__}.")


def canonical_sha256(records: Sequence[Any]) -> str:
    """Hash ordered records as canonical typed JSON lines."""

    digest = hashlib.sha256()
    for record in records:
        encoded = json.dumps(
            canonical_value(record),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash the complete snapshot except for its self-referential fingerprint."""

    payload = {key: value for key, value in snapshot.items() if key != "global_sha256"}
    return canonical_sha256((payload,))


def _dictionary_rows(cursor: Any) -> list[dict[str, Any]]:
    column_names = tuple(column.name for column in cursor.description)
    return [dict(zip(column_names, row, strict=True)) for row in cursor.fetchall()]


def _read_schema_metadata(cursor: Any) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            'schema' AS object_kind,
            namespace.nspname AS table_schema,
            NULL::TEXT AS table_name,
            NULL::INTEGER AS ordinal_position,
            NULL::TEXT AS column_name,
            NULL::TEXT AS data_type,
            NULL::TEXT AS udt_name,
            NULL::TEXT AS is_nullable,
            NULL::INTEGER AS character_maximum_length,
            NULL::INTEGER AS numeric_precision,
            NULL::INTEGER AS numeric_scale,
            NULL::TEXT AS column_default,
            obj_description(namespace.oid, 'pg_namespace') AS object_description
        FROM pg_namespace AS namespace
        WHERE namespace.nspname = ANY(%s)

        UNION ALL

        SELECT
            'column' AS object_kind,
            columns.table_schema,
            columns.table_name,
            columns.ordinal_position,
            columns.column_name,
            columns.data_type,
            columns.udt_name,
            columns.is_nullable,
            columns.character_maximum_length,
            columns.numeric_precision,
            columns.numeric_scale,
            columns.column_default,
            col_description(relation.oid, attributes.attnum) AS object_description
        FROM information_schema.columns AS columns
        INNER JOIN pg_namespace AS namespace
            ON namespace.nspname = columns.table_schema
        INNER JOIN pg_class AS relation
            ON relation.relnamespace = namespace.oid
            AND relation.relname = columns.table_name
        INNER JOIN pg_attribute AS attributes
            ON attributes.attrelid = relation.oid
            AND attributes.attname = columns.column_name
        WHERE columns.table_schema = ANY(%s)
        ORDER BY table_schema, object_kind DESC, table_name, ordinal_position
        """,
        (list(SCHEMAS), list(SCHEMAS)),
    )
    return _dictionary_rows(cursor)


def _read_constraint_metadata(cursor: Any) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            namespace.nspname AS table_schema,
            relation.relname AS table_name,
            constraint_record.conname AS constraint_name,
            constraint_record.contype AS constraint_type,
            pg_get_constraintdef(constraint_record.oid, true) AS constraint_definition
        FROM pg_constraint AS constraint_record
        INNER JOIN pg_class AS relation
            ON relation.oid = constraint_record.conrelid
        INNER JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = ANY(%s)
        ORDER BY
            namespace.nspname,
            relation.relname,
            constraint_record.contype,
            constraint_record.conname
        """,
        (list(SCHEMAS),),
    )
    return _dictionary_rows(cursor)


def _has_schema_privilege(cursor: Any, schema_name: str, privilege: str) -> bool:
    cursor.execute(
        "SELECT has_schema_privilege(current_user, %s, %s)",
        (schema_name, privilege),
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def _has_table_privilege(cursor: Any, table_name: str, privilege: str) -> bool:
    cursor.execute(
        "SELECT has_table_privilege(current_user, %s, %s)",
        (table_name, privilege),
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def _read_safety_facts(cursor: Any, total_row_count: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT
            current_database(),
            current_user,
            current_setting('default_transaction_read_only'),
            current_setting('transaction_read_only'),
            current_setting('statement_timeout'),
            current_setting('lock_timeout')
        """
    )
    settings = cursor.fetchone()
    if settings is None:
        raise SeedVerificationError("PostgreSQL returned no reader safety settings.")

    table_names = tuple(TABLES)
    return {
        "database": settings[0],
        "user": settings[1],
        "default_transaction_read_only": settings[2],
        "transaction_read_only": settings[3],
        "statement_timeout": settings[4],
        "lock_timeout": settings[5],
        "tracked_schema_count": len(SCHEMAS),
        "tracked_table_count": len(TABLES),
        "total_row_count": total_row_count,
        "schema_usage_count": sum(
            _has_schema_privilege(cursor, schema, "USAGE") for schema in SCHEMAS
        ),
        "schema_create_count": sum(
            _has_schema_privilege(cursor, schema, "CREATE") for schema in SCHEMAS
        ),
        "selectable_table_count": sum(
            _has_table_privilege(cursor, table, "SELECT") for table in table_names
        ),
        "insertable_table_count": sum(
            _has_table_privilege(cursor, table, "INSERT") for table in table_names
        ),
        "updateable_table_count": sum(
            _has_table_privilege(cursor, table, "UPDATE") for table in table_names
        ),
        "deletable_table_count": sum(
            _has_table_privilege(cursor, table, "DELETE") for table in table_names
        ),
        "truncatable_table_count": sum(
            _has_table_privilege(cursor, table, "TRUNCATE") for table in table_names
        ),
    }


def inspect_seed(cursor: Any) -> dict[str, Any]:
    """Read and fingerprint the complete allowlisted synthetic corpus."""

    from psycopg import sql

    tables: dict[str, Any] = {}
    total_row_count = 0
    for qualified_name, spec in TABLES.items():
        schema_name, table_name = qualified_name.split(".", maxsplit=1)
        query = sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in spec.columns),
            sql.Identifier(schema_name),
            sql.Identifier(table_name),
            sql.SQL(", ").join(sql.Identifier(column) for column in spec.order_by),
        )
        cursor.execute(query)
        rows = cursor.fetchall()
        total_row_count += len(rows)
        tables[qualified_name] = {
            "columns": list(spec.columns),
            "order_by": list(spec.order_by),
            "row_count": len(rows),
            "data_sha256": canonical_sha256(rows),
        }

    schema_records = _read_schema_metadata(cursor)
    constraint_records = _read_constraint_metadata(cursor)
    snapshot: dict[str, Any] = {
        "format_version": 1,
        "canonicalization": CANONICALIZATION,
        "schemas": list(SCHEMAS),
        "tables": tables,
        "schema_sha256": canonical_sha256(schema_records),
        "constraints_sha256": canonical_sha256(constraint_records),
        "safety": _read_safety_facts(cursor, total_row_count),
    }
    snapshot["global_sha256"] = snapshot_fingerprint(snapshot)
    return snapshot


def read_seed(database_url: str) -> dict[str, Any]:
    """Inspect the seed in one explicit read-only transaction."""

    import psycopg

    connection = psycopg.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            snapshot = inspect_seed(cursor)
        connection.rollback()
    finally:
        connection.close()
    return snapshot


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the tracked expected snapshot."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SeedVerificationError("The tracked demo seed manifest could not be loaded.") from exc
    if not isinstance(payload, dict):
        raise SeedVerificationError("The tracked demo seed manifest must be a mapping.")
    return payload


def first_difference(expected: Any, actual: Any, path: str = "$") -> str | None:
    """Return the first stable, non-secret difference between two snapshots."""

    if type(expected) is not type(actual):
        return f"{path}: expected {type(expected).__name__}, observed {type(actual).__name__}"
    if isinstance(expected, Mapping):
        expected_keys = set(expected)
        actual_keys = set(actual)
        if expected_keys != actual_keys:
            missing = sorted(expected_keys - actual_keys)
            unexpected = sorted(actual_keys - expected_keys)
            return f"{path}: missing keys {missing}; unexpected keys {unexpected}"
        for key in sorted(expected, key=str):
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: expected {len(expected)} items, observed {len(actual)}"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            difference = first_difference(expected_item, actual_item, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return f"{path}: expected {expected!r}, observed {actual!r}"
    return None


def verify_snapshot(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    """Fail closed when any inventory, hash, count, or reader fact drifts."""

    difference = first_difference(expected, actual)
    if difference is not None:
        raise SeedVerificationError(f"Demo seed verification failed at {difference}.")


def _database_url() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("SCHEMABRIDGE_TEST_DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the exact deterministic SchemaBridge demo seed read-only."
    )
    parser.add_argument("--database-url", default=_database_url())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--emit-actual",
        action="store_true",
        help="Print the observed manifest to stdout without writing any file.",
    )
    output.add_argument(
        "--fingerprint-only",
        action="store_true",
        help="Print only the verified global SHA-256.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        actual = read_seed(args.database_url)
        if args.emit_actual:
            print(yaml.safe_dump(actual, sort_keys=False), end="")
            return
        verify_snapshot(load_manifest(args.manifest), actual)
    except SeedVerificationError as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        raise SystemExit(
            "Demo seed verification could not complete against the configured PostgreSQL reader."
        ) from exc

    if args.fingerprint_only:
        print(actual["global_sha256"])
    else:
        print(
            "Demo seed verified read-only: "
            f"{actual['safety']['tracked_table_count']} tables, "
            f"{actual['safety']['total_row_count']} rows, "
            f"global sha256 {actual['global_sha256']}."
        )


if __name__ == "__main__":
    main()
