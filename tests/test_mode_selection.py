"""Per-provider project selection across mode switches and refresh ticks."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from railmux.config import Config
from railmux.modes import CLAUDE_MODE, CODEX_MODE
from railmux.models import Project
from railmux.ui.app import App


def _project(name: str, *, codex_only: bool = False) -> Project:
    return Project(
        real_path=Path(f"/tmp/{name}"),
        encoded_name=f"-tmp-{name}",
        claude_dir=Path() if codex_only else Path(f"/meta/{name}"),
        session_count=1,
        last_activity_ts=1.0,
    )


def _mode_app(monkeypatch, claude_projects: list[Project],
              codex_projects: list[Project]) -> App:
    """Bare App with real toggle/refresh selection logic and mocked I/O."""
    app = App.__new__(App)
    app._codex_mode = False
    app._config = Config()
    app._selected_project = None
    app._mode_view_states = {}
    app._project_snapshot = claude_projects
    app._project_snapshot_at = time.monotonic()
    app._codex_project_filter = {
        project.real_path: project.session_count for project in codex_projects
    }
    app._running = {}
    app._right_pane_id = None
    app._right_pane_claude = None
    app._in_history_mode = False
    app._tmux_error_bar = False
    app._mode_refresh_thread = None
    app._mode_refresh_result = None
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = app._codex_project_filter
    app._scroll_manager = MagicMock()
    app._hint_bar = MagicMock()
    app._pane_sessions = MagicMock(return_value=[])
    app._resolve_placeholders = MagicMock()
    app._update_running_pane = MagicMock()
    app._update_status = MagicMock()
    app._help_context = MagicMock(return_value="projects")
    app._cancel_pending_double_focus = MagicMock()
    app._set_status = MagicMock()
    app._apply_tmux_bar = MagicMock()
    app._maybe_prompt_codex_yolo = MagicMock()
    app._schedule_mode_data_refresh = MagicMock()
    app._mode_refresh_pending = MagicMock(return_value=False)
    app._consume_mode_refresh = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.ui.app.shutil.which", lambda _binary: "/bin/agent")

    monkeypatch.setattr(
        app,
        "_visible_projects",
        lambda **_kwargs: codex_projects if app._codex_mode else claude_projects,
    )
    return app


def test_missing_provider_binary_warning_never_echoes_configured_path(
    monkeypatch,
):
    app = App.__new__(App)
    secret_path = "/private/company/user/claude-wrapper"
    app._config = Config(claude_binary=secret_path)
    app._set_status = MagicMock()
    monkeypatch.setattr("railmux.ui.app.shutil.which", lambda _binary: None)

    assert app._warn_missing_mode_binary(CLAUDE_MODE) is True

    message, level = app._set_status.call_args.args
    assert level == "warn"
    assert "Claude Code executable not found" in message
    assert secret_path not in message


def test_empty_codex_refresh_then_claude_restores_previous_project(monkeypatch):
    claude = _project("claude-a")
    app = _mode_app(monkeypatch, [claude], [])
    app._on_project_select(claude)

    app._toggle_codex_mode()
    assert app._codex_mode is True
    assert app._selected_project is None

    # This tick used to erase the only shared selection permanently.
    app._refresh()
    app._toggle_codex_mode()

    assert app._codex_mode is False
    assert app._selected_project is claude
    assert app._sessions_pane.set_sessions.call_args.args[0] is claude


def test_empty_codex_never_keeps_hidden_actionable_claude_project(monkeypatch):
    claude = _project("claude-a")
    app = _mode_app(monkeypatch, [claude], [])
    app._on_project_select(claude)

    app._toggle_codex_mode()

    assert app._selected_project is None
    assert app._projects_pane.set_selected.call_args.args == (None,)
    assert app._sessions_pane.set_sessions.call_args.args[:2] == (None, [])


def test_each_mode_restores_its_own_project(monkeypatch):
    claude_a = _project("claude-a")
    claude_b = _project("claude-b")
    codex_a = _project("codex-a", codex_only=True)
    codex_b = _project("codex-b", codex_only=True)
    app = _mode_app(
        monkeypatch, [claude_a, claude_b], [codex_a, codex_b])
    app._on_project_select(claude_b)

    app._toggle_codex_mode()
    app._on_project_select(codex_b)
    app._toggle_codex_mode()
    assert app._selected_project is claude_b

    app._toggle_codex_mode()
    assert app._selected_project is codex_b


def test_each_mode_applies_its_own_running_filter(monkeypatch):
    claude = _project("claude")
    codex = _project("codex", codex_only=True)
    app = _mode_app(monkeypatch, [claude], [codex])
    app._mode_view_states[CLAUDE_MODE.key] = app._current_mode_view_state()
    app._mode_view_states[CLAUDE_MODE.key].running_filter = "project:claude"
    app._mode_view_states[CODEX_MODE.key] = type(
        app._mode_view_states[CLAUDE_MODE.key])(
            running_filter="project:codex")

    app._toggle_codex_mode()
    app._running_pane.set_filter.assert_called_with(
        "project:codex", capture_focus=False)

    app._toggle_codex_mode()
    app._running_pane.set_filter.assert_called_with(
        "project:claude", capture_focus=False)


def test_deleted_remembered_project_falls_back_to_visible_project(monkeypatch):
    deleted = _project("deleted")
    fallback = _project("fallback")
    codex = _project("codex", codex_only=True)
    claude_projects = [deleted, fallback]
    app = _mode_app(monkeypatch, claude_projects, [codex])
    app._on_project_select(deleted)
    app._toggle_codex_mode()

    claude_projects.remove(deleted)
    app._toggle_codex_mode()

    assert app._selected_project is fallback
    selected_projects = [call.args[0] for call in app._pane_sessions.call_args_list]
    assert deleted not in selected_projects[1:]
