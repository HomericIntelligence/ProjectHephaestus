"""Shared PR-state predicates used across the automation pipeline.

This module is a pure leaf — it imports only the standard library and
:mod:`typing` so that it can be used by :mod:`ci_driver`,
:mod:`post_merge_processor`, and :mod:`pr_discovery` without introducing
circular imports.  All three modules previously defined their own copies of
these predicates; this module is the single source of truth (#1289).
"""

from __future__ import annotations

from typing import Any

# Conclusion values that indicate a PR's check rollup is failing in a way
# drive-green can act on.  SUCCESS / SKIPPED / NEUTRAL / PENDING are
# explicitly excluded.  Shared with loop_runner._count_failing_prs so the
# SKIP gate and the actual work list never drift (#819).
FAILING_CHECK_CONCLUSIONS: frozenset[str] = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT"})


def _pr_is_failing(pr: dict[str, Any]) -> bool:
    """Return True iff this PR row is one drive-green should pick up.

    A PR is "failing" when it is open, non-draft, and either
    mergeStateStatus is BLOCKED or any statusCheckRollup entry's
    conclusion is in FAILING_CHECK_CONCLUSIONS. BLOCKED captures the
    branch-protection/required-review-not-met case; the conclusion check
    captures every CI red. PENDING is intentionally excluded — the driver
    waits for terminal state elsewhere.
    """
    if pr.get("isDraft"):
        return False
    if pr.get("mergeStateStatus") == "BLOCKED":
        return True
    rollup = pr.get("statusCheckRollup") or []
    return any(c.get("conclusion") in FAILING_CHECK_CONCLUSIONS for c in rollup)
