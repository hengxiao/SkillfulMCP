"""Audit events table (item H).

Append-only `{actor_email, action, account_id, target_kind,
target_id, diff_json, ts}` for forensics. Indexed on
(account_id, ts DESC) so per-tenant listings are a range scan.

Retention + partitioning live outside this migration — the table
starts as a plain Postgres table. A follow-up migration (post-
Wave-9) can add monthly partitions or move cold rows to cold
storage when volume warrants it. Today's scale doesn't need it.

Revision ID: 0007_audit_events
Revises: 0006_shares
Create Date: 2026-04-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_audit_events"
down_revision: Union[str, None] = "0006_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("actor_email", sa.String(), nullable=True, index=True),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False, index=True),
        # account_id is nullable because platform-level events
        # (superadmin login, create-account) aren't scoped to a
        # specific tenant. Indexed together with ts for per-tenant
        # history range scans.
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("target_kind", sa.String(), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("diff", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_audit_events_account_ts",
        "audit_events",
        ["account_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_account_ts", table_name="audit_events")
    op.drop_table("audit_events")
