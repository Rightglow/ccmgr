"""Tests for railmux.codex_index — Codex session scanner."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from railmux.codex_index import (
    CodexIndex,
    _TOOL_BLOCK_AGE_S,
    _scan_codex_session,
)


def _write_codex_session(path: Path, session_id: str, cwd: str,
                          messages: list[dict] | None = None,
                          cli_version: str = "0.98.0",
                          model_provider: str = "deepseek",
                          extra_lines: list[str] | None = None,
                          originator: str = "codex_cli_rs",
                          thread_source: str = "user",
                          source="cli") -> None:
    """Write a minimal Codex rollout JSONL file for testing."""
    lines = [
        json.dumps({
            "timestamp": "2026-07-09T12:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-07-09T12:00:00.000Z",
                "cwd": cwd,
                "originator": originator,
                "cli_version": cli_version,
                "source": source,
                "thread_source": thread_source,
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
    old_mtime = time.time() - _TOOL_BLOCK_AGE_S - 1
    os.utime(path, (old_mtime, old_mtime))
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.pending_tool is True
    # With old mtime it should be "blocked"
    assert meta.status == "blocked"


def test_scan_codex_minute_long_tool_stays_busy(tmp_path: Path):
    """A normal long-running Codex tool must not demand attention after only
    one minute; blocked is intentionally a conservative signal."""
    path = tmp_path / "rollout-long-tool.jsonl"
    record = json.dumps({
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": "call-long",
            "name": "run_shell_command",
            "arguments": '{"cmd": "build"}',
        },
    })
    _write_codex_session(
        path, "019f4509-2908-7a70-a36b-9e1044cb7a88", "/tmp",
        messages=[{"role": "user", "text": "build"}],
        extra_lines=[record],
    )
    one_minute_old = time.time() - 60
    os.utime(path, (one_minute_old, one_minute_old))
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.pending_tool is True
    assert meta.status == "busy"


def _token_event(*, total_tokens=None, input_tokens=None, output_tokens=None):
    """Build a real (direct-schema) Codex ``token_count`` event line."""
    usage: dict = {}
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage},
        },
    })


def test_scan_codex_session_token_count(tmp_path: Path):
    """Real Codex 0.144.x uses the DIRECT schema payload.type == token_count
    with payload.info.total_token_usage.total_tokens (preferred)."""
    path = tmp_path / "rollout-tokens.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "hi"},
                         ],
                         extra_lines=[_token_event(total_tokens=150)])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.token_total == 150


def test_scan_codex_session_token_count_cumulative_last_wins(tmp_path: Path):
    """total_token_usage is CUMULATIVE — keep the last value, never sum."""
    path = tmp_path / "rollout-tokens-cum.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "hi"},
                         ],
                         extra_lines=[
                             _token_event(total_tokens=100),
                             _token_event(total_tokens=250),
                             _token_event(total_tokens=1234),
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.token_total == 1234


def test_scan_codex_session_token_count_fallback_input_output(tmp_path: Path):
    """When total_tokens is absent, fall back to input+output of the last event."""
    path = tmp_path / "rollout-tokens-fb.jsonl"
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "hi"},
                         ],
                         extra_lines=[_token_event(input_tokens=100, output_tokens=50)])
    meta = _scan_codex_session(path)
    assert meta is not None
    assert meta.token_total == 150


def test_scan_codex_session_token_count_ignores_bad_values(tmp_path: Path):
    """Non-numeric / malformed token events must not raise or corrupt the total."""
    path = tmp_path / "rollout-tokens-bad.jsonl"
    bad = json.dumps({
        "type": "event_msg",
        "payload": {"type": "token_count", "info": "not-a-dict"},
    })
    bad2 = json.dumps({
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": "oops"}}},
    })
    _write_codex_session(path, "019f4509-2908-7a70-a36b-9e1044cb7a88",
                         "/tmp", messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "hi"},
                         ],
                         extra_lines=[
                             bad,
                             _token_event(total_tokens=42),
                             bad2,
                             _token_event(total_tokens=-1),
                             _token_event(total_tokens=float("inf")),
                             _token_event(total_tokens=1.5),
                         ])
    meta = _scan_codex_session(path)
    assert meta is not None
    # Last *valid* cumulative value survives; garbage events are ignored.
    assert meta.token_total == 42


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


def test_codex_index_cached_queries_do_not_rescan(
    tmp_path: Path, monkeypatch,
):
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    _write_codex_session(
        sessions_dir / "rollout-a.jsonl",
        sid,
        "/project-a",
        messages=[
            {"role": "user", "text": "a"},
            {"role": "assistant", "text": "ok"},
        ],
    )
    idx = CodexIndex(tmp_path)
    idx.refresh()
    monkeypatch.setattr(
        idx,
        "_refresh",
        lambda: pytest.fail("cached query unexpectedly rescanned"),
    )

    assert idx.all_cwds(refresh=False) == {Path("/project-a"): 1}
    assert len(idx.sessions_for_cwd(Path("/project-a"), refresh=False)) == 1
    assert idx.get(sid, refresh=False) is not None


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
    assert first[0].title == "a"

    # Write valid new content and update mtime so the cache misses and re-scans.
    _write_codex_session(path, sid, "/project-a",
                         messages=[{"role": "user", "text": "changed"},
                                   {"role": "assistant", "text": "ok"}])
    os.utime(path, None)
    second = idx.sessions_for_cwd(Path("/project-a"))
    # Should pick up the new content since mtime changed.
    assert len(second) == 1
    assert second[0].title == "changed"


def test_codex_append_during_scan_forces_next_refresh(
    tmp_path: Path, monkeypatch,
):
    import railmux.codex_index as index_module

    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    path = sessions_dir / "rollout-a.jsonl"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    _write_codex_session(
        path,
        sid,
        "/project-a",
        messages=[
            {"role": "user", "text": "one"},
            {"role": "assistant", "text": "ok"},
        ],
    )
    real_scan = index_module._scan_codex_session
    calls = []

    def scan_then_append(path):
        meta = real_scan(path)
        calls.append(path)
        if len(calls) == 1:
            record = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "two"}],
                },
            }
            with path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
        return meta

    monkeypatch.setattr(index_module, "_scan_codex_session", scan_then_append)
    idx = CodexIndex(tmp_path)

    assert idx.get(sid).message_count == 2
    assert idx.get(sid).message_count == 3
    assert len(calls) == 2


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
    assert idx.all_cwds() == {}
    assert idx.sessions_for_cwd(Path("/x")) == []
    assert idx.get("x") is None


class _StubRenames:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, session_id):
        return self._m.get(session_id)


def test_rename_override_overlays_codex_title(tmp_path: Path):
    sid = "019f4509-2908-7a70-a36b-9e1044cb7a88"
    _write_codex_session(
        tmp_path / "sessions" / "2026" / "07" / "09" / "rollout-x.jsonl",
        sid, "/proj",
        messages=[{"role": "user", "text": "orig"},
                  {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path, _StubRenames({sid: "Renamed"}))

    sessions = idx.sessions_for_cwd(Path("/proj"))
    assert sessions and sessions[0].title == "Renamed"
    assert idx.get(sid).title == "Renamed"


def test_no_override_keeps_codex_auto_title(tmp_path: Path):
    sid = "019f4509-2908-7a70-a36b-9e1044cb7a89"
    _write_codex_session(
        tmp_path / "sessions" / "2026" / "07" / "09" / "rollout-y.jsonl",
        sid, "/proj",
        messages=[{"role": "user", "text": "orig"},
                  {"role": "assistant", "text": "ok"}],
    )
    idx = CodexIndex(tmp_path, _StubRenames({}))
    assert idx.sessions_for_cwd(Path("/proj"))[0].title == "orig"


def test_scan_codex_session_skips_exec_originator(tmp_path: Path):
    """Rollout files whose originator is ``codex_exec`` are non-interactive
    automation / review threads — they must be skipped so they don't flood
    the sidebar."""
    p = tmp_path / "rollout-exec.jsonl"
    _write_codex_session(p, "sid-exec", "/tmp/proj", originator="codex_exec")
    assert _scan_codex_session(p) is None


def test_scan_codex_session_keeps_interactive(tmp_path: Path):
    """Regression: interactive originators (``codex-tui``, ``codex_cli_rs``,
    missing field, etc.) must still be shown."""
    p = tmp_path / "rollout-tui.jsonl"
    _write_codex_session(p, "sid-tui", "/tmp/proj", originator="codex-tui",
                         messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "ok"},
                         ])
    assert _scan_codex_session(p) is not None


def test_scan_codex_session_skips_subagent_thread_source(tmp_path: Path):
    """Rollout files marked ``thread_source == "subagent"`` are one-per-subagent
    copies of a Codex multi-agent run that share the parent's session_id — they
    must be skipped so a single conversation doesn't flood the sidebar."""
    p = tmp_path / "rollout-subagent.jsonl"
    _write_codex_session(p, "sid-sub", "/tmp/proj", originator="codex-tui",
                         thread_source="subagent",
                         messages=[{"role": "user", "text": "hello"}])
    assert _scan_codex_session(p) is None


