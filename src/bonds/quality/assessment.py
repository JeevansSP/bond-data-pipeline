"""Database-wide data-quality assessment (cross-table, cross-source).

Complements the per-batch :mod:`bonds.quality.checks` (which run during each ingest) with checks
that only make sense over the whole warehouse: duplicate keys, referential integrity between the
trade/valuation series and the securities master, field completeness, and cross-source price
reconciliation (CCIL traded prices vs FBIL end-of-day marks). Exposed via ``bonds dq assess``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from bonds.pipelines.spread_matrix import MIN_TRADES_PER_CELL
from bonds.quality.checks import Level, QualityCheck
from bonds.states import SDL_ISSUERS
from bonds.storage import Database
from bonds.valuation import TRADED_PRICE_CAP_DAYS

# --- thresholds (tunable) -------------------------------------------------------------------
MAX_VALUATION_NULL_PRICE_RATE = 0.05
MAX_TRADE_NULL_PRICE_RATE = 0.02
MAX_CROSS_SOURCE_P99_PRICE_DIFF = 3.0  # |CCIL WAP - FBIL price| per 100 face, 99th pctile
MIN_CROSS_SOURCE_PAIRS = 500  # too few matched pairs -> reconciliation is uninformative
# Fewest tenor points a published curve day can legitimately carry (sdl_zcyc's grid is 56;
# gsec 160-205). Below this the workbook parsed only partially.
MIN_CURVE_POINTS_PER_DAY = 20
# How far a daily series may lag the newest date anywhere in the warehouse before it is stale.
# Measured against the warehouse high-water mark rather than today, so a machine that was off
# for a week doesn't light up every series — only a series that fell behind its siblings does.
# 8 days clears a weekend plus a holiday cluster; the CCIL/BSE outages that motivated this ran
# to 21 days.
MAX_SERIES_LAG_DAYS = 8
# Window for the empty-successful-day check (below).
EMPTY_SUCCESS_WINDOW_DAYS = 60
_SOVEREIGN = ("GSEC", "SDL", "TBILL", "STRIPS")
# Canonical interest_type vocabulary (NULL allowed); anything else is a raw source string a
# new/regressed source wrote past the normalizer.
CANONICAL_INTEREST_TYPES = ("FIXED", "ZERO", "FLOATING", "OTHER")


@dataclass(frozen=True, slots=True)
class AssessmentReport:
    """Grouped results of a full assessment (``dimension -> checks``)."""

    groups: dict[str, list[QualityCheck]] = field(default_factory=dict)

    @property
    def checks(self) -> list[QualityCheck]:
        """Every check across all dimensions, flattened."""
        return [c for group in self.groups.values() for c in group]

    @property
    def has_error(self) -> bool:
        """Whether any ERROR-level check failed."""
        return any(c.level is Level.ERROR and not c.passed for c in self.checks)

    @property
    def has_warning(self) -> bool:
        """Whether any WARN-level check failed."""
        return any(c.level is Level.WARN and not c.passed for c in self.checks)


def _scalar(conn: Connection, sql: str, **params: object) -> float:
    result = conn.execute(text(sql), params).scalar()
    return float(result) if result is not None else 0.0


def check_uniqueness(conn: Connection) -> list[QualityCheck]:
    """Duplicate primary keys must not exist in any series or the master."""
    specs = [
        (
            "trades",
            "SELECT count(*) FROM (SELECT 1 FROM trades GROUP BY isin,trade_date,source,"
            "segment HAVING count(*)>1) x",
        ),
        (
            # Superseded rows are legitimate history; only the current version must be unique.
            "valuations",
            "SELECT count(*) FROM (SELECT 1 FROM valuations WHERE superseded_at IS NULL "
            "GROUP BY isin,quote_date,source HAVING count(*)>1) x",
        ),
        (
            "securities",
            "SELECT count(*) FROM (SELECT 1 FROM securities GROUP BY isin HAVING count(*)>1) x",
        ),
        (
            "yield_curves",
            "SELECT count(*) FROM (SELECT 1 FROM yield_curves WHERE superseded_at IS NULL "
            "GROUP BY curve,quote_date,tenor_years,source HAVING count(*)>1) x",
        ),
    ]
    checks = [
        QualityCheck(
            f"dup_key_{name}", Level.ERROR, passed=(n := _scalar(conn, sql)) == 0, observed=n
        )
        for name, sql in specs
    ]
    # An attribute must have exactly one value in force at any instant. Nothing enforced this:
    # SCD-2 correctness lived entirely in the writer. It matters beyond tidiness because the
    # spread matrix joins prints to "the rating in force on the quote date" — two overlapping
    # windows would silently duplicate every print in that ISIN's cell and skew the weighted
    # yield, with no error anywhere.
    overlapping = _scalar(
        conn,
        """
        SELECT count(*) FROM security_attribute_history a
          JOIN security_attribute_history b
            ON b.isin = a.isin AND b.attribute = a.attribute AND b.id <> a.id
           AND a.valid_from < coalesce(b.valid_to, DATE '9999-12-31')
           AND b.valid_from < coalesce(a.valid_to, DATE '9999-12-31')
        """,
    )
    checks.append(
        QualityCheck(
            "attribute_history_overlap",
            Level.ERROR,
            passed=overlapping == 0,
            observed=overlapping,
            detail="SCD-2 rows whose validity windows overlap for the same (isin, attribute)",
        )
    )
    # corporate_trades has no natural transaction key, and content-based duplicate detection
    # false-positives on real market structure (a sliced block order prints hundreds of
    # identical lots — verified against raw files). The honest invariant is audit
    # reconciliation: the table's row count per loaded window must equal what the most
    # recent successful load recorded in ingestion_runs. BSE loads one day per run; NSE
    # loads anchored 7-day windows keyed by the window's end date, where several runs can
    # cover one window as its clamped end advances — only the latest run per window counts.
    checks.append(
        _audit_check(
            conn,
            "corp_trades_bse_audit_drift",
            """
            SELECT count(*) FROM (
              SELECT r.run_date
              FROM ingestion_runs r
              LEFT JOIN (
                SELECT trade_date, count(*) AS n FROM corporate_trades
                WHERE source='bse' GROUP BY trade_date
              ) t ON t.trade_date = r.run_date
              WHERE r.dataset='bse.corp_trades' AND r.status='success'
                AND coalesce(t.n, 0) <> r.rows_ingested
            ) x
            """,
            detail="BSE days whose table rows differ from the audited load count",
        )
    )
    checks.append(
        _audit_check(
            conn,
            "corp_trades_nse_audit_drift",
            """
            WITH latest AS (
              SELECT DISTINCT ON (run_date - ((run_date - DATE '2010-01-04') % 7))
                     run_date - ((run_date - DATE '2010-01-04') % 7) AS win_start,
                     run_date, rows_ingested
              FROM ingestion_runs
              WHERE dataset='nse.corp_trades_ts' AND status='success'
              ORDER BY run_date - ((run_date - DATE '2010-01-04') % 7), run_date DESC
            )
            SELECT count(*) FROM latest l
            LEFT JOIN LATERAL (
              SELECT count(*) AS n FROM corporate_trades
              WHERE source='nse' AND trade_date BETWEEN l.win_start AND l.run_date
            ) t ON true
            WHERE coalesce(t.n, 0) <> l.rows_ingested
            """,
            detail="NSE windows whose table rows differ from the audited load count",
        )
    )
    return checks


def _audit_check(conn: Connection, name: str, sql: str, *, detail: str) -> QualityCheck:
    n = _scalar(conn, sql)
    return QualityCheck(name, Level.ERROR, passed=n == 0, observed=n, detail=detail)


def check_referential_integrity(conn: Connection) -> list[QualityCheck]:
    """Trade/valuation ISINs should resolve to a row in the securities master."""
    sov_orphans = _scalar(
        conn,
        "SELECT count(distinct t.isin) FROM trades t LEFT JOIN securities s ON s.isin=t.isin "
        "WHERE s.isin IS NULL AND t.segment = ANY(:segs)",
        segs=list(_SOVEREIGN),
    )
    corp_orphans = _scalar(
        conn,
        "SELECT count(distinct t.isin) FROM trades t LEFT JOIN securities s ON s.isin=t.isin "
        "WHERE s.isin IS NULL AND NOT (t.segment = ANY(:segs))",
        segs=list(_SOVEREIGN),
    )
    val_orphans = _scalar(
        conn,
        "SELECT count(distinct v.isin) FROM valuations v LEFT JOIN securities s ON s.isin=v.isin "
        "WHERE s.isin IS NULL AND v.superseded_at IS NULL",
    )
    trade_level_orphans = _scalar(
        conn,
        "SELECT count(distinct t.isin) FROM corporate_trades t "
        "LEFT JOIN securities s ON s.isin=t.isin WHERE s.isin IS NULL",
    )
    return [
        QualityCheck(
            "orphan_sovereign_trades",
            Level.ERROR,
            passed=sov_orphans == 0,
            observed=sov_orphans,
            detail="sovereign trade ISINs missing from securities",
        ),
        QualityCheck(
            "orphan_valuations",
            Level.ERROR,
            passed=val_orphans == 0,
            observed=val_orphans,
            detail="valuation ISINs missing from securities",
        ),
        QualityCheck(
            "orphan_corp_trades",
            Level.WARN,
            passed=corp_orphans == 0,
            observed=corp_orphans,
            detail="corporate trade ISINs missing from securities",
        ),
        QualityCheck(
            "orphan_corp_trades_trade_level",
            Level.WARN,
            passed=trade_level_orphans == 0,
            observed=trade_level_orphans,
            detail=(
                "trade-level ISINs missing from securities (mostly the pre-2019 matured "
                "universe no reference source covers)"
            ),
        ),
    ]


def check_completeness(conn: Connection) -> list[QualityCheck]:
    """Null-rate thresholds on fields that should be populated."""
    checks: list[QualityCheck] = []
    val_total = _scalar(conn, "SELECT count(*) FROM valuations WHERE superseded_at IS NULL")
    if val_total:
        null_px = _scalar(
            conn,
            "SELECT count(*) FROM valuations WHERE superseded_at IS NULL AND price IS NULL",
        )
        rate = null_px / val_total
        checks.append(
            QualityCheck(
                "valuation_null_price_rate",
                Level.WARN,
                passed=rate <= MAX_VALUATION_NULL_PRICE_RATE,
                observed=rate,
            )
        )
    trade_total = _scalar(conn, "SELECT count(*) FROM trades WHERE source='ccil'")
    if trade_total:
        null_ltp = _scalar(conn, "SELECT count(*) FROM trades WHERE source='ccil' AND ltp IS NULL")
        rate = null_ltp / trade_total
        checks.append(
            QualityCheck(
                "trade_null_ltp_rate",
                Level.WARN,
                passed=rate <= MAX_TRADE_NULL_PRICE_RATE,
                observed=rate,
            )
        )
    ct_total = _scalar(conn, "SELECT count(*) FROM corporate_trades")
    if ct_total:
        null_px = _scalar(conn, "SELECT count(*) FROM corporate_trades WHERE price IS NULL")
        rate = null_px / ct_total
        checks.append(
            QualityCheck(
                "corp_trade_null_price_rate",
                Level.WARN,
                passed=rate <= MAX_TRADE_NULL_PRICE_RATE,
                observed=rate,
            )
        )
    curve_total = _scalar(conn, "SELECT count(*) FROM yield_curves WHERE superseded_at IS NULL")
    if curve_total:
        null_y = _scalar(
            conn,
            "SELECT count(*) FROM yield_curves "
            "WHERE superseded_at IS NULL AND ytm_semi_annual IS NULL AND ytm_annualized IS NULL",
        )
        checks.append(
            QualityCheck(
                "curve_points_without_yield",
                Level.ERROR,
                passed=null_y == 0,
                observed=null_y,
                detail="curve tenor points carrying no yield in either convention",
            )
        )
    # T-Bills and STRIPS carry an exact maturity in the feed -> every one should parse.
    for seg in ("TBILL", "STRIPS"):
        missing = _scalar(
            conn,
            "SELECT count(*) FROM securities WHERE instrument_type=:s AND maturity_date IS NULL",
            s=seg,
        )
        checks.append(
            QualityCheck(
                f"{seg.lower()}_missing_maturity",
                Level.ERROR,
                passed=missing == 0,
                observed=missing,
            )
        )
    # Sovereign paper has a uniform ₹100 face; NULL means a derivation path forgot to set it.
    # SGB is deliberately excluded — its face is the gold-linked issue price.
    sov_no_face = _scalar(
        conn,
        "SELECT count(*) FROM securities WHERE instrument_type = ANY(:segs) AND face_value IS NULL",
        segs=list(_SOVEREIGN),
    )
    checks.append(
        QualityCheck(
            "sovereign_missing_face_value",
            Level.WARN,
            passed=sov_no_face == 0,
            observed=sov_no_face,
            detail="GSEC/SDL/TBILL/STRIPS rows without the uniform 100 face value",
        )
    )
    return checks


def check_consistency(conn: Connection) -> list[QualityCheck]:
    """Internal contradictions and staleness signals in the securities master."""
    zero_coupon_conflict = _scalar(
        conn,
        "SELECT count(*) FROM securities "
        "WHERE upper(interest_type) LIKE '%ZERO%' AND coupon IS NOT NULL AND coupon > 0",
    )
    # STRIPS are zero-coupon by construction; a positive coupon is a parse/merge defect
    # (seen once: a principal strip inheriting its parent bond's coupon from a CCIL descriptor).
    strips_with_coupon = _scalar(
        conn,
        "SELECT count(*) FROM securities WHERE instrument_type='STRIPS' AND coupon > 0",
    )
    noncanonical_interest = _scalar(
        conn,
        "SELECT count(*) FROM securities "
        "WHERE interest_type IS NOT NULL AND NOT (interest_type = ANY(:canon))",
        canon=list(CANONICAL_INTEREST_TYPES),
    )
    # Implausible sovereign yields in CCIL trades: the parse-time repair guard nulls these now,
    # so anything remaining is old data or a new garbling pattern. Scoped to CCIL only — NSE
    # corporate (distressed) paper can legitimately print yields far above 40, and a permanently
    # red WARN on legit data trains users to ignore warnings. Bounds match the repair guard
    # (capital-indexed bonds have printed genuine negatives down to ~-17).
    implausible_yields = _scalar(
        conn,
        "SELECT count(*) FROM trades "
        "WHERE source='ccil' AND lty IS NOT NULL AND (lty < -20 OR lty > 40)",
    )
    matured_active = _scalar(
        conn,
        "SELECT count(*) FROM securities s "
        "JOIN security_attribute_history h ON h.isin=s.isin "
        "  AND h.attribute='security_status' AND h.valid_to IS NULL AND h.value='ACTIVE' "
        "WHERE s.maturity_date < CURRENT_DATE - INTERVAL '365 days'",
    )
    coupon_above_25 = _scalar(conn, "SELECT count(*) FROM securities WHERE coupon > 25")
    # The SDL issuer vocabulary is closed (bonds.states.SDL_ISSUERS). It fragmented once
    # already — FBIL wrote two-letter codes while CCIL wrote full state names, giving 63 issuer
    # strings for 31 states — and a one-off SQL repair was undone by the next nightly load. This
    # check is what makes that regression fail the same night instead of six weeks later.
    noncanonical_sdl_issuer = _scalar(
        conn,
        "SELECT count(*) FROM securities "
        "WHERE instrument_type='SDL' AND issuer IS NOT NULL AND NOT (issuer = ANY(:canon))",
        canon=sorted(SDL_ISSUERS),
    )
    # Sovereign ISINs are structural: central paper is IN00…, state paper IN10…-IN49…. A
    # central-typed row with a state ISIN means a worksheet title outranked the ISIN (FBIL's
    # pre-2023 combined workbooks put state UDAY bonds on the G-Sec "Special" sheet).
    sovereign_isin_mismatch = _scalar(
        conn,
        "SELECT count(*) FROM securities "
        "WHERE instrument_type IN ('GSEC','TBILL','STRIPS','SGB') AND isin NOT LIKE 'IN00%'",
    )
    # A curve day with a handful of tenor points is a partially-parsed workbook, not a
    # published curve — full grids run 56 (sdl_zcyc) to ~205 (gsec) points.
    sparse_curve_days = _scalar(
        conn,
        "SELECT count(*) FROM ("
        "  SELECT curve, quote_date FROM yield_curves WHERE superseded_at IS NULL"
        "  GROUP BY curve, quote_date HAVING count(*) < :min_points"
        ") x",
        min_points=MIN_CURVE_POINTS_PER_DAY,
    )
    # As-published exchange oddities in the trade-level feed, listed for awareness (not our
    # parse: a handful of prints quote absolute rupees instead of per-100, or carry junk
    # yields). Kept INFO — a permanently-red WARN on upstream-published data trains users
    # to ignore warnings.
    price_scale_outliers = _scalar(conn, "SELECT count(*) FROM corporate_trades WHERE price > 5000")
    extreme_trade_yields = _scalar(
        conn,
        "SELECT count(*) FROM corporate_trades "
        "WHERE trade_yield IS NOT NULL AND (trade_yield < -20 OR trade_yield > 100)",
    )
    return [
        QualityCheck(
            "zero_coupon_contradiction",
            Level.WARN,
            passed=zero_coupon_conflict == 0,
            observed=zero_coupon_conflict,
            detail="interest_type says zero-coupon but coupon > 0 (source contradiction)",
        ),
        QualityCheck(
            "strips_with_nonzero_coupon",
            Level.ERROR,
            passed=strips_with_coupon == 0,
            observed=strips_with_coupon,
            detail="STRIPS are zero-coupon by construction; coupon > 0 is a parse/merge defect",
        ),
        QualityCheck(
            "interest_type_noncanonical",
            Level.ERROR,
            passed=noncanonical_interest == 0,
            observed=noncanonical_interest,
            detail=(
                "interest_type outside the canonical set "
                f"{{{', '.join(CANONICAL_INTEREST_TYPES)}}} — a source wrote raw strings"
            ),
        ),
        QualityCheck(
            "implausible_trade_yields",
            Level.WARN,
            passed=implausible_yields == 0,
            observed=implausible_yields,
            detail="ccil lty < -20 or > 40 — transposed/garbled source columns",
        ),
        QualityCheck(
            "matured_but_status_active",
            Level.INFO,
            passed=True,
            observed=matured_active,
            detail=(
                "matured >1y ago yet upstream status still ACTIVE (stale source status; "
                "the active_securities view already excludes them by maturity)"
            ),
        ),
        QualityCheck(
            "sdl_issuer_noncanonical",
            Level.ERROR,
            passed=noncanonical_sdl_issuer == 0,
            observed=noncanonical_sdl_issuer,
            detail=(
                f"SDL issuers outside the closed {len(SDL_ISSUERS)}-state vocabulary — "
                "a connector bypassed bonds.states.sdl_issuer"
            ),
        ),
        QualityCheck(
            "sovereign_instrument_isin_mismatch",
            Level.ERROR,
            passed=sovereign_isin_mismatch == 0,
            observed=sovereign_isin_mismatch,
            detail="central-typed securities (GSEC/TBILL/STRIPS/SGB) whose ISIN is not IN00…",
        ),
        QualityCheck(
            "coupon_above_25pct",
            Level.INFO,
            passed=True,
            observed=coupon_above_25,
            detail="verified-legit distressed/high-yield paper; listed for awareness",
        ),
        QualityCheck(
            "curve_sparse_days",
            Level.WARN,
            passed=sparse_curve_days == 0,
            observed=sparse_curve_days,
            detail=(
                f"(curve, day)s with < {MIN_CURVE_POINTS_PER_DAY} tenor points — "
                "a partially-parsed workbook"
            ),
        ),
        QualityCheck(
            "corp_trade_price_scale_outliers",
            Level.INFO,
            passed=True,
            observed=price_scale_outliers,
            detail="prints quoting absolute rupees instead of per-100 face (as published)",
        ),
        QualityCheck(
            "corp_trade_extreme_yields",
            Level.INFO,
            passed=True,
            observed=extreme_trade_yields,
            detail="trade yields < -20% or > 100% as published by the exchange",
        ),
    ]


def check_cross_source(conn: Connection) -> list[QualityCheck]:
    """CCIL traded VWAP should track FBIL published price for the same sovereign ISIN & day."""
    row = conn.execute(
        text(
            """
            WITH j AS (
              SELECT abs(t.wap - v.price) AS px_diff
              FROM trades t JOIN valuations v
                ON v.isin=t.isin AND v.quote_date=t.trade_date AND v.source='fbil'
               AND v.superseded_at IS NULL
              WHERE t.source='ccil' AND t.segment IN ('GSEC','SDL')
                AND t.wap IS NOT NULL AND v.price IS NOT NULL
            )
            SELECT count(*), percentile_cont(0.99) WITHIN GROUP (ORDER BY px_diff)
            FROM j
            """
        )
    ).first()
    pairs = float(row[0]) if row and row[0] is not None else 0.0
    p99 = float(row[1]) if row and row[1] is not None else 0.0
    if pairs < MIN_CROSS_SOURCE_PAIRS:
        # A young/small database simply hasn't accumulated enough matched pairs yet — that's
        # "not assessable", not a data-quality failure. A permanently-failing WARN here would
        # train users to ignore warnings.
        return [
            QualityCheck(
                "cross_source_coverage",
                Level.INFO,
                passed=True,
                observed=pairs,
                detail=(
                    f"only {int(pairs)} matched CCIL/FBIL pairs (<{MIN_CROSS_SOURCE_PAIRS}); "
                    "not enough to assess price reconciliation yet"
                ),
            )
        ]
    return [
        QualityCheck("cross_source_matched_pairs", Level.INFO, passed=True, observed=pairs),
        QualityCheck(
            "cross_source_price_p99_diff",
            Level.WARN,
            passed=p99 <= MAX_CROSS_SOURCE_P99_PRICE_DIFF,
            observed=round(p99, 4),
            detail=f"99th pctile |CCIL WAP - FBIL price|, limit {MAX_CROSS_SOURCE_P99_PRICE_DIFF}",
        ),
    ]


def check_freshness(conn: Connection) -> list[QualityCheck]:
    """Every daily series must keep pace with the rest of the warehouse.

    Two failure modes, both observed in production and both invisible to every other check here:

    * a connector silently returns nothing while its audit row still says ``success``, so the
      catch-up anchor advances and the days are lost (CCIL, 17 nights);
    * a connector correctly records ``skipped`` but the retry reads a poisoned empty artifact
      out of the data lake, so it never heals (BSE, 20 nights).

    Both show up here as one series lagging its siblings.
    """
    series = {
        "valuations": "SELECT max(quote_date) FROM valuations",
        "yield_curves": "SELECT max(quote_date) FROM yield_curves",
        "trades_ccil": "SELECT max(trade_date) FROM trades WHERE source='ccil'",
        "trades_nse_cbm": "SELECT max(trade_date) FROM trades WHERE source='nse_cbm'",
        "corp_trades_nse": "SELECT max(trade_date) FROM corporate_trades WHERE source='nse'",
        "corp_trades_bse": "SELECT max(trade_date) FROM corporate_trades WHERE source='bse'",
    }
    latest = {name: conn.execute(text(sql)).scalar() for name, sql in series.items()}
    dated = {name: day for name, day in latest.items() if day is not None}
    if not dated:
        return [
            QualityCheck(
                "freshness_assessable",
                Level.INFO,
                passed=True,
                observed=0.0,
                detail="no dated series in the warehouse yet",
            )
        ]
    high_water = max(dated.values())
    checks = [
        QualityCheck(
            "warehouse_high_water_mark",
            Level.INFO,
            passed=True,
            observed=0.0,
            detail=f"newest date in any daily series: {high_water.isoformat()}",
        )
    ]
    for name, day in sorted(dated.items()):
        lag = (high_water - day).days
        checks.append(
            QualityCheck(
                f"stale_series_{name}",
                Level.ERROR,
                passed=lag <= MAX_SERIES_LAG_DAYS,
                observed=float(lag),
                detail=(
                    f"newest row {day.isoformat()}, {lag}d behind {high_water.isoformat()} "
                    f"(limit {MAX_SERIES_LAG_DAYS}d)"
                ),
            )
        )
    # The silent-loss signature: a date-series run recorded SUCCESS, yet the table it feeds holds
    # nothing for that date. A zero-row audit figure alone is NOT that signature — a bitemporal
    # upsert legitimately writes nothing when catch-up re-runs a day whose data is already
    # loaded and unchanged — so the invariant has to be checked against the data, not the count.
    for dataset, table, date_column, source_filter in (
        ("ccil.trades", "trades", "trade_date", "source='ccil'"),
        ("nse_cbm.trades", "trades", "trade_date", "source='nse_cbm'"),
        ("bse.corp_trades", "corporate_trades", "trade_date", "source='bse'"),
    ):
        empty_days = _scalar(
            conn,
            f"""
            SELECT count(*) FROM ingestion_runs r
             WHERE r.dataset = :dataset
               AND r.status = 'success'
               AND r.run_date >= CURRENT_DATE - make_interval(days => :window)
               AND NOT EXISTS (
                     SELECT 1 FROM {table} t
                      WHERE t.{date_column} = r.run_date AND {source_filter}
                   )
            """,
            dataset=dataset,
            window=int(EMPTY_SUCCESS_WINDOW_DAYS),
        )
        checks.append(
            QualityCheck(
                f"empty_successful_days_{dataset.replace('.', '_')}",
                Level.ERROR,
                passed=empty_days == 0,
                observed=empty_days,
                detail=(
                    f"days in the last {EMPTY_SUCCESS_WINDOW_DAYS}d whose {dataset} run says "
                    "SUCCESS but which have no rows in the table — catch-up resumes from the "
                    "last success, so those dates are never re-attempted"
                ),
            )
        )
    return checks


def check_derived_products(conn: Connection) -> list[QualityCheck]:
    """Invariants for the derived product tables (liquidity, spread matrix, auction results).

    These are computed by us rather than published to us, so a defect here is a defect in our
    arithmetic — a different class from the upstream oddities the other dimensions tolerate, and
    held to a tighter standard accordingly.
    """
    checks: list[QualityCheck] = []

    # --- liquidity -------------------------------------------------------------------------
    liquidity_rows = _scalar(conn, "SELECT count(*) FROM security_liquidity")
    if liquidity_rows:
        # An as-of snapshot where nothing qualifies as an active market means the thresholds or
        # the trade join broke: the corporate tape alone carries ~400 ISINs printing most days.
        latest = conn.execute(text("SELECT max(as_of_date) FROM security_liquidity")).scalar()
        active = _scalar(
            conn,
            "SELECT count(*) FROM security_liquidity WHERE as_of_date = :d AND active_market",
            d=latest,
        )
        checks.append(
            QualityCheck(
                "liquidity_active_market_count",
                Level.ERROR,
                passed=active > 0,
                observed=active,
                detail=f"instruments meeting the active-market test on {latest}",
            )
        )
        # traded_within_15d must agree with days_since_trade; they are computed together, so a
        # disagreement means one of the two expressions drifted.
        inconsistent = _scalar(
            conn,
            """
            SELECT count(*) FROM security_liquidity
             WHERE traded_within_15d
                <> (days_since_trade IS NOT NULL AND days_since_trade <= :cap_days)
            """,
            cap_days=TRADED_PRICE_CAP_DAYS,
        )
        checks.append(
            QualityCheck(
                "liquidity_15d_flag_consistent",
                Level.ERROR,
                passed=inconsistent == 0,
                observed=inconsistent,
                detail="rows where traded_within_15d disagrees with days_since_trade",
            )
        )
        # A window count can never exceed a longer window's.
        nested = _scalar(
            conn,
            """
            SELECT count(*) FROM security_liquidity
             WHERE prints_1m > prints_3m OR prints_3m > prints_12m
                OR days_traded_1m > days_traded_3m OR days_traded_3m > days_traded_12m
            """,
        )
        checks.append(
            QualityCheck(
                "liquidity_windows_nested",
                Level.ERROR,
                passed=nested == 0,
                observed=nested,
                detail="rows whose 1m count exceeds 3m, or 3m exceeds 12m",
            )
        )

    # --- spread matrix ---------------------------------------------------------------------
    if _scalar(conn, "SELECT count(*) FROM corporate_spread_matrix"):
        # A cell computed from fewer prints than the floor should never have been written.
        thin = _scalar(
            conn,
            "SELECT count(*) FROM corporate_spread_matrix WHERE trade_count < :floor",
            floor=MIN_TRADES_PER_CELL,
        )
        checks.append(
            QualityCheck(
                "spread_matrix_thin_cells",
                Level.ERROR,
                passed=thin == 0,
                observed=thin,
                detail="cells below the minimum-prints floor",
            )
        )
        # Negative spreads are possible in a thin cell (a print below the CG curve) and are kept
        # deliberately — the cell's trade_count is how a caller sees why. Reported, not failed.
        negative = _scalar(conn, "SELECT count(*) FROM corporate_spread_matrix WHERE spread_bp < 0")
        checks.append(
            QualityCheck(
                "spread_matrix_negative_cells",
                Level.INFO,
                passed=True,
                observed=negative,
                detail="cells priced through the CG curve (thin cells; check trade_count)",
            )
        )

    # --- auction results -------------------------------------------------------------------
    if _scalar(conn, "SELECT count(*) FROM rbi_auction_results"):
        # RBI can accept slightly more than notified (non-competitive add-ons) but not double
        # it; a large overshoot means a column was read from the wrong security.
        overshoot = _scalar(
            conn,
            """
            SELECT count(*) FROM rbi_auction_results
             WHERE notified_amount_cr IS NOT NULL AND bids_accepted_amount_cr IS NOT NULL
               AND bids_accepted_amount_cr > notified_amount_cr * 1.5
            """,
        )
        checks.append(
            QualityCheck(
                "auction_accepted_within_notified",
                Level.ERROR,
                passed=overshoot == 0,
                observed=overshoot,
                detail="securities where accepted exceeds 150% of notified (column misread?)",
            )
        )
        implausible = _scalar(
            conn,
            "SELECT count(*) FROM rbi_auction_results "
            "WHERE cut_off_yield IS NOT NULL AND (cut_off_yield < 0 OR cut_off_yield > 20)",
        )
        checks.append(
            QualityCheck(
                "auction_cut_off_yield_plausible",
                Level.ERROR,
                passed=implausible == 0,
                observed=implausible,
                detail="cut-off yields outside 0-20% — a parenthetical read as a price",
            )
        )
    return checks


def run_assessment(database: Database) -> AssessmentReport:
    """Run every DB-wide assessment dimension and return the grouped results."""
    with database.engine.connect() as conn:
        return AssessmentReport(
            groups={
                "Uniqueness": check_uniqueness(conn),
                "Referential integrity": check_referential_integrity(conn),
                "Completeness": check_completeness(conn),
                "Consistency": check_consistency(conn),
                "Cross-source reconciliation": check_cross_source(conn),
                "Freshness": check_freshness(conn),
                "Derived products": check_derived_products(conn),
            }
        )
