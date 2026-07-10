"""Tests for soft-quit feature: state file, orphan discovery, truncated ID
resolution, QuitConfirmModal s-key, and teardown branching."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import urwid

from ccmgr.models import Project
from ccmgr.ui.app import App, _Running
from ccmgr.ui.modals import QuitConfirmModal


# ── helpers ──────────────────────────────────────────────────────────────

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
    app._running = {}
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
    path = App._state_path()
    assert path == Path("/run/user/1000/ccmgr-state.json")


def test_state_path_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    path = App._state_path()
    assert path == Path("/tmp/ccmgr-1000/ccmgr-state.json")


def test_save_and_load_state_round_trip(tmp_path, monkeypatch):
    """_save_state writes JSON; _load_state reads it back."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))

    app = _minimal_app(selected_project=_project("myproj"))
    app._save_state()
    assert (tmp_path / "state.json").is_file()

    data = app._load_state()
    assert data["project"] == "-tmp-myproj"
    assert data["right_kind"] == "empty"


def test_save_state_always_writes_right_kind(tmp_path, monkeypatch):
    """Even without a selected project, _save_state records the right-pane state."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=None)
    app._save_state()
    assert (tmp_path / "state.json").is_file()
    data = app._load_state()
    assert data == {"right_kind": "empty"}


def test_save_state_with_claude_in_right_pane(tmp_path, monkeypatch):
    """When a Claude session is open, save its tmux name."""
    monkeypatch.setattr(App, "_state_path", staticmethod(lambda: tmp_path / "state.json"))
    app = _minimal_app(selected_project=_project("myproj"))
    app._right_pane_claude = "cc-abc123"
    app._save_state()
    data = app._load_state()
    assert data["right_kind"] == "claude"
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


def test_load_state_missing_file_returns_none():
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=Path("/tmp/ccmgr-nonexistent.json")):
        assert app._load_state() is None


def test_load_state_invalid_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    app = _minimal_app()
    with patch.object(App, "_state_path", return_value=p):
        assert app._load_state() is None


# ── _discover_orphans parsing ────────────────────────────────────────────

def test_discover_orphans_finds_cc_sessions():
    """A cc-* tmux session in a known project is added to _running."""
    proj = _project("myproj")
    full_id = "ae54affd-ec33-465c-b3c4-c1dc7c46990b"
    truncated = App._safe_name(full_id, 16)

    with patch("subprocess.check_output",
               return_value=f"cc-{truncated}\t/tmp/myproj\nccmgr\t/home/user\n"), \
         patch("ccmgr.ui.app.list_projects", return_value=[proj]), \
         patch.object(App, "_resolve_truncated_id",
                      return_value=full_id):
        app = _minimal_app()
        app._discover_orphans()

    assert full_id in app._running
    assert app._running[full_id].tmux_name == f"cc-{truncated}"
    assert app._running[full_id].project is proj


def test_discover_orphans_skips_placeholder():
    """__new__-N tmux sessions are skipped (handled by the normal poll)."""
    proj = _project()
    with patch("subprocess.check_output",
               return_value="cc-__new__-1\t/tmp/test-proj\n"), \
         patch("ccmgr.ui.app.list_projects", return_value=[proj]):
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
         patch("ccmgr.ui.app.list_projects", return_value=[proj]), \
         patch.object(App, "_resolve_truncated_id", return_value=full_id):
        app = _minimal_app()
        app._running[full_id] = _Running(key=full_id, tmux_name=f"cc-{truncated}",
                                          label="existing", project=proj)
        app._discover_orphans()
    assert app._running[full_id].label == "existing"  # not overwritten


def test_discover_orphans_skips_ccmgr():
    """The ccmgr outer tmux session is not treated as an orphan."""
    with patch("subprocess.check_output", return_value="ccmgr\t/home/user\n"):
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

    with patch("ccmgr.ui.app.tmux_ctl") as tmux:
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

    with patch("ccmgr.ui.app.tmux_ctl") as tmux:
        app._teardown_tmux()

    tmux.kill_session.assert_any_call("cc-abc123")


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
