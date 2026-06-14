"""Unit tests for hephaestus.automation.ci_check_inspector.CICheckInspector."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ci_check_inspector import CICheckInspector
from hephaestus.automation.models import CIDriverOptions

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_options(**kwargs: Any) -> CIDriverOptions:
    defaults: dict[str, Any] = {
        "issues": [],
        "max_workers": 1,
        "dry_run": False,
        "enable_ui": False,
        "enable_advise": False,
        "include_all_authors": False,
        "include_bot_prs": True,
        "prs": [],
    }
    defaults.update(kwargs)
    return CIDriverOptions(**defaults)


def _make_check(
    name: str,
    *,
    required: bool = True,
    status: str = "completed",
    conclusion: str = "success",
) -> dict[str, Any]:
    return {"name": name, "required": required, "status": status, "conclusion": conclusion}


@pytest.fixture
def inspector(tmp_path: Path) -> CICheckInspector:
    """CICheckInspector with all callable slots pre-wired to no-op stubs."""
    ins = CICheckInspector(
        options=_make_options(),
        get_pr_branch=lambda pr_num: f"branch-{pr_num}",
        get_worktree_path=lambda issue, pr: tmp_path,
        status_tracker_update_slot=lambda slot, msg: None,
    )
    # Wire the minimum slots used by check methods
    ins._load_arming_state_fn = lambda issue: None
    ins._clear_arming_state_fn = lambda issue: None
    ins._learn_record_terminal_fn = lambda record: False
    ins._run_drive_green_learnings_fn = lambda issue, pr: True
    ins._run_drive_green_compact_fn = lambda issue, pr: True
    ins._mark_drive_green_learn_result_fn = lambda issue, record, *, succeeded: None
    ins._save_arming_state_fn = lambda issue, record: None
    ins._state_dir = tmp_path
    return ins


# ---------------------------------------------------------------------------
# _failing_required_check_names
# ---------------------------------------------------------------------------


class TestFailingRequiredCheckNames:
    """Tests for the failing-required-check query."""

    def test_returns_names_of_failing_required_checks(self, inspector: CICheckInspector) -> None:
        checks = [
            _make_check("lint", required=True, conclusion="failure"),
            _make_check("test", required=True, conclusion="success"),
            _make_check("build", required=False, conclusion="failure"),
        ]
        with patch("hephaestus.automation.ci_check_inspector.gh_pr_checks", return_value=checks):
            result = inspector._failing_required_check_names(1)
        assert result == ["lint"]

    def test_returns_empty_when_no_checks(self, inspector: CICheckInspector) -> None:
        with patch("hephaestus.automation.ci_check_inspector.gh_pr_checks", return_value=[]):
            result = inspector._failing_required_check_names(1)
        assert result == []

    def test_returns_empty_on_gh_failure(self, inspector: CICheckInspector) -> None:
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_checks",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            result = inspector._failing_required_check_names(1)
        assert result == []

    def test_treats_all_as_required_when_none_marked(self, inspector: CICheckInspector) -> None:
        """If no check has required=True, all checks are treated as required."""
        checks = [
            _make_check("optional", required=False, conclusion="failure"),
        ]
        with patch("hephaestus.automation.ci_check_inspector.gh_pr_checks", return_value=checks):
            result = inspector._failing_required_check_names(1)
        assert result == ["optional"]


# ---------------------------------------------------------------------------
# _pending_required_check_names
# ---------------------------------------------------------------------------


class TestPendingRequiredCheckNames:
    """Tests for the pending-check guard used in the BLOCKED early-exit."""

    def test_returns_names_of_pending_required_checks(self, inspector: CICheckInspector) -> None:
        checks = [
            _make_check("lint", required=True, status="in_progress", conclusion=""),
            _make_check("test", required=True, status="completed", conclusion="success"),
        ]
        with patch("hephaestus.automation.ci_check_inspector.gh_pr_checks", return_value=checks):
            result = inspector._pending_required_check_names(1)
        assert result == ["lint"]

    def test_returns_empty_when_all_completed(self, inspector: CICheckInspector) -> None:
        checks = [
            _make_check("test", required=True, status="completed", conclusion="success"),
        ]
        with patch("hephaestus.automation.ci_check_inspector.gh_pr_checks", return_value=checks):
            result = inspector._pending_required_check_names(1)
        assert result == []

    def test_returns_empty_on_lookup_failure(self, inspector: CICheckInspector) -> None:
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_checks",
            side_effect=Exception("network error"),
        ):
            result = inspector._pending_required_check_names(1)
        assert result == []


# ---------------------------------------------------------------------------
# _reply_and_resolve_bot_threads
# ---------------------------------------------------------------------------


class TestReplyAndResolveBotThreads:
    """Tests for auto-resolve of bot review threads."""

    def test_resolves_bot_threads_skips_human(self, inspector: CICheckInspector) -> None:
        threads = [
            {"id": "t1", "author": "github-actions[bot]", "body": "lint error"},
            {"id": "t2", "author": "alice", "body": "human review"},
        ]
        with (
            patch(
                "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch("hephaestus.automation.ci_check_inspector.gh_pr_resolve_thread") as mock_resolve,
        ):
            count = inspector._reply_and_resolve_bot_threads(99)

        assert count == 1
        mock_resolve.assert_called_once_with("t1", dry_run=False)

    def test_dry_run_skips_resolution(self, inspector: CICheckInspector) -> None:
        inspector.options.dry_run = True
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
            return_value=[{"id": "t1", "author": "bot[bot]"}],
        ):
            count = inspector._reply_and_resolve_bot_threads(99)
        assert count == 0

    def test_resolve_failure_skipped_not_raised(self, inspector: CICheckInspector) -> None:
        threads = [{"id": "t1", "author": "github-actions[bot]", "body": "x"}]
        with (
            patch(
                "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch(
                "hephaestus.automation.ci_check_inspector.gh_pr_resolve_thread",
                side_effect=Exception("API error"),
            ),
        ):
            count = inspector._reply_and_resolve_bot_threads(99)
        assert count == 0


# ---------------------------------------------------------------------------
# _is_bot_author
# ---------------------------------------------------------------------------


class TestIsBotAuthor:
    """Tests for the [bot]-suffix detection."""

    @pytest.mark.parametrize(
        "login,expected",
        [
            ("github-actions[bot]", True),
            ("dependabot[bot]", True),
            ("coderabbitai[bot]", True),
            ("alice", False),
            ("", False),
            ("bot", False),  # no bracket suffix
        ],
    )
    def test_bot_suffix_detection(
        self, login: str, expected: bool, inspector: CICheckInspector
    ) -> None:
        assert inspector._is_bot_author(login) is expected


# ---------------------------------------------------------------------------
# _format_review_threads_block
# ---------------------------------------------------------------------------


class TestFormatReviewThreadsBlock:
    """Tests for the review-thread markdown block formatter."""

    def test_returns_empty_string_when_no_threads(self, inspector: CICheckInspector) -> None:
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
            return_value=[],
        ):
            result = inspector._format_review_threads_block(1)
        assert result == ""

    def test_formats_thread_with_path_and_line(self, inspector: CICheckInspector) -> None:
        threads = [{"path": "hephaestus/foo.py", "line": 42, "body": "fix this"}]
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
            return_value=threads,
        ):
            result = inspector._format_review_threads_block(1)
        assert "hephaestus/foo.py:42" in result
        assert "fix this" in result
        assert "Unresolved PR Review Threads" in result

    def test_returns_empty_on_lookup_failure(self, inspector: CICheckInspector) -> None:
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_list_unresolved_threads",
            side_effect=Exception("network error"),
        ):
            result = inspector._format_review_threads_block(1)
        assert result == ""


# ---------------------------------------------------------------------------
# _gh_pr_state
# ---------------------------------------------------------------------------


class TestGhPrState:
    """Tests for the PR-state polling helper."""

    def test_returns_parsed_state_dict(self, inspector: CICheckInspector) -> None:
        payload = {"state": "OPEN", "headRefOid": "abc123", "mergedAt": None}
        mock_result = MagicMock(stdout=json.dumps(payload))
        with patch("hephaestus.automation.ci_check_inspector._gh_call", return_value=mock_result):
            result = inspector._gh_pr_state(42)
        assert result == payload

    def test_returns_none_on_gh_failure(self, inspector: CICheckInspector) -> None:
        with patch(
            "hephaestus.automation.ci_check_inspector._gh_call",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            result = inspector._gh_pr_state(42)
        assert result is None

    def test_returns_none_on_json_error(self, inspector: CICheckInspector) -> None:
        mock_result = MagicMock(stdout="not-json")
        with patch("hephaestus.automation.ci_check_inspector._gh_call", return_value=mock_result):
            result = inspector._gh_pr_state(42)
        assert result is None
