"""Status-dot derivation: JSONL scan, cache staleness, live-process refinement."""
import json
import os
import time
from pathlib import Path

import pytest

from railmux import tmux_ctl
from railmux.config import Config
from railmux.models import Project, SessionMeta
from railmux.session_cache import SessionCache
from railmux.session_index import _scan_session

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
    from railmux.ui.app import App, _Running
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


def test_effective_status_codex_pending_skips_claude_child_probe(app, monkeypatch):
    """A Codex pane has permanent children, so Claude's child-process probe
    cannot distinguish an active tool from an approval prompt."""
    a, _Running = app
    a._running[_UID] = _Running(
        key=_UID, tmux_name="cx-x", label="l", session_type="codex")
    monkeypatch.setattr(
        tmux_ctl, "process_has_child",
        lambda _pid: pytest.fail("Codex used the Claude process heuristic"),
    )
    monkeypatch.setattr(
        tmux_ctl, "session_has_child",
        lambda _name: pytest.fail("Codex used the Claude process heuristic"),
    )
    meta = SessionMeta(
        project=None, session_id=_UID, jsonl_path=Path("/x"), title="t",
        message_count=1, token_total=1, last_mtime=0.0,
        status="blocked", pending_tool=True, session_type="codex",
    )
    assert a._effective_status(meta) == "blocked"


