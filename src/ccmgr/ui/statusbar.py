"""Bottom-of-screen status + help-hint widgets."""
from __future__ import annotations

import urwid

from ccmgr.ui import keymap


# Generated from the single keymap source of truth so the hint bar can't drift
# from the actual dispatch.
HELP_HINT = keymap.hint_text()


class HelpBar(urwid.WidgetWrap):
    """One-line persistent key reference. Always visible."""

    def __init__(self) -> None:
        text = urwid.Text(HELP_HINT, align="left")
        super().__init__(urwid.AttrMap(text, "dim"))


class StatusBar(urwid.WidgetWrap):
    """Dynamic status message line. Use set_message to update."""

    def __init__(self) -> None:
        self._text = urwid.Text("", align="left")
        super().__init__(urwid.AttrMap(self._text, "statusbar"))

    def set_message(self, msg: str) -> None:
        self._text.set_text(msg)
