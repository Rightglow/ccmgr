"""Tests for ccmgr.ui.sessions_pane — callback dispatch (preview vs open)."""

from pathlib import Path

import pytest
import urwid

from ccmgr.models import Project, SessionMeta
from ccmgr.ui.sessions_pane import SessionsPane, _SessionRow, _NewSessionRow


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
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300)
    assert len(pane._walker) == 1
    assert isinstance(pane._walker[0], urwid.Text)


# ── callback dispatch ────────────────────────────────────────────────────

def test_non_running_sessions_get_preview_and_open():
    """Non-running sessions: on_click=preview, on_double_click=open (when on_preview set)."""
    preview_calls = []
    open_calls = []
    pane = SessionsPane(
        on_select=lambda s: open_calls.append(s.session_id),
        live_threshold=300,
        on_preview=lambda s: preview_calls.append(s.session_id),
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
    assert open_calls == [s.session_id]


def test_running_sessions_get_click_and_double_click():
    """Running sessions: on_click=attach (no focus steal), on_double_click=attach + steal."""
    open_calls = []
    pane = SessionsPane(
        on_select=lambda s, **kw: open_calls.append((s.session_id, kw.get("steal_focus", True))),
        live_threshold=300,
        on_preview=lambda s: None,
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
        (s.session_id, True),   # double-click → steals focus
    ]


def test_non_running_no_preview_callback():
    """When on_preview is None, non-running sessions have no on_click."""
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300, on_preview=None)
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
        live_threshold=300,
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
    pane = SessionsPane(on_select=lambda s: called.append(s), live_threshold=300)
    proj = _project()
    pane.set_sessions(proj, [_session(proj)], running_ids=set(), favorite_ids=set())

    # The _new_row should call on_select(None)
    pane._new_row._on_click()
    assert called == [None]


# ── set_sessions / set_filter ────────────────────────────────────────────

def test_set_sessions_no_project():
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300)
    pane.set_sessions(None, [], running_ids=set(), favorite_ids=set())
    # Should show placeholder text
    assert len(pane._walker) == 1
    assert isinstance(pane._walker[0], urwid.Text)


def test_set_filter_filters_by_title():
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300)
    proj = _project()
    s1 = _session(proj, session_id="a" * 36, title="Shopping research")
    s2 = _session(proj, session_id="b" * 36, title="Refactor ccmgr")

    pane.set_sessions(proj, [s1, s2], running_ids=set(), favorite_ids=set())
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 2

    pane.set_filter("shop")
    visible = [w for w in pane._walker if isinstance(w, _SessionRow)]
    assert len(visible) == 1
    assert visible[0].session.session_id == "a" * 36

    pane.set_filter("")
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 2


def test_set_filter_no_matches():
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300)
    proj = _project()
    pane.set_sessions(proj, [_session(proj)], running_ids=set(), favorite_ids=set())
    pane.set_filter("nonexistent")
    assert len([w for w in pane._walker if isinstance(w, _SessionRow)]) == 0
    assert isinstance(pane._walker[0], urwid.Text)
    assert "no matches" in pane._walker[0].text.lower()


# ── Enter key ────────────────────────────────────────────────────────────

def test_enter_on_session_row_opens():
    open_calls = []
    pane = SessionsPane(on_select=lambda s: open_calls.append(s), live_threshold=300)
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
    pane = SessionsPane(on_select=lambda s: open_calls.append(s), live_threshold=300)
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
        live_threshold=300,
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
        live_threshold=300,
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
    from ccmgr.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["idle"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_idle"
    assert dot[1] == "●"


def test_status_dot_busy():
    from ccmgr.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["busy"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_busy"


def test_status_dot_blocked():
    from ccmgr.ui.sessions_pane import _STATUS_DOTS
    dot = _STATUS_DOTS["blocked"]
    assert isinstance(dot, tuple)
    assert dot[0] == "status_blocked"


def test_session_row_renders_status_dot():
    proj = _project()
    s = _session(proj, title="Busy Session")
    # Force a non-idle status
    s = SessionMeta(project=s.project, session_id=s.session_id,
                    jsonl_path=s.jsonl_path, title=s.title,
                    message_count=s.message_count, token_total=s.token_total,
                    last_mtime=s.last_mtime, status="busy")
    pane = SessionsPane(on_select=lambda s: None, live_threshold=300)
    pane.set_sessions(proj, [s], running_ids=set(), favorite_ids=set())
    row = [w for w in pane._walker if isinstance(w, _SessionRow)][0]
    # The row should render — no crash on tuple status dot
    assert row is not None
