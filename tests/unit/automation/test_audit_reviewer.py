"""Tests for hephaestus.automation.audit_reviewer.

Covers _parse_coordinator_results, post_audit_results, write_audit_report,
print_audit_summary, run_audit_coordinator, AuditReviewer.run(),
AuditReviewer._fetch_prs_by_number, and the CLI main() smoke tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from hephaestus.automation import audit_reviewer


def _make_pr_list(count: int = 2) -> list[dict[str, Any]]:
    """Build a minimal PR list for test inputs."""
    return [
        {
            "number": i,
            "title": f"PR {i}",
            "author": "testuser",
            "headRefName": f"feat-{i}",
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "ci_status": "SUCCESS",
        }
        for i in range(1, count + 1)
    ]


class TestParseCoordinatorResults:
    """Tests for _parse_coordinator_results."""

    def test_extracts_last_json_block(self) -> None:
        text = 'some prose\n```json\n{"results": [{"pr_number": 1}]}\n```\ntrailing'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == [{"pr_number": 1}]

    def test_returns_last_json_block_when_multiple(self) -> None:
        text = (
            '```json\n{"results": [{"pr_number": 1}]}\n```\n'
            "more text\n"
            '```json\n{"results": [{"pr_number": 2}, {"pr_number": 3}]}\n```'
        )
        results = audit_reviewer._parse_coordinator_results(text)
        assert len(results) == 2
        assert results[0]["pr_number"] == 2
        assert results[1]["pr_number"] == 3

    def test_returns_empty_list_when_no_json_block(self) -> None:
        results = audit_reviewer._parse_coordinator_results("no json here")
        assert results == []

    def test_returns_empty_list_when_results_not_a_list(self) -> None:
        text = '```json\n{"results": "not a list"}\n```'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == []

    def test_returns_empty_list_when_invalid_json(self) -> None:
        text = "```json\n{invalid json}\n```"
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == []

    def test_returns_empty_list_when_no_results_key(self) -> None:
        text = '```json\n{"other": "data"}\n```'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == []

    def test_handles_json_without_fence_gracefully(self) -> None:
        """Missing ``` markers should return empty."""
        text = '{"results": [{"pr_number": 1}]}'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == []

    def test_handles_empty_string(self) -> None:
        """Empty string returns empty list."""
        results = audit_reviewer._parse_coordinator_results("")
        assert results == []

    def test_handles_whitespace_only_text(self) -> None:
        """Whitespace-only text with no JSON returns empty list."""
        results = audit_reviewer._parse_coordinator_results("   \n  \n  ")
        assert results == []

    def test_extracts_from_text_with_prose_before_and_after(self) -> None:
        """Coordinator prose before and after the JSON block is ignored."""
        text = (
            "I have reviewed all PRs. Here are the results:\n\n"
            '```json\n{"results": [{"pr_number": 42, "comments": [], "summary": "LGTM"}]}\n```\n\n'
            "All PRs have been analysed."
        )
        results = audit_reviewer._parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 42

    def test_extracts_results_with_extra_fields(self) -> None:
        """Results dict may carry extra fields beyond pr_number/comments/summary."""
        text = (
            "```json\n"
            '{"results": [{"pr_number": 1, "comments": [], "summary": "ok",'
            ' "extra_field": "ignored"}]}\n'
            "```"
        )
        results = audit_reviewer._parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 1

    def test_returns_empty_list_for_malformed_fence(self) -> None:
        """Unclosed fence block returns empty (regex requires closing ```)."""
        text = '```json\n{"results": [{"pr_number": 1}]}'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == []


