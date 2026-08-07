"""Static contracts for additive Query Studio settlement hardening."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/control_plane/0007_harden_ai_usage_settlement.sql"
SERIALIZATION_MIGRATION = (
    ROOT / "migrations/control_plane/0008_serialize_ai_provider_accounting.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _settlement_function(sql: str) -> str:
    return sql.split(
        "CREATE OR REPLACE FUNCTION\n    schemabridge_control.settle_ai_provider_attempt(",
        maxsplit=1,
    )[1].split("REVOKE ALL ON FUNCTION", maxsplit=1)[0]


def test_v7_is_additive_checksumable_and_preflights_without_rewriting_history() -> None:
    sql = _sql()
    preflight = sql.split("ALTER TABLE", maxsplit=1)[0]

    assert MIGRATION.is_file()
    assert len(MIGRATION.read_bytes()) < 1_048_576
    assert (
        hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
        == "5dbc079a995b5fe0373ada20264c20d64c657259e10f77839f1f0805449fbcec"
    )
    assert "LOCK TABLE schemabridge_control.ai_provider_attempt_reservations" in preflight
    assert "LOCK TABLE schemabridge_control.ai_provider_usage_audit" in preflight
    assert "historical AI reservation usage is incompatible with schema v7" in preflight
    assert "historical AI usage audit is incompatible with schema v7" in preflight
    assert "historical AI settlement audit is inconsistent" in preflight
    for mutation in ("UPDATE ", "INSERT ", "DELETE "):
        assert mutation not in preflight.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    assert "CREATE ROLE" not in sql.upper()


def test_v7_constraints_bind_success_to_positive_reserved_usage_and_failures_to_estimate() -> None:
    sql = _sql()

    assert "ADD CONSTRAINT ai_attempt_terminal_usage_accounting_v7" in sql
    assert "observed_input_tokens IS NOT NULL" in sql
    assert "observed_output_tokens IS NOT NULL" in sql
    assert "observed_input_tokens BETWEEN 1 AND estimated_input_tokens" in sql
    assert "observed_output_tokens BETWEEN 1 AND estimated_output_tokens" in sql
    assert "charged_input_tokens = observed_input_tokens" in sql
    assert "charged_output_tokens = observed_output_tokens" in sql
    assert "charged_input_tokens = estimated_input_tokens" in sql
    assert "charged_output_tokens = estimated_output_tokens" in sql
    assert "ADD CONSTRAINT ai_provider_usage_audit_positive_charges_v7" in sql
    assert "input_tokens BETWEEN 1 AND 1000000000" in sql
    assert "output_tokens BETWEEN 1 AND 1000000000" in sql


def test_v7_settlement_preserves_fencing_replay_failure_accounting_and_runtime_grant() -> None:
    sql = _sql()
    settle = _settlement_function(sql)

    assert "p_observed_input_tokens < 1" in settle
    assert "p_observed_output_tokens < 1" in settle
    assert "> reservation_record.estimated_input_tokens" in settle
    assert "> reservation_record.estimated_output_tokens" in settle
    assert "reservation_record.capability_digest" in settle
    assert "reservation_record.fencing_token <> p_fencing_token" in settle
    assert "reservation_record.lease_expires_at <= observed_at" in settle
    assert "reservation_record.settlement_fingerprint" in settle
    assert "'ai_attempt_settlement_v1'" in settle
    assert "charge_input := reservation_record.estimated_input_tokens" in settle
    assert "charge_output := reservation_record.estimated_output_tokens" in settle
    assert "active_attempt_count = active_attempt_count - 1" in settle
    assert "FROM PUBLIC;" in sql
    assert "TO schemabridge_runtime;" in sql
    for role in (
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_catalog",
        "schemabridge_reconciler",
    ):
        assert f"TO {role}" not in sql


def test_v8_preflight_validates_all_deterministic_audit_derivations_without_rewriting() -> None:
    sql = SERIALIZATION_MIGRATION.read_text(encoding="utf-8")
    preflight = sql.split("ALTER FUNCTION", maxsplit=1)[0]

    assert SERIALIZATION_MIGRATION.is_file()
    assert len(SERIALIZATION_MIGRATION.read_bytes()) < 1_048_576
    assert "LOCK TABLE schemabridge_control.ai_provider_attempt_reservations" in preflight
    assert "LOCK TABLE schemabridge_control.ai_provider_usage_audit" in preflight
    assert "audit.audit_id = 'aia_' || encode(" in preflight
    assert "'ai_attempt_settlement_v1'" in preflight
    assert "derived.settlement_fingerprint" in preflight
    assert "reservation.settlement_fingerprint" in preflight
    assert "IS DISTINCT FROM derived.settlement_fingerprint" in preflight
    assert "reservation.reservation_id" in preflight
    assert "reservation.settlement_fingerprint" in preflight
    assert "audit.workspace_scope_digest = encode(" in preflight
    assert "'query_studio_scope_v1|'" in preflight
    assert "audit.retain_until = reservation.settled_at" in preflight
    assert "secs => reservation.audit_retention_seconds" in preflight
    for mutation in ("UPDATE ", "INSERT ", "DELETE "):
        assert mutation not in preflight.upper()


def test_v8_wrappers_refresh_time_and_serialize_state_before_daily_usage() -> None:
    sql = SERIALIZATION_MIGRATION.read_text(encoding="utf-8")
    settle = sql.split(
        "CREATE FUNCTION schemabridge_control.settle_ai_provider_attempt(",
        maxsplit=1,
    )[1].split("ALTER FUNCTION", maxsplit=1)[0]
    reserve = sql.split(
        "CREATE FUNCTION schemabridge_control.reserve_ai_provider_attempt(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.settle_ai_provider_attempt(",
        maxsplit=1,
    )[0]
    expire = sql.split(
        "CREATE FUNCTION schemabridge_control.expire_ai_provider_attempts(",
        maxsplit=1,
    )[1].split(
        "CREATE FUNCTION schemabridge_control.reserve_ai_provider_attempt(",
        maxsplit=1,
    )[0]

    settle_lock = settle.index("FOR UPDATE;")
    settle_clock = settle.index("locked_at := clock_timestamp();")
    settle_ownership = settle.index("AI reservation ownership is stale")
    settle_state = settle.index("ai_provider_admission_state")
    settle_daily = settle.index("ai_provider_daily_usage")
    assert settle_lock < settle_clock < settle_ownership < settle_state < settle_daily
    assert "reservation_record.lease_expires_at <= locked_at" in settle

    reserve_policy = reserve.index("tenant_ai_policies")
    reserve_existing = reserve.index("ai_provider_attempt_reservations")
    reserve_expiry = reserve.index("expire_ai_provider_attempts(")
    reserve_state = reserve.index("ai_provider_admission_state")
    reserve_daily = reserve.index("ai_provider_daily_usage")
    reserve_window = reserve.index("ai_provider_request_windows")
    reserve_core = reserve.index("reserve_ai_provider_attempt_v7_core(")
    assert (
        reserve_policy
        < reserve_existing
        < reserve_expiry
        < reserve_state
        < reserve_daily
        < reserve_window
        < reserve_core
    )
    assert "lock_observed_at := clock_timestamp();" in reserve

    expire_reservations = expire.index("ai_provider_attempt_reservations")
    expire_state = expire.index("ai_provider_admission_state")
    expire_daily = expire.index("ai_provider_daily_usage")
    expire_core = expire.index("expire_ai_provider_attempts_v7_core(")
    assert expire_reservations < expire_state < expire_daily < expire_core
    assert "FOR active_usage_day IN" in expire
    assert "usage.usage_date = active_usage_day" in expire
    assert "reservation.status = 'reserved'" in expire


def test_v8_reasserts_exact_wrapper_acl_and_hides_invoker_cores() -> None:
    sql = SERIALIZATION_MIGRATION.read_text(encoding="utf-8")

    for function in (
        "expire_ai_provider_attempts_v7_core",
        "reserve_ai_provider_attempt_v7_core",
        "settle_ai_provider_attempt_v7_core",
    ):
        assert f"schemabridge_control.{function}(" in sql
    assert sql.count("SECURITY INVOKER;") == 3
    assert sql.count("SECURITY DEFINER") == 3
    assert sql.count("SET search_path = pg_catalog, schemabridge_control") == 3
    assert sql.count("OWNER TO schemabridge_migrator;") == 3
    for role in (
        "PUBLIC",
        "schemabridge_runtime",
        "schemabridge_api",
        "schemabridge_worker",
        "schemabridge_catalog",
        "schemabridge_reconciler",
    ):
        assert role in sql
    assert "TO schemabridge_runtime;" in sql
