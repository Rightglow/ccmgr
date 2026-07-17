"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import urwid

from railmux.models import Project, SessionMeta
from railmux import restart_state
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App, _Running
from railmux.ui.modals import QuitConfirmModal


# ── helpers ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_tmux_identity_stamps(monkeypatch, tmp_path):
    """Unit tests never write options into the developer's real tmux server."""
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_session_user_option",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        App, "_portable_state_path",
        staticmethod(lambda: tmp_path / "portable.json"),
    )
    monkeypatch.setattr(
        restart_state, "legacy_state_path",
        lambda: tmp_path / "legacy.json",
    )
    monkeypatch.setattr(
        restart_state, "cleanup_stale_instances", lambda *_args, **_kwargs: 0,
    )

def _project(name: str = "test-proj", claude_dir: Path | None = None) -> Project:
    return Project(
        real_path=Path(f"/tmp/{name}"),
        encoded_name=f"-tmp-{name}",
        claude_dir=claude_dir or Path(f"/tmp/{name}/.claude/projects/-tmp-{name}"),
        session_count=3,
        last_activity_ts=1000.0,
    )


def _minimal_app(*, selected_project=None):
    """Return a bare App instance with just enough attrs for the method under test."""
    app = App.__new__(App)
    app._selected_project = selected_project
    app._restart_identity = OuterTmuxIdentity(
        server_digest="a" * 64,
        server_pid=123,
        pane_id="%1",
        session_id="$1",
        window_id="@1",
    )
    app._running = {}
    app._codex_mode = False
    app._claude_home = Path.home() / ".claude"
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.return_value = []
    app._status = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._currently_focused_session_meta = MagicMock(return_value=None)
    app._in_history_mode = False
    app._right_pane_claude = None
    app._active_session_id = None
    return app


# ── _resolve_truncated_id ────────────────────────────────────────────────

def test_resolve_truncated_id_finds_full_uuid(tmp_path):
    """Given a truncated key, return the full session_id from .jsonl files."""
    proj = _project(claude_dir=tmp_path)
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    (tmp_path / f"{full_id}.jsonl").write_text("{}")
    (tmp_path / "other.jsonl").write_text("{}")

    result = App._resolve_truncated_id("ae54affd-ec33-46", proj)
    assert result == full_id


def test_resolve_truncated_id_no_match(tmp_path):
    proj = _project(claude_dir=tmp_path)
    (tmp_path / "ae54affd-ec33-465c-b3c4-c1dc7c46990b.jsonl").write_text("{}")

    result = App._resolve_truncated_id("zzzzzzzz-zzzz-zz", proj)
    assert result is None


def test_resolve_truncated_id_empty_dir(tmp_path):
    proj = _project(claude_dir=tmp_path)
    result = App._resolve_truncated_id("anything", proj)
    assert result is None


def test_resolve_truncated_id_skips_non_jsonl(tmp_path):
    proj = _project(claude_dir=tmp_path)
    (tmp_path / "readme.txt").write_text("hello")
    result = App._resolve_truncated_id("anything", proj)
    assert result is None


# ── _safe_name ───────────────────────────────────────────────────────────

def test_safe_name_truncates():
    assert App._safe_name("ae54affd-ec33-465c-b3c4-c1dc7c46990b", 16) == "ae54affd-ec33-46"


def test_safe_name_replaces_non_alnum():
    assert App._safe_name("abc def!ghi", 10) == "abc-def-gh"


def test_safe_name_strips_leading_dashes():
    assert App._safe_name("---abc", 10) == "abc"


# ── state file ───────────────────────────────────────────────────────────

def test_state_path_uses_xdg_runtime_dir(monkeypatch):
    monkeypatch.setitem(os.environ, "XDG_RUNTIME_DIR", "/run/user/1000")
    assert restart_state.instances_dir() == Path(
        "/run/user/1000/railmux/instances")


def test_state_path_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    assert restart_state.instances_dir() == Path(
        "/tmp/railmux-1000/railmux/instances")


