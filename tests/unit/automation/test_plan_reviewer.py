"""Tests for the PlanReviewer automation."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import PlanReviewerOptions, WorkerResult
from hephaestus.automation.plan_reviewer import PlanReviewer


@pytest.fixture
def mock_options() -> PlanReviewerOptions:
    """Create mock PlanReviewerOptions."""
    return PlanReviewerOptions(
        issues=[123],
        dry_run=False,
        max_workers=1,
        enable_ui=False,
    )


@pytest.fixture
def reviewer(mock_options: PlanReviewerOptions) -> PlanReviewer:
    """Create a PlanReviewer instance."""
    return PlanReviewer(mock_options)


def _make_gh_result(payload: Any) -> MagicMock:
    """Return a mock CompletedProcess with JSON stdout."""
    mock = MagicMock()
    mock.stdout = json.dumps(payload)
    return mock


class TestGetLatestPlan:
    """Tests for _get_latest_plan method."""

    def test_get_latest_plan_finds_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns plan text from matching comment."""
        comments = [
            {"body": "Some other comment"},
            {"body": "## Implementation Plan\n\nStep 1: Do something\nStep 2: Do more"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Implementation Plan" in result

    def test_get_latest_plan_returns_last_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns the LAST plan comment when multiple exist."""
        comments = [
            {"body": "## Implementation Plan\n\nFirst plan"},
            {"body": "## Implementation Plan\n\nSecond plan (updated)"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is not None
        assert "Second plan (updated)" in result

    def test_get_latest_plan_returns_none_when_no_plan(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns None when no plan comment exists."""
        comments = [
            {"body": "Just a regular comment"},
            {"body": "Another comment"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._get_latest_plan(123)

        assert result is None

    def test_get_latest_plan_returns_none_on_gh_error(self, reviewer: PlanReviewer) -> None:
        """_get_latest_plan returns None when gh call fails."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("gh failed")
            result = reviewer._get_latest_plan(123)

        assert result is None


class TestHasExistingReview:
    """Tests for _has_existing_review method."""

    def test_has_existing_review_true(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns True when review comment exists."""
        comments = [
            {"body": "## 🔍 Plan Review\n\nSome review content"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._has_existing_review(123)

        assert result is True

    def test_has_existing_review_false_no_review(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns False when no review comment exists."""
        comments = [
            {"body": "## Implementation Plan\n\nSome plan"},
            {"body": "Just a comment"},
        ]
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.return_value = _make_gh_result({"comments": comments})
            result = reviewer._has_existing_review(123)

        assert result is False

    def test_has_existing_review_false_on_error(self, reviewer: PlanReviewer) -> None:
        """_has_existing_review returns False when gh call fails."""
        with patch("hephaestus.automation.plan_reviewer._gh_call") as mock_gh:
            mock_gh.side_effect = RuntimeError("gh failed")
            result = reviewer._has_existing_review(123)

        assert result is False


class TestRunClaudeAnalysis:
    """Tests for _run_claude_analysis method."""

    def test_returns_none_on_empty_output(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude returns empty output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="   ", stderr="")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_returns_none_on_nonzero_exit(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude exits non-zero."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error message")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None

    def test_returns_analysis_on_success(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns review text on successful Claude call."""
        analysis_text = "This plan looks good. Here are some suggestions."
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=analysis_text, stderr="")
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result == analysis_text

    def test_returns_none_on_timeout(self, reviewer: PlanReviewer) -> None:
        """_run_claude_analysis returns None when Claude times out."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)
            result = reviewer._run_claude_analysis(123, "Title", "Body", "Plan text")

        assert result is None


class TestReviewIssue:
    """Tests for _review_issue method."""

    def test_review_skipped_if_no_plan(self, reviewer: PlanReviewer) -> None:
        """When issue has no plan comment, _review_issue returns success with no post."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_skipped_if_already_reviewed(self, reviewer: PlanReviewer) -> None:
        """When latest comment is a plan review, skip posting."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=True),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_review_posted(self, reviewer: PlanReviewer) -> None:
        """When plan exists and no review yet, posts review comment with correct prefix."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Great plan! A few suggestions.",
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_called_once()
        posted_body: str = mock_comment.call_args[0][1]
        assert posted_body.startswith("## 🔍 Plan Review")
        assert "Great plan!" in posted_body

    def test_dry_run_no_post(self, mock_options: PlanReviewerOptions) -> None:
        """dry_run=True → gh_issue_comment never called."""
        mock_options.dry_run = True
        reviewer = PlanReviewer(mock_options)

        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(
                reviewer,
                "_run_claude_analysis",
                return_value="Review text",
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_comment") as mock_comment,
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is True
        mock_comment.assert_not_called()

    def test_returns_failure_when_claude_returns_none(self, reviewer: PlanReviewer) -> None:
        """Returns failed WorkerResult when Claude analysis returns None."""
        with (
            patch.object(reviewer, "_has_existing_review", return_value=False),
            patch.object(
                reviewer, "_get_latest_plan", return_value="## Implementation Plan\n\nDo stuff"
            ),
            patch("hephaestus.automation.plan_reviewer.gh_issue_json") as mock_gh_json,
            patch.object(reviewer, "_run_claude_analysis", return_value=None),
        ):
            mock_gh_json.return_value = {"title": "Test Issue", "body": "Issue body"}
            result = reviewer._review_issue(123, 0)

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# _parse_args (CLI argument parser)
# ---------------------------------------------------------------------------


class TestPlanReviewerRunEmpty:
    """Tests for run() with empty issues list."""

    def test_empty_issues_returns_empty(self, mock_options: PlanReviewerOptions) -> None:
        """Empty issue list → run() returns {} without launching any workers."""
        mock_options.issues = []
        reviewer = PlanReviewer(mock_options)
        results = reviewer.run()
        assert results == {}

    def test_run_returns_worker_results_for_issues(self, reviewer: PlanReviewer) -> None:
        """run() with non-empty issues submits workers and collects results."""
        reviewer.options.issues = [123]
        expected = WorkerResult(issue_number=123, success=True)

        with patch.object(reviewer, "_review_issue", return_value=expected) as mock_review:
            results = reviewer.run()

        assert 123 in results
        assert results[123].success is True
        mock_review.assert_called_once()

    def test_run_captures_worker_exception(self, reviewer: PlanReviewer) -> None:
        """run() records a failure when a worker raises an exception."""
        reviewer.options.issues = [123]

        with patch.object(reviewer, "_review_issue", side_effect=RuntimeError("crash")):
            results = reviewer.run()

        assert 123 in results
        assert results[123].success is False
        assert "crash" in (results[123].error or "")

    def test_run_multiple_issues(self, reviewer: PlanReviewer) -> None:
        """run() processes multiple issues and collects all results."""
        reviewer.options.issues = [1, 2]
        reviewer.options.max_workers = 2

        def _review(issue_num: int, slot_id: int) -> WorkerResult:
            return WorkerResult(issue_number=issue_num, success=True)

        with patch.object(reviewer, "_review_issue", side_effect=_review):
            results = reviewer.run()

        assert len(results) == 2
        assert all(r.success for r in results.values())


class TestPlanReviewerPrintSummary:
    """Tests for PlanReviewer._print_summary."""

    def test_all_successful(self, reviewer: PlanReviewer) -> None:
        """All results successful → no error logged."""
        results = {123: WorkerResult(issue_number=123, success=True)}
        reviewer._print_summary(results)  # Should not raise

    def test_with_failures(self, reviewer: PlanReviewer) -> None:
        """Failed results are included in summary."""
        results = {123: WorkerResult(issue_number=123, success=False, error="timeout")}
        reviewer._print_summary(results)  # Should not raise

    def test_empty_results(self, reviewer: PlanReviewer) -> None:
        """Empty results do not crash."""
        reviewer._print_summary({})


class TestPlanReviewerParseArgs:
    """Tests for _parse_args() CLI argument parser in plan_reviewer."""

    def test_issues_arg_parsed(self) -> None:
        """--issues argument is parsed as a list of ints."""
        import sys

        from hephaestus.automation.plan_reviewer import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "2"]
            args = _parse_args()
            assert args.issues == [1, 2]
        finally:
            sys.argv = orig

    def test_defaults(self) -> None:
        """Default values for optional arguments are correct."""
        import sys

        from hephaestus.automation.plan_reviewer import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1"]
            args = _parse_args()
            assert args.max_workers == 3
            assert args.dry_run is False
            assert args.no_ui is False
            assert args.verbose is False
        finally:
            sys.argv = orig

    def test_dry_run_flag(self) -> None:
        """--dry-run flag sets dry_run=True."""
        import sys

        from hephaestus.automation.plan_reviewer import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "--dry-run"]
            args = _parse_args()
            assert args.dry_run is True
        finally:
            sys.argv = orig

    def test_max_workers_option(self) -> None:
        """--max-workers sets max_workers to the given value."""
        import sys

        from hephaestus.automation.plan_reviewer import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "--max-workers", "5"]
            args = _parse_args()
            assert args.max_workers == 5
        finally:
            sys.argv = orig