def test_scan_codex_session_skips_subagent_source_dict(tmp_path: Path):
    """A dict ``source`` like ``{"subagent": {...}}`` also identifies a subagent
    rollout (real Codex data emits ``{"subagent": {"other": "guardian"}}``),
    even when ``thread_source`` is absent or unexpected."""
    p = tmp_path / "rollout-subagent-src.jsonl"
    _write_codex_session(p, "sid-sub2", "/tmp/proj", originator="codex-tui",
                         thread_source="",
                         source={"subagent": {"other": "guardian"}},
                         messages=[{"role": "user", "text": "hello"}])
    assert _scan_codex_session(p) is None


def test_scan_codex_session_keeps_user_thread_source(tmp_path: Path):
    """Regression: a normal user-initiated rollout (``thread_source == "user"``,
    plain string ``source``) must still be shown."""
    p = tmp_path / "rollout-user.jsonl"
    _write_codex_session(p, "sid-user", "/tmp/proj", originator="codex-tui",
                         thread_source="user", source="cli",
                         messages=[
                             {"role": "user", "text": "hello"},
                             {"role": "assistant", "text": "ok"},
                         ])
    assert _scan_codex_session(p) is not None


# -- tool-call state machine (#3) -----------------------------------------

def _tool_call(kind: str, call_id: str, name: str = "exec"):
    return json.dumps({
        "type": "response_item",
        "payload": {"type": kind, "call_id": call_id, "name": name,
                    "input": "{}"},
    })


