# Audit Reviewer (`hephaestus-audit-prs`)

Batch audit review of **all open pull requests** using a coordinator + sub-agent
dispatch pattern.  One command reviews every open PR at once — the coordinator
agent fans out one sub-agent per PR, collects their findings, and posts inline
review comments back to each PR.

## Overview

The audit reviewer solves the problem of reviewing many open PRs efficiently.
Instead of running a separate agent session per PR, a single **coordinator**
agent parses the open PR list, dispatches one sub-agent per PR (via the Task
tool), aggregates results, and returns a structured JSON payload.  The Python
automation then posts each sub-agent's inline comments to the corresponding PR
and writes a persistent audit report.

```
┌─────────────┐
│ gh pr list  │  enumerate all open PRs
└──────┬──────┘
       ▼
┌─────────────────┐
│  Coordinator    │  dispatch one sub-agent per PR (batches of ≤10)
│  (Claude/Codex) │
└──────┬──────────┘
       ▼
┌─────────────────┐
│  Sub-agent #1   │  gh pr diff, gh pr view, analyse, return JSON
│  Sub-agent #2   │  gh pr diff, gh pr view, analyse, return JSON
│  ...            │
│  Sub-agent #N   │
└──────┬──────────┘
       ▼
┌─────────────────┐
│  Post reviews   │  gh api POST /repos/{owner}/{repo}/pulls/{n}/reviews
│  Write report   │  build/.audit/audit-report-{timestamp}.json
│  Print summary  │  per-PR verdict table (✓/✗, comment count, verdict)
└─────────────────┘
```

## CLI Usage

```bash
# Review all open PRs (up to 100)
hephaestus-audit-prs

# Review with a dry run (no posts, reports intent only)
hephaestus-audit-prs --dry-run

# Review specific PRs only
hephaestus-audit-prs --pr-numbers 595 596 597

# Cap the number of PRs fetched
hephaestus-audit-prs --limit 20

# Use Codex as the coordinator agent
hephaestus-audit-prs --agent codex

# Verbose logging
hephaestus-audit-prs -v

# Machine-readable output
hephaestus-audit-prs --json
```

### CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--agent {claude,codex}` | `claude` (auto-detected if omitted) | Coordinator agent provider |
| `--dry-run` | `false` | Show what would be done without posting reviews |
| `--limit N` | `100` | Maximum number of open PRs to fetch |
| `--pr-numbers N...` | (none) | Explicit PR numbers to review; overrides `--limit` and the open-PR list |
| `-v`, `--verbose` | `false` | Enable DEBUG-level logging |
| `--json` | `false` | Emit machine-readable JSON status on exit |

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success — all reviews posted without failures |
| `1` | Partial failure — at least one posting failed, or coordinator returned no results |
| `130` | Interrupted by user (Ctrl-C) |

## Programmatic API

The `AuditReviewer` class can be used directly from Python for scripting or
integration into larger automation workflows.

```python
from hephaestus.automation.audit_reviewer import AuditReviewer

# Review all open PRs (Claude, dry run)
reviewer = AuditReviewer(agent="claude", dry_run=True, limit=50)
exit_code = reviewer.run()  # 0 on success, 1 on failure

# Review specific PRs
reviewer = AuditReviewer(pr_numbers=[595, 596, 597])
exit_code = reviewer.run()
```

### `AuditReviewer` Constructor

```python
AuditReviewer(
    *,
    agent: str = "claude",
    dry_run: bool = False,
    limit: int = 100,
    pr_numbers: list[int] | None = None,
)
```

| Parameter | Default | Description |
|---|---|---|
| `agent` | `"claude"` | `"claude"` or `"codex"` — coordinator agent provider |
| `dry_run` | `False` | Log intent without invoking agents or posting reviews |
| `limit` | `100` | Maximum open PRs to fetch (ignored when `pr_numbers` is set) |
| `pr_numbers` | `None` | Explicit PR numbers to review; bypasses the open-PR list |

### `AuditReviewer.run()` → `int`

Executes the full 4-stage workflow:

1. **Enumerate PRs** — calls `gh_pr_list_open()` (or `_fetch_prs_by_number()` for
   explicit PRs).  Returns early with exit 0 if no open PRs are found.
2. **Run coordinator** — builds the audit prompt with the full PR list, invokes
   the selected agent, and parses the aggregated JSON results.
3. **Post inline reviews** — posts each sub-agent's `comments` array to the
   corresponding PR via `gh_pr_review_post()`.
4. **Write report + print summary** — persists a timestamped JSON report to
   `build/.audit/` and prints a human-readable summary to the logger.

Returns `0` on success, `1` if any posting failed or coordinator returned no results.

### Lower-Level Functions

For finer-grained control, the individual stages are exposed as standalone functions:

```python
from hephaestus.automation.audit_reviewer import (
    run_audit_coordinator,
    post_audit_results,
    write_audit_report,
    print_audit_summary,
    _parse_coordinator_results,
)
```

| Function | Purpose |
|---|---|
| `run_audit_coordinator(*, pr_list, worktree_path, agent, state_dir, dry_run)` | Build prompt, invoke agent, parse results |
| `post_audit_results(results, *, dry_run)` | Post per-PR inline reviews; returns `dict[int, bool]` |
| `write_audit_report(results, posted, state_dir)` | Write timestamped JSON report to `state_dir` |
| `print_audit_summary(results, posted)` | Print human-readable per-PR verdict table |
| `_parse_coordinator_results(text)` | Extract the last `\`\`\`json` block from coordinator output |

