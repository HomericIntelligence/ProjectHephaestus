"""Unit tests for hephaestus.automation.loop_repo_manager.

Patches the new module path so these tests exercise the extracted helpers
independently. Existing tests in test_loop_runner.py remain on the
loop_runner namespace (which re-exports all 12 names) and do not need
to be changed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_repo_manager


class TestDetectCwdRepo:
    def test_scp_like_remote_parses_org(self) -> None:
        with (
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run,
        ):
            run.side_effect = [
                MagicMock(stdout="/home/u/Projects/MyRepo\n", returncode=0),
                MagicMock(stdout="git@github.com:MyOrg/MyRepo.git\n", returncode=0),
            ]
            org, repo = loop_repo_manager._detect_cwd_repo()
        assert org == "MyOrg"
        assert repo == "MyRepo"

    def test_https_remote_parses_org(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = [
                MagicMock(stdout="/home/u/Projects/MyRepo\n", returncode=0),
                MagicMock(
                    stdout="https://github.com/AnotherOrg/SomeRepo.git\n", returncode=0
                ),
            ]
            org, repo = loop_repo_manager._detect_cwd_repo()
        assert org == "AnotherOrg"
        assert repo == "MyRepo"

    def test_non_github_remote_returns_none_org(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = [
                MagicMock(stdout="/home/u/Projects/MyRepo\n", returncode=0),
                MagicMock(stdout="git@gitlab.com:MyOrg/MyRepo.git\n", returncode=0),
            ]
            org, repo = loop_repo_manager._detect_cwd_repo()
        assert org is None
        assert repo == "MyRepo"

    def test_not_a_git_repo_returns_none_none(self) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = subprocess.CalledProcessError(128, "git")
            org, repo = loop_repo_manager._detect_cwd_repo()
        assert org is None
        assert repo is None

    def test_remote_url_fetch_fails_returns_none_org(self) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = [
                MagicMock(stdout="/home/u/Projects/MyRepo\n", returncode=0),
                subprocess.CalledProcessError(128, "git"),
            ]
            org, repo = loop_repo_manager._detect_cwd_repo()
        assert org is None
        assert repo == "MyRepo"


class TestGhListRepos:
    def test_filters_archived_and_forks(self) -> None:
        import json

        repos = [
            {"name": "active", "isArchived": False, "isFork": False},
            {"name": "archived", "isArchived": True, "isFork": False},
            {"name": "forked", "isArchived": False, "isFork": True},
        ]
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(repos), stderr=""
            )
            result = loop_repo_manager._gh_list_repos("MyOrg")
        assert result == ["active"]

    def test_nonzero_rc_raises_systemexit(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")
            with pytest.raises(SystemExit, match="failed"):
                loop_repo_manager._gh_list_repos("MyOrg")

    def test_invalid_json_raises_systemexit(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="not-json", stderr="")
            with pytest.raises(SystemExit, match="invalid JSON"):
                loop_repo_manager._gh_list_repos("MyOrg")

    def test_timeout_raises_systemexit(self) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(["gh"], timeout=30)
            with pytest.raises(SystemExit, match="timed out"):
                loop_repo_manager._gh_list_repos("MyOrg")


class TestListOpenIssueNumbers:
    def test_sorted_ascending(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="42\n7\n100\n", stderr="")
            result = loop_repo_manager._list_open_issue_numbers("Org", "Repo")
        assert result == [7, 42, 100]

    def test_timeout_returns_empty(self) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(["gh"], timeout=30)
            result = loop_repo_manager._list_open_issue_numbers("Org", "Repo")
        assert result == []

    def test_nonzero_rc_returns_empty(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = loop_repo_manager._list_open_issue_numbers("Org", "Repo")
        assert result == []

    def test_empty_output_returns_empty(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = loop_repo_manager._list_open_issue_numbers("Org", "Repo")
        assert result == []

    def test_ignores_non_digit_lines(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="5\nbad\n10\n", stderr="")
            result = loop_repo_manager._list_open_issue_numbers("Org", "Repo")
        assert result == [5, 10]


class TestCountOpenIssues:
    def test_delegates_to_list_open_issue_numbers(self) -> None:
        with patch.object(
            loop_repo_manager,
            "_list_open_issue_numbers",
            return_value=[1, 2, 3],
        ):
            assert loop_repo_manager._count_open_issues("Org", "Repo") == 3

    def test_zero_when_no_issues(self) -> None:
        with patch.object(
            loop_repo_manager, "_list_open_issue_numbers", return_value=[]
        ):
            assert loop_repo_manager._count_open_issues("Org", "Repo") == 0


class TestCountFailingPrs:
    def test_counts_only_failing(self) -> None:
        import json

        pulls = [
            {"number": 1, "isDraft": False, "statusCheckRollup": [], "mergeStateStatus": "CLEAN"},
            {"number": 2, "isDraft": False, "statusCheckRollup": [], "mergeStateStatus": "CLEAN"},
        ]
        with (
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run,
            patch("hephaestus.automation.loop_repo_manager._pr_is_failing") as failing,
        ):
            run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(pulls), stderr=""
            )
            failing.side_effect = [True, False]
            result = loop_repo_manager._count_failing_prs("Org", "Repo")
        assert result == 1

    def test_timeout_returns_zero(self) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(["gh"], timeout=30)
            assert loop_repo_manager._count_failing_prs("Org", "Repo") == 0

    def test_parse_error_returns_zero(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="bad-json", stderr="")
            assert loop_repo_manager._count_failing_prs("Org", "Repo") == 0

    def test_nonzero_rc_returns_zero(self) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert loop_repo_manager._count_failing_prs("Org", "Repo") == 0


class TestSortReposByOpenCount:
    def test_orders_ascending_stable(self) -> None:
        counts = {"alpha": 5, "beta": 2, "gamma": 8}
        with patch.object(
            loop_repo_manager,
            "_count_open_issues",
            side_effect=lambda org, repo: counts[repo],
        ):
            result = loop_repo_manager._sort_repos_by_open_count(
                "Org", ["alpha", "beta", "gamma"]
            )
        assert result == ["beta", "alpha", "gamma"]

    def test_preserves_original_order_on_tie(self) -> None:
        with patch.object(
            loop_repo_manager, "_count_open_issues", return_value=3
        ):
            result = loop_repo_manager._sort_repos_by_open_count("Org", ["a", "b", "c"])
        assert result == ["a", "b", "c"]


class TestResolveRepoDir:
    def test_joins_projects_dir(self, tmp_path: Path) -> None:
        result = loop_repo_manager._resolve_repo_dir(tmp_path, "MyRepo")
        assert result == tmp_path / "MyRepo"


class TestRebaseHelpers:
    def test_detect_remote_base_ref_symbolic(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="origin/main\n", stderr=""
            )
            result = loop_repo_manager._detect_remote_base_ref("repo", tmp_path)
        assert result == "origin/main"

    def test_detect_remote_base_ref_fallback_main(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            # symbolic-ref fails, then verify origin/main succeeds
            run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="abc1234\n", stderr=""),
            ]
            result = loop_repo_manager._detect_remote_base_ref("repo", tmp_path)
        assert result == "origin/main"

    def test_detect_remote_base_ref_fallback_default(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            # symbolic-ref fails, both verify calls fail
            run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr=""),
                MagicMock(returncode=1, stdout="", stderr=""),
                MagicMock(returncode=1, stdout="", stderr=""),
            ]
            result = loop_repo_manager._detect_remote_base_ref("repo", tmp_path)
        assert result == "origin/main"

    def test_local_ahead_count_parses_int(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="3\n", stderr="")
            result = loop_repo_manager._local_ahead_count("repo", tmp_path, "origin/main")
        assert result == 3

    def test_local_ahead_count_invalid_returns_zero(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="not-a-number\n", stderr="")
            result = loop_repo_manager._local_ahead_count("repo", tmp_path, "origin/main")
        assert result == 0

    def test_local_ahead_count_nonzero_rc_returns_zero(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
            result = loop_repo_manager._local_ahead_count("repo", tmp_path, "origin/main")
        assert result == 0

    def test_local_ahead_count_timeout_returns_zero(self, tmp_path: Path) -> None:
        import subprocess

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(["git"], timeout=10)
            result = loop_repo_manager._local_ahead_count("repo", tmp_path, "origin/main")
        assert result == 0
