"""Tests for business-day helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.calendar import business_days, is_weekend


def test_is_weekend() -> None:
    assert is_weekend(dt.date(2026, 7, 18))  # Saturday
    assert is_weekend(dt.date(2026, 7, 19))  # Sunday
    assert not is_weekend(dt.date(2026, 7, 17))  # Friday


def test_business_days_skips_weekends() -> None:
    days = list(business_days(dt.date(2026, 7, 16), dt.date(2026, 7, 20)))
    assert days == [dt.date(2026, 7, 16), dt.date(2026, 7, 17), dt.date(2026, 7, 20)]


def test_business_days_single_day() -> None:
    assert list(business_days(dt.date(2026, 7, 17), dt.date(2026, 7, 17))) == [dt.date(2026, 7, 17)]


def test_business_days_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="after end"):
        list(business_days(dt.date(2026, 7, 20), dt.date(2026, 7, 10)))
