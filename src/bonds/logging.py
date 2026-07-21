"""Structured logging setup via structlog."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

_configured = False


def configure_logging(*, level: str = "INFO", json: bool = False) -> None:
    """Configure structlog once for the process.

    Args:
        level: Standard logging level name (e.g. ``"INFO"``, ``"DEBUG"``). An unknown name
            falls back to ``INFO`` rather than crashing every command on a typo'd env var.
        json: Emit JSON lines (for cron/prod) instead of human-friendly console output.

    Only the first call configures; later calls (including a different ``level``) are no-ops.
    """
    global _configured
    if _configured:
        return

    level = level.upper()
    if level not in logging.getLevelNamesMapping():
        logging.getLogger(__name__).warning("unknown log level %r; falling back to INFO", level)
        level = "INFO"

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
