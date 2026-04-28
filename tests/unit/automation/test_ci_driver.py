"""Tests for the CIDriver automation (ci_driver.py)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import CIDriverOptions, WorkerResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_check(
    name: str,
    status: str = "completed",
    conclusion: str = "success",
    required: bool = True,
) -> dict[str, Any]:
    """Build a CI check dict."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "required": required,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options() -> CIDriverOptions:
    """Create CIDriverOptions with minimal parallelism and no UI."""
    return CIDriverOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        max_fix_iterations=1,
    )


@pytest.fixture
def driver(mock_options: CIDriverOptions, tmp_path: Path) -> CIDriver:
    """Create a CIDriver with mocked repo root."""
    with (
        patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.ci_driver.WorktreeManager"),
        patch("hephaestus.automation.ci_driver.StatusTracker"),
    ):
        d = CIDriver(mock_options)
        d.state_dir = tmp_path
        return d


# ---------------------------------------------------------------------------
# _load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for _load_impl_session_id."""

    def test_returns_session_id_when_present(self, driver: CIDriver, tmp_path: Path) -> None:
        """state-{issue}.json with session_id → returns it."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"session_id": "sess-xyz"}))
        driver.state_dir = tmp_path

        result = driver._load_impl_session_id(123)

        assert result == "sess-xyz"

    def test_returns_none_when_no_file(self, driver: CIDriver, tmp_path: Path) -> None:
        """No state file → returns None."""
        driver.state_dir = tmp_path  # empty

        result = driver._load_impl_session_id(123)

        assert result is None

    def test_returns_none_when_no_key(self, driver: CIDriver, tmp_path: Path) -> None:
        """State file missing session_id key → returns None."""
        state_file = tmp_path / "state-123.json"
        state_file.write_text(json.dumps({"phase": "completed"}))
        driver.state_dir = tmp_path

        result = driver._load_impl_session_id(123)

        assert result is None


# ---------------------------------------------------------------------------
# _parse_json_block
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for CIDriver._parse_json_block."""

    def test_extracts_json_block(self, driver: CIDriver) -> None:
        """Parses first ```json block from text."""
        payload = {"fixed": True, "notes": "All tests pass"}
        text = "Some output\n```json\n" + json.dumps(payload) + "\n```\nMore text"
        result = driver._parse_json_block(text)
        assert result == payload

    def test_falls_back_to_raw_json(self, driver: CIDriver) -> None:
        """Parses raw JSON if no code block present."""
        payload = {"fixed": False}
        result = driver._parse_json_block(json.dumps(payload))
        assert result == payload

    def test_returns_empty_dict_on_invalid(self, driver: CIDriver) -> None:
        """Returns {} for unparseable input."""
        result = driver._parse_json_block("not json at all")
        assert result == {}


# ---------------------------------------------------------------------------
# _drive_issue: no PR found
# ---------------------------------------------------------------------------


class TestNoPrFound:
    """Tests for when no PR exists for an issue."""

    def test_no_pr_found_skips(self, driver: CIDriver) -> None:
        """No PR for any issue → run() returns {} without launching any workers."""
        with patch.object(driver, "_find_pr_for_issue", return_value=None):
            results = driver.run()

        assert results == {}

    def test_empty_issues_returns_empty(
        self, mock_options: CIDriverOptions, tmp_path: Path
    ) -> None:
        """Empty issue list → run() returns {} immediately."""
        mock_options.issues = []
        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            empty_driver = CIDriver(mock_options)
            empty_driver.state_dir = tmp_path
        results = empty_driver.run()
        assert results == {}


class TestDiscoverPrs:
    """Tests for _discover_prs pre-discovery logic."""

    def test_discover_finds_prs(self, driver: CIDriver) -> None:
        """Issues with PRs are mapped; issues without are skipped."""

        def find_pr(issue_num: int) -> int | None:
            return {123: 456, 789: None}[issue_num]

        driver.options.issues = [123, 789]
        with patch.object(driver, "_find_pr_for_issue", side_effect=find_pr):
            pr_map = driver._discover_prs([123, 789])

        assert pr_map == {123: 456}

    def test_discover_all_missing_returns_empty(self, driver: CIDriver) -> None:
        """No PRs found for any issue → empty dict."""
        with patch.object(driver, "_find_pr_for_issue", return_value=None):
            pr_map = driver._discover_prs([1, 2, 3])
        assert pr_map == {}