def _tool_output(kind: str, call_id: str):
    return json.dumps({
        "type": "response_item",
        "payload": {"type": kind, "call_id": call_id, "output": "done"},
    })


def _event(etype: str, **extra):
    payload = {"type": etype}
    payload.update(extra)
    return json.dumps({"type": "event_msg", "payload": payload})


def _message(role: str, text: str):
    return json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{
                "type": "output_text" if role == "assistant" else "input_text",
                "text": text,
            }],
        },
    })


@pytest.mark.parametrize("call_kind,out_kind", [
    ("custom_tool_call", "custom_tool_call_output"),
    ("function_call", "function_call_output"),
])
def test_scan_codex_paired_tool_call_not_pending(tmp_path, call_kind, out_kind):
    """A tool call WITH a matching output by call_id is not pending."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-paired", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"},
                                   {"role": "assistant", "text": "ok"}],
                         extra_lines=[
                             _tool_call(call_kind, "call_1"),
                             _tool_output(out_kind, "call_1"),
                             _event("task_complete"),
                         ])
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.pending_tool is False
    assert meta.status == "idle"


@pytest.mark.parametrize("call_kind,out_kind", [
    ("custom_tool_call", "custom_tool_call_output"),
    ("function_call", "function_call_output"),
])
def test_scan_codex_unpaired_tool_call_is_pending(tmp_path, call_kind, out_kind):
    """A tool call with NO matching output (different call_id) stays pending."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-unpaired", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"},
                                   {"role": "assistant", "text": "ok"}],
                         extra_lines=[
                             _tool_call(call_kind, "call_1"),
                             _tool_output(out_kind, "call_OTHER"),
                         ])
    old = time.time() - _TOOL_BLOCK_AGE_S - 1
    os.utime(p, (old, old))
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.pending_tool is True
    assert meta.status == "blocked"


