"""Validate that prior PR review comments were actually addressed.

The in-loop review → address cycle (see
:meth:`hephaestus.automation.implementer_phase_runner.ImplementationPhaseRunner._run_impl_review_loop`)
used to resolve review threads on the implementer's *self-report* — the
implementer claimed it addressed a thread and the orchestrator resolved it,
even when no commit was produced (#1083). This module is now the single owner
of thread resolution, and it is evidence-based: a FRESH read-only sub-agent
compares each prior comment against the current diff and partitions them:

- **Addressed** — the diff genuinely resolves the comment. The validator
  resolves the thread in place (:func:`gh_pr_resolve_thread`).
- **Not addressed** — re-opened by posting a NEW inline review thread (GitHub
  has no "unresolve" mutation, and the unresolved-thread lister filters
  resolved threads out — so an already-resolved thread cannot be reopened in
  place). The new thread cites the original comment and explains what is still
  missing, then the loop treats the validation as NOGO so the address step runs
  again.

The implementer's address step no longer resolves anything; it only applies the
fix, commits, and pushes. A clean worktree (no real fix) therefore leaves the
diff unchanged, the validator judges the thread NOT addressed, and it stays
open — closing the "resolved without implementing" hole.

This respects the #375 own-threads-only guarantee (the validator posts and
resolves only its own / the bot's threads) and never mutates human threads.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.io.utils import write_secure

from ._review_utils import log_file_path, parse_json_block
from .claude_invoke import invoke_claude_with_session, raise_for_error_envelope
from .claude_models import reviewer_model
from .claude_timeouts import DEFAULT_AGENT_TIMEOUT
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .github_api import gh_pr_resolve_thread, gh_pr_review_post
from .prompts import get_review_validation_prompt
from .protocol import WONT_FIX_MARKER
from .session_naming import AGENT_PR_REVIEWER, reviewer_agent

logger = logging.getLogger(__name__)

# Number of source lines on EACH side of a comment's line to scan for a
# documenting comment/docstring when deciding whether a recurring re-open is
# an accepted by-design decision (#1329). Kept small and local so the heuristic
# only honours documentation that sits AT the cited code, not a stray comment
# elsewhere in the file.
_SOURCE_DOC_WINDOW = 6

# Phrases that, when present in a code comment/docstring near the cited line,
# signal the reviewer's suggestion was intentionally NOT taken (#1329). The
# match is case-insensitive and deliberately conservative — a recurring re-open
# is suppressed only when the SOURCE itself documents the design decision.
_BY_DESIGN_PHRASES = (
    "by design",
    "intentional",
    "intentionally",
    "on purpose",
    "deliberate",
    "deliberately",
    "wont-fix",
    "won't fix",
    "wont fix",
    "do not change",
    "do not remove",
    "keep this",
    "noqa",
    "nosec",
    "type: ignore",
)


@dataclass(frozen=True)
class _ValidationContext:
    pr_number: int
    issue_number: int
    worktree_path: Path
    diff_text: str
    agent: str
    iteration: int
    state_dir: Path
    timeout: int


@dataclass
class _ReopenReview:
    comments: list[dict[str, Any]]
    pathless: list[dict[str, Any]]
    new_keys: set[str]


def _thread_key(*, path: Any, line: Any, body: Any) -> str:
    """Build a stable cross-round identity for a prior review thread (#1329).

    Keying on ``path:line`` plus a normalized body lets the loop recognize when
    the SAME finding is being re-opened round after round (the GraphQL
    ``thread_id`` changes every time the validator posts a fresh re-open thread,
    so it cannot identify recurrence on its own). Whitespace is collapsed and
    the body lower-cased so cosmetic differences between a reviewer's original
    text and the validator's echo do not defeat the match.

    Args:
        path: File path the comment targets (may be empty for PR-level).
        line: Line number (int or None).
        body: The comment / original_body text.

    Returns:
        A normalized ``"path:line:body"`` key string.

    """
    norm_body = re.sub(r"\s+", " ", str(body or "")).strip().lower()
    line_part = "" if line is None else str(line)
    return f"{path or ''!s}:{line_part}:{norm_body}"


def _source_documents_decision(worktree_path: Path, path: str, line: Any) -> bool:
    """Return True when the source at *path*:*line* documents a by-design choice.

    The "documented in source" heuristic for #1329: when a recurring re-open
    keeps flagging the same spot, read the current source in the worktree around
    the cited line and look for a code comment / docstring that justifies why
    the reviewer's suggestion was intentionally not taken. If such a marker sits
    within :data:`_SOURCE_DOC_WINDOW` lines of the cited line, the decision is
    treated as accepted-by-design and the comment is NOT re-added.

    Deliberately simple and conservative: only an explicit by-design phrase
    (see :data:`_BY_DESIGN_PHRASES`) counts, and only when it sits next to the
    cited code — a generic comment elsewhere in the file does not qualify. Any
    read error (missing file, bad path, non-int line) returns False so a genuine
    unaddressed finding is never silently suppressed.

    Args:
        worktree_path: Worktree root the cited path is relative to.
        path: File path the comment targets.
        line: 1-based line number the comment pointed at.

    Returns:
        True when a by-design marker is found at/near the cited line.

    """
    if not path or not isinstance(line, int) or line < 1:
        return False
    try:
        # Guard against path traversal / absolute paths escaping the worktree.
        source = (worktree_path / path).resolve()
        if not source.is_relative_to(worktree_path.resolve()):
            return False
        text = source.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    lines = text.splitlines()
    if not lines:
        return False
    lo = max(0, line - 1 - _SOURCE_DOC_WINDOW)
    hi = min(len(lines), line + _SOURCE_DOC_WINDOW)
    window = "\n".join(lines[lo:hi]).lower()
    return any(phrase in window for phrase in _BY_DESIGN_PHRASES)


def _run_validation_session(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prior_comments_json: str,
    diff_text: str,
    agent: str,
    review_agent: str,
    state_dir: Path,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the read-only validation sub-agent; return ``(unaddressed, wont_fix)``.

    Mirrors :func:`pr_reviewer.run_pr_review_analysis`'s invocation shape (a
    fresh read-only reviewer session, ``allowed_tools="Read,Glob,Grep"``). On any
    agent failure this returns ``([], [])`` — a failed validation must not block
    the loop, fabricate re-opens, or fabricate won't-fix dismissals.
    """
    prompt = get_review_validation_prompt(
        pr_number=pr_number,
        issue_number=issue_number,
        prior_comments_json=prior_comments_json,
        diff_text=diff_text,
    )
    log_file = log_file_path(state_dir, "review-validation", issue_number)
    try:
        if uses_direct_agent_runner(agent):
            result = run_agent_text(
                agent=agent,
                prompt=prompt,
                cwd=worktree_path,
                timeout=timeout,
                model=direct_agent_model(agent, "HEPH_REVIEWER_MODEL"),
                sandbox="read-only",
            )
            write_secure(log_file, result.stdout or "")
            parsed = parse_json_block(result.stdout or "")
        else:
            repo_slug = get_repo_slug(get_repo_root())
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=review_agent,
                prompt=prompt,
                model=reviewer_model(),
                cwd=worktree_path,
                timeout=timeout,
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            write_secure(log_file, stdout or "")
            # Fail loudly on an ``is_error: true`` envelope (e.g. a 429 cap)
            # instead of validating against the cap message as if it were a
            # real review (#1528 follow-up).
            raise_for_error_envelope(stdout or "")
            try:
                data = json.loads(stdout or "{}")
                response_text: str = data.get("result", stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = stdout or ""
            parsed = parse_json_block(response_text)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "PR #%s: review-validation session failed (%s); skipping re-open pass",
            pr_number,
            exc,
        )
        return [], []

    unaddressed = parsed.get("unaddressed", [])
    if not isinstance(unaddressed, list):
        unaddressed = []
    wont_fix = parsed.get("wont_fix", [])
    if not isinstance(wont_fix, list):
        wont_fix = []
    # Keep only well-formed dict entries.
    return (
        [u for u in unaddressed if isinstance(u, dict)],
        [w for w in wont_fix if isinstance(w, dict)],
    )


def _serialize_prior_threads(prior_threads: list[dict[str, Any]]) -> str:
    """Serialize prior thread fields for the read-only validation prompt."""
    return json.dumps(
        [
            {
                "thread_id": thread.get("id", ""),
                "path": thread.get("path", ""),
                "line": thread.get("line"),
                "body": thread.get("body", ""),
            }
            for thread in prior_threads
        ]
    )


def _run_validation_for_prior_threads(
    context: _ValidationContext,
    prior_threads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run validation using the compact context assembled by the orchestrator."""
    return _run_validation_session(
        pr_number=context.pr_number,
        issue_number=context.issue_number,
        worktree_path=context.worktree_path,
        prior_comments_json=_serialize_prior_threads(prior_threads),
        diff_text=context.diff_text,
        agent=context.agent,
        review_agent=reviewer_agent(AGENT_PR_REVIEWER, context.iteration),
        state_dir=context.state_dir,
        timeout=context.timeout,
    )


def _thread_ids(items: list[dict[str, Any]]) -> set[str]:
    """Return non-empty ``thread_id`` values from validator result items."""
    return {str(item.get("thread_id")) for item in items if item.get("thread_id")}


def _reopen_body(item: dict[str, Any], *, recurring: bool) -> str:
    """Build the body for a re-opened prior review finding."""
    detail = (item.get("detail") or "").strip() or "prior review comment not addressed"
    prefix = (
        "Still unaddressed (recurring; not documented as by-design)"
        if recurring
        else "prior review comment not addressed"
    )
    body = f"Re-opening: {prefix} — {detail}"
    original = (item.get("original_body") or "").strip()
    if original:
        quoted = "\n".join(f"> {line}" for line in original.splitlines())
        body = f"{body}\n\n{quoted}"
    return body


def _is_documented_recurring(
    context: _ValidationContext,
    *,
    path: str,
    line: Any,
    key: str,
    seen_keys: set[str],
) -> bool:
    """Return True when a recurring finding is documented as by-design in source."""
    if key not in seen_keys or not _source_documents_decision(context.worktree_path, path, line):
        return False
    logger.info(
        "PR %s R%s: recurring finding at %s:%s is documented as by-design "
        "in source — not re-adding (accepted design decision)",
        pr_ref(context.pr_number),
        context.iteration,
        path or "(PR-level)",
        line if isinstance(line, int) else "?",
    )
    return True


def _build_reopen_review(
    context: _ValidationContext,
    unaddressed: list[dict[str, Any]],
    seen_keys: set[str],
) -> _ReopenReview:
    """Build inline and PR-level re-open payloads for unaddressed findings."""
    comments: list[dict[str, Any]] = []
    pathless: list[dict[str, Any]] = []
    new_keys: set[str] = set()

    for item in unaddressed:
        path = item.get("path") or ""
        line = item.get("line")
        original = (item.get("original_body") or "").strip()
        key = _thread_key(path=path, line=line, body=original or item.get("detail"))

        if _is_documented_recurring(context, path=path, line=line, key=key, seen_keys=seen_keys):
            continue

        body = _reopen_body(item, recurring=key in seen_keys)
        new_keys.add(key)
        if not path:
            pathless.append({"body": body})
            continue

        comment: dict[str, Any] = {"path": path, "body": body, "side": "RIGHT"}
        if isinstance(line, int):
            comment["line"] = line
        comments.append(comment)

    return _ReopenReview(comments=comments, pathless=pathless, new_keys=new_keys)


def _reopen_summary(review: _ReopenReview) -> str:
    """Build the review summary for inline and pathless re-open findings."""
    summary_parts: list[str] = []
    if review.comments:
        summary_parts.append(
            f"Re-opening {len(review.comments)} prior review comment(s) "
            "the current diff does not address."
        )
    if review.pathless:
        bullets = "\n".join(f"- {item['body']}" for item in review.pathless)
        summary_parts.append(
            f"{len(review.pathless)} unaddressed PR-level review comment(s) remain:\n{bullets}"
        )
    return "\n\n".join(summary_parts)


def _post_reopen_review(context: _ValidationContext, review: _ReopenReview) -> list[str]:
    """Post the re-open review and return any inline thread IDs GitHub created."""
    thread_ids = gh_pr_review_post(
        pr_number=context.pr_number,
        comments=review.comments,
        summary=_reopen_summary(review),
        dry_run=False,
        # #1083: if the line already carries a bot comment, edit it in place
        # rather than stacking a duplicate re-open thread.
        dedupe_existing=True,
    )
    logger.info(
        "PR %s R%s: re-opened %s inline + %s PR-level unaddressed review comment(s)",
        pr_ref(context.pr_number),
        context.iteration,
        len(thread_ids),
        len(review.pathless),
    )
    return thread_ids


def validate_prior_comments_addressed(
    *,
    pr_number: int,
    issue_number: int,
    worktree_path: Path,
    prior_threads: list[dict[str, Any]],
    diff_text: str,
    agent: str,
    iteration: int,
    state_dir: Path,
    dry_run: bool = False,
    prior_reopened_keys: set[str] | None = None,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[list[str], bool, set[str]]:
    """Validate, resolve, and re-open prior bot review threads.

    Returns ``(reopened_thread_ids, is_clean, reopened_keys)``. ``is_clean`` is
    false only when the validator posts at least one inline or PR-level re-open.
    ``reopened_keys`` carries stable finding keys across rounds so documented
    recurring by-design findings can converge.
    """
    seen_keys: set[str] = set(prior_reopened_keys or set())
    if not prior_threads:
        return [], True, seen_keys
    if dry_run:
        logger.info("[DRY RUN] Would validate prior comments on PR #%s", pr_number)
        return [], True, seen_keys

    context = _ValidationContext(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        diff_text=diff_text,
        agent=agent,
        iteration=iteration,
        state_dir=state_dir,
        timeout=timeout,
    )
    unaddressed, wont_fix = _run_validation_for_prior_threads(context, prior_threads)
    unaddressed_ids = _thread_ids(unaddressed)
    wont_fix_ids = _thread_ids(wont_fix)

    _dismiss_wont_fix_prior_threads(prior_threads, wont_fix, wont_fix_ids)
    _resolve_addressed_prior_threads(prior_threads, unaddressed_ids | wont_fix_ids)

    if not unaddressed:
        return [], True, seen_keys

    review = _build_reopen_review(context, unaddressed, seen_keys)
    if not review.comments and not review.pathless:
        return [], True, seen_keys

    thread_ids = _post_reopen_review(context, review)
    return thread_ids, False, seen_keys | review.new_keys


def _dismiss_wont_fix_prior_threads(
    prior_threads: list[dict[str, Any]],
    wont_fix: list[dict[str, Any]],
    wont_fix_ids: set[str],
) -> list[str]:
    """Resolve each won't-fix thread with a durable ``WONT_FIX_MARKER`` reply.

    The reply records WHY the finding is intentional-by-design so the dismissal
    is auditable in the PR UI and recognizable on later runs (the fetch boundary
    skips any thread whose comments carry the marker, so it is never
    re-validated or re-opened). Only threads the loop itself owns are touched
    (#375); a missing/blank reason still resolves with the bare marker.

    Returns the list of dismissed thread IDs (for logging/tests).
    """
    reason_by_id = {
        str(item.get("thread_id")): str(item.get("reason") or "").strip()
        for item in wont_fix
        if isinstance(item, dict) and item.get("thread_id")
    }
    dismissed: list[str] = []
    for thread in prior_threads:
        thread_id = thread.get("id")
        if not thread_id or str(thread_id) not in wont_fix_ids:
            continue
        reason = reason_by_id.get(str(thread_id), "")
        reply = WONT_FIX_MARKER if not reason else f"{WONT_FIX_MARKER} — {reason}"
        try:
            gh_pr_resolve_thread(thread_id, reply_body=reply, dry_run=False)
            dismissed.append(thread_id)
            logger.info(
                "Dismissed won't-fix review thread %s (intentional design): %s",
                thread_id,
                reason or "(no reason given)",
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("Failed to dismiss won't-fix thread %s: %s", thread_id, exc)
    return dismissed


def _resolve_addressed_prior_threads(
    prior_threads: list[dict[str, Any]],
    unaddressed_ids: set[str],
) -> list[str]:
    """Resolve every prior thread the validator did not flag as unaddressed.

    A thread is considered addressed when its ``id`` is absent from
    *unaddressed_ids* (the set of thread_ids the validation sub-agent flagged).
    Matching on ``id`` rather than ``(path, line)`` means two threads on the same
    line resolve independently and a path mismatch cannot misfire (#1085). Only
    threads carrying a real ``id`` are resolved — the #375 own-threads guard,
    since ``prior_threads`` is always the set the loop itself posted/snapshotted.

    Returns the list of resolved thread IDs (useful for logging/tests).
    """
    resolved: list[str] = []
    for thread in prior_threads:
        thread_id = thread.get("id")
        if not thread_id:
            continue
        if str(thread_id) in unaddressed_ids:
            continue  # still open — re-opened above
        try:
            gh_pr_resolve_thread(thread_id, dry_run=False)
            resolved.append(thread_id)
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("Failed to resolve addressed thread %s: %s", thread_id, exc)
    return resolved
