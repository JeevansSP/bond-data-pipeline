"""SQLAlchemy 2.0 ORM schema.

Tables:
    securities                  Current universe state, one row per ISIN (pillar 1).
    security_attribute_history  SCD-2 effective-dated attribute changes, e.g. rating (pillar 2).
    valuations                  Daily per-ISIN price/YTM history (pillar 3, FBIL).
    ingestion_runs              Audit log of every pipeline run.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
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
    interest_type: Mapped[str | None] = mapped_column(String(24))
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
    """One security's end-of-day price/YTM for one business date (pillar 3)."""

    __tablename__ = "valuations"
    __table_args__ = (
        CheckConstraint("price IS NULL OR price > 0", name="ck_valuation_price_positive"),
        CheckConstraint("ytm IS NULL OR ytm >= 0", name="ck_valuation_ytm_nonneg"),
    )

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    quote_date: Mapped[dt.date] = mapped_column(Date, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_type: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    coupon: Mapped[float | None] = mapped_column(Float)
    maturity_date: Mapped[dt.date | None] = mapped_column(Date)
    price: Mapped[float | None] = mapped_column(Float)
    ytm: Mapped[float | None] = mapped_column(Float)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
