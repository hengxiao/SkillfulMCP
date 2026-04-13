"""
User service for Web UI operator accounts.

CRUD + bootstrap. Authentication itself stays in `webui/auth.py`; this
module is just the persistence layer.

Roles in Wave 8b:
- `admin`  — full privileges (manage users, catalog, agents, tokens).
- `viewer` — read-only UI. No mutating routes rendered.

`editor` (productization §3.1) is intentionally deferred until the
operator org needs the split.
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


VALID_ROLES = frozenset({"admin", "viewer"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_users(db: Session) -> list[User]:
    return db.query(User).order_by(User.email).all()


def get_user(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    if not email:
        return None
    return (
        db.query(User)
        .filter(User.email == email.strip().lower())
        .first()
    )


def create_user(
    db: Session,
    *,
    email: str,
    password_hash: str,
    role: str,
    display_name: str | None = None,
) -> User:
    """Insert a new user. Raises ValueError on duplicate email or bad role."""
    role = (role or "").strip().lower()
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}; got {role!r}")
    user = User(
        id=uuid.uuid4().hex,
        email=email.strip().lower(),
        display_name=(display_name or "").strip() or None,
        password_hash=password_hash,
        role=role,
        disabled=False,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"email {email!r} already in use")
    db.commit()
    db.refresh(user)
    return user


def update_user(
    db: Session,
    user_id: str,
    *,
    display_name: str | None = None,
    role: str | None = None,
    disabled: bool | None = None,
    password_hash: str | None = None,
) -> User | None:
    """Partial update. None means "leave alone"."""
    u = db.get(User, user_id)
    if not u:
        return None
    if role is not None:
        role = role.strip().lower()
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        u.role = role
    if display_name is not None:
        u.display_name = display_name.strip() or None
    if disabled is not None:
        u.disabled = bool(disabled)
    if password_hash is not None:
        u.password_hash = password_hash
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
    """Stamp `last_login_at` to now. Best-effort — failures don't block login."""
    u = db.get(User, user_id)
    if u is None:
        return
    u.last_login_at = _now()
    db.commit()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def bootstrap_from_env(db: Session) -> int:
    """Seed the users table from MCP_WEBUI_OPERATORS when it's empty.

    Every entry in the env JSON is upserted as an `admin` user. Runs at
    startup; once the table has any row, this function is a no-op so
    deleting an env operator doesn't accidentally lock anyone out.

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
        _log.error("MCP_WEBUI_OPERATORS is not valid JSON; skipping bootstrap",
                   extra={"error": str(exc)})
        return 0
    if not isinstance(entries, list):
        _log.error("MCP_WEBUI_OPERATORS must be a JSON array")
        return 0

    created = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = str(entry.get("email", "")).strip().lower()
        pw_hash = str(entry.get("password_hash", ""))
        if not email or not pw_hash:
            continue
        try:
            create_user(
                db,
                email=email,
                password_hash=pw_hash,
                role="admin",
                display_name=entry.get("display_name"),
            )
            created += 1
        except ValueError as exc:
            _log.warning("bootstrap skipped one operator",
                         extra={"email": email, "reason": str(exc)})

    if created:
        _log.info("bootstrapped users from env",
                  extra={"count": created, "source": "MCP_WEBUI_OPERATORS"})
    return created
