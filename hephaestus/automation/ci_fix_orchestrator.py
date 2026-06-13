"""CI fix session orchestrator extracted from CIDriver (refs #1179).

Owns the methods that run coding-agent sessions to fix CI failures:
- force-engagement prompts and retry logic (#846)
- the main CI fix session (claude/codex invocation + push)
- mechanical rebase (no-agent fast path, #871)
- CI fix attempt loop
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_session,
)

from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .claude_timeouts import ci_driver_claude_timeout
from .git_utils import (
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    rebase_worktree_onto,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import _gh_call
from .session_naming import AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


class CIFixOrchestrator:
    """Orchestrates CI fix sessions using narrow-callable injection.

    Receives all back-called CIDriver methods as callables instead of the
    full CIDriver to satisfy DIP and avoid bidirectional coupling
    (refs #1179 MAJOR finding 2).
    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Any],
        state_dir_provider: Callable[[], Any],
        status_tracker_provider: Callable[[], Any],
        format_review_threads_block: Callable[[int], str],
        head_advanced: Callable[[Path, str, int], bool],
        ci_fix_head_is_pushable: Callable[[Path, int], bool],
        tracked_worktree_changes: Callable[[Path, int], list[str]],
        failing_required_check_names: Callable[[int], list[str]],
        get_pr_branch: Callable[[int], str],
    ) -> None:
        """Initialise the orchestrator with narrow provider callables.

        Args:
            options_provider: Returns the current CIDriverOptions.
            repo_root_provider: Returns the repo root Path.
            state_dir_provider: Returns the state directory Path.
            status_tracker_provider: Returns the current StatusTracker.
            format_review_threads_block: Builds the review-threads prompt block.
            head_advanced: Returns True iff HEAD moved past pre_agent_sha.
            ci_fix_head_is_pushable: Returns True iff worktree is safe to push.
            tracked_worktree_changes: Returns tracked dirty status lines.
            failing_required_check_names: Returns names of failing required checks.
            get_pr_branch: Returns the head branch name for a PR number.

        """
        self._options = options_provider
        self._repo_root = repo_root_provider
        self._state_dir = state_dir_provider
        self._status = status_tracker_provider
        self._format_review_threads_block = format_review_threads_block
        self._head_advanced = head_advanced
        self._ci_fix_head_is_pushable = ci_fix_head_is_pushable
        self._tracked_worktree_changes = tracked_worktree_changes
        self._failing_required_check_names = failing_required_check_names
        self._get_pr_branch = get_pr_branch

    def force_engagement_prompt(
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        pr_head_branch: str,
        failing_check_names: list[str],
        review_threads_block: str,
        dirty_tracked_changes: list[str] | None = None,
    ) -> str:
        """Build the retry prompt when the agent returned without committing (#846).

        The retry must engage the agent enough to either (a) produce a real
        fix or (b) explicitly say why CI cannot pass / merge. The prompt names
        the failing checks and/or dirty tracked files verbatim, re-emphasises
        the existing PR/branch invariant, and re-emphasises signed commits — a
        no-commit retry is a contract violation that the agent has to address
        head-on.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the agent's working directory.
            pr_head_branch: PR head branch name.
            failing_check_names: Names of currently failing required checks.
            review_threads_block: Pre-built review threads block for the prompt.
            dirty_tracked_changes: Uncommitted tracked file changes (optional).

        Returns:
            Prompt string for the retry invocation.

        """
        failing_block = "\n".join(f"- {n}" for n in failing_check_names) or "- (unknown)"
        dirty_lines = dirty_tracked_changes or []
        dirty_block = "\n".join(f"- {line}" for line in dirty_lines)
        if dirty_block:
            dirty_block = (
                "\n\nThe local worktree also contains uncommitted tracked changes "
                "from the previous turn. Review this existing diff first and either "
                f"commit it after verification or amend it before committing:\n\n{dirty_block}\n"
            )
        remote_block = (
            "The required CI checks below are STILL failing on the remote"
            if failing_check_names
            else "The remote checks may be green, but the PR still needs a committed "
            "repair before the driver can push"
        )
        return (
            f"{review_threads_block}"
            f"## Force-Engagement Retry — Previous Turn Produced No Commit\n\n"
            f"You just returned from a CI-fix session for PR {pr_ref(pr_number)} "
            f"(issue {issue_ref(issue_number)}) WITHOUT producing a new commit on "
            f"branch `{pr_head_branch}`. {remote_block}:\n\n"
            f"{failing_block}\n\n"
            f"{dirty_block}"
            f"Returning no commit when required checks are still red is itself a "
            f"bug; returning no commit after editing tracked files is also a bug. "
            f"Fix the code so the failing checks pass and the PR can merge. If no "
            f"code fix is possible, DO NOT commit a 'blocker' file: a new "
            f"Markdown/docs file will itself fail the repo's lint gates (e.g. "
            f"markdownlint) and turn one blocker into two. Instead leave the tree "
            f"unchanged and report the blocker via the `BLOCKED:` line below — do "
            f"NOT commit any file to document it.\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change, DO NOT create a new branch): "
            f"{pr_head_branch}\n\n"
            f"Required behaviour:\n"
            f"1. Re-read the failing check logs for the names listed above.\n"
            f"2. Make the minimal change that addresses each failure.\n"
            f"3. Run `pixi run python -m pytest tests/ -v` and "
            f"`pre-commit run --all-files` locally to verify before committing. "
            f"This MUST include any markdown/lint hooks — every file you add or "
            f"edit has to pass the repo's own linters, with no rule disabled.\n"
            f"4. **Every commit MUST be cryptographically signed (`git commit -S`).** "
            f"NEVER use `--no-verify`. The repository's CI gate rejects unsigned "
            f"commits and any commit that bypassed pre-commit hooks.\n"
            f"5. Do NOT run `git checkout -b`, `git switch -c`, or any command "
            f"that creates or switches branches — the fix has to land on "
            f"`{pr_head_branch}`.\n"
            f"6. Do NOT add a new top-level `CI_BLOCKER.md` or similar doc file to "
            f"record the blocker — use the `BLOCKED:` line instead.\n\n"
            f"If after the steps above you still cannot produce a commit, reply "
            f"with a single line `BLOCKED: <one-sentence reason>` and stop."
        )

    def record_repeated_no_commit(
        self,
        *,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        failing_check_names: list[str],
    ) -> None:
        """Persist a marker for the next ecosystem run (#846).

        Writes ``state_dir / "repeated-no-commit-<pr>.json"`` so a future
        run (and the human reading the logs) can see which PRs got stuck
        in the no-commit loop. We deliberately do NOT delete the arming
        record here — the PR is still open and may yet land via another
        actor; the marker file is purely a forensics aid.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            pr_head_branch: PR head branch name.
            failing_check_names: Names of failing required checks at time of marker.

        """
        state_dir = self._state_dir()
        marker = state_dir / f"repeated-no-commit-{pr_number}.json"
        try:
            marker.write_text(
                json.dumps(
                    {
                        "issue_number": issue_number,
                        "pr_number": pr_number,
                        "pr_head_branch": pr_head_branch,
                        "failing_required_checks": failing_check_names,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
        except OSError as exc:
            logger.warning(
                "Issue #%s: failed to write repeated-no-commit marker for PR #%s: %s",
                issue_number,
                pr_number,
                exc,
            )

    def retry_no_commit_once(  # codex/claude branches stay coupled to keep one retry path
        self,
        *,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        pr_head_branch: str,
        pre_agent_sha: str,
        session_id: str | None,
        max_retries: int = 2,
    ) -> bool:
        """Re-invoke the agent up to ``max_retries`` times after a no-commit turn (#846).

        A single no-op agent turn used to be terminal: with
        ``max_fix_iterations=1`` the whole issue failed the instant the first
        force-engagement retry also returned no commit. That cost real PRs
        (AchaeanFleet #691/#683, Hermes #643). We now re-engage up to
        ``max_retries`` times before recording the forensics marker, re-checking
        between attempts so a PR that goes green (or where a commit lands)
        short-circuits immediately.

        Only fires when the PR still has failing required checks — a green PR
        that arrived via concurrent activity should NOT be perturbed. Stays on
        the same ``(repo, issue, AGENT_CI_DRIVER)`` session so the agent's
        transcript is continuous; the existing PR and branch are preserved
        (no new PR, no new branch, no force-push to a new ref).

        Args:
            issue_number: GitHub issue number for the PR.
            pr_number: PR number to retry on.
            worktree_path: Worktree the agent runs in (same as the first turn).
            pr_head_branch: PR head branch name; the retry must not switch off it.
            pre_agent_sha: HEAD SHA from before the first turn (used as the
                no-commit baseline for the retry too — if the retry doesn't
                advance past this, we treat it as repeated-no-commit).
            session_id: Codex resume id (Claude resumes by deterministic UUID).
            max_retries: Maximum number of force-engagement retries.

        Returns:
            True if the retry produced a new commit (caller should push).
            False if CI is green now, the retry returned no commit again, or
            any error path fired. On repeated-no-commit, writes a forensics
            marker to ``state_dir``.

        """
        options = self._options()
        repo_root = self._repo_root()
        failing: list[str] = []
        for retry in range(1, max_retries + 1):
            failing = self._failing_required_check_names(pr_number)
            dirty_tracked_changes = self._tracked_worktree_changes(worktree_path, issue_number)
            if not failing and not dirty_tracked_changes:
                logger.info(
                    "Issue #%s: no-commit turn but PR #%s has no failing required checks "
                    "and no tracked worktree changes; skipping force-engagement retry",
                    issue_number,
                    pr_number,
                )
                return False

            review_threads_block = self._format_review_threads_block(pr_number)
            retry_prompt = self.force_engagement_prompt(
                issue_number=issue_number,
                pr_number=pr_number,
                worktree_path=worktree_path,
                pr_head_branch=pr_head_branch,
                failing_check_names=failing,
                dirty_tracked_changes=dirty_tracked_changes,
                review_threads_block=review_threads_block,
            )

            retry_reason = ", ".join(failing) if failing else "tracked worktree changes"
            logger.warning(
                "Issue #%s: no-commit on CI fix turn; re-invoking with "
                "force-engagement prompt (retry %s/%s, reason: %s)",
                issue_number,
                retry,
                max_retries,
                retry_reason,
            )

            try:
                if is_codex(options.agent):
                    if session_id:
                        try:
                            resume_codex_session(
                                session_id,
                                retry_prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                            )
                        except subprocess.CalledProcessError as exc:
                            logger.warning(
                                "Issue #%s: Codex retry resume failed for PR #%s; "
                                "falling back to fresh session: %s",
                                issue_number,
                                pr_number,
                                (exc.stderr or exc.stdout or "")[:300],
                            )
                            run_codex_session(
                                retry_prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                                sandbox="workspace-write",
                            )
                    else:
                        run_codex_session(
                            retry_prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                else:
                    repo_slug = get_repo_slug(repo_root)
                    invoke_claude_with_session(
                        repo=repo_slug,
                        issue=issue_number,
                        agent=AGENT_CI_DRIVER,
                        prompt=retry_prompt,
                        model=implementer_model(),
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                        output_format="json",
                        allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                        extra_args=["--dangerously-skip-permissions"],
                        input_via_stdin=True,
                    )
            except subprocess.CalledProcessError as exc:
                logger.error(
                    "Issue #%s: no-commit retry session failed for PR #%s: %s",
                    issue_number,
                    pr_number,
                    (exc.stderr or exc.stdout or "")[:300],
                )
                return False
            except subprocess.TimeoutExpired:
                logger.error(
                    "Issue #%s: no-commit retry session timed out for PR #%s",
                    issue_number,
                    pr_number,
                )
                return False

            if self._head_advanced(worktree_path, pre_agent_sha, issue_number):
                return True

            logger.warning(
                "Issue #%s: still no commit on PR #%s after force-engagement retry %s/%s",
                issue_number,
                pr_number,
                retry,
                max_retries,
            )

        logger.error(
            "Issue #%s: REPEATED no-commit on PR #%s after %s force-engagement "
            "retries; marking and moving on",
            issue_number,
            pr_number,
            max_retries,
        )
        self.record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing,
        )
        return False

    def run_ci_fix_session(  # noqa: C901  # provider resume/fallback paths are intentionally coupled
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        session_id: str | None,
        advise_findings: str = "",
        *,
        pr_head_branch: str,
    ) -> bool:
        """Invoke the selected coding agent to fix CI failures, then push the result.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            worktree_path: Path to the checked-out worktree.
            ci_logs: Combined CI failure log text.
            session_id: Optional agent session ID to resume.
            advise_findings: Prior learnings from the advise step, prepended to
                the prompt as context. Empty or a skip marker contributes nothing.
            pr_head_branch: The PR's head-branch name on the remote. The push
                uses this as the destination refspec so the fix lands on the
                actual PR branch even if the agent switched branches locally
                during the session (#832).

        Returns:
            True if the fix session succeeded and the branch was pushed.

        """
        options = self._options()
        repo_root = self._repo_root()
        # Sync the worktree to the PR's actual remote head BEFORE the agent
        # runs. WorktreeManager may have reused a stale local branch ref that
        # pointed at an old ``main`` tip — without this reset the agent would
        # commit on top of the wrong base and the force-with-lease push would
        # either no-op or regress the PR (#832).
        try:
            sync_worktree_to_remote_branch(worktree_path, pr_head_branch)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to sync worktree to origin/%s before CI fix: %s",
                issue_number,
                pr_head_branch,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False

        # Snapshot HEAD immediately after the sync. The push helper exits 0
        # silently when HEAD == origin/<branch> (nothing to push), so without
        # this guard we logged "pushed CI fixes" for sessions that returned
        # without committing — the remote was unchanged but the driver
        # claimed success (#836). The post-agent HEAD is compared below.
        try:
            pre_agent_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to snapshot HEAD before CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False

        advise_block = ""
        findings = advise_findings.strip()
        if findings and not findings.startswith("<!-- advise step skipped"):
            advise_block = f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n"
        # Inject any unresolved PR review threads at the top of the prompt so
        # the agent addresses reviewer feedback BEFORE re-running CI — an
        # unresolved bot/human comment is usually the actual blocker (#846).
        review_threads_block = self._format_review_threads_block(pr_number)
        prompt = (
            f"{advise_block}"
            f"{review_threads_block}"
            f"Fix the CI failures for PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}).\n\n"
            f"Working directory: {worktree_path}\n"
            f"Current branch (DO NOT change): {pr_head_branch}\n\n"
            f"CI failure logs:\n{ci_logs}\n\n"
            "Fix the code to make the CI checks pass. After fixing:\n"
            "1. Run: pixi run python -m pytest tests/ -v\n"
            "2. Run: pre-commit run --all-files\n"
            "3. Commit changes (do NOT push) on the current branch — DO NOT run "
            "`git checkout -b`, `git switch -c`, or any other command that creates "
            "or switches to a different branch\n"
            "4. Every commit MUST be cryptographically signed (`git commit -S`); "
            "NEVER use `--no-verify`.\n\n"
            f"Commit message: fix: Address CI failures for PR {pr_ref(pr_number)}\n"
        )

        try:
            if is_codex(options.agent):
                try:
                    if session_id:
                        try:
                            codex_result = resume_codex_session(
                                session_id,
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                            )
                        except subprocess.CalledProcessError as e:
                            logger.warning(
                                "Issue #%s: Codex resume session %r failed for PR #%s; "
                                "falling back to fresh session: %s",
                                issue_number,
                                session_id,
                                pr_number,
                                (e.stderr or e.stdout or "")[:300],
                            )
                            codex_result = run_codex_session(
                                prompt,
                                cwd=worktree_path,
                                timeout=ci_driver_claude_timeout(),
                                sandbox="workspace-write",
                            )
                    else:
                        codex_result = run_codex_session(
                            prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                    logger.debug(
                        "Issue #%s: Codex CI fix output: %s",
                        issue_number,
                        codex_result.stdout[:500],
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(
                        "Issue #%s: Codex CI fix session returned exit code %s: %s",
                        issue_number,
                        e.returncode,
                        (e.stderr or e.stdout or "")[:300],
                    )
                    return False

                # First turn produced no commit → force-engage once on the
                # same session before giving up (#846). Stays on the same PR +
                # branch (no new PR, no new branch). Nested two-step keeps the
                # head-advance check and the retry decision separate for
                # readability.
                if not self._head_advanced(  # noqa: SIM102
                    worktree_path, pre_agent_sha, issue_number
                ):
                    if not self.retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                if not self._ci_fix_head_is_pushable(worktree_path, issue_number):
                    return False
                try:
                    push_current_branch_with_lease_on_divergence(
                        worktree_path,
                        branch=pr_head_branch,
                        push_ref=f"HEAD:{pr_head_branch}",
                    )
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            # drive-green runs its OWN session (Session 3, AGENT_CI_DRIVER),
            # independent of the implementer's transcript. The first fix call
            # creates it via --session-id; later calls resume it. The codex
            # path above instead resumes the raw ``session_id`` it was handed.
            repo_slug = get_repo_slug(repo_root)
            try:
                stdout, _ = invoke_claude_with_session(
                    repo=repo_slug,
                    issue=issue_number,
                    agent=AGENT_CI_DRIVER,
                    prompt=prompt,
                    model=implementer_model(),
                    cwd=worktree_path,
                    timeout=ci_driver_claude_timeout(),
                    output_format="json",
                    allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                    extra_args=["--dangerously-skip-permissions"],
                    input_via_stdin=True,
                )
                claude_result = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=""
                )
            except subprocess.CalledProcessError as exc:
                claude_result = subprocess.CompletedProcess(
                    args=exc.cmd,
                    returncode=exc.returncode,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                )

            if claude_result.returncode == 0:
                # Push the fixes
                # First turn produced no commit → force-engage once on the
                # same session before giving up (#846). Stays on the same PR +
                # branch (no new PR, no new branch). Nested two-step keeps the
                # head-advance check and the retry decision separate for
                # readability.
                if not self._head_advanced(  # noqa: SIM102
                    worktree_path, pre_agent_sha, issue_number
                ):
                    if not self.retry_no_commit_once(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        worktree_path=worktree_path,
                        pr_head_branch=pr_head_branch,
                        pre_agent_sha=pre_agent_sha,
                        session_id=session_id,
                    ):
                        return False
                if not self._ci_fix_head_is_pushable(worktree_path, issue_number):
                    return False
                try:
                    push_current_branch_with_lease_on_divergence(
                        worktree_path,
                        branch=pr_head_branch,
                        push_ref=f"HEAD:{pr_head_branch}",
                    )
                    logger.info("Issue #%s: pushed CI fixes for PR #%s", issue_number, pr_number)
                    return True
                except Exception as push_err:
                    logger.error(
                        "Issue #%s: git push failed after CI fix: %s", issue_number, push_err
                    )
                    return False

            logger.error(
                "Issue #%s: Claude CI fix session returned exit code %s: %s",
                issue_number,
                claude_result.returncode,
                claude_result.stderr[:300],
            )
            return False

        except subprocess.TimeoutExpired:
            logger.error(
                "Issue #%s: Claude CI fix session timed out for PR #%s", issue_number, pr_number
            )
            return False
        except Exception as e:
            logger.error(
                "Issue #%s: CI fix session failed for PR #%s: %s", issue_number, pr_number, e
            )
            return False

    def attempt_mechanical_rebase(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        get_worktree_path: Callable[[int, int], Path],
    ) -> bool:
        """Rebase a behind/conflicting PR onto its base branch with no agent (#871).

        The cheap, deterministic path: a PR that is merely behind its base (or
        whose changes don't textually overlap) is rebased and pushed here. Only a
        PR whose rebase hits real conflicts falls through to the CI-fix agent.

        Flow:

        1. Query ``mergeStateStatus`` / ``mergeable`` / ``baseRefName``. Skip
           (return ``False``) unless the PR is ``BEHIND`` or ``DIRTY`` /
           ``CONFLICTING`` — a PR already on top of its base needs no rebase and
           the normal check-status path handles it.
        2. Sync the worktree to the PR head, then rebase onto the base branch.
        3. Clean rebase → push ``HEAD:<pr-head>`` with ``--force-with-lease`` and
           return ``True``. The push re-triggers CI; the caller's poll loop
           evaluates the rebased head.
        4. Conflicts → the rebase is aborted inside ``rebase_worktree_onto``;
           return ``False`` so the caller continues to the agent path.

        Any unexpected git/subprocess error is logged and swallowed (returns
        ``False``) — a mechanical-rebase failure must never crash the worker; the
        agent path is always the safe fallback.

        Args:
            issue_number: GitHub issue number for the PR.
            pr_number: PR number to rebase.
            acquired_slot: Worker slot ID for status tracking.
            get_worktree_path: Callable resolving the worktree path for (issue, pr).

        Returns:
            ``True`` if the PR was mechanically rebased and pushed; ``False`` if
            no rebase was needed, the rebase conflicted, or an error occurred.

        """
        status_tracker = self._status()
        try:
            result = _gh_call(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "mergeStateStatus,mergeable,headRefName,baseRefName",
                ],
                check=False,
            )
            state = dict(json.loads(result.stdout or "{}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning(
                "Issue #%s: could not fetch PR #%s merge state for rebase; "
                "skipping mechanical rebase: %s",
                issue_number,
                pr_number,
                exc,
            )
            return False

        merge_state = str(state.get("mergeStateStatus") or "").upper()
        # Only behind-base / conflicting PRs need a rebase. CLEAN / BLOCKED /
        # UNSTABLE / HAS_HOOKS PRs are already on top of their base — let the
        # check-status path handle them. (BLOCKED is the review-gated case.)
        if merge_state not in ("BEHIND", "DIRTY", "CONFLICTING"):
            return False

        pr_head_branch = str(state.get("headRefName") or "") or self._get_pr_branch(pr_number)
        base_branch = str(state.get("baseRefName") or "main") or "main"
        if not pr_head_branch:
            logger.warning(
                "Issue #%s: PR #%s has no resolvable head branch; skipping rebase",
                issue_number,
                pr_number,
            )
            return False

        status_tracker.update_slot(
            acquired_slot,
            f"{issue_ref(issue_number)}: mechanical rebase onto {base_branch}",
        )

        try:
            worktree_path = get_worktree_path(issue_number, pr_number)
            # Land on the PR's actual remote head before rebasing so we replay the
            # PR's commits (not a stale local ref) onto the base (#832).
            sync_worktree_to_remote_branch(worktree_path, pr_head_branch)

            if not rebase_worktree_onto(worktree_path, base_branch):
                logger.info(
                    "Issue #%s: PR #%s (%s) has rebase conflicts onto %s; deferring to agent",
                    issue_number,
                    pr_number,
                    merge_state,
                    base_branch,
                )
                return False

            # Clean rebase rewrote history — lease-push to the PR head. The helper
            # no-ops cleanly if the rebase was already up to date (HEAD unchanged).
            push_current_branch_with_lease_on_divergence(
                worktree_path,
                branch=pr_head_branch,
                push_ref=f"HEAD:{pr_head_branch}",
            )
            logger.info(
                "Issue #%s: mechanically rebased PR #%s onto %s and pushed (no agent)",
                issue_number,
                pr_number,
                base_branch,
            )
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Issue #%s: mechanical rebase of PR #%s failed (%s); falling through to agent",
                issue_number,
                pr_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False