def test_save_and_load_state_round_trip(tmp_path, monkeypatch):
    """_save_state writes JSON; _load_state reads it back."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))

    app = _minimal_app(selected_project=_project("myproj"))
    app._save_state()
    assert (tmp_path / "state.json").is_file()

    data = app._load_state()
    assert data["project"] == "-tmp-myproj"
    assert data["right_kind"] == "empty"


def test_local_view_wins_over_shared_portable_view(tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable-shared.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path))
    app = _minimal_app(selected_project=_project("one"))
    app._projects_pane = MagicMock(filter_text="mine")
    app._sessions_pane = MagicMock(filter_text="")
    app._save_state()
    restart_state.write_portable({
        "schema_version": 1,
        "kind": "portable",
        "view": restart_state.build_view(
            {"mode": "codex", "project": "-tmp-other",
             "session_filter": "foreign-filter"}),
    }, portable_path)

    data = app._load_state()

    assert data["mode"] == "claude"
    assert data["project"] == "-tmp-one"
    assert data["project_filter"] == "mine"
    assert "session_filter" not in data


def test_foreign_local_owner_is_ignored_without_process_restore(
        tmp_path, monkeypatch):
    local_path = tmp_path / "foreign.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path))
    app = _minimal_app()
    foreign = OuterTmuxIdentity(
        "b" * 64, 456, "%9", "$9", "@9")
    restart_state.write_instance(foreign, {
        "schema_version": 1,
        "kind": "instance",
        "owner": foreign.to_json(),
        "view": restart_state.build_view({"mode": "codex"}),
        "recovery": {
            "right_kind": "agent",
            "right_tmux": "cx-foreign",
        },
    }, local_path)
    restart_state.write_portable({
        "schema_version": 1,
        "kind": "portable",
        "view": restart_state.build_view({"mode": "claude"}),
    }, portable_path)

    data = app._load_state()

    assert data == {"mode": "claude"}
    assert "right_tmux" not in data


def test_state_saves_are_independent_when_one_destination_fails(
        tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path))
    app = _minimal_app()
    monkeypatch.setattr(restart_state, "write_portable", lambda *_a, **_k: False)

    app._save_state()

    assert local_path.exists()


def test_save_state_always_writes_right_kind(tmp_path, monkeypatch):
    """Even without a selected project, _save_state records the right-pane state."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=None)
    app._save_state()
    assert (tmp_path / "state.json").is_file()
    data = app._load_state()
    assert data == {"right_kind": "empty", "mode": "claude"}


def test_save_state_with_claude_in_right_pane(tmp_path, monkeypatch):
    """When a Claude session is open, save its tmux name."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=_project("myproj"))
    app._right_pane_claude = "cc-abc123"
    app._save_state()
    data = app._load_state()
    assert data["right_kind"] == "agent"
    assert data["right_tmux"] == "cc-abc123"


def test_save_state_with_preview_in_right_pane(tmp_path, monkeypatch):
    """When a transcript preview is showing, save the session id."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=_project("myproj"))
    app._in_history_mode = True
    app._active_session_id = "abc123"
    app._save_state()
    data = app._load_state()
    assert data["right_kind"] == "preview"
    assert data["right_session"] == "abc123"


def test_save_state_persists_codex_mode(tmp_path, monkeypatch):
    """Restart records the stable provider registry key."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=_project("myproj"))
    app._codex_mode = True
    app._save_state()
    data = app._load_state()
    assert data["mode"] == "codex"


def test_save_state_persists_real_binding_with_placeholder_tmux_name(
        tmp_path, monkeypatch):
    """Resolution re-keys the registry but intentionally keeps cx-new---*.

    The soft-restart state must retain that otherwise-invisible association.
    """
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    project = _project("codex-proj")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-proj/resolved",
        project=project,
        session_type="codex",
    )

    app._save_state()

    data = app._load_state()
    assert data["running_bindings_version"] == 1
    assert data["running_bindings"] == [{
        "key": session_id,
        "tmux_name": "cx-new---abcdef-1",
        "session_type": "codex",
        "cwd": str(project.real_path),
    }]


def test_save_state_persists_unresolved_placeholder_context(
        tmp_path, monkeypatch):
    """macOS needs launch context to resume safe heuristic resolution."""
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    project = _project("codex-proj")
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-proj/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=1234.5,
        pre_launch_ids=frozenset({"old-b", "old-a"}),
        session_type="codex",
    )

    app._save_state()

    binding = app._load_state()["running_bindings"][0]
    assert binding["key"] == key
    assert binding["created_at"] == 1234.5
    assert binding["pre_launch_ids"] == ["old-a", "old-b"]


def test_load_state_without_codex_mode_defaults_falsy(tmp_path, monkeypatch):
    """Ownerless legacy state migrates view-only and defaults to Claude."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"project": "-tmp-myproj", "right_kind": "empty"}))
    app = _minimal_app()
    monkeypatch.setattr(restart_state, "legacy_state_path", lambda: p)

    data = app._load_state()

    assert data == {"mode": "claude", "project": "-tmp-myproj"}
    assert p.exists()  # ownerless source remains available for manual cleanup


def test_enter_codex_mode_on_restore_applies_filter(monkeypatch):
    """_enter_codex_mode_on_restore flips the mode, loads the Codex filter and
    repaints the Projects pane with the Codex-visible set."""
    app = App.__new__(App)
    app._codex_mode = False
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {Path("/tmp/myproj"): 2}
    app._projects_pane = MagicMock()
    monkeypatch.setattr(app, "_visible_projects", lambda *a, **k: ["visible-proj"])

    app._enter_codex_mode_on_restore()

    assert app._codex_mode is True
    assert app._codex_project_filter == {Path("/tmp/myproj"): 2}
    app._projects_pane.set_projects.assert_called_once_with(["visible-proj"])