class TestGetWorktreePath:
    """Tests for _get_worktree_path method."""

    def test_returns_worktree_from_review_state(self, driver: CIDriver, tmp_path: Path) -> None:
        """If review state exists with worktree_path, uses that path."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        state_file = tmp_path / "review-123.json"
        state_file.write_text(__import__("json").dumps({"worktree_path": str(wt_path)}))
        driver.state_dir = tmp_path

        result = driver._get_worktree_path(123, 456)

        assert result == wt_path

    def test_falls_back_to_new_worktree_when_path_missing(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """If worktree_path in review state doesn't exist, create new worktree."""
        state_file = tmp_path / "review-123.json"
        state_file.write_text(__import__("json").dumps({"worktree_path": "/nonexistent/path"}))
        driver.state_dir = tmp_path

        expected_path = tmp_path / "wt"
        expected_path.mkdir()

        with (
            patch.object(driver, "_get_pr_branch", return_value="feat/my-branch"),
            patch.object(driver.worktree_manager, "create_worktree", return_value=expected_path),
        ):
            result = driver._get_worktree_path(123, 456)

        assert result == expected_path

    def test_no_review_state_creates_new_worktree(self, driver: CIDriver, tmp_path: Path) -> None:
        """No review state file → creates new worktree via WorktreeManager."""
        driver.state_dir = tmp_path  # empty, no state files

        expected_path = tmp_path / "wt"
        expected_path.mkdir()

        with (
            patch.object(driver, "_get_pr_branch", return_value="feat/branch"),
            patch.object(driver.worktree_manager, "create_worktree", return_value=expected_path),
        ):
            result = driver._get_worktree_path(999, 42)

        assert result == expected_path


# ---------------------------------------------------------------------------
# _drive_issue: all-green path
# ---------------------------------------------------------------------------


