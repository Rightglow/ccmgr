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


class ClickableRow(urwid.WidgetWrap):
    """A selectable list row that handles single- and double-click.

    When *on_double_click* is set, the first left press *does not* fire
    *on_click* immediately.  Instead a timer is started for
    ``_DOUBLE_CLICK_INTERVAL`` (500 ms, the OS standard).  If a second
    press arrives within the window the timer is cancelled and
    *on_double_click* fires; otherwise the timer fires *on_click*.

    When only *on_click* is set (no *on_double_click*) the callback fires
    immediately on the first press — no delay is needed because there is
    nothing to disambiguate.

    .. attribute:: _main_loop

       Must be set before any widget receives mouse events (``App.run()``
       does this).  When ``None`` (e.g. in tests) the single-click action
       fires immediately even when a double-click callback is set.
    """

    _DOUBLE_CLICK_INTERVAL = 0.5
    _main_loop: urwid.MainLoop | None = None

    def __init__(self, widget: urwid.Widget,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None) -> None:
        self._on_click = on_click
        self._on_double_click = on_double_click
        self._on_right_click = on_right_click
        self._last_click_ts: float = 0.0
        self._pending_alarm: object | None = None
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
                if now - self._last_click_ts < self._DOUBLE_CLICK_INTERVAL:
                    self._last_click_ts = 0.0
                    self._cancel_pending()
                    self._on_double_click()
                    return True
                self._last_click_ts = now

                if self._on_click is not None:
                    self._schedule_after(self._DOUBLE_CLICK_INTERVAL,
                                         self._fire_click)
                    return True
                return True

            if self._on_click is not None:
                self._on_click()
                return True

        # ── right-click (button 3) ─────────────────────────────────
        if button == 3 and self._on_right_click is not None:
            self._on_right_click()
            return True

        return super().mouse_event(size, event, button, col, row, focus)

    # ── alarm helpers ─────────────────────────────────────────────────

    def _schedule_after(self, delay: float,
                        callback: Callable[[], None]) -> None:
        if self._main_loop is not None:
            self._pending_alarm = self._main_loop.set_alarm_in(
                delay, lambda _loop, _ud: callback())
        else:
            # No main loop (e.g. unit tests) — fire immediately so tests
            # see consistent callback counts regardless of timing.
            callback()

    def _fire_click(self) -> None:
        self._pending_alarm = None
        if self._on_click is not None:
            self._on_click()

    def _cancel_pending(self) -> None:
        if self._pending_alarm is not None:
            if self._main_loop is not None:
                try:
                    self._main_loop.remove_alarm(self._pending_alarm)
                except Exception:
                    pass
            self._pending_alarm = None


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
