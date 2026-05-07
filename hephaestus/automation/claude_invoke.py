"""Shared Claude CLI invocation with model fallback and review-verdict parsing.

Provides:
- ``Complexity`` enum used by call sites to pick a fallback model.
- ``call_claude`` — a single subprocess wrapper that tries sonnet first and
  falls back to opus (complex tasks) or haiku (simple tasks) on
  unavailability/overload errors.
- ``parse_review_verdict`` — parses the ``Grade:`` and ``Verdict:`` lines that
  the iteration-aware review prompts require.

This module replaces the duplicated ``_call_claude`` in ``planner.py`` and the
ad-hoc subprocess invocation in ``plan_reviewer.py``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from enum import Enum

from hephaestus.github.rate_limit import detect_rate_limit, wait_until

logger = logging.getLogger(__name__)


class Complexity(str, Enum):
    """Task complexity for choosing the fallback model when sonnet is unavailable."""

    COMPLEX = "complex"  # plan, implement, review — falls back to opus
    SIMPLE = "simple"  # advise, learn, follow-up — falls back to haiku


_PRIMARY_MODEL = "sonnet"
_FALLBACK_MODELS: dict[Complexity, str] = {
    Complexity.COMPLEX: "opus",
    Complexity.SIMPLE: "haiku",
}

# stderr substrings that indicate the requested model itself is unavailable
# (as opposed to a transient rate-limit which is handled by detect_rate_limit).
_MODEL_UNAVAILABLE_PATTERNS = (
    "model_unavailable",
    "model not available",
    "overloaded",
    "model is overloaded",
    "service unavailable",
    "503",
)


class ClaudeUnavailableError(RuntimeError):
    """Raised when Claude is unavailable for the requested model.

    Distinct from generic failures so the fallback chain in :func:`call_claude`
    can decide whether to escalate to the next model.
    """


def _is_model_unavailable(stderr: str) -> bool:
    """Return True if stderr indicates the requested model is unavailable."""
    lowered = stderr.lower()
    return any(pat in lowered for pat in _MODEL_UNAVAILABLE_PATTERNS)


def _invoke_once(
    prompt: str,
    *,
    model: str,
    timeout: int,
    extra_args: list[str] | None,
    system_prompt_file: str | None,
    use_stdin: bool,
) -> str:
    """Single subprocess call to ``claude`` with one model.

    Raises:
        ClaudeUnavailableError: model unavailable / overloaded / sustained rate limit.
        RuntimeError: other failures (Claude returned non-zero or empty output).

    """
    cmd: list[str] = ["claude", "--print"]
    if not use_stdin:
        cmd.append(prompt)
    cmd.extend(["--output-format", "text", "--model", model])

    if system_prompt_file:
        cmd.extend(["--system-prompt", system_prompt_file])
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env["CLAUDECODE"] = ""  # avoid nested-session guard

    try:
        result = subprocess.run(
            cmd,
            input=prompt if use_stdin else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
            stdin=None if use_stdin else subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""

        # Sustained rate-limit: wait once, then signal unavailable so the
        # caller can decide whether to fall back to a different model.
        reset_epoch = detect_rate_limit(stderr)
        if reset_epoch is not None:
            if reset_epoch > 0:
                wait_until(reset_epoch)
            else:
                time.sleep(5)
            raise ClaudeUnavailableError(f"Rate limited on model {model}: {stderr[:200]}") from e

        if _is_model_unavailable(stderr):
            raise ClaudeUnavailableError(f"Model {model} unavailable: {stderr[:200]}") from e

        raise RuntimeError(f"Claude failed (model={model}): {stderr[:500]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Claude timed out after {timeout}s (model={model})") from e

    response = (result.stdout or "").strip()
    if not response:
        raise RuntimeError(f"Claude returned empty response (model={model})")
    return response


def call_claude(
    prompt: str,
    *,
    complexity: Complexity,
    timeout: int = 300,
    extra_args: list[str] | None = None,
    system_prompt_file: str | None = None,
    use_stdin: bool = False,
) -> str:
    """Call Claude CLI with model fallback.

    Tries ``sonnet`` first. On :class:`ClaudeUnavailableError`, falls back to
    ``opus`` (for ``COMPLEX`` tasks) or ``haiku`` (for ``SIMPLE`` tasks).
    The env var ``HEPHAESTUS_FORCE_MODEL`` short-circuits the chain — useful
    in tests and for manual overrides.

    Args:
        prompt: Prompt sent to Claude.
        complexity: Task complexity, drives fallback model choice.
        timeout: Per-attempt timeout in seconds.
        extra_args: Additional CLI args (e.g. ``--permission-mode``).
        system_prompt_file: Optional path to system-prompt file.
        use_stdin: Pass the prompt via stdin instead of as a CLI argument.
            Required for prompts large enough to exceed argv limits, or for
            prompts containing characters that confuse argv parsing.

    Returns:
        Claude's response text.

    Raises:
        RuntimeError: All model fallbacks exhausted, or a non-availability
            failure occurred.

    """
    forced = os.environ.get("HEPHAESTUS_FORCE_MODEL")
    models = [forced] if forced else [_PRIMARY_MODEL, _FALLBACK_MODELS[complexity]]

    last_unavailable: ClaudeUnavailableError | None = None
    for model in models:
        try:
            return _invoke_once(
                prompt,
                model=model,
                timeout=timeout,
                extra_args=extra_args,
                system_prompt_file=system_prompt_file,
                use_stdin=use_stdin,
            )
        except ClaudeUnavailableError as e:
            last_unavailable = e
            logger.warning(f"Model {model} unavailable; falling back ({complexity.value} task)")
            continue

    raise RuntimeError(
        f"All Claude model fallbacks exhausted for {complexity.value} task: {last_unavailable}"
    )


# ---------------------------------------------------------------------------
# Review verdict parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewVerdict:
    """Parsed verdict from a review response.

    Attributes:
        grade: Letter grade extracted from ``Grade: <X>`` line. ``None`` if absent.
        verdict: One of ``"GO"``, ``"NOGO"``, or ``"AMBIGUOUS"``.
        raw: Full review text (kept for downstream prompts and logs).

    """

    grade: str | None
    verdict: str
    raw: str

    @property
    def is_go(self) -> bool:
        """True only on an unambiguous GO."""
        return self.verdict == "GO"


_GRADE_RE = re.compile(
    r"^\s*\**\s*Grade\s*:\s*\**\s*([A-F][+-]?)(?![A-Za-z])",
    re.MULTILINE | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(GO|NO[\s-]?GO)\b", re.MULTILINE | re.IGNORECASE
)


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Extract grade and Go/NoGo verdict from a review response.

    Looks for lines like:
        Grade: B+
        Verdict: GO     (or NOGO, NO-GO, NO GO)

    A response missing or contradicting these markers is treated as
    AMBIGUOUS — which the loop treats as NoGo (continue iterating).

    Args:
        text: The full review text from Claude.

    Returns:
        :class:`ReviewVerdict`.

    """
    grade_match = _GRADE_RE.search(text)
    grade = grade_match.group(1).upper() if grade_match else None

    verdict_match = _VERDICT_RE.search(text)
    if verdict_match:
        raw_verdict = re.sub(r"[\s-]", "", verdict_match.group(1).upper())
        verdict = "GO" if raw_verdict == "GO" else "NOGO"
    else:
        verdict = "AMBIGUOUS"

    return ReviewVerdict(grade=grade, verdict=verdict, raw=text)
