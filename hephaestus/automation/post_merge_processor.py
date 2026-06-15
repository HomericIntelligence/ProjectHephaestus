"""Post-merge processing collaborator: auto-merge arming and /learn capture.

Extracted from :class:`~hephaestus.automation.ci_driver.CIDriver` as a narrow
SRP collaborator (#1289). Owns all logic that fires after a PR is green:
enabling auto-merge, writing arming records, and running post-merge ``/learn``
and ``/compact`` sessions.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, run_codex_session
from hephaestus.automation.claude_invoke import invoke_claude_with_session
from hephaestus.automation.claude_models import implementer_model
from hephaestus.automation.claude_timeouts import learn_claude_timeout
from hephaestus.automation.git_utils import get_repo_slug, issue_ref, pr_ref
from hephaestus.automation.github_api import _gh_call
from hephaestus.automation.learn import (
    build_learn_prompt,
    compact_session,
    mnemosyne_update_evidence,
)
from hephaestus.automation.models import CIDriverOptions

from .session_naming import AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


class PostMergeProcessor:
    """Handles auto-merge arming, drive-green arming records, and /learn.

    Args:
        options: CI driver configuration options.
        repo_root: Path to the repository root.
        get_worktree_path: Provider that returns the worktree path for
            ``(issue_number, pr_number)``.
        save_arming_state: Callable ``(issue_number, record) -> None``.
        load_arming_state: Callable ``(issue_number) -> dict | None``.
        clear_arming_state: Callable ``(issue_number) -> None``.
        learn_record_terminal: Callable ``(record) -> bool``.
        shared_pr_issues_getter: Callable ``(pr_number) -> list[int]`` that returns
            the list of issue numbers sharing the same PR.

    """

    def __init__(
        self,
        *,
        options: CIDriverOptions,
        repo_root: Path,
        get_worktree_path: Any,  # Callable[[int, int], Path]
        save_arming_state: Any,  # Callable[[int, dict], None]
        load_arming_state: Any,  # Callable[[int], dict | None]
        clear_arming_state: Any,  # Callable[[int], None]
        learn_record_terminal: Any,  # Callable[[dict], bool]
        shared_pr_issues_getter: Any,  # Callable[[int], list[int]]
    ) -> None:
        """Initialize the processor with all required provider callables."""
        self.options = options
        self.repo_root = repo_root
        self._get_worktree_path = get_worktree_path
        self._save_arming_state = save_arming_state
        self._load_arming_state = load_arming_state
        self._clear_arming_state = clear_arming_state
        self._learn_record_terminal = learn_record_terminal
        self._shared_pr_issues_getter = shared_pr_issues_getter

        # Wired by CIDriver after construction.
        self._state_dir: Path | None = None
        self._gh_pr_state_fn: Any = None  # Callable[[int], dict | None]

        # Mutable evidence slot — populated by _run_drive_green_learnings.
        self._last_drive_green_learn_evidence: dict[str, Any] = mnemosyne_update_evidence("")

    # ------------------------------------------------------------------
    # Auto-merge arming
    # ------------------------------------------------------------------

    def _enable_auto_merge(self, pr_number: int, is_bot_pr: bool = False) -> bool:
        """Enable auto-merge for the given PR using squash strategy.

        First attempts ``gh pr merge --auto --squash``. This repo is
        squash-only — rebase merges are disabled by branch protection, so the
        primary path MUST use ``--squash``. On failure, if
        ``options.force_merge_on_stall`` is set, falls back to a direct
        squash merge (``gh pr merge --squash --delete-branch``). If both
        strategies fail, logs an ERROR and returns False.

        For bot-authored PRs (Dependabot etc., #848) a green PR left ``NOT
        armed`` accumulates forever and keeps the repo perpetually "not done".
        Those PRs are low-risk dependency bumps, so before giving up we make a
        strategy-agnostic ``gh pr merge --auto`` attempt (no ``--squash``),
        letting whatever merge method the repo actually allows take effect.

        Args:
            pr_number: GitHub PR number.
            is_bot_pr: True when this is a bot-authored PR, enabling the
                strategy-agnostic arming retry described above.

        Returns:
            True if auto-merge was enabled (or fallback merge succeeded),
            False if both strategies failed.

        """
        try:
            _gh_call(["pr", "merge", str(pr_number), "--auto", "--squash"])
            logger.info("Enabled auto-merge for PR #%s", pr_number)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Could not enable auto-merge (--squash) for PR #%s: %s; "
                "will attempt squash-merge fallback if force_merge_on_stall is set",
                pr_number,
                e,
            )

        if is_bot_pr:
            try:
                _gh_call(["pr", "merge", str(pr_number), "--auto"])
                logger.info("Enabled auto-merge (strategy-agnostic) for bot PR #%s", pr_number)
                return True
            except subprocess.CalledProcessError as bot_err:
                logger.warning(
                    "Could not enable strategy-agnostic auto-merge for bot PR #%s: %s",
                    pr_number,
                    bot_err,
                )

        if not self.options.force_merge_on_stall:
            logger.error(
                "PR #%s: auto-merge failed and force_merge_on_stall is not set; "
                "skipping squash-merge fallback",
                pr_number,
            )
            return False

        try:
            _gh_call(["pr", "merge", str(pr_number), "--squash", "--delete-branch"])
            logger.info("Squash-merged PR #%s via fallback", pr_number)
            return True
        except subprocess.CalledProcessError as fallback_err:
            logger.error(
                "PR #%s: both auto-merge and squash-merge fallback failed: %s",
                pr_number,
                fallback_err,
            )
            return False

    # ------------------------------------------------------------------
    # Drive-green arming records (#840)
    # ------------------------------------------------------------------

    def _arm_drive_green(self, pr_number: int, pr_head_branch: str, pr_head_sha: str) -> None:
        """Record arming for every issue that resolved to ``pr_number``.

        Called on the auto-merge-armed success path in the same run. For a
        shared-PR group (#834), this writes one arming record per sibling
        issue so each one gets its own ``/learn`` capture once the PR merges
        in a subsequent run. The canonical issue and all deferred siblings
        share the same ``pr_number`` and ``pr_head_branch`` — they differ
        only in the issue id encoded in the filename.
        """
        # shared_pr_issues is provided via the getter wired at construction.
        siblings = self._shared_pr_issues_getter(pr_number)
        if not siblings:
            return
        armed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for issue_num in siblings:
            existing = self._load_arming_state(issue_num) or {}
            if self._learn_record_terminal(existing):
                continue
            record = {
                "pr_number": pr_number,
                "pr_head_branch": pr_head_branch,
                "head_sha_at_arming": pr_head_sha,
                "armed_at": armed_at,
                "learn_attempted_at": None,
                "learn_captured_at": None,
                "learn_status": None,
                "learn_succeeded_at": None,
            }
            self._save_arming_state(issue_num, record)
            logger.info(
                "Issue #%s: armed for /learn on merge of PR #%s (head=%s @ %s)",
                issue_num,
                pr_number,
                pr_head_branch,
                pr_head_sha[:8] if pr_head_sha else "?",
            )

    # ------------------------------------------------------------------
    # Learn-result stamping
    # ------------------------------------------------------------------

    def _mark_drive_green_learn_result(
        self,
        issue_number: int,
        record: dict[str, Any],
        *,
        succeeded: bool,
    ) -> None:
        """Persist an explicit attempted/succeeded/failed learn result.

        ``learn_captured_at`` is retained as the success timestamp for older
        readers, but failure now records ``learn_status=failed`` without
        claiming Mnemosyne was updated.
        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
            record.update(
                getattr(self, "_last_drive_green_learn_evidence", mnemosyne_update_evidence(""))
            )
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
            record.update(
                {
                    "mnemosyne_update_status": "failed",
                    "mnemosyne_update_urls": [],
                    "mnemosyne_update_pr_numbers": [],
                }
            )
        self._save_arming_state(issue_number, record)

    # ------------------------------------------------------------------
    # /learn + /compact sessions
    # ------------------------------------------------------------------

    def _run_drive_green_learnings(self, issue_number: int, pr_number: int) -> bool:
        """Capture drive-green learnings under AGENT_CI_DRIVER (Session 3).

        Runs after a PR reaches green and auto-merge is enabled, mirroring the
        implementer's post-PR ``/learn`` step but scoped to *this* drive: what
        made CI fail and how it was fixed. Resumes Session 3 (the
        ``AGENT_CI_DRIVER`` session the fix session created) so the learnings
        compound on the same transcript that did the work.

        This is best-effort. Any failure is logged at WARNING and swallowed so
        a flaky learnings step never flips a successful drive to failure.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            True if the learnings session completed, False otherwise.

        """
        prompt = build_learn_prompt(
            f"You just drove PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}) "
            "to green CI. Capture concise learnings about what made CI fail and how "
            "you fixed it, scoped to this issue/PR."
        )
        self._last_drive_green_learn_evidence = mnemosyne_update_evidence("")
        try:
            repo_slug = get_repo_slug(self.repo_root)
            try:
                cwd = self._get_worktree_path(issue_number, pr_number)
            except Exception as wt_err:
                logger.info(
                    "Issue #%s: no worktree available for /learn (%s); "
                    "falling back to repo root for a fresh session",
                    issue_number,
                    wt_err,
                )
                cwd = self.repo_root
            if is_codex(self.options.agent):
                codex_result = run_codex_session(
                    prompt,
                    cwd=cwd,
                    timeout=learn_claude_timeout(),
                    sandbox="workspace-write",
                )
                self._last_drive_green_learn_evidence = mnemosyne_update_evidence(
                    codex_result.stdout or ""
                )
                logger.info("Issue #%s: drive-green learnings captured with Codex", issue_number)
                return True
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_CI_DRIVER,
                prompt=prompt,
                model=implementer_model(),
                cwd=cwd,
                timeout=learn_claude_timeout(),
                output_format="text",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                extra_args=["--dangerously-skip-permissions"],
                input_via_stdin=True,
            )
            self._last_drive_green_learn_evidence = mnemosyne_update_evidence(stdout or "")
            logger.info("Issue #%s: drive-green learnings captured", issue_number)
            return True
        except Exception as e:  # broad: external claude process; non-blocking
            logger.warning(
                "Issue #%s: drive-green learnings failed (non-fatal): %s",
                issue_number,
                e,
            )
            return False

    def _run_drive_green_compact(self, issue_number: int, pr_number: int) -> bool:
        """Compact the AGENT_CI_DRIVER session transcript after /learn (#842).

        Mirrors the cwd-derivation of ``_run_drive_green_learnings``: try the
        worktree first so the deterministic JSONL probe in ``session_jsonl_path``
        finds the transcript, fall back to ``repo_root`` when the branch is
        already gone post-merge. Non-fatal.
        """
        if is_codex(self.options.agent):
            logger.info(
                "Issue #%s: skipping /compact (codex has no persisted "
                "drive-green session to resume)",
                issue_number,
            )
            return False
        try:
            cwd = self._get_worktree_path(issue_number, pr_number)
        except Exception as wt_err:
            logger.info(
                "Issue #%s: no worktree available for /compact (%s); using repo root",
                issue_number,
                wt_err,
            )
            cwd = self.repo_root
        repo_slug = get_repo_slug(self.repo_root)
        return compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_CI_DRIVER,
            cwd=cwd,
            model=implementer_model(),
        )

    # ------------------------------------------------------------------
    # Arming-record sweep
    # ------------------------------------------------------------------

    def _sweep_orphaned_arming_records(self) -> None:
        """Drop CLOSED records and capture missed ``/learn`` for MERGED orphans (#848).

        The per-issue ``_check_arming_on_drive_start`` only fires when the
        issue is in the current run's input list. If a PR was replaced via
        the fresh-branch-reopen workflow (the replacement PR merged out of
        band of the bot, the original was closed), the arming record points
        at the closed PR and the issue itself is closed — the next run's
        ``gh issue list --state open`` won't include it, so the record
        leaks forever and ``/learn`` is silently lost.

        Sweep every ``drive-green-armed-*.json`` at startup: drop records
        whose PR is CLOSED-not-merged, fire ``/learn`` once for records
        whose PR is MERGED (then mark explicit succeeded/failed learn status),
        leave OPEN records alone for the normal per-issue path to handle.
        """
        state_dir = self._state_dir
        if state_dir is None:
            logger.info("Arming sweep skipped: state_dir not wired")
            return
        try:
            records = sorted(state_dir.glob("drive-green-armed-*.json"))
        except OSError as exc:
            logger.info("Arming sweep skipped: state_dir scan failed (%s)", exc)
            return
        if not records:
            return
        logger.info("Sweeping %s arming record(s) for orphan resolution", len(records))
        for path in records:
            stem = path.stem  # drive-green-armed-<issue>
            try:
                issue_number = int(stem.rsplit("-", 1)[-1])
            except ValueError:
                logger.info("Arming sweep: ignoring malformed filename %s", path.name)
                continue
            record = self._load_arming_state(issue_number)
            if record is None:
                continue
            if self._learn_record_terminal(record):
                continue
            pr_number = record.get("pr_number")
            if not isinstance(pr_number, int):
                logger.info(
                    "Arming sweep: dropping record %s with non-integer pr_number",
                    path.name,
                )
                self._clear_arming_state(issue_number)
                continue
            gh_state = self._gh_pr_state_fn(pr_number)
            if gh_state is None:
                continue
            state = (gh_state.get("state") or "").upper()
            if state == "MERGED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s MERGED; firing /learn",
                    issue_number,
                    pr_number,
                )
                learn_succeeded = self._run_drive_green_learnings(issue_number, pr_number)
                self._run_drive_green_compact(issue_number, pr_number)
                self._mark_drive_green_learn_result(
                    issue_number,
                    record,
                    succeeded=learn_succeeded,
                )
            elif state == "CLOSED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s CLOSED-not-merged; dropping record",
                    issue_number,
                    pr_number,
                )
                self._clear_arming_state(issue_number)
