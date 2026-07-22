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
from railmux import orphan_marker, restart_state, tmux_ctl, tmux_server
from railmux.modes import CLAUDE_MODE, CODEX_MODE
from railmux.restart_state import OuterTmuxIdentity
from railmux.ui.app import App, _Running
from railmux.ui.modals import QuitConfirmModal
from railmux.ui.workspace import (
    AgentWorkspace,
    SlotRestoreState,
    WorkspaceLayout,
)


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
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_clean_exit",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.clear_clean_exit", lambda: None)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_soft_exit",
        lambda **_kwargs: True,
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
    assert data["right_kind"] == "empty"
    assert data["mode"] == "claude"
    assert data["workspace"] == {
        "version": 1,
        "layout": "single",
        "target": "primary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "empty"},
        },
    }


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


def test_soft_quit_portable_state_keeps_stable_agent_not_tmux_name(
        tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path))
    project = _project("myproj")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cc-12345678-1234-12",
        label="myproj/session",
        project=project,
        session_type="claude",
    )
    app._right_pane_claude = "cc-12345678-1234-12"
    app._active_session_id = session_id
    app._primary_slot.mode_key = "claude"
    app._primary_slot.project_key = project.encoded_name

    app._save_state(portable_right=True)

    portable = restart_state.decode_portable(
        restart_state.read_json_object(portable_path))
    assert portable == {
        "mode": "claude",
        "project": project.encoded_name,
        "right_kind": "agent",
        "right_mode": "claude",
        "right_session": session_id,
        "right_project": project.encoded_name,
    }
    assert "cc-12345678-1234-12" not in portable_path.read_text()
    assert app._load_state()["right_tmux"] == "cc-12345678-1234-12"


def test_portable_state_uses_explicit_active_secondary_slot():
    project = _project("secondary")
    session_id = "22345678-1234-1234-1234-1234567890ab"
    app = _minimal_app(selected_project=project)
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-secondary",
        label="secondary/session",
        project=project,
        session_type="codex",
    )
    slot = app._agent_workspace().secondary
    slot.agent_tmux_name = "cx-secondary"
    slot.active_session_id = session_id
    slot.mode_key = "codex"
    slot.project_key = project.encoded_name
    app._agent_workspace().set_target(AgentWorkspace.SECONDARY)

    data = app._portable_right_state_data()

    assert data["right_mode"] == "codex"
    assert data["right_session"] == session_id


def test_local_state_keeps_full_dual_workspace_but_portable_does_not(
        tmp_path, monkeypatch):
    local_path = tmp_path / "local.json"
    portable_path = tmp_path / "portable.json"
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: local_path))
    monkeypatch.setattr(
        App, "_portable_state_path", staticmethod(lambda: portable_path))
    app = _minimal_app(selected_project=_project("dual"))
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.agent_tmux_name = "cc-primary"
    workspace.primary.active_session_id = "primary-session"
    workspace.primary.mode_key = "claude"
    workspace.secondary.in_history_mode = True
    workspace.secondary.active_session_id = "secondary-session"
    workspace.secondary.mode_key = "codex"
    workspace.secondary.project_key = "-tmp-secondary"
    workspace.secondary.restore_state = SlotRestoreState(
        "agent", "cx-secondary")
    workspace.set_target(AgentWorkspace.SECONDARY)
    app._railmux_has_focus = True

    app._save_state(portable_right=True)

    saved = app._load_state()["workspace"]
    assert saved["layout"] == "stacked"
    assert saved["target"] == "secondary"
    assert saved["focus"] == "sidebar"
    assert saved["slots"]["primary"]["tmux"] == "cc-primary"
    assert saved["slots"]["secondary"] == {
        "kind": "preview",
        "session": "secondary-session",
        "mode": "codex",
        "project": "-tmp-secondary",
        "restore": {"kind": "agent", "tmux": "cx-secondary"},
    }
    assert "workspace" not in portable_path.read_text()


