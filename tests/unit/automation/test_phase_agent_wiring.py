"""Regression guard: each phase module must use its dedicated AGENT_* constant.

Background: prior to this guard, ``address_review.py`` and ``ci_driver.py``
imported ``AGENT_IMPLEMENTER`` and passed it as ``agent=`` to
``invoke_claude_with_session``. That caused all three phases to share a single
deterministic session UUID, defeating the per-agent isolation that the
session-naming design depends on.

These tests assert two properties for every phase module:

1. The module's source contains exactly one ``agent=AGENT_<NAME>,`` call-site
   kwarg, matching the expected constant.
2. The module imports that constant from ``hephaestus.automation.session_naming``.

We assert on source text (not on runtime mocks) because constructing valid
Options objects for every phase is brittle and orthogonal to what we want to
guard: that the *wiring* is correct.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import hephaestus.automation as automation_pkg

AUTOMATION_DIR = Path(automation_pkg.__file__).parent


# (module_filename, expected AGENT_* constant name)
PHASE_WIRING: list[tuple[str, str]] = [
    ("plan_reviewer.py", "AGENT_PLAN_REVIEWER"),
    ("pr_reviewer.py", "AGENT_PR_REVIEWER"),
    ("implementer.py", "AGENT_IMPLEMENTER"),
    ("address_review.py", "AGENT_ADDRESS_REVIEW"),
    ("ci_driver.py", "AGENT_CI_DRIVER"),
]


@pytest.mark.parametrize("module_file, expected_agent", PHASE_WIRING)
def test_phase_module_imports_expected_agent(module_file: str, expected_agent: str) -> None:
    """Each phase module must import its dedicated AGENT_* constant."""
    src = (AUTOMATION_DIR / module_file).read_text()
    import_pattern = re.compile(rf"from\s+\.session_naming\s+import\s+[^\n]*\b{expected_agent}\b")
    assert import_pattern.search(src), (
        f"{module_file} must import {expected_agent} from .session_naming"
    )


@pytest.mark.parametrize("module_file, expected_agent", PHASE_WIRING)
def test_phase_module_passes_expected_agent_kwarg(module_file: str, expected_agent: str) -> None:
    """Each phase module must pass its dedicated AGENT_* constant as agent=."""
    src = (AUTOMATION_DIR / module_file).read_text()
    kwarg_pattern = re.compile(rf"\bagent\s*=\s*{expected_agent}\b")
    assert kwarg_pattern.search(src), (
        f"{module_file} must pass agent={expected_agent} to invoke_claude_with_session"
    )


@pytest.mark.parametrize("module_file, expected_agent", PHASE_WIRING)
def test_phase_module_does_not_use_foreign_agent(module_file: str, expected_agent: str) -> None:
    """No phase module should pass any AGENT_* constant other than its own.

    Exception: ``planner.py`` legitimately uses AGENT_PLANNER, AGENT_ADVISE, and
    AGENT_LEARNINGS — it is covered by a separate test below and excluded here.
    """
    src = (AUTOMATION_DIR / module_file).read_text()
    foreign_agent_pattern = re.compile(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b")
    found = set(foreign_agent_pattern.findall(src))
    assert found <= {expected_agent}, (
        f"{module_file} uses unexpected AGENT_* constants: "
        f"{found - {expected_agent}}; expected only {expected_agent}"
    )


def test_planner_module_uses_its_expected_agents() -> None:
    """planner.py drives multiple call sites with distinct agents.

    AGENT_PLANNER     — main planning call
    AGENT_ADVISE      — pre-plan advice call
    AGENT_LEARNINGS   — post-plan learnings capture
    AGENT_PLAN_REVIEWER — in-process plan-review call (separate from the
                          standalone plan_reviewer.py phase module)
    """
    src = (AUTOMATION_DIR / "planner.py").read_text()
    foreign_agent_pattern = re.compile(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b")
    found = set(foreign_agent_pattern.findall(src))
    expected = {
        "AGENT_PLANNER",
        "AGENT_ADVISE",
        "AGENT_LEARNINGS",
        "AGENT_PLAN_REVIEWER",
    }
    assert found == expected, f"planner.py agent wiring drifted: found={found}, expected={expected}"
