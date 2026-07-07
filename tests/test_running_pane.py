"""Tests for ccmgr.ui.running_pane — callback dispatch (click vs double-click)."""

import urwid

from ccmgr.ui.running_pane import RunningEntry, RunningSessionsPane, _RunningRow


def _entry(name: str = "cc-abc123", label: str = "proj/Test") -> RunningEntry:
    return RunningEntry(tmux_name=name, label=label)


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


def test_running_row_double_click_steals():
    """Double-click on a running row calls on_select with steal_focus=True (default)."""
    calls = []
    pane = RunningSessionsPane(
        on_select=lambda e, **kw: calls.append((e.tmux_name, kw.get("steal_focus", True)))
    )
    pane.set_running([_entry("cc-x")])

    row = [w for w in pane._walker if isinstance(w, _RunningRow)][0]
    assert row._on_double_click is not None
    row._on_double_click()
    assert calls == [("cc-x", True)]


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
    """Enter on a running row steals focus (default behavior, like double-click)."""
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


# ── status dots ─────────────────────────────────────────────────────────

def test_running_entry_default_status():
    e = RunningEntry(tmux_name="cc-x", label="test")
    assert e.status == "idle"


def test_running_row_uses_status_dot():
    from ccmgr.ui.sessions_pane import _STATUS_DOTS
    e = RunningEntry(tmux_name="cc-x", label="test", status="busy")
    row = _RunningRow(e)
    # Row renders without error with tuple status dot
    assert row is not None
