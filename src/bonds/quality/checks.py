"""Pure batch quality checks over parsed records (no DB access).

Each check returns a :class:`QualityCheck`; the :mod:`bonds.quality.inspector` persists them and
adds DB-dependent checks (e.g. row-count drift versus the previous run).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from bonds.models import (
    PublicIssueRecord,
    RbiAuctionRecord,
    SecurityRecord,
    SovereignValuation,
    TradeRecord,
)
from bonds.quality.isin import is_valid_isin

# --- thresholds (tunable) -------------------------------------------------------------------
PRICE_MIN, PRICE_MAX = 50.0, 200.0
"""Clean/dirty bond prices cluster near par; outside this band is almost certainly bad data."""
YTM_MIN, YTM_MAX = 0.0, 25.0
"""Plausible annualised yields for INR bonds."""
MAX_NULL_VALUE_RATE = 0.05
"""Warn if >5% of a valuation batch has a null price/YTM."""


class Level(StrEnum):
    """Severity of a quality finding."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """One assertion's verdict."""

    name: str
    level: Level
    passed: bool
    observed: float | None = None
    detail: str | None = None


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def check_valuations(valuations: list[SovereignValuation]) -> list[QualityCheck]:
    """Quality checks for a batch of valuations (price/YTM sanity, ISINs, null rates)."""
    total = len(valuations)
    checks: list[QualityCheck] = [
        QualityCheck("row_count", Level.ERROR, passed=total > 0, observed=float(total)),
    ]
    if total == 0:
        return checks

    invalid_isin = sum(1 for v in valuations if not is_valid_isin(v.isin))
    null_price = sum(1 for v in valuations if v.price is None)
    null_ytm = sum(1 for v in valuations if v.ytm is None)
    px_oob = sum(
        1 for v in valuations if v.price is not None and not PRICE_MIN <= v.price <= PRICE_MAX
    )
    ytm_oob = sum(1 for v in valuations if v.ytm is not None and not YTM_MIN <= v.ytm <= YTM_MAX)

    checks += [
        QualityCheck(
            "invalid_isin", Level.ERROR, passed=invalid_isin == 0, observed=float(invalid_isin)
        ),
        QualityCheck(
            "null_price_rate",
            Level.WARN,
            passed=_rate(null_price, total) <= MAX_NULL_VALUE_RATE,
            observed=_rate(null_price, total),
        ),
        QualityCheck(
            "null_ytm_rate",
            Level.WARN,
            passed=_rate(null_ytm, total) <= MAX_NULL_VALUE_RATE,
            observed=_rate(null_ytm, total),
        ),
        QualityCheck(
            "price_out_of_range",
            Level.WARN,
            passed=px_oob == 0,
            observed=float(px_oob),
            detail=f"outside [{PRICE_MIN}, {PRICE_MAX}]",
        ),
        QualityCheck(
            "ytm_out_of_range",
            Level.WARN,
            passed=ytm_oob == 0,
            observed=float(ytm_oob),
            detail=f"outside [{YTM_MIN}, {YTM_MAX}]%",
        ),
    ]
    return checks


def check_universe(records: list[SecurityRecord], *, as_of: dt.date) -> list[QualityCheck]:
    """Quality checks for a universe batch (ISINs, maturity presence, matured count)."""
    total = len(records)
    checks: list[QualityCheck] = [
        QualityCheck("row_count", Level.ERROR, passed=total > 0, observed=float(total)),
    ]
    if total == 0:
        return checks

    invalid_isin = sum(1 for r in records if not is_valid_isin(r.isin))
    null_maturity = sum(1 for r in records if r.maturity_date is None)
    matured = sum(1 for r in records if r.maturity_date is not None and r.maturity_date < as_of)

    checks += [
        QualityCheck(
            "invalid_isin", Level.ERROR, passed=invalid_isin == 0, observed=float(invalid_isin)
        ),
        QualityCheck(
            "null_maturity_rate", Level.WARN, passed=True, observed=_rate(null_maturity, total)
        ),
        QualityCheck(
            "matured_in_universe",
            Level.INFO,
            passed=True,
            observed=float(matured),
            detail="carried securities already past maturity (exclude from active ladder)",
        ),
    ]
    return checks


def check_public_issues(issues: list[PublicIssueRecord]) -> list[QualityCheck]:
    """Quality checks for a SEBI public-issue batch (row count, close>=open, size presence)."""
    total = len(issues)
    checks: list[QualityCheck] = [
        QualityCheck("row_count", Level.ERROR, passed=total > 0, observed=float(total)),
    ]
    if total == 0:
        return checks

    bad_window = sum(
        1 for i in issues if i.issue_close is not None and i.issue_close < i.issue_open
    )
    null_final = sum(1 for i in issues if i.final_size_cr is None)
    checks += [
        QualityCheck(
            "close_before_open", Level.WARN, passed=bad_window == 0, observed=float(bad_window)
        ),
        QualityCheck(
            "null_final_size_rate",
            Level.WARN,
            passed=True,
            observed=_rate(null_final, total),
        ),
    ]
    return checks


def check_trades(trades: list[TradeRecord]) -> list[QualityCheck]:
    """Quality checks for a trade batch. Zero rows is INFO (markets close/holidays)."""
    total = len(trades)
    checks: list[QualityCheck] = [
        QualityCheck("row_count", Level.INFO, passed=True, observed=float(total)),
    ]
    if total == 0:
        return checks
    invalid_isin = sum(1 for t in trades if not is_valid_isin(t.isin))
    px_oob = sum(1 for t in trades if t.ltp is not None and not PRICE_MIN <= t.ltp <= PRICE_MAX)
    checks += [
        QualityCheck(
            "invalid_isin", Level.ERROR, passed=invalid_isin == 0, observed=float(invalid_isin)
        ),
        QualityCheck(
            "ltp_out_of_range",
            Level.WARN,
            passed=px_oob == 0,
            observed=float(px_oob),
            detail=f"outside [{PRICE_MIN}, {PRICE_MAX}]",
        ),
    ]
    return checks


def check_rbi_auctions(auctions: list[RbiAuctionRecord]) -> list[QualityCheck]:
    """Quality checks for an RBI auction batch (row count, unclassified, missing-date rate)."""
    total = len(auctions)
    checks: list[QualityCheck] = [
        QualityCheck("row_count", Level.ERROR, passed=total > 0, observed=float(total)),
    ]
    if total == 0:
        return checks
    unclassified = sum(1 for a in auctions if a.auction_type == "Other")
    null_date = sum(1 for a in auctions if a.auction_date is None)
    checks += [
        QualityCheck(
            "unclassified_auction_type",
            Level.WARN,
            passed=unclassified == 0,
            observed=float(unclassified),
        ),
        QualityCheck(
            "null_auction_date_rate", Level.WARN, passed=True, observed=_rate(null_date, total)
        ),
    ]
    return checks
