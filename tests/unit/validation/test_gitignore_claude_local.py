"""Ensure machine-local Claude Code settings never become committable.

Regression guard for issue #1494: ``.claude/settings.local.json`` accumulates
automation-loop permission artifacts and must be ignored by the *repo's own*
.gitignore, not merely by a contributor's personal global excludes file.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_claude_settings_local_ignored_by_repo_gitignore() -> None:
    """The repo's own .gitignore excludes .claude/settings.local.json.

    ``core.excludesFile=/dev/null`` disables the user's global ignore so this
    asserts the *repo* .gitignore alone excludes the file — independent of any
    contributor's personal global excludes.
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "check-ignore",
            "-v",
            ".claude/settings.local.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        ".claude/settings.local.json is NOT ignored by the repo .gitignore; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert ".gitignore" in result.stdout
