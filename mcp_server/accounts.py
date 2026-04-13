"""Accounts + memberships service layer (Wave 9.0).

Owns the tenant model described in spec/user-management.md §2-§3.
This module intentionally has **no HTTP concerns** — handlers in
Wave 9.1 compose on top of it. The split mirrors
:mod:`mcp_server.catalog` vs :mod:`mcp_server.routers.skills`.

Responsibilities
----------------
- CRUD on :class:`Account` rows.
- Membership add / remove / update-role + the last-admin guard that
  enforces "every account has ≥ 1 active account-admin."
- `pending_memberships` invitation CRUD + consumption on signup.
- `bootstrap_default_account` to pair with :func:`users.bootstrap_from_env`.

The last-admin guard uses `SELECT ... FOR UPDATE` inside a
transaction so two concurrent admin-deletes can't both see
`remaining_admins > 0` and both succeed (spec §2.2).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import Account, AccountMembership, PendingMembership, User
from .users import normalize_email

_log = get_logger("mcp.accounts")


VALID_MEMBERSHIP_ROLES: frozenset[str] = frozenset(
    {"account-admin", "contributor", "viewer"}
)


class LastAdminError(Exception):
    """Refused change would leave an account with zero active admins."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in VALID_MEMBERSHIP_ROLES:
        raise ValueError(
            f"role must be one of {sorted(VALID_MEMBERSHIP_ROLES)}; got {role!r}"
        )
    return role


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

def list_accounts(db: Session) -> list[Account]:
    return db.query(Account).order_by(Account.name).all()


def get_account(db: Session, account_id: str) -> Account | None:
    return db.get(Account, account_id)


def get_account_by_name(db: Session, name: str) -> Account | None:
    return db.query(Account).filter(Account.name == name).first()


def create_account(
    db: Session,
    *,
    name: str,
    initial_admin_user_id: str,
) -> Account:
    """Create an account + its first account-admin membership atomically.

    Raises ValueError on:
    - empty name
    - duplicate name (unique constraint)
    - unknown initial_admin_user_id
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("account name is required")

    admin_user = db.get(User, initial_admin_user_id)
    if admin_user is None:
        raise ValueError(
            f"initial admin user {initial_admin_user_id!r} does not exist"
        )

    account = Account(id=uuid.uuid4().hex, name=name)
    db.add(account)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"account name {name!r} already in use")

    membership = AccountMembership(
        user_id=admin_user.id,
        account_id=account.id,
        role="account-admin",
    )
    db.add(membership)

    # Stamp the creator's active account so they land in it on the
    # next request without a re-login.
    admin_user.last_active_account_id = account.id

    db.commit()
    db.refresh(account)
    _log.info(
        "account created",
        extra={
            "account_id": account.id,
            "account_name": account.name,
            "initial_admin_user_id": admin_user.id,
        },
    )
    return account


def delete_account(db: Session, account_id: str) -> bool:
    """Hard-delete an account. Cascades memberships + pending rows via FK.

    Catalog cleanup is the caller's responsibility in Wave 9.1; the
    service-layer primitive here just wipes account + membership rows.
    The HTTP handler layers in the confirm-count interlock + the
    catalog-cascade from spec §3.7.
    """
    a = db.get(Account, account_id)
    if a is None:
        return False
    db.delete(a)
    db.commit()
    _log.info("account deleted", extra={"account_id": account_id})
    return True


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------

def list_memberships(db: Session, account_id: str) -> list[AccountMembership]:
    return (
        db.query(AccountMembership)
        .filter(AccountMembership.account_id == account_id)
        .all()
    )


def list_memberships_for_user(
    db: Session, user_id: str
) -> list[AccountMembership]:
    """Per-request lookup used by the session middleware to resolve
    the caller's membership set. See spec §3.3."""
    return (
        db.query(AccountMembership)
        .filter(AccountMembership.user_id == user_id)
        .all()
    )


def get_membership(
    db: Session, *, account_id: str, user_id: str
) -> AccountMembership | None:
    return db.get(AccountMembership, (user_id, account_id))


def add_membership(
    db: Session,
    *,
    account_id: str,
    user_id: str,
    role: str,
) -> AccountMembership:
    """Insert a membership row. No-op-safe via IntegrityError wrapping."""
    role = _validate_role(role)
    if db.get(Account, account_id) is None:
        raise ValueError(f"account {account_id!r} does not exist")
    if db.get(User, user_id) is None:
        raise ValueError(f"user {user_id!r} does not exist")

    m = AccountMembership(user_id=user_id, account_id=account_id, role=role)
    db.add(m)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(
            f"user {user_id!r} already has a membership in account {account_id!r}"
        )
    db.refresh(m)
    return m