class TestPostAuditResults:
    """Tests for post_audit_results."""

    @patch("hephaestus.automation.audit_reviewer.gh_pr_review_post")
    def test_posts_all_results(self, mock_post: Any) -> None:
        results: list[dict[str, Any]] = [
            {
                "pr_number": 1,
                "comments": [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "fix"}],
                "summary": "needs work",
            },
            {
                "pr_number": 2,
                "comments": [],
                "summary": "LGTM",
            },
        ]
        posted = audit_reviewer.post_audit_results(results)
        assert mock_post.call_count == 2
        assert posted == {1: True, 2: True}

    def test_dry_run_does_nothing(self) -> None:
        results: list[dict[str, Any]] = [{"pr_number": 1, "comments": [], "summary": "test"}]
        posted = audit_reviewer.post_audit_results(results, dry_run=True)
        assert posted == {1: False}

    def test_skips_missing_pr_number(self) -> None:
        results: list[dict[str, Any]] = [{"comments": [], "summary": "no pr_number key"}]
        posted = audit_reviewer.post_audit_results(results)
        assert posted == {}

    def test_handles_non_list_comments(self) -> None:
        results: list[dict[str, Any]] = [
            {"pr_number": 1, "comments": "not-a-list", "summary": "test"}
        ]
        # Should coerce "not-a-list" to [] and still post
        with patch("hephaestus.automation.audit_reviewer.gh_pr_review_post") as mock_post:
            posted = audit_reviewer.post_audit_results(results)
            assert posted == {1: True}
            # comments arg should be [] after coercion
            assert mock_post.call_args.kwargs["comments"] == []

    @patch("hephaestus.automation.audit_reviewer.gh_pr_review_post")
    def test_posting_failure_is_recorded(self, mock_post: Any) -> None:
        mock_post.side_effect = RuntimeError("API down")
        results: list[dict[str, Any]] = [{"pr_number": 1, "comments": [], "summary": "test"}]
        posted = audit_reviewer.post_audit_results(results)
        assert posted == {1: False}


class TestWriteAuditReport:
    """Tests for write_audit_report."""

    def test_writes_report_file(self, tmp_path: Path) -> None:
        results: list[dict[str, Any]] = [
            {"pr_number": 1, "comments": [{}], "summary": "ok"},
            {"pr_number": 2, "comments": [], "summary": "LGTM"},
        ]
        posted = {1: True, 2: True}
        state_dir = tmp_path / "audit"

        path = audit_reviewer.write_audit_report(results, posted, state_dir)

        assert path.exists()
        report = json.loads(path.read_text())
        assert report["total_prs"] == 2
        assert report["posted"] == 2
        assert report["failed"] == 0
        assert len(report["results"]) == 2

        # Check PR 1: 1 comment, posted true
        assert report["results"][0]["pr_number"] == 1
        assert report["results"][0]["comment_count"] == 1
        assert report["results"][0]["posted"] is True

        # Check PR 2: 0 comments, posted true
        assert report["results"][1]["pr_number"] == 2
        assert report["results"][1]["comment_count"] == 0
        assert report["results"][1]["posted"] is True

    def test_report_counts_failures(self, tmp_path: Path) -> None:
        results: list[dict[str, Any]] = [
            {"pr_number": 1, "comments": [], "summary": "ok"},
        ]
        posted = {1: False}
        state_dir = tmp_path / "audit"

        path = audit_reviewer.write_audit_report(results, posted, state_dir)

        report = json.loads(path.read_text())
        assert report["posted"] == 0
        assert report["failed"] == 1

    def test_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "deeply" / "nested" / "audit"
        results: list[dict[str, Any]] = []
        posted: dict[int, bool] = {}

        path = audit_reviewer.write_audit_report(results, posted, state_dir)

        assert path.exists()
        assert path.parent == state_dir


class TestPrintAuditSummary:
    """Tests for print_audit_summary (best-effort logging verification)."""

    def test_prints_summary_lines(self, caplog: Any) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="hephaestus.automation.audit_reviewer")
        results: list[dict[str, Any]] = [
            {
                "pr_number": 1,
                "comments": [{"path": "a.py", "line": 1, "body": "fix"}],
                "summary": "needs work",
            },
        ]
        posted = {1: True}

        audit_reviewer.print_audit_summary(results, posted)

        assert "Total PRs analysed" in caplog.text
        assert "Reviews posted" in caplog.text
        assert "needs work" in caplog.text


