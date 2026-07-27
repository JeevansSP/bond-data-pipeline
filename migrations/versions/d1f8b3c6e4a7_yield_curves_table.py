"""yield_curves table (FBIL Par Yield / GOI ZCYC / SDL ZCYC)

Revision ID: d1f8b3c6e4a7
Revises: c9d4e7a1b2f3
Create Date: 2026-07-27 13:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f8b3c6e4a7"
down_revision: str | None = "c9d4e7a1b2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "yield_curves",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("curve", sa.String(length=24), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("tenor_years", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("ytm_semi_annual", sa.Float(), nullable=True),
        sa.Column("ytm_annualized", sa.Float(), nullable=True),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("tenor_years > 0", name="ck_yield_curve_tenor_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_yield_curves_curve"), "yield_curves", ["curve"], unique=False)
    op.create_index(
        op.f("ix_yield_curves_quote_date"), "yield_curves", ["quote_date"], unique=False
    )
    op.create_index(
        "uq_yield_curves_current",
        "yield_curves",
        ["curve", "quote_date", "tenor_years", "source"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_yield_curves_current", table_name="yield_curves")
    op.drop_index(op.f("ix_yield_curves_quote_date"), table_name="yield_curves")
    op.drop_index(op.f("ix_yield_curves_curve"), table_name="yield_curves")
    op.drop_table("yield_curves")
