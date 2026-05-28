#!/usr/bin/env python3
"""Integration test for GH_TRACE_ID environment variable propagation."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hephaestus.automation.git_utils import run as git_run
from hephaestus.logging.utils import correlation_id_scope, get_current_correlation_id


@pytest.fixture(autouse=True)
def _fake_binary_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add a temporary directory with fake 'gh' and 'git' binaries to PATH.

    These fake binaries echo their environment so tests can verify GH_TRACE_ID.
    """
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()

    # Create a fake 'gh' script that outputs environment
    fake_gh = fake_bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\nenv | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    # Create a fake 'git' script that outputs environment
    fake_git = fake_bin_dir / "git"
    fake_git.write_text(
        "#!/bin/sh\nenv | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    # Prepend the fake bin directory to PATH so our fake binaries are found first
    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(fake_bin_dir) + ":" + original_path)


class TestGhTraceIdPropagation:
    """Test that GH_TRACE_ID is propagated to subprocesses via correlation_id_scope."""

    def test_correlation_id_not_set_by_default(self) -> None:
        """Verify that get_current_correlation_id() returns None by default."""
        assert get_current_correlation_id() is None

    def test_gh_trace_id_present_in_scope(self, tmp_path: Path) -> None:
        """GH_TRACE_ID should be present in subprocess when in correlation_id_scope."""
        result = subprocess.run(
            ["gh", "issue", "list"],
            capture_output=True,
            text=True,
            check=False,
            env=None,  # Use current environment (won't have GH_TRACE_ID)
        )
        # Outside scope: no GH_TRACE_ID
        assert "NO_GH_TRACE_ID" in result.stdout or "GH_TRACE_ID" not in result.stdout

        # Inside scope: GH_TRACE_ID should be present
        with correlation_id_scope("test-trace-id-123"):
            # Use git_run (which calls run_subprocess) to verify the injection
            result = git_run(
                ["--version"],
                cwd=Path(os.getcwd()),
                check=False,
            )
            assert "GH_TRACE_ID=test-trace-id-123" in result.stdout

    def test_gh_trace_id_absent_outside_scope(self) -> None:
        """GH_TRACE_ID should not be set outside of correlation_id_scope."""
        assert get_current_correlation_id() is None

        result = subprocess.run(
            ["gh", "issue", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        # Outside scope: no GH_TRACE_ID
        assert "NO_GH_TRACE_ID" in result.stdout

    def test_nested_correlation_id_scopes(self) -> None:
        """Nested correlation_id_scope should propagate the innermost ID."""
        with correlation_id_scope("outer-id"):
            result_outer = subprocess.run(
                ["gh", "issue", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert "GH_TRACE_ID=outer-id" in result_outer.stdout

            with correlation_id_scope("inner-id"):
                result_inner = subprocess.run(
                    ["gh", "issue", "list"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert "GH_TRACE_ID=inner-id" in result_inner.stdout

            # After exiting inner scope, outer ID should be restored
            result_back = subprocess.run(
                ["gh", "issue", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert "GH_TRACE_ID=outer-id" in result_back.stdout

    def test_correlation_id_cleanup_after_exception(self) -> None:
        """Correlation ID should be cleaned up even if an exception occurs in the scope."""
        try:
            with correlation_id_scope("error-scope-id"):
                result = subprocess.run(
                    ["gh", "issue", "list"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert "GH_TRACE_ID=error-scope-id" in result.stdout
                raise ValueError("Test exception")
        except ValueError:
            pass

        # After exception: correlation ID should be cleared
        assert get_current_correlation_id() is None
        result = subprocess.run(
            ["gh", "issue", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "NO_GH_TRACE_ID" in result.stdout
