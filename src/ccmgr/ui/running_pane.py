"""Sidebar pane: chat sessions currently opened in this ccmgr instance."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import urwid

from ccmgr.ui._widgets import ClickableRow, remember_focus, restore_focus


_FOCUS_REMAP = {None: "focus", "live": "focus"}


@dataclass(frozen=True)
class RunningEntry:
    tmux_name: str  # detached tmux session name (cc-<id>)
    label: str      # display label, e.g. "ger-lang/Refactor X" or "claude-chat/(new)"


class _RunningRow(ClickableRow):
    def __init__(self, entry: RunningEntry,
                 on_click: "Callable[[], None] | None" = None) -> None:
        self.entry = entry
        text = urwid.Text(["● ", entry.label], wrap="clip")
        super().__init__(urwid.AttrMap(text, "live", focus_map=_FOCUS_REMAP), on_click)


class RunningSessionsPane(urwid.WidgetWrap):
    """Lists every chat session this ccmgr instance has opened.

    Enter on a row re-attaches the right pane to that detached claude session.
    """

    def __init__(self, on_select: Callable[[RunningEntry], None]) -> None:
        self._on_select = on_select
        self._walker = urwid.SimpleFocusListWalker(
            [urwid.Text(("dim", "  (no running sessions)"), align="left")]
        )
        self._listbox = urwid.ListBox(self._walker)
        self._linebox = urwid.LineBox(self._listbox, title="Running")
        super().__init__(self._linebox)

    def set_running(self, entries: list[RunningEntry]) -> None:
        prior = self._remember_focus()
        if not entries:
            self._walker[:] = [urwid.Text(("dim", "  (no running sessions)"), align="left")]
            self._linebox.set_title("Running")
            return
        self._walker[:] = [_RunningRow(e, on_click=lambda e=e: self._on_select(e)) for e in entries]
        self._linebox.set_title(f"Running ({len(entries)})")
        self._restore_focus(prior)

    @staticmethod
    def _row_key(row: "_RunningRow") -> str:
        return row.entry.tmux_name

    def _remember_focus(self) -> str | None:
        return remember_focus(self._walker, _RunningRow, self._row_key)

    def _restore_focus(self, tmux_name: str | None) -> None:
        restore_focus(self._walker, _RunningRow, tmux_name, self._row_key)

    def keypress(self, size, key):
        if key == "enter":
            if not self._walker:
                return key
            focus_w, _ = self._walker.get_focus()
            if isinstance(focus_w, _RunningRow):
                self._on_select(focus_w.entry)
                return None
        return super().keypress(size, key)
