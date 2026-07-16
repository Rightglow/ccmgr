"""Tests for railmux.ui.sessions_pane — callback dispatch (preview vs open)."""

from pathlib import Path

import urwid

from railmux.models import Project, SessionMeta
from railmux.ui.sessions_pane import SessionsPane, _SessionRow


# ── helpers ──────────────────────────────────────────────────────────────

def _project(name: str = "test-proj") -> Project:
    return Project(
        real_path=Path(f"/tmp/{name}"),
        encoded_name=f"-tmp-{name}",
        claude_dir=Path(f"~/.claude/projects/-tmp-{name}").expanduser(),
        session_count=3,
        last_activity_ts=1000.0,
    )


def _session(project: Project, session_id: str = "a" * 36, title: str = "Test Session") -> SessionMeta:
    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=project.claude_dir / f"{session_id}.jsonl",
        title=title,
        message_count=5,
        token_total=500,
        last_mtime=2000.0,
        size_bytes=1024,
        status="idle",
    )


def _extract_callbacks(pane: SessionsPane) -> list[dict]:
    """Extract callback configuration from every _SessionRow in the pane."""
    result = []
    for w in pane._walker:
        if isinstance(w, _SessionRow):
            result.append({
                "session_id": w.session.session_id,
                "has_on_click": w._on_click is not None,
                "has_on_double_click": w._on_double_click is not None,
            })
    return result


# ── initial state ────────────────────────────────────────────────────────

def test_initial_state_shows_placeholder():
    pane = SessionsPane(on_select=lambda s: None)
    assert len(pane._walker) == 1
    assert isinstance(pane._walker[0], urwid.Text)


def test_empty_state_updates_for_current_provider():
    pane = SessionsPane(
        on_select=lambda _session: None, provider_label="Claude Code")
    assert "Select a Claude Code project" in pane._walker[0].text

    pane.set_provider_label("Codex")
    assert "Select a Codex project" in pane._walker[0].text

    project = _project()
    pane.set_sessions(project, [], running_ids=set(), favorite_ids=set())
    assert "No Codex sessions yet" in pane._walker[0].text
    assert "Press n to start one" in pane._walker[0].text


# ── callback dispatch ────────────────────────────────────────────────────

def test_non_running_sessions_get_preview_and_open():
    """Non-running sessions: on_click=preview, on_double_click=open (when on_preview set)."""
    preview_calls = []
    open_calls = []
    completed = []
    pane = SessionsPane(
        on_select=lambda s, **kw: open_calls.append((s.session_id, kw["steal_focus"])),
        on_preview=lambda s: preview_calls.append(s.session_id),
        on_double_detected=lambda: completed.append(True),
    )
    proj = _project()
    s = _session(proj)

    pane.set_sessions(proj, [s], running_ids=set(), favorite_ids=set())
    rows = [w for w in pane._walker if isinstance(w, _SessionRow)]
    assert len(rows) == 1
    r = rows[0]
    assert r._on_click is not None  # preview callback
    assert r._on_double_click is not None  # open callback

    # Fire callbacks to verify they dispatch correctly
    r._on_click()
    assert preview_calls == [s.session_id]
    r._on_double_click()
    assert open_calls == [(s.session_id, False)]
    assert completed == [True]


def test_running_sessions_get_click_and_double_click():
    """Running-session double-click defers focus until tmux settles."""
    open_calls = []
    completed = []
    pane = SessionsPane(
        on_select=lambda s, **kw: open_calls.append((s.session_id, kw.get("steal_focus", True))),
        on_preview=lambda s: None,
        on_double_detected=lambda: completed.append(True),
    )
    proj = _project()
    s = _session(proj)

    pane.set_sessions(proj, [s], running_ids={s.session_id}, favorite_ids=set())
    rows = [w for w in pane._walker if isinstance(w, _SessionRow)]
    assert len(rows) == 1
    r = rows[0]
    assert r._on_click is not None
    assert r._on_double_click is not None

    r._on_click()
    r._on_double_click()
    assert open_calls == [
        (s.session_id, False),  # click → no focus steal
        (s.session_id, False),  # double-click → App focuses after tmux settles
    ]
    assert completed == [True]


