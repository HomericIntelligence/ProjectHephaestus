"""Tests for the AddressReviewer automation (address_review.py)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.models import AddressReviewOptions, WorkerResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_options() -> AddressReviewOptions:
    """Create AddressReviewOptions with minimal workers and no UI."""
    return AddressReviewOptions(
        issues=[123],
        max_workers=1,
        dry_run=False,
        enable_ui=False,
        resume_impl_session=True,
    )


@pytest.fixture
def reviewer(mock_options: AddressReviewOptions, tmp_path: Path) -> AddressReviewer:
    """Create an AddressReviewer with mocked repo root pointing to tmp_path."""
    with (
        patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
        patch("hephaestus.automation.address_review.WorktreeManager"),
        patch("hephaestus.automation.address_review.StatusTracker"),
    ):
        ar = AddressReviewer(mock_options)
        ar.state_dir = tmp_path  # point state writes to tmp
        return ar


# ---------------------------------------------------------------------------
# _load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for _load_impl_session_id method."""

    def test_load_impl_session_id_found(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file exists with session_id → returns it."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": "abc-session-123"}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result == "abc-session-123"

    def test_load_impl_session_id_missing_file(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """No state file → returns None."""
        reviewer.state_dir = tmp_path  # empty dir

        result = reviewer._load_impl_session_id(123)

        assert result is None

    def test_load_impl_session_id_null(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file has session_id=null → returns None."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"session_id": None}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result is None

    def test_load_impl_session_id_no_key(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """State file has no session_id key → returns None."""
        state_file = tmp_path / "issue-123.json"
        state_file.write_text(json.dumps({"phase": "completed"}))
        reviewer.state_dir = tmp_path

        result = reviewer._load_impl_session_id(123)

        assert result is None


# ---------------------------------------------------------------------------
# _parse_json_block (instance method on AddressReviewer)
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for AddressReviewer._parse_json_block."""

    def test_extracts_last_json_block(self, reviewer: AddressReviewer) -> None:
        """Returns last parsed JSON block from Claude output."""
        payload = {"addressed": ["thread-1", "thread-2"], "replies": {"thread-1": "Fixed"}}
        text = (
            "Some output\n"
            "```json\n"
            '{"addressed": ["old"], "replies": {}}\n'
            "```\n"
            "More output\n"
            "```json\n" + json.dumps(payload) + "\n```"
        )
        result = reviewer._parse_json_block(text)
        assert result["addressed"] == ["thread-1", "thread-2"]

    def test_no_block_returns_defaults(self, reviewer: AddressReviewer) -> None:
        """No json block → returns defaults with empty addressed list."""
        result = reviewer._parse_json_block("No json here.")
        assert result == {"addressed": [], "replies": {}}

    def test_invalid_json_returns_defaults(self, reviewer: AddressReviewer) -> None:
        """Invalid json block → returns defaults."""
        result = reviewer._parse_json_block("```json\n{broken!!}\n```")
        assert result == {"addressed": [], "replies": {}}


# ---------------------------------------------------------------------------
# _resolve_addressed_threads
# ---------------------------------------------------------------------------


class TestResolveAddressedThreads:
    """Tests for _resolve_addressed_threads method."""

    def test_resolve_only_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """Claude reports [id1] addressed, [id2] not → only id1 resolved."""
        addressed = ["thread-id-1"]
        replies: dict[str, str] = {"thread-id-1": "Fixed the issue"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies)

        mock_resolve.assert_called_once_with("thread-id-1", "Fixed the issue", dry_run=False)

    def test_resolve_multiple_addressed_threads(self, reviewer: AddressReviewer) -> None:
        """All addressed threads are resolved, non-addressed ones are skipped."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {
            "thread-1": "Renamed variable",
            "thread-2": "Added type hint",
        }

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            reviewer._resolve_addressed_threads(addressed, replies)

        assert mock_resolve.call_count == 2
        called_ids = {call[0][0] for call in mock_resolve.call_args_list}
        assert called_ids == {"thread-1", "thread-2"}

    def test_skips_resolve_on_failure(self, reviewer: AddressReviewer) -> None:
        """Individual resolve failures do not abort the rest."""
        addressed = ["thread-1", "thread-2"]
        replies: dict[str, str] = {}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            mock_resolve.side_effect = [RuntimeError("API error"), None]
            # Should not raise
            reviewer._resolve_addressed_threads(addressed, replies)

        assert mock_resolve.call_count == 2

    def test_dry_run_no_resolve(self, mock_options: AddressReviewOptions, tmp_path: Path) -> None:
        """dry_run=True → gh_pr_resolve_thread never called."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.address_review.WorktreeManager"),
            patch("hephaestus.automation.address_review.StatusTracker"),
        ):
            dry_reviewer = AddressReviewer(mock_options)
            dry_reviewer.state_dir = tmp_path

        addressed = ["thread-1"]
        replies: dict[str, str] = {"thread-1": "Fixed"}

        with patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve:
            # dry_run is passed through from options via _resolve_addressed_threads
            # The method itself passes dry_run=self.options.dry_run
            dry_reviewer._resolve_addressed_threads(addressed, replies)

        # With dry_run=True, gh_pr_resolve_thread is called but internally is a no-op;
        # we verify the dry_run flag is forwarded correctly.
        mock_resolve.assert_called_once_with("thread-1", "Fixed", dry_run=True)


# ---------------------------------------------------------------------------
# _address_issue integration
# ---------------------------------------------------------------------------


class TestAddressIssue:
    """Integration-level tests for _address_issue method."""

    def test_no_unresolved_threads_skips(self, reviewer: AddressReviewer) -> None:
        """gh_pr_list_unresolved_threads returns [] → skip gracefully."""
        with patch(
            "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
            return_value=[],
        ):
            result = reviewer._address_issue(123, 456, 0)

        assert result.success is True
        assert result.pr_number == 456

    def test_dry_run_stops_before_resolve(
        self, mock_options: AddressReviewOptions, tmp_path: Path
    ) -> None:
        """dry_run=True → no resolve or push calls."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.address_review.WorktreeManager") as mock_wm_cls,
            patch("hephaestus.automation.address_review.StatusTracker"),
        ):
            mock_wm = MagicMock()
            mock_wm.create_worktree.return_value = tmp_path
            mock_wm_cls.return_value = mock_wm

            dry_reviewer = AddressReviewer(mock_options)
            dry_reviewer.state_dir = tmp_path

        threads = [{"id": "thread-1", "path": "foo.py", "line": 5, "body": "Fix this"}]

        with (
            patch.object(dry_reviewer, "_find_pr_for_issue", return_value=42),
            patch(
                "hephaestus.automation.address_review.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch.object(dry_reviewer, "_load_impl_session_id", return_value=None),
            patch.object(dry_reviewer, "_load_review_state", return_value=None),
            patch.object(dry_reviewer, "_get_or_create_worktree", return_value=tmp_path),
            patch.object(
                dry_reviewer,
                "_run_fix_session",
                return_value={"addressed": ["thread-1"], "replies": {}},
            ),
            patch("hephaestus.automation.address_review.gh_pr_resolve_thread") as mock_resolve,
            patch.object(dry_reviewer, "_push_branch") as mock_push,
        ):
            result = dry_reviewer._address_issue(123, 456, 0)

        assert result.success is True
        mock_resolve.assert_not_called()
        mock_push.assert_not_called()

    def test_no_pr_found_skips_run(self, reviewer: AddressReviewer) -> None:
        """No PR for any issue → run() returns {} without launching any workers."""
        with patch.object(reviewer, "_find_pr_for_issue", return_value=None):
            results = reviewer.run()

        assert results == {}


# ---------------------------------------------------------------------------
# _parse_args (CLI argument parser)
# ---------------------------------------------------------------------------


class TestAddressReviewDiscoverPrs:
    """Tests for AddressReviewer._discover_prs."""

    def test_discover_finds_prs(self, reviewer: AddressReviewer) -> None:
        """Issues with PRs are mapped; issues without are skipped."""

        def find_pr(issue_num: int) -> int | None:
            return {123: 456, 789: None}[issue_num]

        with patch.object(reviewer, "_find_pr_for_issue", side_effect=find_pr):
            pr_map = reviewer._discover_prs([123, 789])

        assert pr_map == {123: 456}

    def test_discover_all_missing_returns_empty(self, reviewer: AddressReviewer) -> None:
        """No PRs found for any issue → empty dict."""
        with patch.object(reviewer, "_find_pr_for_issue", return_value=None):
            pr_map = reviewer._discover_prs([1, 2, 3])
        assert pr_map == {}


class TestAddressReviewLoadSaveReviewState:
    """Tests for _load_review_state and _save_review_state."""

    def test_load_returns_none_when_no_file(
        self, reviewer: AddressReviewer, tmp_path: MagicMock
    ) -> None:
        """No state file → returns None."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            reviewer.state_dir = Path(d)
            result = reviewer._load_review_state(999)
        assert result is None

    def test_load_returns_none_on_invalid_json(self, reviewer: AddressReviewer) -> None:
        """Invalid JSON in state file → returns None."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            state_dir = Path(d)
            reviewer.state_dir = state_dir
            (state_dir / "review-1.json").write_text("{invalid}")
            result = reviewer._load_review_state(1)
        assert result is None


class TestAddressReviewParseJsonBlock:
    """Tests for AddressReviewer._parse_json_block."""

    def test_extracts_json_block(self, reviewer: AddressReviewer) -> None:
        """Parses ```json block from text."""
        payload = {"addressed": ["t1"], "replies": {"t1": "Fixed"}}
        text = "Analysis done\n```json\n" + __import__("json").dumps(payload) + "\n```"
        result = reviewer._parse_json_block(text)
        assert result == payload

    def test_returns_defaults_when_no_block(self, reviewer: AddressReviewer) -> None:
        """Returns defaults when no json block present."""
        result = reviewer._parse_json_block("No code block here")
        assert result == {"addressed": [], "replies": {}}

    def test_returns_defaults_on_invalid_json(self, reviewer: AddressReviewer) -> None:
        """Returns defaults when json block contains invalid JSON."""
        result = reviewer._parse_json_block("```json\n{invalid}\n```")
        assert result == {"addressed": [], "replies": {}}


class TestAddressReviewPrintSummary:
    """Tests for AddressReviewer._print_summary."""

    def test_all_successful(self, reviewer: AddressReviewer) -> None:
        """All results successful → no error logged."""
        results = {
            123: WorkerResult(issue_number=123, success=True),
        }
        reviewer._print_summary(results)  # Should not raise

    def test_with_failures(self, reviewer: AddressReviewer) -> None:
        """Failed results are included in summary."""
        results = {
            123: WorkerResult(issue_number=123, success=False, error="timeout"),
        }
        reviewer._print_summary(results)  # Should not raise

    def test_empty_results(self, reviewer: AddressReviewer) -> None:
        """Empty results do not crash."""
        reviewer._print_summary({})


class TestAddressReviewParseArgs:
    """Tests for _parse_args() CLI argument parser in address_review."""

    def test_issues_arg_parsed(self) -> None:
        """--issues argument is parsed as a list of ints."""
        import sys

        from hephaestus.automation.address_review import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "595", "596"]
            args = _parse_args()
            assert args.issues == [595, 596]
        finally:
            sys.argv = orig

    def test_defaults(self) -> None:
        """Default values for optional arguments are correct."""
        import sys

        from hephaestus.automation.address_review import _parse_args

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

        from hephaestus.automation.address_review import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "--dry-run"]
            args = _parse_args()
            assert args.dry_run is True
        finally:
            sys.argv = orig

    def test_no_ui_flag(self) -> None:
        """--no-ui flag sets no_ui=True."""
        import sys

        from hephaestus.automation.address_review import _parse_args

        orig = sys.argv
        try:
            sys.argv = ["prog", "--issues", "1", "--no-ui"]
            args = _parse_args()
            assert args.no_ui is True
        finally:
            sys.argv = orig


