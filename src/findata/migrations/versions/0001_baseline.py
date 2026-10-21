"""Baseline: the schema findata shipped with before migrations existed.

Databases created by the old `create_all` already have exactly these tables; `findata.schema`
stamps them as this revision instead of running it.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("institution_id", sa.String(64)),
        sa.Column("institution_name", sa.String(255)),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("sync_cursor", sa.Text()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "item_id", sa.String(64), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("official_name", sa.String(255)),
        sa.Column("mask", sa.String(8)),
        sa.Column("type", sa.String(32)),
        sa.Column("subtype", sa.String(64)),
        sa.Column("current_balance", sa.String(32)),
        sa.Column("available_balance", sa.String(32)),
        sa.Column("iso_currency_code", sa.String(8)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounts_item_id", "accounts", ["item_id"])
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(64),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.String(32), nullable=False),
        sa.Column("iso_currency_code", sa.String(8)),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("authorized_date", sa.Date()),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("merchant_name", sa.String(255)),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("pending_transaction_id", sa.String(64)),
        sa.Column("payment_channel", sa.String(32)),
        sa.Column("category_primary", sa.String(64)),
        sa.Column("category_detailed", sa.String(128)),
        sa.Column("raw_enc", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transactions_account_id", "transactions", ["account_id"])
    op.create_index("ix_transactions_date", "transactions", ["date"])
    op.create_index("ix_transactions_category_primary", "transactions", ["category_primary"])


def downgrade() -> None:
    op.drop_table("transactions")
    op.drop_table("accounts")
    op.drop_table("items")
