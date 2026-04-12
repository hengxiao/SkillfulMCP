"""Initial schema — skills, skill_files, skillsets, skill_skillsets, agents.

Matches mcp_server/models.py as of this revision.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-12

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("pk", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Column is named "metadata" in SQL; the ORM attribute is metadata_
        # because SQLAlchemy already uses `metadata` on the declarative base.
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "version", name="uq_skill_id_version"),
    )

    op.create_table(
        "skillsets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "skill_skillsets",
        sa.Column("skill_id", sa.String(), primary_key=True),
        sa.Column(
            "skillset_id",
            sa.String(),
            sa.ForeignKey("skillsets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "skill_files",
        sa.Column(
            "skill_pk",
            sa.Integer(),
            sa.ForeignKey("skills.pk", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.String(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("skillsets", sa.JSON(), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agents")
    op.drop_table("skill_files")
    op.drop_table("skill_skillsets")
    op.drop_table("skillsets")
    op.drop_table("skills")
