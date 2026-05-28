"""Smoke tests for omitted orchestration modules.

These tests serve as an integration backstop for the 10 modules omitted
from unit test coverage in pyproject.toml. The omits are necessary because
these modules require a live CLI session (claude, gh) or terminal (TTY).

This test ensures:
1. All 10 modules are importable (catches import-time regressions)
2. The 4 console scripts (with --help entry points) work without hanging
3. The 2 script-less main() modules are at least callable

No live sessions are started; --help exits before any main() logic runs.
"""

import subprocess
import sys
from pathlib import Path

import pytest


# The 10 deliberately omitted orchestration modules
ORCHESTRATION_MODULES = [
    "hephaestus.automation.implementer",
    "hephaestus.automation.implementer_phase_runner",
    "hephaestus.automation.implementer_summary",
    "hephaestus.automation.planner",
    "hephaestus.automation.address_review",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.loop_runner",
    "hephaestus.automation.curses_ui",
    "hephaestus.automation.github_api",
    "hephaestus.automation.pr_reviewer",
]

# Console scripts (with --help entry points, no live session)
CONSOLE_SCRIPTS = [
    "hephaestus-implement-issues",
    "hephaestus-plan-issues",
    "hephaestus-automation-loop",
    "hephaestus-review-prs",
]


@pytest.mark.integration
class TestOrchestrationSmoke:
    """Smoke tests for omitted orchestration modules."""

    @pytest.mark.parametrize("module_name", ORCHESTRATION_MODULES)
    def test_module_is_importable(self, module_name: str) -> None:
        """All orchestration modules are importable."""
        try:
            __import__(module_name)
        except ImportError as e:
            pytest.fail(f"Module {module_name} failed to import: {e}")

    @pytest.mark.parametrize("script_name", CONSOLE_SCRIPTS)
    def test_console_script_help_exits_cleanly(self, script_name: str) -> None:
        """Console scripts respond to --help without hanging."""
        try:
            result = subprocess.run(
                [script_name, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0, f"{script_name} --help failed with exit {result.returncode}"
            assert "usage:" in result.stdout.lower() or "help" in result.stdout.lower(), (
                f"{script_name} --help did not produce usage text"
            )
        except FileNotFoundError:
            pytest.skip(f"{script_name} not found on PATH")
        except subprocess.TimeoutExpired:
            pytest.fail(f"{script_name} --help timed out")

    def test_address_review_main_is_callable(self) -> None:
        """hephaestus.automation.address_review.main is callable."""
        from hephaestus.automation.address_review import main

        assert callable(main), "address_review.main is not callable"

    def test_ci_driver_main_is_callable(self) -> None:
        """hephaestus.automation.ci_driver.main is callable."""
        from hephaestus.automation.ci_driver import main

        assert callable(main), "ci_driver.main is not callable"
