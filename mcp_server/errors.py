"""
Exception handlers that produce a consistent error envelope and always
propagate the X-Request-ID header.

Envelope shape (backwards-compatible with prior prototype responses):

    {
      "detail":     "<human message>",     # preserved — existing clients
      "code":       "HTTP_404",            # new — stable machine-readable
      "request_id": "abcdef..."            # new — matches X-Request-ID header
    }

Existing callers (Web UI, tests) read `detail` as they always did. New
callers can switch to `code` / `request_id`. We can tighten this in a
future `/v1`-prefixed API without breaking the current surface.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .logging_config import get_request_id
from .middleware import HEADER as REQUEST_ID_HEADER

_log = logging.getLogger("mcp.errors")


def _envelope(*, detail: Any, code: str, request_id: str | None) -> dict:
    body: dict[str, Any] = {"detail": detail, "code": code}
    if request_id:
        body["request_id"] = request_id
    return body


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    rid = get_request_id()
    headers = dict(exc.headers or {})
    if rid:
        headers[REQUEST_ID_HEADER] = rid
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(
            detail=exc.detail,
            code=f"HTTP_{exc.status_code}",
            request_id=rid,
        ),
        headers=headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = get_request_id()
    headers = {REQUEST_ID_HEADER: rid} if rid else None
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_envelope(
            # `jsonable_encoder` scrubs non-serializable ctx values (e.g.
            # Pydantic v2 wraps ValueErrors) that would otherwise break the
            # JSONResponse serializer.
            detail=jsonable_encoder(exc.errors()),
            code="VALIDATION_ERROR",
            request_id=rid,
        ),
        headers=headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for programming errors. Do not leak the exception message."""
    rid = get_request_id()
    _log.exception("unhandled exception", extra={"path": request.url.path})
    headers = {REQUEST_ID_HEADER: rid} if rid else None
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            detail="Internal Server Error",
            code="INTERNAL_ERROR",
            request_id=rid,
        ),
        headers=headers,
    )
