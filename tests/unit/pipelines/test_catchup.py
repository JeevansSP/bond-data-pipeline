"""Tests for the catch-up date-window logic."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.pipelines.catchup import bounded_start

AS_OF = dt.date(2026, 7, 17)


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        # Never ingested -> start at the floor (as_of - max_gap_days), not the beginning of time.
        (None, dt.date(2026, 6, 17)),
        # Ran yesterday -> resume today.
        (dt.date(2026, 7, 16), dt.date(2026, 7, 17)),
        # Ran a few days ago -> resume the day after (gap-fill the missed days).
        (dt.date(2026, 7, 13), dt.date(2026, 7, 14)),
        # Already ran today -> start is tomorrow, i.e. > as_of => "nothing to do".
        (dt.date(2026, 7, 17), dt.date(2026, 7, 18)),
        # Long outage beyond the cap -> clamp to the floor (runaway-backfill guard).
        (dt.date(2026, 1, 1), dt.date(2026, 6, 17)),
    ],
)
def test_bounded_start(anchor: dt.date | None, expected: dt.date) -> None:
    assert bounded_start(anchor, as_of=AS_OF, max_gap_days=30) == expected


def test_bounded_start_already_current_is_after_as_of() -> None:
    # The catch-up treats start > as_of as "no gap"; assert the boundary explicitly.
    assert bounded_start(AS_OF, as_of=AS_OF, max_gap_days=30) > AS_OF
