"""
JWT issuance and validation, with key-ring rotation and a revocation list.

Public surface:
    - `TokenService` — owns a keyring + revocation list. Primary API.
    - Module-level `issue_token(agent, expires_in)` and
      `validate_token(token)` — thin shims that delegate to a lazily-built
      default service. Preserved for `tests/test_auth.py` and any other
      direct callers.
    - `get_default_service()` — exposes the singleton so callers that need
      to revoke a token (the admin endpoint) reach the same instance.
    - `reset_default_service()` — test helper; `conftest.py` calls this
      between tests so revocation state doesn't leak.

Tokens carry `kid` in the header (picked by `KeyRing.active_kid`) and
`jti` in the claims (fresh UUID4 per mint). Validation:
    1. Read `kid` from header, pick secret from keyring. Reject if unknown.
    2. Verify signature + `exp` + issuer.
    3. Reject if `jti` is on the revocation list.

`expires_in` is clamped server-side to `settings.max_token_lifetime_seconds`
so a compromised admin key can't mint a 10-year token.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from jose import JWTError, jwt

from .config import Settings, get_settings
from .keyring import KeyRing, build_keyring
from .logging_config import get_logger
from .models import Agent
from .revocation import RevocationList

_log = get_logger("mcp.auth")


class TokenService:
    def __init__(
        self,
        keyring: KeyRing,
        revocation: RevocationList,
        *,
        issuer: str,
        max_lifetime_seconds: int,
    ) -> None:
        self.keyring = keyring
        self.revocation = revocation
        self.issuer = issuer
        self.max_lifetime_seconds = max_lifetime_seconds

    # ------------------------------------------------------------------
    # Issuance
    # ------------------------------------------------------------------

    def issue_token(self, agent: Agent, expires_in: int = 3600) -> str:
        capped = max(1, min(int(expires_in), self.max_lifetime_seconds))
        if capped != expires_in:
            _log.info(
                "token expires_in clamped",
                extra={
                    "agent_id": agent.id,
                    "requested": int(expires_in),
                    "granted": capped,
                    "max": self.max_lifetime_seconds,
                },
            )
        now = datetime.now(timezone.utc)
        claims: dict[str, Any] = {
            "sub": agent.id,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=capped)).timestamp()),
            "jti": uuid.uuid4().hex,
            "skillsets": agent.skillsets or [],
            "skills": agent.skills or [],
            "scope": agent.scope or [],
        }
        return jwt.encode(
            claims,
            self.keyring.active_secret,
            algorithm=self.keyring.algorithm,
            headers={"kid": self.keyring.active_kid},
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_token(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise _unauthorized(f"Malformed token: {exc}")

        kid = header.get("kid", "primary")
        secret = self.keyring.get_secret(kid)
        if secret is None:
            raise _unauthorized(f"Unknown signing key kid={kid!r}")

        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=[self.keyring.algorithm],
                options={"verify_exp": True},
            )
        except JWTError as exc:
            raise _unauthorized(f"Invalid or expired token: {exc}")

        if claims.get("iss") != self.issuer:
            raise _unauthorized("Token issuer mismatch")

        jti = claims.get("jti")
        if jti and self.revocation.is_revoked(jti):
            raise _unauthorized("Token has been revoked")

        return claims


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Default-service singleton (module-level, lazily built from settings)
# ---------------------------------------------------------------------------

_default_service: TokenService | None = None


def get_default_service() -> TokenService:
    """Return the process-wide default TokenService.

    The keyring is built from Settings once; the revocation list is a fresh
    RevocationList owned by this service. Tests call `reset_default_service`
    between cases so revocation state doesn't leak.
    """
    global _default_service
    if _default_service is None:
        settings: Settings = get_settings()
        keyring = build_keyring(settings)
        revocation = RevocationList()
        _default_service = TokenService(
            keyring=keyring,
            revocation=revocation,
            issuer=settings.jwt_issuer,
            max_lifetime_seconds=settings.max_token_lifetime_seconds,
        )
    return _default_service


def reset_default_service() -> None:
    """Drop the cached default service (pytest fixture helper)."""
    global _default_service
    _default_service = None


# ---------------------------------------------------------------------------
# Module-level shims preserved for backwards compatibility
# ---------------------------------------------------------------------------

def issue_token(agent: Agent, expires_in: int = 3600) -> str:
    return get_default_service().issue_token(agent, expires_in=expires_in)


def validate_token(token: str) -> dict[str, Any]:
    return get_default_service().validate_token(token)
