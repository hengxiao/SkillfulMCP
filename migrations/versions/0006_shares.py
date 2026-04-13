"""Email-based allow lists for skills + skillsets (Wave 9.4).

Per spec §4.3: shares are keyed by raw email (no FK to `users.email`)
so an admin can invite a not-yet-registered address. UNIQUE
`(skill_id, email)` keeps duplicate entries idempotent.

Revision ID: 0006_shares
Revises: 0005_catalog_account
Create Date: 2026-04-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_shares"
down_revision: Union[str, None] = "0005_catalog_account"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _share_table(name: str, parent: str, parent_fk: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            parent_fk,
            sa.String(),
            sa.ForeignKey(f"{parent}.{('pk' if parent == 'skills' else 'id')}",
                          ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(), nullable=False, index=True),
        sa.Column(
            "granted_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(parent_fk, "email",
                            name=f"uq_{name}_parent_email"),
    )


def upgrade() -> None:
    # skill_shares: parent is skills.id (logical id, shared across
    # versions — a share attaches to the skill, not a specific
    # version).
    op.create_table(
        "skill_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("skill_id", sa.String(), nullable=False, index=True),
        sa.Column("email", sa.String(), nullable=False, index=True),
        sa.Column(
            "granted_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("skill_id", "email",
                            name="uq_skill_shares_skill_email"),
    )
    # skillset_shares: parent is skillsets.id.
    op.create_table(
        "skillset_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "skillset_id",
            sa.String(),
            sa.ForeignKey("skillsets.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(), nullable=False, index=True),
        sa.Column(
            "granted_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("skillset_id", "email",
                            name="uq_skillset_shares_skillset_email"),
    )


def downgrade() -> None:
    op.drop_table("skillset_shares")
    op.drop_table("skill_shares")
