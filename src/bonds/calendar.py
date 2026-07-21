"""Business-day helpers for backfill.

We deliberately do NOT hard-code an Indian market holiday calendar: FBIL returns HTTP 500 for
non-publishing days, so the ingestion layer treats a missing file as "skip this day". This module
only filters Sundays. Saturdays are *included*: Indian markets have held rare Saturday sessions
(e.g. the Union Budget day, Sat 2020-02-01), and skipping them would silently lose that data
forever — a normal (closed) Saturday just costs one cheap skipped request per source.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

_SUNDAY = 6


def is_sunday(day: dt.date) -> bool:
    """Return ``True`` for Sunday (the only day filtered from backfills)."""
    return day.weekday() == _SUNDAY


def business_days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Yield candidate market days (Mon-Sat) from ``start`` to ``end`` inclusive, oldest first.

    Args:
        start: First date (inclusive).
        end: Last date (inclusive).

    Yields:
        Each non-Sunday day in ``[start, end]``.

    Raises:
        ValueError: If ``start`` is after ``end``.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    day = start
    while day <= end:
        if not is_sunday(day):
            yield day
        day += dt.timedelta(days=1)