def remove_membership(
    db: Session,
    *,
    account_id: str,
    user_id: str,
) -> bool:
    """Remove a membership. Enforces the last-admin guard under row lock.

    Returns True if a row was deleted, False if the membership didn't
    exist. Raises LastAdminError if removal would strand the account.
    """
    # SELECT ... FOR UPDATE locks the row for the duration of this tx
    # so a second concurrent transaction targeting the same membership
    # blocks instead of racing the count check. Dialects that don't
    # support it (SQLite) silently ignore the lock hint — there, the
    # GIL + single-writer nature of SQLite achieves the same end.
    stmt = (
        select(AccountMembership)
        .where(
            AccountMembership.user_id == user_id,
            AccountMembership.account_id == account_id,
        )
        .with_for_update()
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return False

    if row.role == "account-admin":
        remaining = _count_active_admins(db, account_id, exclude_user_id=user_id)
        if remaining == 0:
            raise LastAdminError(
                "Cannot remove the last account-admin of this account. "
                "Promote another member first, or delete the account."
            )

    db.delete(row)
    db.commit()
    return True


def update_membership_role(
    db: Session,
    *,
    account_id: str,
    user_id: str,
    new_role: str,
) -> AccountMembership:
    """Change a membership's role. Last-admin guard applies on demotions
    away from 'account-admin'. Raises LastAdminError when blocked."""
    new_role = _validate_role(new_role)
    stmt = (
        select(AccountMembership)
        .where(
            AccountMembership.user_id == user_id,
            AccountMembership.account_id == account_id,
        )
        .with_for_update()
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"no membership for user {user_id!r} in account {account_id!r}"
        )
    if row.role == new_role:
        return row

    if row.role == "account-admin" and new_role != "account-admin":
        remaining = _count_active_admins(db, account_id, exclude_user_id=user_id)
        if remaining == 0:
            raise LastAdminError(
                "Cannot demote the last account-admin of this account. "
                "Promote another member first."
            )

    row.role = new_role
    db.commit()
    db.refresh(row)
    return row


def _count_active_admins(
    db: Session, account_id: str, *, exclude_user_id: str | None = None
) -> int:
    """Count active (non-disabled) account-admin memberships in an account.

    Inner-joins to users to filter out disabled accounts — a disabled
    admin doesn't satisfy the last-admin invariant.
    """
    stmt = (
        select(func.count())
        .select_from(AccountMembership)
        .join(User, User.id == AccountMembership.user_id)
        .where(
            AccountMembership.account_id == account_id,
            AccountMembership.role == "account-admin",
            User.disabled.is_(False),
        )
    )
    if exclude_user_id is not None:
        stmt = stmt.where(AccountMembership.user_id != exclude_user_id)
    return int(db.execute(stmt).scalar_one())


# ---------------------------------------------------------------------------
# Pending memberships (invitations for not-yet-registered emails)
# ---------------------------------------------------------------------------

def add_pending_membership(
    db: Session,
    *,
    account_id: str,
    email: str,
    role: str,
    invited_by_user_id: str | None = None,
) -> PendingMembership:
    role = _validate_role(role)
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("email is required")
    if db.get(Account, account_id) is None:
        raise ValueError(f"account {account_id!r} does not exist")

    p = PendingMembership(
        email=normalized,
        account_id=account_id,
        role=role,
        invited_by_user_id=invited_by_user_id,
    )
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(
            f"a pending invitation for {normalized!r} in this account already exists"
        )
    db.refresh(p)
    return p


def list_pending_for_account(
    db: Session, account_id: str
) -> list[PendingMembership]:
    return (
        db.query(PendingMembership)
        .filter(PendingMembership.account_id == account_id)
        .all()
    )


def list_pending_for_email(db: Session, email: str) -> list[PendingMembership]:
    normalized = normalize_email(email)
    if not normalized:
        return []
    return (
        db.query(PendingMembership)
        .filter(PendingMembership.email == normalized)
        .all()
    )


def delete_pending_membership(db: Session, pending_id: int) -> bool:
    p = db.get(PendingMembership, pending_id)
    if p is None:
        return False
    db.delete(p)
    db.commit()
    return True


def consume_pending_for_user(
    db: Session, *, user_id: str, email: str
) -> list[AccountMembership]:
    """Called during /signup. For each pending row matching the new
    user's email, insert the real membership and delete the pending
    row. All in the same transaction. Returns the list of memberships
    created.
    """
    normalized = normalize_email(email)
    if not normalized:
        return []

    rows = (
        db.query(PendingMembership)
        .filter(PendingMembership.email == normalized)
        .all()
    )
    created: list[AccountMembership] = []
    for p in rows:
        m = AccountMembership(
            user_id=user_id,
            account_id=p.account_id,
            role=p.role,
        )
        db.add(m)
        try:
            db.flush()
            created.append(m)
        except IntegrityError:
            # The user already has a membership in this account (e.g.
            # a race). Drop the pending row anyway and move on.
            db.rollback()
        db.delete(p)
    db.commit()
    return created


# ---------------------------------------------------------------------------
# Bootstrap — pair with users.bootstrap_from_env.
# ---------------------------------------------------------------------------

def bootstrap_default_account(db: Session) -> Account | None:
    """Ensure a `default` account exists + attach every role-less user
    as an account-admin membership.

    Intended to run in the catalog lifespan right after
    :func:`users.bootstrap_from_env`. Safe to call repeatedly:

    - If the `accounts` table has any row, this is a no-op (the
      migration or a previous bootstrap already set things up).
    - Otherwise, create a `default` account and insert an
      `account-admin` membership for every user that doesn't yet
      have one.
    """
    if db.query(Account).first() is not None:
        return None

    # No accounts exist — create the default and wire up every user.
    default = Account(id=uuid.uuid4().hex, name="default")
    db.add(default)
    db.flush()

    users = db.query(User).all()
    for u in users:
        db.add(
            AccountMembership(
                user_id=u.id,
                account_id=default.id,
                role="account-admin",
            )
        )
        u.last_active_account_id = default.id

    db.commit()
    db.refresh(default)
    _log.info(
        "bootstrapped default account",
        extra={"account_id": default.id, "member_count": len(users)},
    )
    return default
