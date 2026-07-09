"""Bottom-of-screen status + help-hint widgets."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from ccmgr.ui import keymap


# Generated from the single keymap source of truth so the hint bar can't drift
# from the actual dispatch.
HELP_HINT = keymap.hint_text()


# Idle tips cycled through the status bar when there's no active message. These
# intentionally avoid the keys already listed in the always-visible HelpBar
# (n/r/s/d, /, i, ?, q, Ctrl-B ←/→) — they surface behaviour that isn't obvious
# from the hint bar, like soft-quit and history preview.
TIPS: tuple[str, ...] = (
    "Quit with q, then s to soft-quit — leaves every Claude session running",
    "Click a stopped session to preview its history without launching Claude",
    "F3 toggles fullscreen for the focused pane",
    "t opens a shell in the focused project's directory",
)

# Message severity → palette attribute. Idle tips share the neutral info style
# (same font/colour) so they don't read as a different kind of message; warn and
# error escalate so failures stand out.
_LEVEL_ATTR = {
    "error": "status_error",
    "warn": "status_warn",
    "info": "status_info",
    "tip": "status_info",
}


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
    """Two-line status bar — height is fixed so the sidebar never jitters.

    A ``Pile`` of two ``Text`` widgets. The message is soft-wrapped to the
    render width across both lines: it stays on line 1 when it fits, spills
    into line 2 when longer, and is truncated with an ellipsis only when it
    exceeds two lines. The second line is otherwise empty, guaranteeing a
    fixed 2-line height. The colour tracks the message level (info / warn /
    error / idle tip).
    """

    def __init__(self) -> None:
        self._line1 = urwid.Text("", align="left", wrap="clip")
        self._line2 = urwid.Text("", align="left", wrap="clip")
        self._text = ""
        self._level = "tip"
        body = urwid.Pile([self._line1, self._line2])
        self._attr = urwid.AttrMap(body, _LEVEL_ATTR["tip"])
        super().__init__(self._attr)

    def set_message(self, msg: str, level: str = "info") -> None:
        """Set the message text and severity; re-wrapped on next render."""
        self._text = msg or ""
        self._level = level if level in _LEVEL_ATTR else "info"
        self._attr.set_attr_map({None: _LEVEL_ATTR[self._level]})
        # Re-flow immediately for a best-effort width; render() re-wraps to the
        # real column count so this is only a fallback for width-less callers.
        self._reflow(80)

    def _reflow(self, maxcol: int) -> None:
        maxcol = max(1, maxcol)
        line1, rest = self._split_at_width(self._text, maxcol)
        self._line1.set_text(line1)
        if not rest:
            self._line2.set_text("")
            return
        line2, overflow = self._split_at_width(rest, maxcol)
        if overflow:
            # More than two lines' worth: truncate line 2 with an ellipsis so
            # it's clear the message was cut off. Reserve one column for "…".
            line2, _ = self._split_at_width(rest, max(1, maxcol - 1))
            line2 = line2.rstrip() + "…"
        self._line2.set_text(line2)

    @staticmethod
    def _split_at_width(text: str, maxcol: int) -> tuple[str, str]:
        """Split *text* into (head, tail) where head fits in *maxcol* display
        columns. Uses urwid's column arithmetic so wide (CJK) glyphs — which
        occupy two columns — wrap correctly; ``textwrap`` counts characters and
        would let a wide line overflow and get clipped instead of wrapping.
        Prefers to break on the last space within the width."""
        if not text:
            return "", ""
        pos, _ = urwid.calc_text_pos(text, 0, len(text), maxcol)
        if pos >= len(text):
            return text, ""
        # Prefer a word boundary, but only if it doesn't waste most of the
        # line — otherwise (e.g. CJK text whose only early space trails a lone
        # glyph) hard-break at the column limit.
        brk = text.rfind(" ", 0, pos)
        if brk > pos // 2:
            return text[:brk], text[brk + 1:]
        return text[:pos], text[pos:]

    def render(self, size, focus: bool = False):
        # Wrap to the actual available width so resizing stays correct.
        if size:
            self._reflow(size[0])
        return super().render(size, focus)
