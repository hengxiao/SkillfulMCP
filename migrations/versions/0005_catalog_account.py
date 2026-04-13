"""Catalog account-scoping + ownership (Wave 9.2).

Adds three new columns to `skills`, `skillsets`, and `agents`:

- `account_id`          — tenant boundary. NOT NULL.
  FK: accounts.id ON DELETE RESTRICT (handler-level cascade in §3.7).
- `owner_user_id`       — creator pointer. Nullable so a deleted
  owner doesn't require cascading the catalog row.
  FK: users.id ON DELETE SET NULL.
- `owner_email_snapshot`— denorm for "owned by deleted-user@..."
  display after a null-out.

Migration strategy (no pre-Wave-9 production to preserve per spec
§4.1): add columns nullable, backfill every existing row to the
`default` account created by the Wave 9.0 bootstrap / migration,
then flip to NOT NULL. If no `default` account exists (fresh
deployment — the 9.0 bootstrap didn't find any users to migrate),
create one on the fly in this migration so the NOT NULL flip is
always satisfied.

Also extends the `visibility` CHECK set conceptually: the new tier
`"account"` joins `"public"` and `"private"`. The column is
free-form TEXT so no DB-side enum change is required — the schema
validator at the Pydantic layer enforces the set.

Revision ID: 0005_catalog_account
Revises: 0004_accounts_and_memberships
Create Date: 2026-04-13
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_catalog_account"
down_revision: Union[str, None] = "0004_accounts_and_memberships"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_default_account(bind) -> str:
    """Return the id of an account named `default`, creating one if
    the Wave 9.0 bootstrap didn't already make one (fresh deploy,
    empty users table)."""
    existing = bind.execute(
        sa.text("SELECT id FROM accounts WHERE name = 'default'")
    ).fetchone()
    if existing:
        return existing[0]
    new_id = uuid.uuid4().hex
    bind.execute(
        sa.text(
            "INSERT INTO accounts (id, name, created_at, updated_at) "
            "VALUES (:id, 'default', :ts, :ts)"
        ),
        {"id": new_id, "ts": _now()},
    )
    return new_id


def _backfill(bind, table: str, account_id: str) -> None:
    """Set `account_id` on every existing row in `table`. No-op when
    the table is empty."""
    bind.execute(
        sa.text(
            f"UPDATE {table} SET account_id = :aid WHERE account_id IS NULL"
        ),
        {"aid": account_id},
    )


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Guarantee a default account to backfill against.
    default_account_id = _ensure_default_account(bind)

    # 2. skills — add nullable columns, backfill, flip to NOT NULL.
    with op.batch_alter_table("skills") as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey(
                    "accounts.id",
                    name="fk_skills_account_id",
                    ondelete="RESTRICT",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(),
                sa.ForeignKey(
                    "users.id",
                    name="fk_skills_owner_user_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("owner_email_snapshot", sa.String(), nullable=True)
        )
    _backfill(bind, "skills", default_account_id)
    with op.batch_alter_table("skills") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.String(), nullable=False)
    op.create_index("ix_skills_account_id", "skills", ["account_id"])
    op.create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"])

    # 3. skillsets — same pattern.
    with op.batch_alter_table("skillsets") as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey(
                    "accounts.id",
                    name="fk_skillsets_account_id",
                    ondelete="RESTRICT",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(),
                sa.ForeignKey(
                    "users.id",
                    name="fk_skillsets_owner_user_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("owner_email_snapshot", sa.String(), nullable=True)
        )
    _backfill(bind, "skillsets", default_account_id)
    with op.batch_alter_table("skillsets") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.String(), nullable=False)
    op.create_index("ix_skillsets_account_id", "skillsets", ["account_id"])
    op.create_index("ix_skillsets_owner_user_id", "skillsets", ["owner_user_id"])

    # 4. agents — same pattern. (owner_email_snapshot is less useful
    #    for agents but kept for schema symmetry.)
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey(
                    "accounts.id",
                    name="fk_agents_account_id",
                    ondelete="RESTRICT",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(),
                sa.ForeignKey(
                    "users.id",
                    name="fk_agents_owner_user_id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("owner_email_snapshot", sa.String(), nullable=True)
        )
    _backfill(bind, "agents", default_account_id)
    with op.batch_alter_table("agents") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.String(), nullable=False)
    op.create_index("ix_agents_account_id", "agents", ["account_id"])
    op.create_index("ix_agents_owner_user_id", "agents", ["owner_user_id"])


def downgrade() -> None:
    for table in ("agents", "skillsets", "skills"):
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.drop_index(f"ix_{table}_account_id", table_name=table)
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("owner_email_snapshot")
            batch_op.drop_column("owner_user_id")
            batch_op.drop_column("account_id")
