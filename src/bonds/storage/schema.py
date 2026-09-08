"""SQLAlchemy 2.0 ORM schema.

Tables:
    securities                  Current universe state, one row per ISIN (pillar 1).
    security_attribute_history  SCD-2 effective-dated attribute changes, e.g. rating (pillar 2).
    valuations                  Daily per-ISIN price/YTM history, append-only/bitemporal (pillar 3).
    ingestion_runs              Audit log of every pipeline run.
    security_liquidity          Derived per-ISIN traded-liquidity metrics as at a business date.
    corporate_spread_matrix     Derived daily rating x tenor spread grid, from actual prints.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


ACTIVE_SECURITIES_VIEW = "active_securities"

# The ladder must never select a matured or dead security. This view is the canonical
# "investable now" universe: not past maturity, and (where a status is known) still ACTIVE.
# For a point-in-time backtest, filter securities/valuations by the as-of date directly instead.
_ACTIVE_SECURITIES_DDL = f"""
CREATE OR REPLACE VIEW {ACTIVE_SECURITIES_VIEW} AS
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

# Create the view after tables (so create_all in tests gets it); drop it before tables.
event.listen(Base.metadata, "after_create", DDL(_ACTIVE_SECURITIES_DDL))  # type: ignore[no-untyped-call]
event.listen(
    Base.metadata,
    "before_drop",
    DDL(f"DROP VIEW IF EXISTS {ACTIVE_SECURITIES_VIEW}"),  # type: ignore[no-untyped-call]
)


