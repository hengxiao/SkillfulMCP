"""Add visibility columns to skills + skillsets.

Default 'private' so existing rows stay restricted (backwards compatible
with every grant/agent setup that already exists). Operators flip to
'public' through the Web UI or API.

Revision ID: 0002_visibility
Revises: 0001_initial
Create Date: 2026-04-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_visibility"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `with batch_alter_table` so SQLite's no-ALTER-COLUMN limitation
    # doesn't trip us up; on Postgres alembic emits a plain ALTER.
    with op.batch_alter_table("skills") as batch_op:
        batch_op.add_column(
            sa.Column(
                "visibility",
                sa.String(),
                nullable=False,
                server_default="private",
            )
        )
    with op.batch_alter_table("skillsets") as batch_op:
        batch_op.add_column(
            sa.Column(
                "visibility",
                sa.String(),
                nullable=False,
                server_default="private",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("skills") as batch_op:
        batch_op.drop_column("visibility")
    with op.batch_alter_table("skillsets") as batch_op:
        batch_op.drop_column("visibility")
