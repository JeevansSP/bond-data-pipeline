"""corporate_trades table (trade-level BSE/NSE RFQ + OTC-reported)

Revision ID: e7a2c5d9f1b4
Revises: d1f8b3c6e4a7
Create Date: 2026-07-27 13:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a2c5d9f1b4"
down_revision: str | None = "d1f8b3c6e4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("trade_time", sa.DateTime(), nullable=True),
        sa.Column("listed", sa.String(length=16), nullable=True),
        sa.Column("deal_type", sa.String(length=32), nullable=True),
        sa.Column("seller_deal_type", sa.String(length=32), nullable=True),
        sa.Column("buyer_deal_type", sa.String(length=32), nullable=True),
        sa.Column("issuer", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("coupon", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("trade_yield", sa.Float(), nullable=True),
        sa.Column("yield_type", sa.String(length=8), nullable=True),
        sa.Column("outside_yield_range", sa.String(length=8), nullable=True),
        sa.Column("put_call_date", sa.Date(), nullable=True),
        sa.Column("trade_value_lakh", sa.Float(), nullable=True),
        sa.Column("settlement_date", sa.Date(), nullable=True),
        sa.Column("settlement_status", sa.String(length=24), nullable=True),
        sa.Column("venue", sa.String(length=16), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("price IS NULL OR price > 0", name="ck_corp_trade_price_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_corporate_trades_isin"), "corporate_trades", ["isin"], unique=False)
    op.create_index(
        op.f("ix_corporate_trades_trade_date"), "corporate_trades", ["trade_date"], unique=False
    )
    op.create_index(
        op.f("ix_corporate_trades_source"), "corporate_trades", ["source"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_corporate_trades_source"), table_name="corporate_trades")
    op.drop_index(op.f("ix_corporate_trades_trade_date"), table_name="corporate_trades")
    op.drop_index(op.f("ix_corporate_trades_isin"), table_name="corporate_trades")
    op.drop_table("corporate_trades")
