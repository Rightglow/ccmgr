"""Tests for ccmgr.transcript — JSONL → ANSI text formatting."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ccmgr.transcript import (
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
