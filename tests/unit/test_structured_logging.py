from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier

import pytest

from schemabridge.adapters.observability.runtime import RuntimeOperationalTelemetry
from schemabridge.adapters.observability.structured_logging import (
    LogOutcome,
    LogSeverity,
    StructuredEvent,
    StructuredEventHandler,
    StructuredEventWriter,
    StructuredLoggingSession,
    UnsafeTelemetryError,
    configure_structured_logging,
    emit_structured_log,
    ensure_structured_logging,
    render_structured_event,
    resolve_runtime_environment,
    resolve_runtime_log_level,
)


def _event(**changes: object) -> StructuredEvent:
    values: dict[str, object] = {
        "occurred_at": datetime(2026, 7, 29, 9, 10, 11, 123000, tzinfo=UTC),
        "severity": LogSeverity.INFO,
        "service": "worker",
        "environment": "local-evidence",
        "event": "job.execution",
        "outcome": LogOutcome.SUCCEEDED,
        "duration_ms": 17,
        "correlation_id": "req_public-42",
        "counts": {"jobs_completed": 1},
    }
    values.update(changes)
    return StructuredEvent(**values)  # type: ignore[arg-type]


def test_structured_event_is_one_line_schema_versioned_json_with_only_public_fields() -> None:
    line = render_structured_event(_event())

    assert "\n" not in line
    assert "\r" not in line
    payload = json.loads(line)
    assert payload == {
        "schema_version": "schemabridge.telemetry.v1",
        "timestamp": "2026-07-29T09:10:11.123Z",
        "severity": "info",
        "service": "worker",
        "environment": "local-evidence",
        "event": "job.execution",
        "outcome": "succeeded",
        "duration_ms": 17,
        "correlation_id": "req_public-42",
        "jobs_completed": 1,
    }


def test_timestamp_is_normalized_to_utc_and_optional_public_fields_are_omitted() -> None:
    local_zone = timezone(timedelta(hours=2))

    payload = json.loads(
        render_structured_event(
            _event(
                occurred_at=datetime(2026, 7, 29, 11, 10, 11, tzinfo=local_zone),
                correlation_id=None,
                counts={},
            )
        )
    )

    assert payload["timestamp"] == "2026-07-29T09:10:11.000Z"
    assert "correlation_id" not in payload
    assert "counts" not in payload


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("service", "tenant-a"),
        ("environment", "prod\nforged"),
        ("event", "raw.sql"),
        ("correlation_id", 'req-1\n{"severity":"critical"}'),
        ("duration_ms", -1),
        ("service", ["worker"]),
        ("occurred_at", "2026-07-29T09:10:11Z"),
    ],
)
def test_hostile_or_unregistered_core_fields_fail_with_a_sanitized_error(
    change: str,
    value: object,
) -> None:
    with pytest.raises(UnsafeTelemetryError) as raised:
        _event(**{change: value})

    assert str(raised.value) == "structured telemetry event rejected"
    assert repr(value) not in str(raised.value)


def test_hostile_count_mapping_failure_is_replaced_with_the_sanitized_error() -> None:
    class _HostileCounts(dict[str, int]):
        def items(self) -> object:
            raise RuntimeError("credential-secret-from-mapping")

    with pytest.raises(UnsafeTelemetryError) as raised:
        _event(counts=_HostileCounts())

    assert str(raised.value) == "structured telemetry event rejected"
    assert "credential-secret-from-mapping" not in str(raised.value)


def test_unknown_operational_count_and_unregistered_error_code_are_rejected() -> None:
    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        _event(counts={"sql_text": 1})

    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        _event(
            outcome=LogOutcome.FAILED,
            error_code="provider said password=hunter2",
            counts={},
        )


