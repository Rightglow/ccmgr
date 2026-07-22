"""Tests for railmux.ui.running_pane — callback dispatch (click vs double-click)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import urwid

from railmux.models import AttentionCategory, AttentionState
from railmux.ui.running_pane import RunningEntry, RunningSessionsPane, _RunningRow


def _entry(name: str = "cc-abc123", label: str = "proj/Test",
           project: str = "proj", provider: str = "Claude Code",
           status: str = "idle") -> RunningEntry:
    return RunningEntry(
        tmux_name=name, label=label, project_label=project,
        provider_label=provider, status=status)


# ── callback dispatch ────────────────────────────────────────────────────

def test_running_row_click_no_steal():
    """Single-click on a running row calls on_select with steal_focus=False."""
    calls = []
    pane = RunningSessionsPane(
        on_select=lambda e, **kw: calls.append((e.tmux_name, kw.get("steal_focus", True)))
    )
    entries = [_entry("cc-a"), _entry("cc-b")]
    pane.set_running(entries)

    rows = [w for w in pane._walker if isinstance(w, _RunningRow)]
    assert len(rows) == 2

    # Click row 0
    assert rows[0]._on_click is not None
    rows[0]._on_click()
    assert calls == [("cc-a", False)]


def test_running_row_double_click_defers_focus():
    """Double-click attaches without focus, then notifies App to focus later."""
    calls = []
    completed = []
    pane = RunningSessionsPane(
        on_select=lambda e, **kw: calls.append((e.tmux_name, kw.get("steal_focus", True))),
        on_double_detected=lambda: completed.append(True),
    )
    pane.set_running([_entry("cc-x")])

    row = [w for w in pane._walker if isinstance(w, _RunningRow)][0]
    assert row._on_double_click is not None
    row._on_double_click()
    assert calls == [("cc-x", False)]
    assert completed == [True]


def test_running_row_both_callbacks_set():
    """Every running row has both on_click and on_double_click."""
    pane = RunningSessionsPane(on_select=lambda e, **kw: None)
    pane.set_running([_entry("cc-1"), _entry("cc-2"), _entry("cc-3")])

    rows = [w for w in pane._walker if isinstance(w, _RunningRow)]
    for r in rows:
        assert r._on_click is not None
        assert r._on_double_click is not None


# ── Enter key ────────────────────────────────────────────────────────────

def test_enter_on_running_row_steals_focus():
    """Enter on a running row still steals focus immediately."""
    calls = []
    pane = RunningSessionsPane(
        on_select=lambda e, **kw: calls.append(kw.get("steal_focus", True))
    )
    pane.set_running([_entry("cc-e")])

    pane._walker.set_focus(0)
    result = pane.keypress((20, 10), "enter")
    assert result is None
    assert calls == [True]  # Enter steals


# ── empty state ──────────────────────────────────────────────────────────

def test_set_running_empty():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([])
    assert isinstance(pane._walker[0], urwid.Text)
    assert "no running" in pane._walker[0].text.lower()


def test_set_running_restores_focus():
    pane = RunningSessionsPane(on_select=lambda e: None)
    entries = [_entry("cc-1"), _entry("cc-2")]
    pane.set_running(entries)

    pane._walker.set_focus(1)
    pane.set_running(entries)
    focus_w, _ = pane._walker.get_focus()
    assert isinstance(focus_w, _RunningRow)
    assert focus_w.entry.tmux_name == "cc-2"


def test_set_running_unchanged_preserves_rows():
    pane = RunningSessionsPane(on_select=lambda e: None)
    entries = [_entry("cc-1"), _entry("cc-2")]
    pane.set_running(entries)
    rows = list(pane._walker)

    pane.set_running(entries)

    assert list(pane._walker) == rows
    assert all(current is prior for current, prior in zip(pane._walker, rows))


def test_wheel_over_bottom_border_scrolls_running_list():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([_entry(f"cc-{i}") for i in range(12)])
    pane._walker.set_focus(6)

    handled = pane.mouse_event(
        (30, 10), "mouse press", 4, 1, 9, False)

    assert handled is True
    assert pane._walker.focus == 5


# ── filtering ───────────────────────────────────────────────────────────

def _running_names(pane: RunningSessionsPane) -> list[str]:
    return [row.entry.tmux_name for row in pane._walker
            if isinstance(row, _RunningRow)]


def test_filter_fuzzy_matches_visible_label_and_provider():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "alpha/Fix mobile login", "alpha"),
        _entry("cx-b", "beta/Parser", "beta", "Codex"),
    ])

    pane.set_filter("mobile fix")
    assert _running_names(pane) == ["cc-a"]

    pane.set_filter("codex")
    assert _running_names(pane) == ["cx-b"]


def test_project_token_matches_explicit_project_only():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "alpha/Work on beta", "alpha"),
        _entry("cc-b", "beta/Unrelated", "beta"),
    ])

    pane.set_filter("project:beta")

    assert _running_names(pane) == ["cc-b"]


def test_half_typed_project_token_is_non_destructive():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([_entry("cc-a"), _entry("cc-b")])

    pane.set_filter("project:")

    assert _running_names(pane) == ["cc-a", "cc-b"]


def test_unknown_key_value_remains_ordinary_search_text():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "proj/fix:login", "proj"),
        _entry("cc-b", "proj/other", "proj"),
    ])

    pane.set_filter("fix:login")

    assert _running_names(pane) == ["cc-a"]


def test_filter_empty_copy_is_provider_aware_and_keeps_snapshot():
    pane = RunningSessionsPane(
        on_select=lambda e: None, provider_label="Codex")
    pane.set_running([_entry("cx-a", provider="Codex")])

    pane.set_filter("missing")

    assert "matching Codex" in pane._walker[0].text
    assert "Ctrl-U" in pane._walker[0].text
    assert pane._entries[0].tmux_name == "cx-a"
    assert pane._linebox.title_widget.text.strip() == "Running (0/1) [filtered]"


def test_filter_clear_restores_pre_filter_focus():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "alpha/One", "alpha"),
        _entry("cc-b", "beta/Two", "beta"),
        _entry("cc-c", "gamma/Three", "gamma"),
    ])
    pane._walker.set_focus(1)

    pane.set_filter("alpha")
    assert _running_names(pane) == ["cc-a"]
    pane.set_filter("")

    focus_w, _ = pane._walker.get_focus()
    assert isinstance(focus_w, _RunningRow)
    assert focus_w.entry.tmux_name == "cc-b"


def test_noninteractive_mode_filter_drops_foreign_focus_anchor():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "alpha/One", "alpha"),
        _entry("cc-b", "beta/Two", "beta"),
    ])
    pane._walker.set_focus(1)
    pane.set_filter("project:alpha")
    assert pane._pre_filter_focus == "cc-b"

    pane.set_filter("project:codex", capture_focus=False)

    assert pane._pre_filter_focus is None


def test_filter_refresh_preserves_visible_focus_by_tmux_identity():
    pane = RunningSessionsPane(on_select=lambda e: None)
    a = _entry("cc-a", "proj/Alpha")
    b = _entry("cc-b", "proj/Beta")
    pane.set_running([a, b])
    pane.set_filter("proj")
    pane._walker.set_focus(1)

    pane.set_running([b, a])

    focus_w, _ = pane._walker.get_focus()
    assert isinstance(focus_w, _RunningRow)
    assert focus_w.entry.tmux_name == "cc-b"


def test_active_and_context_highlights_do_not_discard_hidden_entries():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry("cc-a", "alpha/One", "alpha"),
        _entry("cc-b", "beta/Two", "beta"),
    ])
    pane.set_filter("alpha")

    pane.set_active("cc-a")
    pane.set_selected("cc-a")

    assert [entry.tmux_name for entry in pane._entries] == ["cc-a", "cc-b"]
    pane.set_filter("")
    assert _running_names(pane) == ["cc-a", "cc-b"]


def test_filtered_click_and_enter_target_only_visible_identity():
    calls: list[str] = []
    pane = RunningSessionsPane(
        on_select=lambda entry, **_kw: calls.append(entry.tmux_name))
    pane.set_running([
        _entry("cc-hidden", "alpha/One", "alpha"),
        _entry("cc-visible", "beta/Two", "beta"),
    ])
    pane.set_filter("project:beta")
    row = next(row for row in pane._walker if isinstance(row, _RunningRow))

    row._on_click()
    pane.keypress((40, 10), "enter")

    assert calls == ["cc-visible", "cc-visible"]


def test_filter_large_snapshot_performs_no_file_io():
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([
        _entry(f"cc-{i}", f"project-{i % 20}/Session {i}",
               f"project-{i % 20}")
        for i in range(2000)
    ])

    with patch("builtins.open", side_effect=AssertionError("filter did file IO")):
        pane.set_filter("project:project-7 session")

    assert _running_names(pane)


# ── status dots ─────────────────────────────────────────────────────────

def test_running_entry_default_status():
    e = RunningEntry(tmux_name="cc-x", label="test")
    assert e.status == "idle"


def test_running_row_uses_status_dot():
    e = RunningEntry(tmux_name="cc-x", label="test", status="busy")
    row = _RunningRow(e)
    # Row renders without error with tuple status dot
    assert row is not None


def test_running_row_attention_is_separate_from_idle_dot():
    attention = AttentionState(
        AttentionCategory.UNKNOWN_ERROR, "Provider reported an error.")
    row = _RunningRow(RunningEntry(
        tmux_name="cc-x", label="test", status="idle", attention=attention))
    text = row._wrapped_widget.base_widget

    assert text.text.startswith("● ! test")
    assert text.attrib[0][0] == "status_idle"
    assert any(attr == "attention" for attr, _length in text.attrib)


def _selected_tmux_names(pane: RunningSessionsPane) -> set[str]:
    return {
        row.entry.tmux_name
        for row in pane._walker
        if isinstance(row, _RunningRow)
        and row._wrapped_widget.attr_map.get(None) == "selected"
    }


def test_running_select_codex_preserves_projects_and_sessions_highlight(monkeypatch):
    """Bug 3: clicking a Running row in Codex mode must keep the Projects +
    Sessions highlight. The session's project is resolved to the visible
    (Codex) row so its encoded_name lands, and sessions come from the Codex
    index (not the empty Claude cache), so the running session's row is
    highlighted rather than the pane being cleared."""
    from railmux.ui.app import App, _Running
    from railmux.models import Project, SessionMeta
    from railmux.ui.projects_pane import ProjectsPane
    from railmux.ui.sessions_pane import SessionsPane, _SessionRow

    real_path = Path("/tmp/myproj")
    # Project as shown in the sidebar (Claude-discovery encoded_name).
    view_proj = Project(real_path=real_path, encoded_name="-tmp-myproj",
                        claude_dir=Path("/tmp/myproj/.claude"),
                        session_count=1, last_activity_ts=1000.0)
    # Project the running session carries: same path, DIFFERENT encoded_name
    # (as the Codex index would synthesise).
    running_proj = Project(real_path=real_path, encoded_name="codex-tmp-myproj",
                           claude_dir=Path(), session_count=1, last_activity_ts=0.0)
    sess_id = "12345678-1234-1234-1234-1234567890ab"
    codex_sess = SessionMeta(
        project=view_proj, session_id=sess_id,
        jsonl_path=Path("/tmp/rollout.jsonl"), title="Codex chat",
        message_count=1, token_total=1, last_mtime=2000.0, status="idle")

    app = App.__new__(App)
    app._codex_mode = True
    app._selected_project = None
    app._in_history_mode = False
    app._restore_state = None
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._running = {sess_id: _Running(
        key=sess_id, tmux_name="cx-abc", label="myproj/Codex chat",
        project=running_proj)}
    app._projects_pane = ProjectsPane([view_proj], on_select=lambda p: None)
    app._sessions_pane = SessionsPane(on_select=lambda *a, **k: None)
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [codex_sess]
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.side_effect = AssertionError(
        "Codex running-select queried the Claude cache")
    monkeypatch.setattr(app, "_visible_projects", lambda *a, **k: [view_proj])
    monkeypatch.setattr(app, "_cancel_pending_double_focus", lambda *a, **k: None)
    monkeypatch.setattr(app, "_set_status", lambda *a, **k: None)

    def fake_attach(tmux_name, *, steal_focus=True):
        # Mirror the real attach: mark the running session active first.
        app._active_session_id = sess_id
        app._sessions_pane.set_active_session(sess_id)
        return True
    monkeypatch.setattr(app, "_attach_in_right_pane", fake_attach)

    app._on_running_select(RunningEntry(tmux_name="cx-abc", label="myproj/Codex chat"))

    # Projects: highlight lands on the VISIBLE encoded_name, not the foreign one.
    assert app._selected_project.encoded_name == "-tmp-myproj"
    assert app._projects_pane._selected_encoded_name == "-tmp-myproj"
    # Sessions: rows come from the Codex index and include the running session.
    app._codex_index.sessions_for_cwd.assert_called_with(real_path, refresh=True)
    session_rows = [w for w in app._sessions_pane._walker
                    if isinstance(w, _SessionRow)]
    assert [row.session.session_id for row in session_rows] == [sess_id]
    # ...and that row is the highlighted (active) one.
    assert app._sessions_pane._active_session_id == sess_id
    assert session_rows[0]._wrapped_widget.attr_map.get(None) == "selected"


def test_active_running_entry_persists_after_context_selection_clears():
    active = _entry("cc-active")
    context = _entry("cc-context")
    pane = RunningSessionsPane(on_select=lambda e: None)
    pane.set_running([active, context])

    pane.set_active(active.tmux_name)
    assert _selected_tmux_names(pane) == {active.tmux_name}

    pane.set_selected(context.tmux_name)
    assert _selected_tmux_names(pane) == {context.tmux_name}

    pane.set_selected(None)
    assert _selected_tmux_names(pane) == {active.tmux_name}
