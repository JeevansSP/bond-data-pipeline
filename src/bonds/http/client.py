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
    ) -> httpx.Response:
        """GET ``url`` with throttling and retries; raise on non-2xx after retries.

        Args:
            url: Absolute URL to fetch.
            params: Optional query parameters.
            headers: Optional per-request headers merged over the client defaults.

        Returns:
            The successful :class:`httpx.Response`.

        Raises:
            httpx.HTTPStatusError: On a non-retryable 4xx, or after exhausting retries.
        """

        @retry(
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatusError)),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(self._settings.max_retries),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._throttle()
            response = self._client.get(url, params=params, headers=headers)
            if response.status_code in _RETRYABLE_STATUS:
                logger.warning("http.retryable_status", url=url, status=response.status_code)
                raise RetryableStatusError(
                    f"retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response

        return _do()
