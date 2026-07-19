"""Source-agnostic domain records produced by connectors and consumed by pipelines."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Plausible bond-maturity window. Outside this, a date is a sentinel/placeholder, not a real
# maturity: CDSL uses 1999-12-31 (no-maturity) and 2099-12-31 / 2999-12-31 (perpetual bonds), and
# the floor is set below the earliest real instrument (a 2002 T-Bill) so no genuine maturity is cut.
_MATURITY_MIN: dt.date = dt.date(2001, 1, 1)
_MATURITY_MAX: dt.date = dt.date(2098, 12, 31)


def _plausible_maturity(value: dt.date | None) -> dt.date | None:
    """Null a maturity that falls outside the plausible window (a placeholder, not a real date)."""
    if value is not None and not (_MATURITY_MIN <= value <= _MATURITY_MAX):
        return None
    return value


class InstrumentType(StrEnum):
    """Bond instrument classification used across the universe."""

    GSEC = "GSEC"
    """Central government dated security."""
    SDL = "SDL"
    """State Development Loan / State Government Security."""
    TBILL = "TBILL"
    """Treasury Bill (91/182/364-day)."""
    STRIPS = "STRIPS"
    """Separately traded G-Sec principal/interest STRIP."""
    SGB = "SGB"
    """Sovereign Gold Bond (gold-linked, quoted per gram)."""
    CORP = "CORP"
    """Corporate bond / debenture."""


class SovereignValuation(BaseModel):
    """One security's FBIL end-of-day valuation for a single business date.

    This is the atomic unit of the sovereign price/yield history (pillar 3).
    """

    model_config = ConfigDict(frozen=True)

    isin: str = Field(min_length=12, max_length=12)
    quote_date: dt.date
    instrument_type: InstrumentType
    source: str
    description: str | None = None
    coupon: float | None = None
    maturity_date: dt.date | None = None
    price: float | None = None
    ytm: float | None = None

    @field_validator("price")
    @classmethod
    def _price_positive_or_none(cls, v: float | None) -> float | None:
        # Match ck_valuation_price_positive: a non-positive price is bad data -> null (DQ flags it).
        return v if v is None or v > 0 else None

    @field_validator("ytm")
    @classmethod
    def _ytm_nonneg_or_none(cls, v: float | None) -> float | None:
        return v if v is None or v >= 0 else None

    @field_validator("maturity_date")
    @classmethod
    def _maturity_plausible_or_none(cls, v: dt.date | None) -> dt.date | None:
        return _plausible_maturity(v)


class TradeRecord(BaseModel):
    """A per-ISIN secondary-market trade summary for one session (e.g. NSE corporate bonds)."""

    model_config = ConfigDict(frozen=True)

    isin: str = Field(min_length=12, max_length=12)
    trade_date: dt.date
    source: str
    segment: str
    descriptor: str | None = None
    ltp: float | None = None
    """Last traded price."""
    lty: float | None = None
    """Last traded yield."""
    no_of_trades: int | None = None
    trade_value: float | None = None
    wap: float | None = None
    """Weighted-average price."""
    way: float | None = None
    """Weighted-average yield."""

    @field_validator("ltp")
    @classmethod
    def _ltp_positive_or_none(cls, v: float | None) -> float | None:
        # Match ck_trade_ltp_positive: an untraded row's ltp=0 becomes null instead of crashing.
        return v if v is None or v > 0 else None


class RbiAuctionRecord(BaseModel):
    """An RBI sovereign auction announcement (calendar level; financials are a follow-up)."""

    model_config = ConfigDict(frozen=True)

    prid: str
    title: str
    auction_type: str
    source: str
    auction_date: dt.date | None = None
    detail_url: str | None = None
    pdf_url: str | None = None


class PublicIssueRecord(BaseModel):
    """A corporate-bond public issue (SEBI primary-market calendar; not per-ISIN)."""

    model_config = ConfigDict(frozen=True)

    company: str
    issue_open: dt.date
    source: str
    issue_close: dt.date | None = None
    base_size_cr: float | None = None
    final_size_cr: float | None = None
    financial_year: str | None = None


class SecurityRecord(BaseModel):
    """A universe security's identifying + reference attributes for upsert (pillar 1)."""

    model_config = ConfigDict(frozen=True)

    isin: str = Field(min_length=12, max_length=12)
    instrument_type: InstrumentType
    source: str
    description: str | None = None
    issuer: str | None = None
    coupon: float | None = None
    interest_type: str | None = None
    maturity_date: dt.date | None = None
    face_value: float | None = None
    attributes: dict[str, str | None] = Field(default_factory=dict)
    """Trackable attributes for SCD-2 history (e.g. ``{"credit_rating": "AAA"}``)."""

    @field_validator("coupon")
    @classmethod
    def _coupon_nonneg_or_none(cls, v: float | None) -> float | None:
        # Match ck_security_coupon_nonneg: one bad coupon nulls out rather than failing the batch.
        return v if v is None or v >= 0 else None

    @field_validator("maturity_date")
    @classmethod
    def _maturity_plausible_or_none(cls, v: dt.date | None) -> dt.date | None:
        return _plausible_maturity(v)
