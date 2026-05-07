"""Tests for the shared Claude invocation helper."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import (
    ClaudeUnavailableError,
    Complexity,
    ReviewVerdict,
    _invoke_once,
    call_claude,
    parse_review_verdict,
)


class TestInvokeOnce:
    """Tests for _invoke_once — single subprocess call wrapping `claude`."""

    def test_success_returns_stripped_stdout(self) -> None:
        with patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="  hello world  \n", returncode=0)
            out = _invoke_once(
                "prompt",
                model="sonnet",
                timeout=60,
                extra_args=None,
                system_prompt_file=None,
                use_stdin=False,
            )
        assert out == "hello world"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_empty_response_raises_runtime_error(self) -> None:
        with patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="   ", returncode=0)
            with pytest.raises(RuntimeError, match="empty response"):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=60,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )

    def test_timeout_raises_runtime_error(self) -> None:
        with patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 30)
            with pytest.raises(RuntimeError, match="timed out"):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=30,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )

    def test_model_unavailable_stderr_raises_unavailable_error(self) -> None:
        with patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "claude", stderr="Error: model_unavailable for sonnet"
            )
            with pytest.raises(ClaudeUnavailableError, match="unavailable"):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=60,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )

    def test_overloaded_stderr_raises_unavailable_error(self) -> None:
        with patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "claude", stderr="503 model is overloaded, retry later"
            )
            with pytest.raises(ClaudeUnavailableError):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=60,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )

    def test_rate_limit_with_reset_raises_unavailable(self) -> None:
        with (
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
            patch("hephaestus.automation.claude_invoke.detect_rate_limit", return_value=0),
            patch("hephaestus.automation.claude_invoke.time.sleep"),
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "claude", stderr="Limit reached. resets 2pm (UTC)"
            )
            with pytest.raises(ClaudeUnavailableError, match="Rate limited"):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=60,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )

    def test_other_failure_raises_runtime_error(self) -> None:
        with (
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
            patch("hephaestus.automation.claude_invoke.detect_rate_limit", return_value=None),
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                2, "claude", stderr="some unexpected internal error"
            )
            with pytest.raises(RuntimeError, match="Claude failed"):
                _invoke_once(
                    "p",
                    model="sonnet",
                    timeout=60,
                    extra_args=None,
                    system_prompt_file=None,
                    use_stdin=False,
                )


class TestCallClaudeFallback:
    """Fallback chain: sonnet → opus (COMPLEX) or haiku (SIMPLE)."""

    def test_complex_falls_back_to_opus(self) -> None:
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.side_effect = [
                ClaudeUnavailableError("sonnet down"),
                "opus result",
            ]
            out = call_claude("p", complexity=Complexity.COMPLEX)
        assert out == "opus result"
        assert mock_invoke.call_count == 2
        # Second call must be opus
        second_kwargs = mock_invoke.call_args_list[1].kwargs
        assert second_kwargs["model"] == "opus"

    def test_simple_falls_back_to_haiku(self) -> None:
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.side_effect = [
                ClaudeUnavailableError("sonnet down"),
                "haiku result",
            ]
            out = call_claude("p", complexity=Complexity.SIMPLE)
        assert out == "haiku result"
        second_kwargs = mock_invoke.call_args_list[1].kwargs
        assert second_kwargs["model"] == "haiku"

    def test_sonnet_succeeds_no_fallback(self) -> None:
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.return_value = "sonnet result"
            out = call_claude("p", complexity=Complexity.COMPLEX)
        assert out == "sonnet result"
        assert mock_invoke.call_count == 1
        assert mock_invoke.call_args.kwargs["model"] == "sonnet"

    def test_all_fail_raises_runtime_error(self) -> None:
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.side_effect = [
                ClaudeUnavailableError("sonnet down"),
                ClaudeUnavailableError("opus down"),
            ]
            with pytest.raises(RuntimeError, match="exhausted"):
                call_claude("p", complexity=Complexity.COMPLEX)

    def test_force_model_env_var_skips_fallback(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HEPHAESTUS_FORCE_MODEL", "opus")
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.return_value = "forced"
            call_claude("p", complexity=Complexity.SIMPLE)
        assert mock_invoke.call_count == 1
        assert mock_invoke.call_args.kwargs["model"] == "opus"

    def test_non_unavailable_error_does_not_fall_back(self) -> None:
        """A generic RuntimeError should NOT trigger fallback — only model-unavailable does."""
        with patch("hephaestus.automation.claude_invoke._invoke_once") as mock_invoke:
            mock_invoke.side_effect = RuntimeError("generic failure")
            with pytest.raises(RuntimeError, match="generic failure"):
                call_claude("p", complexity=Complexity.COMPLEX)
        assert mock_invoke.call_count == 1


class TestParseReviewVerdict:
    """Tests for parsing Grade and Verdict lines from review output."""

    def test_unambiguous_go(self) -> None:
        v = parse_review_verdict("blah blah\nGrade: A\nVerdict: GO\n")
        assert v == ReviewVerdict(grade="A", verdict="GO", raw=v.raw)
        assert v.is_go is True

    def test_unambiguous_nogo(self) -> None:
        v = parse_review_verdict("Grade: D+\nVerdict: NOGO")
        assert v.grade == "D+"
        assert v.verdict == "NOGO"
        assert v.is_go is False

    def test_no_go_with_dash(self) -> None:
        v = parse_review_verdict("Grade: F\nVerdict: NO-GO")
        assert v.verdict == "NOGO"

    def test_no_go_with_space(self) -> None:
        v = parse_review_verdict("Grade: F\nVerdict: NO GO")
        assert v.verdict == "NOGO"

    def test_missing_verdict_is_ambiguous(self) -> None:
        v = parse_review_verdict("Grade: B")
        assert v.verdict == "AMBIGUOUS"
        assert v.is_go is False

    def test_missing_grade_only_verdict(self) -> None:
        v = parse_review_verdict("Verdict: GO")
        assert v.grade is None
        assert v.verdict == "GO"
        assert v.is_go is True

    def test_with_bold_markers(self) -> None:
        v = parse_review_verdict("**Grade:** B+\n**Verdict:** GO")
        assert v.grade == "B+"
        assert v.verdict == "GO"

    def test_case_insensitive(self) -> None:
        v = parse_review_verdict("grade: c-\nverdict: nogo")
        assert v.grade == "C-"
        assert v.verdict == "NOGO"
