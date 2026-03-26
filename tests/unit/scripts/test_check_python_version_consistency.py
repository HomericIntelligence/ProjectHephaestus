"""Tests for scripts/check_python_version_consistency.py."""

import pytest
from check_python_version_consistency import (
    check_pixi_version_drift,
    check_python_versions,
    extract_pixi_workspace_version,
    extract_project_version,
    extract_pyproject_versions,
    main,
)

# ---------------------------------------------------------------------------
# Fixtures: minimal config file contents
# ---------------------------------------------------------------------------

PYPROJECT_CONSISTENT = """\
[project]
name = "test"
version = "0.4.0"
requires-python = ">=3.10"

[tool.mypy]
python_version = "3.10"

[tool.ruff]
target-version = "py310"
"""

PYPROJECT_INCONSISTENT = """\
[project]
name = "test"
version = "0.4.0"
requires-python = ">=3.10"

[tool.mypy]
python_version = "3.11"

[tool.ruff]
target-version = "py310"
"""

PIXI_NO_VERSION = """\
[workspace]
name = "test"
description = "A test project"
channels = ["conda-forge"]
"""

PIXI_MATCHING_VERSION = """\
[workspace]
name = "test"
version = "0.4.0"
description = "A test project"
channels = ["conda-forge"]
"""

PIXI_MISMATCHED_VERSION = """\
[workspace]
name = "test"
version = "0.3.0"
description = "A test project"
channels = ["conda-forge"]
"""


# ---------------------------------------------------------------------------
# Unit tests: extraction helpers
# ---------------------------------------------------------------------------


class TestExtractPyprojectVersions:
    """Tests for extract_pyproject_versions."""

    def test_extracts_all_three_versions(self) -> None:
        versions = extract_pyproject_versions(PYPROJECT_CONSISTENT)
        assert versions == {
            "requires-python": "3.10",
            "mypy.python_version": "3.10",
            "ruff.target-version": "3.10",
        }

    def test_detects_inconsistency(self) -> None:
        versions = extract_pyproject_versions(PYPROJECT_INCONSISTENT)
        assert versions["mypy.python_version"] == "3.11"
        assert versions["requires-python"] == "3.10"

    def test_empty_content(self) -> None:
        versions = extract_pyproject_versions("")
        assert versions == {}


class TestExtractProjectVersion:
    """Tests for extract_project_version."""

    def test_extracts_version(self) -> None:
        assert extract_project_version(PYPROJECT_CONSISTENT) == "0.4.0"

    def test_missing_version(self) -> None:
        content = "[project]\nname = 'test'\n"
        assert extract_project_version(content) is None

    def test_no_project_section(self) -> None:
        assert extract_project_version("") is None


class TestExtractPixiWorkspaceVersion:
    """Tests for extract_pixi_workspace_version."""

    def test_returns_version_when_present(self) -> None:
        assert extract_pixi_workspace_version(PIXI_MATCHING_VERSION) == "0.4.0"

    def test_returns_none_when_absent(self) -> None:
        assert extract_pixi_workspace_version(PIXI_NO_VERSION) is None

    def test_empty_content(self) -> None:
        assert extract_pixi_workspace_version("") is None


# ---------------------------------------------------------------------------
# Integration tests: check functions using tmp_path
# ---------------------------------------------------------------------------


class TestCheckPythonVersions:
    """Tests for check_python_versions."""

    def test_consistent_versions(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        assert check_python_versions(tmp_path) == 0

    def test_inconsistent_versions(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_INCONSISTENT)
        assert check_python_versions(tmp_path) == 1

    def test_missing_pyproject(self, tmp_path: pytest.TempPathFactory) -> None:
        assert check_python_versions(tmp_path) == 1


class TestCheckPixiVersionDrift:
    """Tests for check_pixi_version_drift."""

    def test_no_pixi_version_field(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_NO_VERSION)
        assert check_pixi_version_drift(tmp_path) == 0

    def test_pixi_version_matches_pyproject(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_MATCHING_VERSION)
        assert check_pixi_version_drift(tmp_path) == 0

    def test_pixi_version_mismatches_pyproject(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_MISMATCHED_VERSION)
        assert check_pixi_version_drift(tmp_path) == 1

    def test_pyproject_missing(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pixi.toml").write_text(PIXI_MATCHING_VERSION)
        assert check_pixi_version_drift(tmp_path) == 1

    def test_pixi_missing(self, tmp_path: pytest.TempPathFactory) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        assert check_pixi_version_drift(tmp_path) == 1


class TestMain:
    """Tests for main() using monkeypatch to control repo root."""

    def test_all_ok(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_NO_VERSION)
        monkeypatch.setattr(
            "check_python_version_consistency.get_repo_root", lambda: tmp_path
        )
        assert main() == 0

    def test_pixi_drift_fails(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_CONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_MISMATCHED_VERSION)
        monkeypatch.setattr(
            "check_python_version_consistency.get_repo_root", lambda: tmp_path
        )
        assert main() == 1

    def test_python_version_inconsistency_fails(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_INCONSISTENT)
        (tmp_path / "pixi.toml").write_text(PIXI_NO_VERSION)
        monkeypatch.setattr(
            "check_python_version_consistency.get_repo_root", lambda: tmp_path
        )
        assert main() == 1
