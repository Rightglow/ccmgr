"""Status-dot derivation: JSONL scan, cache staleness, live-process refinement."""
import json
import os
import time
from pathlib import Path

import pytest

from ccmgr import tmux_ctl
from ccmgr.config import Config
from ccmgr.models import Project, SessionMeta
from ccmgr.session_cache import SessionCache
from ccmgr.session_index import _scan_session

_UID = "11111111-1111-1111-1111-111111111111"
U = {"type": "user", "message": {"role": "user", "content": "hi"}}
A_END = {"type": "assistant", "message": {"role": "assistant", "content": "ok",
         "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}}
A_TOOL = {"type": "assistant", "message": {"role": "assistant", "content": "x",
          "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}}}
LP = {"type": "last-prompt", "prompt": "whatever"}


def _project(tmp_path):
    return Project(real_path=tmp_path, encoded_name="-x", claude_dir=tmp_path,
                   session_count=1, last_activity_ts=0.0)


def _scan(tmp_path, records, age=0.0, sid=_UID):
    p = tmp_path / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    if age:
        os.utime(p, (time.time() - age, time.time() - age))
    return _scan_session(_project(tmp_path), p)


# ── JSONL-derived status ─────────────────────────────────────────────────

def test_last_prompt_after_end_turn_is_idle(tmp_path):
    # Real ordering: assistant end_turn → last-prompt → (next) user.  The
    # last-prompt must NOT make a finished session look busy.
    m = _scan(tmp_path, [U, A_END, LP])
    assert m.status == "idle"
    assert m.pending_tool is False


def test_last_user_message_is_busy(tmp_path):
    m = _scan(tmp_path, [A_END, LP, U])
    assert m.status == "busy"


def test_recent_tool_use_is_busy_and_pending(tmp_path):
    m = _scan(tmp_path, [U, A_TOOL], age=2)
    assert m.status == "busy"
    assert m.pending_tool is True


def test_old_tool_use_is_blocked_and_pending(tmp_path):
    m = _scan(tmp_path, [U, A_TOOL], age=30)
    assert m.status == "blocked"
    assert m.pending_tool is True


def test_end_turn_is_idle(tmp_path):
    m = _scan(tmp_path, [U, A_TOOL, A_END])
    assert m.status == "idle"
    assert m.pending_tool is False


# ── cache: stale "busy" re-scans into "blocked" ──────────────────────────

def test_cache_rescans_stale_busy(tmp_path):
    """A tool_use scanned as busy must transition to blocked once it ages,
    even though its mtime never changes."""
    proj = _project(tmp_path)
    path = tmp_path / f"{_UID}.jsonl"
    path.write_text(json.dumps(U) + "\n" + json.dumps(A_TOOL) + "\n")
    cache = SessionCache()
    # Fresh: busy.
    assert cache.get(proj, _UID).status == "busy"
    # Age the file past the block window without changing its content-derived
    # status, then re-query — cache must re-scan and report blocked.
    old = time.time() - 30
    os.utime(path, (old, old))
    assert cache.get(proj, _UID).status == "blocked"


def test_cache_get_missing_returns_none(tmp_path):
    assert SessionCache().get(_project(tmp_path), _UID) is None


# ── App._effective_status: live-process refinement ───────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    from ccmgr.ui.app import App, _Running
    ch = tmp_path / ".claude"
    (ch / "projects").mkdir(parents=True)
    a = App(claude_home=ch, config=Config(), auto_launched=False)
    return a, _Running


def _meta(pending):
    return SessionMeta(project=None, session_id=_UID, jsonl_path=Path("/x"),
                       title="t", message_count=1, token_total=1, last_mtime=0.0,
                       status="blocked" if pending else "idle", pending_tool=pending)


def test_effective_status_running_with_child_is_busy(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    monkeypatch.setattr(tmux_ctl, "session_has_child", lambda name: True)
    assert a._effective_status(_meta(pending=True)) == "busy"


def test_effective_status_running_no_child_is_blocked(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    monkeypatch.setattr(tmux_ctl, "session_has_child", lambda name: False)
    assert a._effective_status(_meta(pending=True)) == "blocked"


def test_effective_status_probe_failure_falls_back_to_jsonl(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    monkeypatch.setattr(tmux_ctl, "session_has_child", lambda name: None)
    assert a._effective_status(_meta(pending=True)) == "blocked"

    recent = SessionMeta(
        project=None, session_id=_UID, jsonl_path=Path("/x"), title="t",
        message_count=1, token_total=1, last_mtime=0.0,
        status="busy", pending_tool=True,
    )
    assert a._effective_status(recent) == "busy"


def test_effective_status_not_opened_falls_back_to_time(app, monkeypatch):
    a, _Running = app
    # Not in _running → no live process → use meta.status, never call pgrep.
    called = []
    monkeypatch.setattr(tmux_ctl, "session_has_child",
                        lambda name: called.append(name) or True)
    m = _meta(pending=True)  # meta.status == "blocked" (time-derived)
    assert a._effective_status(m) == "blocked"
    assert called == []


def test_effective_status_non_pending_unchanged(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    called = []
    monkeypatch.setattr(tmux_ctl, "session_has_child",
                        lambda name: called.append(name) or True)
    m = SessionMeta(project=None, session_id=_UID, jsonl_path=Path("/x"), title="t",
                    message_count=1, token_total=1, last_mtime=0.0,
                    status="busy", pending_tool=False)
    assert a._effective_status(m) == "busy"
    assert called == []  # non-pending never inspects the process


def test_refresh_clears_visual_selection_for_missing_project(app):
    a, _Running = app
    project = Project(
        real_path=Path("/tmp/missing"),
        encoded_name="-tmp-missing",
        claude_dir=Path("/tmp/missing-meta"),
        session_count=0,
        last_activity_ts=0.0,
    )
    a._selected_project = project
    a._projects_pane.set_projects([project])
    a._projects_pane.set_selected(project.encoded_name)

    a._refresh()

    assert a._selected_project is None
    assert a._projects_pane._selected_encoded_name is None
