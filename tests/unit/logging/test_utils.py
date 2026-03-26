#!/usr/bin/env python3
"""Tests for logging utilities."""

import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock

from hephaestus.logging.utils import (
    ContextLogger,
    _close_handlers,
    get_logger,
    setup_logging,
)


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

    def test_repeated_calls_close_file_handlers(self, tmp_path: Path) -> None:
        """Repeated setup_logging calls close previous FileHandlers."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            file1 = str(tmp_path / "first.log")
            file2 = str(tmp_path / "second.log")

            setup_logging(log_file=file1)
            # Grab the file handler from the first call
            first_file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            assert len(first_file_handlers) == 1
            first_handler = first_file_handlers[0]

            # Save the stream reference before it gets closed
            first_stream = first_handler.stream

            setup_logging(log_file=file2)
            # The first file handler's stream should be closed
            assert first_stream.closed
            # Only the second file handler should remain
            current_file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            assert len(current_file_handlers) == 1
            assert current_file_handlers[0].baseFilename == file2
        finally:
            for h in root.handlers[:]:
                h.close()
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_repeated_calls_no_handler_accumulation(self) -> None:
        """Repeated setup_logging calls do not accumulate handlers."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging()
            count_after_first = len(root.handlers)

            setup_logging()
            count_after_second = len(root.handlers)

            assert count_after_second == count_after_first
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_force_reconfigures_level(self) -> None:
        """Repeated setup_logging calls actually change the log level."""
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        root.handlers.clear()
        try:
            setup_logging(level=logging.WARNING)
            assert root.level == logging.WARNING

            setup_logging(level=logging.DEBUG)
            assert root.level == logging.DEBUG
        finally:
            root.handlers.clear()
            root.handlers.extend(saved_handlers)
            root.setLevel(saved_level)

    def test_handler_close_failure_does_not_raise(self) -> None:
        """setup_logging completes even if a handler's close() raises."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            # Add a mock handler whose close() raises
            bad_handler = MagicMock(spec=logging.Handler)
            bad_handler.close.side_effect = OSError("disk error")
            root.addHandler(bad_handler)

            # Should not raise
            setup_logging()
            bad_handler.close.assert_called_once()
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)


class TestCloseHandlers:
    """Tests for _close_handlers helper."""

    def test_closes_and_removes_all_handlers(self) -> None:
        """_close_handlers closes and removes every handler."""
        logger = logging.getLogger("test._close_handlers")
        h1 = logging.StreamHandler()
        h2 = logging.StreamHandler()
        logger.addHandler(h1)
        logger.addHandler(h2)

        _close_handlers(logger)

        assert len(logger.handlers) == 0

    def test_closes_file_handler_stream(self, tmp_path: Path) -> None:
        """_close_handlers closes the underlying file stream."""
        logger = logging.getLogger("test._close_handlers_file")
        fh = logging.FileHandler(str(tmp_path / "test.log"))
        logger.addHandler(fh)
        # Save stream reference before close() sets it to None
        stream = fh.stream

        _close_handlers(logger)

        assert stream.closed
        assert len(logger.handlers) == 0
