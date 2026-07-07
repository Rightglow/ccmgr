"""Bottom-of-screen status + help-hint widgets."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from ccmgr.ui import keymap


# Generated from the single keymap source of truth so the hint bar can't drift
# from the actual dispatch.
HELP_HINT = keymap.hint_text()


class _HelpButton(urwid.WidgetWrap):
    """A compact clickable label for the trailing help-hint bar."""

    def __init__(self, label: str, on_click: Callable[[], None]) -> None:
        self._on_click = on_click
        super().__init__(urwid.AttrMap(urwid.Text(label), "help_btn"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key == "enter":
            self._on_click()
            return None
        return key

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button == 1:
            self._on_click()
            return True
        return super().mouse_event(size, event, button, col, row, focus)


class HelpBar(urwid.WidgetWrap):
    """Persistent key reference — two lines: actions, then utility/exit.

    The second line items are clickable buttons with a subtle background.
    """

    def __init__(self, on_help: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_detach: Callable[[], None]) -> None:
        main, trail = HELP_HINT.split("\n", 1)
        # Build clickable buttons from the trailing items.
        buttons: list = []
        for item in trail.split(" · "):
            label = " " + item + " "
            if "help" in item:
                btn = _HelpButton(label, on_help)
            elif "quit" in item:
                btn = _HelpButton(label, on_quit)
            elif "detach" in item:
                btn = _HelpButton(label, on_detach)
            else:
                btn = urwid.Text(label)
            buttons.append(("pack", btn))
        body = urwid.Pile([
            urwid.Text(main, align="left"),
            urwid.Columns(buttons, dividechars=1),
        ])
        super().__init__(urwid.AttrMap(body, "dim"))


class StatusBar(urwid.WidgetWrap):
    """Dynamic status message line. Use set_message to update."""

    def __init__(self) -> None:
        self._text = urwid.Text("", align="left")
        super().__init__(urwid.AttrMap(self._text, "statusbar"))

    def set_message(self, msg: str) -> None:
        self._text.set_text(msg)
