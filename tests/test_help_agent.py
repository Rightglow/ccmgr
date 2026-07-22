from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from railmux.config import Config
from railmux.modes import CODEX_MODE, DEFAULT_MODE_REGISTRY
from railmux.models import Project
from railmux.ui.app import App
from railmux.ui.workspace import AgentWorkspace


_YOLO = "--dangerously-bypass-approvals-and-sandbox"


def _app(mode_key: str, tmp_path: Path) -> App:
    app = App.__new__(App)
    app._mode_registry = DEFAULT_MODE_REGISTRY
    app._active_mode_key = mode_key
    app._config = Config(
        claude_binary="claude",
        codex_binary="codex",
        codex_home=str(tmp_path / "codex-home"),
        show_empty_projects=True,
    )
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = "%2"
    app._set_status = MagicMock()
    return app


def test_codex_help_command_auto_runs_inside_read_only_sandbox(tmp_path):
    app = _app("codex", tmp_path)

    command, env, login_shell = app._help_command(CODEX_MODE, tmp_path)

    assert command[0] == "codex"
    assert _YOLO not in command
    assert 'history.persistence="none"' in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert env == {"CODEX_HOME": str(tmp_path / "codex-home")}
    assert login_shell is True


def test_claude_help_command_auto_runs_only_read_search_tools(tmp_path):
    app = _app("claude", tmp_path)
    mode = DEFAULT_MODE_REGISTRY.get("claude")

    command, env, login_shell = app._help_command(mode, tmp_path)

    assert command[:2] == ["claude", "--safe-mode"]
    assert command[command.index("--permission-mode") + 1] == (
        "bypassPermissions")
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert "Bash" not in command
    assert command[-2:] == [
        "--append-system-prompt-file", str(tmp_path / "CLAUDE.md")]
    assert env is None
    assert login_shell is False


def test_ask_prepares_before_close_then_starts_and_attaches(
    monkeypatch, tmp_path,
):
    app = _app("codex", tmp_path)
    events: list[str] = []
    app._warn_missing_mode_binary = MagicMock(return_value=False)
    app._help_command = MagicMock(return_value=(["codex"], None, False))
    app._shellify = MagicMock(return_value="support-command")
    app._close_help_modal = lambda: events.append("close")
    app._attach_agent_slot = (
        lambda *_args, **_kwargs: events.append("attach") or True)
    monkeypatch.setattr(
        "railmux.ui.app.materialize_help_workspace",
        lambda: events.append("prepare") or tmp_path,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: False)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.new_detached_session",
        lambda *_args, **_kwargs: events.append("start") or (True, None),
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_session_user_option",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda *_args: "codex:read-only-auto-v2",
    )

    app._ask_railmux()

    assert events == ["prepare", "close", "start", "attach"]
    assert getattr(app, "_running", {}) == {}
    assert app._help_session_names_used == {"railmux-help-v1-codex"}


def test_missing_provider_keeps_static_help_open(monkeypatch, tmp_path):
    app = _app("claude", tmp_path)
    app._warn_missing_mode_binary = MagicMock(return_value=True)
    app._close_help_modal = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: False)

    app._ask_railmux()

    app._close_help_modal.assert_not_called()


def test_unverified_existing_help_session_is_never_adopted(
    monkeypatch, tmp_path,
):
    app = _app("codex", tmp_path)
    app._close_help_modal = MagicMock()
    app._attach_agent_slot = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda *_args: None,
    )

    app._ask_railmux()

    app._close_help_modal.assert_not_called()
    app._attach_agent_slot.assert_not_called()
    app._set_status.assert_called_once_with(
        "Ask Railmux refused: existing help session identity is unverified",
        "error",
    )


