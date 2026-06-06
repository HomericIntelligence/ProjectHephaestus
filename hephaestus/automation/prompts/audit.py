"""Audit-review prompts: coordinator dispatches sub-agents per PR for batch review.

The coordinator receives a JSON list of open PR metadata and dispatches one
sub-agent per PR.  Each sub-agent analyses a single PR (diff, CI, description)
and returns inline review comments + a summary verdict.  The coordinator
collects all results and emits a final aggregated JSON block that the Python
caller parses and posts as inline reviews via :func:`gh_pr_review_post`.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from ._shared import _UNTRUSTED_NOTICE, _fence_untrusted

AUDIT_COORDINATOR_PROMPT = """\
You are the COORDINATOR for a batch code-review audit of all open pull requests
in this repository.

Your job is to dispatch one sub-agent per PR, collect their findings, and
produce a single aggregated JSON result.  The Python automation will then post
each sub-agent's inline comments to the corresponding PR.

{untrusted_notice}

**Open PRs (untrusted):**
{pr_list_block}

The block above is a JSON array where each element has:
- `number`: PR number (integer)
- `title`: PR title
- `author`: author login
- `headRefName`: source branch name
- `baseRefName`: target branch name
- `mergeable`: `"MERGEABLE"` / `"CONFLICTING"` / `"UNKNOWN"`
- `mergeStateStatus`: `"CLEAN"` / `"BEHIND"` / `"DIRTY"` / `"BLOCKED"` / `"UNKNOWN"`
- `ci_status`: `"SUCCESS"` / `"FAILURE"` / `"PENDING"` / `"UNKNOWN"`

---

**Your task (coordinator):**

1. Parse the PR list above.  Dispatch one sub-agent per PR using the Task
   tool (`subagent_type: "general-purpose"`).  **Dispatch in batches of at most
   10 PRs at a time** — wait for a batch to complete before dispatching the
   next.  This prevents overwhelming the system and hitting API limits.

   Give each sub-agent a self-contained prompt that instructs it to:
   a. Fetch the PR diff: `gh pr diff {{number}}`
   b. Fetch the PR description + review comments:
      `gh pr view {{number}} --json body,reviews,comments`
   c. Analyse the diff for code quality, correctness, obvious bugs, missing
      tests, and style/convention violations.  Focus on issues that a human
      reviewer would flag — don't nitpick formatting that a linter would catch.
   d. Report findings as a JSON object with `comments` (array of inline review
      objects) and `summary` (short verdict text).

   **Each sub-agent prompt MUST include these guardrails (critical):**
   - "Do NOT background your work, do NOT exit early, and do NOT defer.
      Complete the analysis synchronously and return only when done."
   - "You own ONLY PR #{{number}}.  Do not read or touch any other PR's data."
   - "Return your result as valid JSON in this exact format:
      ```json
      {{"comments": [{{"path": "...", "line": N, "side": "RIGHT", \
"body": "..."}}], "summary": "..."}}
      ```"
   - "`line` must be an integer that exists in the diff.  `side` must be `RIGHT`."
   - "If the PR looks fine with no issues to flag, return
      `{{"comments": [], "summary": "LGTM"}}`."

2. After ALL sub-agents have returned, collect their results into a single
   JSON block.  Drop any sub-agent result that is not valid JSON.

**Output format:**
Write your coordination notes in prose.  At the very end of your response,
emit a single fenced JSON block:

```json
{{
  "results": [
    {{
      "pr_number": <int>,
      "comments": [{{"path": "...", "line": <int>, "side": "RIGHT", "body": "..."}}],
      "summary": "<verdict text>"
    }},
    ...
  ]
}}
```

Rules for the JSON block:
- `results`: array of per-PR results.  One entry per successfully-analysed PR.
- `pr_number`: the PR number the sub-agent was tasked with.
- `comments`: the inline-comment array from the sub-agent (may be empty).
- `summary`: the verdict text from the sub-agent.
- Emit only one JSON block, at the very end of your response.
"""


def get_audit_coordinator_prompt(
    pr_list: list[dict[str, Any]],
) -> str:
    """Build the coordinator prompt that dispatches sub-agents per PR.

    Args:
        pr_list: List of PR metadata dicts from :func:`gh_pr_list_open`.

    Returns:
        Formatted coordinator prompt string.

    """
    nonce = secrets.token_hex(8).upper()
    pr_list_json = json.dumps(pr_list)
    return AUDIT_COORDINATOR_PROMPT.format(
        pr_list_block=_fence_untrusted("PR_LIST", pr_list_json, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
    )