class Security(Base):
    """Current identifying + reference attributes for a universe security (pillar 1)."""

    __tablename__ = "securities"
    __table_args__ = (
        CheckConstraint("coupon IS NULL OR coupon >= 0", name="ck_security_coupon_nonneg"),
    )

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    instrument_type: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(Text, index=True)
    coupon: Mapped[float | None] = mapped_column(Float)
    interest_type: Mapped[str | None] = mapped_column(String(48))
    maturity_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    face_value: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    first_seen: Mapped[dt.date] = mapped_column(Date)
    last_seen: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SecurityAttributeHistory(Base):
    """Effective-dated (SCD-2) history of a single tracked attribute for a security.

    A new row is written only when the value changes; the previous row's ``valid_to`` is
    closed to the day before the change. ``valid_to IS NULL`` marks the current value.
    """

    __tablename__ = "security_attribute_history"
    __table_args__ = (
        UniqueConstraint("isin", "attribute", "valid_from", name="uq_attr_history_point"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    attribute: Mapped[str] = mapped_column(String(48), index=True)
    value: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32))
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Valuation(Base):
    """One security's end-of-day price/YTM as published for one business date (pillar 3).

    Append-only (bitemporal): a source restating a date's price never overwrites history.
    The stale row is closed by stamping ``superseded_at`` and a fresh row is inserted, so
    "what did we believe on date X" is always answerable. The current belief for a key is
    the row with ``superseded_at IS NULL`` (enforced by a partial unique index);
    ``loaded_at`` records when each version was ingested.
    """

    __tablename__ = "valuations"
    __table_args__ = (
        CheckConstraint("price IS NULL OR price > 0", name="ck_valuation_price_positive"),
        CheckConstraint("ytm IS NULL OR ytm >= 0", name="ck_valuation_ytm_nonneg"),
        Index(
            "uq_valuations_current",
            "isin",
            "quote_date",
            "source",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    quote_date: Mapped[dt.date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32))
    instrument_type: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    coupon: Mapped[float | None] = mapped_column(Float)
    maturity_date: Mapped[dt.date | None] = mapped_column(Date)
    price: Mapped[float | None] = mapped_column(Float)
    ytm: Mapped[float | None] = mapped_column(Float)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class YieldCurve(Base):
    """One tenor point of a published yield curve for one business date.

    Append-only/bitemporal like :class:`Valuation`: restatements close the current row via
    ``superseded_at`` instead of overwriting. Current curve = ``superseded_at IS NULL``.
    """

    __tablename__ = "yield_curves"
    __table_args__ = (
        CheckConstraint("tenor_years > 0", name="ck_yield_curve_tenor_positive"),
        Index(
            "uq_yield_curves_current",
            "curve",
            "quote_date",
            "tenor_years",
            "source",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    curve: Mapped[str] = mapped_column(String(24), index=True)
    quote_date: Mapped[dt.date] = mapped_column(Date, index=True)
    tenor_years: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    ytm_semi_annual: Mapped[float | None] = mapped_column(Float)
    ytm_annualized: Mapped[float | None] = mapped_column(Float)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Trade(Base):
    """Per-ISIN secondary-market trade summary for one session (e.g. NSE corporate bonds)."""

    __tablename__ = "trades"
    __table_args__ = (CheckConstraint("ltp IS NULL OR ltp > 0", name="ck_trade_ltp_positive"),)

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    segment: Mapped[str] = mapped_column(String(24), primary_key=True)
    descriptor: Mapped[str | None] = mapped_column(Text)
    ltp: Mapped[float | None] = mapped_column(Float)
    lty: Mapped[float | None] = mapped_column(Float)
    no_of_trades: Mapped[int | None] = mapped_column(Integer)
    trade_value: Mapped[float | None] = mapped_column(Float)
    wap: Mapped[float | None] = mapped_column(Float)
    way: Mapped[float | None] = mapped_column(Float)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CorporateTrade(Base):
    """One corporate-bond transaction (RFQ or OTC-reported) from an exchange feed.

    Trade-level grain, unlike :class:`Trade` (per-ISIN session summaries). There is no
    natural transaction key across venues, so idempotency is delete-and-replace per
    ``(source, trade_date window)`` at load time rather than an upsert.
    """

    __tablename__ = "corporate_trades"
    __table_args__ = (
        CheckConstraint("price IS NULL OR price > 0", name="ck_corp_trade_price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    isin: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    trade_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    listed: Mapped[str | None] = mapped_column(String(16))
    deal_type: Mapped[str | None] = mapped_column(String(32))
    seller_deal_type: Mapped[str | None] = mapped_column(String(32))
    buyer_deal_type: Mapped[str | None] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    coupon: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    trade_yield: Mapped[float | None] = mapped_column(Float)
    yield_type: Mapped[str | None] = mapped_column(String(8))
    outside_yield_range: Mapped[str | None] = mapped_column(String(8))
    put_call_date: Mapped[dt.date | None] = mapped_column(Date)
    trade_value_lakh: Mapped[float | None] = mapped_column(Float)
    settlement_date: Mapped[dt.date | None] = mapped_column(Date)
    settlement_status: Mapped[str | None] = mapped_column(String(24))
    venue: Mapped[str | None] = mapped_column(String(16))
    remarks: Mapped[str | None] = mapped_column(Text)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionRun(Base):
    """Audit record for a pipeline execution against one dataset + business date.

    Idempotent per ``(source, dataset, run_date)``: re-running a day updates the record rather
    than appending, so multiple runs in a day converge to one row.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (UniqueConstraint("source", "dataset", "run_date", name="uq_ingestion_run"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str] = mapped_column(String(48), index=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16))
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PublicIssue(Base):
    """A corporate-bond public issue (SEBI primary-market calendar)."""

    __tablename__ = "public_issues"
    __table_args__ = (UniqueConstraint("company", "issue_open", "source", name="uq_public_issue"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company: Mapped[str] = mapped_column(Text, index=True)
    issue_open: Mapped[dt.date] = mapped_column(Date, index=True)
    issue_close: Mapped[dt.date | None] = mapped_column(Date)
    base_size_cr: Mapped[float | None] = mapped_column(Float)
    final_size_cr: Mapped[float | None] = mapped_column(Float)
    financial_year: Mapped[str | None] = mapped_column(String(9), index=True)
    source: Mapped[str] = mapped_column(String(32))
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RbiAuction(Base):
    """An RBI sovereign auction announcement (calendar; financials are a follow-up)."""

    __tablename__ = "rbi_auctions"
    __table_args__ = (UniqueConstraint("prid", "source", name="uq_rbi_auction"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prid: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(Text)
    auction_type: Mapped[str] = mapped_column(String(16), index=True)
    auction_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    detail_url: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    first_seen: Mapped[dt.date] = mapped_column(Date)
    last_seen: Mapped[dt.date] = mapped_column(Date)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EtlFileMetric(Base):
    """Per-artifact extract/transform funnel metrics for one dataset + run date.

    The load stage (rows written) lives in ``ingestion_runs.rows_ingested``; together they describe
    the full ETL funnel. Idempotent per ``(source, dataset, run_date, artifact)``.
    """

    __tablename__ = "etl_file_metrics"
    __table_args__ = (
        UniqueConstraint("source", "dataset", "run_date", "artifact", name="uq_etl_file_metric"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str] = mapped_column(String(48), index=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    artifact: Mapped[str] = mapped_column(String(64))
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_extracted: Mapped[int] = mapped_column(Integer, default=0)
    rows_parsed: Mapped[int] = mapped_column(Integer, default=0)
    rows_dropped: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DataQualityCheck(Base):
    """Result of one data-quality assertion for a dataset + business date.

    Persisted every run so quality can be monitored over time (drift, null-rate creep, anomalies)
    rather than inspected ad hoc. ``level`` is ``info``/``warn``/``error``; ``passed`` is the
    boolean verdict; ``observed`` carries the measured value behind the verdict.
    """

    __tablename__ = "data_quality_checks"
    __table_args__ = (
        UniqueConstraint("dataset", "run_date", "check_name", name="uq_data_quality_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str] = mapped_column(String(48), index=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    check_name: Mapped[str] = mapped_column(String(48), index=True)
    level: Mapped[str] = mapped_column(String(8))
    passed: Mapped[bool] = mapped_column(Boolean)
    observed: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SecurityLiquidity(Base):
    """Per-ISIN traded-liquidity metrics as at one business date.

    Derived, not ingested: recomputed from ``corporate_trades`` (trade-level) and ``trades``
    (per-session summaries) for a given as-of date. It exists as a table rather than a view
    because every question asked of it is point-in-time — "was this instrument's market active
    on 31 March", "was there a print within 15 days of the valuation date" — and a view keyed to
    ``CURRENT_DATE`` cannot answer those a quarter later.

    This is the dataset behind two RBI obligations: the fair-value-hierarchy active-market test
    (trade frequency and volume per instrument, clause 4(1)) and the 15-day traded-price cap on
    corporate valuations (clause 78(1)(i)(c)).
    """

    __tablename__ = "security_liquidity"
    __table_args__ = (
        CheckConstraint(
            "days_since_trade IS NULL OR days_since_trade >= 0",
            name="ck_liquidity_days_since_trade",
        ),
        Index("ix_security_liquidity_as_of_active", "as_of_date", "active_market"),
    )

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, primary_key=True, index=True)
    instrument_type: Mapped[str] = mapped_column(String(12), index=True)
    last_trade_date: Mapped[dt.date | None] = mapped_column(Date)
    days_since_trade: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[float | None] = mapped_column(Float)
    last_yield: Mapped[float | None] = mapped_column(Float)
    prints_1m: Mapped[int] = mapped_column(Integer, default=0)
    prints_3m: Mapped[int] = mapped_column(Integer, default=0)
    prints_12m: Mapped[int] = mapped_column(Integer, default=0)
    days_traded_1m: Mapped[int] = mapped_column(Integer, default=0)
    days_traded_3m: Mapped[int] = mapped_column(Integer, default=0)
    days_traded_12m: Mapped[int] = mapped_column(Integer, default=0)
    turnover_1m: Mapped[float | None] = mapped_column(Float)
    turnover_3m: Mapped[float | None] = mapped_column(Float)
    turnover_12m: Mapped[float | None] = mapped_column(Float)
    traded_within_15d: Mapped[bool] = mapped_column(Boolean, default=False)
    active_market: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CorporateSpreadPoint(Base):
    """One (rating, tenor bucket) cell of the daily corporate spread matrix.

    FIMMDA publishes the market's reference spread matrix, but its spreads are poll-derived down
    to AA- and, below that, fixed for three months from a trailing three-month traded level. This
    table is the same grid computed from actual prints in ``corporate_trades`` — trade-weighted
    yield in the cell, minus the G-Sec par yield at the cell's tenor.

    Thin cells are kept rather than suppressed: ``trade_count`` and ``isin_count`` are the
    caller's basis for deciding whether a cell is usable, and a silently-absent cell is
    indistinguishable from a cell nobody computed.
    """

    __tablename__ = "corporate_spread_matrix"
    __table_args__ = (
        UniqueConstraint("quote_date", "rating", "tenor_bucket", name="uq_corp_spread_cell"),
        CheckConstraint("tenor_bucket > 0", name="ck_corp_spread_tenor_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    quote_date: Mapped[dt.date] = mapped_column(Date, index=True)
    rating: Mapped[str] = mapped_column(String(8), index=True)
    tenor_bucket: Mapped[float] = mapped_column(Float)
    trade_count: Mapped[int] = mapped_column(Integer)
    isin_count: Mapped[int] = mapped_column(Integer)
    notional_lakh: Mapped[float | None] = mapped_column(Float)
    wavg_yield_pct: Mapped[float] = mapped_column(Float)
    median_yield_pct: Mapped[float | None] = mapped_column(Float)
    cg_yield_pct: Mapped[float] = mapped_column(Float)
    spread_bp: Mapped[float] = mapped_column(Float)
    lookback_days: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RbiAuctionResult(Base):
    """One security's outcome in an RBI auction, from a "Full Auction Result" press release.

    ``rbi_auctions`` records that an auction was announced; this records what it cleared at.
    Keyed on (prid, security) because one release covers every security in that day's auction —
    two G-Sec lines, three T-Bill tenors, or thirty state loans.

    The cut-off is the primary-market pricing reference: the level at which the sovereign
    actually placed paper on the day, against which the secondary curve in ``yield_curves`` and
    the marks in ``valuations`` can be judged.
    """

    __tablename__ = "rbi_auction_results"
    __table_args__ = (
        CheckConstraint(
            "notified_amount_cr IS NULL OR notified_amount_cr >= 0",
            name="ck_auction_result_notified_non_negative",
        ),
    )

    prid: Mapped[str] = mapped_column(String(16), primary_key=True)
    security: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    auction_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    auction_type: Mapped[str | None] = mapped_column(String(24), index=True)
    tenor_note: Mapped[str | None] = mapped_column(Text)
    notified_amount_cr: Mapped[float | None] = mapped_column(Float)
    bids_received_count: Mapped[int | None] = mapped_column(Integer)
    bids_received_amount_cr: Mapped[float | None] = mapped_column(Float)
    bids_accepted_count: Mapped[int | None] = mapped_column(Integer)
    bids_accepted_amount_cr: Mapped[float | None] = mapped_column(Float)
    cut_off_price: Mapped[float | None] = mapped_column(Float)
    cut_off_yield: Mapped[float | None] = mapped_column(Float)
    wavg_price: Mapped[float | None] = mapped_column(Float)
    wavg_yield: Mapped[float | None] = mapped_column(Float)
    partial_allotment_pct: Mapped[float | None] = mapped_column(Float)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