def test_failure_requires_a_stable_error_code_and_success_cannot_carry_one() -> None:
    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        _event(outcome=LogOutcome.FAILED, counts={})

    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        _event(error_code="source_timeout")

    failed = _event(
        outcome=LogOutcome.FAILED,
        error_code="source_timeout",
        counts={"jobs_failed": 1},
    )
    payload = json.loads(render_structured_event(failed))
    assert payload["error_code"] == "source_timeout"
    assert "hunter2" not in render_structured_event(failed)


def test_writer_emits_exactly_one_utf8_json_line_per_event() -> None:
    stream = io.StringIO()
    writer = StructuredEventWriter(stream)

    writer.write(_event())
    writer.write(_event(correlation_id="req_public-43"))

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["correlation_id"] for line in lines] == [
        "req_public-42",
        "req_public-43",
    ]


def test_runtime_sink_rejects_cross_capability_or_unknown_events_before_writing() -> None:
    stream = io.StringIO()
    telemetry = RuntimeOperationalTelemetry(
        service="api",
        environment="production",
        stream=stream,
    )

    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        telemetry.emit(
            event="job.execution",
            outcome="succeeded",
            duration_ms=1,
        )
    with pytest.raises(UnsafeTelemetryError, match="structured telemetry event rejected"):
        telemetry.emit(
            event="secret.value",  # type: ignore[arg-type]
            outcome="succeeded",
            duration_ms=1,
        )

    assert stream.getvalue() == ""


def test_only_explicit_source_operation_events_update_source_metrics() -> None:
    telemetry = RuntimeOperationalTelemetry(
        service="worker",
        environment="production",
        stream=io.StringIO(),
    )

    telemetry.emit(
        event="job.execution",
        outcome="succeeded",
        duration_ms=1,
        counts={"jobs_completed": 1},
    )

    assert "schemabridge_source_operations_total{" not in telemetry.render_openmetrics()

    telemetry.emit(
        event="source.operation",
        outcome="failed",
        duration_ms=2,
        error_code="source_timeout",
    )

    assert (
        'schemabridge_source_operations_total{capability="execution",outcome="timeout"} 1.0'
        in telemetry.render_openmetrics()
    )


def test_stdlib_handler_never_formats_message_args_extra_or_exception() -> None:
    stream = io.StringIO()
    handler = StructuredEventHandler(
        service="api",
        environment="production",
        stream=stream,
    )
    record = logging.LogRecord(
        name="hostile.library",
        level=logging.ERROR,
        pathname="postgresql://reader:path-secret@private/source",
        lineno=17,
        msg="Bearer %s\nforged-json",
        args=("token-secret-value",),
        exc_info=None,
    )
    record.password = "credential-secret"  # type: ignore[attr-defined]
    try:
        raise RuntimeError("traceback-source-secret")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
        record.stack_info = "stack-secret"

    handler.emit(record)

    raw = stream.getvalue()
    payload = json.loads(raw)
    assert payload == {
        "schema_version": "schemabridge.telemetry.v1",
        "timestamp": payload["timestamp"],
        "severity": "error",
        "service": "api",
        "environment": "production",
        "event": "runtime.log",
        "outcome": "failed",
        "duration_ms": 0,
        "error_code": "internal_failure",
    }
    assert len(raw.encode("utf-8")) <= 2_049
    for forbidden in (
        "Bearer",
        "token-secret-value",
        "credential-secret",
        "traceback-source-secret",
        "stack-secret",
        "postgresql://",
        "Traceback",
    ):
        assert forbidden not in raw


def test_typed_stdlib_event_keeps_only_allowlisted_public_fields() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("tests.structured-typed-event")

    with configure_structured_logging(
        service="worker",
        environment="production",
        level="INFO",
        stream=stream,
    ):
        emit_structured_log(
            logger,
            service="worker",
            environment="production",
            event="job.execution",
            outcome="failed",
            duration_ms=19,
            correlation_id="job-public-17",
            error_code="source_timeout",
            counts={"jobs_failed": 1, "source_timeouts": 1},
        )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "job.execution"
    assert payload["correlation_id"] == "job-public-17"
    assert payload["jobs_failed"] == 1
    assert payload["source_timeouts"] == 1
    assert set(payload).issubset(
        {
            "schema_version",
            "timestamp",
            "severity",
            "service",
            "environment",
            "event",
            "outcome",
            "duration_ms",
            "correlation_id",
            "error_code",
            "jobs_failed",
            "source_timeouts",
        }
    )


