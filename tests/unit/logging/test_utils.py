#!/usr/bin/env python3
"""Tests for logging utilities."""

import logging
import threading
from pathlib import Path

import pytest

from hephaestus.logging.utils import (
    ContextLogger,
    _configured_loggers,
    get_logger,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging_registry() -> None:  # type: ignore[misc]
    """Reset the handler registry and remove test loggers after each test."""
    yield  # type: ignore[misc]
    # Teardown: remove any test loggers we created
    for name in list(_configured_loggers):
        if name.startswith("test."):
            _configured_loggers.pop(name, None)
            logger = logging.getLogger(name)
            logger.handlers.clear()


class TestGetLogger:
    """Tests for get_logger function."""

    def test_returns_context_logger(self) -> None:
        """get_logger returns a ContextLogger instance."""
        logger = get_logger("test.module")
        assert isinstance(logger, ContextLogger)

    def test_default_level_is_info(self) -> None:
        """Logger defaults to INFO level."""
        logger = get_logger("test.default_level")
        assert logger.logger.level == logging.INFO

    def test_custom_level(self) -> None:
        """Logger respects custom level argument."""
        logger = get_logger("test.custom_level", level=logging.DEBUG)
        assert logger.logger.level == logging.DEBUG

    def test_with_context(self) -> None:
        """Logger stores supplied context."""
        ctx = {"request_id": "abc123"}
        logger = get_logger("test.context", context=ctx)
        assert logger._context == ctx

    def test_with_log_file(self, tmp_path: Path) -> None:
        """Logger creates file handler when log_file is given."""
        log_file = str(tmp_path / "test.log")
        logger = get_logger("test.file_handler", log_file=log_file)
        handler_types = [type(h) for h in logger.logger.handlers]
        assert logging.FileHandler in handler_types

    def test_no_duplicate_handlers_on_repeated_calls(self) -> None:
        """Calling get_logger twice with the same name adds only one StreamHandler."""
        get_logger("test.dup")
        logger2 = get_logger("test.dup")
        stream_handlers = [
            h for h in logger2.logger.handlers if isinstance(h, logging.StreamHandler)
        ]
        assert len(stream_handlers) == 1

    def test_adds_file_handler_on_second_call(self, tmp_path: Path) -> None:
        """A file handler is added when requested on a subsequent call."""
        get_logger("test.lazy_file")
        log_file = str(tmp_path / "lazy.log")
        logger2 = get_logger("test.lazy_file", log_file=log_file)
        file_handlers = [h for h in logger2.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1

    def test_does_not_duplicate_same_file_handler(self, tmp_path: Path) -> None:
        """Requesting the same log_file twice adds only one FileHandler."""
        log_file = str(tmp_path / "dup_file.log")
        get_logger("test.dup_file", log_file=log_file)
        logger2 = get_logger("test.dup_file", log_file=log_file)
        file_handlers = [h for h in logger2.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1

    def test_propagate_false(self) -> None:
        """get_logger sets propagate=False to prevent duplicate parent output."""
        logger = get_logger("test.propagate")
        assert logger.logger.propagate is False

    def test_level_updated_on_second_call(self) -> None:
        """A second call with a different level updates the logger's level."""
        get_logger("test.level_update", level=logging.INFO)
        logger2 = get_logger("test.level_update", level=logging.DEBUG)
        assert logger2.logger.level == logging.DEBUG


class TestContextLogger:
    """Tests for ContextLogger adapter."""

    def test_bind_adds_context(self) -> None:
        """bind() returns new logger with merged context."""
        base = get_logger("test.bind", context={"a": 1})
        bound = base.bind(b=2)
        assert bound._context == {"a": 1, "b": 2}

    def test_bind_does_not_mutate_original(self) -> None:
        """bind() does not modify the original logger's context."""
        base = get_logger("test.bind_immutable", context={"a": 1})
        base.bind(b=2)
        assert "b" not in base._context

    def test_unbind_removes_key(self) -> None:
        """unbind() returns logger without the specified key."""
        base = get_logger("test.unbind", context={"a": 1, "b": 2})
        unbound = base.unbind("a")
        assert "a" not in unbound._context
        assert unbound._context["b"] == 2

    def test_process_adds_extra(self) -> None:
        """process() merges context into kwargs['extra']."""
        logger = get_logger("test.process", context={"x": 42})
        _msg, kwargs = logger.process("hello", {})
        assert kwargs["extra"]["x"] == 42

    def test_bind_thread_safe(self) -> None:
        """Concurrent bind() calls do not corrupt context."""
        base = get_logger("test.thread_safe", context={"base": 0})
        results: list[dict[str, object]] = []

        def worker(val: int) -> None:
            bound = base.bind(val=val)
            results.append(dict(bound._context))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Original context must not be mutated
        assert "val" not in base._context
        # Each result must contain both base key and the thread-specific val
        for ctx in results:
            assert "base" in ctx
            assert "val" in ctx


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_runs_without_error(self) -> None:
        """setup_logging runs without raising."""
        setup_logging(level=logging.WARNING)

    def test_with_log_file(self, tmp_path: Path) -> None:
        """setup_logging creates log file handler."""
        log_file = str(tmp_path / "setup.log")
        setup_logging(log_file=log_file)
        root_logger = logging.getLogger()
        handler_files = [getattr(h, "baseFilename", None) for h in root_logger.handlers]
        assert log_file in handler_files

    def test_log_to_stderr(self) -> None:
        """setup_logging with log_to_stderr=True adds a stderr StreamHandler."""
        import sys

        root = logging.getLogger()
        # basicConfig is a no-op if handlers already exist; clear them first.
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_to_stderr=True)
            stderr_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            ]
            assert len(stderr_handlers) >= 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)
