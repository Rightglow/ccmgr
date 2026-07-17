from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from railmux import orphan_marker, restart_state, tmux_ctl
from railmux.models import Project, SessionMeta
from railmux.ui.app import App, _Running
from railmux.ui.running_pane import RunningEntry


def _owner(pane: str = "%1") -> restart_state.OuterTmuxIdentity:
    return restart_state.OuterTmuxIdentity(
        server_digest="a" * 64,
        server_pid=123,
        pane_id=pane,
        session_id="$1",
        window_id="@1",
    )


def _pane(name: str = "cx-new---token-1") -> tmux_ctl.PaneIdentity:
    return tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=999, session_name=name,
        session_id="$9", window_id="@9", dead=False,
        width=80, height=24,
    )


def _marker(*, phase: str = "unresolved", owner=None,
            session_id: str | None = None) -> orphan_marker.Marker:
    return orphan_marker.Marker(
        mode_key="codex",
        placeholder_key="__new__-token-1",
        tmux_name="cx-new---token-1",
        tmux_session_id="$9",
        tmux_pane_id="%9",
        owner=owner or _owner(),
        cwd=Path("/work/project"),
        created_at=1000.0,
        creation_token="b" * 32,
        phase=phase,
        session_id=session_id,
    )


def _app() -> App:
    app = App.__new__(App)
    app._restart_identity = _owner()
    app._running = {}
    app._claude_home = Path("/missing/claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {}
    app._codex_index.get.return_value = None
    app._session_cache = MagicMock()
    app._set_status = MagicMock()
    return app


def test_marker_round_trip_is_bounded_and_contains_no_launch_payload():
    marker = _marker()
    raw = orphan_marker.encode(marker)
    assert orphan_marker.decode(raw) == marker
    data = json.loads(raw)
    assert len(raw.encode()) < orphan_marker.MAX_BYTES
    assert not ({"command", "env", "prompt", "transcript", "credentials"}
                & set(data))


def test_marker_rejects_corrupt_shape_and_phase_uuid_mismatch():
    assert orphan_marker.decode("not json") is None
    raw = json.loads(orphan_marker.encode(_marker()))
    raw["session_id"] = "wrongly-known"
    assert orphan_marker.decode(json.dumps(raw)) is None
    raw["phase"] = "future"
    assert orphan_marker.decode(json.dumps(raw)) is None
    assert orphan_marker.decode("{" + "x" * orphan_marker.MAX_BYTES + "}") is None


def test_launch_marks_exact_holder_before_provider(monkeypatch):
    app = _app()
    app._codex_index.sessions_for_cwd.return_value = []
    app._session_name = lambda key: "cx-new---token-1"
    app._shellify = lambda *a, **k: "provider-command"
    app._attach_in_right_pane = lambda *a, **k: True
    app._clear_error = lambda: None
    app._show_error = MagicMock()
    events: list[str] = []
    holder = _pane()
    monkeypatch.setattr(tmux_ctl, "session_exists", lambda name: False)
    monkeypatch.setattr(
        tmux_ctl, "create_detached_holder",
        lambda *a, **k: (events.append("holder") or holder, None),
    )
    monkeypatch.setattr(
        tmux_ctl, "start_detached_holder",
        lambda *a, **k: (events.append("provider") or True, None),
    )
    app._write_orphan_marker = (
        lambda marker: events.append(f"marker:{marker.phase}") or True)
    app._stamp_running = lambda running: True

    assert app._launch(
        "__new__-token-1", ["codex"], Path("/work/project"), "p/(new)",
        None, placeholder_path=Path("/work/project"), session_type="codex",
    )
    assert events == ["holder", "marker:launching", "provider",
                      "marker:unresolved"]
    running = app._running["__new__-token-1"]
    assert running.orphan is not None
    assert running.orphan.tmux_session_id == "$9"
    assert running.orphan.phase == "unresolved"


def test_marker_failure_kills_only_holder_and_never_starts_provider(monkeypatch):
    app = _app()
    app._codex_index.sessions_for_cwd.return_value = []
    app._session_name = lambda key: "cx-new---token-1"
    app._shellify = lambda *a, **k: "provider-command"
    app._show_error = MagicMock()
    holder = _pane()
    killed: list[tmux_ctl.PaneIdentity] = []
    started = MagicMock()
    monkeypatch.setattr(tmux_ctl, "session_exists", lambda name: False)
    monkeypatch.setattr(tmux_ctl, "create_detached_holder",
                        lambda *a, **k: (holder, None))
    monkeypatch.setattr(tmux_ctl, "kill_session_identity",
                        lambda identity: killed.append(identity) or True)
    monkeypatch.setattr(tmux_ctl, "start_detached_holder", started)
    app._write_orphan_marker = lambda marker: False

    assert not app._launch(
        "__new__-token-1", ["codex"], Path("/work/project"), "p/(new)",
        None, placeholder_path=Path("/work/project"), session_type="codex",
    )
    assert killed == [holder]
    started.assert_not_called()


def test_discovery_readopts_unresolved_without_provider_directory(monkeypatch):
    marker = _marker()
    app = _app()
    row = (f"{marker.tmux_name}\t/holder/cwd\t1000\t$9\t%9\t"
           f"{orphan_marker.encode(marker)}\t")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    monkeypatch.setattr(
        tmux_ctl, "pane_identity",
        lambda pane_id: _pane() if pane_id == "%9" else _pane("outer"),
    )

    assert app._discover_orphans()
    running = app._running[marker.placeholder_key]
    assert running.is_placeholder
    assert running.status == "blocked"
    assert running.allow_heuristic_resolution is False
    assert running.placeholder_path == marker.cwd


def test_discovery_does_not_steal_from_live_other_owner(monkeypatch):
    marker = _marker(owner=_owner("%2"))
    app = _app()
    app._codex_index.all_cwds.return_value = {marker.cwd: 1}
    legacy = json.dumps({
        "key": marker.placeholder_key,
        "tmux_name": marker.tmux_name,
        "session_type": "codex",
        "cwd": str(marker.cwd),
        "created_at": marker.created_at,
        "pre_launch_complete": False,
    })
    row = (f"{marker.tmux_name}\t/work/project\t1000\t$9\t%9\t"
           f"{orphan_marker.encode(marker)}\t{legacy}")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: tmux_ctl.ServerSnapshot(
            sessions=frozenset({marker.tmux_name}),
            panes=frozenset({"%2", "%9"}),
        ),
    )
    monkeypatch.setattr(
        tmux_ctl, "pane_identity",
        lambda pane_id: _pane() if pane_id == "%9" else _pane("other-owner"),
    )
    assert app._discover_orphans()
    assert app._running == {}