# -- lifecycle status (#4) ------------------------------------------------

def test_scan_codex_turn_aborted_not_busy(tmp_path):
    """A session ending in turn_aborted must NOT show busy, even with a
    dangling (unpaired) tool call from the interrupted turn."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-abort", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"}],
                         extra_lines=[
                             _event("task_started"),
                             _tool_call("custom_tool_call", "call_1"),
                             _event("turn_aborted", reason="interrupted"),
                         ])
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.pending_tool is False
    assert meta.status == "idle"


def test_scan_codex_thread_rolled_back_not_busy(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-rollback", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"},
                                   {"role": "assistant", "text": "ok"}],
                         extra_lines=[
                             _event("task_started"),
                             _event("thread_rolled_back", num_turns=1),
                         ])
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.status == "idle"


def test_scan_codex_task_started_without_complete_is_busy(tmp_path):
    """A turn in progress (task_started, no completion, no pending tool)
    reads as busy."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-inflight", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"}],
                         extra_lines=[_event("task_started")])
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.status == "busy"


def test_scan_codex_intermediate_assistant_does_not_end_active_turn(tmp_path):
    """Codex emits assistant messages before a turn's task_complete.  A
    paired tool result and later reasoning must not make that live turn idle."""
    p = tmp_path / "rollout.jsonl"
    reasoning = json.dumps({
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": []},
    })
    _write_codex_session(
        p, "sid-inflight-assistant", "/tmp/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "go"}],
        extra_lines=[
            _event("task_started"),
            _message("assistant", "I will inspect that."),
            _tool_call("custom_tool_call", "call_1"),
            _tool_output("custom_tool_call_output", "call_1"),
            reasoning,
        ],
    )
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.pending_tool is False
    assert meta.status == "busy"


def test_scan_codex_legacy_assistant_without_lifecycle_is_idle(tmp_path):
    """Rollouts from before lifecycle events keep last-message semantics."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(
        p, "sid-legacy", "/tmp/proj", originator="codex-tui",
        messages=[
            {"role": "user", "text": "go"},
            {"role": "assistant", "text": "done"},
        ],
    )
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.status == "idle"


def test_scan_codex_user_after_completed_turn_reopens_busy(tmp_path):
    """A new user record may be flushed just before its task_started event."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(
        p, "sid-next-user", "/tmp/proj", originator="codex-tui",
        messages=[
            {"role": "user", "text": "first"},
            {"role": "assistant", "text": "done"},
        ],
        extra_lines=[
            _event("task_started"),
            _event("task_complete"),
            _message("user", "follow up"),
        ],
    )
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.status == "busy"


def test_scan_codex_task_complete_is_idle(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-done", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "go"},
                                   {"role": "assistant", "text": "ok"}],
                         extra_lines=[_event("task_started"),
                                      _event("task_complete")])
    meta = _scan_codex_session(p)
    assert meta is not None
    assert meta.status == "idle"


# -- parser defense (#13) -------------------------------------------------