def test_managed_soft_restart_loads_dual_layout_after_controller_pane_changes(
        tmp_path, monkeypatch):
    monkeypatch.setattr(restart_state, "runtime_base", lambda: tmp_path)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda _session: MagicMock(session_name="railmux"),
    )
    monkeypatch.setattr(
        "railmux.restart_state.tmux_ctl.pane_identity", lambda _pane: None)
    source = _minimal_app(selected_project=_project("dual"))
    source._auto_launched = True
    source._agent_workspace().layout = WorkspaceLayout.SIDE_BY_SIDE
    source._agent_workspace().set_target(AgentWorkspace.SECONDARY)
    source._railmux_has_focus = True

    source._save_state(portable_right=True)
    assert source._publish_managed_restart_handoff()

    replacement = _minimal_app()
    replacement._auto_launched = True
    replacement._restart_identity = OuterTmuxIdentity(
        server_digest=source._restart_identity.server_digest,
        server_pid=source._restart_identity.server_pid,
        pane_id="%2",
        session_id="$2",
        window_id="@2",
    )
    replacement._loaded_restart_source = None
    replacement._loaded_restart_state_path = None

    restored = replacement._load_state()

    assert restored is not None
    assert restored["workspace"]["layout"] == "side-by-side"
    assert restored["workspace"]["target"] == "secondary"
    assert replacement._loaded_restart_source == source._restart_identity

    replacement._pending_restore_state = restored
    replacement._running_recovery_ok = True
    replacement._restore_right_pane = MagicMock(return_value=True)
    replacement._restore_pending_right_pane(None, None)

    replacement._restore_right_pane.assert_called_once_with(restored)
    assert not restart_state.instance_state_path(
        source._restart_identity).exists()
    assert not restart_state.managed_handoff_path(
        replacement._restart_identity).exists()


def test_local_state_snapshots_actual_agent_focus_before_save(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%2"
    workspace.secondary.pane_id = "%3"
    workspace.set_target(AgentWorkspace.PRIMARY)
    app._railmux_has_focus = False

    def sync_focus():
        workspace.set_target(AgentWorkspace.SECONDARY)
        return workspace.secondary

    app._sync_target_slot_from_tmux = MagicMock(side_effect=sync_focus)

    app._save_state()

    saved = app._load_state()["workspace"]
    assert saved["target"] == "secondary"
    assert saved["focus"] == "secondary"


def test_local_state_saves_collapsed_agent_with_stable_identity():
    app = _minimal_app()
    workspace = app._agent_workspace()
    workspace.collapsed_secondary_agent = "cx-collapsed"
    app._running["collapsed-session"] = _Running(
        key="collapsed-session",
        tmux_name="cx-collapsed",
        label="collapsed",
        session_type="codex",
    )

    saved = app._workspace_recovery_state_data()

    assert saved["collapsed_secondary"] == {
        "tmux": "cx-collapsed",
        "session": "collapsed-session",
        "mode": "codex",
    }


def test_restore_workspace_rebuilds_both_slots_target_and_agent_focus(
        monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()

    def restore_primary(_state, slot):
        slot.pane_id = "%2"
        slot.agent_tmux_name = "cc-primary"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    def restore_secondary(slot, _saved, _bindings):
        slot.in_history_mode = True
        slot.active_session_id = "secondary-session"
        return True

    app._restore_agent_target = MagicMock(side_effect=restore_primary)
    transport.create_secondary.side_effect = create_secondary
    app._restore_workspace_slot = MagicMock(side_effect=restore_secondary)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "secondary",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-primary"},
            "secondary": {
                "kind": "preview", "session": "secondary-session",
                "mode": "codex",
            },
        },
    }

    assert app._restore_workspace({"running_bindings": []}, saved)

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    app._set_railmux_focus.assert_called_with(False, force_border=True)
    app._restore_workspace_slot.assert_called_once_with(
        workspace.secondary, saved["slots"]["secondary"], [])


