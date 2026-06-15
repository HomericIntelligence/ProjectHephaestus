"""CI fix session management: drives a coding agent to fix failing CI checks.

Provides:
- CIFixOrchestrator: manages per-issue CI fix sessions, agent invocation,
  push-guard, no-commit retry logic, and bot-thread resolution.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    is_codex,
    resume_codex_session,
    run_codex_session,
    session_agent_matches,
)

from .advise_runner import run_advise
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, implementer_model
from .claude_timeouts import advise_claude_timeout, ci_driver_claude_timeout
from .git_utils import (
    get_repo_slug,
    issue_ref,
    pr_ref,
    push_current_branch_with_lease_on_divergence,
    run,
    sync_worktree_to_remote_branch,
)
from .github_api import (
    gh_issue_json,
    gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread,
)
from .models import WorkerResult
from .prompts import get_advise_prompt_builder
from .session_naming import AGENT_ADVISE, AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


def _is_bot_pr_mode(issue_number: int, pr_number: int) -> bool:
    """Return True iff this work item is a synthetic-issue bot PR (#848).

    The bot-PR enumeration uses the PR number as a stand-in for an
    issue number because Dependabot PRs have no associated issue.
    Anywhere we would normally call ``gh issue view <issue_number>``
    we must instead short-circuit; this helper centralises the check
    so a single rule (issue == pr) keeps both ends honest.
    """
    return issue_number == pr_number


def _parse_json_block(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON block from a text string.

    Args:
        text: Input text that may contain a JSON block.

    Returns:
        Parsed dictionary, or empty dict if no valid JSON found.

    """
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            return dict(json.loads(match.group(1)))

    # Try raw JSON
    with contextlib.suppress(json.JSONDecodeError):
        return dict(json.loads(text))

    return {}


class CIFixOrchestrator:
    """Orchestrates CI fix sessions for failing PRs.

    Manages agent invocation, no-commit retry logic, push-guard, and
    bot-thread resolution. All CIDriver state is accessed via injected
    provider callables so this class has no direct dependency on CIDriver.
    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Path],
        state_dir_provider: Callable[[], Path],
        status_tracker_provider: Callable[[], Any],
        get_pr_branch: Callable[[int], str],
        get_worktree_path: Callable[[int, int], Path],
        format_review_threads_block: Callable[[int], str],
        failing_required_check_names: Callable[[int], list[str]],
        get_failing_ci_logs: Callable[[int], str],
    ) -> None:
        """Initialise the orchestrator with provider callables.

        Args:
            options_provider: Returns the driver's options object.
            repo_root_provider: Returns the repository root Path.
            state_dir_provider: Returns the state directory Path.
            status_tracker_provider: Returns the status tracker instance.
            get_pr_branch: Returns the PR head-branch name for a PR number.
            get_worktree_path: Returns the worktree Path for (issue, pr).
            format_review_threads_block: Returns the review-threads Markdown block.
            failing_required_check_names: Returns failing required check names.
            get_failing_ci_logs: Returns combined CI failure logs for a PR number.

        """
        self._options_provider = options_provider
        self._repo_root_provider = repo_root_provider
        self._state_dir_provider = state_dir_provider
        self._status_tracker_provider = status_tracker_provider
        self._get_pr_branch = get_pr_branch
        self._get_worktree_path = get_worktree_path
        self._format_review_threads_block = format_review_threads_block
        self._failing_required_check_names = failing_required_check_names
        self._get_failing_ci_logs = get_failing_ci_logs

    # ------------------------------------------------------------------
    # Advise step
    # ------------------------------------------------------------------

    def _run_advise(self, issue_number: int) -> str:
        """Pull prior learnings from ProjectMnemosyne before the CI fix loop.

        Stage 3's advise-first step. Runs under ``AGENT_ADVISE`` (its own cheap,
        read-only session), gated by ``enable_advise``; the findings are
        prepended to the CI fix-session prompt. Delegates the Mnemosyne setup +
        prompt build to the shared :mod:`advise_runner`; any failure degrades to
        a skip marker so the drive never aborts over missing advice.
        """
        issue_data = gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        def _invoke(prompt: str) -> str:
            if is_codex(self._options_provider().agent):
                result = run_codex_session(
                    prompt,
                    cwd=self._repo_root_provider(),
                    timeout=advise_claude_timeout(),
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            repo_slug = get_repo_slug(self._repo_root_provider())
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_ADVISE,
                prompt=prompt,
                model=advise_model(),
                cwd=self._repo_root_provider(),
                timeout=advise_claude_timeout(),
                output_format="text",
                allowed_tools="Read,Glob,Grep,Bash",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt_builder(self._options_provider().agent),
        )

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def _attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        extra_context: str = "",
    ) -> WorkerResult | None:
        """Attempt CI fix iterations for a failing PR.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.
            acquired_slot: Worker slot ID for status tracking.
            extra_context: Optional text prepended to the CI failure logs in the
                fix-session prompt — e.g. merge-conflict resolution instructions
                for a DIRTY PR, where ``_get_failing_ci_logs`` alone is empty.

        Returns:
            WorkerResult on success or dry-run, None if all iterations failed.

        """
        # Advise-first (#30): pull prior learnings once before the fix loop, so
        # we only spend an advise call on PRs that actually need fixing. Fed
        # into every fix-session prompt below. Skipped for bot-PR work items
        # (#848): the issue number is synthetic (equals the PR number) so
        # ``gh issue view`` would 404; there is also no human-authored issue
        # body that would meaningfully steer the advise prompt.
        advise_findings = ""
        if self._options_provider().enable_advise and not _is_bot_pr_mode(
            issue_number, pr_number
        ):
            self._status_tracker_provider().update_slot(
                acquired_slot, f"{issue_ref(issue_number)}: advising"
            )
            advise_findings = self._run_advise(issue_number)

        for iteration in range(self._options_provider().max_fix_iterations):
            self._status_tracker_provider().update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: fetching CI logs (attempt {iteration + 1})",
            )
            ci_logs = self._get_failing_ci_logs(pr_number)
            if extra_context:
                ci_logs = f"{extra_context}\n\n{ci_logs}".strip()
            session_id = self._load_impl_session_id(issue_number)
            worktree_path = self._get_worktree_path(issue_number, pr_number)
            # Resolve the PR's actual head-branch name once per iteration. The
            # CI fix push must target THIS remote ref even if Claude switches
            # branches locally during the session (#832).
            pr_head_branch = self._get_pr_branch(pr_number)

            if self._options_provider().dry_run:
                logger.info(
                    "[dry_run] Would run CI fix session for PR #%s (issue #%s, iteration %s)",
                    pr_number,
                    issue_number,
                    iteration + 1,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            self._status_tracker_provider().update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: running CI fix session (attempt {iteration + 1})",
            )
            fixed = self._run_ci_fix_session(
                issue_number,
                pr_number,
                worktree_path,
                ci_logs,
                session_id,
                advise_findings,
                pr_head_branch=pr_head_branch,
            )
            if fixed:
                logger.info(
                    "Issue #%s: CI fix applied successfully (attempt %s)",
                    issue_number,
                    iteration + 1,
                )
                # Acknowledge automated review comments the fix addressed by
                # replying + resolving the bot threads (human threads untouched).
                self._reply_and_resolve_bot_threads(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)

        return None

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _load_impl_session_id(self, issue_number: int) -> str | None:
        """Load the agent session ID from the implementer's saved state.

        Args:
            issue_number: GitHub issue number.

        Returns:
            Session ID string, or None if not found.

        """
        # The implementer persists its state to ``issue-<n>.json`` (see
        # ImplementationStateManager.save), NOT ``state-<n>.json``. Reading the
        # wrong name always missed, so this lookup silently never resumed the
        # implementer's session — masked for Claude (deterministic-UUID resume)
        # but breaking Codex session continuity on the CI-fix path.
        state_file = self._state_dir_provider() / f"issue-{issue_number}.json"
        if not state_file.exists():
            logger.debug("No implementer state file for issue #%s", issue_number)
            return None

        try:
            data = json.loads(state_file.read_text())
            session_id: str | None = data.get("session_id")
            session_agent: str | None = data.get("session_agent")
            if session_id and not session_agent_matches(
                session_agent, self._options_provider().agent
            ):
                logger.info(
                    "Skipping impl session for issue #%s: session belongs to %s, "
                    "selected agent is %s",
                    issue_number,
                    session_agent or "claude",
                    self._options_provider().agent,
                )
                return None
            if session_id:
                logger.debug(
                    "Loaded session_id for issue #%s: %s...", issue_number, session_id[:8]
                )
            return session_id
        except Exception as e:
            logger.warning("Could not load session_id for issue #%s: %s", issue_number, e)
            return None

    # ------------------------------------------------------------------
    # Thread helpers
    # ------------------------------------------------------------------

    def _list_unresolved_threads_safe(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch unresolved review threads, swallowing lookup failures.

        Shared by the prompt-context formatter and the post-fix bot-thread
        reply/resolve step so both run off a single fetch contract. Network/JSON
        errors are downgraded to an info log and yield an empty list — neither
        caller is ever gated on review-thread availability (#846).
        """
        try:
            return gh_pr_list_unresolved_threads(
                pr_number, dry_run=self._options_provider().dry_run
            )
        except Exception as exc:
            logger.info(
                "Issue PR #%s: failed to fetch unresolved review threads (%s); "
                "skipping review-thread handling",
                pr_number,
                exc,
            )
            return []

    @staticmethod
    def _is_bot_author(login: str) -> bool:
        """Return True for automated review authors (GitHub App / bot accounts).

        GitHub App authors carry a ``[bot]`` suffix on their login
        (``github-actions[bot]``, ``coderabbitai[bot]``, ``dependabot[bot]``).
        Human review threads are never auto-resolved — only the bot's own reply
        thread is closed after the CI fix addresses it.
        """
        return login.endswith("[bot]")

    def _reply_and_resolve_bot_threads(self, pr_number: int) -> int:
        """Resolve automated review threads after a successful CI fix.

        ci_driver surfaces unresolved threads to the fix prompt but cannot rely
        on GitHub auto-resolving a bot thread when its line moves. After a fix
        lands, resolve each BOT-authored unresolved thread without adding
        another reply comment, so automated review comments are closed rather
        than left dangling. Human threads are left untouched. Best-effort: a
        failure on one thread is logged and skipped (never blocks the fix).

        Returns the number of threads resolved.
        """
        if self._options_provider().dry_run:
            return 0
        threads = self._list_unresolved_threads_safe(pr_number)
        resolved = 0
        for t in threads:
            if not self._is_bot_author(t.get("author") or ""):
                continue
            thread_id = t.get("id")
            if not thread_id:
                continue
            try:
                gh_pr_resolve_thread(thread_id, dry_run=False)
                resolved += 1
            except Exception as exc:
                logger.info(
                    "PR #%s: could not resolve bot thread %s (%s); skipping",
                    pr_number,
                    thread_id,
                    exc,
                )
        if resolved:
            logger.info(
                "PR #%s: resolved %s automated review thread(s)",
                pr_number,
                resolved,
            )
        return resolved

    # ------------------------------------------------------------------
    # Worktree change inspection
    # ------------------------------------------------------------------

    def _tracked_worktree_changes(self, worktree_path: Path, issue_number: int) -> list[str]:
        """Return tracked dirty status lines for a post-agent worktree.

        Untracked tool output such as a local ``uv.lock`` is intentionally
        ignored. A no-commit turn that left tracked files modified is still
        actionable even when the remote has no red required checks, as happens
        for merge-conflict/behind-branch repairs where the agent fixed files but
        forgot the signed commit.
        """
        status = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "status", "--porcelain"],
            "failed to inspect worktree status for no-commit retry",
        )
        if status is None:
            return []
        return [
            line for line in status.splitlines() if line.strip() and not line.startswith("?? ")
        ]

    # ------------------------------------------------------------------
    # Force-engagement retry prompt
    # ------------------------------------------------------------------

    def _force_engagement_prompt(
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

    def _record_repeated_no_commit(
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
        """
        marker = self._state_dir_provider() / f"repeated-no-commit-{pr_number}.json"
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

    # ------------------------------------------------------------------
    # Agent invocation
    # ------------------------------------------------------------------

    def _invoke_agent_session(
        self,
        *,
        prompt: str,
        session_id: str | None,
        worktree_path: Path,
        issue_number: int,
        pr_number: int,
    ) -> subprocess.CompletedProcess[str]:
        """Dispatch a prompt to the configured agent (codex or claude).

        Returns a CompletedProcess whose returncode signals success or failure:
        - returncode == 0: agent ran successfully (stdout has output)
        - returncode != 0: agent failed (stderr has details)

        For codex, returncode is synthetic — AgentRunResult has no returncode
        field; success is "no CalledProcessError" (hephaestus/agents/runtime.py).
        For claude, returncode comes from the subprocess exit code.

        CalledProcessError is absorbed from all codex paths into a non-zero
        CompletedProcess. TimeoutExpired propagates to the caller so it can
        log a distinct timeout message.
        """
        if is_codex(self._options_provider().agent):
            if session_id:
                try:
                    result = resume_codex_session(
                        session_id,
                        prompt,
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                    )
                except subprocess.CalledProcessError as exc:
                    logger.warning(
                        "Issue #%s: Codex resume session %r failed for PR #%s; "
                        "falling back to fresh session: %s",
                        issue_number,
                        session_id,
                        pr_number,
                        (exc.stderr or exc.stdout or "")[:300],
                    )
                    try:
                        result = run_codex_session(
                            prompt,
                            cwd=worktree_path,
                            timeout=ci_driver_claude_timeout(),
                            sandbox="workspace-write",
                        )
                    except subprocess.CalledProcessError as fresh_exc:
                        return subprocess.CompletedProcess(
                            args=fresh_exc.cmd,
                            returncode=fresh_exc.returncode,
                            stdout=fresh_exc.stdout or "",
                            stderr=fresh_exc.stderr or "",
                        )
            else:
                try:
                    result = run_codex_session(
                        prompt,
                        cwd=worktree_path,
                        timeout=ci_driver_claude_timeout(),
                        sandbox="workspace-write",
                    )
                except subprocess.CalledProcessError as exc:
                    return subprocess.CompletedProcess(
                        args=exc.cmd,
                        returncode=exc.returncode,
                        stdout=exc.stdout or "",
                        stderr=exc.stderr or "",
                    )
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=result.stdout, stderr=result.stderr or ""
            )

        repo_slug = get_repo_slug(self._repo_root_provider())
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
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        except subprocess.CalledProcessError as exc:
            return subprocess.CompletedProcess(
                args=exc.cmd,
                returncode=exc.returncode,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )

    # ------------------------------------------------------------------
    # Push guards
    # ------------------------------------------------------------------

    def _push_ci_fix(
        self,
        *,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
        pr_number: int,
        pr_head_branch: str,
        session_id: str | None,
    ) -> bool:
        """Check head advancement, retry if needed, then push CI fixes.

        Shared post-agent contract for both codex and claude providers (#846).
        Returns True if fixes were pushed, False on any failure or no-commit.
        """
        if not self._head_advanced(worktree_path, pre_agent_sha, issue_number):  # noqa: SIM102
            if not self._retry_no_commit_once(
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
            logger.error("Issue #%s: git push failed after CI fix: %s", issue_number, push_err)
            return False

    def _retry_no_commit_once(
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

        Returns:
            True if the retry produced a new commit (caller should push).
            False if CI is green now, the retry returned no commit again, or
            any error path fired. On repeated-no-commit, writes a forensics
            marker to ``state_dir``.

        """
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
            retry_prompt = self._force_engagement_prompt(
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
                retry_result = self._invoke_agent_session(
                    prompt=retry_prompt,
                    session_id=session_id,
                    worktree_path=worktree_path,
                    issue_number=issue_number,
                    pr_number=pr_number,
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    "Issue #%s: no-commit retry session timed out for PR #%s",
                    issue_number,
                    pr_number,
                )
                return False

            if retry_result.returncode != 0:
                logger.error(
                    "Issue #%s: no-commit retry session failed for PR #%s: %s",
                    issue_number,
                    pr_number,
                    (retry_result.stderr or "")[:300],
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
        self._record_repeated_no_commit(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            failing_check_names=failing,
        )
        return False

    def _head_advanced(
        self,
        worktree_path: Path,
        pre_agent_sha: str,
        issue_number: int,
    ) -> bool:
        """Return True iff HEAD has moved past ``pre_agent_sha`` after the agent ran.

        Called between the agent session and the push to detect the
        no-commit-made case (#836). When ``pre_agent_sha`` still matches HEAD
        the agent did not commit anything, so a force-with-lease push of
        HEAD:<branch> would be a silent 0-exit no-op and the driver would
        falsely log "pushed CI fixes". We instead log a warning and return
        False so the iteration counts as failed.

        Args:
            worktree_path: Worktree to inspect.
            pre_agent_sha: HEAD SHA captured right after the pre-agent sync.
            issue_number: For log context.

        Returns:
            True if HEAD moved (something to push); False if it did not (or
            if reading HEAD failed — we treat that as "don't push" too).

        """
        try:
            post_agent_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to read HEAD after CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return False
        if post_agent_sha == pre_agent_sha:
            logger.warning(
                "Issue #%s: agent session produced no new commit (HEAD unchanged "
                "at %s); skipping push and treating iteration as failed",
                issue_number,
                pre_agent_sha[:8],
            )
            return False
        return True

    def _git_stdout_for_push_guard(
        self,
        worktree_path: Path,
        issue_number: int,
        argv: list[str],
        failure_message: str,
    ) -> str | None:
        """Run a git inspection command for the CI pre-push guard."""
        try:
            result = run(argv, cwd=worktree_path, capture_output=True, check=False)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: %s: %s",
                issue_number,
                failure_message,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return None
        if result.returncode != 0:
            logger.error(
                "Issue #%s: %s: %s",
                issue_number,
                failure_message,
                (result.stderr or result.stdout or "")[:300],
            )
            return None
        return result.stdout or ""

    def _ci_fix_head_is_pushable(
        self,
        worktree_path: Path,
        issue_number: int,
        *,
        base_ref: str = "origin/main",
    ) -> bool:
        """Return True when the post-agent worktree is safe to push.

        ``_head_advanced`` only proves HEAD changed. A conflict-resolution agent
        can still leave the index unmerged, leave tracked files uncommitted, or
        accidentally detach at the base branch itself. None of those states may
        be pushed to the PR head.
        """
        unmerged = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "diff", "--name-only", "--diff-filter=U"],
            "failed to inspect merge state before push",
        )
        if unmerged is None:
            return False
        unmerged_paths = [line for line in unmerged.splitlines() if line.strip()]
        if unmerged_paths:
            logger.error(
                "Issue #%s: refusing to push CI fix with unresolved merge paths: %s",
                issue_number,
                ", ".join(unmerged_paths[:10]),
            )
            return False

        status = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "status", "--porcelain"],
            "failed to inspect worktree status before push",
        )
        if status is None:
            return False
        tracked_dirty = [
            line for line in status.splitlines() if line.strip() and not line.startswith("?? ")
        ]
        if tracked_dirty:
            logger.error(
                "Issue #%s: refusing to push CI fix with uncommitted tracked changes: %s",
                issue_number,
                ", ".join(tracked_dirty[:10]),
            )
            return False

        ahead = self._git_stdout_for_push_guard(
            worktree_path,
            issue_number,
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
            f"failed to inspect HEAD ahead of {base_ref} before push",
        )
        if ahead is None:
            return False
        try:
            ahead_count = int(ahead.strip() or "0")
        except ValueError:
            logger.error(
                "Issue #%s: refusing to push CI fix with invalid ahead count: %r",
                issue_number,
                ahead,
            )
            return False
        if ahead_count <= 0:
            logger.error(
                "Issue #%s: refusing to push CI fix because HEAD has no commits ahead of %s",
                issue_number,
                base_ref,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _sync_worktree_and_snapshot_sha(
        self, issue_number: int, worktree_path: Path, pr_head_branch: str
    ) -> str | None:
        """Sync worktree to remote PR head and snapshot HEAD SHA.

        Returns the pre-agent SHA string, or ``None`` on any subprocess failure
        (caller should return ``False`` immediately).

        Syncing before the agent prevents the agent from committing on a stale
        base and failing the force-with-lease push (#832). Snapshotting the SHA
        lets the push helper detect sessions that return without committing (#836).
        """
        try:
            sync_worktree_to_remote_branch(worktree_path, pr_head_branch)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to sync worktree to origin/%s before CI fix: %s",
                issue_number,
                pr_head_branch,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return None
        try:
            return run(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Issue #%s: failed to snapshot HEAD before CI fix session: %s",
                issue_number,
                (exc.stderr or exc.stdout or "")[:300],
            )
            return None

    def _build_ci_fix_prompt(
        self,
        issue_number: int,
        pr_number: int,
        worktree_path: Path,
        ci_logs: str,
        pr_head_branch: str,
        advise_findings: str,
    ) -> str:
        """Build the CI-fix agent prompt string."""
        advise_block = ""
        findings = advise_findings.strip()
        if findings and not findings.startswith("<!-- advise step skipped"):
            advise_block = f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n"
        review_threads_block = self._format_review_threads_block(pr_number)
        return (
            f"{advise_block}{review_threads_block}"
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

    def _run_ci_fix_session(
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
        pre_agent_sha = self._sync_worktree_and_snapshot_sha(
            issue_number, worktree_path, pr_head_branch
        )
        if pre_agent_sha is None:
            return False

        prompt = self._build_ci_fix_prompt(
            issue_number,
            pr_number,
            worktree_path,
            ci_logs,
            pr_head_branch,
            advise_findings,
        )

        try:
            agent_result = self._invoke_agent_session(
                prompt=prompt,
                session_id=session_id,
                worktree_path=worktree_path,
                issue_number=issue_number,
                pr_number=pr_number,
            )
        except subprocess.TimeoutExpired:
            logger.error("Issue #%s: CI fix session timed out for PR #%s", issue_number, pr_number)
            return False
        except Exception as e:
            logger.error(
                "Issue #%s: CI fix session failed for PR #%s: %s", issue_number, pr_number, e
            )
            return False

        if agent_result.returncode != 0:
            logger.error(
                "Issue #%s: CI fix session returned exit code %s: %s",
                issue_number,
                agent_result.returncode,
                (agent_result.stderr or "")[:300],
            )
            return False

        logger.debug("Issue #%s: CI fix output: %s", issue_number, agent_result.stdout[:500])
        return self._push_ci_fix(
            worktree_path=worktree_path,
            pre_agent_sha=pre_agent_sha,
            issue_number=issue_number,
            pr_number=pr_number,
            pr_head_branch=pr_head_branch,
            session_id=session_id,
        )