def test_scan_codex_malformed_records_do_not_crash(tmp_path):
    """List / string / wrong-typed payloads must be skipped, not raise."""
    p = tmp_path / "rollout.jsonl"
    junk = [
        json.dumps([1, 2, 3]),                       # top-level list
        json.dumps("a bare string"),                 # top-level string
        json.dumps({"type": "response_item", "payload": ["not", "a", "dict"]}),
        json.dumps({"type": "response_item", "payload": "str"}),
        json.dumps({"type": "event_msg", "payload": 12345}),
        # user message whose content is a bare string (not a list) — the text
        # extraction must be skipped without raising.
        json.dumps({"type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": "not-a-list"}}),
        # non-numeric call_id on a tool call — must not raise.
        json.dumps({"type": "response_item",
                    "payload": {"type": "custom_tool_call", "call_id": 123}}),
        "this is not json at all {{{",
    ]
    _write_codex_session(p, "sid-junk", "/tmp/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "hello"},
                                   {"role": "assistant", "text": "ok"}],
                         extra_lines=junk)
    meta = _scan_codex_session(p)
    assert meta is not None
    # The two well-formed messages plus the malformed-content assistant record.
    assert meta.message_count >= 2
    assert meta.title == "hello"


def test_scan_codex_non_dict_session_meta_returns_none(tmp_path):
    """A first line that isn't a session_meta dict yields None (no crash)."""
    p = tmp_path / "rollout.jsonl"
    p.write_text(json.dumps([1, 2, 3]) + "\n", encoding="utf-8")
    assert _scan_codex_session(p) is None


# -- missing cwd (#15) ----------------------------------------------------

def test_scan_codex_missing_cwd_skipped_not_root(tmp_path):
    """A rollout with a missing/blank cwd is skipped, never mapped to '/'."""
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-nocwd", "", originator="codex-tui",
                         messages=[{"role": "user", "text": "hi"}])
    assert _scan_codex_session(p) is None


def test_scan_codex_whitespace_cwd_skipped(tmp_path):
    p = tmp_path / "rollout.jsonl"
    _write_codex_session(p, "sid-wscwd", "   ", originator="codex-tui",
                         messages=[{"role": "user", "text": "hi"}])
    assert _scan_codex_session(p) is None


# -- duplicate session ids (#14) ------------------------------------------

def test_codex_index_duplicate_session_id_newest_wins(tmp_path):
    """Two rollout files sharing one session_id collapse to a single entry;
    counts == list length and the newest mtime wins in get()."""
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    dup = "dddddddd-1111-7111-a36b-9e1044cb7a88"
    old_path = sessions_dir / "rollout-old.jsonl"
    new_path = sessions_dir / "rollout-new.jsonl"
    _write_codex_session(old_path, dup, "/dupproj",
                         messages=[{"role": "user", "text": "old"},
                                   {"role": "assistant", "text": "ok"}])
    _write_codex_session(new_path, dup, "/dupproj",
                         messages=[{"role": "user", "text": "new"},
                                   {"role": "assistant", "text": "ok"}])
    old_t = time.time() - 100
    os.utime(old_path, (old_t, old_t))
    new_t = time.time()
    os.utime(new_path, (new_t, new_t))

    idx = CodexIndex(tmp_path)
    cwds = idx.all_cwds()
    assert cwds == {Path("/dupproj"): 1}
    sessions = idx.sessions_for_cwd(Path("/dupproj"))
    assert len(sessions) == 1
    # count == list length, and the newest file's content wins.
    assert sessions[0].title == "new"
    assert idx.get(dup).title == "new"


# -- negative cache (#6, index part) --------------------------------------

