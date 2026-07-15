"""Tests for railmux.transcript — Codex JSONL rendering."""
from __future__ import annotations

import json
import io

import pytest

from railmux.transcript import format_transcript


def _make_codex_lines(
    session_id: str = "019f4509-2908-7a70-a36b-9e1044cb7a88",
    cwd: str = "/home/test/project",
    cli_version: str = "0.98.0",
    model_provider: str = "deepseek",
    messages: list[dict] | None = None,
) -> str:
    """Build a minimal Codex rollout JSONL string."""
    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": cwd,
                "cli_version": cli_version,
                "model_provider": model_provider,
            },
        }),
    ]
    if messages:
        for msg in messages:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            bt = "output_text" if role == "assistant" else "input_text"
            lines.append(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": bt, "text": text}],
                },
            }))
    return "\n".join(lines) + "\n"


def test_codex_transcript_header():
    text = _make_codex_lines()
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "Codex Session" in joined
    assert "019f4509-2908-7a70-a36b-9e1044cb7a88" in joined
    assert "/home/test/project" in joined
    assert "deepseek" in joined
    assert "0.98.0" in joined


def test_codex_transcript_user_message():
    text = _make_codex_lines(messages=[
        {"role": "user", "text": "Fix the auth bug"},
    ])
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "User" in joined
    assert "Fix the auth bug" in joined


def test_codex_transcript_assistant_message():
    text = _make_codex_lines(messages=[
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "Hi, how can I help?"},
    ])
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "Assistant" in joined
    assert "Hi, how can I help?" in joined


def test_codex_transcript_function_call():
    """function_call items are rendered as tool calls."""
    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "x", "cwd": "/tmp"},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "run_shell_command",
                "arguments": '{"cmd": "ls -la"}',
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "Tool:" in joined
    assert "run_shell_command" in joined
    assert "ls -la" in joined


def test_codex_transcript_skips_developer():
    """Developer messages (system instructions) are skipped."""
    text = _make_codex_lines(messages=[])
    # Add a developer message directly.
    dev = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "system prompt here"}],
        },
    })
    # Insert after session_meta, before newline
    text = text.replace("\n", "\n" + dev, 1)
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "system prompt here" not in joined


def test_codex_transcript_skips_event_msg():
    """event_msg records (token counts, turn boundaries) are skipped."""
    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "x", "cwd": "/tmp"},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        }),
        json.dumps({
            "type": "event_msg",
            "payload": {"event": {"type": "token_count"}},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    # token_count should not appear
    assert "token_count" not in joined
    # Both messages should still appear
    assert "hello" in joined
    assert "hi" in joined


def test_codex_transcript_does_not_affect_claude():
    """Claude-format JSONL is still rendered correctly."""
    claude_lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "Hello Claude"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]}}),
    ]
    text = "\n".join(claude_lines) + "\n"
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "User" in joined
    assert "Hello Claude" in joined
    assert "Assistant" in joined
    assert "Hi!" in joined


def test_codex_transcript_mixed_blocks():
    """User messages with assistant role use output_text; user uses input_text."""
    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "x", "cwd": "/tmp"},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Let me check."},
                ],
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    chunks = list(format_transcript(io.StringIO(text)))
    joined = "".join(chunks)
    assert "Let me check." in joined
    assert "Assistant" in joined


def test_codex_transcript_filters_internal_context_but_keeps_angle_text():
    lines = [
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message", "role": "user",
                "content": [
                    {"type": "input_text",
                     "text": "<environment_context>secret cwd</environment_context>"},
                    {"type": "input_text", "text": "<html> is real user text"},
                ],
            },
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": "# AGENTS.md instructions\ninternal"}],
            },
        }),
    ]
    joined = "".join(format_transcript(
        io.StringIO("\n".join(lines) + "\n"), fmt="codex"))
    assert "secret cwd" not in joined
    assert "AGENTS.md" not in joined
    assert "<html> is real user text" in joined


def test_codex_transcript_renders_plaintext_reasoning_summary_only():
    record = {
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "Checked the call graph."},
                {"type": "future_type", "text": "do not display"},
            ],
            "encrypted_content": "never-render-this-secret",
        },
    }
    joined = "".join(format_transcript(
        io.StringIO(json.dumps(record) + "\n"), fmt="codex"))
    assert "Reasoning summary" in joined
    assert "Checked the call graph." in joined
    assert "do not display" not in joined
    assert "never-render-this-secret" not in joined
