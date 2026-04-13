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

from fastapi import Request

from mcp_server.pwhash import hash_password, verify_password  # re-exported

from .config import Settings, get_settings, parse_operators


SESSION_KEY_OPERATOR = "operator"
SESSION_KEY_CSRF = "csrf_token"


@dataclass(frozen=True)
class Operator:
    email: str
    # Wave 8b: role is carried on the session so templates and the
    # `require_role` dep can make authorization decisions without a
    # round-trip to the server on every request.
    role: str = "admin"
    user_id: str | None = None


# Re-exported from mcp_server.pwhash so existing imports keep working.
__all__ = [
    "Operator",
    "hash_password",
    "verify_password",
    "authenticate",
    "authenticate_via_server",
    "get_session_operator",
    "set_session_operator",
    "clear_session",
    "get_csrf_token",
    "verify_csrf",
]


def authenticate(email: str, password: str, settings: Settings | None = None) -> Operator | None:
    """Env-only fallback authenticator.

    Wave 8b moves the source of truth to the `users` table on the
    mcp_server. This function stays for backward-compat with tests and as
    an emergency fallback when the server is unreachable but env
    operators exist. The primary login path is
    `authenticate_via_server` (see `main.login_submit`).
    """
    settings = settings or get_settings()
    email = (email or "").strip().lower()
    ops = parse_operators(settings.operators_raw)
    pw_hash = ops.get(email)
    if not pw_hash:
        return None
    if not verify_password(password, pw_hash):
        return None
    # Env operators are implicitly admins — they predate roles.
    return Operator(email=email, role="admin")


async def authenticate_via_server(email: str, password: str) -> Operator | None:
    """Ask the mcp_server to verify (email, password) against the users table.

    Returns None on bad creds or transport error. Emits a log line on
    transport error so ops can notice if the Web UI is talking to the
    wrong server.
    """
    from .client import MCPClient, MCPError  # local to avoid cycle at import

    try:
        data = await MCPClient().authenticate_user(email, password)
    except MCPError:
        return None
    if not data:
        return None
    return Operator(
        email=data["email"],
        role=data.get("role", "viewer"),
        user_id=data.get("id"),
    )


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
    return Operator(
        email=email,
        role=raw.get("role", "admin"),
        user_id=raw.get("user_id"),
    )


def set_session_operator(request: Request, op: Operator) -> None:
    request.session[SESSION_KEY_OPERATOR] = {
        "email": op.email,
        "role": op.role,
        "user_id": op.user_id,
    }
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
