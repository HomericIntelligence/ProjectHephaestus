"""Tests for the show-prompt CLI (issue #1170)."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from scripts.show_prompt import STAGES, build_parser, build_prompt, main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_issue_data() -> dict:
    return {
        "title": "Test Issue Title",
        "body": "Test issue body with acceptance criteria.",
        "comments": [
            {"body": "Some random comment"},
            {"body": "# Implementation Plan\n\n## Approach\nDo stuff."},
        ],
    }


@pytest.fixture()
def mock_extract_comment() -> str:
    return "# Implementation Plan\n\n## Approach\nCreate foo.py with bar()."


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestBuildParser:
    def test_requires_issue(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--stage", "planning"])

    def test_requires_stage(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--issue", "1"])

    def test_invalid_stage_rejected(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--issue", "1", "--stage", "bogus"])

    def test_valid_args_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--issue", "42", "--stage", "planning"])
        assert args.issue == 42
        assert args.stage == "planning"
        assert args.repo == "HomericIntelligence/ProjectHephaestus"

    def test_optional_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--issue", "10",
            "--stage", "implementation",
            "--branch", "feature/foo",
            "--worktree", "/tmp/wt",
            "--pr", "5",
            "--iteration", "2",
        ])
        assert args.branch == "feature/foo"
        assert args.worktree == "/tmp/wt"
        assert args.pr == 5
        assert args.iteration == 2


# ---------------------------------------------------------------------------
# build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_planning_stage(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        mock_extract.return_value = "# Implementation Plan"
        prompt = build_prompt("planning", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_review_stage(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_extract.return_value = "# Plan"
        prompt = build_prompt("plan-review", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt._extract_plan_from_issue_data")
    def test_plan_loop_review_stage(
        self, mock_extract: MagicMock, mock_issue: MagicMock
    ) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_extract.return_value = "# Plan"
        prompt = build_prompt("plan-loop-review", 1, "owner/repo", iteration=1)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_implementation_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt(
            "implementation", 1, "owner/repo",
            branch_name="feat/x", worktree_path="/tmp/wt",
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_impl_review_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("impl-review", 1, "owner/repo", iteration=0)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_impl_resume_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("impl-resume", 1, "owner/repo", iteration=1)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_pr_review_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("pr-review", 1, "owner/repo", pr_number=5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    @patch("scripts.show_prompt.fetch_pr_threads")
    def test_address_review_stage(
        self, mock_threads: MagicMock, mock_issue: MagicMock
    ) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        mock_threads.return_value = "[]"
        prompt = build_prompt("address-review", 1, "owner/repo", pr_number=5)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_follow_up_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("follow-up", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    @patch("scripts.show_prompt.fetch_issue")
    def test_advise_stage(self, mock_issue: MagicMock) -> None:
        mock_issue.return_value = {"title": "T", "body": "B", "comments": []}
        prompt = build_prompt("advise", 1, "owner/repo")
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_unknown_stage_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown stage"):
            build_prompt("bogus", 1, "owner/repo")


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

class TestMain:
    @patch("scripts.show_prompt.build_prompt")
    def test_main_prints_prompt(self, mock_build: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        mock_build.return_value = "THE PROMPT TEXT"
        rc = main(["--issue", "1", "--stage", "planning"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "THE PROMPT TEXT" in captured.out

    @patch("scripts.show_prompt.build_prompt")
    def test_main_returns_1_on_error(self, mock_build: MagicMock) -> None:
        mock_build.side_effect = RuntimeError("gh failed")
        rc = main(["--issue", "1", "--stage", "planning"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Coverage: all stages are listed
# ---------------------------------------------------------------------------

def test_all_stages_covered() -> None:
    expected = {
        "planning",
        "plan-review",
        "plan-loop-review",
        "implementation",
        "impl-review",
        "impl-resume",
        "pr-review",
        "address-review",
        "follow-up",
        "advise",
    }
    assert set(STAGES) == expected