def test_toggle_codex_mode_round_trip_uses_cached_snapshot(monkeypatch):
    """A rapid Claude-Codex-Claude round trip never scans NFS on the UI path.

    It paints the warm snapshot immediately and schedules one background refresh.
    """
    import time as _time
    proj = _project("myproj")
    app = App.__new__(App)
    app._codex_mode = False
    app._selected_project = None
    app._project_snapshot = [proj]
    snapshot_at = _time.monotonic()
    app._project_snapshot_at = snapshot_at
    app._running = {}
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._codex_index = MagicMock()
    app._codex_project_filter = {proj.real_path: 1}
    app._codex_index.sessions_for_cwd.return_value = []
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.return_value = []
    app._claude_home = Path.home() / ".claude"
    schedule_refresh = MagicMock()
    monkeypatch.setattr(app, "_apply_tmux_bar", lambda *a, **k: None)
    monkeypatch.setattr(app, "_set_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_mode_data_refresh", schedule_refresh)

    with patch("railmux.ui.app.list_projects",
               side_effect=AssertionError("toggle forced an NFS rescan")):
        app._toggle_codex_mode()
        app._toggle_codex_mode()

    assert app._codex_mode is False
    schedule_refresh.assert_called_once_with()
    assert app._project_snapshot_at == snapshot_at


def test_load_state_missing_file_returns_none():
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=Path("/tmp/railmux-nonexistent.json")):
        assert app._load_state() is None


def test_load_state_invalid_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=p):
        assert app._load_state() is None


def test_load_state_rejects_non_object_json(tmp_path):
    p = tmp_path / "bad-shape.json"
    p.write_text("[]")
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=p):
        assert app._load_state() is None


def test_newer_state_schemas_are_ignored_and_never_overwritten(
        tmp_path, monkeypatch):
    local = tmp_path / "local.json"
    portable = tmp_path / "portable.json"
    newer_portable = {"schema_version": 2, "kind": "portable"}
    newer_local = {"schema_version": 2, "kind": "instance"}
    portable.write_text(json.dumps(newer_portable))
    local.write_text(json.dumps(newer_local))
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable))
    app = _minimal_app()

    assert app._load_state() is None
    app._save_state()

    assert json.loads(portable.read_text()) == newer_portable
    assert json.loads(local.read_text()) == newer_local


# ── _discover_orphans parsing ────────────────────────────────────────────

def test_discover_orphans_finds_cc_sessions():
    """A cc-* tmux session in a known project is added to _running."""
    proj = _project("myproj")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)

    with patch("subprocess.check_output",
               return_value=f"cc-{truncated}\t/tmp/myproj\nrailmux\t/home/user\n"), \
         patch("railmux.ui.app.list_projects", return_value=[proj]), \
         patch.object(App, "_resolve_truncated_id",
                      return_value=full_id):
        app = _minimal_app()
        app._discover_orphans()

    assert full_id in app._running
    assert app._running[full_id].tmux_name == f"cc-{truncated}"
    assert app._running[full_id].project is proj


def test_discover_orphans_finds_codex_only_project():
    """A cx-* session without a Claude project is re-adopted through a
    synthetic project built from the Codex index."""
    cwd = Path("/tmp/codex-only")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._resolve_truncated_codex_id = MagicMock(return_value=full_id)

    with patch("subprocess.check_output",
               return_value=f"cx-{truncated}\t{cwd}\n"), \
         patch("railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans()

    running = app._running[full_id]
    assert running.session_type == "codex"
    assert running.project.real_path == cwd
    assert running.project.claude_dir == Path()


def _codex_meta(project: Project, session_id: str) -> SessionMeta:
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=Path("/tmp/rollout.jsonl"),
        title="Recovered",
        message_count=1,
        token_total=1,
        last_mtime=1000.0,
        status="idle",
        session_type="codex",
    )


def test_discover_orphans_recovers_codex_placeholder_from_procfs(monkeypatch):
    """A state-free Linux restart re-adopts the exact live rollout writer."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_mode = True
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.side_effect = (
        lambda candidate, refresh=False: meta if candidate == session_id else None)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids",
        lambda name, root: {session_id},
    )

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert not app._running[session_id].is_placeholder


def test_discover_orphans_restores_persisted_binding_without_procfs(
        monkeypatch):
    """A validated state binding is the cross-platform soft-restart path."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = meta
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: None)
    state = {
        "running_bindings_version": 1,
        "running_bindings": [{
            "key": session_id,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
        }],
    }

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans(state)

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert app._running[session_id].label.endswith("/Recovered")