def test_restore_workspace_keeps_dual_layout_when_secondary_content_fails(
        monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    transport.create_primary.side_effect = create_primary
    transport.create_secondary.side_effect = create_secondary
    transport.reset_slot.return_value = True
    app._restore_workspace_slot = MagicMock(return_value=False)
    selected = []
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane",
        lambda pane: selected.append(pane) or True,
    )
    app._railmux_pane_id = "%1"
    saved = {
        "layout": "stacked",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "agent", "tmux": "cx-missing"},
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    transport.reset_slot.assert_called_once_with(workspace.secondary)
    assert selected[-1] == "%1"
    app._set_railmux_focus.assert_called_with(True, force_border=True)


def test_restore_workspace_geometry_fallback_remembers_validated_secondary(
        monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    transport.displayed_real_pane.return_value = None
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(70, 20))
    app._layout_fits = MagicMock(return_value=False)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._set_status = MagicMock()
    app._running["secondary-session"] = _Running(
        key="secondary-session",
        tmux_name="cx-secondary",
        label="secondary",
        session_type="codex",
    )

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    transport.create_primary.side_effect = create_primary
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "stacked",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {
                "kind": "agent",
                "tmux": "cx-secondary",
                "session": "secondary-session",
                "mode": "codex",
            },
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.collapsed_secondary_agent == "cx-secondary"
    transport.create_secondary.assert_not_called()


def test_restore_workspace_keeps_layout_when_primary_content_falls_back_empty(
        monkeypatch):
    app = _minimal_app()
    workspace = app._agent_workspace()
    transport = MagicMock()
    app._display_transport_manager = transport
    app._agent_region_size = MagicMock(return_value=(200, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._restore_agent_target = MagicMock(return_value=False)

    def create_primary():
        workspace.primary.pane_id = "%2"
        return True

    def create_secondary(layout):
        workspace.layout = layout
        workspace.secondary.pane_id = "%3"
        return True

    transport.create_primary.side_effect = create_primary
    transport.create_secondary.side_effect = create_secondary
    app._restore_workspace_slot = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    saved = {
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "agent", "tmux": "cc-missing"},
            "secondary": {"kind": "empty"},
        },
    }

    assert not app._restore_workspace({}, saved)

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.pane_id == "%2"
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    transport.create_primary.assert_called_once_with()


def test_workspace_preview_drops_unrepresented_agent_rollback(monkeypatch):
    app = _minimal_app()
    slot = app._agent_workspace().secondary
    app._restore_preview_target = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    assert app._restore_workspace_slot(slot, {
        "kind": "preview",
        "session": "history",
        "mode": "codex",
        "restore": {"kind": "agent", "tmux": "cx-reused-name"},
    })

    assert slot.restore_state == SlotRestoreState("empty")


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


def test_soft_restart_migrates_idle_pre_marker_codex_session(monkeypatch):
    """An old idle cx-new session remains visible across the next restart.

    Idle Codex closes its rollout fd, so the normal exact fd correlation has
    nothing to bind.  A strict historical launch-command match may preserve it
    as unresolved; the resulting state binding then makes future restarts
    independent of both procfs and the migration path.
    """
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---61404b-6"
    tmux_row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t\n"
    start_command = shlex.quote(
        f"cd {cwd} && exec $SHELL -li -c "
        f"'export CODEX_HOME=/tmp/codex-home && "
        f"exec codex -C {cwd}'")

    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(
        tmux_ctl, "session_rollout_ids", lambda *_args: set())
    start_probe = MagicMock(return_value=start_command)
    monkeypatch.setattr(
        tmux_ctl, "detached_single_pane_start_command", start_probe)
    written: list[orphan_marker.Marker] = []
    app._write_orphan_marker = lambda marker: written.append(marker) or True

    with patch("subprocess.check_output", return_value=tmux_row), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        assert app._discover_orphans() is True

    key = "__new__-61404b-6"
    migrated = app._running[key]
    assert migrated.tmux_name == tmux_name
    assert migrated.label.endswith("/(recovering)")
    assert migrated.allow_heuristic_resolution is False
    assert migrated.orphan == written[0]
    assert migrated.orphan.phase == "unresolved"
    assert migrated.orphan.tmux_session_id == "$42"
    assert migrated.orphan.tmux_pane_id == "%9"
    start_probe.assert_called_once_with(
        tmux_name, session_id="$42", pane_id="%9")

    binding = app._running_binding_data(
        migrated, include_launch_context=True)
    assert binding is not None
    assert binding["pre_launch_complete"] is False
    state = {
        "running_bindings_version": 1,
        "running_bindings": [binding],
    }

    restarted = _minimal_app()
    restarted._config = app._config
    restarted._codex_index = MagicMock()
    restarted._codex_index.all_cwds.return_value = {cwd: 1}
    restarted._codex_home_path = app._codex_home_path
    start_probe.reset_mock()
    marker_row = (
        f"{tmux_name}\t{cwd}\t100\t$42\t%9\t"
        f"{orphan_marker.encode(migrated.orphan)}\t\n"
    )
    pane = tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=999, session_name=tmux_name,
        session_id="$42", window_id="@42", dead=False,
        width=80, height=24,
    )
    monkeypatch.setattr(tmux_ctl, "pane_identity", lambda _pane_id: pane)
    with patch("subprocess.check_output", return_value=marker_row), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        assert restarted._discover_orphans(state) is True

    assert restarted._running[key].tmux_name == tmux_name
    assert restarted._running[key].allow_heuristic_resolution is False
    assert restarted._running[key].orphan == migrated.orphan
    start_probe.assert_not_called()


