"""Business-day helpers for backfill.

We deliberately do NOT hard-code an Indian market holiday calendar: FBIL returns HTTP 500 for
non-publishing days, so the ingestion layer treats a missing file as "skip this day". This module
only filters obvious weekends to avoid pointless requests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

_SATURDAY = 5


def is_weekend(day: dt.date) -> bool:
    """Return ``True`` for Saturday/Sunday."""
    return day.weekday() >= _SATURDAY


def business_days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Yield weekdays from ``start`` to ``end`` inclusive, oldest first.

    Args:
        start: First date (inclusive).
        end: Last date (inclusive).

    Yields:
        Each weekday in ``[start, end]``.

    Raises:
        ValueError: If ``start`` is after ``end``.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    day = start
    while day <= end:
        if not is_weekend(day):
            yield day
        day += dt.timedelta(days=1)
