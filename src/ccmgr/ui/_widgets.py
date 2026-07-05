"""Shared building blocks for the sidebar panes.

The three panes (projects / sessions / running) and the path browser all use
the same "selectable row that fires on left-click" widget and the same
remember-focus-then-restore-after-rebuild dance. Those live here so each pane
only defines its own markup and identity key.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import urwid


class ClickableRow(urwid.WidgetWrap):
    """A selectable list row that invokes ``on_click`` on a left mouse press.

    Subclasses build their markup and call ``super().__init__(widget, on_click)``.
    """

    def __init__(self, widget: urwid.Widget,
                 on_click: "Callable[[], None] | None" = None) -> None:
        self._on_click = on_click
        super().__init__(widget)

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button == 1 and self._on_click:
            self._on_click()
            return True
        return super().mouse_event(size, event, button, col, row, focus)


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