def test_codex_index_negative_cache_skips_reparse(tmp_path, monkeypatch):
    """Filtered / None-returning files aren't re-opened until their signature
    changes."""
    import railmux.codex_index as index_module

    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    # An interactive session that IS indexed.
    _write_codex_session(
        sessions_dir / "rollout-good.jsonl",
        "a1111111-1111-7111-a36b-9e1044cb7a88", "/proj",
        originator="codex-tui",
        messages=[{"role": "user", "text": "hi"},
                  {"role": "assistant", "text": "ok"}],
    )
    # A codex_exec file that is filtered (scan -> None).
    exec_path = sessions_dir / "rollout-exec.jsonl"
    _write_codex_session(exec_path, "sid-exec", "/proj",
                         originator="codex_exec")

    real_scan = index_module._scan_codex_session
    scanned: list = []

    def counting_scan(path):
        scanned.append(path)
        return real_scan(path)

    monkeypatch.setattr(index_module, "_scan_codex_session", counting_scan)
    idx = CodexIndex(tmp_path)

    idx.refresh()
    assert exec_path in scanned
    scanned.clear()

    # Second refresh: neither the cached good file nor the negatively-cached
    # exec file is reopened (signatures unchanged).
    idx.refresh()
    assert exec_path not in scanned

    # Change the exec file's signature -> it is reconsidered.
    with exec_path.open("a") as fh:
        fh.write(json.dumps({"type": "event_msg", "payload": {}}) + "\n")
    scanned.clear()
    idx.refresh()
    assert exec_path in scanned


def test_codex_index_unchanged_busy_session_is_not_reparsed(
    tmp_path, monkeypatch,
):
    """Age alone never requires reopening an unchanged rollout."""
    import railmux.codex_index as index_module

    path = tmp_path / "sessions/2026/07/09/rollout.jsonl"
    _write_codex_session(
        path, "sid-busy", "/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "go"}],
    )
    idx = CodexIndex(tmp_path)
    meta = idx.get("sid-busy")
    assert meta is not None and meta.status == "busy"
    monkeypatch.setattr(
        index_module, "_scan_codex_session",
        lambda _path: pytest.fail("unchanged busy rollout was reopened"),
    )
    monkeypatch.setattr(index_module.time, "time",
                        lambda: meta.last_mtime + 60)
    assert idx.get("sid-busy").status == "busy"


def test_codex_index_pending_tool_ages_to_blocked_without_reparse(
    tmp_path, monkeypatch,
):
    """The only time-dependent status change is handled in cache."""
    import railmux.codex_index as index_module

    path = tmp_path / "sessions/2026/07/09/rollout.jsonl"
    _write_codex_session(
        path, "sid-tool", "/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "go"}],
        extra_lines=[_tool_call("custom_tool_call", "call_1")],
    )
    idx = CodexIndex(tmp_path)
    meta = idx.get("sid-tool")
    assert meta is not None and meta.status == "busy" and meta.pending_tool
    monkeypatch.setattr(
        index_module, "_scan_codex_session",
        lambda _path: pytest.fail("unchanged tool rollout was reopened"),
    )
    monkeypatch.setattr(index_module.time, "time",
                        lambda: meta.last_mtime + _TOOL_BLOCK_AGE_S + 1)
    assert idx.get("sid-tool").status == "blocked"


def test_codex_index_partial_walk_keeps_unvisited_cached_sessions(
    tmp_path, monkeypatch,
):
    """A transient subtree error must not look like mass file deletion."""
    import railmux.codex_index as index_module

    session_dir = tmp_path / "sessions/2026/07/09"
    first = session_dir / "rollout-a.jsonl"
    second = session_dir / "rollout-b.jsonl"
    _write_codex_session(
        first, "sid-a", "/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "a"}],
    )
    _write_codex_session(
        second, "sid-b", "/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "b"}],
    )
    idx = CodexIndex(tmp_path)
    idx.refresh()

    def partial_walk(_root, onerror):
        yield str(session_dir), [], [first.name]
        onerror(OSError("temporary NFS failure"))

    monkeypatch.setattr(index_module.os, "walk", partial_walk)
    idx.refresh()
    assert idx.get("sid-a", refresh=False) is not None
    assert idx.get("sid-b", refresh=False) is not None


def test_scan_codex_open_error_returns_scan_error(tmp_path):
    """A file that can't be opened is a *transient* failure (SCAN_ERROR), not a
    deterministic skip (None) — so the index won't permanently hide it."""
    from railmux.codex_index import SCAN_ERROR

    missing = tmp_path / "does-not-exist.jsonl"
    assert _scan_codex_session(missing) is SCAN_ERROR


