"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches."""

import pytest

from hephaestus.github.tidy import parse_problem_branches


# Fixture: clean gh-tidy run (no problem branches)
CLEAN_OUTPUT = """\
Checking out main and pulling the latest from remote origin...
Finished tidying!
"""

# Fixture: one problem branch
ONE_PROBLEM = """\
Rebasing ALL local branches on to latest master...
Rebasing feature/my-branch...
WARNING: Problem rebasing feature/my-branch
Finished rebasing!

Cleaning unnecessary files & optimizing your local repo...
WARNING: Unable to auto-rebase the following branches:
    * feature/my-branch

Finished tidying!
"""

# Fixture: multiple problem branches
MULTI_PROBLEM = """\
WARNING: Unable to auto-rebase the following branches:
    * feature/alpha
    * fix/beta-crash
    * chore/deps-update

Finished tidying!
"""

# Fixture: ANSI-coloured output (gh-tidy emits \e[93m yellow for warnings)
ANSI_PROBLEM = (
    "\x1b[93mWARNING: Unable to auto-rebase the following branches:\x1b[0m\n"
    "\x1b[93m    * feature/with-ansi\x1b[0m\n"
    "\x1b[92mFinished tidying!\x1b[0m\n"
)

# Fixture: problem header with no bullets (edge case — header present, no branch listed)
EMPTY_PROBLEM_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:

Finished tidying!
"""

# Fixture: problem header where a non-bullet line immediately follows
TRAILING_TEXT_AFTER_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:
    * chore/broken
Please fix manually.
Finished tidying!
"""


def test_clean_output_returns_empty():
    assert parse_problem_branches(CLEAN_OUTPUT) == []


def test_single_problem_branch():
    result = parse_problem_branches(ONE_PROBLEM)
    assert result == ["feature/my-branch"]


def test_multiple_problem_branches():
    result = parse_problem_branches(MULTI_PROBLEM)
    assert result == ["feature/alpha", "fix/beta-crash", "chore/deps-update"]


def test_ansi_codes_stripped():
    result = parse_problem_branches(ANSI_PROBLEM)
    assert result == ["feature/with-ansi"]


def test_empty_problem_block():
    result = parse_problem_branches(EMPTY_PROBLEM_BLOCK)
    assert result == []


def test_trailing_text_terminates_block():
    result = parse_problem_branches(TRAILING_TEXT_AFTER_BLOCK)
    assert result == ["chore/broken"]


def test_no_problem_header_at_all():
    result = parse_problem_branches("Finished tidying!\n")
    assert result == []


@pytest.mark.parametrize("branch", [
    "main",
    "feature/foo-bar",
    "fix/issue-123",
    "chore/bump-deps",
    "release/v2.0.0",
])
def test_various_branch_name_formats(branch: str):
    output = (
        "WARNING: Unable to auto-rebase the following branches:\n"
        f"    * {branch}\n"
        "Finished tidying!\n"
    )
    assert parse_problem_branches(output) == [branch]