def test_owner_takeover_fails_closed_without_complete_snapshot(monkeypatch):
    marker = _marker(owner=_owner("%2"))
    app = _app()
    row = (f"{marker.tmux_name}\t/work/project\t1000\t$9\t%9\t"
           f"{orphan_marker.encode(marker)}\t")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda pane_id: _pane())
    monkeypatch.setattr(tmux_ctl, "server_snapshot", lambda: None)
    assert app._discover_orphans()
    assert app._running == {}


def test_dead_owner_takeover_claims_marker_before_adoption(monkeypatch):
    marker = _marker(owner=_owner("%2"))
    app = _app()
    row = (f"{marker.tmux_name}\t/work/project\t1000\t$9\t%9\t"
           f"{orphan_marker.encode(marker)}\t")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda pane_id: _pane())
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot",
        lambda: tmux_ctl.ServerSnapshot(
            sessions=frozenset({marker.tmux_name}), panes=frozenset({"%9"})),
    )
    claims: list[tuple[orphan_marker.Marker, object]] = []
    monkeypatch.setattr(
        orphan_marker, "claim_owner",
        lambda value, current, load, store: (
            claims.append((value, current)) or value.with_owner(current)),
    )
    assert app._discover_orphans()
    running = app._running[marker.placeholder_key]
    assert claims == [(marker, app._restart_identity)]
    assert running.orphan is not None
    assert running.orphan.owner == app._restart_identity


