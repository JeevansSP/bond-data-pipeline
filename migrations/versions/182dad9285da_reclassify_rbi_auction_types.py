"""reclassify T-Bill cut-off and conversion/switch releases in rbi_auctions

Data repair (no schema change), paired with the connector-side guard. ``rbi_auctions.auction_type``
is derived from the press-release title by keyword, and two title families RBI uses fell through
to ``Other`` — surfaced nightly by the ``unclassified_auction_type`` WARN, which nobody acted on:

1. "91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off" — RBI abbreviates to "T-Bill" in
   result titles where the announcement spells out "Treasury Bills". 10 rows -> ``T-Bill``.
2. "Result: Conversion/Switch Auction of Government of India Securities" — a switch, not an
   issuance; it gets its own ``Switch`` label rather than being folded into ``G-Sec`` so the
   calendar does not overstate primary supply. 2 rows.

``bonds.sources.rbi._classify`` now knows both keywords; this migration applies the same rule to
the rows already loaded. The nightly upsert alone would only have relabelled the ~19 releases
still on RBI's index page.

Revision ID: 182dad9285da
Revises: 5dc049204fa7
Create Date: 2026-09-19 12:02:09.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "182dad9285da"
down_revision: str | None = "5dc049204fa7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE rbi_auctions SET auction_type = 'T-Bill' "
            "WHERE auction_type = 'Other' AND lower(title) LIKE '%t-bill%'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE rbi_auctions SET auction_type = 'Switch' "
            "WHERE auction_type = 'Other' AND lower(title) LIKE '%conversion/switch%'"
        )
    )


def downgrade() -> None:
    # Only rows this migration could have touched: a T-Bill row whose title spells out
    # "Treasury Bill" was classified before it and stays.
    op.execute(
        sa.text("UPDATE rbi_auctions SET auction_type = 'Other' WHERE auction_type = 'Switch'")
    )
    op.execute(
        sa.text(
            "UPDATE rbi_auctions SET auction_type = 'Other' "
            "WHERE auction_type = 'T-Bill' AND lower(title) LIKE '%t-bill%' "
            "AND lower(title) NOT LIKE '%treasury bill%'"
        )
    )
