"""rbi_auction_results table

Per-security RBI auction outcomes: notified versus accepted amount, cut-off price and yield,
and (T-Bills) the weighted average. ``rbi_auctions`` recorded only that an auction was
announced — title, date and a link — which made it a calendar stub rather than data. This is the
primary-market pricing reference the secondary curve gets judged against.

Keyed on (prid, security): one press release covers every security in that day's auction, from
two G-Sec lines to thirty state loans.

Revision ID: 5dc049204fa7
Revises: c90beb9c7976
Create Date: 2026-09-08 07:53:01.030576
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5dc049204fa7"
down_revision: str | None = "c90beb9c7976"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rbi_auction_results",
        sa.Column("prid", sa.String(length=16), nullable=False),
        sa.Column("security", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("auction_date", sa.Date(), nullable=True),
        sa.Column("auction_type", sa.String(length=24), nullable=True),
        sa.Column("tenor_note", sa.Text(), nullable=True),
        sa.Column("notified_amount_cr", sa.Float(), nullable=True),
        sa.Column("bids_received_count", sa.Integer(), nullable=True),
        sa.Column("bids_received_amount_cr", sa.Float(), nullable=True),
        sa.Column("bids_accepted_count", sa.Integer(), nullable=True),
        sa.Column("bids_accepted_amount_cr", sa.Float(), nullable=True),
        sa.Column("cut_off_price", sa.Float(), nullable=True),
        sa.Column("cut_off_yield", sa.Float(), nullable=True),
        sa.Column("wavg_price", sa.Float(), nullable=True),
        sa.Column("wavg_yield", sa.Float(), nullable=True),
        sa.Column("partial_allotment_pct", sa.Float(), nullable=True),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "notified_amount_cr IS NULL OR notified_amount_cr >= 0",
            name="ck_auction_result_notified_non_negative",
        ),
        sa.PrimaryKeyConstraint("prid", "security"),
    )
    op.create_index(
        op.f("ix_rbi_auction_results_auction_date"),
        "rbi_auction_results",
        ["auction_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_rbi_auction_results_auction_type"),
        "rbi_auction_results",
        ["auction_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rbi_auction_results_auction_type"), table_name="rbi_auction_results")
    op.drop_index(op.f("ix_rbi_auction_results_auction_date"), table_name="rbi_auction_results")
    op.drop_table("rbi_auction_results")
