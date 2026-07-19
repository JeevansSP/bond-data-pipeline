"""Database-wide data-quality assessment (cross-table, cross-source).

Complements the per-batch :mod:`bonds.quality.checks` (which run during each ingest) with checks
that only make sense over the whole warehouse: duplicate keys, referential integrity between the
trade/valuation series and the securities master, field completeness, and cross-source price
reconciliation (CCIL traded prices vs FBIL end-of-day marks). Exposed via ``bonds dq assess``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from bonds.quality.checks import Level, QualityCheck
from bonds.storage import Database

# --- thresholds (tunable) -------------------------------------------------------------------
MAX_VALUATION_NULL_PRICE_RATE = 0.05
MAX_TRADE_NULL_PRICE_RATE = 0.02
MAX_CROSS_SOURCE_P99_PRICE_DIFF = 3.0  # |CCIL WAP - FBIL price| per 100 face, 99th pctile
MIN_CROSS_SOURCE_PAIRS = 500  # too few matched pairs -> reconciliation is uninformative
_SOVEREIGN = ("GSEC", "SDL", "TBILL", "STRIPS")


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
            "valuations",
            "SELECT count(*) FROM (SELECT 1 FROM valuations GROUP BY isin,quote_date,"
            "source HAVING count(*)>1) x",
        ),
        (
            "securities",
            "SELECT count(*) FROM (SELECT 1 FROM securities GROUP BY isin HAVING count(*)>1) x",
        ),
    ]
    return [
        QualityCheck(
            f"dup_key_{name}", Level.ERROR, passed=(n := _scalar(conn, sql)) == 0, observed=n
        )
        for name, sql in specs
    ]


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
        "WHERE s.isin IS NULL",
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
    ]


def check_completeness(conn: Connection) -> list[QualityCheck]:
    """Null-rate thresholds on fields that should be populated."""
    checks: list[QualityCheck] = []
    val_total = _scalar(conn, "SELECT count(*) FROM valuations")
    if val_total:
        null_px = _scalar(conn, "SELECT count(*) FROM valuations WHERE price IS NULL")
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
    return checks


def check_cross_source(conn: Connection) -> list[QualityCheck]:
    """CCIL traded VWAP should track FBIL published price for the same sovereign ISIN & day."""
    row = conn.execute(
        text(
            """
            WITH j AS (
              SELECT abs(t.wap - v.price) AS px_diff
              FROM trades t JOIN valuations v
                ON v.isin=t.isin AND v.quote_date=t.trade_date AND v.source='fbil'
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
        return [
            QualityCheck(
                "cross_source_coverage",
                Level.WARN,
                passed=False,
                observed=pairs,
                detail=f"only {int(pairs)} matched CCIL/FBIL pairs (<{MIN_CROSS_SOURCE_PAIRS})",
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


def run_assessment(database: Database) -> AssessmentReport:
    """Run every DB-wide assessment dimension and return the grouped results."""
    with database.engine.connect() as conn:
        return AssessmentReport(
            groups={
                "Uniqueness": check_uniqueness(conn),
                "Referential integrity": check_referential_integrity(conn),
                "Completeness": check_completeness(conn),
                "Cross-source reconciliation": check_cross_source(conn),
            }
        )
