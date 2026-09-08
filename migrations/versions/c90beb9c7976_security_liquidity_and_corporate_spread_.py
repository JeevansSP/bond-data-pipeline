"""security_liquidity and corporate_spread_matrix tables

Two derived product tables. Both are recomputed per date rather than appended to, so neither is
bitemporal: they are a *view of* the tapes at an as-of date, and the tapes are already the
system of record for what was believed when.

    security_liquidity       Per-ISIN traded-liquidity metrics as at a business date: last
                             print, prints and distinct trading days over 1/3/12 months,
                             turnover, plus the two regulatory verdicts that need them — the
                             15-day traded-price cap (clause 78(1)(i)(c)) and the fair-value
                             active-market test (clause 4(1)).
    corporate_spread_matrix  Daily rating x tenor spread grid computed from actual prints, on
                             FIMMDA's published tenor buckets so the two are comparable cell by
                             cell. FIMMDA polls down to AA- and freezes below it; this measures.

Revision ID: c90beb9c7976
Revises: f3a91c7d2e58
Create Date: 2026-09-08 07:42:39.885347
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c90beb9c7976"
down_revision: str | None = "f3a91c7d2e58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_spread_matrix",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("tenor_bucket", sa.Float(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("isin_count", sa.Integer(), nullable=False),
        sa.Column("notional_lakh", sa.Float(), nullable=True),
        sa.Column("wavg_yield_pct", sa.Float(), nullable=False),
        sa.Column("median_yield_pct", sa.Float(), nullable=True),
        sa.Column("cg_yield_pct", sa.Float(), nullable=False),
        sa.Column("spread_bp", sa.Float(), nullable=False),
        sa.Column("lookback_days", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("tenor_bucket > 0", name="ck_corp_spread_tenor_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_date", "rating", "tenor_bucket", name="uq_corp_spread_cell"),
    )
    op.create_index(
        op.f("ix_corporate_spread_matrix_quote_date"),
        "corporate_spread_matrix",
        ["quote_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_corporate_spread_matrix_rating"),
        "corporate_spread_matrix",
        ["rating"],
        unique=False,
    )
    op.create_table(
        "security_liquidity",
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("instrument_type", sa.String(length=12), nullable=False),
        sa.Column("last_trade_date", sa.Date(), nullable=True),
        sa.Column("days_since_trade", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("last_yield", sa.Float(), nullable=True),
        sa.Column("prints_1m", sa.Integer(), nullable=False),
        sa.Column("prints_3m", sa.Integer(), nullable=False),
        sa.Column("prints_12m", sa.Integer(), nullable=False),
        sa.Column("days_traded_1m", sa.Integer(), nullable=False),
        sa.Column("days_traded_3m", sa.Integer(), nullable=False),
        sa.Column("days_traded_12m", sa.Integer(), nullable=False),
        sa.Column("turnover_1m", sa.Float(), nullable=True),
        sa.Column("turnover_3m", sa.Float(), nullable=True),
        sa.Column("turnover_12m", sa.Float(), nullable=True),
        sa.Column("traded_within_15d", sa.Boolean(), nullable=False),
        sa.Column("active_market", sa.Boolean(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "days_since_trade IS NULL OR days_since_trade >= 0",
            name="ck_liquidity_days_since_trade",
        ),
        sa.PrimaryKeyConstraint("isin", "as_of_date"),
    )
    op.create_index(
        "ix_security_liquidity_as_of_active",
        "security_liquidity",
        ["as_of_date", "active_market"],
        unique=False,
    )
    op.create_index(
        op.f("ix_security_liquidity_as_of_date"), "security_liquidity", ["as_of_date"], unique=False
    )
    op.create_index(
        op.f("ix_security_liquidity_instrument_type"),
        "security_liquidity",
        ["instrument_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_security_liquidity_instrument_type"), table_name="security_liquidity")
    op.drop_index(op.f("ix_security_liquidity_as_of_date"), table_name="security_liquidity")
    op.drop_index("ix_security_liquidity_as_of_active", table_name="security_liquidity")
    op.drop_table("security_liquidity")
    op.drop_index(op.f("ix_corporate_spread_matrix_rating"), table_name="corporate_spread_matrix")
    op.drop_index(
        op.f("ix_corporate_spread_matrix_quote_date"), table_name="corporate_spread_matrix"
    )
    op.drop_table("corporate_spread_matrix")
