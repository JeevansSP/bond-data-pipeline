"""Source-agnostic domain records produced by connectors and consumed by pipelines."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