# ---------------------------------------------------------------------------
# run() with discovered PRs — exercises lines 108-122 and _address_all 153-190
# ---------------------------------------------------------------------------


class TestRunWithDiscoveredPrs:
    """Tests for run() when issues have PRs (exercises lines 108-122 and _address_all)."""

    def test_run_returns_worker_results_for_found_prs(self, reviewer: AddressReviewer) -> None:
        """run() submits workers and returns their results."""
        reviewer.options.issues = [123]
        expected_result = WorkerResult(issue_number=123, success=True, pr_number=456)

        with (
            patch.object(reviewer, "_discover_prs", return_value={123: 456}),
            patch.object(reviewer, "_address_issue", return_value=expected_result) as mock_addr,
        ):
            results = reviewer.run()

        assert 123 in results
        assert results[123].success is True
        mock_addr.assert_called_once()

    def test_run_captures_exception_from_worker(self, reviewer: AddressReviewer) -> None:
        """run() catches worker exceptions and records a failure."""
        reviewer.options.issues = [123]

        with (
            patch.object(reviewer, "_discover_prs", return_value={123: 456}),
            patch.object(reviewer, "_address_issue", side_effect=RuntimeError("worker crash")),
        ):
            results = reviewer.run()

        assert 123 in results
        assert results[123].success is False
        assert "worker crash" in (results[123].error or "")

    def test_run_multiple_issues_all_successful(self, reviewer: AddressReviewer) -> None:
        """run() processes multiple issues and returns all results."""
        reviewer.options.issues = [10, 20]
        reviewer.options.max_workers = 2

        def _address(issue_num: int, pr_num: int, slot_id: int) -> WorkerResult:
            return WorkerResult(issue_number=issue_num, success=True, pr_number=pr_num)

        with (
            patch.object(reviewer, "_discover_prs", return_value={10: 100, 20: 200}),
            patch.object(reviewer, "_address_issue", side_effect=_address),
        ):
            results = reviewer.run()

        assert len(results) == 2
        assert all(r.success for r in results.values())


