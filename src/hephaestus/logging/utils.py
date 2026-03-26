#!/usr/bin/env python3
"""Enhanced logging utilities for ProjectHephaestus.

Standardized logging interface with configurable output formats,
multiple destinations, and context management.

Usage:
    from hephaestus.logging.utils import get_logger, setup_logging

    setup_logging(level=logging.DEBUG)
    logger = get_logger(__name__)
    logger.info("This is an info message")
"""

import json
import logging
import sys
import threading
from typing import Any

from hephaestus.constants import LOG_FORMAT

# Standard LogRecord attribute names (used to filter out extras in JSON output)
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "msg", "args"}
)


class JsonFormatter(logging.Formatter):
    """Formatter that outputs log records as JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string.

        Args:
            record: The log record to format.

        Returns:
            JSON-encoded string with timestamp, name, level, and message fields.

        """
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "name": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Include extra context fields added by ContextLogger
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                log_entry.setdefault(key, value)
        return json.dumps(log_entry, default=str)


class ContextLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Logger adapter that adds context information to log messages."""

    def __init__(self, logger: logging.Logger, context: dict[str, Any] | None = None) -> None:
        """Initialize with logger and optional context dict."""
        super().__init__(logger, context or {})
        self._context = context or {}
        self._context_lock = threading.Lock()

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        """Add context information to log messages."""
        extra = kwargs.get("extra", {})
        extra.update(self._context)
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **kwargs: Any) -> "ContextLogger":
        """Create a new logger with additional context."""
        with self._context_lock:
            new_context = self._context.copy()
        new_context.update(kwargs)
        return ContextLogger(self.logger, new_context)

    def unbind(self, *keys: str) -> "ContextLogger":
        """Remove context keys from logger."""
        with self._context_lock:
            new_context = self._context.copy()
        for key in keys:
            new_context.pop(key, None)
        return ContextLogger(self.logger, new_context)


def get_logger(
    name: str,
    level: int | None = None,
    log_file: str | None = None,
    context: dict[str, Any] | None = None,
) -> ContextLogger:
    """Get a configured logger instance with optional context.

    Args:
        name: Logger name (typically __name__)
        level: Logging level (defaults to INFO)
        log_file: Optional file to log to
        context: Optional context dictionary to include in logs

    Returns:
        Configured ContextLogger instance

    """
    logger = logging.getLogger(name)
    logger.setLevel(level or logging.INFO)

    # Prevent adding handlers multiple times
    if not logger.handlers:
        formatter = logging.Formatter(LOG_FORMAT)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # File handler (optional)
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

    return ContextLogger(logger, context)


def setup_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
    format_string: str | None = None,
    log_to_stderr: bool = False,
    json_format: bool = False,
) -> None:
    """Set up global logging configuration.

    Args:
        level: Default logging level
        log_file: Optional file to log to
        format_string: Custom log format (ignored when json_format=True)
        log_to_stderr: Whether to also log to stderr
        json_format: If True, output logs as JSON objects

    """
    formatter: logging.Formatter
    if json_format:
        formatter = JsonFormatter()
    else:
        format_string = format_string or LOG_FORMAT
        formatter = logging.Formatter(format_string)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stdout_handler]

    if log_to_stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        handlers.append(stderr_handler)

    logging.basicConfig(level=level, handlers=handlers)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logging.getLogger().addHandler(file_handler)