def test_stale_preview_callback_rechecks_live_running_session(monkeypatch):
    """A row rendered just before the running registry changed must not open
    history for an agent whose tmux session is live at click time."""
    from railmux import tmux_ctl
    from railmux.config import Config
    from railmux.ui.app import App, _Running
    from railmux.ui.workspace import AgentWorkspace

    project = _project()
    session = _session(project)
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._config = Config()
    app._auto_launched = False
    app._railmux_pane_id = None
    app._running = {}
    app._has_less = True
    attached = []
    previewed = []
    monkeypatch.setattr(tmux_ctl, "session_exists", lambda _name: True)
    monkeypatch.setattr(
        app, "_on_running_select",
        lambda entry, **kwargs: attached.append((entry.tmux_name, kwargs)),
    )
    monkeypatch.setattr(app, "_cancel_pending_double_focus", lambda: None)
    monkeypatch.setattr(
        app, "_show_transcript",
        lambda *_args, **_kwargs: previewed.append(True) or True,
    )
    monkeypatch.setattr(app, "_set_active_target", lambda *_args: None)
    monkeypatch.setattr(app, "_set_status", lambda *_args: None)

    pane = SessionsPane(
        on_select=lambda *_args, **_kwargs: None,
        on_preview=app._on_session_preview,
    )
    pane.set_sessions(project, [session], running_ids=set(), favorite_ids=set())
    row = next(w for w in pane._walker if isinstance(w, _SessionRow))
    app._running[session.session_id] = _Running(
        key=session.session_id,
        tmux_name="cx-live",
        label="test-proj/Test Session",
        project=project,
        session_type="codex",
    )

    assert row._on_click is not None
    row._on_click()

    assert attached == [("cx-live", {"steal_focus": False})]
    assert previewed == []


def test_stale_running_registry_does_not_hide_stopped_preview(monkeypatch):
    """A dead tmux entry is not enough to turn a stopped row into an attach."""
    from railmux import tmux_ctl
    from railmux.config import Config
    from railmux.ui.app import App, _Running
    from railmux.ui.workspace import AgentWorkspace

    project = _project()
    session = _session(project)
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._config = Config()
    app._auto_launched = False
    app._railmux_pane_id = None
    app._running = {
        session.session_id: _Running(
            key=session.session_id,
            tmux_name="cx-dead",
            label="test-proj/Test Session",
            project=project,
            session_type="codex",
        ),
    }
    app._has_less = True
    app._primary_slot.in_history_mode = False
    attached = []
    previewed = []
    monkeypatch.setattr(tmux_ctl, "session_exists", lambda _name: False)
    monkeypatch.setattr(
        app, "_on_running_select",
        lambda *_args, **_kwargs: attached.append(True),
    )
    monkeypatch.setattr(app, "_cancel_pending_double_focus", lambda: None)
    monkeypatch.setattr(app, "_save_restore_state", lambda: None)
    monkeypatch.setattr(
        app, "_show_transcript",
        lambda *_args, **_kwargs: previewed.append(True) or True,
    )
    monkeypatch.setattr(app, "_set_active_target", lambda *_args: None)
    monkeypatch.setattr(app, "_set_status", lambda *_args: None)

    app._on_session_preview(session)

    assert attached == []
    assert previewed == [True]


def test_agent_session_liveness_uses_displayed_swap_pane(monkeypatch):
    """A swap placeholder session must not mask death of the real pane."""
    from types import SimpleNamespace

    from railmux import tmux_ctl
    from railmux.ui.app import App

    app = App.__new__(App)
    transport = SimpleNamespace(
        displayed_real_pane=lambda _name: "%real",
    )
    monkeypatch.setattr(app, "_display_transport", lambda: transport)
    monkeypatch.setattr(tmux_ctl, "pane_alive", lambda _pane: False)
    monkeypatch.setattr(tmux_ctl, "session_exists", lambda _name: True)

    assert not app._agent_session_alive("cx-placeholder-home")


