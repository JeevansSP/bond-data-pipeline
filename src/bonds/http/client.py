"""A polite, resilient HTTP client wrapper around httpx.

Enforces a minimum interval between requests (per client instance) and retries transient
failures with exponential backoff. Government/​exchange sources here are Akamai-protected and
rate-limit hard, so throttling is a first-class concern rather than an afterthought.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bonds.config import HttpSettings, get_settings
from bonds.logging import get_logger

logger = get_logger(__name__)

# Status codes worth retrying: transient upstream / edge throttling.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableStatusError(httpx.HTTPStatusError):
    """Raised for a retryable HTTP status so tenacity can back off and try again."""


class ThrottledClient:
    """Synchronous HTTP client with per-instance rate limiting and retries.

    Use as a context manager so the underlying connection pool is closed cleanly::

        with ThrottledClient() as client:
            resp = client.get("https://example.com/data.json")
    """

    def __init__(self, settings: HttpSettings | None = None) -> None:
        self._settings = settings or get_settings().http
        self._min_interval = self._settings.min_interval_seconds
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=self._settings.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._settings.user_agent},
        )

    def __enter__(self) -> Self:
        """Enter the context manager, returning this client."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client on context exit."""
        self.close()

    def close(self) -> None:
        """Close the underlying httpx client and its connection pool."""
        self._client.close()

    def __del__(self) -> None:
        """Best-effort GC cleanup: release the connection pool if never explicitly closed."""
        client = getattr(self, "_client", None)
        if client is not None:
            client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        no_retry_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """GET ``url`` with throttling and retries; raise on non-2xx after retries.

        ``no_retry_statuses`` opts specific codes out of retrying — e.g. FBIL returns 500 for every
        non-publishing day, an expected case that shouldn't burn the backoff budget.
        """
        return self._send("GET", url, params=params, headers=headers, no_retry=no_retry_statuses)

    def post(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST ``url`` with a form body, throttling and retries (e.g. Liferay portlet calls)."""
        return self._send("POST", url, params=params, headers=headers, data=data)

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        no_retry: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        @retry(
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatusError)),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(self._settings.max_retries),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._throttle()
            response = self._client.request(method, url, params=params, headers=headers, data=data)
            if response.status_code in _RETRYABLE_STATUS and response.status_code not in no_retry:
                logger.warning("http.retryable_status", url=url, status=response.status_code)
                raise RetryableStatusError(
                    f"retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response

        return _do()
