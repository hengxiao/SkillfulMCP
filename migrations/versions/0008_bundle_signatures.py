"""Skill bundle signatures (item J).

Adds two columns to `skills`:

  bundle_signature     base64-encoded signature of the SHA-256
                       digest of the bundle contents. Produced by
                       the uploader's private Ed25519 key.
  bundle_signature_kid kid that identifies which public key (from
                       MCP_BUNDLE_SIGNING_PUBLIC_KEYS) verifies the
                       signature.

Both nullable — pre-Wave-J rows + unsigned uploads surface as
`verified: false` on the response, not as an error.

Revision ID: 0008_bundle_signatures
Revises: 0007_audit_events
Create Date: 2026-04-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_bundle_signatures"
down_revision: Union[str, None] = "0007_audit_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("skills") as batch_op:
        batch_op.add_column(
            sa.Column("bundle_signature", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("bundle_signature_kid", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("skills") as batch_op:
        batch_op.drop_column("bundle_signature_kid")
        batch_op.drop_column("bundle_signature")