def test_non_running_no_preview_callback():
    """When on_preview is None, non-running sessions have no on_click."""
    pane = SessionsPane(on_select=lambda s: None, on_preview=None)
    proj = _project()
    s = _session(proj)

    pane.set_sessions(proj, [s], running_ids=set(), favorite_ids=set())
    row = [w for w in pane._walker if isinstance(w, _SessionRow)][0]
    assert row._on_click is None
    assert row._on_double_click is not None  # still has open


def test_mixed_running_and_non_running():
    """Three sessions: one running, two non-running.  Verify each gets correct callbacks."""
    preview_calls = []
    open_calls = []
    pane = SessionsPane(
        on_select=lambda s: open_calls.append(s.session_id),
        on_preview=lambda s: preview_calls.append(s.session_id),
    )
    proj = _project()
    running = _session(proj, session_id="a" * 36, title="Running")
    idle1 = _session(proj, session_id="b" * 36, title="Idle 1")
    idle2 = _session(proj, session_id="c" * 36, title="Idle 2")

    pane.set_sessions(proj, [running, idle1, idle2],
                      running_ids={"a" * 36}, favorite_ids=set())

    cb = _extract_callbacks(pane)
    assert len(cb) == 3

    running_cb = [c for c in cb if c["session_id"] == "a" * 36][0]
    assert running_cb["has_on_click"] and running_cb["has_on_double_click"]

    for sid in ("b" * 36, "c" * 36):
        idle_cb = [c for c in cb if c["session_id"] == sid][0]
        assert idle_cb["has_on_click"] and idle_cb["has_on_double_click"]


# ── + New session row ────────────────────────────────────────────────────

def test_new_session_row_click():
    called = []
    pane = SessionsPane(on_select=lambda s: called.append(s))
    proj = _project()
    pane.set_sessions(proj, [_session(proj)], running_ids=set(), favorite_ids=set())

    # The _new_row should call on_select(None)
    pane._new_row._on_click()
    assert called == [None]


# ── set_sessions / set_filter ────────────────────────────────────────────

def test_set_sessions_no_project():
    pane = SessionsPane(on_select=lambda s: None)
    pane.set_sessions(None, [], running_ids=set(), favorite_ids=set())
    # Should show placeholder text
    assert len(pane._walker) == 1
    assert isinstance(pane._walker[0], urwid.Text)


def test_set_sessions_unchanged_preserves_rows(monkeypatch):
    monkeypatch.setattr("railmux.ui.sessions_pane.time.time", lambda: 2050.0)
    pane = SessionsPane(on_select=lambda s: None)
    project = _project()
    session = _session(project)
    pane.set_sessions(
        project, [session], running_ids=set(), favorite_ids=set())
    row = next(w for w in pane._walker if isinstance(w, _SessionRow))

    pane.set_sessions(
        project, [session], running_ids=set(), favorite_ids=set())

    assert pane._walker[0] is row


def test_set_sessions_refreshes_when_relative_time_changes(monkeypatch):
    monkeypatch.setattr("railmux.ui.sessions_pane.time.time", lambda: 2050.0)
    pane = SessionsPane(on_select=lambda s: None)
    project = _project()
    session = _session(project)
    pane.set_sessions(
        project, [session], running_ids=set(), favorite_ids=set())
    row = next(w for w in pane._walker if isinstance(w, _SessionRow))

    monkeypatch.setattr("railmux.ui.sessions_pane.time.time", lambda: 2061.0)
    pane.set_sessions(
        project, [session], running_ids=set(), favorite_ids=set())

    assert pane._walker[0] is not row