def test_legacy_session_is_not_claimed_when_v2_marker_write_fails(
        monkeypatch):
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---61404b-6"
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    app._write_orphan_marker = MagicMock(return_value=False)
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: set())
    monkeypatch.setattr(
        tmux_ctl,
        "detached_single_pane_start_command",
        lambda *_args, **_kwargs: shlex.quote(
            f"cd {cwd} && exec $SHELL -li -c 'exec codex -C {cwd}'"),
    )
    row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t\n"

    with patch("subprocess.check_output", return_value=row), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        assert app._discover_orphans() is True

    assert app._running == {}
    app._write_orphan_marker.assert_called_once()


def test_legacy_command_matcher_accepts_only_historical_launch_grammar():
    import shlex

    cwd = Path("/tmp/codex-only")
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    assert app._is_legacy_new_session_command(
        shlex.quote(f"cd {cwd} && exec claude"), CLAUDE_MODE, cwd)
    command = shlex.quote(
        f"cd {cwd} && exec $SHELL -li -c "
        f"'exec codex -C {cwd}; touch /tmp/not-railmux'")

    assert not app._is_legacy_new_session_command(
        command, CODEX_MODE, cwd)


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


def test_discover_orphans_reuses_initial_project_snapshot():
    project = _project("cached-startup")
    app = _minimal_app(selected_project=project)
    app._project_snapshot = [project]

    with patch("subprocess.check_output", return_value=""), patch(
            "railmux.ui.app.list_projects") as scan:
        assert app._discover_orphans() is True

    scan.assert_not_called()


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


def test_valid_v1_placeholder_binding_is_upgraded_to_resolved_v2(monkeypatch):
    import shlex

    cwd = Path("/tmp/codex-only")
    tmux_name = "cx-new---abcdef-1"
    session_id = "12345678-1234-1234-1234-1234567890ab"
    project = _project("codex-only")
    app = _minimal_app()
    app._config = MagicMock(codex_binary="codex", claude_binary="claude")
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {cwd: 1}
    app._codex_index.get.return_value = _codex_meta(project, session_id)
    app._codex_home_path = lambda: Path("/tmp/codex-home")
    monkeypatch.setattr(tmux_ctl, "session_rollout_ids", lambda *_args: None)
    monkeypatch.setattr(
        tmux_ctl,
        "detached_single_pane_start_command",
        lambda *_args, **_kwargs: shlex.quote(
            f"cd {cwd} && exec $SHELL -li -c 'exec codex -C {cwd}'"),
    )
    stamp = json.dumps({
        "key": session_id,
        "tmux_name": tmux_name,
        "session_type": "codex",
        "cwd": str(cwd),
    }, separators=(",", ":"), sort_keys=True)
    written: list[orphan_marker.Marker] = []
    app._write_orphan_marker = lambda marker: written.append(marker) or True
    row = f"{tmux_name}\t{cwd}\t100\t$42\t%9\t\t{stamp}\n"

    with patch("subprocess.check_output", return_value=row), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        assert app._discover_orphans() is True

    marker = app._running[session_id].orphan
    assert marker == written[0]
    assert marker.phase == "resolved"
    assert marker.session_id == session_id


