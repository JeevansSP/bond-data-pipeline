"""Tests for the spread-matrix tenor bucketing."""

from __future__ import annotations

import pytest

from bonds.pipelines.spread_matrix import TENOR_BUCKETS, bucket_for


def test_buckets_match_fimmdas_published_grid() -> None:
    # The product is the cell-by-cell comparison against FIMMDA, so the grids must line up.
    assert TENOR_BUCKETS == (0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15)


@pytest.mark.parametrize(
    ("tenor", "expected"),
    [
        (0.1, 0.5),
        (0.5, 0.5),
        (0.8, 1),
        (2.4, 2),
        (2.6, 3),
        # Nearest, not next-highest: the grid is uneven at the long end, so bucketing a 12-year
        # bond up to 15 stretches it three years while nearest puts it in 10, two years away.
        (12.0, 10),
        (12.6, 15),
        (40.0, 15),
    ],
)
def test_bucket_for(tenor: float, expected: float) -> None:
    assert bucket_for(tenor) == expected


def test_every_bucket_maps_to_itself() -> None:
    for bucket in TENOR_BUCKETS:
        assert bucket_for(bucket) == bucket
