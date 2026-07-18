"""Tests for the CCIL NDS-OM trade connector.

The live open-market response shape is unobservable off-hours, so these tests cover the verified
paths: market-closed gating, the "Undeployed" sentinel, and the best-effort JSON parser.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.ccil import CcilSource, parse_main

AS_OF = dt.date(2026, 7, 18)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_parse_main_handles_undeployed_sentinel() -> None:
    assert parse_main("Undeployed", "CG", AS_OF) == []


def test_parse_main_handles_non_json() -> None:
    assert parse_main("<html>error</html>", "CG", AS_OF) == []


def test_parse_main_extracts_trades_from_record_list() -> None:
    body = '{"data": [{"isin": "IN0020160035", "price": "100.24", "yield": "6.61"}]}'
    records = parse_main(body, "CG", AS_OF)
    assert len(records) == 1
    assert records[0].isin == "IN0020160035"
    assert records[0].segment == "CG"
    assert records[0].ltp == pytest.approx(100.24)
    assert records[0].lty == pytest.approx(6.61)


def test_parse_main_skips_rows_without_valid_isin() -> None:
    body = '[{"isin": "NOTANISIN", "price": "100"}, {"price": "99"}]'
    assert parse_main(body, "TB", AS_OF) == []


def _source(tmp_path: Path) -> CcilSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return CcilSource(client=ThrottledClient(settings.http), settings=settings)


@respx.mock
def test_fetch_trades_returns_empty_when_market_closed(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/individual-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    respx.post("https://www.ccilindia.com/individual-trades").mock(
        return_value=httpx.Response(200, json={"resultMarketOpenClose": "N"})
    )
    assert _source(tmp_path).fetch_trades(AS_OF) == []


@respx.mock
def test_fetch_trades_parses_when_market_open(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/individual-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    # ticker says open; every 'main' POST returns one CG-style trade row
    respx.post(
        "https://www.ccilindia.com/individual-trades",
        params={"p_p_resource_id": "ticker"},
    ).mock(return_value=httpx.Response(200, json={"resultMarketOpenClose": "Y"}))
    respx.post(
        "https://www.ccilindia.com/individual-trades",
        params={"p_p_resource_id": "main"},
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"isin": "IN0020160035", "price": 100.2, "yield": 6.6}]}
        )
    )
    records = _source(tmp_path).fetch_trades(AS_OF)
    assert len(records) == 3  # one row per Sec Type (CG/SG/TB)
    assert {r.segment for r in records} == {"CG", "SG", "TB"}
    assert (tmp_path / "raw" / "ccil" / "2026-07-18" / "CG.json").exists()
