"""repair sdl issuers, state UDAY misclassification and corporate-trade vocabularies

Four data repairs (no schema change), each with the ingest-side guard that stops it recurring:

1. Collapse the ``securities.issuer`` strings for SDLs from 63 spellings to the canonical 31.
   FBIL wrote the raw two-letter code from the description while CCIL normalised to full state
   names, so the same issuer arrived twice ("State Government (AP)" and
   "State Government (ANDHRA PRADESH)"), and FBIL printed two codes for three states
   (MG/ML Meghalaya, NG/NL Nagaland, PD/PY Puducherry — confirmed as one issuer each by SDL
   ISIN prefix IN24/IN26/IN38). Both connectors now derive the issuer through
   ``bonds.states.sdl_issuer``, and ``dq assess`` fails on any issuer outside the closed
   vocabulary. An earlier repair fixed the rows without unifying the two code paths, so the
   next nightly load re-introduced the codes.

2. Reclassify state paper mis-typed as central. FBIL's pre-Feb-2023 combined workbooks carry
   state UDAY and SDL-SPL bonds on the G-Sec workbook's "Special" sheet, whose title maps to
   GSEC; 233 securities landed in the central bucket. Sovereign ISINs are structural (central
   IN00…, state IN10…-IN49…), so the ISIN now outranks the sheet title in the parser.

3. The same correction on ``valuations`` (470 current rows, all on 2021-03-16). Corrected in
   place rather than superseded: FBIL never restated these prices, our parser mislabelled them,
   and a supersede would assert a restatement that did not happen.

4. Normalise the ``corporate_trades`` categoricals. NSE and BSE publish the same categories in
   different casings and abbreviations, so ``deal_type`` held DIRECT/Direct/BROKERED/Brokered/
   IST/SELECT, ``listed`` held Listed/LISTED/Unlisted/UNLISTED and ``settlement_status`` held
   Settled/SETTLED/Pending/PENDING. ``CorporateTradeRecord`` now canonicalises all five columns
   at the model boundary.

Revision ID: f3a91c7d2e58
Revises: 7d7744003bff
Create Date: 2026-09-07 23:35:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a91c7d2e58"
down_revision: str | None = "7d7744003bff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Two-letter-code issuer -> canonical issuer. Mirror of bonds.states.STATE_ALIASES applied to
# every SDL issuer string present at the time of this migration; verified unambiguous (no code
# maps to two different states).
_SDL_ISSUER_REPAIR: dict[str, str] = {
    "State Government (AP)": "State Government (ANDHRA PRADESH)",
    "State Government (AR)": "State Government (ARUNACHAL PRADESH)",
    "State Government (AS)": "State Government (ASSAM)",
    "State Government (BR)": "State Government (BIHAR)",
    "State Government (CG)": "State Government (CHHATTISGARH)",
    "State Government (DL)": "State Government (DELHI)",
    "State Government (GA)": "State Government (GOA)",
    "State Government (GJ)": "State Government (GUJARAT)",
    "State Government (HP)": "State Government (HIMACHAL PRADESH)",
    "State Government (HR)": "State Government (HARYANA)",
    "State Government (JH)": "State Government (JHARKHAND)",
    "State Government (JK)": "State Government (JAMMU & KASHMIR)",
    "State Government (KA)": "State Government (KARNATAKA)",
    "State Government (KL)": "State Government (KERALA)",
    "State Government (MG)": "State Government (MEGHALAYA)",
    "State Government (MH)": "State Government (MAHARASHTRA)",
    "State Government (ML)": "State Government (MEGHALAYA)",
    "State Government (MN)": "State Government (MANIPUR)",
    "State Government (MP)": "State Government (MADHYA PRADESH)",
    "State Government (MZ)": "State Government (MIZORAM)",
    "State Government (NG)": "State Government (NAGALAND)",
    "State Government (NL)": "State Government (NAGALAND)",
    "State Government (OD)": "State Government (ODISHA)",
    "State Government (PD)": "State Government (PUDUCHERRY)",
    "State Government (PN)": "State Government (PUNJAB)",
    "State Government (PY)": "State Government (PUDUCHERRY)",
    "State Government (RJ)": "State Government (RAJASTHAN)",
    "State Government (SK)": "State Government (SIKKIM)",
    "State Government (TN)": "State Government (TAMIL NADU)",
    "State Government (TR)": "State Government (TRIPURA)",
    "State Government (TS)": "State Government (TELANGANA)",
    "State Government (UK)": "State Government (UTTARAKHAND)",
    "State Government (UP)": "State Government (UTTAR PRADESH)",
    "State Government (WB)": "State Government (WEST BENGAL)",
}

# Two-letter code as printed in an FBIL description -> canonical issuer, for the state paper
# reclassified out of the GSEC bucket in step 2 (descriptions read "07.67 TN UDAY 2023").
_CODE_TO_ISSUER: dict[str, str] = {
    key[len("State Government (") : -1]: value for key, value in _SDL_ISSUER_REPAIR.items()
}

# State sovereign ISIN form; mirror of bonds.states._STATE_SOVEREIGN_ISIN_RE.
_STATE_ISIN = "^IN[1-4][0-9]"

# Raw value (lowercased, whitespace collapsed) -> canonical, mirroring the vocabularies in
# bonds.models.records at the time of this migration.
_DEAL_TYPE: dict[str, str] = {
    "direct": "DIRECT",
    "brokered": "BROKERED",
    "ist": "INTER_SCHEME_TRANSFER",
    "inter scheme transfer": "INTER_SCHEME_TRANSFER",
    "buyback": "BUYBACK",
    "buy back": "BUYBACK",
}
_LISTED: dict[str, str] = {
    "listed": "LISTED",
    "unlisted": "UNLISTED",
    "y": "LISTED",
    "n": "UNLISTED",
}
_SETTLEMENT_STATUS: dict[str, str] = {
    "settled": "SETTLED",
    "not settled": "NOT_SETTLED",
    "unsettled": "NOT_SETTLED",
    "pending": "PENDING",
    "rejected": "REJECTED",
}


def _canonical_case(column: str, vocabulary: dict[str, str], prefix: str) -> tuple[str, dict]:
    """Build a CASE expression mapping ``column`` onto ``vocabulary``, plus its bind params.

    Mirrors ``bonds.models.records._canonicalize``: case-insensitive, whitespace collapsed,
    empty -> NULL, unknown non-empty -> OTHER.
    """
    key = f"lower(btrim(regexp_replace({column}, '\\s+', ' ', 'g')))"
    params: dict[str, str] = {}
    cases = []
    for i, (raw, canonical) in enumerate(sorted(vocabulary.items())):
        cases.append(f"WHEN {key} = :{prefix}k{i} THEN :{prefix}v{i}")
        params[f"{prefix}k{i}"] = raw
        params[f"{prefix}v{i}"] = canonical
    expression = (
        f"CASE WHEN {column} IS NULL THEN NULL "
        f"WHEN {key} = '' THEN NULL " + " ".join(cases) + " ELSE 'OTHER' END"
    )
    return expression, params


def _canonicalize_corporate_trades() -> None:
    """Rewrite all five ``corporate_trades`` categoricals in a single pass.

    One UPDATE rather than five: each of the 4.9M rows is rewritten once instead of five times,
    which is the difference between a job that finishes in a couple of minutes and one that
    holds a write transaction open for a quarter of an hour.
    """
    columns = (
        ("deal_type", _DEAL_TYPE),
        ("seller_deal_type", _DEAL_TYPE),
        ("buyer_deal_type", _DEAL_TYPE),
        ("listed", _LISTED),
        ("settlement_status", _SETTLEMENT_STATUS),
    )
    assignments: list[str] = []
    predicates: list[str] = []
    params: dict[str, str] = {}
    for column, vocabulary in columns:
        expression, column_params = _canonical_case(column, vocabulary, f"{column}_")
        assignments.append(f"{column} = {expression}")
        predicates.append(f"{column} IS DISTINCT FROM ({expression})")
        params |= column_params
    op.get_bind().execute(
        sa.text(
            "UPDATE corporate_trades SET "
            + ", ".join(assignments)
            + " WHERE "
            + " OR ".join(predicates)
        ),
        params,
    )


def upgrade() -> None:
    conn = op.get_bind()

    # 1) Collapse two-letter-code SDL issuers onto the canonical full state names.
    for raw, canonical in _SDL_ISSUER_REPAIR.items():
        conn.execute(
            sa.text(
                "UPDATE securities SET issuer = :canonical "
                "WHERE instrument_type = 'SDL' AND issuer = :raw"
            ),
            {"canonical": canonical, "raw": raw},
        )

    # 2) State paper (UDAY / SDL-SPL) mis-typed as central by the worksheet title: retype to SDL
    #    and set the issuer from the state code in the description. The issuer update runs first,
    #    while the rows are still selectable by their central instrument_type; it overwrites the
    #    wrong "Government of India" these rows inherited from the sheet's central classification.
    for code, issuer in _CODE_TO_ISSUER.items():
        conn.execute(
            sa.text(
                f"""
                UPDATE securities SET issuer = :issuer
                 WHERE instrument_type IN ('GSEC', 'TBILL', 'STRIPS', 'SGB')
                   AND isin ~ '{_STATE_ISIN}'
                   AND description ~ ('^[0-9.]+ ' || :code || ' ')
                """
            ),
            {"issuer": issuer, "code": code},
        )
    conn.execute(
        sa.text(
            f"""
            UPDATE securities SET instrument_type = 'SDL'
             WHERE instrument_type IN ('GSEC', 'TBILL', 'STRIPS', 'SGB')
               AND isin ~ '{_STATE_ISIN}'
            """
        )
    )
    # The retype above keys on the ISIN, which is wider than the description-based issuer fix:
    # a state-ISIN row whose description carries a full state name, "SDL SPL" wording, or no
    # description at all is retyped to SDL while still holding the "Government of India" it
    # inherited from the sheet's central classification. That combination is precisely what the
    # new ERROR-level sdl_issuer_noncanonical check rejects, so it would make `bonds dq assess`
    # exit 1 the moment this migration ran. An unknown issuer is a NULL — surfaced by the
    # completeness check, and never a wrong central attribution.
    conn.execute(
        sa.text(
            """
            UPDATE securities SET issuer = NULL
             WHERE instrument_type = 'SDL' AND issuer = 'Government of India'
            """
        )
    )

    # 3) The same correction on the price history (in place — this was our parse error, not an
    #    FBIL restatement, so it must not create a superseded "previous belief").
    conn.execute(
        sa.text(
            f"""
            UPDATE valuations SET instrument_type = 'SDL'
             WHERE instrument_type IN ('GSEC', 'TBILL', 'STRIPS', 'SGB')
               AND isin ~ '{_STATE_ISIN}'
            """
        )
    )

    # 4) Canonicalise the corporate-trade categoricals (one pass over the table).
    _canonicalize_corporate_trades()


def downgrade() -> None:
    # Not reversible: the pre-repair state was several spellings collapsing onto one canonical
    # value (and a mislabelled instrument type), so the original strings cannot be recovered
    # from the repaired rows. Re-ingesting from the data lake rebuilds either state.
    pass
