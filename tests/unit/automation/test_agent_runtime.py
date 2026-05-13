"""Tests for provider-neutral agent runtime helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hephaestus.automation import agent_runtime


def test_parse_codex_json_events_extracts_session_id_and_messages() -> None:
    """Codex JSONL exposes the resumable UUID in the session_meta event."""
    text = "\n".join(
        [
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}',
            '{"type":"agent_message","message":"first"}',
            '{"type":"agent_message","message":"second"}',
        ]
    )

    session_id, output = agent_runtime._parse_codex_json_events(text)

    assert session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert output == "first\nsecond"


def test_parse_codex_json_events_extracts_nested_agent_message() -> None:
    """Current Codex JSONL nests user-visible messages inside event_msg payloads."""
    text = "\n".join(
        [
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}',
            '{"type":"event_msg","payload":{"type":"agent_message","message":"nested"}}',
        ]
    )

    session_id, output = agent_runtime._parse_codex_json_events(text)

    assert session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert output == "nested"


def test_run_codex_session_returns_session_id_and_last_message(tmp_path: Path) -> None:
    """The runtime should prefer --output-last-message and preserve session id."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("final answer", encoding="utf-8")
        stdout = (
            '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
            '{"type":"agent_message","message":"fallback"}\n'
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("hephaestus.automation.agent_runtime.codex_approval_args", return_value=[]):
        with patch("subprocess.run", side_effect=fake_run):
            result = agent_runtime.run_codex_session(
                "prompt",
                cwd=tmp_path,
                timeout=30,
                sandbox="workspace-write",
            )

    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
    assert result.stdout == "final answer"


def test_resume_codex_session_uses_exec_resume(tmp_path: Path) -> None:
    """Codex feedback loops must resume the captured non-interactive session."""
    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text("resumed", encoding="utf-8")
        stdout = '{"type":"session_meta","payload":{"id":"019e1e57-7652-7892-b1ca-c31c93d4b160"}}\n'
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = agent_runtime.resume_codex_session(
            "019e1e57-7652-7892-b1ca-c31c93d4b160",
            "feedback",
            cwd=tmp_path,
            timeout=30,
        )

    assert captured_cmd[:4] == [
        "codex",
        "exec",
        "resume",
        "019e1e57-7652-7892-b1ca-c31c93d4b160",
    ]
    assert result.stdout == "resumed"
    assert result.session_id == "019e1e57-7652-7892-b1ca-c31c93d4b160"
