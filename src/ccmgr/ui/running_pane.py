"""Sidebar pane: chat sessions currently opened in this ccmgr instance."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import urwid


@dataclass(frozen=True)
class RunningEntry:
    tmux_name: str  # detached tmux session name (cc-<id>)
    label: str      # display label, e.g. "ger-lang/Refactor X" or "claude-chat/(new)"


class _RunningRow(urwid.WidgetWrap):
    def __init__(self, entry: RunningEntry) -> None:
        self.entry = entry
        markup = ["● ", entry.label]
        self._text = urwid.Text(markup, wrap="clip")
        focus_remap = {None: "focus", "live": "focus"}
        super().__init__(urwid.AttrMap(self._text, "live", focus_map=focus_remap))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


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
        self._walker[:] = [_RunningRow(e) for e in entries]
        self._linebox.set_title(f"Running ({len(entries)})")
        self._restore_focus(prior)

    def _remember_focus(self) -> str | None:
        if not self._walker:
            return None
        focus_w, _ = self._walker.get_focus()
        if isinstance(focus_w, _RunningRow):
            return focus_w.entry.tmux_name
        return None

    def _restore_focus(self, tmux_name: str | None) -> None:
        if not self._walker:
            return
        if tmux_name:
            for i, w in enumerate(self._walker):
                if isinstance(w, _RunningRow) and w.entry.tmux_name == tmux_name:
                    self._walker.set_focus(i)
                    return
        for i, w in enumerate(self._walker):
            if isinstance(w, _RunningRow):
                self._walker.set_focus(i)
                return
        self._walker.set_focus(0)

    def keypress(self, size, key):
        if key == "enter":
            if not self._walker:
                return key
            focus_w, _ = self._walker.get_focus()
            if isinstance(focus_w, _RunningRow):
                self._on_select(focus_w.entry)
                return None
        return super().keypress(size, key)