class TestAuditReviewerFetchPrsByNumber:
    """Tests for AuditReviewer._fetch_prs_by_number static method."""

    def test_empty_input_returns_empty(self) -> None:
        """Fetching with an empty list returns an empty list."""
        prs = audit_reviewer.AuditReviewer._fetch_prs_by_number([])
        assert prs == []

    @patch("hephaestus.automation.audit_reviewer._gh_call")
    def test_fetches_and_parses_prs(self, mock_gh_call: Any) -> None:
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "number": 42,
                "title": "Test PR",
                "author": {"login": "alice"},
                "headRefName": "feat",
                "baseRefName": "main",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }
        )
        mock_gh_call.return_value = mock_result

        prs = audit_reviewer.AuditReviewer._fetch_prs_by_number([42])

        assert len(prs) == 1
        assert prs[0]["number"] == 42
        assert prs[0]["author"] == "alice"
        assert prs[0]["ci_status"] == "SUCCESS"

    @patch("hephaestus.automation.audit_reviewer._gh_call")
    def test_returns_sorted_by_number(self, mock_gh_call: Any) -> None:
        """Even when fetching by explicit numbers, results are sorted by PR number."""

        def side_effect(args: list[str], **__: Any) -> Any:
            num = next(a for a in args if a.isdigit())
            result = Mock()
            result.stdout = json.dumps(
                {
                    "number": int(num),
                    "title": f"PR {num}",
                    "author": {"login": "user"},
                    "headRefName": "feat",
                    "baseRefName": "main",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                }
            )
            return result

        mock_gh_call.side_effect = side_effect

        prs = audit_reviewer.AuditReviewer._fetch_prs_by_number([3, 1, 2])

        assert [p["number"] for p in prs] == [1, 2, 3]

    @patch("hephaestus.automation.audit_reviewer._gh_call")
    def test_skip_failed_fetches(self, mock_gh_call: Any) -> None:
        """Failed fetches are logged and skipped, not propagated."""
        mock_gh_call.side_effect = RuntimeError("gh offline")

        prs = audit_reviewer.AuditReviewer._fetch_prs_by_number([42])

        assert prs == []


