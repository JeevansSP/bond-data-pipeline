"""Tests for business-day helpers."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.calendar import business_days, is_sunday


def test_is_sunday() -> None:
    assert is_sunday(dt.date(2026, 7, 19))  # Sunday
    assert not is_sunday(dt.date(2026, 7, 18))  # Saturday — a possible special session
    assert not is_sunday(dt.date(2026, 7, 17))  # Friday


def test_business_days_skips_sundays_keeps_saturdays() -> None:
    # Saturdays stay in: Indian markets have held special Saturday sessions (Budget day
    # Sat 2020-02-01); a normal closed Saturday is just a cheap skipped request.
    days = list(business_days(dt.date(2026, 7, 16), dt.date(2026, 7, 20)))
    assert days == [
        dt.date(2026, 7, 16),  # Thu
        dt.date(2026, 7, 17),  # Fri
        dt.date(2026, 7, 18),  # Sat
        dt.date(2026, 7, 20),  # Mon (Sunday 19th filtered)
    ]


def test_business_days_single_day() -> None:
    assert list(business_days(dt.date(2026, 7, 17), dt.date(2026, 7, 17))) == [dt.date(2026, 7, 17)]


def test_business_days_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="after end"):
        list(business_days(dt.date(2026, 7, 20), dt.date(2026, 7, 10)))
