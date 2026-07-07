"""Bottom sidebar pane: sessions in the currently-selected project."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

import urwid

from ccmgr.models import Project, SessionMeta
from ccmgr.session_index import is_live
from ccmgr.ui._widgets import ClickableRow, remember_focus, restore_focus


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


def _format_size(nbytes: int) -> str:
    """Human file size: 175907 -> '175.9KB', 1342177 -> '1.3MB'."""
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f}MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f}KB"
    return f"{nbytes}B"


_STATUS_DOTS = {"idle": ("status_idle", "●"), "busy": ("status_busy", "●"),
                "blocked": ("status_blocked", "●")}
# When a row is selected (right-click context menu), map the status-dot
# attributes to variants with the selected background so the dot blends in.
_SELECTED_MAP = {None: "selected", "dim": "selected",
                 "status_idle": "status_idle_sel",
                 "status_busy": "status_busy_sel",
                 "status_blocked": "status_blocked_sel"}
# Focus highlight (brown bg). Status dots need their own remap so the coloured
# ● inherits the focus background instead of leaving a black gap; everything
# else (title, star, meta) collapses to the plain "focus" attribute.
_FOCUS_REMAP = {None: "focus", "live": "focus", "dim": "focus", "live_tag": "focus",
                "status_idle": "status_idle_focus",
                "status_busy": "status_busy_focus",
                "status_blocked": "status_blocked_focus"}


class _SessionRow(ClickableRow):
    def __init__(self, session: SessionMeta, live_threshold: float,
                 is_running: bool = False, is_favorite: bool = False,
                 is_selected: bool = False,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None,
                 on_right_click: "Callable[[], None] | None" = None) -> None:
        self.session = session
        is_active = is_live(session, live_threshold)

        # Two-line layout:
        #   [●] [★] Title  [LIVE]
        #   <relative time> · <branch> · <size>
        # Status dot stays in a fixed leftmost column so the status column
        # aligns across rows; the star sits next to the title it marks.
        title_markup: list = []
        title_markup.append(_STATUS_DOTS.get(session.status, ("dim", "○")))
        title_markup.append("  ")
        if is_favorite:
            # Plain text (no colour) so the star simply inherits the row's
            # highlight background instead of leaving an un-highlighted gap.
            title_markup.append("★ ")
        title_markup.append(session.display_title)
        if is_active:
            title_markup.append(("live_tag", " [LIVE]"))
        title_text = urwid.Text(title_markup, wrap="clip")

        # Size is captured on SessionMeta at scan time — no stat() in the
        # render/poll hot path.
        size_str = _format_size(session.size_bytes) if session.size_bytes else "—"
        parts = [_format_when(session.last_mtime)]
        if session.git_branch:
            parts.append(session.git_branch)
        parts.append(size_str)
        meta_text = urwid.Text(("dim", "  " + " · ".join(parts)), wrap="clip")

        body = urwid.Pile([title_text, meta_text])
        if is_selected:
            row_attr: str | dict | None = _SELECTED_MAP
        elif is_running:
            row_attr = "live"
        else:
            row_attr = None
        super().__init__(urwid.AttrMap(body, row_attr, focus_map=_FOCUS_REMAP),
                         on_click, on_double_click, on_right_click)


class _NewSessionRow(ClickableRow):
    def __init__(self, on_click: "Callable[[], None] | None" = None) -> None:
        super().__init__(urwid.AttrMap(urwid.Text("+ New session"), "dim", focus_map="focus"),
                         on_click)


class SessionsPane(urwid.WidgetWrap):
    def __init__(self, on_select: Callable[[SessionMeta | None], None],
                 live_threshold: float,
                 on_preview: "Callable[[SessionMeta], None] | None" = None,
                 on_context: "Callable[[SessionMeta], None] | None" = None) -> None:
        self._on_select = on_select
        self._on_preview = on_preview
        self._on_context = on_context
        self._live_threshold = live_threshold
        self._sessions: list[SessionMeta] = []
        self._project: Project | None = None
        self._filter = ""
        self._running_ids: set[str] = set()
        self._favorite_ids: set[str] = set()
        self._selected_session_id: str | None = None

        self._new_row = _NewSessionRow(on_click=lambda: self._on_select(None))
        self._divider = urwid.Divider("─")
        self._walker = urwid.SimpleFocusListWalker([urwid.Text("(no project selected)", align="center")])
        self._listbox = urwid.ListBox(self._walker)
        self._pile = urwid.Pile([
            ("weight", 1, self._listbox),
        ])
        self._linebox = urwid.LineBox(self._pile, title="Sessions")
        # Start focused on the listbox when it has selectable content.
        if self._walker:
            self._pile.focus_position = 0
        super().__init__(self._linebox)

    def set_sessions(self, project: Project | None, sessions: list[SessionMeta],
                     running_ids: set[str] | None = None,
                     favorite_ids: set[str] | None = None) -> None:
        prior_focus = self._remember_focus()
        self._project = project
        self._sessions = sessions
        # Both id sets are "leave unchanged when None" — callers may refresh one
        # without touching the other.
        if favorite_ids is not None:
            self._favorite_ids = favorite_ids
        if running_ids is not None:
            self._running_ids = running_ids

        if project is None:
            self._walker[:] = [urwid.Text("(no project selected)", align="center")]
            self._linebox.set_title("Sessions")
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
        self._linebox.set_title(f"Sessions ({project.display_name})")

        self._restore_focus(prior_focus)

    def set_selected_session(self, session_id: str | None) -> None:
        if self._selected_session_id == session_id:
            return
        self._selected_session_id = session_id
        self._render(self._visible_sessions())

    def set_filter(self, needle: str) -> None:
        self._filter = needle
        if self._project is not None:
            self._render(self._visible_sessions())

    def _visible_sessions(self) -> list[SessionMeta]:
        """Sessions after applying the current filter, sorted favorites-first
        then most-recent. Shared by set_sessions and set_filter so both views
        order identically."""
        needle = self._filter.lower()
        if needle:
            sessions = [s for s in self._sessions if needle in s.display_title.lower()]
        else:
            sessions = list(self._sessions)
        f_ids = self._favorite_ids
        sessions.sort(key=lambda s: (0 if s.session_id in f_ids else 1, -s.last_mtime))
        return sessions

    def _render(self, sessions: list[SessionMeta]) -> None:
        rows: list = []
        for s in sessions:
            is_running = s.session_id in self._running_ids
            is_fav = s.session_id in self._favorite_ids
            is_sel = s.session_id == self._selected_session_id
            if is_running:
                on_click = lambda s=s: self._on_select(s, steal_focus=False)
                on_dbl = lambda s=s: self._on_select(s)
            else:
                on_click = (lambda s=s: self._on_preview(s)) if self._on_preview else None
                on_dbl = lambda s=s: self._on_select(s)  # double-click opens
            rows.append(_SessionRow(
                s, self._live_threshold,
                is_running=is_running, is_favorite=is_fav,
                is_selected=is_sel,
                on_click=on_click, on_double_click=on_dbl,
                on_right_click=(lambda s=s: self._on_context(s))
                               if self._on_context else None,
            ))
        if not rows:
            rows = [urwid.Text("  (no matches)" if self._filter else "  (no sessions yet)", align="left")]
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