def test_discover_orphans_prefers_valid_tmux_stamp_without_procfs(monkeypatch):
    """The live session-local stamp is the primary cross-platform identity."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = meta
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: None)
    stamp = json.dumps({
        "key": session_id,
        "tmux_name": "cx-new---abcdef-1",
        "session_type": "codex",
        "cwd": str(cwd),
    }, separators=(",", ":"), sort_keys=True)

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"


def test_stamp_running_writes_session_local_identity(monkeypatch):
    project = _project("codex-only")
    running = _Running(
        key="12345678-1234-1234-1234-1234567890ab",
        tmux_name="cx-new---abcdef-1",
        label="label",
        project=project,
        session_type="codex",
    )
    app = _minimal_app()
    set_option = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_session_user_option", set_option)

    assert app._stamp_running(running) is True

    tmux_name, option, raw = set_option.call_args.args
    assert tmux_name == running.tmux_name
    assert option == "@railmux_binding_v1"
    assert json.loads(raw)["key"] == running.key


def test_discover_orphans_restores_unresolved_placeholder_state(monkeypatch):
    """Launch snapshots survive restart until normal polling can resolve them."""
    cwd = Path("/tmp/codex-only")
    key = "__new__-abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    state = {
        "running_bindings_version": 1,
        "running_bindings": [{
            "key": key,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
            "created_at": 123.0,
            "pre_launch_ids": ["old-session"],
        }],
    }

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans(state)

    running = app._running[key]
    assert running.is_placeholder
    assert running.created_at == 123.0
    assert running.pre_launch_ids == frozenset({"old-session"})


def test_unresolved_stamp_merges_state_pre_launch_fence(monkeypatch):
    """Stamp identity must not discard the macOS anti-misbinding snapshot."""
    cwd = Path("/tmp/codex-only")
    key = "__new__-abcdef-1"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    stamp = json.dumps({
        "key": key,
        "tmux_name": "cx-new---abcdef-1",
        "session_type": "codex",
        "cwd": str(cwd),
        "created_at": 123.0,
    }, separators=(",", ":"), sort_keys=True)
    state = {
        "running_bindings_version": 1,
        "running_bindings": [{
            "key": key,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
            "created_at": 123.0,
            "pre_launch_ids": ["old-session"],
        }],
    }

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans(state)

    assert app._running[key].pre_launch_ids == frozenset({"old-session"})


def test_discover_orphans_rejects_persisted_binding_with_wrong_cwd(
        monkeypatch):
    """A stale/untrusted state file cannot bind a live tmux from another cwd."""
    live_cwd = Path("/tmp/live")
    saved_cwd = Path("/tmp/saved")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {live_cwd: 1, saved_cwd: 1}
    app._codex_index.get.return_value = None
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids", lambda *a: set())
    state = {
        "running_bindings_version": 1,
        "running_bindings": [{
            "key": session_id,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(saved_cwd),
        }],
    }

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{live_cwd}\t100\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        complete = app._discover_orphans(state)

    assert app._running == {}
    assert complete is False


def test_discover_orphans_duplicate_uuid_keeps_oldest_writer(monkeypatch):
    """Historical duplicate resumes never replace the original live writer."""
    cwd = Path("/tmp/codex-only")
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.side_effect = (
        lambda candidate, refresh=False: meta if candidate == session_id else None)
    app._resolve_truncated_codex_id = MagicMock(return_value=session_id)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_rollout_ids",
        lambda name, root: {session_id},
    )
    stable = f"cx-{App._safe_name(session_id, 16)}"
    output = (
        f"{stable}\t{cwd}\t200\n"
        f"cx-new---abcdef-1\t{cwd}\t100\n"
    )

    with patch("subprocess.check_output", return_value=output), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans()

    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"


def test_restore_right_pane_refuses_unrepresented_live_tmux(monkeypatch):
    """Pane restoration cannot bypass the exactly-once running registry."""
    app = _minimal_app()
    app._attach_in_right_pane = MagicMock(return_value=True)
    app._set_status = MagicMock()
    app._show_error = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    restored = app._restore_right_pane({
        "right_kind": "agent",
        "right_tmux": "cx-untracked",
    })

    assert restored is False
    app._attach_in_right_pane.assert_not_called()


def test_pending_restore_retains_state_after_incomplete_running_recovery(
        tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    app = _minimal_app()
    app._pending_restore_state = {"right_kind": "empty"}
    app._running_recovery_ok = False
    app._restore_right_pane = MagicMock(return_value=True)
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: state_path))

    app._restore_pending_right_pane(None, None)

    assert state_path.exists()


def test_launch_resume_attaches_recovered_writer_instead_of_resuming_again():
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    running = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/Recovered",
        project=project,
        session_type="codex",
    )
    app = App.__new__(App)
    app._running = {session_id: running}
    app._agent_session_alive = MagicMock(return_value=True)
    app._on_running_select = MagicMock()
    app._launch = MagicMock()

    app._launch_resume(meta)

    app._on_running_select.assert_called_once()
    app._launch.assert_not_called()


def test_launch_resume_promotes_recovered_placeholder_before_resume():
    """Linux exact correlation closes the pre-poll duplicate-writer window."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=999.0,
        session_type="codex",
    )
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [meta]
    app._correlate_codex_rollout = lambda _running: {session_id}
    app._discover_orphans = MagicMock(return_value=True)
    app._agent_session_alive = MagicMock(return_value=True)
    app._on_running_select = MagicMock()
    app._launch = MagicMock()

    app._launch_resume(meta)

    assert session_id in app._running and key not in app._running
    app._on_running_select.assert_called_once()
    app._launch.assert_not_called()


