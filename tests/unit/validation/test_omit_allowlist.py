"""Guard the intentional omit list for orchestration modules.

This test documents and enforces the deliberate gap in coverage measurement:
10 orchestration modules are omitted from coverage.run.omit in pyproject.toml
because they are live-CLI/TTY-dependent and cannot be measured in CI.

The test verifies that the omit list hasn't grown silently, ensuring that
any new omissions are deliberate and documented.
"""

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class TestOmitAllowlist:
    """Verify the intentional omit list is frozen and documented."""

    def test_omit_list_matches_documented_set(self) -> None:
        """The [tool.coverage.run].omit list matches the documented 10 modules."""
        # Find the repo root: tests/unit/validation/test_omit_allowlist.py -> repo_root
        test_file = Path(__file__).resolve()
        # Walk up: test_omit_allowlist.py -> validation -> unit -> tests -> repo_root
        repo_root = test_file.parent.parent.parent.parent

        pyproject = repo_root / "pyproject.toml"
        assert pyproject.exists(), f"pyproject.toml not found at {pyproject}"

        with open(pyproject, "rb") as f:
            config = tomllib.load(f)

        omit_list = config.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", [])

        # The 10 deliberately omitted orchestration modules:
        expected_modules = {
            "hephaestus/automation/implementer.py",
            "hephaestus/automation/implementer_phase_runner.py",
            "hephaestus/automation/implementer_summary.py",
            "hephaestus/automation/planner.py",
            "hephaestus/automation/address_review.py",
            "hephaestus/automation/ci_driver.py",
            "hephaestus/automation/loop_runner.py",
            "hephaestus/automation/curses_ui.py",
            "hephaestus/automation/github_api.py",
            "hephaestus/automation/pr_reviewer.py",
        }

        # The standard patterns (boilerplate)
        expected_globs = {
            "*/tests/*",
            "*/__init__.py",
        }

        omit_set = set(omit_list)

        # Check that all expected modules are in the omit list
        missing_modules = expected_modules - omit_set
        if missing_modules:
            pytest.fail(f"Expected omitted modules not found: {missing_modules}")

        # Check that all expected globs are in the omit list
        missing_globs = expected_globs - omit_set
        if missing_globs:
            pytest.fail(f"Expected omit globs not found: {missing_globs}")

        # Check that no unexpected modules have been added
        # (globs are excluded from this check as they might legitimately grow)
        only_modules = {item for item in omit_set if not ("*" in item)}
        extra_modules = only_modules - expected_modules
        if extra_modules:
            pytest.fail(
                f"Unexpected modules added to omit list: {extra_modules}. "
                f"This test guards against silent growth. "
                f"If intentional, update the expected_modules set in this test."
            )
