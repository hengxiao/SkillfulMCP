"""
Request-scoped middleware.

Currently: a request-id middleware that
  1. reads X-Request-ID from the incoming request (if present),
  2. generates a UUID4 hex otherwise,
  3. stores it in a ContextVar so log records pick it up automatically,
  4. echoes it back on the response as X-Request-ID,
  5. emits one access log line per request with method, path, status,
     and latency.

Future middleware (auth, tenant, rate-limit) will layer on top.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import get_logger, set_request_id

_log = get_logger("mcp.access")

HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = request.headers.get(HEADER) or uuid.uuid4().hex
        set_request_id(rid)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Let FastAPI's exception handler produce the response; we still
            # want to log the failure with a latency + request id.
            latency_ms = (time.perf_counter() - start) * 1000
            _log.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            set_request_id(None)
            raise

        latency_ms = (time.perf_counter() - start) * 1000
        response.headers[HEADER] = rid
        _log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round(latency_ms, 2),
            },
        )
        set_request_id(None)
        return response
