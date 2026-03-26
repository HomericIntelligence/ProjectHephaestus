#!/usr/bin/env python3
"""Check Python version consistency across project config files.

Verifies that:
1. requires-python in pyproject.toml, python_version in mypy config,
   and target-version in ruff config are all consistent.
2. If pixi.toml has a [workspace] version, it matches pyproject.toml's
   [project] version (catches accidental re-introduction of the duplicate).

Usage:
    python scripts/check_python_version_consistency.py
"""

import re
import sys
from pathlib import Path


def get_repo_root() -> Path:
    """Find repository root by looking for pyproject.toml."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def extract_pyproject_versions(content: str) -> dict[str, str]:
    """Extract version strings from pyproject.toml content."""
    versions = {}

    # requires-python = ">=3.10"
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', content)
    if match:
        # Normalize to major.minor (e.g., ">=3.10" -> "3.10")
        raw = match.group(1)
        ver_match = re.search(r"(\d+\.\d+)", raw)
        if ver_match:
            versions["requires-python"] = ver_match.group(1)

    # [tool.mypy] python_version = "3.10"
    match = re.search(r'\[tool\.mypy\].*?python_version\s*=\s*"([^"]+)"', content, re.DOTALL)
    if match:
        versions["mypy.python_version"] = match.group(1)

    # [tool.ruff] target-version = "py310"
    match = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', content)
    if match:
        versions["ruff.target-version"] = f"{match.group(1)}.{match.group(2)}"

    return versions


def extract_project_version(content: str) -> str | None:
    """Extract the [project] version from pyproject.toml content.

    Args:
        content: The pyproject.toml file content.

    Returns:
        The version string, or None if not found.

    """
    match = re.search(r'\[project\]\s*\n(?:.*\n)*?version\s*=\s*"([^"]+)"', content)
    if match:
        return match.group(1)
    return None


def extract_pixi_workspace_version(content: str) -> str | None:
    """Extract the [workspace] version from pixi.toml content, if present.

    Args:
        content: The pixi.toml file content.

    Returns:
        The version string, or None if the field is absent.

    """
    match = re.search(r'\[workspace\]\s*\n(?:.*\n)*?version\s*=\s*"([^"]+)"', content)
    if match:
        return match.group(1)
    return None


def check_python_versions(repo_root: Path) -> int:
    """Check Python version consistency across pyproject.toml settings.

    Args:
        repo_root: Path to the repository root.

    Returns:
        0 if consistent, 1 if inconsistent.

    """
    pyproject_path = repo_root / "pyproject.toml"

    if not pyproject_path.exists():
        print(f"ERROR: pyproject.toml not found at {pyproject_path}")
        return 1

    content = pyproject_path.read_text()
    versions = extract_pyproject_versions(content)

    if len(versions) < 2:
        print(f"WARNING: Could only find {len(versions)} version spec(s) in pyproject.toml")
        for key, val in versions.items():
            print(f"  {key}: {val}")
        return 0

    unique_versions = set(versions.values())
    if len(unique_versions) == 1:
        version = next(iter(unique_versions))
        print(f"OK: Python version is consistent at {version}")
        for key, val in versions.items():
            print(f"  {key}: {val}")
        return 0

    print("ERROR: Python version inconsistency detected!")
    for key, val in versions.items():
        print(f"  {key}: {val}")
    print("\nAll version specifications must agree on the same Python version.")
    return 1


def check_pixi_version_drift(repo_root: Path) -> int:
    """Check that pixi.toml workspace version, if present, matches pyproject.toml.

    The version field was intentionally removed from pixi.toml to avoid
    duplication. This check catches accidental re-introduction with a
    mismatched value.

    Args:
        repo_root: Path to the repository root.

    Returns:
        0 if OK (no version in pixi.toml, or versions match), 1 if drift detected.

    """
    pyproject_path = repo_root / "pyproject.toml"
    pixi_path = repo_root / "pixi.toml"

    if not pyproject_path.exists():
        print("ERROR: pyproject.toml not found")
        return 1

    if not pixi_path.exists():
        print("ERROR: pixi.toml not found")
        return 1

    pyproject_content = pyproject_path.read_text()
    pixi_content = pixi_path.read_text()

    pixi_version = extract_pixi_workspace_version(pixi_content)
    if pixi_version is None:
        print("OK: pixi.toml has no workspace version (single source of truth in pyproject.toml)")
        return 0

    project_version = extract_project_version(pyproject_content)
    if project_version is None:
        print("ERROR: Could not extract [project] version from pyproject.toml")
        return 1

    if pixi_version == project_version:
        print(
            f"OK: pixi.toml workspace version ({pixi_version}) "
            f"matches pyproject.toml ({project_version})"
        )
        return 0

    print("ERROR: Version drift detected between pixi.toml and pyproject.toml!")
    print(f"  pixi.toml [workspace] version: {pixi_version}")
    print(f"  pyproject.toml [project] version: {project_version}")
    print("\nRemove the version from pixi.toml [workspace] — pyproject.toml is the single source.")
    return 1


def main() -> int:
    """Check Python version consistency. Returns 0 if OK, 1 if inconsistent."""
    repo_root = get_repo_root()

    python_result = check_python_versions(repo_root)
    pixi_result = check_pixi_version_drift(repo_root)

    if python_result != 0 or pixi_result != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