def test_set_filter_filters_by_title():
    pane = SessionsPane(on_select=lambda s: None)
    proj = _project()
    s1 = _session(proj, session_id="a" * 36, title="Shopping research")
    s2 = _session(proj, session_id="b" * 36, title="Refactor railmux")

    pane.set_sessions(proj, [s1, s2], running_ids=set(), favorite_ids=set())
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 2

    pane.set_filter("shop")
    visible = [w for w in pane._walker if isinstance(w, _SessionRow)]
    assert len(visible) == 1
    assert visible[0].session.session_id == "a" * 36

    pane.set_filter("")
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 2


def test_set_filter_no_matches():
    pane = SessionsPane(on_select=lambda s: None)
    proj = _project()
    pane.set_sessions(proj, [_session(proj)], running_ids=set(), favorite_ids=set())
    pane.set_filter("nonexistent")
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 0
    assert isinstance(pane._walker[0], urwid.Text)
    assert "no matches" in pane._walker[0].text.lower()


# ── Enter key ────────────────────────────────────────────────────────────

def test_enter_on_session_row_opens():
    open_calls = []
    pane = SessionsPane(on_select=lambda s: open_calls.append(s))
    proj = _project()
    s = _session(proj, session_id="x" * 36)
    pane.set_sessions(proj, [s], running_ids=set(), favorite_ids=set())

    # Move focus to the session row: set pile focus to listbox (position 2)
    # then focus the first row in the walker (the session).
    pane._pile.focus_position = 2
    pane._walker.set_focus(0)
    result = pane.keypress((20, 10), "enter")
    assert result is None  # consumed
    assert len(open_calls) == 1
    assert open_calls[0].session_id == "x" * 36


def test_enter_on_new_session_row():
    open_calls = []
    pane = SessionsPane(on_select=lambda s: open_calls.append(s))
    proj = _project()
    pane.set_sessions(proj, [_session(proj)], running_ids=set(), favorite_ids=set())

    # Focus on the _new_row (position 0 in the pile)
    pane._pile.focus_position = 0
    result = pane.keypress((20, 10), "enter")
    assert result is None
    assert open_calls == [None]


# ── Enter key steals focus ───────────────────────────────────────────────

def test_enter_on_running_session_steals_focus():
    """Enter always steals focus (equivalent to double-click), even for running
    sessions that single-click without stealing."""
    open_calls = []
    pane = SessionsPane(
        on_select=lambda s, **kw: open_calls.append(kw.get("steal_focus", True)),
    )
    proj = _project()
    s = _session(proj)
    pane.set_sessions(proj, [s], running_ids={s.session_id}, favorite_ids=set())

    pane._pile.focus_position = 2
    pane._walker.set_focus(0)
    result = pane.keypress((20, 10), "enter")
    assert result is None
    assert open_calls == [True]  # Enter steals focus


def test_enter_on_non_running_session_steals_focus():
    open_calls = []
    pane = SessionsPane(
        on_select=lambda s, **kw: open_calls.append(kw.get("steal_focus", True)),
    )
    proj = _project()
    s = _session(proj)
    pane.set_sessions(proj, [s], running_ids=set(), favorite_ids=set())

    pane._pile.focus_position = 2
    pane._walker.set_focus(0)
    result = pane.keypress((20, 10), "enter")
    assert result is None
    assert open_calls == [True]  # Enter always steals


# ── status dots ─────────────────────────────────────────────────────────

