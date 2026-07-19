"""widen securities.interest_type to 48

Some BondCentral corporate interest_type categories exceed the original 24 chars, which aborted
enrichment batches with StringDataRightTruncation. Widen the column (the model also truncates as a
safety net).

Revision ID: b7e1a2c4d5f6
Revises: e42c25cd4a00
Create Date: 2026-07-19 08:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1a2c4d5f6"
down_revision: str | None = "e42c25cd4a00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The active_securities view (SELECT s.*) depends on the column, so it must be dropped before the
# ALTER and recreated after. This matches src/bonds/storage/schema.py._ACTIVE_SECURITIES_DDL.
_CREATE_VIEW = """
CREATE OR REPLACE VIEW active_securities AS
SELECT s.*
FROM securities s
LEFT JOIN LATERAL (
    SELECT value FROM security_attribute_history h
    WHERE h.isin = s.isin AND h.attribute = 'security_status' AND h.valid_to IS NULL
    LIMIT 1
) st ON true
WHERE (s.maturity_date IS NULL OR s.maturity_date >= CURRENT_DATE)
  AND (st.value IS NULL OR upper(st.value) = 'ACTIVE')
"""


def _resize(from_len: int, to_len: int) -> None:
    op.execute("DROP VIEW IF EXISTS active_securities")
    op.alter_column(
        "securities",
        "interest_type",
        existing_type=sa.String(length=from_len),
        type_=sa.String(length=to_len),
        existing_nullable=True,
    )
    op.execute(_CREATE_VIEW)


def upgrade() -> None:
    _resize(24, 48)


def downgrade() -> None:
    _resize(48, 24)
