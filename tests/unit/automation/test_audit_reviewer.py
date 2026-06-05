"""Tests for hephaestus.automation.audit_reviewer.

Covers _parse_coordinator_results, post_audit_results, write_audit_report,
print_audit_summary, and the CLI main() smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from hephaestus.automation import audit_reviewer


class TestParseCoordinatorResults:
    """Tests for _parse_coordinator_results."""

    def test_extracts_last_json_block(self) -> None:
        text = 'some prose\n```json\n{"results": [{"pr_number": 1}]}\n```\ntrailing'
        results = audit_reviewer._parse_coordinator_results(text)
        assert results == [{"pr_number": 1}]

    def test_returns_last_json_block_when_multiple(self) -> None:
        text = (
            '```json\n{"results": [{"pr_number": 1}]}\n```\n'
            'more text\n'
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
        results: list[dict[str, Any]] = [
            {"pr_number": 1, "comments": [], "summary": "test"}
        ]
        posted = audit_reviewer.post_audit_results(results, dry_run=True)
        assert posted == {1: False}

    def test_skips_missing_pr_number(self) -> None:
        results: list[dict[str, Any]] = [
            {"comments": [], "summary": "no pr_number key"}
        ]
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
        results: list[dict[str, Any]] = [
            {"pr_number": 1, "comments": [], "summary": "test"}
        ]
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
            {"pr_number": 1, "comments": [{"path": "a.py", "line": 1, "body": "fix"}], "summary": "needs work"},
        ]
        posted = {1: True}

        audit_reviewer.print_audit_summary(results, posted)

        assert "Total PRs analysed" in caplog.text
        assert "Reviews posted" in caplog.text
        assert "needs work" in caplog.text


class TestAuditReviewerFetchPrsByNumber:
    """Tests for AuditReviewer._fetch_prs_by_number static method."""

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
            num = [a for a in args if a.isdigit()][0]
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
            patch.object(audit_reviewer.AuditReviewer, "run", MagicMock(side_effect=KeyboardInterrupt())),
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
