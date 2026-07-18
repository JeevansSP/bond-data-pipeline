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
    BigInteger,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Security(Base):
    """Current identifying + reference attributes for a universe security (pillar 1)."""

    __tablename__ = "securities"

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    instrument_type: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(Text, index=True)
    coupon: Mapped[float | None] = mapped_column(Float)
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


class IngestionRun(Base):
    """Audit record for a single pipeline execution against one dataset + business date."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset: Mapped[str] = mapped_column(String(48), index=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16))
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