def test_effective_status_reuses_probe_within_refresh(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    calls = []
    monkeypatch.setattr(
        tmux_ctl,
        "session_has_child",
        lambda name: calls.append(name) or True,
    )
    probes = {}

    assert a._effective_status(_meta(pending=True), probes) == "busy"
    assert a._effective_status(_meta(pending=True), probes) == "busy"
    assert calls == ["cc-x"]


def test_effective_status_uses_snapshot_pane_pid(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    calls = []
    monkeypatch.setattr(
        tmux_ctl,
        "process_has_child",
        lambda pid: calls.append(pid) or True,
    )
    monkeypatch.setattr(
        tmux_ctl,
        "session_has_child",
        lambda _name: pytest.fail("unexpected per-session tmux probe"),
    )
    server = tmux_ctl.ServerSnapshot(
        sessions=frozenset({"cc-x"}),
        panes=frozenset({"%9"}),
        session_pids=(("cc-x", 4321),),
    )

    assert a._effective_status(
        _meta(pending=True), {}, server) == "busy"
    assert calls == [4321]


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


def test_refresh_skips_server_snapshot_without_liveness_targets(
    app, monkeypatch,
):
    a, _Running = app
    monkeypatch.setattr(
        tmux_ctl,
        "server_snapshot",
        lambda: pytest.fail("idle refresh queried tmux server"),
    )

    a._refresh()


def test_refresh_uses_server_snapshot_instead_of_targeted_probes(
    app, monkeypatch,
):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    a._right_pane_id = "%9"
    a._right_pane_claude = "cc-x"
    monkeypatch.setattr(
        tmux_ctl,
        "server_snapshot",
        lambda: tmux_ctl.ServerSnapshot(
            sessions=frozenset({"cc-x"}),
            panes=frozenset({"%9"}),
        ),
    )
    monkeypatch.setattr(
        tmux_ctl,
        "session_exists",
        lambda _name: pytest.fail("unexpected targeted session probe"),
    )
    monkeypatch.setattr(
        tmux_ctl,
        "pane_alive",
        lambda _pane: pytest.fail("unexpected targeted pane probe"),
    )

    a._refresh()

    assert _UID in a._running
    assert a._right_pane_id == "%9"


def test_refresh_snapshot_prunes_dead_targets(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    a._right_pane_id = "%9"
    a._right_pane_claude = "cc-x"
    killed = []
    monkeypatch.setattr(
        tmux_ctl,
        "server_snapshot",
        lambda: tmux_ctl.ServerSnapshot(frozenset(), frozenset()),
    )
    monkeypatch.setattr(
        tmux_ctl, "kill_pane", lambda pane: killed.append(pane) or True)

    a._refresh()

    assert _UID not in a._running
    assert a._right_pane_id is None
    assert a._right_pane_claude is None
    assert killed == ["%9"]


def test_refresh_falls_back_when_snapshot_is_unavailable(app, monkeypatch):
    a, _Running = app
    a._running[_UID] = _Running(key=_UID, tmux_name="cc-x", label="l")
    a._right_pane_id = "%9"
    a._right_pane_claude = "cc-x"
    session_calls = []
    pane_calls = []
    monkeypatch.setattr(tmux_ctl, "server_snapshot", lambda: None)
    monkeypatch.setattr(
        tmux_ctl,
        "session_exists",
        lambda name: session_calls.append(name) or True,
    )
    monkeypatch.setattr(
        tmux_ctl,
        "pane_alive",
        lambda pane: pane_calls.append(pane) or True,
    )

    a._refresh()

    assert session_calls == ["cc-x", "cc-x"]
    assert pane_calls == ["%9"]


def test_refresh_scans_codex_once_and_uses_cached_queries(app, monkeypatch):
    a, _Running = app
    project = Project(
        real_path=Path("/tmp/codex-project"),
        encoded_name="-tmp-codex-project",
        claude_dir=Path(),
        session_count=0,
        last_activity_ts=0.0,
    )
    a._codex_mode = True
    a._running[_UID] = _Running(
        key=_UID,
        tmux_name="cx-x",
        label="codex",
        project=project,
    )

    class CodexProbe:
        def __init__(self):
            self.refresh_calls = 0
            self.all_cwds_calls = []
            self.get_calls = []

        def refresh(self):
            self.refresh_calls += 1

        def all_cwds(self, *, refresh=True):
            self.all_cwds_calls.append(refresh)
            return {}

        def get(self, session_id, *, refresh=True):
            self.get_calls.append((session_id, refresh))
            return None

    probe = CodexProbe()
    a._codex_index = probe
    monkeypatch.setattr(
        tmux_ctl,
        "server_snapshot",
        lambda: tmux_ctl.ServerSnapshot(
            sessions=frozenset({"cx-x"}),
            panes=frozenset(),
        ),
    )
    monkeypatch.setattr(
        a._session_cache,
        "get",
        lambda *_args: pytest.fail("Codex entry queried Claude cache"),
    )

    a._refresh()

    assert probe.refresh_calls == 1
    assert probe.all_cwds_calls == [False]
    assert probe.get_calls == [(_UID, False)]


def test_visible_projects_reuses_short_lived_snapshot(app, monkeypatch):
    a, _Running = app
    cached = Project(
        real_path=Path("/tmp/cached"),
        encoded_name="-tmp-cached",
        claude_dir=Path("/tmp/meta-cached"),
        session_count=1,
        last_activity_ts=1.0,
    )
    refreshed = Project(
        real_path=Path("/tmp/refreshed"),
        encoded_name="-tmp-refreshed",
        claude_dir=Path("/tmp/meta-refreshed"),
        session_count=2,
        last_activity_ts=2.0,
    )
    now = [101.0]
    calls = []
    a._project_snapshot = [cached]
    a._project_snapshot_at = 100.0
    monkeypatch.setattr(
        "railmux.ui.app.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "railmux.ui.app.list_projects",
        lambda _home: calls.append(True) or [refreshed],
    )

    assert a._visible_projects() == [cached]
    assert calls == []

    now[0] = 104.0
    assert a._visible_projects() == [refreshed]
    assert calls == [True]


def test_visible_projects_force_bypasses_snapshot(app, monkeypatch):
    a, _Running = app
    a._project_snapshot = []
    a._project_snapshot_at = 100.0
    monkeypatch.setattr(
        "railmux.ui.app.time.monotonic", lambda: 101.0)
    calls = []
    monkeypatch.setattr(
        "railmux.ui.app.list_projects",
        lambda _home: calls.append(True) or [],
    )

    assert a._visible_projects(force=True) == []
    assert calls == [True]
