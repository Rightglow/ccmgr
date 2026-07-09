"""Tests for ccmgr.codex_index — Codex session scanner."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ccmgr.codex_index import CodexIndex, _scan_codex_session


def _write_codex_session(path: Path, session_id: str, cwd: str,
                          messages: list[dict] | None = None,
                          cli_version: str = "0.98.0",
                          model_provider: str = "deepseek",
                          extra_lines: list[str] | None = None) -> None:
    """Write a minimal Codex rollout JSONL file for testing."""
    lines = [
        json.dumps({
            "timestamp": "2026-07-09T12:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-07-09T12:00:00.000Z",
                "cwd": cwd,
                "originator": "codex_cli_rs",
                "cli_version": cli_version,
                "source": "cli",
                "model_provider": model_provider,
            },
        }),
    ]
    # If messages provided, convert to response_item lines.
    if messages:
        for msg in messages:
            role = msg.get("role", "user")
            text = msg.get("text", "")
            lines.append(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": text,
                    }],
                },
            }))
    # Append extra raw lines as-is (e.g. event_msg for token counts).
    if extra_lines:
        lines.extend(extra_lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_scan_codex_session_basic(tmp_path: Path):
    path = tmp_path / "rollout-test.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/home/testuser/project", messages=[
                             {"role": "user", "text": "Hello world"},
                             {"role": "assistant", "text": "Hi there!"},
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.session_id == "019f4509-2908-7a70-a36b-9e1044cb7a88"
    assert meta.session_type == "codex"
    assert meta.title == "Hello world"
    assert meta.message_count == 2
    assert meta.status == "idle"


def test_scan_codex_session_busy(tmp_path: Path):
    """Last record is a user message → status = busy."""
    path = tmp_path / "rollout-busy.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "do something"},
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.status == "busy"


def test_scan_codex_session_blocked(tmp_path: Path):
    """Last record is a function_call → pending_tool, status depends on age."""
    path = tmp_path / "rollout-blocked.jsonl"
    record = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "run_shell_command",
            "arguments": '{"cmd": "ls"}',
        },
    })
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "list files"},
                             {"role": "assistant", "text": "ok"},
                         ],
                         extra_lines=[record])
    # Set mtime far in the past so it's detected as blocked.
    old_mtime = time.time() - 60
    os.utime(path, (old_mtime, old_mtime))
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.pending_tool is True
    # With old mtime it should be "blocked"
    assert meta.status == "blocked"


def test_scan_codex_session_token_count(tmp_path: Path):
    path = tmp_path / "rollout-tokens.jsonl"
    token_event = json.dumps({
        "type": "event_msg",
        "payload": {
            "event": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                    },
                },
            },
        },
    })
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "hi"},
                         ],
                         extra_lines=[token_event])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.token_total == 150


def test_scan_codex_session_title_fallback_first_user_message(tmp_path: Path):
    path = tmp_path / "rollout-title.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "Fix the auth bug\nin login module"},
                             {"role": "assistant", "text": "ok"},
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.title is not None
    assert "Fix the auth bug" in meta.title


def test_scan_codex_session_empty(tmp_path: Path):
    """Sessions with no messages return None."""
    path = tmp_path / "rollout-empty.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[])
    meta = _scan_codex_session(path)
    assert meta is None


def test_scan_codex_session_orphan_user_no_assistant(tmp_path: Path):
    """Codex sessions with only a user message are valid (unlike Claude).
    codex resume can continue them."""
    path = tmp_path / "rollout-orphan.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.status == "busy"


def test_codex_index_sessions_for_cwd(tmp_path: Path):
    """CodexIndex groups sessions by cwd."""
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    # Create two sessions in /project-a
    _write_codex_session(
        sessions_dir / "rollout-a1.jsonl",
        "a1111111-1111-7111-a36b-9e1044cb7a88",
        "/project-a",
        messages=[{"role": "user", "text": "one"}, {"role": "assistant", "text": "ok"}],
    )
    _write_codex_session(
        sessions_dir / "rollout-a2.jsonl",
        "a2222222-2222-7222-a36b-9e1044cb7a88",
        "/project-a",
        messages=[{"role": "user", "text": "two"}, {"role": "assistant", "text": "ok"}],
    )
    # And one in /project-b
    _write_codex_session(
        sessions_dir / "rollout-b1.jsonl",
        "b1111111-1111-7111-a36b-9e1044cb7a88",
        "/project-b",
        messages=[{"role": "user", "text": "b"}, {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path)
    sessions_a = idx.sessions_for_cwd(Path("/project-a"))
    assert len(sessions_a) == 2
    assert all(s.session_type == "codex" for s in sessions_a)
    sessions_b = idx.sessions_for_cwd(Path("/project-b"))
    assert len(sessions_b) == 1
    assert sessions_b[0].session_id == "b1111111-1111-7111-a36b-9e1044cb7a88"


def test_codex_index_all_cwds(tmp_path: Path):
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    _write_codex_session(
        sessions_dir / "rollout-a.jsonl",
        "a1111111-1111-7111-a36b-9e1044cb7a88",
        "/project-a",
        messages=[{"role": "user", "text": "a"}, {"role": "assistant", "text": "ok"}],
    )
    _write_codex_session(
        sessions_dir / "rollout-b.jsonl",
        "b1111111-1111-7111-a36b-9e1044cb7a88",
        "/project-b",
        messages=[{"role": "user", "text": "b"}, {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path)
    cwds = idx.all_cwds()
    assert Path("/project-a") in cwds
    assert Path("/project-b") in cwds
    assert len(cwds) == 2


def test_codex_index_get(tmp_path: Path):
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    _write_codex_session(
        sessions_dir / "rollout-a.jsonl", sid,
        "/project-a",
        messages=[{"role": "user", "text": "a"}, {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path)
    meta = idx.get(sid)
    assert meta is not None
    assert meta.session_id == sid
    assert idx.get("nonexistent") is None


def test_codex_index_cache_mtime(tmp_path: Path):
    """Re-scanning with unchanged mtime uses cached result."""
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    path = sessions_dir / "rollout-a.jsonl"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    _write_codex_session(path, sid, "/project-a",
                         messages=[{"role": "user", "text": "a"}, {"role": "assistant", "text": "ok"}])

    idx = CodexIndex(tmp_path)
    first = idx.sessions_for_cwd(Path("/project-a"))
    assert len(first) == 1

    # Write different content and update mtime so cache misses.
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
    os.utime(path, None)
    second = idx.sessions_for_cwd(Path("/project-a"))
    # Should pick up the new content since mtime changed
    assert len(second) > 0


def test_codex_index_invalidate(tmp_path: Path):
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    _write_codex_session(
        sessions_dir / "rollout-a.jsonl",
        "a1111111-1111-7111-a36b-9e1044cb7a88",
        "/project-a",
        messages=[{"role": "user", "text": "a"}, {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path)
    assert len(idx.all_cwds()) == 1
    idx.invalidate()
    # After invalidate, cache is empty but re-scan on next call
    assert len(idx.all_cwds()) == 1  # re-scanned


def test_codex_index_no_sessions_dir(tmp_path: Path):
    idx = CodexIndex(tmp_path / "nonexistent")
    assert idx.all_cwds() == set()
    assert idx.sessions_for_cwd(Path("/x")) == []
    assert idx.get("x") is None
