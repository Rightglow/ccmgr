"""Tests for ccmgr.ui._widgets — ClickableRow mouse / double-click."""

import time
from unittest.mock import MagicMock

import pytest
import urwid

from ccmgr.ui._widgets import ClickableRow, remember_focus, restore_focus


# ── helpers ──────────────────────────────────────────────────────────────

def _make_row(on_click=None, on_double_click=None,
              on_right_click=None) -> ClickableRow:
    return ClickableRow(urwid.Text("test row"), on_click, on_double_click,
                        on_right_click)


def _click(row: ClickableRow, button: int = 1) -> bool:
    """Simulate a left mouse press on *row*.  Returns the handled flag."""
    return row.mouse_event((20,), "mouse press", button, 0, 0, focus=True)


# ── single click ─────────────────────────────────────────────────────────

def test_single_click_fires_on_click():
    called = []
    row = _make_row(on_click=lambda: called.append(1))
    assert _click(row)
    assert called == [1]


def test_single_click_no_callbacks_does_not_crash():
    row = _make_row()
    # Should not raise; urwid default handles focus
    result = _click(row)
    assert not result  # not handled by our code, falls through


def test_single_click_on_click_none_but_double_set():
    """When on_click is None but on_double_click is set, the first click is
    consumed (records timestamp) but the double-click callback is not fired."""
    dbl_called = []
    row = _make_row(on_double_click=lambda: dbl_called.append(1))
    result = _click(row)
    assert result  # consumed, urwid should not act on it
    assert dbl_called == []  # not fired on first click


def test_middle_click_ignored():
    called = []
    row = _make_row(on_click=lambda: called.append(1))
    result = row.mouse_event((20,), "mouse press", 2, 0, 0, focus=True)
    assert not result  # falls through
    assert called == []  # not called


def test_right_click_ignored():
    called = []
    row = _make_row(on_click=lambda: called.append(1))
    result = row.mouse_event((20,), "mouse press", 3, 0, 0, focus=True)
    assert not result
    assert called == []


def test_mouse_release_not_handled():
    """Only 'mouse press' fires callbacks, not 'mouse release'."""
    called = []
    row = _make_row(on_click=lambda: called.append(1))
    result = row.mouse_event((20,), "mouse release", 1, 0, 0, focus=True)
    assert not result
    assert called == []


# ── double click ─────────────────────────────────────────────────────────

def test_double_click_fires_on_double_click(monkeypatch):
    """Two rapid clicks within the threshold fire on_double_click, not on_click."""
    click_called = []
    dbl_called = []
    row = _make_row(on_click=lambda: click_called.append(1),
                    on_double_click=lambda: dbl_called.append(1))

    # First click — records timestamp, fires on_click
    assert _click(row)
    assert click_called == [1]
    assert dbl_called == []

    # Advance time just under the threshold
    fake_now = time.monotonic() + 0.3
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)

    # Second click — within threshold → double-click
    assert _click(row)
    assert click_called == [1]  # NOT called again
    assert dbl_called == [1]  # double-click fired


def test_slow_clicks_fire_on_click_twice(monkeypatch):
    """Two clicks spaced beyond the threshold both fire on_click."""
    click_called = []
    dbl_called = []
    row = _make_row(on_click=lambda: click_called.append(1),
                    on_double_click=lambda: dbl_called.append(1))

    assert _click(row)
    assert click_called == [1]

    # Advance beyond the 500 ms threshold
    fake_now = time.monotonic() + 0.6
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)

    assert _click(row)
    assert click_called == [1, 1]  # fired again
    assert dbl_called == []  # double-click NOT fired


def test_double_click_resets_after_firing(monkeypatch):
    """After a double-click, the next click is treated as a new first click."""
    dbl_called = []
    click_called = []
    row = _make_row(on_click=lambda: click_called.append(1),
                    on_double_click=lambda: dbl_called.append(1))

    # Double-click
    assert _click(row)  # first
    fake_now = time.monotonic() + 0.2
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert _click(row)  # second → double-click
    assert dbl_called == [1]

    # Third click — _last_click_ts was reset to 0.0, so it's a new first click
    fake_now = time.monotonic() + 0.3
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert _click(row)
    assert dbl_called == [1]  # NOT another double-click
    assert click_called == [1, 1]  # two on_click calls total


def test_double_click_without_on_click(monkeypatch):
    """Double-click works even when on_click is None."""
    dbl_called = []
    row = _make_row(on_double_click=lambda: dbl_called.append(1))

    assert _click(row)  # first click — consumed, records timestamp
    # Simulate first click recorded, then advance time
    row._last_click_ts = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: row._last_click_ts + 0.2)
    assert _click(row)  # second → double-click
    assert dbl_called == [1]


# ── selectable / keypress ────────────────────────────────────────────────

def test_selectable():
    row = _make_row()
    assert row.selectable()


def test_keypress_passes_through():
    row = _make_row()
    assert row.keypress((20,), "enter") == "enter"
    assert row.keypress((20,), "j") == "j"


# ── remember_focus / restore_focus ───────────────────────────────────────

class _TestRow(ClickableRow):
    def __init__(self, row_id: str):
        self.row_id = row_id
        super().__init__(urwid.Text(row_id))


def test_remember_restore_focus_round_trip():
    rows = [_TestRow("a"), _TestRow("b"), _TestRow("c")]
    walker = urwid.SimpleFocusListWalker(rows)

    walker.set_focus(1)  # focus on "b"
    key = remember_focus(walker, _TestRow, lambda r: r.row_id)
    assert key == "b"

    # Rebuild walker with new instances
    new_rows = [_TestRow("a"), _TestRow("b"), _TestRow("c")]
    new_walker = urwid.SimpleFocusListWalker(new_rows)

    restore_focus(new_walker, _TestRow, "b", lambda r: r.row_id)
    focus_w, _ = new_walker.get_focus()
    assert focus_w.row_id == "b"


def test_restore_focus_falls_back_to_first_row():
    walker = urwid.SimpleFocusListWalker([_TestRow("x"), _TestRow("y")])
    restore_focus(walker, _TestRow, "nonexistent", lambda r: r.row_id)
    focus_w, _ = walker.get_focus()
    assert focus_w.row_id == "x"


def test_restore_focus_empty_walker():
    walker = urwid.SimpleFocusListWalker([])
    restore_focus(walker, _TestRow, "a", lambda r: r.row_id)
    # Should not raise


# ── right-click ─────────────────────────────────────────────────────────

def test_right_click_fires_on_right_click():
    called = []
    row = _make_row(on_right_click=lambda: called.append(1))
    result = row.mouse_event((20,), "mouse press", 3, 0, 0, focus=True)
    assert result  # handled
    assert called == [1]


def test_right_click_does_not_fire_on_click():
    click_called = []
    right_called = []
    row = _make_row(on_click=lambda: click_called.append(1),
                    on_right_click=lambda: right_called.append(1))
    row.mouse_event((20,), "mouse press", 3, 0, 0, focus=True)
    assert click_called == []
    assert right_called == [1]


def test_right_click_no_callback_falls_through():
    row = _make_row()
    result = row.mouse_event((20,), "mouse press", 3, 0, 0, focus=True)
    assert not result