def test_owner_claim_lock_allows_only_one_concurrent_takeover(
    monkeypatch, tmp_path,
):
    marker = _marker(owner=_owner("%2"))
    first = _owner("%10")
    second = _owner("%11")
    monkeypatch.setattr(restart_state, "runtime_base", lambda: tmp_path)
    storage = {orphan_marker.OPTION_NAME: orphan_marker.encode(marker)}
    entered = threading.Event()
    release = threading.Event()
    first_result: list[orphan_marker.Marker | None] = []

    def load(_session: str, option: str) -> str | None:
        return storage.get(option)

    def store(_session: str, option: str, raw: str | None) -> bool:
        storage[option] = raw
        entered.set()
        assert release.wait(timeout=2)
        return True

    thread = threading.Thread(
        target=lambda: first_result.append(
            orphan_marker.claim_owner(marker, first, load, store)))
    thread.start()
    assert entered.wait(timeout=2)
    second_result = orphan_marker.claim_owner(marker, second, load, store)
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert first_result == [marker.with_owner(first)]
    assert second_result is None
    # Even after the lock is free, compare-current prevents overwriting the
    # winner with a claim based on the stale dead-owner marker.
    assert orphan_marker.claim_owner(marker, second, load, store) is None


def test_corrupt_v2_marker_never_falls_back_to_ownerless_legacy(monkeypatch):
    marker = _marker()
    app = _app()
    app._codex_index.all_cwds.return_value = {marker.cwd: 1}
    legacy = json.dumps({
        "key": marker.placeholder_key,
        "tmux_name": marker.tmux_name,
        "session_type": "codex",
        "cwd": str(marker.cwd),
        "created_at": marker.created_at,
        "pre_launch_complete": False,
    })
    row = (f"{marker.tmux_name}\t/work/project\t1000\t$9\t%9\t"
           f"{{corrupt-marker\t{legacy}")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    assert app._discover_orphans()
    assert app._running == {}


def test_claude_marker_without_data_dir_uses_safe_expected_project_dir(
    monkeypatch,
):
    marker = replace(
        _marker(), mode_key="claude", tmux_name="cc-new---token-1")
    app = _app()
    row = (f"{marker.tmux_name}\t/holder/cwd\t1000\t$9\t%9\t"
           f"{orphan_marker.encode(marker)}\t")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: row)
    monkeypatch.setattr("railmux.ui.app.list_projects", lambda path: [])
    monkeypatch.setattr(
        tmux_ctl, "pane_identity",
        lambda pane_id: _pane(marker.tmux_name)
        if pane_id == "%9" else _pane("outer"),
    )
    assert app._discover_orphans()
    project = app._running[marker.placeholder_key].project
    assert project is not None
    assert project.claude_dir.parent == app._claude_home / "projects"
    assert project.claude_dir != Path()


def test_resolution_commits_marker_before_registry(monkeypatch):
    marker = _marker()
    project = Project(Path("/work/project"), "work-project", Path(), 1, 1)
    session_id = "11111111-1111-1111-1111-111111111111"
    meta = SessionMeta(
        project=project, session_id=session_id,
        jsonl_path=Path("/sessions/rollout.jsonl"), title="mine",
        message_count=1, token_total=1, last_mtime=1001,
        session_type="codex",
    )
    app = _app()
    app._running = {
        marker.placeholder_key: _Running(
            key=marker.placeholder_key, tmux_name=marker.tmux_name,
            label="p/(unresolved)", project=project,
            placeholder_path=project.real_path, created_at=marker.created_at,
            session_type="codex", orphan=marker,
        )
    }
    app._codex_index.sessions_for_cwd.return_value = [meta]
    app._correlate_codex_rollout = lambda running: {session_id}
    app._stamp_running = lambda running: True
    writes: list[orphan_marker.Marker] = []
    app._write_orphan_marker = lambda value: writes.append(value) or False

    app._resolve_placeholders([project])
    assert marker.placeholder_key in app._running
    assert writes[0].phase == "resolved"
    app._write_orphan_marker = lambda value: writes.append(value) or True
    app._resolve_placeholders([project])
    assert session_id in app._running
    assert marker.placeholder_key not in app._running
    assert app._running[session_id].orphan.session_id == session_id


def test_stale_running_row_cannot_attach_reused_name(monkeypatch):
    marker = _marker()
    app = _app()
    app._running = {
        marker.placeholder_key: _Running(
            key=marker.placeholder_key, tmux_name=marker.tmux_name,
            label="p/(unresolved)", orphan=marker,
        )
    }
    app._cancel_pending_double_focus = lambda: None
    app._show_error = MagicMock()
    app._attach_in_right_pane = MagicMock()
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda pane_id: None)
    app._on_running_select(RunningEntry(
        marker.tmux_name, "stale", identity_token=marker.creation_token))
    app._attach_in_right_pane.assert_not_called()
