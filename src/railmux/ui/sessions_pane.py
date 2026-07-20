"""Bottom sidebar pane: sessions in the currently-selected project."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from functools import partial

import urwid

from railmux.fuzzy import fuzzy_match
from railmux.models import AttentionCategory, Project, SessionMeta
from railmux.ui._widgets import (
    ClickableRow,
    ScrollableSidebarPane,
    remember_focus,
    restore_focus,
)


def _format_when(epoch: float) -> str:
    """Abbreviated relative time: 'just now', '5m ago', '4h ago', '2d ago',
    and a date for anything older than a week."""
    if epoch <= 0:
        return "—"
    delta = max(0.0, time.time() - epoch)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


_COUNT_UNITS = ("", "k", "M", "B", "T", "P", "E")


def _format_scientific_count(value: int) -> str:
    """One-decimal scientific notation using integer arithmetic only."""
    digits = str(value)
    exponent = len(digits) - 1
    leading_tenths = int(digits[:2])
    if len(digits) > 2 and digits[2] >= "5":
        leading_tenths += 1
    if leading_tenths == 100:
        return f"1e{exponent + 1}"
    mantissa = (str(leading_tenths // 10) if leading_tenths % 10 == 0
                else f"{leading_tenths // 10}.{leading_tenths % 10}")
    return f"{mantissa}e{exponent}"


def _format_count(value: int) -> str:
    """Compact a non-negative count without losing small values.

    Counts below 1,000 stay exact. Larger values use one decimal below ten
    units and whole units thereafter; rounding across a unit boundary is
    promoted (999,500 -> 1M). Values beyond the named SI units fall back to a
    compact scientific form instead of overflowing a float.
    """
    value = max(0, value)
    if value < 1000:
        return str(value)

    divisor = 1
    unit_index = 0
    while (unit_index < len(_COUNT_UNITS) - 1
           and value >= divisor * 1000):
        divisor *= 1000
        unit_index += 1

    if (unit_index == len(_COUNT_UNITS) - 1
            and value >= divisor * 1000):
        return _format_scientific_count(value)

    if value < divisor * 10:
        tenths = (value * 10 + divisor // 2) // divisor
        number = (str(tenths // 10) if tenths % 10 == 0
                  else f"{tenths // 10}.{tenths % 10}")
    else:
        rounded = (value + divisor // 2) // divisor
        if rounded == 1000:
            if unit_index == len(_COUNT_UNITS) - 1:
                return _format_scientific_count(value)
            divisor *= 1000
            unit_index += 1
            number = "1"
        else:
            number = str(rounded)
    return number + _COUNT_UNITS[unit_index]


def _live_state(session: SessionMeta) -> str:
    """Compact current state for a session owned by a live tmux process."""
    if (session.attention is not None
            and session.attention.category is AttentionCategory.ABORTED):
        return "aborted"
    if session.status in {"idle", "busy", "blocked"}:
        return session.status
    return "running"


_STATUS_DOTS = {"idle": ("status_idle", "●"), "busy": ("status_busy", "●"),
                "blocked": ("status_blocked", "●")}
_ATTENTION_MARK = ("attention", "!")
# When a row is active in the right pane (or targeted by a context menu), map
# status-dot attributes to variants with the selected background.
_SELECTED_MAP = {None: "selected", "dim": "selected",
                 "session_meta": "session_meta_sel",
                 "status_idle": "status_idle_sel",
                 "status_busy": "status_busy_sel",
                 "status_blocked": "status_blocked_sel",
                 "attention": "attention_sel"}
# Focus highlight (deep-grass bg). Status dots need their own remap so the coloured
# ● inherits the focus background instead of leaving a black gap; everything
# else (title, star, meta) collapses to the plain "focus" attribute.
_FOCUS_REMAP = {None: "focus", "live": "focus", "dim": "focus",
                "session_meta": "session_meta_focus",
                "status_idle": "status_idle_focus",
                "status_busy": "status_busy_focus",
                "status_blocked": "status_blocked_focus",
                "attention": "attention_focus"}


class _SessionRow(ClickableRow):
    def __init__(self, session: SessionMeta, is_running: bool = False,
                 is_favorite: bool = False,
                 is_selected: bool = False,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None) -> None:
        self.session = session
        # Two-line layout:
        #   [●] [★] Title
        #   <relative time/live state> · <message count> · <token count>
        # Status dot stays in a fixed leftmost column so the status column
        # aligns across rows; the star sits next to the title it marks.
        title_markup: list = []
        # Lifecycle status is meaningful only while a tmux session is live.
        # Historical rows use one neutral hollow marker rather than preserving
        # a stale idle/busy/blocked state from their final saved event.
        dot = (_STATUS_DOTS.get(session.status, ("dim", "○"))
               if is_running else ("dim", "○"))
        title_markup.append(dot)
        title_markup.append(" ")
        if session.attention is not None:
            title_markup.append(_ATTENTION_MARK)
            title_markup.append(" ")
        else:
            title_markup.append("  ")
        if is_favorite:
            # Plain text (no colour) so the star simply inherits the row's
            # highlight background instead of leaving an un-highlighted gap.
            title_markup.append("★ ")
        title_markup.append(session.display_title)
        title_text = urwid.Text(title_markup, wrap="clip")

        parts = [
            _live_state(session) if is_running
            else _format_when(session.last_mtime)
        ]
        parts.append(f"{_format_count(session.message_count)} msg")
        parts.append(f"{_format_count(session.token_total)} tok")
        meta_text = urwid.Text(
            ("session_meta", "  " + " · ".join(parts)), wrap="ellipsis")

        body = urwid.Pile([title_text, meta_text])
        if is_selected:
            row_attr: str | dict | None = _SELECTED_MAP
        elif is_running:
            row_attr = "live"
        else:
            row_attr = None
        # Immediate preview keeps single-click latency low; a following double
        # click reuses the preview pane when it opens the session.
        super().__init__(urwid.AttrMap(body, row_attr, focus_map=_FOCUS_REMAP),
                         on_click, on_double_click, on_right_click,
                         click_key=session.session_id,
                         immediate_click=True)


class _NewSessionRow(ClickableRow):
    def __init__(self, on_click: "Callable[[], None] | None" = None) -> None:
        super().__init__(urwid.AttrMap(urwid.Text("+ New session"), "dim", focus_map="focus"),
                         on_click)


class SessionsPane(ScrollableSidebarPane, urwid.WidgetWrap):
    def __init__(self, on_select: Callable[[SessionMeta | None], None],
                 on_preview: "Callable[[SessionMeta], None] | None" = None,
                 on_context: "Callable[[SessionMeta], None] | None" = None,
                 on_double_detected: "Callable[[], None] | None" = None,
                 provider_label: str = "Agent",
                 *, boxed: bool = True) -> None:
        self._on_select = on_select
        self._on_preview = on_preview
        self._on_context = on_context
        self._on_double_detected = on_double_detected
        self._provider_label = provider_label
        self._boxed = boxed
        self._section_title = "Sessions"
        self._sessions: list[SessionMeta] = []
        self._project: Project | None = None
        self._filter = ""
        self._running_ids: set[str] = set()
        self._favorite_ids: set[str] = set()
        self._active_session_id: str | None = None
        self._selected_session_id: str | None = None
        self._rendered_data: tuple | None = None

        self._new_row = _NewSessionRow(on_click=lambda: self._on_select(None))
        self._divider = urwid.AttrMap(urwid.Divider("─"), "dim")
        self._walker = urwid.SimpleFocusListWalker([
            urwid.Text(self._no_project_text(), align="center")])
        self._listbox = urwid.ListBox(self._walker)
        self._pile = urwid.Pile([
            ("weight", 1, self._listbox),
        ])
        # Keep pane focus on the LineBox border/title. Without an explicit body
        # attr, the outer AttrMap also turns every ordinary session title green.
        self._body = urwid.AttrMap(self._pile, "body")
        self._linebox = (
            urwid.LineBox(self._body, title=self._section_title)
            if boxed else None
        )
        # Start focused on the listbox when it has selectable content.
        if self._walker:
            self._pile.focus_position = 0
        super().__init__(self._linebox or self._body)

    def _wheel_chrome_rows(self) -> int:
        # A selected project adds the pinned New Session row and divider.
        pinned_rows = 2 if self._project is not None else 0
        return pinned_rows + (2 if self._boxed else 0)

    def _wheel_border_columns(self) -> int:
        return 2 if self._boxed else 0

    @property
    def section_title(self) -> str:
        return self._section_title

    def _set_section_title(self, title: str) -> None:
        self._section_title = title
        if self._linebox is not None:
            self._linebox.set_title(title)

    def set_sessions(self, project: Project | None, sessions: list[SessionMeta],
                     running_ids: set[str] | None = None,
                     favorite_ids: set[str] | None = None) -> None:
        next_running_ids = self._running_ids if running_ids is None else running_ids
        next_favorite_ids = self._favorite_ids if favorite_ids is None else favorite_ids
        rendered_data = (
            project,
            tuple(sessions),
            frozenset(next_running_ids),
            frozenset(next_favorite_ids),
            tuple(_format_when(session.last_mtime) for session in sessions),
            self._provider_label,
        )
        if self._rendered_data == rendered_data:
            return

        prior_focus = self._remember_focus()
        self._project = project
        self._sessions = sessions
        # Both id sets are "leave unchanged when None" — callers may refresh one
        # without touching the other.
        if favorite_ids is not None:
            self._favorite_ids = favorite_ids
        if running_ids is not None:
            self._running_ids = running_ids
        self._rendered_data = rendered_data

        if project is None:
            self._walker[:] = [
                urwid.Text(self._no_project_text(), align="center")]
            self._set_section_title("Sessions")
            self._pile.contents[:] = [
                (self._listbox, self._pile.options("weight", 1)),
            ]
            self._pile.focus_position = 0
            return

        # Show "+ New session" header row when a project is selected.
        self._pile.contents[:] = [
            (self._new_row, self._pile.options("pack")),
            (self._divider, self._pile.options("pack")),
            (self._listbox, self._pile.options("weight", 1)),
        ]

        self._render(self._visible_sessions())
        self._set_section_title(f"Sessions ({project.display_name})")

        self._restore_focus(prior_focus)

    def _no_project_text(self) -> str:
        return (
            f"Select a {self._provider_label} project above\n"
            "or choose + New project"
        )

    def set_provider_label(self, label: str) -> None:
        """Repaint provider-aware onboarding text without losing pane state."""
        if self._provider_label == label:
            return
        self._provider_label = label
        self._rendered_data = None
        if self._project is None:
            self._walker[:] = [
                urwid.Text(self._no_project_text(), align="center")]
        else:
            self._rerender_preserving_focus()

    def set_active_session(self, session_id: str | None) -> None:
        """Persistently highlight the conversation displayed in the right pane."""
        if self._active_session_id == session_id:
            return
        self._active_session_id = session_id
        self._rerender_preserving_focus()

    def set_selected_session(self, session_id: str | None) -> None:
        """Temporarily highlight a context-menu or modal target."""
        if self._selected_session_id == session_id:
            return
        self._selected_session_id = session_id
        self._rerender_preserving_focus()

    def _rerender_preserving_focus(self) -> None:
        if self._project is None:
            return
        prior_focus = self._remember_focus()
        self._render(self._visible_sessions())
        self._restore_focus(prior_focus)

    def set_filter(self, needle: str) -> None:
        if self._filter == needle:
            return
        self._filter = needle
        if self._project is not None:
            self._render(self._visible_sessions())

    @property
    def filter_text(self) -> str:
        return self._filter

    def _visible_sessions(self) -> list[SessionMeta]:
        """Sessions after applying the current filter, sorted favorites-first
        then most-recent. Shared by set_sessions and set_filter so both views
        order identically."""
        needle = self._filter.lower()
        if needle:
            sessions = [s for s in self._sessions if fuzzy_match(needle, s.display_title)]
        else:
            sessions = list(self._sessions)
        f_ids = self._favorite_ids
        sessions.sort(key=lambda s: (0 if s.session_id in f_ids else 1, -s.last_mtime))
        return sessions

    def _on_double_select(self, session: SessionMeta) -> None:
        # Paint right focus before attach; the real select-pane stays delayed.
        if self._on_double_detected is not None:
            self._on_double_detected()
        self._on_select(session, steal_focus=False, from_double=True)

    def _render(self, sessions: list[SessionMeta]) -> None:
        rows: list = []
        for s in sessions:
            is_running = s.session_id in self._running_ids
            is_fav = s.session_id in self._favorite_ids
            selected_id = self._selected_session_id or self._active_session_id
            is_sel = s.session_id == selected_id
            on_dbl = partial(self._on_double_select, s)
            # Preserve the established distinction: running rows switch the
            # active display immediately, while stopped rows preview history.
            # Both callbacks route through App's remembered agent slot.
            if is_running:
                on_click = partial(self._on_select, s, steal_focus=False)
            else:
                on_click = (
                    partial(self._on_preview, s) if self._on_preview else None)
            rows.append(_SessionRow(
                s,
                is_running=is_running, is_favorite=is_fav,
                is_selected=is_sel,
                on_click=on_click, on_double_click=on_dbl,
                on_right_click=(lambda s=s: self._on_context(s))
                               if self._on_context else None,
            ))
        if not rows:
            text = (
                "  (no matches)"
                if self._filter
                else (
                    f"No {self._provider_label} sessions yet\n"
                    "Press n to start one"
                )
            )
            rows = [urwid.Text(text, align="center")]
        self._walker[:] = rows

    @staticmethod
    def _row_key(row: "_SessionRow") -> str:
        return row.session.session_id

    def _remember_focus(self) -> str | None:
        return remember_focus(self._walker, _SessionRow, self._row_key)

    def _restore_focus(self, session_id: str | None) -> None:
        restore_focus(self._walker, _SessionRow, session_id, self._row_key)

    def keypress(self, size, key):
        listbox_pos = 2 if self._project is not None else 0
        if key == "enter":
            # "+ New session" row is only present when a project is loaded.
            if self._project is not None and self._pile.focus_position == 0:
                self._on_select(None)
                return None
            if self._walker:
                focus_w, _ = self._walker.get_focus()
                if isinstance(focus_w, _SessionRow):
                    self._on_select(focus_w.session)
                    return None
        # Consume up/down at pane boundaries — Tab/Shift-Tab is the only way
        # to switch panes, preventing accidental overscroll.
        if key == "up" and self._pile.focus_position == 0:
            return None
        if key == "down" and self._pile.focus_position == listbox_pos and self._walker:
            cur = self._walker.focus
            last = None
            for i, w in enumerate(self._walker):
                if isinstance(w, _SessionRow):
                    last = i
            if last is not None and cur == last:
                return None
        return super().keypress(size, key)