class TestAllRequiredGreen:
    """Tests for the all-green CI path."""

    def test_all_required_green_enables_auto_merge(self, driver: CIDriver) -> None:
        """All required checks success → _enable_auto_merge called."""
        checks = [
            _make_check("test", required=True),
            _make_check("lint", required=True),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_dry_run_no_auto_merge(self, mock_options: CIDriverOptions, tmp_path: Path) -> None:
        """dry_run=True, all green → gh pr merge not called."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            dry_driver = CIDriver(mock_options)
            dry_driver.state_dir = tmp_path

        checks = [_make_check("test", required=True)]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(dry_driver, "_enable_auto_merge") as mock_merge,
            patch("hephaestus.automation.ci_driver._gh_call") as mock_gh,
        ):
            result = dry_driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_not_called()
        # Ensure the raw gh call for merge was not made either
        merge_calls = [c for c in mock_gh.call_args_list if "merge" in str(c)]
        assert len(merge_calls) == 0


# ---------------------------------------------------------------------------
# required vs non-required check classification
# ---------------------------------------------------------------------------


class TestRequiredVsNonRequired:
    """Tests for required vs non-required check gate logic."""

    def test_no_required_checks_uses_all(self, driver: CIDriver) -> None:
        """No check has required=True → all checks treated as required."""
        checks = [
            _make_check("test", required=False, conclusion="success"),
            _make_check("lint", required=False, conclusion="success"),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        # All non-required treated as required → all green → auto-merge
        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_required_vs_nonrequired_only_required_gates_green(self, driver: CIDriver) -> None:
        """Mix of required/non-required; only required=True ones gate green."""
        checks = [
            _make_check("required-test", required=True, conclusion="success"),
            # Non-required check is failing but should NOT block auto-merge
            _make_check("optional-lint", required=False, conclusion="failure"),
        ]
        with (
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_enable_auto_merge") as mock_merge,
        ):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        mock_merge.assert_called_once_with(456)

    def test_failing_required_runs_fix_session(self, driver: CIDriver) -> None:
        """Required check failed → _run_ci_fix_session called."""
        checks = [
            _make_check("required-test", required=True, conclusion="failure"),
        ]
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_get_failing_ci_logs", return_value="error log"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=Path("/tmp/wt")),
            patch.object(driver, "_run_ci_fix_session", return_value=True) as mock_fix,
        ):
            result = driver._drive_issue(123, 456, 0)

        mock_fix.assert_called_once()
        assert result.success is True

    def test_pending_checks_skip_fix(self, driver: CIDriver) -> None:
        """All checks pending (not completed) → no fix attempted."""
        checks = [
            _make_check("test", status="in_progress", conclusion="", required=True),
        ]
        with (
            patch.object(driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = driver._drive_issue(123, 456, 0)

        mock_fix.assert_not_called()
        assert result.success is True


# ---------------------------------------------------------------------------
# dry_run with failing checks
# ---------------------------------------------------------------------------


class TestDryRunWithFailingChecks:
    """Tests for dry_run=True when checks are failing."""

    def test_dry_run_no_fix_push(self, mock_options: CIDriverOptions, tmp_path: Path) -> None:
        """dry_run=True, required check failed → fix session logs intent but doesn't push."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            dry_driver = CIDriver(mock_options)
            dry_driver.state_dir = tmp_path

        checks = [_make_check("test", required=True, conclusion="failure")]
        with (
            patch.object(dry_driver, "_find_pr_for_issue", return_value=42),
            patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=checks),
            patch.object(dry_driver, "_get_failing_ci_logs", return_value="log"),
            patch.object(dry_driver, "_load_impl_session_id", return_value=None),
            patch.object(dry_driver, "_get_worktree_path", return_value=tmp_path),
            patch.object(dry_driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = dry_driver._drive_issue(123, 456, 0)

        # dry_run returns success before actually running the fix session
        assert result.success is True
        mock_fix.assert_not_called()


# ---------------------------------------------------------------------------
# No CI checks found
# ---------------------------------------------------------------------------


class TestNoCiChecks:
    """Tests for when no CI checks are returned."""

    def test_no_checks_returns_success(self, driver: CIDriver) -> None:
        """No CI checks for PR → returns WorkerResult(success=True)."""
        with patch("hephaestus.automation.ci_driver.gh_pr_checks", return_value=[]):
            result = driver._drive_issue(123, 456, 0)

        assert result.success is True
        assert result.pr_number == 456


# ---------------------------------------------------------------------------
# _print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    """Tests for CIDriver._print_summary."""

    def test_all_successful(self, driver: CIDriver) -> None:
        """All results successful logs summary without 'Failed issues' section."""
        results = {
            123: WorkerResult(issue_number=123, success=True),
            456: WorkerResult(issue_number=456, success=True),
        }
        # Should not raise and just logs
        driver._print_summary(results)

    def test_with_failures(self, driver: CIDriver) -> None:
        """Failed results include issue number and error in summary."""
        results = {
            123: WorkerResult(issue_number=123, success=True),
            456: WorkerResult(issue_number=456, success=False, error="timeout"),
        }
        # Should not raise
        driver._print_summary(results)

    def test_empty_results(self, driver: CIDriver) -> None:
        """Empty results → no crash, logs totals as 0."""
        driver._print_summary({})


# ---------------------------------------------------------------------------
# _parse_args (CLI arg parser)
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Tests for _parse_args() CLI argument parser."""

    def test_issues_required(self) -> None:
        """--issues is a required argument."""
        import sys

        from hephaestus.automation.ci_driver import _parse_args

        monkeypatch_argv = ["prog", "--issues", "123", "456"]
        orig = sys.argv
        try:
            sys.argv = monkeypatch_argv
            args = _parse_args()
            assert args.issues == [123, 456]
        finally:
            sys.argv = orig

    def test_defaults(self) -> None:
        """Default values for optional arguments."""
        import sys

        from hephaestus.automation.ci_driver import _parse_args

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
        """--dry-run sets dry_run=True."""
        import sys

        from hephaestus.automation.ci_driver import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "--dry-run"]
            args = _parse_args()
            assert args.dry_run is True
        finally:
            sys.argv = orig


# ---------------------------------------------------------------------------
# run() with discovered PRs — exercises the ThreadPoolExecutor body
# ---------------------------------------------------------------------------


class TestRunWithDiscoveredPrs:
    """Tests for run() when issues have PRs — exercises the ThreadPoolExecutor path."""

    def test_run_returns_worker_results_for_found_prs(
        self, driver: CIDriver, tmp_path: Path
    ) -> None:
        """run() submits workers for each found PR and returns their results."""
        driver.options.issues = [123]
        expected_result = WorkerResult(issue_number=123, success=True, pr_number=456)

        with (
            patch.object(driver, "_discover_prs", return_value={123: 456}),
            patch.object(driver, "_drive_issue", return_value=expected_result) as mock_drive,
        ):
            results = driver.run()

        assert 123 in results
        assert results[123].success is True
        mock_drive.assert_called_once_with(123, 456, 0)

    def test_run_captures_exception_from_worker(self, driver: CIDriver, tmp_path: Path) -> None:
        """run() catches exceptions raised from workers and records a failure."""
        driver.options.issues = [123]

        with (
            patch.object(driver, "_discover_prs", return_value={123: 456}),
            patch.object(driver, "_drive_issue", side_effect=RuntimeError("worker died")),
        ):
            results = driver.run()

        assert 123 in results
        assert results[123].success is False
        assert "worker died" in (results[123].error or "")

    def test_run_multiple_prs_all_successful(self, driver: CIDriver) -> None:
        """run() handles multiple issues processed in parallel."""
        driver.options.issues = [10, 20]
        driver.options.max_workers = 2

        def _drive(issue_num: int, pr_num: int, slot_id: int) -> WorkerResult:
            return WorkerResult(issue_number=issue_num, success=True, pr_number=pr_num)

        with (
            patch.object(driver, "_discover_prs", return_value={10: 100, 20: 200}),
            patch.object(driver, "_drive_issue", side_effect=_drive),
        ):
            results = driver.run()

        assert len(results) == 2
        assert all(r.success for r in results.values())


# ---------------------------------------------------------------------------
# _find_pr_for_issue
# ---------------------------------------------------------------------------


class TestFindPrForIssue:
    """Tests for CIDriver._find_pr_for_issue."""

    def test_finds_pr_by_branch_name(self, driver: CIDriver) -> None:
        """PR found by matching branch name → returns its number."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps([{"number": 42}])

        with patch("hephaestus.automation.ci_driver._gh_call", return_value=mock_result) as mock_gh:
            pr_number = driver._find_pr_for_issue(123)

        assert pr_number == 42
        # First call should be branch-name lookup
        first_call_args = mock_gh.call_args_list[0][0][0]
        assert "--head" in first_call_args

    def test_falls_back_to_body_search_when_branch_empty(self, driver: CIDriver) -> None:
        """Empty branch-name results → tries body search and returns that PR."""
        branch_result = MagicMock()
        branch_result.stdout = "[]"  # no PR on branch

        body_result = MagicMock()
        body_result.stdout = json.dumps([{"number": 99}])

        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=[branch_result, body_result],
        ):
            pr_number = driver._find_pr_for_issue(123)

        assert pr_number == 99

    def test_returns_none_when_both_strategies_fail(self, driver: CIDriver) -> None:
        """Both lookup strategies return empty → returns None."""
        empty_result = MagicMock()
        empty_result.stdout = "[]"

        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            return_value=empty_result,
        ):
            pr_number = driver._find_pr_for_issue(123)

        assert pr_number is None

    def test_returns_none_on_gh_exception(self, driver: CIDriver) -> None:
        """Gh calls raise exception → returns None."""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=RuntimeError("gh not found"),
        ):
            pr_number = driver._find_pr_for_issue(123)

        assert pr_number is None


