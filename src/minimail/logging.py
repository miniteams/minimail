"""structlog setup with TTY detection: console renderer on a terminal, JSON otherwise."""

import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    # stderr, so stdout stays clean for command output (`search … | jq`).
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        # Not cached: a cached logger keeps the stream it first saw, even after reconfiguration.
        cache_logger_on_first_use=False,
    )