def test_generation_zero_keeps_exact_codex_stamp_visible(monkeypatch):
    """A slow first scan must not drop an exact live session from Running."""
    cwd = Path("/tmp/codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {}
    app._codex_index.get.return_value = None
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
        complete = app._discover_orphans(
            allow_missing_codex_metadata=True)

    assert complete is True
    assert app._running[session_id].tmux_name == "cx-new---abcdef-1"
    assert app._running[session_id].status == "busy"


def test_first_codex_generation_revalidates_provisional_recovery():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = _minimal_app()
    app._running[session_id] = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._pending_restore_state = None
    app._loop = None
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=0))
    app._codex_index.is_unavailable = False

    def rediscover(_state, *, allow_missing_codex_metadata):
        assert allow_missing_codex_metadata is False
        assert session_id not in app._running
        app._running[session_id] = _Running(
            key=session_id,
            tmux_name="cx-new---abcdef-1",
            label="codex-only/Recovered",
            project=_project("codex-only"),
            status="idle",
            session_type="codex",
        )
        return True

    app._discover_orphans = MagicMock(side_effect=rediscover)
    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is False
    assert app._running_recovery_ok is True
    assert app._running[session_id].label.endswith("/Recovered")


def test_transient_first_generation_keeps_provisional_session_visible():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    recovered = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app = _minimal_app()
    app._running = {session_id: recovered}
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._last_orphan_probe_ok = True
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=1))
    app._discover_orphans = MagicMock(return_value=False)

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True
    assert app._running[session_id] is recovered
    assert app._codex_provisional_recovery_keys == {session_id}


def test_clean_generation_keeps_exact_session_until_metadata_appears():
    session_id = "12345678-1234-1234-1234-1234567890ab"
    recovered = _Running(
        key=session_id,
        tmux_name="cx-new---abcdef-1",
        label="codex-only/12345678",
        project=_project("codex-only"),
        status="busy",
        session_type="codex",
    )
    app = _minimal_app()
    app._running = {session_id: recovered}
    app._codex_recovery_pending = True
    app._codex_recovery_state = {"running_bindings_version": 1}
    app._codex_recovery_generation = 0
    app._codex_provisional_recovery_keys = {session_id}
    app._last_orphan_probe_ok = True
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=1, report=MagicMock(transient_errors=0))
    app._codex_index.get.return_value = None
    app._discover_orphans = MagicMock(return_value=False)

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True
    assert app._running[session_id] is recovered
    assert app._codex_provisional_recovery_keys == {session_id}


def test_unavailable_initial_index_keeps_recovery_pending_for_later_retry():
    app = _minimal_app()
    app._codex_recovery_pending = True
    app._codex_recovery_generation = 0
    app._codex_index = MagicMock()
    app._codex_index.current_snapshot.return_value = MagicMock(
        generation=0, report=None)
    app._codex_index.is_unavailable = True

    app._retry_pending_codex_recovery()

    assert app._codex_recovery_pending is True


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


