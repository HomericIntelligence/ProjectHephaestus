"""Post-merge processor for the drive-green /learn + /compact flow.

Extracted from :class:`hephaestus.automation.ci_driver.CIDriver` as part of
the #1357 SRP decomposition. Owns the per-issue arming-state lifecycle
(arm, load, save, clear) and fires the drive-green ``/learn`` and ``/compact``
sessions once a PR reaches merged state.

All external state is accessed through narrow ``Callable`` providers injected
at construction (Dependency Inversion Principle). ``CIDriver`` passes
``lambda``-wrapped references so that ``patch.object(driver, "_method")``
intercepts correctly at call time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, run_codex_session

from .arming_state import ArmingStateStore
from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .claude_timeouts import learn_claude_timeout
from .git_utils import get_repo_slug, issue_ref, pr_ref
from .learn import build_learn_prompt, compact_session, mnemosyne_update_evidence
from .session_naming import AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


class PostMergeProcessor:
    """Drive-green /learn + /compact after a PR merges.

    Owns the per-issue arming-state lifecycle and fires the drive-green
    learnings and compact sessions once a PR reaches merged state.

    Attributes:
        _options_provider: Zero-arg callable returning the driver's options.
        _repo_root_provider: Zero-arg callable returning the repo root path.
        _get_worktree_path: Two-arg callable ``(issue_number, pr_number) ->
            Path`` returning the worktree path for a given issue/PR pair.
        _shared_pr_issues_provider: Zero-arg callable returning the
            ``shared_pr_issues`` mapping ``{pr_number: [issue_number, ...]}``.
        _arming_store: Persistence layer for drive-green arming records.
        _last_drive_green_learn_evidence: Evidence dict from the most recent
            drive-green /learn session.

    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Path],
        get_worktree_path: Callable[[int, int], Path],
        shared_pr_issues_provider: Callable[[], dict[int, list[int]]],
        state_dir_provider: Callable[[], Path] | None = None,
    ) -> None:
        """Initialize the post-merge processor.

        Args:
            options_provider: Zero-arg callable returning the driver's options
                namespace.
            repo_root_provider: Zero-arg callable returning the repository root
                ``Path``.
            get_worktree_path: Two-arg callable ``(issue_number, pr_number) ->
                Path`` used to locate the worktree for /learn and /compact.
            shared_pr_issues_provider: Zero-arg callable returning the
                ``{pr_number: [issue_number, ...]}`` mapping used by
                ``_arm_drive_green`` to iterate sibling issues.
            state_dir_provider: Optional zero-arg callable returning the
                arming-state directory ``Path``. When ``None`` the processor
                falls back to ``repo_root / "build" / ".issue_implementer"``.
                Pass ``lambda: driver.state_dir`` so test overrides of
                ``state_dir`` propagate into the arming store.

        """
        self._options_provider = options_provider
        self._repo_root_provider = repo_root_provider
        self._get_worktree_path = get_worktree_path
        self._shared_pr_issues_provider = shared_pr_issues_provider
        if state_dir_provider is not None:
            self._arming_store = ArmingStateStore(state_dir_provider)
        else:
            self._arming_store = ArmingStateStore(
                lambda: self._repo_root_provider() / "build" / ".issue_implementer"
            )
        self._last_drive_green_learn_evidence: str | dict[str, Any] = ""

    # ------------------------------------------------------------------
    # Arming-state lifecycle
    # ------------------------------------------------------------------

    def _arming_state_path(self, issue_number: int) -> Path:
        return self._arming_store.path(issue_number)

    def _load_arming_state(self, issue_number: int) -> dict[str, Any] | None:
        """Return the parsed arming record for ``issue_number`` or ``None``."""
        return self._arming_store.load(issue_number)

    def _save_arming_state(self, issue_number: int, record: dict[str, Any]) -> None:
        """Persist the arming record. Best-effort; logs and swallows IO errors."""
        self._arming_store.save(issue_number, record)

    def _clear_arming_state(self, issue_number: int) -> None:
        self._arming_store.clear(issue_number)

    # ------------------------------------------------------------------
    # Learn-record helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _learn_record_terminal(record: dict[str, Any]) -> bool:
        """Return whether a drive-green /learn record should not be retried."""
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

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
            evidence = self._last_drive_green_learn_evidence
            if not isinstance(evidence, dict):
                evidence = mnemosyne_update_evidence(str(evidence))
            record.update(evidence)
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
    # Arming
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
        siblings = self._shared_pr_issues_provider().get(pr_number, [])
        if not siblings:
            # Defensive: the PR map should always know the issue, but if not
            # we still want SOMETHING to fire /learn on the next run.
            return
        armed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for issue_num in siblings:
            existing = self._load_arming_state(issue_num) or {}
            if self._learn_record_terminal(existing):
                # Already attempted terminally — don't overwrite learn evidence.
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
    # Drive-green /learn and /compact
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
            repo_slug = get_repo_slug(self._repo_root_provider())
            # Best-effort: try to resume in the original worktree (so the
            # AGENT_CI_DRIVER transcript is found by ``session_jsonl_path``).
            # In the post-merge code path (#840) the PR's head branch may be
            # gone from the remote, so ``_get_worktree_path`` could fail. In
            # that case fall back to ``repo_root`` cwd — the prompt is
            # self-contained, and a fresh ``--session-id`` create still
            # captures the lesson.
            try:
                cwd = self._get_worktree_path(issue_number, pr_number)
            except Exception as wt_err:
                logger.info(
                    "Issue #%s: no worktree available for /learn (%s); "
                    "falling back to repo root for a fresh session",
                    issue_number,
                    wt_err,
                )
                cwd = self._repo_root_provider()
            if is_codex(self._options_provider().agent):
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
                # /learn inherits the parent phase's model. drive-green resumes
                # the implementer's session, and ``claude --resume`` is locked to
                # the model that created it, so we must use implementer_model().
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
        if is_codex(self._options_provider().agent):
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
            cwd = self._repo_root_provider()
        repo_slug = get_repo_slug(self._repo_root_provider())
        return compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_CI_DRIVER,
            cwd=cwd,
            model=implementer_model(),
        )
