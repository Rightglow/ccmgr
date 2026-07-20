"""Sidebar pane: chat sessions currently opened in this railmux instance."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import urwid

from railmux.fuzzy import fuzzy_match
from railmux.models import AttentionState
from railmux.ui._widgets import (
    ClickableRow,
    ScrollableSidebarPane,
    remember_focus,
    restore_focus,
)
# Reuse the status-dot glyphs and the focus/selected attribute maps so the
# coloured ● blends into highlighted rows the same way it does in the Sessions
# pane (extra keys like "dim" are harmless here).
from railmux.ui.sessions_pane import (
    _ATTENTION_MARK,
    _FOCUS_REMAP,
    _SELECTED_MAP,
    _STATUS_DOTS,
)


@dataclass(frozen=True)
class RunningEntry:
    tmux_name: str  # detached tmux session name (cc-<id>)
    label: str      # display label, e.g. "ger-lang/Refactor X" or "claude-chat/(new)"
    status: str = "idle"  # "idle" | "busy" | "blocked"
    attention: AttentionState | None = None
    project_label: str = ""
    provider_label: str = "Agent"
    # Immutable launch token carried by callbacks so a stale row cannot act on
    # a different tmux session that later reused the same human-readable name.
    identity_token: str | None = None


class _RunningRow(ClickableRow):
    def __init__(self, entry: RunningEntry,
                 is_selected: bool = False,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None) -> None:
        self.entry = entry
        dot = _STATUS_DOTS.get(entry.status, ("dim", "○"))
        markup: list = [dot, " "]
        if entry.attention is not None:
            markup.extend([_ATTENTION_MARK, " "])
        text = urwid.Text(markup + [entry.label], wrap="clip")
        # Use the dict map when selected so the coloured dot picks up the
        # selected background (a bare "selected" string would leave the dot's
        # own attribute — and thus its background — untouched).
        row_attr = _SELECTED_MAP if is_selected else "live"
        super().__init__(urwid.AttrMap(text, row_attr, focus_map=_FOCUS_REMAP),
                         on_click, on_double_click, on_right_click,
                         click_key=entry.tmux_name,
                         immediate_click=True)


class RunningSessionsPane(ScrollableSidebarPane, urwid.WidgetWrap):
    """Lists every agent session this Railmux instance has opened.

    Single-click switches the remembered display; Enter/double-click also
    transfers keyboard focus to the detached agent.
    """

    def __init__(self, on_select: Callable[[RunningEntry], None],
                 on_context: "Callable[[RunningEntry], None] | None" = None,
                 on_double_detected: "Callable[[], None] | None" = None,
                 provider_label: str = "Agent",
                 *, boxed: bool = True) -> None:
        self._on_select = on_select
        self._on_context = on_context
        self._on_double_detected = on_double_detected
        self._provider_label = provider_label
        self._boxed = boxed
        self._section_title = "Running"
        self._entries: list[RunningEntry] = []
        self._filter = ""
        self._pre_filter_focus: str | None = None
        self._active_tmux_name: str | None = None
        self._selected_tmux_name: str | None = None
        self._rendered_data: tuple | None = None
        self._walker = urwid.SimpleFocusListWalker(
            [urwid.Text(("dim", "  (no running sessions)"), align="left")]
        )
        self._listbox = urwid.ListBox(self._walker)
        # Keep App's pane-focus colour on chrome instead of leaking it into
        # unstyled running-session text.
        self._body = urwid.AttrMap(self._listbox, "body")
        self._linebox = (
            urwid.LineBox(self._body, title=self._section_title)
            if boxed else None
        )
        super().__init__(self._linebox or self._body)

    def _wheel_chrome_rows(self) -> int:
        return 2 if self._boxed else 0

    def _wheel_border_columns(self) -> int:
        return 2 if self._boxed else 0

    @property
    def section_title(self) -> str:
        return self._section_title

    def _set_section_title(self, title: str) -> None:
        self._section_title = title
        if self._linebox is not None:
            self._linebox.set_title(title)

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
        self._rendered_data = None
        self.set_running(self._entries)

    @property
    def filter_text(self) -> str:
        return self._filter

    def set_provider_label(self, label: str) -> None:
        if self._provider_label == label:
            return
        self._provider_label = label
        self._rerender()

    def set_filter(self, needle: str, *, capture_focus: bool = True) -> None:
        # Loading another provider's saved query is not an interactive filter
        # transition. Drop the outgoing provider's anchor even when both query
        # strings happen to be equal, so clearing cannot target a foreign row.
        if not capture_focus:
            self._pre_filter_focus = None
        if self._filter == needle:
            return
        previous = self._filter
        if capture_focus and not previous and needle:
            self._pre_filter_focus = self._remember_focus()
        prior = self._remember_focus()
        self._filter = needle
        restore_key = (
            self._pre_filter_focus if previous and not needle else prior
        )
        self._rendered_data = None
        self.set_running(self._entries)
        if previous and not needle:
            self._restore_focus(restore_key)
            self._pre_filter_focus = None

    def _visible_entries(self) -> list[RunningEntry]:
        """Return an in-memory filtered view without touching provider data."""
        tokens = self._filter.split()
        if not tokens:
            return list(self._entries)
        project_terms: list[str] = []
        text_terms: list[str] = []
        for token in tokens:
            key, separator, value = token.partition(":")
            if separator and key.lower() == "project":
                # A half-typed ``project:`` must not blank the pane while the
                # user is still entering its value.
                if value:
                    project_terms.append(value)
            else:
                # Unknown key:value tokens deliberately remain ordinary text.
                text_terms.append(token)

        visible: list[RunningEntry] = []
        text_needle = " ".join(text_terms)
        for entry in self._entries:
            if any(not fuzzy_match(term, entry.project_label)
                   for term in project_terms):
                continue
            searchable = " ".join((
                entry.label, entry.project_label, entry.provider_label,
                self._provider_label))
            if fuzzy_match(text_needle, searchable):
                visible.append(entry)
        return visible

    def _on_double_select(self, entry: RunningEntry) -> None:
        # Paint right focus before attach; the real select-pane stays delayed.
        if self._on_double_detected is not None:
            self._on_double_detected()
        self._on_select(entry, steal_focus=False, from_double=True)

    def set_running(self, entries: list[RunningEntry]) -> None:
        rendered_data = (
            tuple(entries), self._active_tmux_name, self._selected_tmux_name,
            self._filter, self._provider_label)
        if self._rendered_data == rendered_data:
            return
        self._rendered_data = rendered_data

        prior = self._remember_focus()
        self._entries = list(entries)
        visible = self._visible_entries()
        if not visible:
            if self._filter:
                text = f"  (no matching {self._provider_label} sessions)"
                title = f"Running (0/{len(entries)})"
            else:
                text = f"  (no running {self._provider_label} sessions)"
                title = "Running"
            self._walker[:] = [urwid.Text(("dim", text), align="left")]
            self._set_section_title(title)
            return
        self._walker[:] = [
            _RunningRow(
                e,
                is_selected=(e.tmux_name
                             == (self._selected_tmux_name or self._active_tmux_name)),
                on_click=lambda e=e: self._on_select(
                    e, steal_focus=False),
                on_double_click=lambda e=e: self._on_double_select(e),
                on_right_click=(lambda e=e: self._on_context(e))
                               if self._on_context else None,
            )
            for e in visible
        ]
        count = (
            f"{len(visible)}/{len(entries)}" if self._filter else str(len(entries))
        )
        self._set_section_title(f"Running ({count})")
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