# ---------------------------------------------------------------------------
# _get_pr_branch
# ---------------------------------------------------------------------------


class TestGetPrBranch:
    """Tests for CIDriver._get_pr_branch."""

    def test_returns_branch_name_from_gh(self, driver: CIDriver) -> None:
        """Gh pr view returns headRefName → method returns it."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps({"headRefName": "feat/my-branch"})

        with patch("hephaestus.automation.ci_driver._gh_call", return_value=mock_result):
            branch = driver._get_pr_branch(42)

        assert branch == "feat/my-branch"

    def test_returns_fallback_on_error(self, driver: CIDriver) -> None:
        """Gh call fails → returns 'pr-{pr_number}'."""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=RuntimeError("gh error"),
        ):
            branch = driver._get_pr_branch(42)

        assert branch == "pr-42"


# ---------------------------------------------------------------------------
# _enable_auto_merge
# ---------------------------------------------------------------------------


class TestEnableAutoMerge:
    """Tests for CIDriver._enable_auto_merge."""

    def test_calls_gh_pr_merge(self, driver: CIDriver) -> None:
        """_enable_auto_merge calls _gh_call with pr merge --auto --rebase."""
        with patch("hephaestus.automation.ci_driver._gh_call") as mock_gh:
            driver._enable_auto_merge(42)

        mock_gh.assert_called_once()
        args = mock_gh.call_args[0][0]
        assert "merge" in args
        assert "--auto" in args
        assert "--rebase" in args

    def test_swallows_exception_on_error(self, driver: CIDriver) -> None:
        """_enable_auto_merge does not raise when gh call fails."""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=RuntimeError("merge failed"),
        ):
            # Should not raise
            driver._enable_auto_merge(42)


# ---------------------------------------------------------------------------
# _get_failing_ci_logs
# ---------------------------------------------------------------------------


class TestGetFailingCiLogs:
    """Tests for CIDriver._get_failing_ci_logs."""

    def test_returns_empty_string_on_exception(self, driver: CIDriver) -> None:
        """Exception in gh run list → returns empty string."""
        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=RuntimeError("gh error"),
        ):
            result = driver._get_failing_ci_logs(42)

        assert result == ""

    def test_returns_empty_string_when_no_failures(self, driver: CIDriver) -> None:
        """No failed runs → returns empty string."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(
            [
                {"databaseId": 1, "conclusion": "success", "name": "test", "headSha": "abc"},
            ]
        )

        with patch("hephaestus.automation.ci_driver._gh_call", return_value=mock_result):
            result = driver._get_failing_ci_logs(42)

        assert result == ""

    def test_returns_logs_for_failed_runs(self, driver: CIDriver) -> None:
        """Failed run → fetches log and includes it in output."""
        list_result = MagicMock()
        list_result.stdout = json.dumps(
            [
                {"databaseId": 99, "conclusion": "failure", "name": "test-job", "headSha": "abc"},
            ]
        )

        log_result = MagicMock()
        log_result.stdout = "error: test failed at line 10"

        with patch(
            "hephaestus.automation.ci_driver._gh_call",
            side_effect=[list_result, log_result],
        ):
            result = driver._get_failing_ci_logs(42)

        assert "test-job" in result
        assert "error: test failed" in result