def test_logging_session_restores_existing_handlers_and_neutralizes_uvicorn() -> None:
    root = logging.getLogger()
    previous_handlers = tuple(root.handlers)
    previous_level = root.level
    uvicorn_logger = logging.getLogger("uvicorn.error")
    previous_uvicorn_handlers = tuple(uvicorn_logger.handlers)
    previous_propagate = uvicorn_logger.propagate
    previous_uvicorn_disabled = uvicorn_logger.disabled
    streamlit_logger = logging.getLogger("streamlit.runtime.test-boundary")
    previous_streamlit_handlers = tuple(streamlit_logger.handlers)
    previous_streamlit_propagate = streamlit_logger.propagate
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    uvicorn_sentinel = logging.NullHandler()
    uvicorn_logger.addHandler(uvicorn_sentinel)
    streamlit_sentinel = logging.NullHandler()
    streamlit_logger.addHandler(streamlit_sentinel)
    stream = io.StringIO()

    session = configure_structured_logging(
        service="observer",
        environment="staging",
        level="WARNING",
        stream=stream,
    )
    try:
        assert sentinel not in root.handlers
        assert root.level == logging.WARNING
        assert uvicorn_sentinel not in uvicorn_logger.handlers
        assert uvicorn_logger.propagate is True
        assert streamlit_sentinel not in streamlit_logger.handlers
        assert streamlit_logger.propagate is True
    finally:
        session.close()

    assert tuple(root.handlers) == (*previous_handlers, sentinel)
    assert root.level == previous_level
    assert tuple(uvicorn_logger.handlers) == (*previous_uvicorn_handlers, uvicorn_sentinel)
    assert uvicorn_logger.propagate is previous_propagate
    assert uvicorn_logger.disabled is previous_uvicorn_disabled
    assert tuple(streamlit_logger.handlers) == (
        *previous_streamlit_handlers,
        streamlit_sentinel,
    )
    assert streamlit_logger.propagate is previous_streamlit_propagate
    root.removeHandler(sentinel)
    uvicorn_logger.removeHandler(uvicorn_sentinel)
    streamlit_logger.removeHandler(streamlit_sentinel)


def test_runtime_environment_and_level_never_reuse_hostile_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEMABRIDGE_ENVIRONMENT", "prod\ncredential-secret")
    monkeypatch.setenv("SCHEMABRIDGE_LOG_LEVEL", "TRACE\nforged")

    assert resolve_runtime_environment() == "unknown"
    assert resolve_runtime_log_level() == logging.INFO
    assert resolve_runtime_environment(["production"]) == "unknown"  # type: ignore[arg-type]
    assert resolve_runtime_log_level(object()) == logging.INFO  # type: ignore[arg-type]


def test_concurrent_streamlit_reruns_reuse_exactly_one_process_session() -> None:
    stream = io.StringIO()
    barrier = Barrier(8)

    def configure() -> StructuredLoggingSession:
        barrier.wait()
        return ensure_structured_logging(
            service="web",
            environment="production",
            stream=stream,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        sessions = tuple(executor.map(lambda _index: configure(), range(8)))

    try:
        assert len({id(session) for session in sessions}) == 1
        assert (
            sum(
                isinstance(handler, StructuredEventHandler)
                for handler in logging.getLogger().handlers
            )
            == 1
        )
        emit_structured_log(
            logging.getLogger("schemabridge.entrypoints.streamlit.app"),
            service="web",
            environment="production",
            event="service.health",
            outcome="succeeded",
            duration_ms=0,
        )
        assert len(stream.getvalue().splitlines()) == 1
    finally:
        sessions[0].close()