class TestRunAuditCoordinator:
    """Tests for run_audit_coordinator."""

    def test_dry_run_returns_empty(self, tmp_path: Path) -> None:
        """dry_run=True skips agent invocation and returns empty list."""
        pr_list = _make_pr_list()
        results = audit_reviewer.run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=tmp_path,
            agent="claude",
            state_dir=tmp_path / "audit",
            dry_run=True,
        )
        assert results == []

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=True)
    @patch("hephaestus.automation.audit_reviewer.run_codex_text")
    def test_codex_path_parses_results(
        self, mock_run_codex: Any, mock_is_codex: Any, tmp_path: Path
    ) -> None:
        """Codex path: runs run_codex_text and parses its JSON output."""
        pr_list = _make_pr_list()
        mock_result = Mock()
        mock_result.stdout = (
            '```json\n{"results": [{"pr_number": 1, "comments": [],'
            ' "summary": "LGTM"}, {"pr_number": 2, "comments": [],'
            ' "summary": "LGTM"}]}\n```'
        )
        mock_run_codex.return_value = mock_result

        results = audit_reviewer.run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=tmp_path,
            agent="codex",
            state_dir=tmp_path / "audit",
        )

        assert len(results) == 2
        assert results[0]["pr_number"] == 1
        assert results[1]["pr_number"] == 2
        mock_run_codex.assert_called_once()

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=True)
    @patch("hephaestus.automation.audit_reviewer.run_codex_text")
    def test_codex_path_handles_empty_stdout(
        self, mock_run_codex: Any, mock_is_codex: Any, tmp_path: Path
    ) -> None:
        """Codex path: empty/NULL stdout returns empty list gracefully."""
        pr_list = _make_pr_list()
        mock_result = Mock()
        mock_result.stdout = None
        mock_run_codex.return_value = mock_result

        results = audit_reviewer.run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=tmp_path,
            agent="codex",
            state_dir=tmp_path / "audit",
        )

        assert results == []

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=False)
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    @patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    def test_claude_path_parses_results(
        self,
        mock_invoke: Any,
        mock_repo_slug: Any,
        mock_repo_root: Any,
        mock_is_codex: Any,
        tmp_path: Path,
    ) -> None:
        """Claude path: runs invoke_claude_with_session and parses JSON output."""
        pr_list = _make_pr_list()
        mock_repo_root.return_value = tmp_path
        mock_repo_slug.return_value = "test-repo"
        mock_invoke.return_value = (
            '```json\n{"results": [{"pr_number": 1, "comments": [], "summary": "LGTM"}]}\n```',
            "session-sid",
        )

        results = audit_reviewer.run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=tmp_path,
            agent="claude",
            state_dir=tmp_path / "audit",
        )

        assert len(results) == 1
        assert results[0]["pr_number"] == 1
        mock_invoke.assert_called_once()

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=False)
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    @patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    def test_claude_path_handles_json_output_format(
        self,
        mock_invoke: Any,
        mock_repo_slug: Any,
        mock_repo_root: Any,
        mock_is_codex: Any,
        tmp_path: Path,
    ) -> None:
        """Claude path with json output_format: extracts result from JSON wrapper."""
        pr_list = _make_pr_list()
        mock_repo_root.return_value = tmp_path
        mock_repo_slug.return_value = "test-repo"
        # When output_format="json", stdout is a JSON dict with "result" key
        wrapped_json = json.dumps(
            {
                "result": (
                    '```json\n{"results": [{"pr_number": 1, "comments": [],'
                    ' "summary": "LGTM"}]}\n```'
                )
            }
        )
        mock_invoke.return_value = (wrapped_json, "session-sid")

        results = audit_reviewer.run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=tmp_path,
            agent="claude",
            state_dir=tmp_path / "audit",
        )

        assert len(results) == 1

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=False)
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    @patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    def test_claude_path_raises_on_called_process_error(
        self,
        mock_invoke: Any,
        mock_repo_slug: Any,
        mock_repo_root: Any,
        mock_is_codex: Any,
        tmp_path: Path,
    ) -> None:
        """Claude path: CalledProcessError raises RuntimeError with stderr."""
        pr_list = _make_pr_list()
        mock_repo_root.return_value = tmp_path
        mock_repo_slug.return_value = "test-repo"
        mock_invoke.side_effect = subprocess.CalledProcessError(
            1, "claude", output="", stderr="fatal error"
        )

        with pytest.raises(RuntimeError, match="fatal error"):
            audit_reviewer.run_audit_coordinator(
                pr_list=pr_list,
                worktree_path=tmp_path,
                agent="claude",
                state_dir=tmp_path / "audit",
            )

    @patch("hephaestus.automation.audit_reviewer.is_codex", return_value=False)
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    @patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    def test_claude_path_raises_on_timeout(
        self,
        mock_invoke: Any,
        mock_repo_slug: Any,
        mock_repo_root: Any,
        mock_is_codex: Any,
        tmp_path: Path,
    ) -> None:
        """Claude path: TimeoutExpired raises RuntimeError."""
        pr_list = _make_pr_list()
        mock_repo_root.return_value = tmp_path
        mock_repo_slug.return_value = "test-repo"
        mock_invoke.side_effect = subprocess.TimeoutExpired("claude", 300, output="")

        with pytest.raises(RuntimeError, match="timed out"):
            audit_reviewer.run_audit_coordinator(
                pr_list=pr_list,
                worktree_path=tmp_path,
                agent="claude",
                state_dir=tmp_path / "audit",
            )


