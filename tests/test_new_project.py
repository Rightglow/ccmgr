from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux.config import Config
from railmux.models import Project
from railmux.ui import app as app_mod
from railmux.ui.app import App, _Running
from railmux.ui.running_pane import RunningEntry


def _project(path: Path, count: int) -> Project:
    return Project(
        real_path=path,
        encoded_name=str(path).replace("/", "-"),
        claude_dir=Path("/metadata") / path.name,
        session_count=count,
        last_activity_ts=1.0,
    )


def _submission_app(codex_mode: bool):
    app = App.__new__(App)
    app._codex_mode = codex_mode
    app._config = Config(codex_binary="codex-bin", claude_binary="claude-bin")
    app._settings = SimpleNamespace(codex_yolo=False)
    app._close_modal = lambda: None
    app._new_placeholder_key = lambda: "__new__-test-1"
    app._codex_env = lambda: {"CODEX_HOME": "/codex-home"}
    app._set_status = lambda *args, **kwargs: None
    captured = {}

    def launch(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    app._launch = launch
    return app, captured


def test_codex_new_project_creates_missing_directory_and_launches(tmp_path):
    app, captured = _submission_app(codex_mode=True)
    target = tmp_path / "new" / "codex-project"

    app._on_new_project_submit(target)

    assert target.is_dir()
    args = captured["args"]
    kwargs = captured["kwargs"]
    assert args[0] == "__new__-test-1"
    assert args[1] == ["codex-bin", "-C", str(target)]
    assert args[2] == target
    assert args[4].real_path == target
    assert args[4].claude_dir == Path()
    assert kwargs == {
        "placeholder_path": target,
        "env": {"CODEX_HOME": "/codex-home"},
        "login_shell": True,
        "session_type": "codex",
    }


def test_claude_new_project_keeps_provider_specific_launch(tmp_path):
    app, captured = _submission_app(codex_mode=False)
    target = tmp_path / "new-claude-project"

    app._on_new_project_submit(target)

    args = captured["args"]
    kwargs = captured["kwargs"]
    assert args[1] == ["claude-bin"]
    assert args[4] is None
    assert kwargs["session_type"] == "claude"
    assert kwargs["login_shell"] is False
    assert kwargs["env"] is None


def test_new_project_is_available_in_codex_mode():
    app = App.__new__(App)
    app._codex_mode = True
    app._pending_project = object()
    app._cancel_pending_double_focus = lambda: None
    opened = []
    app._open_new_project_modal = lambda: opened.append(True)

    app._on_project_select(None)

    assert opened == [True]
    assert app._pending_project is None


def test_empty_claude_projects_hidden_by_default(tmp_path):
    full = _project(tmp_path / "full", 1)
    empty = _project(tmp_path / "empty", 0)
    app = App.__new__(App)
    app._codex_mode = False
    app._config = Config()
    app._project_snapshot = [empty, full]
    app._project_snapshot_at = 1.0

    assert app._visible_projects(allow_stale=True) == [full]


def test_empty_claude_projects_can_be_shown(tmp_path):
    empty = _project(tmp_path / "empty", 0)
    app = App.__new__(App)
    app._codex_mode = False
    app._config = Config(show_empty_projects=True)
    app._project_snapshot = [empty]
    app._project_snapshot_at = 1.0

    assert app._visible_projects(allow_stale=True) == [empty]


def test_new_project_placeholder_has_running_context_menu(tmp_path):
    path = tmp_path / "new-project"
    app = App.__new__(App)
    app._running = {
        "__new__-test-1": _Running(
            key="__new__-test-1",
            tmux_name="cc-new-test-1",
            label="new-project/(new)",
            project=None,
            placeholder_path=path,
        )
    }
    app._running_pane = MagicMock()
    app._railmux_pane_id = None
    app._close_modal = lambda: None
    shown = []
    app._show_overlay = lambda modal, **kwargs: shown.append(modal)

    app._on_running_context_menu(RunningEntry(
        tmux_name="cc-new-test-1", label="new-project/(new)", status="idle"))

    assert len(shown) == 1
    labels = [row._wrapped_widget.base_widget.text
              for row in shown[0]._walker]
    assert any("Open" in label for label in labels)
    assert any("Kill" in label for label in labels)
    assert any("Term" in label for label in labels)


def test_open_terminal_for_unresolved_project_path(monkeypatch, tmp_path):
    path = tmp_path / "new-project"
    path.mkdir()
    app = App.__new__(App)
    app._right_pane_id = None
    statuses = []
    focus = []
    app._set_status = lambda text, *args: statuses.append(text)
    app._set_railmux_focus = lambda active: focus.append(active)
    monkeypatch.setattr(
        app_mod.tmux_ctl, "split_window_v",
        lambda command, target=None: "%9")
    monkeypatch.setattr(app_mod.tmux_ctl, "select_pane", lambda pane: None)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)

    app._open_terminal_for_path(path)

    assert focus == [False]
    assert statuses == ["terminal: new-project"]
