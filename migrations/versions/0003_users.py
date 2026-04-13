"""Add users table.

Wave 8b. Replaces the env-only operator list (`MCP_WEBUI_OPERATORS`) as
the source of truth. The env list is still read at startup as a
**bootstrap** when the table is empty, but after that all changes go
through the Web UI.

Revision ID: 0003_users
Revises: 0002_visibility
Create Date: 2026-04-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_users"
down_revision: Union[str, None] = "0002_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),  # uuid4 hex
        sa.Column("email", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),  # 'admin' | 'viewer'
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("users")
