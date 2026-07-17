"""Tests for context-aware hint text generation."""

from unittest.mock import MagicMock

from railmux.ui.app import App
from railmux.ui.keymap import (
    CTX_AGENT,
    CTX_PROJECTS,
    CTX_RUNNING,
    CTX_SESSIONS,
    Binding,
    _visible_in,
    action_for,
    hint_text,
    hint_text_for,
)


# ── _visible_in ─────────────────────────────────────────────────────────

def test_visible_in_no_contexts_always_shown():
    b = Binding(("x",), "x", "test")
    assert _visible_in(b, CTX_PROJECTS) is True
    assert _visible_in(b, CTX_SESSIONS) is True
    assert _visible_in(b, CTX_RUNNING) is True
    assert _visible_in(b, None) is True


def test_visible_in_matching_context():
    b = Binding(("x",), "x", "test", contexts=(CTX_SESSIONS,))
    assert _visible_in(b, CTX_SESSIONS) is True
    assert _visible_in(b, CTX_PROJECTS) is False
    assert _visible_in(b, CTX_RUNNING) is False


# ── hint_text_for ───────────────────────────────────────────────────────

def test_hint_text_for_projects_hides_session_only_keys():
    text = hint_text_for(CTX_PROJECTS)
    # Session-only operations must not appear.
    assert "rename" not in text
    assert "del" not in text
    assert "star" not in text
    # Navigation + core operations must appear.
    assert "move" in text
    assert "open" in text
    assert "new" in text
    assert "filter" in text
    assert "info" in text
    assert "term" in text
    # Trailing line always present.
    assert "help" in text
    assert "quit" in text


def test_hint_text_for_sessions_shows_all_actions():
    text = hint_text_for(CTX_SESSIONS)
    assert "move" in text
    assert "new" in text
    assert "rename" in text
    assert "star" in text
    assert "kill" in text
    assert "del" in text
    assert "info" in text
    assert "term" in text


def test_hint_text_for_running_hides_creation_and_editing():
    text = hint_text_for(CTX_RUNNING)
    assert "new" not in text
    assert "filter" in text
    assert "rename" not in text
    assert "star" not in text
    assert "del" in text
    # Running-valid operations.
    assert "move" in text
    assert "open" in text
    assert "kill" in text
    assert "info" in text
    assert "term" in text


def test_running_slash_routes_to_filter_editor():
    app = App.__new__(App)
    app._loop = None
    app._sidebar = type("Sidebar", (), {"focus_position": 2})()
    app._enter_filter_mode = MagicMock()

    app._on_input("/")

    app._enter_filter_mode.assert_called_once_with()


def test_hint_text_legacy_returns_all_keys():
    text = hint_text()
    # Should match the original all-keys view.
    assert "move" in text
    assert "new" in text
    assert "filter" in text
    assert "rename" in text
    assert "star" in text
    assert "kill" in text
    assert "del" in text
    assert "fullscreen" in text


def test_hint_text_for_always_has_two_lines():
    for ctx in (CTX_PROJECTS, CTX_SESSIONS, CTX_RUNNING, CTX_AGENT, None):
        text = hint_text_for(ctx)
        lines = text.split("\n")
        assert len(lines) == 2, f"Context {ctx!r} produced {len(lines)} lines"
        assert lines[0], f"Context {ctx!r} line 1 is empty"


def test_different_contexts_produce_different_first_lines():
    """Each context should produce a different first line."""
    projects = hint_text_for(CTX_PROJECTS).split("\n")[0]
    sessions = hint_text_for(CTX_SESSIONS).split("\n")[0]
    running = hint_text_for(CTX_RUNNING).split("\n")[0]
    assert projects != sessions
    assert projects != running
    assert sessions != running


def test_trailing_line_identical_across_sidebar_contexts():
    """Line 2 (?, q, C-b d) is identical across all three sidebar panes."""
    trail = hint_text_for(None).split("\n")[1]
    for ctx in (CTX_PROJECTS, CTX_SESSIONS, CTX_RUNNING):
        assert hint_text_for(ctx).split("\n")[1] == trail


# ── agent context ────────────────────────────────────────────────────────

def test_hint_text_for_agent_shows_only_back_and_fullscreen():
    """When the right-hand agent pane has focus, only C-b ← and F9 appear."""
    text = hint_text_for(CTX_AGENT)
    assert "back" in text
    assert "fullscreen" in text
    # Sidebar keys must not appear.
    assert "move" not in text
    assert "pane" not in text
    assert "open" not in text
    assert "new" not in text
    assert "filter" not in text
    assert "info" not in text
    assert "rename" not in text
    assert "star" not in text
    assert "kill" not in text
    assert "del" not in text
    assert "term" not in text
    # Trailing items must not appear.
    assert "help" not in text
    assert "quit" not in text


def test_agent_context_trail_is_empty():
    """Line 2 is empty for agent context — no help/quit/detach buttons."""
    text = hint_text_for(CTX_AGENT)
    assert text.split("\n")[1] == ""


def test_agent_context_produces_different_line():
    """Agent context line 1 differs from all three sidebar contexts."""
    agent_line = hint_text_for(CTX_AGENT).split("\n")[0]
    for ctx in (CTX_PROJECTS, CTX_SESSIONS, CTX_RUNNING):
        assert hint_text_for(ctx).split("\n")[0] != agent_line


def test_mode_binding_resolves_to_compatibility_action():
    action = action_for("m", CTX_PROJECTS)

    assert action == "_toggle_codex_mode"
    assert callable(getattr(App, action))


def test_mode_binding_is_not_duplicated_in_hint_bar():
    for context in (CTX_PROJECTS, CTX_SESSIONS, CTX_RUNNING, None):
        assert "m Mode" not in hint_text_for(context).split("\n", 1)[0]