# ---------------------------------------------------------------------------
# _attempt_ci_fixes dry_run
# ---------------------------------------------------------------------------


class TestAttemptCiFixes:
    """Tests for CIDriver._attempt_ci_fixes."""

    def test_dry_run_returns_success_without_running_fix(
        self, mock_options: CIDriverOptions, tmp_path: Path
    ) -> None:
        """dry_run=True → returns success without calling _run_ci_fix_session."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.ci_driver.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.ci_driver.WorktreeManager"),
            patch("hephaestus.automation.ci_driver.StatusTracker"),
        ):
            dry_driver = CIDriver(mock_options)
            dry_driver.state_dir = tmp_path

        with (
            patch.object(dry_driver, "_get_failing_ci_logs", return_value="logs"),
            patch.object(dry_driver, "_load_impl_session_id", return_value=None),
            patch.object(dry_driver, "_get_worktree_path", return_value=tmp_path),
            patch.object(dry_driver, "_run_ci_fix_session") as mock_fix,
        ):
            result = dry_driver._attempt_ci_fixes(123, 456, 0)

        assert result is not None
        assert result.success is True
        mock_fix.assert_not_called()

    def test_returns_none_when_all_iterations_fail(self, driver: CIDriver, tmp_path: Path) -> None:
        """All fix iterations fail → returns None."""
        driver.options.max_fix_iterations = 2

        with (
            patch.object(driver, "_get_failing_ci_logs", return_value="logs"),
            patch.object(driver, "_load_impl_session_id", return_value=None),
            patch.object(driver, "_get_worktree_path", return_value=tmp_path),
            patch.object(driver, "_run_ci_fix_session", return_value=False),
        ):
            result = driver._attempt_ci_fixes(123, 456, 0)

        assert result is None