def test_scan_codex_filtered_is_none_not_scan_error(tmp_path):
    """Contrast: a genuinely filtered rollout is a deterministic None (safe to
    negative-cache), never the transient SCAN_ERROR sentinel."""
    from railmux.codex_index import SCAN_ERROR

    p = tmp_path / "rollout-exec.jsonl"
    _write_codex_session(p, "sid-exec", "/tmp/proj", originator="codex_exec")
    result = _scan_codex_session(p)
    assert result is None
    assert result is not SCAN_ERROR


def test_codex_index_filtered_stays_negative_cached(tmp_path, monkeypatch):
    """A deterministic skip (codex_exec) is negative-cached and NOT re-parsed on
    a later refresh while its signature is unchanged."""
    import railmux.codex_index as index_module

    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    exec_path = sessions_dir / "rollout-exec.jsonl"
    _write_codex_session(exec_path, "sid-exec", "/proj", originator="codex_exec")

    real_scan = index_module._scan_codex_session
    scanned: list = []

    def counting_scan(path):
        scanned.append(path)
        return real_scan(path)

    monkeypatch.setattr(index_module, "_scan_codex_session", counting_scan)
    idx = CodexIndex(tmp_path)

    idx.refresh()
    assert exec_path in scanned
    scanned.clear()
    # Signature unchanged -> negative cache hit -> not reopened.
    idx.refresh()
    assert exec_path not in scanned


def test_codex_index_transient_error_retried_not_hidden(tmp_path, monkeypatch):
    """A transient error (SCAN_ERROR) must NOT be negative-cached: the file is
    retried on the next refresh even though its signature is unchanged, and it
    appears once the error clears.  This is the #6 regression — a passing NFS
    read error used to hide an otherwise-stable rollout indefinitely."""
    import railmux.codex_index as index_module

    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    path = sessions_dir / "rollout.jsonl"
    _write_codex_session(
        path, sid, "/proj", originator="codex-tui",
        messages=[{"role": "user", "text": "hi"},
                  {"role": "assistant", "text": "ok"}],
    )

    real_scan = index_module._scan_codex_session
    calls = {"n": 0}

    def flaky_scan(p):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a one-off transient read error.
            return index_module.SCAN_ERROR
        return real_scan(p)

    monkeypatch.setattr(index_module, "_scan_codex_session", flaky_scan)
    idx = CodexIndex(tmp_path)

    # First refresh: transient error -> not indexed, but not permanently hidden.
    idx.refresh()
    assert idx.get(sid, refresh=False) is None

    # Signature is unchanged.  A *filtered* file would stay negative-cached and
    # be skipped here; a transient error must be retried instead.
    idx.refresh()
    assert calls["n"] == 2  # the file was reopened, not negative-cache-skipped
    meta = idx.get(sid, refresh=False)
    assert meta is not None
    assert meta.session_id == sid


def test_codex_index_stale_entry_evicted_when_scan_returns_none(tmp_path):
    """If a cached file's signature changes and it now scans to None, the old
    entry is dropped rather than lingering."""
    sessions_dir = tmp_path / "sessions" / "2026" / "07" / "09"
    sid = "a1111111-1111-7111-a36b-9e1044cb7a88"
    path = sessions_dir / "rollout.jsonl"
    _write_codex_session(path, sid, "/proj", originator="codex-tui",
                         messages=[{"role": "user", "text": "hi"},
                                   {"role": "assistant", "text": "ok"}])
    idx = CodexIndex(tmp_path)
    assert idx.get(sid) is not None

    # Rewrite the file so it now scans to None (codex_exec -> filtered).
    _write_codex_session(path, sid, "/proj", originator="codex_exec")
    os.utime(path, None)
    assert idx.get(sid) is None
    assert idx.all_cwds() == {}
