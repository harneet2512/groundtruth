"""Structured logging configuration."""

from __future__ import annotations

import logging
import sys

import structlog

# Module-level flag: when True, only WARNING+ logs are emitted and ANSI
# colors are disabled.  Set via ``configure_serve_logging()`` before any
# logger is created during MCP stdio serve.
_serve_mode: bool = False


def configure_serve_logging() -> None:
    """Switch to serve-safe logging: WARNING+, no ANSI, stderr only.

    Must be called **before** any ``get_logger()`` call in the serve path
    so that ``cache_logger_on_first_use`` picks up the right config.
    """
    global _serve_mode  # noqa: PLW0603
    _serve_mode = True
    # Reset structlog so the next get_logger() applies the new config.
    structlog.reset_defaults()


def _min_level_filter(
    _logger: object, method_name: str, event_dict: dict[str, object]
) -> dict[str, object]:
    """Drop log events below WARNING when in serve mode."""
    if _serve_mode and logging.getLevelName(method_name.upper()) < logging.WARNING:
        raise structlog.DropEvent
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a configured structlog logger.

    Logs to stderr to avoid polluting stdout (used by MCP stdio transport).
    """
    processors: list[object] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _min_level_filter,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=not _serve_mode),
    ]

    structlog.configure(
        processors=processors,  # type: ignore[arg-type]
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