def test_unresolved_legacy_stamp_keeps_heuristic_resolution_disabled(
        monkeypatch):
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
        "pre_launch_complete": False,
    }, separators=(",", ":"), sort_keys=True)
    state = {
        "running_bindings_version": 1,
        "running_bindings": [{
            "key": key,
            "tmux_name": "cx-new---abcdef-1",
            "session_type": "codex",
            "cwd": str(cwd),
            "created_at": 123.0,
            "pre_launch_ids": [],
            "pre_launch_complete": False,
        }],
    }

    with patch(
            "subprocess.check_output",
            return_value=f"cx-new---abcdef-1\t{cwd}\t100\t{stamp}\n"), patch(
            "railmux.ui.app.list_projects", return_value=[]):
        app._discover_orphans(state)

    assert app._running[key].allow_heuristic_resolution is False


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
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)

    restored = app._restore_right_pane({
        "right_kind": "agent",
        "right_tmux": "cx-untracked",
    })

    assert restored is False
    app._attach_in_right_pane.assert_not_called()


def test_restore_portable_agent_by_validated_session_id(monkeypatch):
    project = _project("portable")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    running = _Running(
        key=session_id,
        tmux_name="cc-12345678-1234-12",
        label="portable/session",
        project=project,
        session_type="claude",
    )
    app = _minimal_app(selected_project=project)
    app._running[session_id] = running
    app._attach_in_right_pane = MagicMock(return_value=True)
    app._set_status = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda name: name == running.tmux_name)

    restored = app._restore_right_pane({
        "right_kind": "agent",
        "right_mode": "claude",
        "right_session": session_id,
        "right_project": project.encoded_name,
    })

    assert restored is True
    app._attach_in_right_pane.assert_called_once_with(
        running.tmux_name, steal_focus=False)


def test_restore_preview_uses_persisted_mode_and_project_not_sidebar_mode():
    preview_project = _project("preview")
    sidebar_project = _project("sidebar")
    session_id = "preview-session"
    meta = MagicMock()
    meta.session_id = session_id
    meta.session_type = "claude"
    meta.jsonl_path = Path("/tmp/preview.jsonl")
    meta.project = preview_project
    app = _minimal_app(selected_project=sidebar_project)
    app._codex_mode = True
    app._project_snapshot = [preview_project, sidebar_project]
    app._session_cache.get.return_value = meta
    app._codex_index = MagicMock()
    app._show_transcript = MagicMock(return_value=True)
    app._set_active_target = MagicMock()

    restored = app._restore_right_pane({
        "right_kind": "preview",
        "right_mode": "claude",
        "right_session": session_id,
        "right_project": preview_project.encoded_name,
    })

    assert restored is True
    app._session_cache.get.assert_called_once_with(preview_project, session_id)
    app._codex_index.get.assert_not_called()
    app._set_active_target.assert_called_once_with(
        session_id,
        None,
        mode_key="claude",
        project_key=preview_project.encoded_name,
    )


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


def test_pending_index_allows_exact_running_target_restore(monkeypatch):
    state_path = Path("/tmp/not-used-state")
    app = _minimal_app()
    app._codex_recovery_pending = True
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "agent",
        "right_tmux": "cx-new---abcdef-1",
    }
    app._running["session"] = _Running(
        key="session",
        tmux_name="cx-new---abcdef-1",
        label="project/session",
        project=_project("codex-only"),
        session_type="codex",
    )
    app._restore_right_pane = MagicMock(return_value=True)
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: state_path))

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_called_once()
    assert app._pending_restore_state is None


def test_pending_codex_preview_waits_for_first_history_generation(monkeypatch):
    class _Snapshot:
        generation = 0

    class _Index:
        def current_snapshot(self):
            return _Snapshot()

    monkeypatch.setattr("railmux.ui.app.BackgroundCodexIndex", _Index)
    app = _minimal_app()
    app._codex_index = _Index()
    app._codex_recovery_pending = False
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "preview",
        "right_mode": "codex",
        "right_session": "codex-session",
    }
    app._restore_right_pane = MagicMock(return_value=True)

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_not_called()
    assert app._pending_restore_state is not None

    _Snapshot.generation = 1
    app._restore_pending_right_pane(None, None)
    app._restore_right_pane.assert_called_once()
    assert app._pending_restore_state is None


