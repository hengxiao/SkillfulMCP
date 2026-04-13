"""
Operator auth for the Web UI.

Scope (Wave 6a):
- Password-based local operators, configured via `MCP_WEBUI_OPERATORS`.
- Bcrypt hashes verified with `passlib`.
- Operator identity stored in the signed session cookie.
- CSRF token pattern: per-session token; embedded in forms and in a
  `<meta name="csrf-token">` tag so HTMX can include it as a header.

OIDC and tenant-scoped roles are Wave 6b concerns. The password flow
exercises the full session / CSRF / auth-middleware pipeline; swapping
the credential source for OIDC later is a localized change.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import bcrypt
from fastapi import Request

from .config import Settings, get_settings, parse_operators


SESSION_KEY_OPERATOR = "operator"
SESSION_KEY_CSRF = "csrf_token"


@dataclass(frozen=True)
class Operator:
    email: str


# ---------------------------------------------------------------------------
# Credential verification
# ---------------------------------------------------------------------------

_MAX_PASSWORD_BYTES = 72  # bcrypt's input limit; longer → truncated silently


def _encode_password(plain: str) -> bytes:
    """bcrypt rejects inputs > 72 bytes. Truncate at the byte level so the
    behavior is deterministic — callers must know this cap."""
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def verify_password(plain: str, hashed: str) -> bool:
    """bcrypt-verify. Bad hashes surface as False, not an exception."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(_encode_password(plain), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash (wrong format / truncated). Treat as failed auth.
        return False


def hash_password(plain: str) -> str:
    """Helper for the CLI + tests to generate a bcrypt hash."""
    return bcrypt.hashpw(_encode_password(plain), bcrypt.gensalt()).decode("utf-8")


def authenticate(email: str, password: str, settings: Settings | None = None) -> Operator | None:
    """Return an Operator if (email, password) matches the configured list."""
    settings = settings or get_settings()
    email = (email or "").strip().lower()
    ops = parse_operators(settings.operators_raw)
    pw_hash = ops.get(email)
    if not pw_hash:
        return None
    if not verify_password(password, pw_hash):
        return None
    return Operator(email=email)


# ---------------------------------------------------------------------------
# Session accessors
# ---------------------------------------------------------------------------

def get_session_operator(request: Request) -> Operator | None:
    raw = request.session.get(SESSION_KEY_OPERATOR)
    if not raw or not isinstance(raw, dict):
        return None
    email = raw.get("email")
    if not email:
        return None
    return Operator(email=email)


def set_session_operator(request: Request, op: Operator) -> None:
    request.session[SESSION_KEY_OPERATOR] = {"email": op.email}
    # A new login gets a fresh CSRF token so a leaked pre-login token
    # cannot be replayed after auth.
    request.session[SESSION_KEY_CSRF] = secrets.token_urlsafe(32)


def clear_session(request: Request) -> None:
    request.session.clear()


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def get_csrf_token(request: Request) -> str:
    """Return the session-bound CSRF token, generating one if absent."""
    token = request.session.get(SESSION_KEY_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY_CSRF] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> bool:
    """Constant-time comparison of the submitted token vs the session token."""
    if not submitted:
        return False
    session_token = request.session.get(SESSION_KEY_CSRF)
    if not session_token:
        return False
    return secrets.compare_digest(str(submitted), str(session_token))
