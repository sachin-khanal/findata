"""Audit log: one row per request to the daemon (who, what, when, outcome; never data).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table needs no ALTER, so this is safe on SQLite without batch mode.
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("summary", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