# ---------------------------------------------------------------------------
# _find_pr_for_issue
# ---------------------------------------------------------------------------


class TestFindPrForIssue:
    """Tests for AddressReviewer._find_pr_for_issue."""

    def test_finds_pr_by_branch_name(self, reviewer: AddressReviewer) -> None:
        """PR found by branch-name lookup → returns its number."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps([{"number": 42}])

        with patch(
            "hephaestus.automation.address_review._gh_call", return_value=mock_result
        ) as mock_gh:
            pr_number = reviewer._find_pr_for_issue(123)

        assert pr_number == 42
        first_call_args = mock_gh.call_args_list[0][0][0]
        assert "--head" in first_call_args

    def test_falls_back_to_body_search(self, reviewer: AddressReviewer) -> None:
        """Empty branch results → body search finds a PR."""
        branch_result = MagicMock()
        branch_result.stdout = "[]"

        body_result = MagicMock()
        body_result.stdout = json.dumps([{"number": 99}])

        with patch(
            "hephaestus.automation.address_review._gh_call",
            side_effect=[branch_result, body_result],
        ):
            pr_number = reviewer._find_pr_for_issue(123)

        assert pr_number == 99

    def test_returns_none_when_both_strategies_empty(self, reviewer: AddressReviewer) -> None:
        """Both lookups return [] → returns None."""
        empty = MagicMock()
        empty.stdout = "[]"

        with patch("hephaestus.automation.address_review._gh_call", return_value=empty):
            pr_number = reviewer._find_pr_for_issue(123)

        assert pr_number is None

    def test_returns_none_on_exception(self, reviewer: AddressReviewer) -> None:
        """Gh calls raise → returns None."""
        with patch(
            "hephaestus.automation.address_review._gh_call",
            side_effect=RuntimeError("gh error"),
        ):
            pr_number = reviewer._find_pr_for_issue(123)

        assert pr_number is None


# ---------------------------------------------------------------------------
# _get_or_create_worktree
# ---------------------------------------------------------------------------


class TestGetOrCreateWorktree:
    """Tests for AddressReviewer._get_or_create_worktree."""

    def test_reuses_existing_worktree(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """review_state has worktree_path that exists → returns it."""
        from hephaestus.automation.address_review import ReviewState

        wt = tmp_path / "existing-wt"
        wt.mkdir()
        (wt / ".git").mkdir()  # simulate git worktree marker

        state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
            worktree_path=str(wt),
        )

        with patch.object(reviewer.worktree_manager, "create_worktree") as mock_create:
            result = reviewer._get_or_create_worktree(123, "123-auto-impl", state)

        assert result == wt
        mock_create.assert_not_called()

    def test_creates_new_worktree_when_path_missing(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """review_state worktree_path doesn't exist → creates new worktree."""
        from hephaestus.automation.address_review import ReviewState

        state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
            worktree_path="/nonexistent/path",
        )

        new_path = tmp_path / "new-wt"
        new_path.mkdir()

        with patch.object(
            reviewer.worktree_manager, "create_worktree", return_value=new_path
        ) as mock_create:
            result = reviewer._get_or_create_worktree(123, "123-auto-impl", state)

        assert result == new_path
        mock_create.assert_called_once_with(123, "123-auto-impl")

    def test_creates_new_worktree_when_no_worktree_in_state(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """review_state has no worktree_path → creates new worktree."""
        from hephaestus.automation.address_review import ReviewState

        state = ReviewState(
            issue_number=123,
            pr_number=456,
            branch_name="123-auto-impl",
        )

        new_path = tmp_path / "new-wt"
        new_path.mkdir()

        with patch.object(
            reviewer.worktree_manager, "create_worktree", return_value=new_path
        ) as mock_create:
            result = reviewer._get_or_create_worktree(123, "123-auto-impl", state)

        assert result == new_path
        mock_create.assert_called_once_with(123, "123-auto-impl")


# ---------------------------------------------------------------------------
# _run_fix_session dry_run path
# ---------------------------------------------------------------------------


class TestRunFixSession:
    """Tests for AddressReviewer._run_fix_session."""

    def test_dry_run_returns_empty_result(
        self, mock_options: AddressReviewOptions, tmp_path: Path
    ) -> None:
        """dry_run=True → returns {'addressed': [], 'replies': {}} without running Claude."""
        mock_options.dry_run = True

        with (
            patch("hephaestus.automation.address_review.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.address_review.WorktreeManager"),
            patch("hephaestus.automation.address_review.StatusTracker"),
        ):
            dry_reviewer = AddressReviewer(mock_options)
            dry_reviewer.state_dir = tmp_path

        threads = [{"id": "t1", "path": "foo.py", "line": 5, "body": "Fix this"}]
        result = dry_reviewer._run_fix_session(
            issue_number=123,
            pr_number=456,
            worktree_path=tmp_path,
            threads=threads,
            session_id=None,
        )

        assert result == {"addressed": [], "replies": {}}


# ---------------------------------------------------------------------------
# _save_review_state and _load_review_state round-trip
# ---------------------------------------------------------------------------


class TestReviewStateRoundTrip:
    """Tests for _save_review_state / _load_review_state round-trip."""

    def test_save_and_load_round_trip(self, reviewer: AddressReviewer, tmp_path: Path) -> None:
        """Saved state can be loaded back with the same data."""
        from hephaestus.automation.address_review import ReviewState

        reviewer.state_dir = tmp_path
        state = ReviewState(
            issue_number=55,
            pr_number=66,
            branch_name="55-auto-impl",
        )

        reviewer._save_review_state(state)
        loaded = reviewer._load_review_state(55)

        assert loaded is not None
        assert loaded.issue_number == 55
        assert loaded.pr_number == 66
        assert loaded.branch_name == "55-auto-impl"

    def test_load_invalid_json_returns_none(
        self, reviewer: AddressReviewer, tmp_path: Path
    ) -> None:
        """Invalid JSON in state file → _load_review_state returns None."""
        reviewer.state_dir = tmp_path
        (tmp_path / "review-77.json").write_text("{INVALID}")

        result = reviewer._load_review_state(77)
        assert result is None
