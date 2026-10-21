"""Logging configuration. Logs go to stderr, because stdout carries the MCP protocol in stdio mode."""

from __future__ import annotations

import logging
import sys

import structlog

QUIET_LOGGERS = ("httpx", "mcp", "uvicorn.access")


def configure_logging(level: str) -> None:
    numeric = logging.getLevelNamesMapping()[level]
    logging.basicConfig(stream=sys.stderr, level=numeric, format="%(message)s", force=True)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
