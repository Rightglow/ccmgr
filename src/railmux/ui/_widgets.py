"""Shared building blocks for the sidebar panes.

The three panes (projects / sessions / running) and the path browser all use
the same "selectable row that fires on left-click" widget and the same
remember-focus-then-restore-after-rebuild dance. Those live here so each pane
only defines its own markup and identity key.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import urwid


class ScrollableSidebarPane:
    """Route wheel presses anywhere in pane chrome to its ``ListBox``.

    tmux targets mouse events by pointer coordinates, not keyboard focus.  A
    pane title, border, pinned action row, or divider should therefore scroll
    the list under it exactly like a body row.  Subclasses provide the number
    of rows occupied by their LineBox plus fixed inner chrome.
    """

    _listbox: urwid.ListBox

    def _wheel_chrome_rows(self) -> int:
        return 2  # LineBox top and bottom borders

    def _wheel_border_columns(self) -> int:
        return 2  # LineBox left and right borders

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button in (4, 5):
            if len(size) >= 2:
                maxcol, maxrow = size[:2]
                rows = maxrow - self._wheel_chrome_rows()
                columns = maxcol - self._wheel_border_columns()
                if columns > 0 and rows > 0:
                    self._listbox.keypress(
                        (columns, rows),
                        "up" if button == 4 else "down",
                    )
            # Consume even at a boundary or critically-small geometry so the
            # event cannot leak into a sibling pane or tmux copy mode.
            return True
        return super().mouse_event(size, event, button, col, row, focus)


class ClickableRow(urwid.WidgetWrap):
    """A selectable list row that handles single- and double-click.

    When *on_double_click* is set, the first left press *does not* fire
    *on_click* immediately.  Instead a timer is started for
    ``_DOUBLE_CLICK_INTERVAL`` (500 ms, the OS standard).  If a second
    press arrives within the window the timer is cancelled and
    *on_double_click* fires; otherwise the timer fires *on_click*.

    With *immediate_click*, *on_click* fires on the first press while the same
    press still starts the double-click window. This suits cheap, reversible
    selection/attach actions where waiting 500 ms feels like input lag.

    When only *on_click* is set (no *on_double_click*) the callback fires
    immediately on the first press — no delay is needed because there is
    nothing to disambiguate.

    **Double-click state is stored at class level**, keyed by *click_key*,
    so that polling-driven row rebuilds do not reset the 500 ms window.
    Two presses on different instances of the same logical row (same
    *click_key*) are still recognised as a double-click.

    .. attribute:: _main_loop

       Must be set before any widget receives mouse events (``App.run()``
       does this).  When ``None`` (e.g. in tests) the single-click action
       fires immediately even when a double-click callback is set.
    """

    _DOUBLE_CLICK_INTERVAL = 0.5
    _main_loop: urwid.MainLoop | None = None

    # Class-level double-click state — survives row rebuilds from polling.
    _last_click_key: str | None = None
    _last_click_ts: float = 0.0
    _pending_alarm: object | None = None
    _pending_click_cb: Callable[[], None] | None = None

    def __init__(self, widget: urwid.Widget,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None,
                 click_key: str | None = None,
                 immediate_click: bool = False) -> None:
        self._on_click = on_click
        self._on_double_click = on_double_click
        self._on_right_click = on_right_click
        self._click_key = click_key
        self._immediate_click = immediate_click
        super().__init__(widget)

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key

    # ── mouse ─────────────────────────────────────────────────────────

    def mouse_event(self, size, event, button, col, row, focus):
        if event != "mouse press":
            return super().mouse_event(size, event, button, col, row, focus)

        # ── left-click (button 1) ──────────────────────────────────
        if button == 1:
            if self._on_double_click is not None:
                now = time.monotonic()
                key = self._click_key
                if (key is not None
                        and ClickableRow._last_click_key is not None
                        and key == ClickableRow._last_click_key
                        and now - ClickableRow._last_click_ts < self._DOUBLE_CLICK_INTERVAL):
                    # Double-click — same logical row within the window,
                    # even if polling rebuilt the widget instance.
                    ClickableRow._last_click_ts = 0.0
                    ClickableRow._last_click_key = None
                    ClickableRow._cancel_pending()
                    self._on_double_click()
                    return True

                # First press (or different row, or window expired).
                # Cancel the previous pending single-click — the user
                # either moved to a different row or this is a fresh
                # first press on the same logical row after a rebuild.
                ClickableRow._cancel_pending()
                ClickableRow._last_click_key = key
                ClickableRow._last_click_ts = now

                if self._on_click is not None:
                    if self._immediate_click:
                        self._on_click()
                    else:
                        ClickableRow._pending_click_cb = self._on_click
                        ClickableRow._schedule_after(self._DOUBLE_CLICK_INTERVAL)
                return True

            if self._on_click is not None:
                self._on_click()
                return True

        # ── right-click (button 3) ─────────────────────────────────
        if button == 3 and self._on_right_click is not None:
            self._on_right_click()
            return True

        return super().mouse_event(size, event, button, col, row, focus)

    # ── alarm helpers (class-level — only one double-click is in
    #    flight at a time, so a single shared slot suffices) ──────────

    @classmethod
    def _schedule_after(cls, delay: float) -> None:
        if cls._main_loop is not None:
            cls._pending_alarm = cls._main_loop.set_alarm_in(
                delay, lambda _loop, _ud: cls._fire_click())
        else:
            # No main loop (e.g. unit tests) — fire immediately so tests
            # see consistent callback counts regardless of timing.
            cls._fire_click()

    @classmethod
    def _fire_click(cls) -> None:
        cls._pending_alarm = None
        cb = cls._pending_click_cb
        cls._pending_click_cb = None
        if cb is not None:
            cb()

    @classmethod
    def _cancel_pending(cls) -> None:
        if cls._pending_alarm is not None:
            if cls._main_loop is not None:
                try:
                    cls._main_loop.remove_alarm(cls._pending_alarm)
                except Exception:
                    pass
            cls._pending_alarm = None
        cls._pending_click_cb = None


def remember_focus(walker: urwid.SimpleFocusListWalker, row_type: type,
                   key_fn: Callable[[Any], str]) -> str | None:
    """Return an identity key for the focused row (if it's ``row_type``)."""
    if not walker:
        return None
    focus_w, _ = walker.get_focus()
    if isinstance(focus_w, row_type):
        return key_fn(focus_w)
    return None


def restore_focus(walker: urwid.SimpleFocusListWalker, row_type: type,
                  key: str | None, key_fn: Callable[[Any], str]) -> None:
    """Re-focus the row whose identity matches ``key``; else the first
    selectable row of ``row_type``; else index 0."""
    if not walker:
        return
    if key is not None:
        for i, w in enumerate(walker):
            if isinstance(w, row_type) and key_fn(w) == key:
                walker.set_focus(i)
                return
    for i, w in enumerate(walker):
        if isinstance(w, row_type):
            walker.set_focus(i)
            return
    walker.set_focus(0)
