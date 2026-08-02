"""Tests for the CCIL historical-trades connector (AES, CSV aggregation, fetch)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.models import InstrumentType, TradeRecord
from bonds.sources import SourceError
from bonds.sources.ccil_historical import (
    CcilHistoricalTradesSource,
    _as_date,
    _instrument_segment,
    _parse_row,
    _time_key,
    aggregate_trades,
    derive_securities,
    derive_security,
    encrypt_date,
)

_HEADER = (
    "Trade,Trade Time,ISIN,Security Description,Face Value,Trade Price,YTM/Yield,Trade Indicator\n"
)

_CSV = (
    "Trade,Trade Time,ISIN,Security Description,Face Value,Trade Price,YTM/Yield,Trade Indicator\n"
    "17-07-2026,16:59:59,IN0020260025,06.94 GOVT. STOCK 2036,50000000.000,101.1150,6.7806,NRML\n"
    "17-07-2026,16:58:00,IN0020260025,06.94 GOVT. STOCK 2036,100000000.000,101.2000,6.7500,NRML\n"
    "17-07-2026,16:00:00,IN2220190127,06.97 MAHARASHTRA SGS 2028,7500000.000,101.8000,6.2315,NRML\n"
    "17-07-2026,15:00:00,IN0020260099,DTB 15012027,25000000.000,98.5000,5.5000,NRML\n"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_encrypt_date_matches_site_ciphertext() -> None:
    # Reproduces CCIL's encryptDetails byte-for-byte (verified against the live page).
    assert encrypt_date("16/07/2026") == "6AUyCd+B1T3jyWBfbj3Kng=="


def test_aggregate_trades_vwap_count_and_segment() -> None:
    by_isin = {r.isin: r for r in aggregate_trades(_CSV, source="ccil")}
    assert set(by_isin) == {"IN0020260025", "IN2220190127", "IN0020260099"}

    gsec = by_isin["IN0020260025"]
    assert gsec.segment == "GSEC"
    assert gsec.no_of_trades == 2
    assert gsec.trade_value == pytest.approx(150_000_000.0)  # sum of face
    assert gsec.ltp == pytest.approx(101.1150)  # latest trade (16:59:59)
    # VWAP = (101.1150*50M + 101.2000*100M) / 150M
    assert gsec.wap == pytest.approx((101.1150 * 50e6 + 101.2000 * 100e6) / 150e6)

    assert by_isin["IN2220190127"].segment == "SDL"  # "SGS" = State Government Securities
    assert by_isin["IN0020260099"].segment == "TBILL"  # "DTB" = Discounted Treasury Bill


def test_aggregate_empty_or_error_returns_empty() -> None:
    assert aggregate_trades("", source="ccil") == []
    assert aggregate_trades("Undeployed", source="ccil") == []


def test_last_trade_uses_numeric_time_not_string_compare() -> None:
    # Unpadded "9:05:00" lexically sorts AFTER "16:00:00"; the numeric key must rank 16:00 last.
    csv_text = _HEADER + (
        "17-07-2026,9:05:00,IN0020260025,06.94 GS 2036,50000000.000,101.0000,6.80,NRML\n"
        "17-07-2026,16:00:00,IN0020260025,06.94 GS 2036,50000000.000,102.0000,6.70,NRML\n"
    )
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.ltp == pytest.approx(102.0000)  # the 16:00 close, not the 9:05 morning print
    assert rec.lty == pytest.approx(6.70)


def test_zero_and_missing_values_excluded_from_vwap_but_counted() -> None:
    csv_text = _HEADER + (
        "17-07-2026,10:00:00,IN0020260025,06.94 GS 2036,50000000.000,0.0000,6.80,NRML\n"  # price 0
        "17-07-2026,11:00:00,IN0020260025,06.94 GS 2036,50000000.000,101.0000,6.70,NRML\n"  # valid
        "17-07-2026,12:00:00,IN0020260025,06.94 GS 2036,,101.5000,6.60,NRML\n"  # face missing
    )
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.no_of_trades == 3  # every trade counts
    assert rec.trade_value == pytest.approx(50_000_000.0)  # only the valid row's face
    assert rec.wap == pytest.approx(101.0000)  # zero/missing rows don't pollute the VWAP
    assert rec.ltp == pytest.approx(101.0000)  # ltp = last VALID-price trade (11:00), not 12:00


def test_group_with_no_valid_prices_yields_none_wap() -> None:
    csv_text = _HEADER + "17-07-2026,10:00:00,IN2220190127,SGS 2028,50000000.000,0,6,NRML\n"
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.no_of_trades == 1
    assert rec.wap is None and rec.ltp is None  # no ZeroDivisionError
    assert rec.way == pytest.approx(6.0)  # the valid yield leg still informs the weighted yield


def test_column_shift_row_is_rejected() -> None:
    # A stray unquoted comma in the description makes this row 9 fields; it must be dropped, not
    # parsed with shifted columns. The clean 8-field row still comes through.
    csv_text = _HEADER + (
        "17-07-2026,16:00:00,IN0020260099,GOI FRB, 2033,50000000.000,101.0,6.8,NRML\n"  # 9 fields
        "17-07-2026,16:00:00,IN0020260025,06.94 GS 2036,50000000.000,101.0,6.8,NRML\n"  # 8 fields
    )
    isins = {r.isin for r in aggregate_trades(csv_text, source="ccil")}
    assert isins == {"IN0020260025"}


@pytest.mark.parametrize(
    ("desc", "isin", "segment"),
    [
        # Central government (IN00...) — sub-type comes from the description.
        ("06.94 GOVT. STOCK 2036", "IN0020260025", "GSEC"),
        ("DTB 15012027", "IN0020260099", "TBILL"),
        ("CMB 91 DAYS 2026", "IN0020260107", "TBILL"),
        ("91 TBILL MATURING 08/03/2002", "IN002001X019", "TBILL"),  # old bare-"TBILL" naming
        ("SGB 2028 SR-II", "IN0020280010", "SGB"),
        ("GOVT. STOCK 12DEC2041C", "IN001241C032", "STRIPS"),  # coupon strip
        ("07.09 GOVT. STOCK 25NOV2074P", "IN0020740015", "STRIPS"),  # principal strip
        ("GOVT. STOCK 02JAN2024 C", "IN000124C015", "STRIPS"),  # old space-before-C spelling
        ("06.94 GOVT. STOCK 2036", "IN0020260025", "GSEC"),  # year suffix, not a strip
        (None, "IN0020260025", "GSEC"),
        ("", "IN0020260025", "GSEC"),
        # State issuers (non-IN00 prefix) — SDL by ISIN regardless of description spelling.
        ("06.97 MAHARASHTRA SGS 2028", "IN2220190127", "SDL"),
        ("07.20 KARNATAKA SDL 2030", "IN1920300011", "SDL"),
        ("11.50% MAHARASHTRA S.D. 2010", "IN2219900015", "SDL"),  # old "S.D." naming
        ("9.40% PUNJAB GOVT. STOCK 2011", "IN2820110019", "SDL"),  # state loan named "GOVT. STOCK"
    ],
)
def test_instrument_segment_classification(desc: str | None, isin: str, segment: str) -> None:
    assert _instrument_segment(desc, isin) == segment


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("09:05:00", (9, 5, 0)),
        ("9:05:00", (9, 5, 0)),  # unpadded parses the same
        ("16:59:59", (16, 59, 59)),
        ("garbage", (-1, -1, -1)),
        ("aa:bb:cc", (-1, -1, -1)),  # right shape, non-numeric
        ("10:20", (-1, -1, -1)),  # wrong shape
    ],
)
def test_time_key(value: str, expected: tuple[int, int, int]) -> None:
    assert _time_key(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("17-07-2026", dt.date(2026, 7, 17)),  # %d-%m-%Y
        ("2026-07-17", dt.date(2026, 7, 17)),  # %Y-%m-%d
        ("17-Jul-2026", dt.date(2026, 7, 17)),  # %d-%b-%Y
        ("not-a-date", None),
    ],
)
def test_as_date_format_fallbacks(value: str, expected: dt.date | None) -> None:
    assert _as_date(value) == expected


def test_parse_row_rejects_short_and_bad_isin() -> None:
    assert _parse_row(["17-07-2026", "16:00:00", "IN0020260025"]) is None  # too few fields
    assert (
        _parse_row(["17-07-2026", "16:00:00", "NOTANISIN", "d", "1000", "100", "6", "NRML"]) is None
    )  # ISIN doesn't start with IN / wrong length


def test_derive_security_tbill_parses_maturity_and_zero_coupon() -> None:
    sec = derive_security("IN0020260099", "DTB 15012027", "TBILL")
    assert sec is not None
    assert sec.instrument_type is InstrumentType.TBILL
    assert sec.coupon == 0.0 and sec.interest_type == "ZERO"  # validator canonicalizes
    assert sec.maturity_date == dt.date(2027, 1, 15)
    assert sec.face_value == 100.0  # sovereign Rs 100 par convention
    assert sec.issuer == "Government of India" and sec.source == "ccil"


def test_derive_security_old_tbill_slash_date() -> None:
    sec = derive_security("IN002001X019", "091 DTB MATURING 07/06/2002", "TBILL")
    assert sec is not None and sec.maturity_date == dt.date(2002, 6, 7)


def test_derive_security_strip_and_gsec_coupon() -> None:
    strip = derive_security("IN001241C032", "GOVT. STOCK 12DEC2041C", "STRIPS")
    assert strip is not None
    assert strip.instrument_type is InstrumentType.STRIPS
    assert strip.coupon == 0.0 and strip.maturity_date == dt.date(2041, 12, 12)


@pytest.mark.parametrize(
    ("desc", "maturity"),
    [
        ("GS02JAN2011C", dt.date(2011, 1, 2)),  # date glued to "GS" (no space/boundary)
        ("07.72 GOVT. STOCK GS15JUN2049P", dt.date(2049, 6, 15)),
        ("GOVT. STOCK 12DEC2041C", dt.date(2041, 12, 12)),  # spaced form still works
    ],
)
def test_derive_security_strip_maturity_glued_and_spaced(desc: str, maturity: dt.date) -> None:
    sec = derive_security("IN000111C012", desc, "STRIPS")
    assert sec is not None and sec.maturity_date == maturity

    gsec = derive_security("IN0020260025", "06.94 GOVT. STOCK 2036", "GSEC")
    assert gsec is not None
    assert gsec.coupon == pytest.approx(6.94)
    assert gsec.maturity_date is None  # year-only in the feed -> left unknown


def test_derive_security_sdl_state_issuer_and_sgb() -> None:
    sdl = derive_security("IN2220190127", "06.97 MAHARASHTRA SGS 2028", "SDL")
    assert sdl is not None and sdl.instrument_type is InstrumentType.SDL
    assert sdl.issuer == "State Government (MAHARASHTRA)"
    assert sdl.face_value == 100.0
    sgb = derive_security("IN0020210228", "02.50 SGB 2029 SERIES VIII", "SGB")
    assert sgb is not None and sgb.instrument_type is InstrumentType.SGB
    assert sgb.coupon == pytest.approx(2.50)
    assert sgb.face_value is None  # gold-linked issue price, not the Rs 100 par convention


def test_derive_securities_dedupes_by_isin() -> None:
    def _t(isin: str, desc: str, seg: str) -> TradeRecord:
        return TradeRecord(
            isin=isin, trade_date=dt.date(2026, 7, 17), source="ccil", segment=seg, descriptor=desc
        )

    secs = derive_securities(
        [
            _t("IN0020260099", "DTB 15012027", "TBILL"),
            _t("IN0020260099", "DTB 15012027", "TBILL"),  # dup ISIN
            _t("IN0020260025", "06.94 GOVT. STOCK 2036", "GSEC"),
        ]
    )
    assert {s.isin for s in secs} == {"IN0020260099", "IN0020260025"}


@respx.mock
def test_html_challenge_reprimes_then_succeeds(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    # First POST returns an Akamai challenge page; after a re-prime the retry returns the CSV.
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        side_effect=[
            httpx.Response(200, text="<!DOCTYPE html><html><body>Access Denied</body></html>"),
            httpx.Response(200, text=_CSV),
        ]
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    records = source.fetch_trades(dt.date(2026, 7, 17))
    assert {r.segment for r in records} == {"GSEC", "SDL", "TBILL"}


@respx.mock
def test_persistent_html_challenge_raises(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html><body>Access Denied</body></html>")
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    with pytest.raises(SourceError, match="non-CSV"):
        source.fetch_trades(dt.date(2026, 7, 17))


@respx.mock
def test_fetch_trades_end_to_end(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text=_CSV)
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    records = source.fetch_trades(dt.date(2026, 7, 17))
    assert {r.segment for r in records} == {"GSEC", "SDL", "TBILL"}
    assert (tmp_path / "raw" / "ccil" / "historical_trades_2026-07-17_2026-07-17.csv").exists()


class TestRepairPriceYield:
    """CCIL occasionally lands price/yield transposed; the parser must repair or discard."""

    def test_transposed_columns_are_swapped_back(self) -> None:
        # Real example (IN0020140029, 2014-06-24): price 8.48 / yield 104.2283.
        csv = _HEADER + (
            "24-06-2014,10:00:00,IN0020140029,08.27 GOVT. STOCK 2020(WR),50000000,8.48,104.2283,O\n"
        )
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.ltp == pytest.approx(104.2283)
        assert rec.lty == pytest.approx(8.48)
        assert rec.wap == pytest.approx(104.2283)
        assert rec.way == pytest.approx(8.48)

    def test_double_garbled_row_drops_both_legs(self) -> None:
        # Real example (IN002012X024, 2012-08-22): price 83.93 and yield 98.43 — verified in
        # every landed occurrence that the "plausible" price cell is ALSO wrong (the true price,
        # 98.94-style, sits in the yield column). Neither leg can be trusted -> record the trade
        # with no price and no yield rather than a confidently-wrong price.
        csv = (
            _HEADER
            + "22-08-2012,10:00:00,IN002012X024,091 DTB 02112012,50000000,83.9307,98.4262,O\n"
        )
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.no_of_trades == 1
        assert rec.ltp is None and rec.wap is None
        assert rec.lty is None and rec.way is None

    def test_yield_in_price_column_is_reclassified(self) -> None:
        # Real example (11-01-2019 "NI GOVT. STOCK 2029"): when-issued trades quoted in yield —
        # price=7.25, ytm=0.0. 7.25 must not enter wap/ltp as a price; it IS the yield.
        csv = _HEADER + "11-01-2019,10:00:00,IN0020180488,NI GOVT. STOCK 2029,50000000,7.25,0.0,O\n"
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.ltp is None and rec.wap is None
        assert rec.way == pytest.approx(7.25)  # yield-only print still informs the weighted yield

    def test_deep_discount_strips_low_price_is_kept(self) -> None:
        # A 2040 principal strip legitimately trades at 7.79 with a ~9% yield — the low-price
        # reclassification must NOT fire for STRIPS.
        csv = _HEADER + "20-10-2011,10:00:00,IN0020110066,GS02JUL2040C,50000000,7.79,9.09,O\n"
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.segment == "STRIPS"
        assert rec.ltp == pytest.approx(7.79)
        assert rec.lty == pytest.approx(9.09)

    def test_capital_indexed_negative_yield_is_kept(self) -> None:
        # Real print: 6% capital-indexed bond above redemption near maturity, yield -16.73.
        row = "15-05-2002,10:00:00,IN0020020017,6% CAPITAL INDEXED BONDS 2002,50000000,121.4,-16.73,O\n"  # noqa: E501
        rec = aggregate_trades(_HEADER + row, source="ccil")[0]
        assert rec.ltp == pytest.approx(121.4)
        assert rec.lty == pytest.approx(-16.73)

    def test_absurd_negative_yield_is_discarded(self) -> None:
        csv = (
            _HEADER
            + "09-12-2013,10:00:00,IN2220190127,09.37 GUJARAT SDL 2023,50000000,101.5,-50.76,O\n"
        )
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.ltp == pytest.approx(101.5)
        assert rec.lty is None

    def test_plausible_rows_pass_through_untouched(self) -> None:
        csv = (
            _HEADER
            + "17-07-2026,10:00:00,IN0020260025,07.10 GOVT. STOCK 2029,50000000,101.115,7.08,O\n"
        )
        rec = aggregate_trades(csv, source="ccil")[0]
        assert rec.ltp == pytest.approx(101.115)
        assert rec.lty == pytest.approx(7.08)


@pytest.mark.parametrize(
    ("desc", "issuer"),
    [
        ("05.60 ANDHRA PRADESH SDL 2014", "State Government (ANDHRA PRADESH)"),
        ("09.37 MAHARASHTRA S.D. 2023", "State Government (MAHARASHTRA)"),
        ("06.97 MAHARASHTRA SGS 2028", "State Government (MAHARASHTRA)"),
        ("07.33 JAMMU & KASHMIR SDL 2029", "State Government (JAMMU & KASHMIR)"),
        # Legacy marker drift (all real formats from the landed history).
        ("8.84% Maharashtra GS 2022", "State Government (MAHARASHTRA)"),
        ("7.83% GUJARAT G.S. 2012", "State Government (GUJARAT)"),
        ("7.39% MAHARASHTRA G S 2015", "State Government (MAHARASHTRA)"),
        ("9.40% PUNJAB GOVT. STOCK 2011", "State Government (PUNJAB)"),
        ("12.47% PUNJAB GOVT.STOCK 2009", "State Government (PUNJAB)"),
        ("13.05% ORISSA GOVT LOAN 2007", "State Government (ODISHA)"),
        ("12.50% MEGHALAYA SDL2008", "State Government (MEGHALAYA)"),
        ("8.33%GUJARAT GS 2020", "State Government (GUJARAT)"),
        ("7.80% A.P. SDL(APL) 2012", "State Government (ANDHRA PRADESH)"),
        # Alias canonicalisation: abbreviations and typos collapse to one issuer per state.
        ("10.35%  A. P. SDL 2011", "State Government (ANDHRA PRADESH)"),
        ("8.29% Ar.Pr. GS 2020", "State Government (ARUNACHAL PRADESH)"),
        ("8.58% W.Bengal GS 2020", "State Government (WEST BENGAL)"),
        ("8.00% MAHRASTRA GS 2018", "State Government (MAHARASHTRA)"),
        ("7.95% UTRANCHAL GS 2016", "State Government (UTTARAKHAND)"),
        ("6.95% TAMILNADU GS 2018", "State Government (TAMIL NADU)"),
        ("10.50% JAMMU & KASHMIR 2011", "State Government (JAMMU & KASHMIR)"),
        ("garbage with no state marker", None),
        (None, None),
    ],
)
def test_sdl_issuer_derivation(desc: str | None, issuer: str | None) -> None:
    rec = derive_security("IN1020090015", desc, "SDL")
    assert rec is not None
    assert rec.issuer == issuer


def test_sgb_issuer_is_goi() -> None:
    rec = derive_security("IN0020260033", "02.50 SGB 2026 SERIES XIV 17 18 FV 2881", "SGB")
    assert rec is not None
    assert rec.issuer == "Government of India"
