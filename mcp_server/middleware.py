"""
Request-scoped middleware.

Current stack (outermost last — Starlette applies them in the order they are
added, so the order below is also the order they run on the way in):

  RequestIDMiddleware          tag + access log
  RequestSizeLimitMiddleware   413 on oversize bodies (before handler)
  RateLimitMiddleware          429 per-IP token bucket

Future middleware (auth, tenant) will layer on top.
"""

from __future__ import annotations

import time
import uuid
from typing import Iterable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import get_logger, get_request_id, set_request_id
from .ratelimit import TokenBucket

_log = get_logger("mcp.access")
_limit_log = get_logger("mcp.ratelimit")

HEADER = "X-Request-ID"

# Paths that never consume rate-limit tokens. Probes and the legacy health
# alias must always respond so a load balancer doesn't take pods out of
# rotation on hitting a throttle.
_DEFAULT_RATE_LIMIT_EXEMPT: frozenset[str] = frozenset({
    "/livez", "/readyz", "/health",
})


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Request-body size cap
# ---------------------------------------------------------------------------

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose `Content-Length` exceeds `max_bytes`.

    Sits above per-endpoint caps (the bundle upload endpoint does its own
    100 MB check on the parsed archive). `max_bytes <= 0` disables this
    middleware entirely.

    Caveat: chunked transfers have no `Content-Length` header. This
    middleware does NOT read the body stream to enforce a cap on such
    requests; the reverse proxy / ingress should handle that. Documented
    as a known gap in spec/productization.md §3.3.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = int(max_bytes)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if self.max_bytes <= 0:
            return await call_next(request)
        raw = request.headers.get("content-length")
        if raw:
            try:
                n = int(raw)
            except ValueError:
                n = -1
            if n > self.max_bytes:
                return _error_response(
                    status=413,
                    detail=(
                        f"Request body {n} bytes exceeds limit of "
                        f"{self.max_bytes} bytes"
                    ),
                    code="REQUEST_TOO_LARGE",
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Rate limit (in-process token bucket, per IP)
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limit. See ratelimit.py for the bucket semantics."""

    def __init__(
        self,
        app,
        *,
        limiter: TokenBucket,
        exempt_paths: Iterable[str] = _DEFAULT_RATE_LIMIT_EXEMPT,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter
        self.exempt_paths = frozenset(exempt_paths)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self.limiter.enabled or request.url.path in self.exempt_paths:
            return await call_next(request)

        key = _client_key(request)
        allowed, retry_after = self.limiter.allow(key)
        if not allowed:
            _limit_log.info(
                "rate limit hit",
                extra={
                    "client": key,
                    "path": request.url.path,
                    "retry_after": round(retry_after, 3),
                },
            )
            return _error_response(
                status=429,
                detail="Rate limit exceeded",
                code="RATE_LIMIT_EXCEEDED",
                headers={"Retry-After": f"{retry_after:.1f}"},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_key(request: Request) -> str:
    """Resolve the client key for rate-limiting.

    Uses request.client.host. Proxy-aware resolution (X-Forwarded-For)
    is deferred until we have a `MCP_TRUST_PROXY_HEADERS` knob — blindly
    trusting X-Forwarded-For lets anyone spoof the key.
    """
    if request.client:
        return request.client.host
    return "unknown"


def _error_response(
    *, status: int, detail: str, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    rid = get_request_id()
    body: dict = {"detail": detail, "code": code}
    if rid:
        body["request_id"] = rid
    resp_headers = dict(headers or {})
    if rid:
        resp_headers[HEADER] = rid
    return JSONResponse(status_code=status, content=body, headers=resp_headers)