def test_launch_resume_refuses_ambiguous_live_placeholder_without_procfs():
    """When exact identity is unknowable, fail closed instead of duplicating."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    meta = _codex_meta(project, session_id)
    key = "__new__-abcdef-1"
    app = _minimal_app(selected_project=project)
    app._running[key] = _Running(
        key=key,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/(new)",
        project=project,
        placeholder_path=project.real_path,
        created_at=999.0,
        session_type="codex",
        allow_heuristic_resolution=False,
    )
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [meta]
    app._correlate_codex_rollout = lambda _running: None
    app._discover_orphans = MagicMock(return_value=True)
    app._agent_session_alive = MagicMock(return_value=True)
    app._launch = MagicMock()
    app._set_status = MagicMock()
    app._show_error = MagicMock()

    app._launch_resume(meta)

    assert key in app._running and session_id not in app._running
    app._launch.assert_not_called()
    app._show_error.assert_called_once()


def test_launch_refuses_untracked_preexisting_tmux(monkeypatch):
    """The final launch gate cannot stamp or reuse an identity collision."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._running = {}
    app._session_name = lambda _key: "cx-12345678-1234-12"
    app._set_status = MagicMock()
    app._show_error = MagicMock()
    app._ensure_detached_agent = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    launched = app._launch(
        session_id, ["codex"], project.real_path, "label", project,
        session_type="codex",
    )

    assert launched is False
    app._ensure_detached_agent.assert_not_called()


def test_discover_orphans_skips_placeholder():
    """__new__-N tmux sessions are skipped (handled by the normal poll)."""
    proj = _project()
    with patch("subprocess.check_output",
               return_value="cc-__new__-1\t/tmp/test-proj\n"), \
         patch("railmux.ui.app.list_projects", return_value=[proj]):
        app = _minimal_app()
        app._discover_orphans()
    assert len(app._running) == 0


def test_discover_orphans_skips_already_running():
    """Already-tracked sessions are not re-added."""
    proj = _project("myproj")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)

    with patch("subprocess.check_output",
               return_value=f"cc-{truncated}\t/tmp/myproj\n"), \
         patch("railmux.ui.app.list_projects", return_value=[proj]), \
         patch.object(App, "_resolve_truncated_id", return_value=full_id):
        app = _minimal_app()
        app._running[full_id] = _Running(key=full_id, tmux_name=f"cc-{truncated}",
                                          label="existing", project=proj)
        app._discover_orphans()
    assert app._running[full_id].label == "existing"  # not overwritten


def test_discover_orphans_skips_railmux():
    """The railmux outer tmux session is not treated as an orphan."""
    with patch("subprocess.check_output", return_value="railmux\t/home/user\n"):
        app = _minimal_app()
        app._discover_orphans()
    assert len(app._running) == 0


def test_discover_orphans_handles_tmux_error():
    """If tmux list-sessions fails, quietly return with no orphans."""
    with patch("subprocess.check_output", side_effect=OSError("no tmux")):
        app = _minimal_app()
        app._discover_orphans()  # should not raise
    assert len(app._running) == 0


# ── _teardown_tmux branching ────────────────────────────────────────────

def test_teardown_soft_quit_skips_session_kill():
    """With _soft_quit_flag set, cc-* and outer tmux sessions are left alive."""
    app = _minimal_app()
    app._soft_quit_flag = True
    app._right_pane_id = "%5"
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(key="abc123", tmux_name="cc-abc123", label="test", project=None),
    }

    with patch("railmux.ui.app.tmux_ctl") as tmux, \
         patch("railmux.display_transport.tmux_ctl", tmux):
        app._teardown_tmux()

    # Right-pane cleanup still happens.
    tmux.kill_pane.assert_called_once_with("%5")
    # Session kill must NOT be called.
    tmux.kill_session.assert_not_called()


def test_teardown_hard_quit_kills_sessions():
    """Without the flag, cc-* sessions are killed."""
    app = _minimal_app()
    app._soft_quit_flag = False
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(key="abc123", tmux_name="cc-abc123", label="test", project=None),
    }

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    tmux.kill_session.assert_any_call("cc-abc123")


def test_teardown_failed_swap_return_degrades_to_soft_quit():
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None),
    }
    transport = MagicMock()
    transport.close_all.return_value = False
    app._display_transport_manager = transport

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    assert app._soft_quit_flag is True
    tmux.kill_session.assert_not_called()


def test_teardown_reverts_every_bar_option(monkeypatch):
    """Every appearance option railmux paints onto the outer bar — plus the
    dynamically set status-right — is reverted with ``set-option -u`` on
    teardown, so the user's tmux config is left clean. The revert runs BEFORE
    the soft-quit early return (the outer session survives soft quit, so a
    leftover would linger)."""
    app = _minimal_app()
    app._soft_quit_flag = True
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    app._tmux_status_enabled = True
    app._tmux_status_session = "railmux"

    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    with patch("railmux.ui.app.tmux_ctl"):
        app._teardown_tmux()

    reverted = {
        argv[5]
        for c in run.call_args_list
        if (argv := c.args[0])[:4] == ["tmux", "set-option", "-u", "-t"]
    }
    expected = ({opt for opt, _ in App._TMUX_BAR_OPTIONS}
                | set(App._TMUX_BAR_STYLE_OPTIONS) | {"status-right"})
    assert reverted == expected
    # Regression guard: the noisy window list and the unified bar style are
    # among what we set — and therefore must be among what we revert.
    assert {"window-status-format", "window-status-current-format",
            "status-style", "status-left"} <= reverted


