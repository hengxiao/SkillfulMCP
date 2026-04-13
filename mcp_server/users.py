"""User identity service (Wave 9).

Wave 9 drops `users.role` — authority lives on
:class:`AccountMembership`. This module is just the identity layer:
create/read/update users, stamp `last_login_at`, and bootstrap the
table from `MCP_WEBUI_OPERATORS` on first run.

Role-scoped operations (creating memberships, changing roles, last-
admin guard, pending invitations) live in :mod:`mcp_server.accounts`.

The hardcoded superadmin identity (spec §2.3) is never stored here —
it's matched against env vars at login time.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import User

_log = get_logger("mcp.users")


# Reserved pseudo-email for the env-hardcoded superadmin. Normalization
# (.strip().lower()) is applied before comparing, so case/whitespace
# variants are also blocked from registering.
SUPERADMIN_EMAIL = "superadmin@skillfulmcp.com"

# Reserved user id for the superadmin identity. uuid4().hex never
# equals "0" (it's always 32 hex chars), and the users.id CHECK
# enforces this at the DB layer as defense-in-depth.
SUPERADMIN_USER_ID = "0"


def normalize_email(email: str | None) -> str:
    """Canonical email form used everywhere: stripped + lowercased."""
    return (email or "").strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.email).all()


def get_user(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return db.query(User).filter(User.email == normalized).first()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def create_user(
    db: Session,
    *,
    email: str,
    password_hash: str,
    display_name: str | None = None,
) -> User:
    """Insert a new user identity.

    Raises ValueError on:
    - empty email / password_hash (client error)
    - normalized email equal to the reserved superadmin pseudo-email
    - duplicate email (unique constraint violation surfaces here)

    No role parameter — authority lives on memberships, added
    separately via `mcp_server.accounts`.
    """
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("email is required")
    if not password_hash:
        raise ValueError("password_hash is required")
    if normalized == SUPERADMIN_EMAIL:
        raise ValueError(
            f"email {normalized!r} is reserved for the platform superadmin"
        )

    user = User(
        id=uuid.uuid4().hex,
        email=normalized,
        display_name=(display_name or "").strip() or None,
        password_hash=password_hash,
        disabled=False,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"email {normalized!r} already in use")
    db.commit()
    db.refresh(user)
    return user


def update_user(
    db: Session,
    user_id: str,
    *,
    display_name: str | None = None,
    disabled: bool | None = None,
    password_hash: str | None = None,
    last_active_account_id: str | None = None,
) -> User | None:
    """Partial update. None = leave alone. No role field — that's
    a membership-layer concern."""
    u = db.get(User, user_id)
    if not u:
        return None
    if display_name is not None:
        u.display_name = display_name.strip() or None
    if disabled is not None:
        u.disabled = bool(disabled)
    if password_hash is not None:
        u.password_hash = password_hash
    if last_active_account_id is not None:
        # Allow clearing by passing the sentinel empty string.
        u.last_active_account_id = last_active_account_id or None
    db.commit()
    db.refresh(u)
    return u


def delete_user(db: Session, user_id: str) -> bool:
    u = db.get(User, user_id)
    if not u:
        return False
    db.delete(u)
    db.commit()
    return True


def touch_login(db: Session, user_id: str) -> None:
    """Stamp `last_login_at` to now. Best-effort; never blocks login."""
    u = db.get(User, user_id)
    if u is None:
        return
    u.last_login_at = _now()
    db.commit()


# ---------------------------------------------------------------------------
# Bootstrap — env operators seed the table on first run.
# ---------------------------------------------------------------------------

def bootstrap_from_env(db: Session) -> int:
    """Seed the users table from MCP_WEBUI_OPERATORS when it's empty.

    Each entry in the env JSON becomes a plain user identity. Their
    `account-admin` membership in the `default` account (created by
    the 0004 migration on upgrade, or by this function on a fresh DB)
    is added separately — see `mcp_server.accounts.bootstrap_default_account`.

    Returns the number of users created (0 on no-op).
    """
    if db.query(User).first() is not None:
        return 0

    raw = os.environ.get("MCP_WEBUI_OPERATORS", "").strip()
    if not raw:
        _log.info(
            "no users in DB and MCP_WEBUI_OPERATORS is empty; "
            "Web UI will refuse all logins until an admin is created"
        )
        return 0

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.error(
            "MCP_WEBUI_OPERATORS is not valid JSON; skipping bootstrap",
            extra={"error": str(exc)},
        )
        return 0
    if not isinstance(entries, list):
        _log.error("MCP_WEBUI_OPERATORS must be a JSON array")
        return 0

    created = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = normalize_email(entry.get("email"))
        pw_hash = str(entry.get("password_hash", ""))
        if not email or not pw_hash:
            continue
        try:
            create_user(
                db,
                email=email,
                password_hash=pw_hash,
                display_name=entry.get("display_name"),
            )
            created += 1
        except ValueError as exc:
            _log.warning(
                "bootstrap skipped one operator",
                extra={"email": email, "reason": str(exc)},
            )

    if created:
        _log.info(
            "bootstrapped users from env",
            extra={"count": created, "source": "MCP_WEBUI_OPERATORS"},
        )
    return created
