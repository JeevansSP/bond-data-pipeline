"""Tests for the throttled, retrying HTTP client."""

from __future__ import annotations

import httpx
import pytest
import respx

from bonds.config import HttpSettings
from bonds.http import ThrottledClient

URL = "https://example.test/data"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make throttle + tenacity backoff instant so tests stay fast."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def _settings() -> HttpSettings:
    return HttpSettings(min_interval_seconds=0.0, max_retries=3, timeout_seconds=5.0)


@respx.mock
def test_get_returns_successful_response() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    with ThrottledClient(_settings()) as client:
        response = client.get(URL)
    assert response.json() == {"ok": True}


@respx.mock
def test_get_retries_transient_500_then_succeeds() -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, text="ok")])
    with ThrottledClient(_settings()) as client:
        response = client.get(URL)
    assert response.text == "ok"
    assert route.call_count == 2


@respx.mock
def test_get_raises_on_non_retryable_404() -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))
    with ThrottledClient(_settings()) as client, pytest.raises(httpx.HTTPStatusError):
        client.get(URL)


@respx.mock
def test_get_exhausts_retries_and_raises() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with ThrottledClient(_settings()) as client, pytest.raises(httpx.HTTPStatusError):
        client.get(URL)
    assert route.call_count == 3  # max_retries