def test_run_teardown_reverts_bar_if_setup_raises(monkeypatch):
    """Regression: run() applies the tmux status-bar overrides BEFORE building the
    urwid Screen/MainLoop. If that construction raises, `finally` must still call
    _teardown_tmux, or the user's outer bar keeps railmux's status/style/brand."""
    import railmux.ui.app as app_mod

    app = _minimal_app()
    app._pending_project = None
    app._pending_restore_state = None
    app._config = MagicMock(poll_interval_ms=500)
    app._frame = MagicMock()
    app._hint_bar = MagicMock()
    app._set_railmux_focus = MagicMock()
    teardown = MagicMock()
    app._teardown_tmux = teardown

    monkeypatch.setattr(app_mod.tmux_ctl, "in_tmux", lambda: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "current_session_name", lambda: "railmux")
    monkeypatch.setattr(app_mod.tmux_ctl, "enable_clipboard_passthrough", lambda: None)
    monkeypatch.setattr(app_mod.tmux_ctl, "current_pane_id", lambda: "%0")
    monkeypatch.setattr("subprocess.run", MagicMock())
    # Screen construction blows up AFTER the status bar has been set up.
    monkeypatch.setattr("urwid.raw_display.Screen",
                        MagicMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        app.run()

    assert app._tmux_status_enabled is True  # setup ran (bar was mutated)...
    teardown.assert_called_once_with()       # ...and teardown reverted it


# ── QuitConfirmModal ─────────────────────────────────────────────────────

def _render_text(modal) -> str:
    """Extract all plain text from a QuitConfirmModal's body."""
    pile = modal._wrapped_widget.base_widget.base_widget
    texts = []
    for widget, _ in pile.contents:
        if isinstance(widget, urwid.Text):
            texts.append(widget.text)
    return "\n".join(str(t) for t in texts)


def test_quit_modal_shows_s_option():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=3,
    )
    text = _render_text(modal)
    assert "s = soft quit" in text


def test_quit_modal_soft_quit_key_fires_callback():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: called.append("soft"),
        on_cancel=lambda: None,
        running_count=0,
    )
    result = modal.keypress((20,), "s")
    assert result is None  # consumed
    assert called == ["soft"]


def test_quit_modal_soft_quit_key_upper_case():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: called.append("soft"),
        on_cancel=lambda: None,
    )
    modal.keypress((20,), "S")
    assert called == ["soft"]


def test_quit_modal_soft_quit_none_callback_ignores_s():
    """When on_soft_quit is None, 's' is passed through."""
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=None,
        on_cancel=lambda: None,
    )
    result = modal.keypress((20,), "s")
    assert result == "s"  # not consumed


def test_quit_modal_enter_confirms():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: called.append("hard"),
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
    )
    modal.keypress((20,), "enter")
    assert called == ["hard"]


def test_quit_modal_esc_cancels():
    called = []
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: called.append("cancel"),
    )
    modal.keypress((20,), "esc")
    assert called == ["cancel"]


def test_quit_modal_shows_running_count():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=5,
    )
    text = _render_text(modal)
    assert "5 agent sessions" in text


def test_quit_modal_no_running():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=0,
    )
    text = _render_text(modal)
    assert "No running sessions" in text


# ── Codex placeholder resolution ─────────────────────────────────────────

def _codex_session(project: Project, session_id: str, mtime: float) -> SessionMeta:
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=Path("/tmp/rollout.jsonl"),
        title="Real session",
        message_count=1,
        token_total=1,
        last_mtime=mtime,
        status="idle",
    )


def test_resolve_placeholders_codex_rekeys_to_real_uuid():
    """In Codex mode a `__new__-N` placeholder resolves to its real UUID
    via the Codex index (not the Claude session cache), so clicking the real
    row doesn't spawn a duplicate session and force_projects can clear."""
    proj = _project()
    real_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-1": _Running(
            key="__new__-1", tmux_name="cx-new----1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, real_id, mtime=1000.0)
    ]
    # Codex mode must NOT consult the Claude session cache.
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.side_effect = AssertionError(
        "Codex placeholder resolution queried the Claude cache")

    app._resolve_placeholders([proj])

    assert "__new__-1" not in app._running
    assert real_id in app._running
    entry = app._running[real_id]
    assert entry.tmux_name == "cx-new----1"      # same tmux session, re-keyed
    assert not entry.is_placeholder
    app._codex_index.sessions_for_cwd.assert_called_once_with(
        proj.real_path, refresh=False)


def test_resolve_placeholders_codex_keeps_placeholder_until_jsonl_appears():
    """Before the rollout file exists (no session yet), the placeholder must
    stay a placeholder rather than mis-binding to nothing."""
    proj = _project()
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-1": _Running(
            key="__new__-1", tmux_name="cx-new----1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = []  # nothing on disk yet

    app._resolve_placeholders([proj])

    assert "__new__-1" in app._running
    assert app._running["__new__-1"].is_placeholder


def test_consume_mode_refresh_swaps_both_indexes():
    import threading
    proj = _project()
    index = MagicMock()
    index.all_cwds.return_value = {proj.real_path: 2}
    app = App.__new__(App)
    app._mode_refresh_lock = threading.Lock()
    app._mode_refresh_result = ([proj], index, None)
    app._project_snapshot = None
    app._project_snapshot_at = 0.0
    app._codex_index = MagicMock()
    app._codex_project_filter = {}

    assert app._consume_mode_refresh() is True
    assert app._project_snapshot == [proj]
    assert app._project_snapshot_at > 0.0
    assert app._codex_index is index
    assert app._codex_project_filter == {proj.real_path: 2}


