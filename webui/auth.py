"""
Operator auth for the Web UI.

History:
- Wave 6a — password-based local operators, configured via
  `MCP_WEBUI_OPERATORS` env (JSON), bcrypt hashes, signed session
  cookies + per-session CSRF token embedded in forms and the
  `<meta name="csrf-token">` tag so HTMX can include it as a header.
- Wave 8b — moved the source of truth to the mcp_server `users`
  table. `authenticate_via_server` is now the primary path;
  `authenticate` stays as an env-only fallback for disaster recovery.
- Wave 9.0 — `users.role` dropped from the identity layer (account-
  scoped roles live on `account_memberships`). Session still carries
  a `role` field for the Wave-8b-style admin UI gates, always
  `"admin"` for logged-in users; `is_superadmin` is the new marker
  for platform-level authority (env-hardcoded superadmin at
  `superadmin@skillfulmcp.com`).

OIDC remains a Wave 6b concern; swapping the credential source later
is a localized change to `authenticate_via_server`.
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
    # Wave 8b kept `role` on the session for template gates. Wave 9
    # drops `users.role` entirely — authority lives on account
    # memberships (resolved per-request). Until Wave 9.5 reworks the
    # Web UI around that model, `role` stays here so existing
    # templates continue to render; it's always `"admin"` for any
    # logged-in user because the old admin/viewer split is gone.
    role: str = "admin"
    user_id: str | None = None
    # Wave 9: marks the env-hardcoded platform superadmin
    # (superadmin@skillfulmcp.com). Templates that want to show
    # platform-admin-only UI should gate on this, not on `role`.
    is_superadmin: bool = False


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
    # Wave 9 drops the platform role, but the env fallback still
    # logs people in as admins of the existing UI surface.
    return Operator(email=email, role="admin", is_superadmin=False)


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
        # Wave 9: the mcp_server authenticate response no longer
        # carries a platform role; every logged-in user is effectively
        # an admin for the existing UI surface. is_superadmin carries
        # the platform-level marker.
        role="admin",
        user_id=data.get("id"),
        is_superadmin=bool(data.get("is_superadmin", False)),
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
        is_superadmin=bool(raw.get("is_superadmin", False)),
    )


def set_session_operator(request: Request, op: Operator) -> None:
    request.session[SESSION_KEY_OPERATOR] = {
        "email": op.email,
        "role": op.role,
        "user_id": op.user_id,
        "is_superadmin": op.is_superadmin,
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
