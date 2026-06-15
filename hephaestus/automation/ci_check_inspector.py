"""CI check inspection collaborator for the automation pipeline.

Provides :class:`CICheckInspector`, a focused SRP collaborator responsible
for querying and interpreting the CI check state of a pull request.  It is
extracted from :class:`~hephaestus.automation.ci_driver.CIDriver` as part of
the god-class decomposition (#1357).

The class depends on two injectable :class:`~typing.Callable` providers rather
than holding a reference to the full ``CIDriver``, keeping the coupling minimal
and making the class trivially unit-testable in isolation.

Note:
----
The ``FAILING_CHECK_CONCLUSIONS`` constant is defined once in
:mod:`hephaestus.automation.ci_driver` (the canonical location) and
imported by this module's consumers as needed.

"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .github_api import _gh_call, gh_pr_checks

logger = logging.getLogger(__name__)


class CICheckInspector:
    """Inspect CI check state for pull requests.

    Encapsulates the three CI-check query methods extracted from
    :class:`~hephaestus.automation.ci_driver.CIDriver`:

    * :meth:`_get_failing_ci_logs` — fetch combined failure logs for recent
      failed CI runs on a PR.
    * :meth:`_failing_required_check_names` — return names of required checks
      that are currently failing.
    * :meth:`_pending_required_check_names` — return names of required checks
      that are still in flight.

    Args:
        get_pr_branch: Callable that accepts a PR number and returns the PR's
            head branch name.  Wraps ``CIDriver._get_pr_branch``.
        options_provider: Zero-argument callable that returns the current
            options object (must expose at minimum a ``dry_run`` attribute).
            Wraps a lambda over ``CIDriver.options``.

    """

    def __init__(
        self,
        *,
        get_pr_branch: Callable[[int], str],
        options_provider: Callable[[], Any],
    ) -> None:
        """Initialise with narrow callable providers."""
        self._get_pr_branch = get_pr_branch
        self._options_provider = options_provider

    # ------------------------------------------------------------------
    # Public inspection methods
    # ------------------------------------------------------------------

    def _get_failing_ci_logs(self, pr_number: int) -> str:
        """Fetch combined failure logs for recent failed CI runs on a PR.

        Scopes the ``gh run list`` query to the PR's head branch so we only
        see runs that belong to this PR rather than the most-recent repo-wide
        runs (the previous repo-wide query could return runs for other PRs
        and even other branches, making the logs useless for fixing *this* PR).

        Args:
            pr_number: GitHub PR number.

        Returns:
            Combined log string, truncated to 10 000 characters.

        """
        try:
            branch = self._get_pr_branch(pr_number)
            result2 = _gh_call(
                [
                    "run",
                    "list",
                    "--branch",
                    branch,
                    "--status",
                    "failure",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,conclusion,name,headSha",
                ],
                check=False,
            )
            runs: list[dict[str, Any]] = json.loads(result2.stdout or "[]")
            failed_runs = [r for r in runs if r.get("conclusion") == "failure"][:3]

            logs: list[str] = []
            for run_info in failed_runs:
                run_id = run_info.get("databaseId")
                run_name = run_info.get("name", str(run_id))
                if not run_id:
                    continue
                try:
                    log_result = _gh_call(
                        ["run", "view", str(run_id), "--log-failed"],
                        check=False,
                    )
                    logs.append(f"=== {run_name} ===\n{log_result.stdout[:3000]}")
                except Exception as log_err:
                    logger.debug("Could not fetch log for run %s: %s", run_id, log_err)

            return "\n\n".join(logs)[:10000]

        except Exception as e:
            logger.warning("Could not fetch CI logs for PR #%s: %s", pr_number, e)
            return ""

    def _failing_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are currently failing.

        Used by the no-commit retry path (#846) to name the actual offenders
        verbatim in the force-engagement prompt. Returns an empty list if
        the lookup fails — the caller treats that as "cannot prove still
        red" and skips the retry rather than launching Claude blind.
        """
        try:
            checks = gh_pr_checks(pr_number, dry_run=self._options_provider().dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to re-check CI for no-commit retry decision (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [
            c.get("name", "")
            for c in required
            if c.get("status") == "completed" and c.get("conclusion") == "failure"
        ]

    def _pending_required_check_names(self, pr_number: int) -> list[str]:
        """Return names of required checks that are still in flight (not completed).

        Used by the BLOCKED early-exit guard in ``_wait_for_pr_terminal`` to
        distinguish branch-protection blocks (all checks green but conversations
        unresolved) from pending-CI blocks (checks still running).  Returns an
        empty list on lookup failure — the caller then conservatively assumes no
        checks are pending and exits the poll.
        """
        try:
            checks = gh_pr_checks(pr_number, dry_run=self._options_provider().dry_run)
        except Exception as exc:
            logger.info(
                "PR #%s: failed to fetch CI checks for BLOCKED pending guard (%s)",
                pr_number,
                exc,
            )
            return []
        if not checks:
            return []
        required = [c for c in checks if c.get("required")] or checks
        return [c.get("name", "") for c in required if c.get("status") != "completed"]
