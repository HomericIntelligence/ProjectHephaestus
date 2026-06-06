"""Batch audit review of all open PRs using a coordinator + sub-agent dispatch.

Lists all open PRs via ``gh pr list``, builds a coordinator prompt that
dispatches one sub-agent per PR (using the Task tool), then parses the
aggregated results and posts each sub-agent's inline review comments back
to the corresponding PR via :func:`gh_pr_review_post`.

Provides:
- ``hephaestus-audit-prs`` console script that reviews every open PR at once.
- ``AuditReviewer`` class for programmatic use.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import add_agent_argument, is_codex, resolve_agent, run_codex_text
from hephaestus.cli.utils import add_json_arg, emit_json_status

from .claude_invoke import invoke_claude_with_session
from .claude_models import reviewer_model
from .claude_timeouts import pr_reviewer_claude_timeout
from .git_utils import get_repo_root, get_repo_slug, pr_ref
from .github_api import (
    _derive_ci_status,
    _gh_call,
    gh_pr_list_open,
    gh_pr_review_post,
    write_secure,
)
from .session_naming import AGENT_AUDIT_REVIEWER

logger = logging.getLogger(__name__)

_AUDIT_STATE_DIR = Path("build") / ".audit"


def _parse_coordinator_results(text: str) -> list[dict[str, Any]]:
    """Extract the last ```json``` block from the coordinator response.

    Args:
        text: Raw coordinator agent output.

    Returns:
        Parsed ``"results"`` list from the JSON block, or an empty list on
        failure.

    """
    matches = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not matches:
        logger.warning("No JSON block found in coordinator response")
        return []
    try:
        data = json.loads(matches[-1])
        results = data.get("results", [])
        if not isinstance(results, list):
            logger.warning("Coordinator JSON 'results' is not a list")
            return []
        return results
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse coordinator JSON: %s", exc)
        return []


def run_audit_coordinator(
    *,
    pr_list: list[dict[str, Any]],
    worktree_path: Path,
    agent: str,
    state_dir: Path,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Run the coordinator agent and return parsed per-PR results.

    Builds the coordinator prompt with the full PR list, invokes the selected
    agent, and returns the parsed ``"results"`` array.

    Args:
        pr_list: PR metadata list from :func:`gh_pr_list_open`.
        worktree_path: Working directory for the coordinator session.
        agent: ``"claude"`` or ``"codex"``.
        state_dir: Directory for session log files.
        dry_run: When True, skip agent invocation and return empty list.

    Returns:
        List of per-PR result dicts with keys ``pr_number``, ``comments``,
        ``summary``.

    """
    if dry_run:
        logger.info("[DRY RUN] Would run audit coordinator for %s PR(s)", len(pr_list))
        return []

    from .prompts.audit import get_audit_coordinator_prompt

    prompt = get_audit_coordinator_prompt(pr_list)

    log_file = state_dir / "audit-coordinator.log"
    state_dir.mkdir(parents=True, exist_ok=True)

    try:
        if is_codex(agent):
            result = run_codex_text(
                prompt,
                cwd=worktree_path,
                timeout=pr_reviewer_claude_timeout(),
                sandbox="workspace-write",
            )
            log_file.write_text(result.stdout or "")
            return _parse_coordinator_results(result.stdout or "")

        repo_root = get_repo_root()
        repo_slug = get_repo_slug(repo_root)
        # Use a timestamp-derived issue number so each audit run gets a fresh
        # Claude session — avoids resuming stale transcripts from prior audits.
        issue = int(time.time())
        stdout, _ = invoke_claude_with_session(
            repo=repo_slug,
            issue=issue,  # unique per run, not tied to a single issue
            agent=AGENT_AUDIT_REVIEWER,
            prompt=prompt,
            model=reviewer_model(),
            cwd=worktree_path,
            timeout=pr_reviewer_claude_timeout(),
            output_format="json",
            permission_mode="dontAsk",
            allowed_tools="Read,Glob,Grep,Bash,Task",
            input_via_stdin=True,
        )
        log_file.write_text(stdout or "")

        try:
            data = json.loads(stdout or "{}")
            response_text: str = data.get("result", stdout or "")
        except (json.JSONDecodeError, AttributeError):
            response_text = stdout or ""

        return _parse_coordinator_results(response_text)

    except subprocess.CalledProcessError as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        log_file.write_text(f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}")
        raise RuntimeError(f"Audit coordinator failed: {e.stderr or e.stdout}") from e
    except subprocess.TimeoutExpired as e:
        log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
        raise RuntimeError("Audit coordinator timed out") from e