class TestAuditReviewerRun:
    """Tests for AuditReviewer.run() orchestration method."""

    @patch("hephaestus.automation.audit_reviewer.write_audit_report")
    @patch("hephaestus.automation.audit_reviewer.print_audit_summary")
    @patch("hephaestus.automation.audit_reviewer.post_audit_results")
    @patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @patch("hephaestus.automation.audit_reviewer.gh_pr_list_open")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_happy_path(
        self,
        mock_get_root: Any,
        mock_list_open: Any,
        mock_run_coord: Any,
        mock_post: Any,
        mock_print: Any,
        mock_write: Any,
        tmp_path: Path,
    ) -> None:
        """Full happy path: list PRs → run coordinator → post → write → print."""
        pr_list = _make_pr_list()
        mock_get_root.return_value = str(tmp_path)
        mock_list_open.return_value = pr_list
        mock_run_coord.return_value = [
            {"pr_number": 1, "comments": [], "summary": "LGTM"},
            {"pr_number": 2, "comments": [], "summary": "LGTM"},
        ]
        mock_post.return_value = {1: True, 2: True}

        reviewer = audit_reviewer.AuditReviewer(agent="claude")
        exit_code = reviewer.run()

        assert exit_code == 0
        mock_list_open.assert_called_once()
        mock_run_coord.assert_called_once()
        assert mock_run_coord.call_args.kwargs["dry_run"] is False
        mock_post.assert_called_once()
        mock_write.assert_called_once()
        mock_print.assert_called_once()

    @patch("hephaestus.automation.audit_reviewer.gh_pr_list_open")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_no_open_prs(
        self,
        mock_get_root: Any,
        mock_list_open: Any,
        tmp_path: Path,
    ) -> None:
        """When no open PRs exist, returns 0 early without running coordinator."""
        mock_get_root.return_value = str(tmp_path)
        mock_list_open.return_value = []

        reviewer = audit_reviewer.AuditReviewer(agent="claude")
        exit_code = reviewer.run()

        assert exit_code == 0
        mock_list_open.assert_called_once()

    @patch("hephaestus.automation.audit_reviewer.gh_pr_list_open")
    @patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_coordinator_returns_empty(
        self,
        mock_get_root: Any,
        mock_run_coord: Any,
        mock_list_open: Any,
        tmp_path: Path,
    ) -> None:
        """When coordinator returns no results, returns 1."""
        pr_list = _make_pr_list()
        mock_get_root.return_value = str(tmp_path)
        mock_list_open.return_value = pr_list
        mock_run_coord.return_value = []

        reviewer = audit_reviewer.AuditReviewer(agent="claude")
        exit_code = reviewer.run()

        assert exit_code == 1

    @patch("hephaestus.automation.audit_reviewer.write_audit_report")
    @patch("hephaestus.automation.audit_reviewer.print_audit_summary")
    @patch("hephaestus.automation.audit_reviewer.post_audit_results")
    @patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @patch("hephaestus.automation.audit_reviewer.gh_pr_list_open")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_with_posting_failure(
        self,
        mock_get_root: Any,
        mock_list_open: Any,
        mock_run_coord: Any,
        mock_post: Any,
        mock_print: Any,
        mock_write: Any,
        tmp_path: Path,
    ) -> None:
        """When posting fails for some PRs, returns 1."""
        pr_list = _make_pr_list()
        mock_get_root.return_value = str(tmp_path)
        mock_list_open.return_value = pr_list
        mock_run_coord.return_value = [
            {"pr_number": 1, "comments": [], "summary": "LGTM"},
        ]
        mock_post.return_value = {1: False}

        reviewer = audit_reviewer.AuditReviewer(agent="claude")
        exit_code = reviewer.run()

        assert exit_code == 1

    @patch("hephaestus.automation.audit_reviewer.write_audit_report")
    @patch("hephaestus.automation.audit_reviewer.print_audit_summary")
    @patch("hephaestus.automation.audit_reviewer.post_audit_results")
    @patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_with_pr_numbers(
        self,
        mock_get_root: Any,
        mock_run_coord: Any,
        mock_post: Any,
        mock_print: Any,
        mock_write: Any,
        tmp_path: Path,
    ) -> None:
        """When pr_numbers are provided, uses _fetch_prs_by_number instead of gh_pr_list_open."""
        mock_get_root.return_value = str(tmp_path)
        mock_run_coord.return_value = [
            {"pr_number": 42, "comments": [], "summary": "LGTM"},
        ]
        mock_post.return_value = {42: True}

        with patch.object(audit_reviewer.AuditReviewer, "_fetch_prs_by_number") as mock_fetch:
            mock_fetch.return_value = [
                {
                    "number": 42,
                    "title": "Test",
                    "author": "alice",
                    "headRefName": "feat",
                    "baseRefName": "main",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "ci_status": "SUCCESS",
                },
            ]
            reviewer = audit_reviewer.AuditReviewer(agent="claude", pr_numbers=[42])
            exit_code = reviewer.run()

        assert exit_code == 0
        mock_fetch.assert_called_once_with([42])

    @patch("hephaestus.automation.audit_reviewer.gh_pr_list_open")
    @patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_run_dry_run_early_exit(
        self,
        mock_get_root: Any,
        mock_list_open: Any,
        tmp_path: Path,
    ) -> None:
        """Dry run: gh_pr_list_open returns empty → early exit 0."""
        mock_get_root.return_value = str(tmp_path)
        mock_list_open.return_value = []

        reviewer = audit_reviewer.AuditReviewer(agent="claude", dry_run=True)
        exit_code = reviewer.run()

        assert exit_code == 0
        mock_list_open.assert_called_once()


