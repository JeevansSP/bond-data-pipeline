"""Source-agnostic domain records produced by connectors and consumed by pipelines."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

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


# Canonical interest-type vocabulary. Sources spell this 15+ ways ("Fixed", "FIXED",
# "Fixed Interest", "ZERO_COUPON", "Variable - Mibor Linked", ...), so every raw value is
# collapsed to exactly one of FIXED / ZERO / FLOATING / OTHER (or None) at record-construction
# time. Keys are matched case-insensitively with whitespace collapsed.
_INTEREST_TYPE_CANONICAL: Final[dict[str, str]] = {
    # Plain fixed-coupon paper.
    "fixed": "FIXED",
    "fixed interest": "FIXED",
    # Zero-coupon / discount paper.
    "zero": "ZERO",
    "zero_coupon": "ZERO",
    "zero interest": "ZERO",
    "no interest": "ZERO",
    # Rate/index/inflation-linked floaters.
    "floating": "FLOATING",
    "variable interest": "FLOATING",
    "variable-others": "FLOATING",
    "variable-index linked": "FLOATING",
    "variable - mibor linked": "FLOATING",
    "variable-inflation": "FLOATING",
    # Market-linked debentures (equity/commodity payoffs, not rate floaters) and explicit N/A.
    "variable-equity linked": "OTHER",
    "variable- commodity linked": "OTHER",
    "not applicable": "OTHER",
}


def normalize_interest_type(raw: str | None) -> str | None:
    """Map any raw source interest-type spelling to the canonical vocabulary.

    Returns exactly one of ``FIXED``, ``ZERO``, ``FLOATING``, ``OTHER`` or ``None``:

    - FIXED: ``fixed``, ``fixed interest``
    - ZERO: ``zero_coupon``, ``zero interest``, ``no interest``, ``zero``
    - FLOATING: ``floating``, ``variable interest``, ``variable-others``,
      ``variable-index linked``, ``variable - mibor linked``, ``variable-inflation``
    - OTHER: ``variable-equity linked``, ``variable- commodity linked``, ``not applicable``,
      and ANY unrecognized non-empty value (new source vocab must never crash ingestion)
    - None: NULL/empty/whitespace-only input stays None

    Matching is case-insensitive with surrounding whitespace stripped and internal runs of
    whitespace collapsed to a single space.
    """
    if raw is None:
        return None
    key = " ".join(raw.split()).lower()
    if not key:
        return None
    return _INTEREST_TYPE_CANONICAL.get(key, "OTHER")


# --- corporate-trade categorical vocabularies -----------------------------------------------
# NSE and BSE publish the same categories in different casings and abbreviations — "Listed" and
# "LISTED", "Direct" and "DIRECT", "IST" and "Inter Scheme Transfer" — so the raw columns
# fragment into twice as many values as there are real states. Normalising at the model boundary
# (as :func:`normalize_interest_type` already does for the master) keeps every source on one
# vocabulary no matter which connector wrote the row.
_DEAL_TYPE_CANONICAL: Final[dict[str, str]] = {
    "direct": "DIRECT",
    "brokered": "BROKERED",
    "ist": "INTER_SCHEME_TRANSFER",
    "inter scheme transfer": "INTER_SCHEME_TRANSFER",
    "buyback": "BUYBACK",
    "buy back": "BUYBACK",
}
_LISTED_CANONICAL: Final[dict[str, str]] = {
    "listed": "LISTED",
    "unlisted": "UNLISTED",
    "y": "LISTED",
    "n": "UNLISTED",
}
_SETTLEMENT_STATUS_CANONICAL: Final[dict[str, str]] = {
    "settled": "SETTLED",
    "not settled": "NOT_SETTLED",
    "unsettled": "NOT_SETTLED",
    "pending": "PENDING",
    "rejected": "REJECTED",
}


def _canonicalize(raw: str | None, vocabulary: dict[str, str]) -> str | None:
    """Map a raw source value onto ``vocabulary``, or ``OTHER`` when unrecognised.

    Case-insensitive, whitespace-collapsed. An unknown non-empty value becomes ``OTHER`` rather
    than raising: a new source spelling must never break ingestion, and the DQ vocabulary checks
    surface the ``OTHER`` count.
    """
    if raw is None:
        return None
    key = " ".join(raw.split()).lower()
    if not key:
        return None
    return vocabulary.get(key, "OTHER")


def normalize_deal_type(raw: str | None) -> str | None:
    """Normalize a published deal type to the canonical vocabulary.

    One of ``DIRECT``, ``BROKERED``, ``INTER_SCHEME_TRANSFER``, ``BUYBACK`` or ``OTHER``;
    ``None`` stays ``None``.
    """
    return _canonicalize(raw, _DEAL_TYPE_CANONICAL)


def normalize_listed(raw: str | None) -> str | None:
    """Normalize a listing flag to ``LISTED``/``UNLISTED``/``OTHER`` (``None`` stays ``None``)."""
    return _canonicalize(raw, _LISTED_CANONICAL)


def normalize_settlement_status(raw: str | None) -> str | None:
    """Normalize a settlement status to the canonical vocabulary.

    One of ``SETTLED``, ``NOT_SETTLED``, ``PENDING``, ``REJECTED`` or ``OTHER``; ``None``
    stays ``None``.
    """
    return _canonicalize(raw, _SETTLEMENT_STATUS_CANONICAL)


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


class CorporateTradeRecord(BaseModel):
    """One corporate-bond transaction (RFQ or OTC-reported) from an exchange reporting feed.

    Trade-level grain — the regulatory 15-day traded-price rule references the rate of a
    specific recorded transaction, so daily per-ISIN aggregates are not a substitute.
    ``deal_type`` carries BSE's single flag; NSE reports seller/buyer sides separately.
    """

    model_config = ConfigDict(frozen=True)

    isin: str = Field(min_length=12, max_length=12)
    trade_date: dt.date
    source: str
    trade_time: dt.datetime | None = None
    listed: str | None = None
    deal_type: str | None = None
    seller_deal_type: str | None = None
    buyer_deal_type: str | None = None
    issuer: str | None = None
    description: str | None = None
    coupon: float | None = None
    price: float | None = None
    trade_yield: float | None = None
    yield_type: str | None = None
    outside_yield_range: str | None = None
    put_call_date: dt.date | None = None
    trade_value_lakh: float | None = None
    settlement_date: dt.date | None = None
    settlement_status: str | None = None
    venue: str | None = None
    """Reporting venue flag: ``RFQ`` vs ``OTC``/``Reported``."""
    remarks: str | None = None

    @field_validator("deal_type", "seller_deal_type", "buyer_deal_type")
    @classmethod
    def _deal_type_canonical(cls, v: str | None) -> str | None:
        """Normalize to the canonical deal-type vocabulary (see :func:`normalize_deal_type`)."""
        return normalize_deal_type(v)

    @field_validator("listed")
    @classmethod
    def _listed_canonical(cls, v: str | None) -> str | None:
        """Normalize to ``LISTED``/``UNLISTED`` (see :func:`normalize_listed`)."""
        return normalize_listed(v)

    @field_validator("settlement_status")
    @classmethod
    def _settlement_status_canonical(cls, v: str | None) -> str | None:
        """Normalize to the canonical settlement vocabulary."""
        return normalize_settlement_status(v)

    @field_validator("price")
    @classmethod
    def _price_positive_or_none(cls, v: float | None) -> float | None:
        return v if v is None or v > 0 else None


class YieldCurvePoint(BaseModel):
    """One tenor point of a published yield curve for a single business date.

    ``curve`` is the canonical curve key (e.g. ``gsec_par``, ``gsec_zcyc``, ``sdl_zcyc``);
    yields are % p.a. in both the semi-annual and annualized conventions FBIL publishes.
    """

    model_config = ConfigDict(frozen=True)

    curve: str
    quote_date: dt.date
    tenor_years: float = Field(gt=0)
    source: str
    ytm_semi_annual: float | None = None
    ytm_annualized: float | None = None

    @field_validator("ytm_semi_annual", "ytm_annualized")
    @classmethod
    def _yield_nonneg_or_none(cls, v: float | None) -> float | None:
        return v if v is None or v >= 0 else None


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


class RbiAuctionResultRecord(BaseModel):
    """One security's outcome in an RBI auction, from a "Full Auction Result" press release.

    The announcement (:class:`RbiAuctionRecord`) is a calendar entry; this is what actually
    happened — notified versus accepted amount, and the cut-off the auction cleared at, which is
    the primary-market pricing reference the secondary curve is judged against.

    Every financial field is optional because the three result layouts publish different subsets:
    G-Sec and T-Bill releases give a cut-off *price* with the yield in a parenthetical, SDL
    releases give cut-off yield and price as separate rows, and only T-Bill releases carry a
    weighted-average line.
    """

    model_config = ConfigDict(frozen=True)

    prid: str
    security: str
    """Security label as published ("New GS 2031", "91-Day", "ASSAM SGS 2046")."""
    source: str
    auction_date: dt.date | None = None
    auction_type: str | None = None
    tenor_note: str | None = None
    """As-published tenor text, e.g. "Re-issue of 7.62% ASSAM SGS 2046" or "25 -Years"."""
    notified_amount_cr: float | None = None
    bids_received_count: int | None = None
    bids_received_amount_cr: float | None = None
    bids_accepted_count: int | None = None
    bids_accepted_amount_cr: float | None = None
    cut_off_price: float | None = None
    cut_off_yield: float | None = None
    wavg_price: float | None = None
    wavg_yield: float | None = None
    partial_allotment_pct: float | None = None

    @property
    def bid_cover_ratio(self) -> float | None:
        """Bids received over amount notified — the auction's demand signal."""
        if not self.notified_amount_cr or self.bids_received_amount_cr is None:
            return None
        return self.bids_received_amount_cr / self.notified_amount_cr


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

    @field_validator("interest_type")
    @classmethod
    def _interest_type_canonical(cls, v: str | None) -> str | None:
        """Normalize to the canonical vocabulary (see :func:`normalize_interest_type`).

        Every source constructs a SecurityRecord, so mapping here keeps the whole universe on
        one vocabulary without per-source edits. The canonical values are all far shorter than
        the VARCHAR(48) column, so the old truncation guard is subsumed (unknown long values
        collapse to ``OTHER``).
        """
        return normalize_interest_type(v)