## Report Format

The audit report is written to `build/.audit/audit-report-{timestamp}.json`:

```json
{
  "timestamp": "20260605T120000Z",
  "total_prs": 5,
  "posted": 4,
  "failed": 1,
  "results": [
    {
      "pr_number": 595,
      "summary": "LGTM — clean refactor, all tests pass",
      "comment_count": 0,
      "posted": true
    },
    {
      "pr_number": 596,
      "summary": "Missing test for new error path in _derive_ci_status",
      "comment_count": 2,
      "posted": true
    },
    {
      "pr_number": 597,
      "summary": "Potential race in worktree cleanup — see inline comments",
      "comment_count": 3,
      "posted": false
    }
  ]
}
```

## Coordinator Prompt

The coordinator receives a JSON list of open PRs (fetched via `gh pr list --json`)
and is instructed to:

1. Parse the PR list.
2. Dispatch one sub-agent per PR in **batches of at most 10** (to avoid API
   rate limits).
3. Each sub-agent runs `gh pr diff {number}` and `gh pr view {number} --json`
   to fetch the diff and metadata.
4. Each sub-agent returns a JSON object with `comments` (inline review array)
   and `summary` (verdict text).
5. The coordinator collects all results and emits a single `\`\`\`json` block
   with a `results` array.

The prompt is built by `get_audit_coordinator_prompt()` in
[`hephaestus/automation/prompts/audit.py`](../hephaestus/automation/prompts/audit.py),
which applies untrusted-content fencing (random nonces) to all PR metadata —
the same safety contract used by the plan reviewer and PR reviewer prompts.

### Sub-Agent Guardrails

Every sub-agent prompt includes these critical guardrails:

- **No backgrounding**: "Do NOT background your work, do NOT exit early, and do
  NOT defer.  Complete the analysis synchronously."
- **Scope isolation**: "You own ONLY PR #N.  Do not read or touch any other PR's data."
- **Strict output format**: The exact JSON schema is specified with `\`\`\`json`
  fencing; `line` must be an integer that exists in the diff; `side` must be `RIGHT`.
- **LGTM shortcut**: If the PR has no issues, return `{"comments": [], "summary": "LGTM"}`.

## Session State

Each audit run creates a Claude/Codex session in `build/.audit/`:

| File | Content |
|---|---|
| `audit-coordinator.log` | Raw coordinator agent output (stdout + stderr) |
| `audit-report-{timestamp}.json` | Parsed results + posting outcomes |

The coordinator session uses a timestamp-derived `issue` number (rather than a
real GitHub issue number) so each audit run gets a **fresh** Claude session —
avoiding resumption of stale transcripts from prior audits.

## Integration with the Automation Pipeline

The audit reviewer complements the existing 3-stage pipeline
(plan → implement → drive-green) by providing **batch review** of *all* open PRs:

| Tool | Scope | Agent count |
|---|---|---|
| `hephaestus-review-prs` | One PR per invocation (read-only review) | 1 agent |
| `hephaestus-audit-prs` | **All** open PRs in one invocation | 1 coordinator + N sub-agents |

The audit reviewer is particularly useful for:

- **Pre-release sweeps** — review every open PR before a release cut.
- **Stale PR detection** — identify PRs that have been open too long without
  activity.
- **Cross-PR consistency** — spot conflicts or duplication across multiple
  in-flight PRs.

## Error Handling

| Failure mode | Behaviour |
|---|---|
| No open PRs | Logs info, exits 0 |
| Coordinator returns no JSON block | Logs warning, exits 1 |
| Coordinator process crashes (`CalledProcessError`) | Raises `RuntimeError` with stderr |
| Coordinator times out (`TimeoutExpired`) | Raises `RuntimeError` with "timed out" |
| Individual PR fetch fails | Logs warning, skips that PR |
| Individual PR posting fails | Logs warning, marks `posted: false` for that PR; exits 1 |
| Keyboard interrupt | Logs warning, exits 130 |

## Environment Variables

The coordinator session timeout is controlled by the same environment variables
used by the PR reviewer:

| Variable | Default | Description |
|---|---|---|
| `HEPH_PR_REVIEWER_CLAUDE_TIMEOUT` | `1800` | Coordinator session timeout in seconds (30 min) |

See [`hephaestus/automation/claude_timeouts.py`](../hephaestus/automation/claude_timeouts.py)
for the full timeout resolution logic.

## Examples

### Pre-Release Sweep

```bash
# Dry-run to see what would be reviewed
hephaestus-audit-prs --dry-run

# Review all open PRs before cutting a release
hephaestus-audit-prs --limit 100

# Check the report
cat build/.audit/audit-report-*.json | python -m json.tool
```

### Review Specific PRs

```bash
# Review only PRs 595, 596, and 597 using Codex
hephaestus-audit-prs --agent codex --pr-numbers 595 596 597 -v
```

### Scripted Integration

```python
import json
from pathlib import Path
from hephaestus.automation.audit_reviewer import AuditReviewer

# Review with a low limit for quick feedback
reviewer = AuditReviewer(agent="claude", dry_run=False, limit=5)
exit_code = reviewer.run()

# Read the report programmatically
reports = sorted(Path("build/.audit").glob("audit-report-*.json"))
if reports:
    report = json.loads(reports[-1].read_text())
    print(f"{report['posted']}/{report['total_prs']} reviews posted")
```
