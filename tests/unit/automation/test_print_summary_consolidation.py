"""Regression tests for issue #1461 worker-summary consolidation.

The four reviewer/driver classes must delegate worker-summary printing to
``print_worker_summary`` rather than re-implementing it (DRY).

The four classes ``CIDriver``, ``PRReviewer``, ``AddressReviewer`` and
``PlanReviewer`` once each carried a near-identical ``_print_summary`` body
(total/successful/failed computation, the ``"=" * 60`` banner, and the
failed-issue loop). PR #1612 consolidated that into the single canonical
``print_worker_summary`` helper in ``_review_utils.py``; these tests guard
against the duplication drifting back in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import hephaestus.automation as automation_pkg
from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import WorkerResult
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.pr_reviewer import PRReviewer

_AUTOMATION_DIR = Path(automation_pkg.__file__).parent

# The four classes the issue named, the module that holds each one, and the
# exact ``print_worker_summary`` call the delegate must make.
_DELEGATING_MODULES = (
    "ci_driver.py",
    "pr_reviewer.py",
    "address_review.py",
    "plan_reviewer.py",
)


@pytest.mark.parametrize("module_name", _DELEGATING_MODULES)
def test_named_summary_modules_do_not_inline_standard_separator(module_name: str) -> None:
    """Issue-named modules must not reintroduce the duplicated summary banner."""
    source = (_AUTOMATION_DIR / module_name).read_text(encoding="utf-8")

    assert '"=" * 60' not in source, (
        f"{module_name} re-introduced an inline summary separator; it must "
        f"delegate to print_worker_summary (issue #1461)."
    )


@pytest.mark.parametrize(
    ("reviewer_cls", "patch_target", "title", "expected_kwargs"),
    (
        (
            CIDriver,
            "hephaestus.automation.ci_driver.print_worker_summary",
            "CI Driver Summary",
            {},
        ),
        (
            PRReviewer,
            "hephaestus.automation.pr_reviewer.print_worker_summary",
            "PR Review Summary",
            {"count_noun": "PRs", "failed_header": "\nFailed issues:"},
        ),
        (
            AddressReviewer,
            "hephaestus.automation.address_review.print_worker_summary",
            "Address Review Summary",
            {"failed_header": "\nFailed issues:"},
        ),
        (
            PlanReviewer,
            "hephaestus.automation.plan_reviewer.print_worker_summary",
            "Plan Review Summary",
            {},
        ),
    ),
)
def test_named_summary_methods_delegate_to_print_worker_summary(
    reviewer_cls: type[Any],
    patch_target: str,
    title: str,
    expected_kwargs: dict[str, str],
) -> None:
    """The four issue-named wrappers delegate to the shared summary helper."""
    results = {1: WorkerResult(issue_number=1, success=True)}

    with patch(patch_target) as summary:
        reviewer_cls._print_summary(object.__new__(reviewer_cls), results)

    summary.assert_called_once_with(title, results, **expected_kwargs)
