"""Integration smoke tests for the audit reviewer module.

Validates that the audit-reviewer module works correctly end-to-end without
requiring live GitHub or agent access.

These tests complement the auto-discovered CLI entry-point checks in
``test_cli_entry_points.py`` (which cover ``--help``, ``--json``, and
importability for every ``[project.scripts]`` entry) with functional
smoke tests of the core parsing, reporting, and class-construction paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Module importability
# ---------------------------------------------------------------------------


class TestAuditReviewerImportable:
    """The audit reviewer module must be importable without errors."""

    def test_module_importable(self) -> None:
        """Verify the module can be imported."""
        try:
            import hephaestus.automation.audit_reviewer  # noqa: F401
        except ImportError as e:
            pytest.fail(f"audit_reviewer module failed to import: {e}")

    def test_key_symbols_exported(self) -> None:
        """Verify all public API symbols are accessible."""
        from hephaestus.automation import audit_reviewer

        expected = [
            "AuditReviewer",
            "run_audit_coordinator",
            "post_audit_results",
            "write_audit_report",
            "print_audit_summary",
            "_parse_coordinator_results",
        ]
        for name in expected:
            assert hasattr(audit_reviewer, name), f"audit_reviewer.{name} not found"


# ---------------------------------------------------------------------------
# _parse_coordinator_results — integration-level smoke
# ---------------------------------------------------------------------------


class TestParseCoordinatorResultsIntegration:
    """End-to-end parsing of realistic coordinator output."""

    @staticmethod
    def _coordinator_text(results: list[dict]) -> str:
        """Build a realistic coordinator response with prose + JSON block."""
        prose = (
            "I've dispatched sub-agents for all 3 PRs.\n\n"
            "PR #100: Clean refactor, LGTM.\n"
            "PR #101: Found a potential issue.\n"
            "PR #102: Looks good with minor nits.\n"
        )
        block = "```json\n" + json.dumps({"results": results}) + "\n```"
        return prose + "\n" + block

    def test_parse_realistic_output(self) -> None:
        """Parse a realistic coordinator response with prose + JSON block."""
        from hephaestus.automation.audit_reviewer import (
            _parse_coordinator_results,
        )

        text = self._coordinator_text(
            [
                {
                    "pr_number": 100,
                    "comments": [],
                    "summary": "LGTM — clean refactor",
                },
                {
                    "pr_number": 101,
                    "comments": [
                        {
                            "path": "src/foo.py",
                            "line": 42,
                            "side": "RIGHT",
                            "body": "Possible race condition here",
                        }
                    ],
                    "summary": "Needs attention — race condition",
                },
            ]
        )

        results = _parse_coordinator_results(text)
        assert len(results) == 2
        assert results[0]["pr_number"] == 100
        assert results[0]["summary"] == "LGTM — clean refactor"
        assert results[1]["pr_number"] == 101
        assert len(results[1]["comments"]) == 1

    def test_parse_multiple_json_blocks_uses_last(self) -> None:
        """Only the last ```json``` block is parsed."""
        from hephaestus.automation.audit_reviewer import (
            _parse_coordinator_results,
        )

        text = (
            "```json\n"
            + json.dumps({"results": [{"pr_number": 1, "comments": [], "summary": "old"}]})
            + "\n```\n"
            "Some more prose...\n"
            "```json\n"
            + json.dumps({"results": [{"pr_number": 2, "comments": [], "summary": "new"}]})
            + "\n```"
        )

        results = _parse_coordinator_results(text)
        assert len(results) == 1
        assert results[0]["pr_number"] == 2
        assert results[0]["summary"] == "new"


# ---------------------------------------------------------------------------
# write_audit_report — integration-level smoke
# ---------------------------------------------------------------------------


class TestWriteAuditReportIntegration:
    """End-to-end audit report writing."""

    def test_writes_valid_json_report(self, tmp_path: Path) -> None:
        """Report file must be valid JSON with expected top-level keys."""
        from hephaestus.automation.audit_reviewer import write_audit_report

        results = [
            {
                "pr_number": 100,
                "comments": [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "nit"}],
                "summary": "One nit",
            }
        ]
        posted = {100: True}

        report_path = write_audit_report(results, posted, tmp_path)
        assert report_path.exists()
        assert report_path.suffix == ".json"

        data = json.loads(report_path.read_text())
        assert data["total_prs"] == 1
        assert data["posted"] == 1
        assert data["failed"] == 0
        assert data["results"][0]["pr_number"] == 100
        assert data["results"][0]["comment_count"] == 1
        assert data["results"][0]["posted"] is True

    def test_report_with_posting_failures(self, tmp_path: Path) -> None:
        """Failed postings must be reflected in the report."""
        from hephaestus.automation.audit_reviewer import write_audit_report

        results = [
            {"pr_number": 100, "comments": [], "summary": "ok"},
            {"pr_number": 101, "comments": [], "summary": "ok"},
        ]
        posted = {100: True, 101: False}

        report_path = write_audit_report(results, posted, tmp_path)
        data = json.loads(report_path.read_text())

        assert data["total_prs"] == 2
        assert data["posted"] == 1
        assert data["failed"] == 1


# ---------------------------------------------------------------------------
# print_audit_summary — integration-level smoke
# ---------------------------------------------------------------------------


class TestPrintAuditSummaryIntegration:
    """End-to-end summary printing."""

    def test_prints_summary_without_errors(self, caplog) -> None:
        """print_audit_summary must log the summary including per-PR lines."""
        from hephaestus.automation.audit_reviewer import print_audit_summary

        results = [
            {"pr_number": 100, "comments": [], "summary": "LGTM"},
            {
                "pr_number": 101,
                "comments": [{"path": "x.py", "line": 1, "side": "RIGHT", "body": "bug"}],
                "summary": "Has issues",
            },
        ]
        posted = {100: True, 101: False}

        with caplog.at_level(logging.INFO):
            print_audit_summary(results, posted)

        log_text = "\n".join(caplog.messages)
        assert "PR Audit Review Summary" in log_text
        assert "Total PRs analysed" in log_text
        assert "Reviews posted" in log_text
        assert "Post failures" in log_text
        # Per-PR verdict lines must appear with PR number, verdict, and counts
        assert "PR #100" in log_text
        assert "LGTM" in log_text
        assert "PR #101" in log_text
        assert "Has issues" in log_text
        assert "0 comment" in log_text  # PR #100
        assert "1 comment" in log_text  # PR #101


# ---------------------------------------------------------------------------
# AuditReviewer construction
# ---------------------------------------------------------------------------


class TestAuditReviewerConstruction:
    """AuditReviewer must be constructable with various parameter combinations."""

    def test_default_construction(self) -> None:
        """Default parameters produce a valid instance."""
        from hephaestus.automation.audit_reviewer import AuditReviewer

        reviewer = AuditReviewer()
        assert reviewer.agent == "claude"
        assert reviewer.dry_run is False
        assert reviewer.limit == 100
        assert reviewer.pr_numbers is None
        assert isinstance(reviewer.repo_root, Path)

    def test_codex_construction(self) -> None:
        """Codex agent parameter is accepted."""
        from hephaestus.automation.audit_reviewer import AuditReviewer

        reviewer = AuditReviewer(agent="codex", dry_run=True, limit=10)
        assert reviewer.agent == "codex"
        assert reviewer.dry_run is True
        assert reviewer.limit == 10

    def test_pr_numbers_construction(self) -> None:
        """Explicit PR numbers parameter is accepted."""
        from hephaestus.automation.audit_reviewer import AuditReviewer

        reviewer = AuditReviewer(pr_numbers=[595, 596])
        assert reviewer.pr_numbers == [595, 596]

    def test_state_dir_is_under_build(self) -> None:
        """State directory must end with ``build/.audit`` under the repo root."""
        from hephaestus.automation.audit_reviewer import AuditReviewer

        reviewer = AuditReviewer()
        assert str(reviewer.state_dir).endswith("build/.audit")