def test_pending_secondary_codex_preview_waits_for_history_generation(
        monkeypatch):
    class _Snapshot:
        generation = 0

    class _Index:
        def current_snapshot(self):
            return _Snapshot()

    monkeypatch.setattr("railmux.ui.app.BackgroundCodexIndex", _Index)
    app = _minimal_app()
    app._codex_index = _Index()
    app._codex_recovery_pending = False
    app._running_recovery_ok = False
    app._pending_restore_state = {
        "right_kind": "agent",
        "right_mode": "claude",
        "right_tmux": "cc-primary",
        "workspace": {
            "slots": {
                "primary": {"kind": "agent", "mode": "claude"},
                "secondary": {"kind": "preview", "mode": "codex"},
            },
        },
    }
    app._restore_right_pane = MagicMock(return_value=True)

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_not_called()
    assert app._pending_restore_state is not None

    _Snapshot.generation = 1
    app._restore_pending_right_pane(None, None)
    app._restore_right_pane.assert_called_once()


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

    app._launch_resume(meta, steal_focus=False, from_double=True)

    entry = app._on_running_select.call_args.args[0]
    assert entry.tmux_name == running.tmux_name
    assert app._on_running_select.call_args.kwargs == {
        "steal_focus": False,
        "from_double": True,
    }
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

    app._launch_resume(meta)

    assert key in app._running and session_id not in app._running
    app._launch.assert_not_called()
    app._set_status.assert_called_once_with(
        "Resume deferred: a live initializing agent in this project could "
        "own this session",
        "error",
    )


def test_launch_refuses_untracked_preexisting_tmux(monkeypatch):
    """The final launch gate cannot stamp or reuse an identity collision."""
    project = _project("codex-only")
    session_id = "12345678-1234-1234-1234-1234567890ab"
    app = App.__new__(App)
    app._running = {}
    app._session_name = lambda _key: "cx-12345678-1234-12"
    app._set_status = MagicMock()
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

def test_commit_soft_exit_publishes_intent_before_teardown(monkeypatch):
    app = _minimal_app()
    events = []
    app._save_state = MagicMock(
        side_effect=lambda **_kwargs: events.append("state"))
    app._publish_managed_restart_handoff = MagicMock(
        side_effect=lambda: events.append("handoff"))
    app._begin_exit = MagicMock(
        side_effect=lambda **_kwargs: events.append("begin"))
    record = MagicMock(
        side_effect=lambda **_kwargs: events.append("intent") or True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_soft_exit", record)

    app._commit_exit(soft=True)

    assert events == ["state", "handoff", "intent", "begin"]
    record.assert_called_once_with(server_pid=123, session_id="$1")
    app._begin_exit.assert_called_once_with(soft=True)


def test_begin_exit_paints_progress_before_synchronous_cleanup():
    app = _minimal_app()
    app._exit_in_progress = False
    app._soft_quit_flag = False
    app._loop = MagicMock()
    app._close_modal = MagicMock()
    events = []
    app._show_overlay = MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("overlay"))
    app._loop.draw_screen.side_effect = lambda: events.append("draw")
    app._teardown_tmux = MagicMock(
        side_effect=lambda **_kwargs: events.append("teardown"))

    with pytest.raises(urwid.ExitMainLoop):
        app._begin_exit(soft=False)

    assert events[:3] == ["overlay", "draw", "teardown"]
    assert app._show_overlay.call_args.kwargs == {
        "width": 44,
        "height": 7,
        "fixed_width": True,
        "fixed_height": True,
    }
    app._teardown_tmux.assert_called_once_with(defer_outer=True)


def test_teardown_phases_are_idempotent():
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._root_wheel_manager = MagicMock()
    app._running = {
        "abc123": _Running(
            key="abc123", tmux_name="cc-abc123", label="test", project=None),
    }
    transport = MagicMock()
    transport.close_all.return_value = True
    app._display_transport_manager = transport

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        app._teardown_tmux()
        app._teardown_tmux()

    transport.close_all.assert_called_once_with()
    app._root_wheel_manager.close.assert_called_once_with()
    assert tmux.kill_session.call_count == 2
    tmux.kill_session.assert_any_call("cc-abc123")
    tmux.kill_session.assert_any_call("railmux")


