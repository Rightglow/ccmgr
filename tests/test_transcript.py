"""Tests for railmux.transcript — JSONL → ANSI text formatting."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from railmux.transcript import (
    _is_real_user,
    _render_user,
    _render_assistant_blocks,
    format_transcript,
    main,
)


# ── _is_real_user ────────────────────────────────────────────────────────

def test_is_real_user_plain_text():
    assert _is_real_user({"type": "user", "message": {"role": "user", "content": "hello"}})


def test_is_real_user_empty_string():
    assert not _is_real_user({"type": "user", "message": {"role": "user", "content": ""}})
    assert not _is_real_user({"type": "user", "message": {"role": "user", "content": "   "}})


def test_is_real_user_system_command():
    assert not _is_real_user(
        {"type": "user", "message": {"role": "user", "content": "<command-name>/exit</command-name>"}}
    )
    assert not _is_real_user(
        {"type": "user", "message": {"role": "user", "content": "<local-command-caveat>..."}}
    )


def test_is_real_user_not_user_type():
    assert not _is_real_user({"type": "assistant", "message": {"role": "assistant", "content": []}})
    assert not _is_real_user({"type": "system"})
    assert not _is_real_user({"type": "ai-title", "aiTitle": "Foo"})


def test_is_real_user_no_message_key():
    assert not _is_real_user({"type": "user"})


def test_is_real_user_wrong_role():
    assert not _is_real_user(
        {"type": "user", "message": {"role": "assistant", "content": "x"}}
    )


def test_is_real_user_content_list_with_text():
    """User message whose content is a list containing a text block → real."""
    assert _is_real_user(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "resume this"}],
            },
        }
    )


def test_is_real_user_content_list_pure_tool_result():
    """User message whose content list is ALL tool_result blocks → not real."""
    assert not _is_real_user(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "abc", "content": "ok"}
                ],
            },
        }
    )


# ── _render_user ─────────────────────────────────────────────────────────

def test_render_user_plain_text():
    result = _render_user({"type": "user", "message": {"role": "user", "content": "hello world"}})
    assert result is not None
    assert "hello world" in result
    assert "───── User ─────" in result
    assert "\033[36m" in result  # CYAN


def test_render_user_with_leading_trailing_whitespace():
    result = _render_user(
        {"type": "user", "message": {"role": "user", "content": "  hi  "}}
    )
    assert result is not None
    assert result.strip().endswith("hi")


def test_render_user_mixed_blocks():
    """Mixed content list: emit text blocks, skip tool_result blocks."""
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "my question"},
                {"type": "tool_result", "tool_use_id": "x", "content": "..."},
            ],
        },
    }
    result = _render_user(record)
    assert result is not None
    assert "my question" in result
    assert "..." not in result  # tool_result skipped


def test_render_user_all_tool_results_returns_none():
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "x"}],
        },
    }
    assert _render_user(record) is None


# ── _render_assistant_blocks ─────────────────────────────────────────────

def test_render_assistant_text_block():
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Here is the answer."}],
        },
    }
    chunks = list(_render_assistant_blocks(record))
    text = "".join(chunks)
    assert "Here is the answer." in text
    assert "───── Assistant" in text
    assert "\033[32m" in text  # GREEN


def test_render_assistant_tool_use():
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List files"}},
            ],
        },
    }
    chunks = list(_render_assistant_blocks(record))
    text = "".join(chunks)
    assert "───── Tool: Bash ─────" in text
    assert "command" in text and "ls" in text
    assert "\033[33m" in text  # YELLOW


def test_render_assistant_shows_token_count():
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }
    chunks = list(_render_assistant_blocks(record))
    text = "".join(chunks)
    assert "150 tokens" in text


def test_render_assistant_skips_thinking():
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "hmm..."}],
        },
    }
    chunks = list(_render_assistant_blocks(record))
    assert len(chunks) == 0


def test_render_assistant_no_message_key():
    assert list(_render_assistant_blocks({"type": "assistant"})) == []


def test_render_assistant_content_not_list():
    assert list(_render_assistant_blocks(
        {"type": "assistant", "message": {"role": "assistant", "content": "plain text"}}
    )) == []


def test_render_assistant_truncates_long_input_values():
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"content": "x" * 500}},
            ],
        },
    }
    chunks = list(_render_assistant_blocks(record))
    text = "".join(chunks)
    assert "…" in text
    assert len(text) < 2000  # should be truncated


# ── format_transcript (pipeline) ─────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_format_transcript_full_pipeline(tmp_path: Path):
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi there"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )
    output = "".join(format_transcript(jsonl))
    assert "hello" in output
    assert "hi there" in output
    assert "15 tokens" in output


def test_format_transcript_skips_metadata_records(tmp_path: Path):
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "mode", "mode": "normal"},
            {"type": "ai-title", "aiTitle": "Test Session"},
            {"type": "user", "message": {"role": "user", "content": "real message"}},
        ],
    )
    output = "".join(format_transcript(jsonl))
    assert "Test Session" not in output  # ai-title skipped
    assert "real message" in output


def test_format_transcript_skips_system_commands(tmp_path: Path):
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user", "message": {"role": "user", "content": "<command-name>/exit</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": "actual question"}},
        ],
    )
    output = "".join(format_transcript(jsonl))
    assert "/exit" not in output
    assert "actual question" in output


def test_format_transcript_missing_file(tmp_path: Path):
    output = "".join(format_transcript(tmp_path / "nonexistent.jsonl"))
    assert "Could not read" in output or output == ""


# ── main() CLI ───────────────────────────────────────────────────────────

def test_main_no_args(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["transcript"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Usage" in captured.err


def test_main_file_not_found(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["transcript", "/nonexistent/path.jsonl"])
    assert exc.value.code == 1


def test_main_writes_to_stdout(tmp_path, capsys):
    jsonl = tmp_path / "test.jsonl"
    _write_jsonl(
        jsonl,
        [{"type": "user", "message": {"role": "user", "content": "hi"}}],
    )
    main(["transcript", str(jsonl)])
    captured = capsys.readouterr()
    assert "hi" in captured.out
    assert captured.err == ""


# ── Codex format detection (issue #5) ────────────────────────────────────
# The UI previews via ``tail -n 2000`` before piping into this module, so for
# rollouts longer than 2000 lines the leading ``session_meta`` header is gone
# and the first record seen is a ``response_item`` / ``event_msg`` /
# ``turn_context``.  These must still be detected as Codex, not Claude.

import io


def test_codex_detected_without_session_meta_header():
    """A Codex rollout whose first record is NOT session_meta still renders
    as Codex (regression for the tail-truncated long-rollout case)."""
    lines = [
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "resumed answer"}],
            },
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "a follow-up"}],
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    # Rendered via the Codex path (would be blank under Claude parsing).
    assert "resumed answer" in joined
    assert "a follow-up" in joined
    assert "Assistant" in joined
    assert "User" in joined


@pytest.mark.parametrize("first_type", ["event_msg", "turn_context"])
def test_codex_detected_from_leading_lifecycle_record(first_type):
    """A tail-truncated rollout can start with an event_msg / turn_context."""
    lines = [
        json.dumps({"type": first_type, "payload": {"type": "token_count"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello from codex"}],
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    assert "hello from codex" in joined
    assert "token_count" not in joined  # lifecycle noise still skipped


def test_explicit_format_hint_forces_codex():
    """An explicit ``fmt='codex'`` hint overrides auto-detection."""
    line = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "forced codex"}],
        },
    }) + "\n"
    joined = "".join(format_transcript(io.StringIO(line), fmt="codex"))
    assert "forced codex" in joined


def test_explicit_format_hint_forces_claude():
    """An explicit ``fmt='claude'`` hint keeps Claude parsing even if a
    Codex-looking record leads (defensive back-compat)."""
    line = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "claude msg"},
    }) + "\n"
    joined = "".join(format_transcript(io.StringIO(line), fmt="claude"))
    assert "claude msg" in joined


def test_main_accepts_format_flag(tmp_path, capsys):
    jsonl = tmp_path / "codex.jsonl"
    _write_jsonl(jsonl, [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi codex"}],
            },
        },
    ])
    main(["transcript", "--format", "codex", str(jsonl)])
    captured = capsys.readouterr()
    assert "hi codex" in captured.out


# ── Codex tool-call rendering (issue #3) ──────────────────────────────────
# Real rollouts are dominated by custom_tool_call / custom_tool_call_output;
# function_call / function_call_output are the minority.  All four must render.

def test_codex_custom_tool_call_and_output_render_and_pair():
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_ABC",
                "input": "run ls -la",
            },
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_ABC",
                "output": [
                    {"type": "input_text", "text": "total 8\n"},
                    {"type": "input_text", "text": "drwxr-xr-x\n"},
                ],
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    assert "───── Tool: exec ─────" in joined
    assert "run ls -la" in joined
    # Output rendered and attributed back to the call by call_id.
    assert "Tool output: exec" in joined
    assert "total 8" in joined
    assert "drwxr-xr-x" in joined


def test_codex_function_call_output_renders():
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_XYZ",
                "arguments": '{"cmd": "pwd"}',
            },
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_XYZ",
                "output": "/home/user\n",
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    assert "───── Tool: shell ─────" in joined
    assert "pwd" in joined
    assert "Tool output: shell" in joined
    assert "/home/user" in joined


def test_codex_tool_output_string_form():
    """Output may be a plain string rather than a list of blocks."""
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "unknown_call",
                "output": "plain string output",
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    # No matching call recorded → generic label, but output still shown.
    assert "plain string output" in joined
    assert "Tool output" in joined


def test_codex_tool_output_empty_skipped():
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "c", "output": "   "},
        }),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "c", "output": []},
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    assert "Tool output" not in joined


def test_codex_tool_call_output_truncates_long_output():
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/tmp"}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c",
                "output": "y" * 2000,
            },
        }),
    ]
    text = "\n".join(lines) + "\n"
    joined = "".join(format_transcript(io.StringIO(text)))
    assert "…" in joined
    assert joined.count("y") < 2000  # truncated


def test_transcript_skips_malformed_json_values():
    text = "\n".join([
        json.dumps([1, 2, 3]),
        json.dumps({"type": "user", "message": "not-a-dict"}),
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }),
    ])
    assert "hello" in "".join(format_transcript(io.StringIO(text)))


def test_transcript_unknown_prefix_does_not_force_claude_detection():
    text = "\n".join([
        json.dumps({"type": "future_codex_metadata", "payload": {}}),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex hello"}],
            },
        }),
    ])
    assert "codex hello" in "".join(format_transcript(io.StringIO(text)))
