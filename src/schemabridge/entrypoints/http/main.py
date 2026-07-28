"""Independent ASGI process for the authenticated M24 API."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from schemabridge.bootstrap import ApiProcessRuntime, build_api_process_runtime

logger = logging.getLogger(__name__)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def build_http_application(runtime: ApiProcessRuntime | None = None) -> FastAPI:
    """Return the ASGI app composed exclusively by the bootstrap root."""

    return (runtime or build_api_process_runtime()).application


def main() -> None:
    """Serve one API process; Uvicorn owns signal-driven graceful shutdown."""

    import uvicorn

    try:
        runtime = build_api_process_runtime()
        logging.basicConfig(
            level=runtime.log_level.upper(),
            format=_LOG_FORMAT,
        )
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
        )
    except Exception as error:
        logging.basicConfig(level=logging.ERROR, format=_LOG_FORMAT)
        logger.error("api_startup_failed error_type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
