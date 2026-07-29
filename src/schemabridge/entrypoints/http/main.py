"""Independent ASGI process for the authenticated M24 API."""

from __future__ import annotations

from fastapi import FastAPI

from schemabridge.bootstrap import (
    ApiProcessRuntime,
    build_api_process_runtime,
    configure_runtime_logging,
)


def build_http_application(runtime: ApiProcessRuntime | None = None) -> FastAPI:
    """Return the ASGI app composed exclusively by the bootstrap root."""

    return (runtime or build_api_process_runtime()).application


def main() -> None:
    """Serve one API process; Uvicorn owns signal-driven graceful shutdown."""

    logging_session = configure_runtime_logging(service="api")
    try:
        import uvicorn

        logging_session.emit(
            event="service.health",
            outcome="started",
            duration_ms=0,
        )
        runtime = build_api_process_runtime()
        logging_session.set_level(runtime.log_level)
        uvicorn.run(
            build_http_application(runtime),
            host=runtime.bind_host,
            port=runtime.port,
            workers=1,
            timeout_graceful_shutdown=runtime.graceful_shutdown_seconds,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            log_config=None,
        )
    except Exception:
        logging_session.set_level("ERROR")
        logging_session.emit(
            event="service.health",
            outcome="failed",
            duration_ms=0,
            error_code="internal_failure",
        )
        raise SystemExit(1) from None
    finally:
        logging_session.close()


if __name__ == "__main__":
    main()