def test_restore_codex_preview_uses_codex_index():
    app = App.__new__(App)
    app._codex_mode = True
    app._selected_project = _project()
    meta = MagicMock()
    meta.session_id = "codex-session"
    meta.jsonl_path = Path("/tmp/codex-rollout.jsonl")
    app._codex_index = MagicMock()
    app._codex_index.get.return_value = meta
    app._session_cache = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()
    app._in_history_mode = False

    app._restore_right_pane({"right_kind": "preview", "right_session": meta.session_id})

    app._codex_index.get.assert_called_once_with(meta.session_id)
    app._session_cache.get.assert_not_called()
    # Restore passes the explicit Codex format hint so a tailed long rollout
    # renders correctly (#5), not just the path.
    app._show_transcript.assert_called_once_with(
        meta.jsonl_path, session_type=meta.session_type)
    app._set_active_target.assert_called_once_with(meta.session_id, None)


# ── #11: placeholder names are namespaced per process (no restart collision)

def test_placeholder_names_are_process_namespaced():
    """Two railmux processes (distinct per-process tokens) never generate the
    same placeholder key OR tmux session name, even though each process's
    counter restarts at 0 — so a fresh launch can't reuse a previous process's
    placeholder name and hijack a surviving orphan tmux session (#11)."""
    def _app(token: str):
        app = App.__new__(App)
        app._proc_token = token
        app._new_session_counter = 0
        app._codex_mode = True
        return app

    a, b = _app("aaaaaa"), _app("bbbbbb")
    # First placeholder of each "process" — identical counter value.
    ka, kb = a._new_placeholder_key(), b._new_placeholder_key()
    assert ka != kb
    assert ka.startswith("__new__-") and kb.startswith("__new__-")
    # Still classified as placeholders.
    assert _Running(key=ka, tmux_name="x", label="l").is_placeholder
    # The token survives _safe_name's 16-char truncation, so the derived tmux
    # session names differ too (the actual collision surface).
    assert a._session_name(ka) != b._session_name(kb)
    assert a._session_name(ka).startswith("cx-")


def test_placeholder_counter_reset_still_unique_across_processes():
    """Within one process the counter increments; across processes the token
    differs — so `process A #1` and `process B #1` never collide."""
    def _app(token: str):
        app = App.__new__(App)
        app._proc_token = token
        app._new_session_counter = 0
        app._codex_mode = False
        return app

    a, b = _app("a1b2c3"), _app("d4e5f6")
    keys = {a._new_placeholder_key(), a._new_placeholder_key(),
            b._new_placeholder_key(), b._new_placeholder_key()}
    assert len(keys) == 4  # all distinct


def test_placeholder_session_name_not_truncated_to_collision():
    """High counters must not collapse to the same tmux name. Placeholders skip
    the 16-char _safe_name truncation, so `__new__-<tok>-1000` and `-10000`
    (which would both truncate to `...-100`) stay distinct (#11)."""
    app = App.__new__(App)
    app._proc_token = "abcdef"
    app._codex_mode = True
    app._new_session_counter = 999
    k1 = app._new_placeholder_key()        # counter -> 1000
    app._new_session_counter = 9999
    k2 = app._new_placeholder_key()        # counter -> 10000
    assert app._session_name(k1) != app._session_name(k2)


# ── #12: placeholder never binds a pre-existing same-cwd rollout ──────────

def test_resolve_placeholders_ignores_pre_existing_cwd_rollout():
    """A rollout that existed in the launch cwd BEFORE this placeholder launched
    (captured in pre_launch_ids) is never bound — even if it is the NEWEST
    session in the cwd — so a placeholder can't hijack another process's
    conversation written to the same cwd (#12)."""
    proj = _project()
    pre_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    new_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new---tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            pre_launch_ids=frozenset({pre_id}),
        )
    }
    app._codex_index = MagicMock()
    # pre_id is the NEWEST session in the cwd; without the fix it would win.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, pre_id, mtime=2000.0),
        _codex_session(proj, new_id, mtime=1001.0),
    ]

    app._resolve_placeholders([proj])

    assert pre_id not in app._running                  # never bound
    assert new_id in app._running                      # our real session bound
    assert "__new__-tok-1" not in app._running
    assert not app._running[new_id].is_placeholder


def test_resolve_placeholders_ambiguous_new_rollouts_not_bound():
    """If TWO new rollouts appear in the launch cwd since our launch, a
    concurrent codex/railmux is writing there and we can't tell which is ours —
    the placeholder stays unresolved rather than binding the wrong one (#12)."""
    proj = _project()
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new----tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            pre_launch_ids=frozenset(),
        )
    }
    app._codex_index = MagicMock()
    # Two brand-new rollouts, both post-launch, both unclaimed → ambiguous.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, "aaaa1111-0000-0000-0000-000000000000", mtime=1001.0),
        _codex_session(proj, "bbbb2222-0000-0000-0000-000000000000", mtime=1002.0),
    ]

    app._resolve_placeholders([proj])

    # Nothing bound; placeholder preserved (safer than mis-binding).
    assert "__new__-tok-1" in app._running
    assert app._running["__new__-tok-1"].is_placeholder