class TestAuditReviewerMain:
    """Smoke tests for audit_reviewer.main() CLI entry point."""

    def test_main_success(self, monkeypatch: Any) -> None:
        """When run() returns 0, main() exits 0."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_reviewer", "--dry-run", "--agent", "claude"],
        )
        with (
            patch.object(audit_reviewer.AuditReviewer, "__init__", return_value=None),
            patch.object(audit_reviewer.AuditReviewer, "run", return_value=0),
        ):
            assert audit_reviewer.main() == 0

    def test_main_failure(self, monkeypatch: Any) -> None:
        """When run() returns 1, main() exits 1."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_reviewer", "--dry-run", "--agent", "claude"],
        )
        with (
            patch.object(audit_reviewer.AuditReviewer, "__init__", return_value=None),
            patch.object(audit_reviewer.AuditReviewer, "run", return_value=1),
        ):
            assert audit_reviewer.main() == 1

    def test_main_keyboard_interrupt(self, monkeypatch: Any) -> None:
        """KeyboardInterrupt during run() returns 130."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_reviewer", "--dry-run", "--agent", "claude"],
        )
        with (
            patch.object(audit_reviewer.AuditReviewer, "__init__", return_value=None),
            patch.object(
                audit_reviewer.AuditReviewer,
                "run",
                MagicMock(side_effect=KeyboardInterrupt()),
            ),
        ):
            assert audit_reviewer.main() == 130

    def test_main_with_pr_numbers(self, monkeypatch: Any) -> None:
        """The --pr-numbers flag is passed through to AuditReviewer."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_reviewer", "--pr-numbers", "595", "596", "--dry-run", "--agent", "claude"],
        )
        init_mock = MagicMock(return_value=None)
        with (
            patch.object(audit_reviewer.AuditReviewer, "__init__", init_mock),
            patch.object(audit_reviewer.AuditReviewer, "run", return_value=0),
        ):
            audit_reviewer.main()
            assert init_mock.call_args.kwargs["pr_numbers"] == [595, 596]

    def test_main_with_limit(self, monkeypatch: Any) -> None:
        """The --limit flag is passed through to AuditReviewer."""
        monkeypatch.setattr(
            "sys.argv",
            ["audit_reviewer", "--limit", "20", "--dry-run", "--agent", "claude"],
        )
        init_mock = MagicMock(return_value=None)
        with (
            patch.object(audit_reviewer.AuditReviewer, "__init__", init_mock),
            patch.object(audit_reviewer.AuditReviewer, "run", return_value=0),
        ):
            audit_reviewer.main()
            assert init_mock.call_args.kwargs["limit"] == 20
