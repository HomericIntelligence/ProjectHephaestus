"""Guard rails for scripts that manipulate Git metadata."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_PATHS = (
    ".github",
    "docs",
    "hephaestus",
    "scripts",
    "skills",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "justfile",
)

FORBIDDEN_PATTERNS = {
    "sets-core-bare": re.compile(
        r"\bgit\s+config\b[^\n]*\bcore[.]bare\b[^\n]*(?:true|1|yes|on)\b",
        re.IGNORECASE,
    ),
    "uses-explicit-git-dir": re.compile(
        r'(?:\bgit\s+--git-dir(?:=|\s)|\["git",\s*"--git-dir")',
        re.IGNORECASE,
    ),
    "sets-git-dir-env": re.compile(
        r"(?:^|[;&|]\s*|\benv\s+)(?:export\s+)?GIT_DIR=",
        re.MULTILINE,
    ),
    "sets-git-work-tree-env": re.compile(
        r"(?:^|[;&|]\s*|\benv\s+)(?:export\s+)?GIT_WORK_TREE=",
        re.MULTILINE,
    ),
}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *SCAN_PATHS],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        REPO_ROOT / path.decode()
        for path in result.stdout.split(b"\0")
        if path
    ]


def test_project_scripts_do_not_flip_repository_to_bare_mode() -> None:
    """Checked-in automation must not rewrite a normal checkout as bare."""
    violations: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for rule, pattern in FORBIDDEN_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                rel_path = path.relative_to(REPO_ROOT)
                violations.append(f"{rel_path}:{line}: {rule}: {match.group(0)!r}")

    assert violations == []