def test_teardown_hard_quit_publishes_exact_clean_exit_intent(monkeypatch):
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {}
    record = MagicMock(return_value=True)
    clear = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_clean_exit", record)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.clear_clean_exit", clear)

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        tmux.current_session_id.return_value = "$1"
        tmux.kill_session.return_value = True
        app._teardown_tmux()

    record.assert_called_once_with(server_pid=123, session_id="$1")
    clear.assert_not_called()


def test_teardown_clears_clean_exit_intent_when_outer_kill_fails(monkeypatch):
    app = _minimal_app()
    app._soft_quit_flag = False
    app._auto_launched = True
    app._scroll_manager = MagicMock()
    app._running = {}
    record = MagicMock(return_value=True)
    clear = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.record_clean_exit", record)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_health.clear_clean_exit", clear)

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        tmux.current_session_name.return_value = "railmux"
        tmux.current_session_id.return_value = "$1"
        tmux.kill_session.return_value = False
        app._teardown_tmux()

    clear.assert_called_once_with()

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


def test_teardown_hard_quit_preserves_legacy_server_sessions():
    """Only an explicit per-row Kill may mutate a legacy server."""
    app = _minimal_app()
    app._soft_quit_flag = False
    app._right_pane_id = None
    app._auto_launched = False
    app._scroll_manager = MagicMock()
    target = tmux_server.TmuxServerTarget("/tmp/default", 44)
    app._running = {
        "current": _Running("current", "cc-current", "current"),
        "legacy": _Running(
            "legacy", "cc-old::legacy:44:7", "old",
            legacy_server=target, legacy_session_id="$7",
        ),
    }

    with patch("railmux.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    tmux.kill_session.assert_called_once_with("cc-current")


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
    meta.session_type = "codex"
    meta.jsonl_path = Path("/tmp/codex-rollout.jsonl")
    meta.project = app._selected_project
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
    app._set_active_target.assert_called_once_with(
        meta.session_id,
        None,
        mode_key="codex",
        project_key=meta.project.encoded_name,
    )


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


def test_correlate_codex_rollout_fails_closed_without_config(monkeypatch):
    """A helper failure on procfs must wait, never unlock the heuristic."""
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: True)
    app = App.__new__(App)
    r = _Running(key="__new__-tok-1", tmux_name="cx-x", label="l",
                 session_type="codex")
    assert app._correlate_codex_rollout(r) == set()


def test_launch_snapshots_pre_existing_ids(monkeypatch):
    """_launch captures the cwd's existing session ids into the placeholder's
    pre_launch_ids before starting the child (#12)."""
    proj = _project()
    existing = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    app = App.__new__(App)
    app._running = {}
    app._set_status = lambda *a, **k: None
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [
        _codex_session(proj, existing, mtime=5.0)]
    app._shellify = lambda *a, **k: "SHELLCMD"
    app._ensure_detached_agent = lambda *a, **k: (True, None)
    app._attach_in_right_pane = lambda *a, **k: True
    app._session_name = lambda key: "cx-abc"
    app._restart_identity = OuterTmuxIdentity(
        server_digest="a" * 64, server_pid=123, pane_id="%1",
        session_id="$1", window_id="@1")
    holder = tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=999, session_name="cx-abc",
        session_id="$9", window_id="@9", dead=False,
        width=80, height=24)
    monkeypatch.setattr(tmux_ctl, "create_detached_holder",
                        lambda *a, **k: (holder, None))
    monkeypatch.setattr(tmux_ctl, "start_detached_holder",
                        lambda *a, **k: (True, None))
    app._write_orphan_marker = lambda marker: True

    assert app._launch("__new__-tok-1", ["codex"], proj.real_path, "l", proj,
                       placeholder_path=proj.real_path, session_type="codex")
    entry = app._running["__new__-tok-1"]
    assert entry.pre_launch_ids == frozenset({existing})
    # Snapshot taken with a fresh scan of the cwd.
    app._codex_index.sessions_for_cwd.assert_called_once_with(
        proj.real_path, refresh=True)
