"""Accounts, account_memberships, pending_memberships + drop users.role.

Wave 9.0. Introduces the tenant model described in
spec/user-management.md:

- `accounts` table (flat list of tenants).
- `account_memberships` (user × account × role join — no hierarchy).
- `pending_memberships` (email invitations that resolve on signup).
- `users.role` is dropped; account-scoped roles live on the membership
  table. Superadmin is hardcoded / env-only (not a DB row).
- `users.last_active_account_id` added so login can land a user in
  their last-used account.
- `users.id != '0'` CHECK reserves id `"0"` for the hardcoded
  superadmin identity (uuid4 hex is 32 chars and never collides).

Migration path for existing Wave 8b rows:
- Every 8b `viewer` becomes a `viewer` membership in a new
  `default` account.
- Every 8b `admin` becomes a sole `account-admin` of a fresh account
  named "{email}'s team". Admins don't silently share a tenant.

Revision ID: 0004_accounts_and_memberships
Revises: 0003_users
Create Date: 2026-04-13
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_accounts_and_memberships"
down_revision: Union[str, None] = "0003_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Create the new tables.                                          #
    # ------------------------------------------------------------------ #
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(), primary_key=True),  # uuid4 hex
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "account_memberships",
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(), nullable=False),  # account-admin|contributor|viewer
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Secondary index for "list members of account X" lookups.
    op.create_index(
        "ix_account_memberships_account_id",
        "account_memberships",
        ["account_id"],
    )

    op.create_table(
        "pending_memberships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(), nullable=False, index=True),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "invited_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "email", "account_id", name="uq_pending_membership_email_account"
        ),
    )

    # ------------------------------------------------------------------ #
    # 2. Add new users columns + the '0' CHECK.                          #
    # ------------------------------------------------------------------ #
    # SQLite can't ALTER TABLE ... ADD COLUMN with a FK, so we use batch.
    # The FK needs an explicit name for alembic's batch mode.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "last_active_account_id",
                sa.String(),
                sa.ForeignKey(
                    "accounts.id",
                    name="fk_users_last_active_account_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )

    # ------------------------------------------------------------------ #
    # 3. Migrate Wave 8b rows (viewers → default, admins → own account). #
    # ------------------------------------------------------------------ #
    bind = op.get_bind()

    existing = bind.execute(
        sa.text("SELECT id, email, role FROM users ORDER BY created_at, id")
    ).fetchall()

    if existing:
        default_account_id = uuid.uuid4().hex
        bind.execute(
            sa.text(
                "INSERT INTO accounts (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :ts, :ts)"
            ),
            {"id": default_account_id, "name": "default", "ts": _now()},
        )

        for row in existing:
            user_id = row[0]
            email = row[1]
            role = row[2]
            if role == "admin":
                # Give this admin their own fresh account.
                acct_id = uuid.uuid4().hex
                bind.execute(
                    sa.text(
                        "INSERT INTO accounts (id, name, created_at, updated_at) "
                        "VALUES (:id, :name, :ts, :ts)"
                    ),
                    {"id": acct_id, "name": f"{email}'s team", "ts": _now()},
                )
                bind.execute(
                    sa.text(
                        "INSERT INTO account_memberships "
                        "(user_id, account_id, role, created_at, updated_at) "
                        "VALUES (:u, :a, 'account-admin', :ts, :ts)"
                    ),
                    {"u": user_id, "a": acct_id, "ts": _now()},
                )
                bind.execute(
                    sa.text(
                        "UPDATE users SET last_active_account_id = :a "
                        "WHERE id = :u"
                    ),
                    {"u": user_id, "a": acct_id},
                )
            elif role == "viewer":
                bind.execute(
                    sa.text(
                        "INSERT INTO account_memberships "
                        "(user_id, account_id, role, created_at, updated_at) "
                        "VALUES (:u, :a, 'viewer', :ts, :ts)"
                    ),
                    {"u": user_id, "a": default_account_id, "ts": _now()},
                )
                bind.execute(
                    sa.text(
                        "UPDATE users SET last_active_account_id = :a "
                        "WHERE id = :u"
                    ),
                    {"u": user_id, "a": default_account_id},
                )
            # Any other role value (unlikely) is left without a membership —
            # those users will get a "you have no memberships" banner on
            # next login and can be re-invited by an admin.

    # ------------------------------------------------------------------ #
    # 4. Drop users.role + add the '0' CHECK.                            #
    # ------------------------------------------------------------------ #
    # batch_alter_table handles both SQLite and PG for these shape changes.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("role")
        batch_op.create_check_constraint(
            "ck_users_id_not_reserved",
            "id != '0'",
        )


def downgrade() -> None:
    # Best-effort reverse: re-add users.role, drop the new tables +
    # columns. Existing memberships are discarded — roles come back
    # as 'admin' for everyone with an account-admin membership and
    # 'viewer' for everyone else. The exact 8b shape isn't recoverable
    # once accounts exist, so downgrade is for development only.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_id_not_reserved", type_="check")
        batch_op.add_column(
            sa.Column("role", sa.String(), nullable=True)
        )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE users SET role = 'admin' WHERE id IN ("
            "  SELECT user_id FROM account_memberships WHERE role = 'account-admin'"
            ")"
        )
    )
    bind.execute(sa.text("UPDATE users SET role = 'viewer' WHERE role IS NULL"))

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("role", nullable=False)
        batch_op.drop_column("last_active_account_id")

    op.drop_table("pending_memberships")
    op.drop_index(
        "ix_account_memberships_account_id",
        table_name="account_memberships",
    )
    op.drop_table("account_memberships")
    op.drop_table("accounts")
