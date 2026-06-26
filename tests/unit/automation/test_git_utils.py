"""Compatibility tests for automation Git utility imports."""

from hephaestus.automation import git_utils
from hephaestus.utils import git as shared_git


def test_automation_git_utils_reexports_shared_run() -> None:
    """The automation import path remains a stable alias for shared Git helpers."""
    assert git_utils.run is shared_git.run


def test_automation_git_utils_reexports_shared_rebase_helper() -> None:
    """Automation callers keep using the same rebase helper through the wrapper."""
    assert git_utils.rebase_worktree_onto is shared_git.rebase_worktree_onto
