"""Tests for git utility functions."""

import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import ANY, Mock, patch

import pytest

from hephaestus.utils.git import (
    _remove_untracked_files_tracked_by_ref,
    clear_repo_caches,
    get_current_branch,
    get_repo_info,
    get_repo_root,
    git_branch_exists,
    git_config_get,
    git_diff_unmerged_names,
    git_ls_remote,
    git_push,
    git_remote_url,
    git_rev_list_count,
    git_rev_parse,
    git_show_toplevel,
    git_status_porcelain,
    is_clean_working_tree,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    run_git,
    safe_git_fetch,
    sync_worktree_to_remote_branch,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> Generator[None, None, None]:
    """Clear repo caches before each test to avoid cross-test interference."""
    clear_repo_caches()
    yield
    clear_repo_caches()


@pytest.mark.requires_posix
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="TestRun shells out to echo/false/ls; POSIX coreutils not guaranteed on win32 (#742)",
)
class TestRun:
    """Tests for run function."""

    def test_successful_command(self) -> None:
        """Test running a successful command."""
        result = run(["echo", "hello"], check=True, capture_output=True)

        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_failed_command_with_check(self) -> None:
        """Test running a failed command with check=True."""
        with pytest.raises(subprocess.CalledProcessError):
            run(["false"], check=True)

    def test_failed_command_without_check(self) -> None:
        """Test running a failed command with check=False."""
        result = run(["false"], check=False)
        assert result.returncode != 0

    def test_with_cwd(self, tmp_path: Any) -> None:
        """Test running command with custom working directory."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = run(["ls", "test.txt"], cwd=tmp_path, capture_output=True)

        assert result.returncode == 0
        assert "test.txt" in result.stdout


class TestRunGit:
    """Tests for Git-specific subprocess wrapper behavior."""

    @patch("hephaestus.utils.git.run")
    def test_prefixes_git_and_uses_metadata_timeout(self, mock_run: Any) -> None:
        """Local git commands are prefixed and bounded by the metadata timeout."""
        run_git(["status", "--porcelain"], cwd=Path("/repo"))

        args, kwargs = mock_run.call_args
        assert args[0] == ["git", "status", "--porcelain"]
        assert kwargs["cwd"] == Path("/repo")
        assert kwargs["timeout"] > 0

    @patch("hephaestus.utils.git.run")
    def test_accepts_git_prefixed_args_without_double_prefix(self, mock_run: Any) -> None:
        """Callers may pass an existing git-prefixed command during migration."""
        run_git(["git", "status"], cwd=Path("/repo"))

        args, _kwargs = mock_run.call_args
        assert args[0] == ["git", "status"]

    @patch("hephaestus.utils.git.run")
    def test_network_commands_use_network_timeout(self, mock_run: Any) -> None:
        """Network git commands get the longer network timeout by default."""
        run_git(["fetch", "origin"], cwd=Path("/repo"))

        assert mock_run.call_args.kwargs["timeout"] > 10

    @patch("hephaestus.utils.git.run")
    def test_dry_run_returns_success_without_running(self, mock_run: Any) -> None:
        """Dry-run mode logs intent and does not invoke the subprocess runner."""
        result = run_git(["push", "origin", "HEAD"], cwd=Path("/repo"), dry_run=True)

        assert result.returncode == 0
        assert result.args == ["git", "push", "origin", "HEAD"]
        mock_run.assert_not_called()

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.retry.time.sleep")
    def test_retries_transient_failures(self, _mock_sleep: Any, mock_run: Any) -> None:
        """Retry-enabled calls use the shared retry policy."""
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, ["git", "fetch"]),
            Mock(returncode=0, stdout="", stderr=""),
        ]

        result = run_git(["fetch", "origin"], cwd=Path("/repo"), retries=1)

        assert result.returncode == 0
        assert mock_run.call_count == 2


class TestGitConvenienceWrappers:
    """Tests for GitHub-facing helper wrappers."""

    @patch("hephaestus.utils.git.run_git")
    def test_git_config_get_returns_value(self, mock_run_git: Any) -> None:
        """Git config helper returns stripped stdout on success."""
        mock_run_git.return_value = Mock(returncode=0, stdout="dev@example.com\n")

        assert git_config_get("user.email", global_=True) == "dev@example.com"
        args, kwargs = mock_run_git.call_args
        assert args[0] == ["config", "--global", "--get", "user.email"]
        assert kwargs["check"] is False

    @patch("hephaestus.utils.git.run_git")
    def test_git_config_get_returns_none_when_unset(self, mock_run_git: Any) -> None:
        """Unset git config values return None without raising."""
        mock_run_git.return_value = Mock(returncode=1, stdout="")

        assert git_config_get("user.email") is None

    @patch("hephaestus.utils.git.run_git")
    def test_git_remote_url_returns_stripped_origin(self, mock_run_git: Any) -> None:
        """Remote URL helper returns the requested remote URL."""
        mock_run_git.return_value = Mock(returncode=0, stdout="git@github.com:owner/repo.git\n")

        assert git_remote_url() == "git@github.com:owner/repo.git"
        mock_run_git.assert_called_once_with(
            ["remote", "get-url", "origin"],
            cwd=None,
            check=True,
            timeout=ANY,
        )

    @patch("hephaestus.utils.git.run_git")
    def test_git_branch_exists_returns_true_for_output(self, mock_run_git: Any) -> None:
        """A non-empty branch listing means the local branch exists."""
        mock_run_git.return_value = Mock(returncode=0, stdout="  feature\n")

        assert git_branch_exists("feature") is True

    @patch("hephaestus.utils.git.run_git")
    def test_git_branch_exists_returns_false_on_timeout(self, mock_run_git: Any) -> None:
        """A hung branch lookup degrades to False."""
        mock_run_git.side_effect = subprocess.TimeoutExpired(["git"], 10)

        assert git_branch_exists("feature") is False

    @patch("hephaestus.utils.git.run_git")
    def test_git_status_porcelain_delegates(self, mock_run_git: Any) -> None:
        """Status helper delegates with check=False by default."""
        expected = Mock(returncode=0, stdout="")
        mock_run_git.return_value = expected

        assert git_status_porcelain(cwd=Path("/repo")) is expected
        mock_run_git.assert_called_once()
        assert mock_run_git.call_args.args[0] == ["status", "--porcelain"]
        assert mock_run_git.call_args.kwargs["check"] is False

    @patch("hephaestus.utils.git.run_git")
    def test_git_rev_parse_delegates(self, mock_run_git: Any) -> None:
        """rev-parse helper appends caller arguments."""
        expected = Mock(returncode=0, stdout=".git\n")
        mock_run_git.return_value = expected

        assert git_rev_parse(["--git-dir"], cwd=Path("/repo"), check=False) is expected
        assert mock_run_git.call_args.args[0] == ["rev-parse", "--git-dir"]
        assert mock_run_git.call_args.kwargs["check"] is False

    @patch("hephaestus.utils.git.run_git")
    def test_git_show_toplevel_returns_path(self, mock_run_git: Any) -> None:
        """show-toplevel helper parses stdout into a Path."""
        mock_run_git.return_value = Mock(returncode=0, stdout="/repo\n")

        assert git_show_toplevel() == Path("/repo")

    @patch("hephaestus.utils.git.run_git")
    def test_git_diff_unmerged_names_splits_lines(self, mock_run_git: Any) -> None:
        """Unmerged-file helper returns non-empty stripped lines."""
        mock_run_git.return_value = Mock(returncode=0, stdout="a.py\n\nb.py\n")

        assert git_diff_unmerged_names(Path("/repo")) == ["a.py", "b.py"]

    @patch("hephaestus.utils.git.run_git")
    def test_git_rev_list_count_parses_int(self, mock_run_git: Any) -> None:
        """rev-list count helper returns an int."""
        mock_run_git.return_value = Mock(returncode=0, stdout="3\n")

        assert git_rev_list_count(Path("/repo"), "origin/main..HEAD") == 3

    @patch("hephaestus.utils.git.run_git")
    def test_git_ls_remote_uses_network_timeout(self, mock_run_git: Any) -> None:
        """ls-remote helper is a network operation."""
        expected = Mock(returncode=0, stdout="sha\trefs/heads/feature\n")
        mock_run_git.return_value = expected

        assert git_ls_remote(Path("/repo"), "origin", "feature") is expected
        assert mock_run_git.call_args.args[0] == ["ls-remote", "origin", "feature"]
        assert mock_run_git.call_args.kwargs["check"] is False

    @patch("hephaestus.utils.git.run_git")
    def test_git_push_delegates_to_network_wrapper(self, mock_run_git: Any) -> None:
        """git_push delegates through run_git with retry support."""
        expected = Mock(returncode=0, stdout="")
        mock_run_git.return_value = expected

        assert git_push(Path("/repo"), "origin", "feature:feature", retries=2) is expected
        assert mock_run_git.call_args.args[0] == ["push", "origin", "feature:feature"]
        assert mock_run_git.call_args.kwargs["retries"] == 2


class TestGetRepoRoot:
    """Tests for get_repo_root function."""

    @patch("hephaestus.utils.helpers.Path.cwd")
    def test_successful_detection(self, mock_cwd: Any, tmp_path: Any) -> None:
        """Test successful repository root detection via the canonical resolver."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        mock_cwd.return_value = sub

        root = get_repo_root()

        assert root == repo

    def test_returns_path(self, tmp_path: Any) -> None:
        """Test that get_repo_root returns a Path object."""
        root = get_repo_root(tmp_path)
        assert isinstance(root, Path)


