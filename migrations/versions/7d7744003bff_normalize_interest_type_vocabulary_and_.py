"""normalize interest_type vocabulary and repair sovereign reference data

Four data repairs on ``securities`` (no schema change):

1. Collapse 15+ raw ``interest_type`` spellings to the canonical vocabulary
   (FIXED / ZERO / FLOATING / OTHER / NULL) — the same mapping
   ``bonds.models.records.normalize_interest_type`` now enforces on ingest.
2. Backfill ``face_value = 100`` on sovereign par instruments (GSEC/SDL/TBILL/STRIPS)
   where it is NULL — the Rs 100 sovereign face convention. SGB (gold-linked face)
   and CORP are never touched.
3. Repair IN001174P011, a principal STRIP that inherited coupon 7.09 + FIXED from a
   CCIL dated-stock descriptor ("07.09 GOVT. STOCK 25NOV2074P").
4. Reclassify 15 CCIL rows frozen as GSEC by an early parser: 14 T-Bills
   (description mentions DTB/TREASURY) and 1 SGB (description mentions SGB/GOLD).

Revision ID: 7d7744003bff
Revises: e7a2c5d9f1b4
Create Date: 2026-08-02 14:59:49.532705
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d7744003bff"
down_revision: str | None = "e7a2c5d9f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Raw spelling (lowercased, whitespace collapsed) -> canonical value. Mirror of
# bonds.models.records._INTEREST_TYPE_CANONICAL at the time of this migration.
_CANONICAL: dict[str, str] = {
    "fixed": "FIXED",
    "fixed interest": "FIXED",
    "zero": "ZERO",
    "zero_coupon": "ZERO",
    "zero interest": "ZERO",
    "no interest": "ZERO",
    "floating": "FLOATING",
    "variable interest": "FLOATING",
    "variable-others": "FLOATING",
    "variable-index linked": "FLOATING",
    "variable - mibor linked": "FLOATING",
    "variable-inflation": "FLOATING",
    "variable-equity linked": "OTHER",
    "variable- commodity linked": "OTHER",
    "not applicable": "OTHER",
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1) Normalize interest_type. Empty/whitespace-only -> NULL; known spellings -> canonical
    #    bucket; anything else non-null -> OTHER (never fail on new vocabulary).
    conn.execute(
        sa.text(
            "UPDATE securities SET interest_type = NULL "
            "WHERE interest_type IS NOT NULL AND btrim(interest_type) = ''"
        )
    )
    when_clauses = " ".join(f"WHEN :k{i} THEN :v{i}" for i in range(len(_CANONICAL)))
    params: dict[str, str] = {}
    for i, (raw, canonical) in enumerate(_CANONICAL.items()):
        params[f"k{i}"] = raw
        params[f"v{i}"] = canonical
    conn.execute(
        sa.text(
            "UPDATE securities SET interest_type = "
            f"CASE regexp_replace(lower(btrim(interest_type)), '\\s+', ' ', 'g') {when_clauses} "
            "ELSE 'OTHER' END "
            "WHERE interest_type IS NOT NULL"
        ),
        params,
    )

    # 2) Sovereign face-value backfill (Rs 100 par convention; never SGB/CORP).
    conn.execute(
        sa.text(
            "UPDATE securities SET face_value = 100 "
            "WHERE instrument_type IN ('GSEC', 'SDL', 'TBILL', 'STRIPS') AND face_value IS NULL"
        )
    )

    # 3) Hybrid strip repair: a principal STRIP pays no coupon.
    conn.execute(
        sa.text(
            "UPDATE securities SET coupon = 0, interest_type = 'ZERO' WHERE isin = 'IN001174P011'"
        )
    )

    # 4) Reclassify CCIL rows an early parser froze as GSEC. Expected 14 T-Bills and 1 SGB at
    #    the time of writing; drift is reported but not fatal (the repair itself is still right).
    tbills = conn.execute(
        sa.text(
            "UPDATE securities SET instrument_type = 'TBILL', interest_type = 'ZERO', coupon = 0 "
            "WHERE source = 'ccil' AND instrument_type = 'GSEC' "
            "AND description ~* 'DTB|TREASURY'"
        )
    ).rowcount
    # The rows frozen as GSEC never had their maturity parsed (the GSEC path is year-only),
    # but a DTB description carries the full DDMMYYYY date ("182 DTB 03092026") — recover it,
    # or tbill_missing_maturity (every T-Bill must carry an exact maturity) trips.
    conn.execute(
        sa.text(
            "UPDATE securities "
            "SET maturity_date = to_date(substring(description FROM '(\\d{8})'), 'DDMMYYYY') "
            "WHERE instrument_type = 'TBILL' AND maturity_date IS NULL "
            "AND description ~ '\\d{8}'"
        )
    )
    sgbs = conn.execute(
        sa.text(
            "UPDATE securities SET instrument_type = 'SGB', face_value = NULL "
            "WHERE source = 'ccil' AND instrument_type = 'GSEC' "
            "AND description ~* 'SGB|GOLD'"
        )
    ).rowcount
    if tbills != 14 or sgbs != 1:
        print(  # alembic runs on a console; drift is informational, not fatal
            f"7d7744003bff: reclassified {tbills} T-Bill rows (expected 14) "
            f"and {sgbs} SGB rows (expected 1); counts drifted but repairs applied."
        )


def downgrade() -> None:
    """Mostly a documented no-op: these are lossy data repairs.

    - The original interest_type spellings (15+ variants across 5 sources) are destroyed by
      normalization and cannot be reconstructed per-row.
    - The face_value backfill cannot be distinguished from rows that legitimately carried 100
      before this migration.
    - The GSEC->TBILL / GSEC->SGB reclassification cannot be reversed by predicate: the same
      description patterns also match rows that were correctly TBILL/SGB all along.

    The one precisely reversible repair — the IN001174P011 strip, whose prior values are known
    constants — is reversed below.
    """
    op.get_bind().execute(
        sa.text(
            "UPDATE securities SET coupon = 7.09, interest_type = 'FIXED' "
            "WHERE isin = 'IN001174P011'"
        )
    )