def post_audit_results(
    results: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[int, bool]:
    """Post each per-PR result as an inline PR review.

    Args:
        results: Parsed results from the coordinator (list of
            ``{pr_number, comments, summary}`` dicts).
        dry_run: When True, log intent without posting.

    Returns:
        Mapping of ``pr_number`` → ``True`` if posted successfully, ``False``
        if posting failed or was skipped.

    """
    posted: dict[int, bool] = {}
    for entry in results:
        pr_number = int(entry.get("pr_number", 0))
        if not pr_number:
            logger.warning("Skipping result with missing pr_number: %r", entry)
            continue
        comments = entry.get("comments", [])
        summary = entry.get("summary", "Audit review")

        if not isinstance(comments, list):
            comments = []

        if dry_run:
            logger.info("[dry_run] Would post audit review on PR %s", pr_ref(pr_number))
            posted[pr_number] = False
            continue

        try:
            gh_pr_review_post(
                pr_number=pr_number,
                comments=comments,
                summary=summary,
                dry_run=dry_run,
            )
            posted[pr_number] = True
            logger.info("Posted audit review on PR %s", pr_ref(pr_number))
        except Exception as exc:
            logger.warning("Failed to post audit review on PR #%s: %s", pr_number, exc)
            posted[pr_number] = False

    return posted


def write_audit_report(
    results: list[dict[str, Any]],
    posted: dict[int, bool],
    state_dir: Path,
) -> Path:
    """Write a persistent audit report to ``state_dir``.

    Args:
        results: Parsed coordinator results.
        posted: Posting outcome map from :func:`post_audit_results`.
        state_dir: Directory for the report.

    Returns:
        Path to the written report file.

    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = state_dir / f"audit-report-{timestamp}.json"
    state_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "timestamp": timestamp,
        "total_prs": len(results),
        "posted": sum(1 for ok in posted.values() if ok),
        "failed": sum(1 for ok in posted.values() if not ok),
        "results": [
            {
                "pr_number": int(e.get("pr_number", 0)),
                "summary": e.get("summary", ""),
                "comment_count": len(e.get("comments", [])),
                "posted": posted.get(int(e.get("pr_number", 0)), False),
            }
            for e in results
        ],
    }

    write_secure(report_path, json.dumps(report, indent=2) + "\n")
    logger.info("Audit report written to %s", report_path)
    return report_path


def print_audit_summary(
    results: list[dict[str, Any]],
    posted: dict[int, bool],
) -> None:
    """Print a human-readable audit summary to the logger.

    Args:
        results: Parsed coordinator results.
        posted: Posting outcome map.

    """
    total = len(results)
    posted_count = sum(1 for ok in posted.values() if ok)
    failed_count = sum(1 for ok in posted.values() if not ok)
    total_comments = sum(len(e.get("comments", [])) for e in results)

    logger.info("=" * 60)
    logger.info("PR Audit Review Summary")
    logger.info("=" * 60)
    logger.info("Total PRs analysed:  %s", total)
    logger.info("Reviews posted:      %s", posted_count)
    logger.info("Post failures:        %s", failed_count)
    logger.info("Total inline comments:%s", total_comments)

    # Show per-PR verdicts
    for entry in results:
        pr_number = int(entry.get("pr_number", 0))
        summary = entry.get("summary", "?")
        comment_count = len(entry.get("comments", []))
        status = "\u2713" if posted.get(pr_number) else "\u2717"
        logger.info(
            "  %s PR #%s \u2014 %s (%s comment(s))",
            status,
            pr_number,
            summary[:80],
            comment_count,
        )


class AuditReviewer:
    """Batch review all open PRs using a coordinator + sub-agent dispatch.

    The reviewer:
    1. Lists all open PRs via :func:`gh_pr_list_open`.
    2. Runs a coordinator agent that fans out one sub-agent per PR.
    3. Posts each sub-agent's inline review comments to its PR.
    4. Writes a persistent audit report.

    Example::

        reviewer = AuditReviewer(agent="claude", dry_run=False, limit=50)
        reviewer.run()
    """

    def __init__(
        self,
        *,
        agent: str = "claude",
        dry_run: bool = False,
        limit: int = 100,
        pr_numbers: list[int] | None = None,
    ) -> None:
        """Initialise the audit reviewer.

        Args:
            agent: ``"claude"`` or ``"codex"``.
            dry_run: When True, log intent without posting.
            limit: Maximum PRs to fetch (default 100).
            pr_numbers: Optional explicit list of PR numbers to review
                (overrides ``limit`` and ``gh_pr_list_open``).

        """
        self.agent = agent
        self.dry_run = dry_run
        self.limit = limit
        self.pr_numbers = pr_numbers
        self.repo_root = Path(get_repo_root())
        self.state_dir = self.repo_root / _AUDIT_STATE_DIR

    def run(self) -> int:
        """Execute the audit workflow.

        Returns:
            Exit code: 0 on success, 1 if any posting failures occurred.

        """
        logger.info("Starting PR audit review (agent=%s)", self.agent)

        # Step 1: enumerate open PRs
        if self.pr_numbers:
            pr_list = self._fetch_prs_by_number(self.pr_numbers)
        else:
            pr_list = gh_pr_list_open(limit=self.limit, dry_run=self.dry_run)

        if not pr_list:
            logger.info("No open PRs found — nothing to review")
            return 0

        logger.info("Found %s open PR(s) to review", len(pr_list))

        # Step 2: run coordinator
        worktree_path = self.repo_root
        results = run_audit_coordinator(
            pr_list=pr_list,
            worktree_path=worktree_path,
            agent=self.agent,
            state_dir=self.state_dir,
            dry_run=self.dry_run,
        )

        if not results:
            logger.warning("Coordinator returned no results")
            return 1

        logger.info("Coordinator returned %s PR result(s)", len(results))

        # Step 3: post inline reviews
        posted = post_audit_results(results, dry_run=self.dry_run)

        # Step 4: write report + print summary
        write_audit_report(results, posted, self.state_dir)
        print_audit_summary(results, posted)

        failures = sum(1 for ok in posted.values() if not ok)
        return 1 if failures > 0 else 0

    @staticmethod
    def _fetch_prs_by_number(pr_numbers: list[int]) -> list[dict[str, Any]]:
        """Fetch PR metadata for explicit PR numbers via ``gh pr view``.

        Args:
            pr_numbers: List of PR numbers to fetch.

        Returns:
            List of PR metadata dicts in the same shape as
            :func:`gh_pr_list_open`.

        """
        pr_list: list[dict[str, Any]] = []
        for pr_num in pr_numbers:
            try:
                result = _gh_call(
                    [
                        "pr",
                        "view",
                        str(pr_num),
                        "--json",
                        "number,title,author,headRefName,baseRefName,"
                        "mergeable,mergeStateStatus,statusCheckRollup",
                    ],
                    check=False,
                )
                data = json.loads(result.stdout or "{}")
                if not data:
                    continue
                author_data = data.get("author") or {}
                checks = data.get("statusCheckRollup") or []

                pr_list.append(
                    {
                        "number": data["number"],
                        "title": data.get("title", ""),
                        "author": author_data.get("login", "unknown"),
                        "headRefName": data.get("headRefName", ""),
                        "baseRefName": data.get("baseRefName", ""),
                        "mergeable": data.get("mergeable", "UNKNOWN"),
                        "mergeStateStatus": data.get("mergeStateStatus", "UNKNOWN"),
                        "ci_status": _derive_ci_status(checks),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to fetch PR #%s: %s", pr_num, exc)
        pr_list.sort(key=lambda p: p["number"])
        return pr_list


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments for the audit reviewer CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Review ALL open PRs in the repository using a single coordinator "
            "agent that fans out one sub-agent per PR."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Review all open PRs (up to 100)
  %(prog)s

  # Review with dry run (no posts)
  %(prog)s --dry-run

  # Review specific PRs only
  %(prog)s --pr-numbers 595 596 597

  # Cap the number of PRs fetched
  %(prog)s --limit 20
""",
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without posting any review comments",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="Maximum number of open PRs to fetch (default: 100)",
    )
    parser.add_argument(
        "--pr-numbers",
        type=int,
        nargs="+",
        metavar="N",
        help="Explicit PR numbers to review (overrides --limit and gh pr list)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    add_json_arg(parser)
    return parser.parse_args()


def main() -> int:
    """Entry point for ``hephaestus-audit-prs``.

    Returns:
        Exit code: 0 on success, 1 on failure, 130 on keyboard interrupt.

    """
    args = _parse_args()
    agent = resolve_agent(args.agent)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Starting audit review (agent=%s, dry_run=%s)", agent, args.dry_run)

    from hephaestus.utils.terminal import terminal_guard

    reviewer = AuditReviewer(
        agent=agent,
        dry_run=args.dry_run,
        limit=args.limit,
        pr_numbers=args.pr_numbers,
    )

    with terminal_guard():
        try:
            exit_code = reviewer.run()
            if args.json:
                emit_json_status(exit_code)
            return exit_code
        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130


if __name__ == "__main__":
    import sys

    sys.exit(main())