class TestGetRepoInfo:
    """Tests for get_repo_info function."""

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_ssh_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test parsing SSH URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        owner, repo = get_repo_info()

        assert owner == "owner"
        assert repo == "repo"

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_https_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test parsing HTTPS URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo.git\n"
        mock_run.return_value = mock_result

        owner, repo = get_repo_info()

        assert owner == "owner"
        assert repo == "repo"

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_invalid_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test handling invalid URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "invalid-url\n"
        mock_run.return_value = mock_result

        with pytest.raises(RuntimeError, match="Unable to parse git remote URL"):
            get_repo_info()

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_result_caching_prevents_repeated_run_calls(
        self, mock_get_root: Any, mock_run: Any
    ) -> None:
        """Test that repeated get_repo_info calls use cached result."""
        repo_root = Path("/home/user/repo")
        mock_get_root.return_value = repo_root
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        # First call should invoke run() and cache the result
        owner1, repo1 = get_repo_info(repo_root)
        assert owner1 == "owner"
        assert repo1 == "repo"
        assert mock_run.call_count == 1

        # Second call with same repo_root should return cached result without calling run()
        owner2, repo2 = get_repo_info(repo_root)
        assert owner2 == "owner"
        assert repo2 == "repo"
        assert mock_run.call_count == 1  # Should not increase

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_clear_repo_caches_forces_re_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test that clear_repo_caches forces re-detection on next call."""
        repo_root = Path("/home/user/repo")
        mock_get_root.return_value = repo_root
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        # First call caches the result
        get_repo_info(repo_root)
        assert mock_run.call_count == 1

        # Clear caches
        clear_repo_caches()

        # Next call should invoke run() again
        get_repo_info(repo_root)
        assert mock_run.call_count == 2


class TestGetCurrentBranch:
    """Tests for get_current_branch function."""

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_successful_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test successful branch detection."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "main\n"
        mock_run.return_value = mock_result

        branch = get_current_branch()

        assert branch == "main"

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_failed_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test failed branch detection."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")

        with pytest.raises(RuntimeError, match="Failed to get current branch"):
            get_current_branch()


class TestIsCleanWorkingTree:
    """Tests for is_clean_working_tree function."""

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_clean_tree(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test clean working tree."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        assert is_clean_working_tree() is True

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_dirty_tree(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test dirty working tree."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = " M modified_file.txt\n"
        mock_run.return_value = mock_result

        assert is_clean_working_tree() is False

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.git.get_repo_root")
    def test_error_returns_false(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test error returns False."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")

        assert is_clean_working_tree() is False


class TestSafeGitFetch:
    """Tests for safe_git_fetch function."""

    @patch("hephaestus.utils.git.run")
    def test_successful_fetch(self, mock_run: Any) -> None:
        """Test successful git fetch."""
        repo_root = Path("/home/user/repo")

        result = safe_git_fetch(repo_root, retries=1)

        assert result is True
        mock_run.assert_called_once()

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.retry.time.sleep")
    def test_retry_on_failure(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test retry on fetch failure."""
        repo_root = Path("/home/user/repo")
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),
            subprocess.CalledProcessError(1, "git"),
            Mock(),  # Success on third try
        ]

        result = safe_git_fetch(repo_root, retries=3)

        assert result is True
        assert mock_run.call_count == 3

    @patch("hephaestus.utils.git.run")
    @patch("hephaestus.utils.retry.time.sleep")
    def test_all_retries_fail(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test when all retries fail."""
        repo_root = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")

        result = safe_git_fetch(repo_root, retries=2)

        assert result is False
        # With retry_with_backoff(max_retries=2), it runs initial + 2 retries = 3
        assert mock_run.call_count == 3


class TestPushCurrentBranchWithLeaseOnDivergence:
    """Tests for push_current_branch_with_lease_on_divergence."""

    @patch("hephaestus.utils.git.run")
    def test_happy_path_plain_push(self, mock_run: Any) -> None:
        """Successful initial push: no fetch, no force, single git invocation."""
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        result = push_current_branch_with_lease_on_divergence(worktree)

        assert result is mock_run.return_value
        assert mock_run.call_count == 1
        args, _kwargs = mock_run.call_args
        assert args[0] == ["git", "push", "origin", "HEAD"]

    @patch("hephaestus.utils.git.run")
    def test_non_fast_forward_triggers_fetch_and_lease(self, mock_run: Any) -> None:
        """non-fast-forward rejection → fetch + force-with-lease retry succeeds."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr=" ! [rejected]   HEAD -> 511-impl (non-fast-forward)\n",
        )
        # Sequence: push fails, get_current_branch, fetch, lease push (all succeed).
        mock_run.side_effect = [
            push_err,
            Mock(returncode=0, stdout="511-impl\n"),  # get_current_branch
            Mock(returncode=0),  # fetch
            Mock(returncode=0),  # lease push
        ]

        result = push_current_branch_with_lease_on_divergence(worktree)

        assert result.returncode == 0
        assert mock_run.call_count == 4
        # 3rd call must be a fetch of the diverged branch.
        fetch_args, _ = mock_run.call_args_list[2]
        assert fetch_args[0] == ["git", "fetch", "origin", "511-impl"]
        # 4th call must be the lease-protected push.
        lease_args, _ = mock_run.call_args_list[3]
        assert lease_args[0] == [
            "git",
            "push",
            "--force-with-lease=511-impl",
            "origin",
            "HEAD:511-impl",
        ]

    @patch("hephaestus.utils.git.run")
    def test_fetch_first_also_triggers_lease(self, mock_run: Any) -> None:
        """'fetch first' rejection text triggers the same retry path."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr=" ! [rejected]   HEAD -> 43-impl (fetch first)\n",
        )
        mock_run.side_effect = [
            push_err,
            Mock(returncode=0, stdout="43-impl\n"),
            Mock(returncode=0),
            Mock(returncode=0),
        ]

        push_current_branch_with_lease_on_divergence(worktree)
        # Confirm the retry path executed (4 git invocations).
        assert mock_run.call_count == 4

    @patch("hephaestus.utils.git.run")
    def test_branch_arg_skips_get_current_branch(self, mock_run: Any) -> None:
        """If caller passes branch=, no rev-parse call is needed for lease retry."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="non-fast-forward\n",
        )
        # Only 3 calls now: failed push, fetch, lease push.
        mock_run.side_effect = [push_err, Mock(returncode=0), Mock(returncode=0)]

        push_current_branch_with_lease_on_divergence(worktree, branch="577-nomad-vessel-groups")

        assert mock_run.call_count == 3
        fetch_args, _ = mock_run.call_args_list[1]
        assert fetch_args[0] == ["git", "fetch", "origin", "577-nomad-vessel-groups"]
        lease_args, _ = mock_run.call_args_list[2]
        assert "--force-with-lease=577-nomad-vessel-groups" in lease_args[0]

    @patch("hephaestus.utils.git.run")
    def test_unrelated_push_failure_raises(self, mock_run: Any) -> None:
        """Auth/network failures (not divergence) propagate without retry."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            128,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="fatal: could not read Username for 'https://github.com'\n",
        )
        mock_run.side_effect = [push_err]

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            push_current_branch_with_lease_on_divergence(worktree)
        # The original (non-divergence) error is re-raised — not silently swallowed.
        assert exc_info.value.returncode == 128
        assert mock_run.call_count == 1

    @patch("hephaestus.utils.git.run")
    def test_lease_push_failure_propagates(self, mock_run: Any) -> None:
        """If the lease-protected retry also fails, the caller sees that error."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="non-fast-forward\n",
        )
        lease_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "--force-with-lease=511-impl", "origin", "HEAD:511-impl"],
            output="",
            stderr="stale info\n",
        )
        mock_run.side_effect = [push_err, Mock(returncode=0), lease_err]

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            push_current_branch_with_lease_on_divergence(worktree, branch="511-impl")
        assert exc_info.value.stderr == "stale info\n"

    @patch("hephaestus.utils.git.run")
    def test_explicit_push_ref_used_on_initial_push(self, mock_run: Any) -> None:
        """When push_ref is set, the initial push uses it as the refspec (#832)."""
        # Without this, a Claude-side branch switch would route the push to a
        # stray branch instead of the PR's head.
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        push_current_branch_with_lease_on_divergence(
            worktree, branch="5391-auto-impl", push_ref="HEAD:5391-auto-impl"
        )

        args, _ = mock_run.call_args
        assert args[0] == ["git", "push", "origin", "HEAD:5391-auto-impl"]

    @patch("hephaestus.utils.git.run")
    def test_explicit_push_ref_preserved_in_lease_retry(self, mock_run: Any) -> None:
        """The lease-retry path must use the same explicit push_ref (#832)."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD:5391-auto-impl"],
            output="",
            stderr="non-fast-forward\n",
        )
        mock_run.side_effect = [push_err, Mock(returncode=0), Mock(returncode=0)]

        push_current_branch_with_lease_on_divergence(
            worktree, branch="5391-auto-impl", push_ref="HEAD:5391-auto-impl"
        )

        # 3rd call must be the lease push using the explicit refspec.
        lease_args, _ = mock_run.call_args_list[2]
        assert lease_args[0] == [
            "git",
            "push",
            "--force-with-lease=5391-auto-impl",
            "origin",
            "HEAD:5391-auto-impl",
        ]


class TestSyncWorktreeToRemoteBranch:
    """Tests for sync_worktree_to_remote_branch (#832 — reset before agent)."""

    @patch("hephaestus.utils.git.run")
    def test_fetches_then_resets_to_remote_head(self, mock_run: Any) -> None:
        """Runs ``git fetch origin <branch>`` then ``git reset --hard origin/<branch>``."""
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        sync_worktree_to_remote_branch(worktree, "5450-auto-impl")

        assert mock_run.call_count == 2
        fetch_args, fetch_kwargs = mock_run.call_args_list[0]
        assert fetch_args[0] == ["git", "fetch", "origin", "5450-auto-impl"]
        assert fetch_kwargs["cwd"] == worktree
        reset_args, reset_kwargs = mock_run.call_args_list[1]
        assert reset_args[0] == ["git", "reset", "--hard", "origin/5450-auto-impl"]
        assert reset_kwargs["cwd"] == worktree

    @patch("hephaestus.utils.git.run")
    def test_fetch_failure_propagates(self, mock_run: Any) -> None:
        """If fetch fails, raise — we cannot safely reset to a stale ref."""
        fetch_err = subprocess.CalledProcessError(
            128,
            ["git", "fetch"],
            output="",
            stderr="fatal: unable to access remote\n",
        )
        mock_run.side_effect = [fetch_err]

        with pytest.raises(subprocess.CalledProcessError):
            sync_worktree_to_remote_branch(Path("/tmp/worktree-xyz"), "any-branch")
        # Reset must NOT have run after a fetch failure.
        assert mock_run.call_count == 1


class TestRebaseWorktreeOnto:
    """Tests for rebase_worktree_onto (#871 — mechanical rebase before agent)."""

    @patch("hephaestus.utils.git.run")
    def test_clean_rebase_fetches_then_rebases_returns_true(self, mock_run: Any) -> None:
        """Runs ``git fetch origin <base>`` then ``git rebase origin/<base>`` → True."""
        mock_run.return_value = Mock(returncode=0, stdout="")
        worktree = Path("/tmp/worktree-xyz")

        assert rebase_worktree_onto(worktree, "main") is True

        assert mock_run.call_count == 3
        fetch_args, fetch_kwargs = mock_run.call_args_list[0]
        assert fetch_args[0] == ["git", "fetch", "origin", "main"]
        assert fetch_kwargs["cwd"] == worktree
        ls_files_args, ls_files_kwargs = mock_run.call_args_list[1]
        assert ls_files_args[0] == ["git", "ls-files", "--others", "--exclude-standard", "-z"]
        assert ls_files_kwargs["cwd"] == worktree
        rebase_args, rebase_kwargs = mock_run.call_args_list[2]
        assert rebase_args[0] == ["git", "rebase", "origin/main"]
        assert rebase_kwargs["cwd"] == worktree

    @patch("hephaestus.utils.git.run")
    def test_conflict_aborts_and_returns_false(self, mock_run: Any) -> None:
        """A rebase conflict triggers ``git rebase --abort`` and returns False."""
        rebase_err = subprocess.CalledProcessError(
            1, ["git", "rebase"], output="", stderr="CONFLICT (content)\n"
        )
        # fetch ok, no stale untracked files, rebase conflicts, abort ok.
        mock_run.side_effect = [
            Mock(returncode=0),
            Mock(returncode=0, stdout=""),
            rebase_err,
            Mock(returncode=0),
        ]
        worktree = Path("/tmp/worktree-xyz")

        assert rebase_worktree_onto(worktree, "main") is False

        assert mock_run.call_count == 4
        abort_args, abort_kwargs = mock_run.call_args_list[3]
        assert abort_args[0] == ["git", "rebase", "--abort"]
        # The abort must be best-effort so it cannot mask the conflict signal.
        assert abort_kwargs.get("check") is False

    @patch("hephaestus.utils.git.run")
    def test_removes_untracked_files_that_are_tracked_by_base_ref(
        self, mock_run: Any, tmp_path: Path
    ) -> None:
        """Stale untracked files from old agent turns should not block rebase."""
        tracked_shadow = tmp_path / "scripts" / "check_conventional_commit.py"
        unrelated = tmp_path / "scratch.txt"
        tracked_shadow.parent.mkdir()
        tracked_shadow.write_text("old local copy\n")
        unrelated.write_text("keep me\n")
        missing_from_ref = subprocess.CalledProcessError(
            1, ["git", "cat-file", "-e"], output="", stderr="missing\n"
        )
        mock_run.side_effect = [
            Mock(
                returncode=0,
                stdout="scripts/check_conventional_commit.py\0scratch.txt\0",
            ),
            Mock(returncode=0),
            missing_from_ref,
        ]

        removed = _remove_untracked_files_tracked_by_ref(tmp_path, "origin/main")

        assert removed == [Path("scripts/check_conventional_commit.py")]
        assert not tracked_shadow.exists()
        assert unrelated.exists()

    @patch("hephaestus.utils.git.run")
    def test_fetch_failure_propagates(self, mock_run: Any) -> None:
        """A fetch failure is a hard error (no current base) — it raises."""
        fetch_err = subprocess.CalledProcessError(
            128, ["git", "fetch"], output="", stderr="fatal: unable to access remote\n"
        )
        mock_run.side_effect = [fetch_err]

        with pytest.raises(subprocess.CalledProcessError):
            rebase_worktree_onto(Path("/tmp/worktree-xyz"), "main")
        # Rebase must NOT have run after a fetch failure.
        assert mock_run.call_count == 1

    @patch("hephaestus.utils.git.run")
    def test_custom_base_and_remote(self, mock_run: Any) -> None:
        """Base branch and remote are threaded into both git commands."""
        mock_run.return_value = Mock(returncode=0, stdout="")

        assert rebase_worktree_onto(Path("/wt"), "develop", remote="upstream") is True

        assert mock_run.call_args_list[0][0][0] == ["git", "fetch", "upstream", "develop"]
        assert mock_run.call_args_list[2][0][0] == ["git", "rebase", "upstream/develop"]