def test_resolve_placeholders_correlation_binds_exact_rollout(monkeypatch):
    """Staggered race (#12): our OWN rollout AND an unrelated newer rollout both
    appear in the launch cwd, so both are candidates. The heuristic would refuse
    (ambiguous). Exact child→rollout correlation — the codex process in the
    placeholder's pane holds OUR rollout open — binds THAT id, not the newer one.
    """
    proj = _project()
    ours = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new----tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1002.0),  # newer, NOT ours
        _codex_session(proj, ours, mtime=1001.0),
    ]
    # Correlation resolves the pane's codex process to OUR rollout fd.
    app._correlate_codex_rollout = lambda r: {ours}

    app._resolve_placeholders([proj])

    assert ours in app._running and not app._running[ours].is_placeholder
    assert unrelated not in app._running          # newer rollout NOT mis-bound
    assert "__new__-tok-1" not in app._running


def test_resolve_placeholders_correlation_waits_when_id_not_yet_candidate(monkeypatch):
    """Correlation KNOWS the exact rollout, but it isn't a bindable candidate
    yet (index lag). Even though an unrelated single rollout is present (which
    the heuristic would bind), we WAIT rather than let the heuristic mis-bind.
    """
    proj = _project()
    ours = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new----tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    # Only the unrelated rollout is indexed so far; ours (held open) isn't yet.
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1001.0),
    ]
    app._correlate_codex_rollout = lambda r: {ours}

    app._resolve_placeholders([proj])

    assert "__new__-tok-1" in app._running          # left unresolved
    assert app._running["__new__-tok-1"].is_placeholder
    assert unrelated not in app._running            # heuristic did NOT bind it


def test_resolve_placeholders_falls_back_to_heuristic_when_no_correlation(monkeypatch):
    """Correlation unavailable (None: no procfs/macOS, no pane pid, no fd yet) →
    the existing exactly-one heuristic still binds the single new rollout, so the
    #12 fix never regresses the interactive default on platforms without /proc.
    """
    proj = _project()
    real_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new----tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, real_id, mtime=1000.0)
    ]
    app._correlate_codex_rollout = lambda r: None   # correlation unavailable

    app._resolve_placeholders([proj])

    assert real_id in app._running and not app._running[real_id].is_placeholder
    assert "__new__-tok-1" not in app._running


def test_resolve_placeholders_empty_correlation_waits_not_fallback(monkeypatch):
    """procfs available but codex hasn't opened its rollout fd YET (correlation
    returns an empty set), while an unrelated rollout already appeared. We must
    WAIT — NOT fall back to the heuristic, which would mis-bind the unrelated
    one (the staggered race codex flagged, #12)."""
    proj = _project()
    unrelated = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    app = App.__new__(App)
    app._codex_mode = True
    app._right_pane_claude = None
    app._running = {
        "__new__-tok-1": _Running(
            key="__new__-tok-1", tmux_name="cx-new----tok-1",
            label="test-proj/(new)", project=proj,
            placeholder_path=proj.real_path, created_at=999.0,
            session_type="codex",
        )
    }
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, unrelated, mtime=1001.0),
    ]
    app._correlate_codex_rollout = lambda r: set()  # procfs, but no fd open yet

    app._resolve_placeholders([proj])

    assert "__new__-tok-1" in app._running          # waited, not bound
    assert app._running["__new__-tok-1"].is_placeholder
    assert unrelated not in app._running            # heuristic did NOT fire


def test_correlate_codex_rollout_degrades_without_config():
    """The helper must never raise into the UI: a bare App (no _config) yields
    None so resolution falls back to the heuristic."""
    app = App.__new__(App)
    r = _Running(key="__new__-tok-1", tmux_name="cx-x", label="l",
                 session_type="codex")
    assert app._correlate_codex_rollout(r) is None


def test_launch_snapshots_pre_existing_ids(monkeypatch):
    """_launch captures the cwd's existing session ids into the placeholder's
    pre_launch_ids before starting the child (#12)."""
    proj = _project()
    existing = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    app = App.__new__(App)
    app._running = {}
    app._set_status = lambda *a, **k: None
    app._show_error = lambda *a, **k: None
    app._clear_error = lambda: None
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, existing, mtime=5.0)]
    app._shellify = lambda *a, **k: "SHELLCMD"
    app._ensure_detached_agent = lambda *a, **k: (True, None)
    app._attach_in_right_pane = lambda *a, **k: True
    app._session_name = lambda key: "cx-abc"

    assert app._launch("__new__-tok-1", ["codex"], proj.real_path, "l", proj,
                       placeholder_path=proj.real_path, session_type="codex")
    entry = app._running["__new__-tok-1"]
    assert entry.pre_launch_ids == frozenset({existing})
    # Snapshot taken with a fresh scan of the cwd.
    app._codex_index.sessions_for_cwd.assert_called_once_with(
        proj.real_path, refresh=True)
