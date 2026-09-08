"""Tests for the RBI "Full Auction Result" parser.

Fixtures are the real results tables lifted out of four live press releases (the surrounding
~130KB of RBI page chrome stripped), one per layout the parser has to handle:

    63517  G-Sec, two securities, cut-off price with the yield in a "(YTM: …)" continuation
    63461  G-Sec, a single security — the case a "header needs two names" rule silently dropped
    63501  T-Bill, three tenors, plus the weighted-average row and its "(WAY: …)" line
    63492  SDL, thirty state loans split across eight sibling tables inside a wrapper table

Every expected figure below is read off the published page, so a regression in alignment shows
up as a wrong number rather than a wrong shape.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bonds.models import RbiAuctionResultRecord
from bonds.sources.rbi import is_full_result
from bonds.sources.rbi_auction_result import parse_auction_results

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "rbi"
DATE = dt.date(2026, 9, 4)


def _parse(prid: str) -> list[RbiAuctionResultRecord]:
    return parse_auction_results(
        (FIXTURES / f"{prid}.html").read_bytes(),
        prid=prid,
        source="rbi",
        auction_date=DATE,
        auction_type="G-Sec",
    )


def _by_security(prid: str) -> dict[str, RbiAuctionResultRecord]:
    return {r.security: r for r in _parse(prid)}


def test_gsec_two_securities_align_to_their_own_columns() -> None:
    # The header has three cells and the metric rows four (a leading roman-numeral cell), so
    # keying on absolute cell indices shifted every value one security along.
    records = _by_security("63517")
    assert set(records) == {"New GS 2031", "7.71% GS 2066"}

    new = records["New GS 2031"]
    assert new.notified_amount_cr == pytest.approx(21000.0)
    assert new.bids_received_count == 137
    assert new.bids_received_amount_cr == pytest.approx(49886.0)
    assert new.bids_accepted_count == 76
    assert new.bids_accepted_amount_cr == pytest.approx(20996.823)
    assert new.cut_off_price == pytest.approx(100.0)
    assert new.cut_off_yield == pytest.approx(6.53)
    assert new.wavg_price == pytest.approx(100.06)
    assert new.wavg_yield == pytest.approx(6.5163)
    assert new.partial_allotment_pct == pytest.approx(35.1939)

    old = records["7.71% GS 2066"]
    assert old.notified_amount_cr == pytest.approx(11000.0)
    assert old.cut_off_yield == pytest.approx(7.6618)
    assert old.partial_allotment_pct == pytest.approx(84.8966)


def test_single_security_auction_is_parsed() -> None:
    records = _parse("63461")
    assert len(records) == 1
    only = records[0]
    assert only.security == "6.94% GS 2036"
    assert only.notified_amount_cr == pytest.approx(34000.0)
    assert only.bids_accepted_amount_cr == pytest.approx(33995.602)
    assert only.cut_off_yield == pytest.approx(6.9136)
    assert only.wavg_yield == pytest.approx(6.9122)


def test_tbill_layout_including_weighted_average() -> None:
    records = _by_security("63501")
    assert set(records) == {"91-Day", "182-Day", "364-Day"}
    for tenor, notified, cut_off, way in (
        ("91-Day", 9000.0, 5.2599, 5.2525),
        ("182-Day", 8000.0, 5.6588, 5.6189),
        ("364-Day", 7000.0, 5.909, 5.8887),
    ):
        record = records[tenor]
        assert record.notified_amount_cr == pytest.approx(notified)
        assert record.cut_off_yield == pytest.approx(cut_off)
        assert record.wavg_yield == pytest.approx(way)


def test_sdl_layout_spans_sibling_tables() -> None:
    # An SDL release has one table per group of four securities, all nested inside a wrapper
    # table that also mentions "notified amount". Taking the first match read the wrapper and
    # produced totals; the parser keeps only innermost tables and concatenates.
    records = _by_security("63492")
    assert len(records) == 30

    assam = records["ASSAM SGS 2046"]
    assert assam.notified_amount_cr == pytest.approx(1000.0)
    assert assam.bids_received_count == 54
    assert assam.bids_accepted_amount_cr == pytest.approx(971.994)
    # SDL releases publish yield and price on separate rows rather than as a parenthetical.
    assert assam.cut_off_yield == pytest.approx(7.7585)
    assert assam.cut_off_price == pytest.approx(98.61)
    assert assam.partial_allotment_pct == pytest.approx(97.1994)
    assert assam.tenor_note is not None and "Re-issue" in assam.tenor_note


def test_non_competitive_bids_do_not_overwrite_competitive_figures() -> None:
    # Non-competitive sections repeat the same "(i) No." / "(ii) Amount" sub-row labels. The
    # 63461 release has both; the competitive figures must survive.
    only = _parse("63461")[0]
    assert only.bids_received_count == 383
    assert only.bids_received_amount_cr == pytest.approx(82151.762)


def test_bid_cover_ratio_is_derived() -> None:
    new = _by_security("63517")["New GS 2031"]
    assert new.bid_cover_ratio == pytest.approx(49886.0 / 21000.0)


def test_bid_cover_ratio_is_none_without_a_notified_amount() -> None:
    record = RbiAuctionResultRecord(prid="1", security="X", source="rbi")
    assert record.bid_cover_ratio is None


def test_a_page_with_no_results_table_yields_nothing() -> None:
    # An announcement rather than a result. Returning nothing lets the caller record a skip; a
    # partial record passed off as complete would be worse.
    assert (
        parse_auction_results(b"<html><body><p>no tables</p></body></html>", prid="0", source="rbi")
        == []
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Government Stock - Full Auction Results", True),
        ("Treasury Bills: Full Auction Result", True),
        ("State Government Securities - Full Auction Result", True),
        # The companion cut-off release is a one-line summary — fetching it wastes a request.
        ("Government Stock - Auction Results: Cut-off", False),
        ("91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off", False),
        # Different instruments entirely: no notified/cut-off table to read.
        ("Result of Underwriting Auction conducted on September 04, 2026", False),
        ("Result of Buyback Auction of Government of India Dated Securities", False),
        # A calendar entry, not a result.
        ("Auction of State Government Securities", False),
    ],
)
def test_is_full_result(title: str, expected: bool) -> None:
    assert is_full_result(title) is expected


def test_continuation_row_stays_aligned_when_a_cell_is_blank() -> None:
    """A "(YTM: …)" row is right-aligned under the security columns.

    Regression: the continuation branch filtered out empty cells before mapping them, which
    collapsed the columns and handed one security's yield to another whenever a release left a
    cell blank — a fresh issue quoted on yield alone, or a devolved line. The metric rows were
    already positional; this branch was not.
    """
    html = b"""<html><body><table>
    <tr><td>Auction Results</td><td>SEC A</td><td>SEC B</td></tr>
    <tr><td>I.</td><td>Notified Amount</td><td></td><td>500</td></tr>
    <tr><td>III.</td><td>Cut-off price (Rs) / Yield</td><td></td><td>99.5</td></tr>
    <tr><td></td><td></td><td></td><td>(YTM: 7.1234%)</td></tr>
    </table></body></html>"""
    records = {r.security: r for r in parse_auction_results(html, prid="X", source="rbi")}
    # SEC A has no figures at all and is dropped; the yield must land on SEC B, not on SEC A.
    assert set(records) == {"SEC B"}
    assert records["SEC B"].cut_off_price == pytest.approx(99.5)
    assert records["SEC B"].cut_off_yield == pytest.approx(7.1234)


def test_all_fixture_yields_survive_the_alignment_rule() -> None:
    # The right-alignment fix must not move any figure in the four real releases.
    for prid, security, cut_off, wavg in (
        ("63517", "New GS 2031", 6.53, 6.5163),
        ("63517", "7.71% GS 2066", 7.6618, 7.657),
        ("63501", "364-Day", 5.909, 5.8887),
        ("63461", "6.94% GS 2036", 6.9136, 6.9122),
    ):
        record = {r.security: r for r in _parse(prid)}[security]
        assert record.cut_off_yield == pytest.approx(cut_off)
        assert record.wavg_yield == pytest.approx(wavg)