def test_status_dot_idle():
    from railmux.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["idle"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_idle"
    assert dot[1] == "●"


def test_status_dot_busy():
    from railmux.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["busy"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_busy"


def test_status_dot_blocked():
    from railmux.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["blocked"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_blocked"


def test_running_session_row_renders_status_dot():
    proj = _project()
    s = _session(proj, title="Busy Session")
    # Force a non-idle status
    s = SessionMeta(project=s.project, session_id=s.session_id,
                    jsonl_path=s.jsonl_path, title=s.title,
                    message_count=s.message_count, token_total=s.token_total,
                    last_mtime=s.last_mtime, status="busy")
    pane = SessionsPane(on_select=lambda s: None)
    pane.set_sessions(
        proj, [s], running_ids={s.session_id}, favorite_ids=set())
    row = [w for w in pane._walker if isinstance(w, _SessionRow)][0]
    title = row._wrapped_widget.base_widget.contents[0][0]
    assert title.text.startswith("●")
    assert title.attrib[0][0] == "status_busy"


def test_stopped_session_row_uses_neutral_hollow_marker():
    proj = _project()
    s = _session(proj, title="Stopped Session")
    s = SessionMeta(project=s.project, session_id=s.session_id,
                    jsonl_path=s.jsonl_path, title=s.title,
                    message_count=s.message_count, token_total=s.token_total,
                    last_mtime=s.last_mtime, status="blocked")

    row = _SessionRow(s, is_running=False)
    title = row._wrapped_widget.base_widget.contents[0][0]

    assert title.text.startswith("○")
    assert title.attrib[0] == ("dim", 1)


# ── attribute maps ──────────────────────────────────────────────────────

def test_focus_remap_includes_status_dots():
    from railmux.ui.sessions_pane import _FOCUS_REMAP
    for key in ("status_idle", "status_busy", "status_blocked"):
        assert key in _FOCUS_REMAP, f"{key} missing from _FOCUS_REMAP"
        assert "focus" in _FOCUS_REMAP[key], \
            f"{key} focus variant should contain 'focus'"


def test_selected_map_includes_status_dots():
    from railmux.ui.sessions_pane import _SELECTED_MAP
    for key in ("status_idle", "status_busy", "status_blocked"):
        assert key in _SELECTED_MAP, f"{key} missing from _SELECTED_MAP"
        assert "sel" in _SELECTED_MAP[key], \
            f"{key} selected variant should contain 'sel'"


def test_star_is_plain_text_no_palette():
    """Star should be plain text (inherits row highlight), not a palette tuple."""
    proj = _project()
    s = _session(proj)
    pane = SessionsPane(on_select=lambda s: None)
    pane.set_sessions(proj, [s], running_ids=set(),
                      favorite_ids={s.session_id})
    row = [w for w in pane._walker if isinstance(w, _SessionRow)][0]
    # _SessionRow > WidgetWrap._w > AttrMap > Pile > (title_text, meta_text)
    pile = row._wrapped_widget.base_widget
    title_text = pile.contents[0][0]  # Pile row 0 col 0
    markup = title_text.base_widget.text
    # urwid may flatten markup; just verify the star is present
    assert "★" in str(markup), "star not found in title markup"


def _selected_session_ids(pane: SessionsPane) -> set[str]:
    return {
        row.session.session_id
        for row in pane._walker
        if isinstance(row, _SessionRow)
        and row._wrapped_widget.attr_map.get(None) == "selected"
    }


def test_active_session_persists_after_context_selection_clears():
    proj = _project()
    active = _session(proj, session_id="a" * 36, title="Active")
    context = _session(proj, session_id="b" * 36, title="Context")
    pane = SessionsPane(on_select=lambda s: None)
    pane.set_sessions(proj, [active, context])

    pane.set_active_session(active.session_id)
    assert _selected_session_ids(pane) == {active.session_id}

    pane.set_selected_session(context.session_id)
    assert _selected_session_ids(pane) == {context.session_id}

    pane.set_selected_session(None)
    assert _selected_session_ids(pane) == {active.session_id}


def test_session_title_no_longer_renders_live_badge():
    proj = _project()
    session = _session(proj)
    pane = SessionsPane(on_select=lambda s: None)
    pane.set_sessions(proj, [session])
    row = next(w for w in pane._walker if isinstance(w, _SessionRow))
    title = row._wrapped_widget.base_widget.contents[0][0].text
    assert "[LIVE]" not in title
