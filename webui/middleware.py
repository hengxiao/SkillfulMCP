"""
Web UI middleware + deps: auth redirect and CSRF enforcement.

Architecture:
- Auth is middleware (redirects unauthenticated requests to /login).
- CSRF is a FastAPI **dependency** (not middleware). Reading the request
  body in a BaseHTTPMiddleware prevents downstream handlers from seeing
  it; applying CSRF via Depends(...) sidesteps that by running in
  FastAPI's normal request cycle where `request.form()` is cached.

Each state-changing route in `webui/main.py` declares
`dependencies=[Depends(csrf_required)]`.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .auth import get_session_operator, verify_csrf
from .config import get_settings


# Paths that never require a session. Keep this tight.
_DEFAULT_AUTH_EXEMPT: frozenset[str] = frozenset({
    "/login",
    "/logout",  # harmless to allow unauth'd; it's a no-op
    "/favicon.ico",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a session operator on every non-exempt path."""

    def __init__(
        self,
        app,
        *,
        exempt_paths: Iterable[str] = _DEFAULT_AUTH_EXEMPT,
    ) -> None:
        super().__init__(app)
        self.exempt_paths = frozenset(exempt_paths)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)
        if get_session_operator(request) is not None:
            return await call_next(request)
        # Not logged in. Redirect to /login?next=<path>.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/login?next={quote(target)}",
            status_code=303,
        )


async def csrf_required(request: Request) -> None:
    """FastAPI dependency that fails with 403 on missing/invalid CSRF token.

    Token lookup order:
      1. `X-CSRF-Token` header — used by HTMX (see the global hook in
         base.html) and direct JSON/fetch clients.
      2. `csrf_token` form field — standard POST forms.

    The check is skipped entirely when `csrf_enabled=False` in settings so
    narrow unit tests don't have to thread tokens through every request.

    This is a FastAPI Dependency, NOT middleware, because
    `BaseHTTPMiddleware` consuming the request body breaks downstream
    handlers' ability to read it back. FastAPI's dep-injection layer
    shares the cached `request.form()` with handler Form() params cleanly.
    """
    settings = get_settings()
    if not settings.csrf_enabled:
        return

    submitted: str | None = request.headers.get("x-csrf-token")
    if not submitted:
        ct = request.headers.get("content-type", "")
        if ct.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            try:
                form = await request.form()
                submitted = form.get("csrf_token")  # type: ignore[assignment]
            except Exception:
                submitted = None

    if not verify_csrf(request, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing CSRF token",
            # Preserved for programmatic clients that switch on a code.
            headers={"X-Error-Code": "CSRF_FAILED"},
        )