def test_existing_v1_help_session_is_safely_replaced_with_auto_read_policy(
    monkeypatch, tmp_path,
):
    app = _app("codex", tmp_path)
    app._warn_missing_mode_binary = MagicMock(return_value=False)
    app._help_command = MagicMock(return_value=(["codex"], None, False))
    app._shellify = MagicMock(return_value="support-command")
    app._close_help_modal = MagicMock()
    app._return_agent_before_kill = MagicMock(return_value=True)
    app._attach_agent_slot = MagicMock(return_value=True)
    live = True
    marker = "codex"

    def session_exists(_name):
        return live

    def kill_session(_name):
        nonlocal live
        live = False
        return True

    def new_session(*_args, **_kwargs):
        nonlocal live
        live = True
        return True, None

    def set_option(_name, _option, value):
        nonlocal marker
        marker = value
        return True

    monkeypatch.setattr(
        "railmux.ui.app.materialize_help_workspace", lambda: tmp_path)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", session_exists)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_session", kill_session)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.new_detached_session", new_session)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.set_session_user_option", set_option)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda *_args: marker,
    )

    app._ask_railmux()

    app._return_agent_before_kill.assert_called_once_with(
        "railmux-help-v1-codex")
    assert marker == "codex:read-only-auto-v2"
    app._attach_agent_slot.assert_called_once()


def test_hard_quit_helper_sweep_requires_exact_persisted_identity(
    monkeypatch, tmp_path,
):
    app = _app("codex", tmp_path)
    app._help_session_names_used = {
        "railmux-help-v1-codex",
        "railmux-help-v1-reused",
    }
    server = MagicMock()
    server.sessions = frozenset({
        "railmux-help-v1-codex",
        "railmux-help-v1-claude",
        "railmux-help-v1-reused",
        "cx-normal",
    })
    markers = {
        "railmux-help-v1-codex": "codex:read-only-auto-v2",
        "railmux-help-v1-claude": "claude",  # safe legacy helper
        "railmux-help-v1-reused": "unverified-owner",
    }
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.server_snapshot", lambda: server)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda name, _option: markers.get(name),
    )

    assert app._verified_help_session_names() == {
        "railmux-help-v1-codex",
        "railmux-help-v1-claude",
    }


def test_hard_quit_helper_sweep_tolerates_unavailable_tmux_snapshot(
    monkeypatch, tmp_path,
):
    app = _app("codex", tmp_path)
    app._help_session_names_used = {"railmux-help-v1-codex"}
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.server_snapshot", lambda: None)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda *_args: "codex:read-only-auto-v2",
    )

    assert app._verified_help_session_names() == {
        "railmux-help-v1-codex",
    }


def test_help_display_is_not_persisted_as_normal_agent(tmp_path):
    app = _app("codex", tmp_path)
    slot = app._workspace.primary
    slot.agent_tmux_name = "railmux-help-v1-codex"
    slot.active_session_id = "old-session"
    slot.mode_key = "codex"

    assert app._slot_recovery_state_data(slot) == {"kind": "empty"}
    app._running = {}
    recovery = app._recovery_state_data()
    assert recovery["right_kind"] == "empty"
    assert "right_tmux" not in recovery
    assert recovery["workspace"]["slots"]["primary"] == {"kind": "empty"}


def test_help_workspace_is_hidden_from_both_project_views(
    monkeypatch, tmp_path,
):
    help_path = tmp_path / "railmux" / "help"
    normal_path = tmp_path / "project"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    help_project = Project(help_path, "-help", tmp_path / "h", 1, 2.0)
    normal_project = Project(normal_path, "-normal", tmp_path / "n", 1, 1.0)

    claude = _app("claude", tmp_path)
    claude._project_snapshot = [help_project, normal_project]
    claude._project_snapshot_at = time.monotonic()
    assert claude._visible_projects() == [normal_project]

    codex = _app("codex", tmp_path)
    codex._project_snapshot = [help_project, normal_project]
    codex._project_snapshot_at = time.monotonic()
    codex._codex_project_filter = {help_path: 1, normal_path: 1}
    codex._codex_index = MagicMock()
    assert [item.real_path for item in codex._visible_projects()] == [normal_path]
