"""
Liveness + readiness probes.

/health     — kept for backwards compatibility (returns 200 if alive).
/livez      — alive (process responding). No dependency checks.
/readyz     — ready to serve traffic. Fails 503 on dependency outage.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..dependencies import get_db

router = APIRouter(tags=["health"])

_log = logging.getLogger("mcp.health")


@router.get("/health")
def health_check():
    """Legacy liveness alias — kept so old probes don't break."""
    return {"status": "ok"}


@router.get("/livez")
def livez():
    """Process is up. No dependency checks."""
    return {"status": "alive"}


@router.get("/.well-known/jwks.json")
def jwks():
    """Public JWK set (Wave 9 item I).

    When asymmetric signing is configured
    (`MCP_JWT_PRIVATE_KEY_PEM` or `*_FILE`), external verifiers
    fetch this endpoint to validate tokens without a shared secret.
    Symmetric-mode deployments return an empty key set — their
    tokens are verified in-process with the same secret.
    """
    from ..auth import get_default_service
    from ..keyring import public_jwks

    ring = get_default_service().keyring
    return public_jwks(ring)


@router.get("/readyz")
def readyz(request: Request, db: Session = Depends(get_db)):
    """
    Ready to serve traffic: DB reachable, settings loaded, JWT secret present.

    Returns 200 with a component breakdown on success, 503 with the same
    shape (plus a failure reason) when anything is unhealthy.
    """
    from ..config import get_settings  # deferred to catch startup-time errors

    components: dict[str, str] = {}
    healthy = True

    # 1. Settings available?
    try:
        settings = get_settings()
        if not settings.jwt_secret:
            components["settings"] = "fail: MCP_JWT_SECRET empty"
            healthy = False
        else:
            components["settings"] = "ok"
    except Exception as exc:
        components["settings"] = f"fail: {exc.__class__.__name__}: {exc}"
        healthy = False

    # 2. DB reachable?
    try:
        db.execute(text("SELECT 1")).scalar_one()
        components["db"] = "ok"
    except Exception as exc:
        components["db"] = f"fail: {exc.__class__.__name__}"
        healthy = False
        _log.error("readyz db check failed", extra={"error": str(exc)})

    body = {
        "status": "ready" if healthy else "not_ready",
        "components": components,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)
