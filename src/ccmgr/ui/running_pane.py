"""Sidebar pane: chat sessions currently opened in this ccmgr instance."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import urwid

from ccmgr.ui._widgets import ClickableRow, remember_focus, restore_focus
# Reuse the status-dot glyphs and the focus/selected attribute maps so the
# coloured ● blends into highlighted rows the same way it does in the Sessions
# pane (extra keys like "dim" are harmless here).
from ccmgr.ui.sessions_pane import _STATUS_DOTS, _FOCUS_REMAP, _SELECTED_MAP


@dataclass(frozen=True)
class RunningEntry:
    tmux_name: str  # detached tmux session name (cc-<id>)
    label: str      # display label, e.g. "ger-lang/Refactor X" or "claude-chat/(new)"
    status: str = "idle"  # "idle" | "busy" | "blocked"


class _RunningRow(ClickableRow):
    def __init__(self, entry: RunningEntry,
                 is_selected: bool = False,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None) -> None:
        self.entry = entry
        dot = _STATUS_DOTS.get(entry.status, ("dim", "○"))
        text = urwid.Text([dot, " ", entry.label], wrap="clip")
        # Use the dict map when selected so the coloured dot picks up the
        # selected background (a bare "selected" string would leave the dot's
        # own attribute — and thus its background — untouched).
        row_attr = _SELECTED_MAP if is_selected else "live"
        super().__init__(urwid.AttrMap(text, row_attr, focus_map=_FOCUS_REMAP),
                         on_click, on_double_click, on_right_click,
                         click_key=entry.tmux_name,
                         immediate_click=True)


class RunningSessionsPane(urwid.WidgetWrap):
    """Lists every chat session this ccmgr instance has opened.

    Enter on a row re-attaches the right pane to that detached claude session.
    """

    def __init__(self, on_select: Callable[[RunningEntry], None],
                 on_context: "Callable[[RunningEntry], None] | None" = None) -> None:
        self._on_select = on_select
        self._on_context = on_context
        self._active_tmux_name: str | None = None
        self._selected_tmux_name: str | None = None
        self._walker = urwid.SimpleFocusListWalker(
            [urwid.Text(("dim", "  (no running sessions)"), align="left")]
        )
        self._listbox = urwid.ListBox(self._walker)
        self._linebox = urwid.LineBox(self._listbox, title="Running")
        super().__init__(self._linebox)

    def set_active(self, tmux_name: str | None) -> None:
        """Persistently highlight the session attached in the right pane."""
        if self._active_tmux_name == tmux_name:
            return
        self._active_tmux_name = tmux_name
        self._rerender()

    def set_selected(self, tmux_name: str | None) -> None:
        """Temporarily highlight a context-menu target."""
        if self._selected_tmux_name == tmux_name:
            return
        self._selected_tmux_name = tmux_name
        self._rerender()

    def _rerender(self) -> None:
        entries = [w.entry for w in self._walker
                   if isinstance(w, _RunningRow)]
        if entries:
            self.set_running(entries)

    def set_running(self, entries: list[RunningEntry]) -> None:
        prior = self._remember_focus()
        if not entries:
            self._walker[:] = [urwid.Text(("dim", "  (no running sessions)"), align="left")]
            self._linebox.set_title("Running")
            return
        self._walker[:] = [
            _RunningRow(
                e,
                is_selected=(e.tmux_name
                             == (self._selected_tmux_name or self._active_tmux_name)),
                on_click=lambda e=e: self._on_select(e, steal_focus=False),
                on_double_click=lambda e=e: self._on_select(e),
                on_right_click=(lambda e=e: self._on_context(e))
                               if self._on_context else None,
            )
            for e in entries
        ]
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
        # Consume up/down at pane boundaries — Tab/Shift-Tab is the only way
        # to switch panes, preventing accidental overscroll into sibling panes.
        if self._walker:
            if key == "up" and self._walker.focus == 0:
                return None
            if key == "down":
                cur = self._walker.focus
                last = None
                for i, w in enumerate(self._walker):
                    if isinstance(w, _RunningRow):
                        last = i
                if last is not None and cur == last:
                    return None
        return super().keypress(size, key)
