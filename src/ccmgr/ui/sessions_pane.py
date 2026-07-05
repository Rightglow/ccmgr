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
    """Last-used timestamp, written exactly as `claude --resume` does:
    'just now', '5 minutes ago', '4 hours ago', '2 days ago', and a date for
    anything older than a week. Singular/plural picked correctly.
    """
    if epoch <= 0:
        return "—"
    delta = max(0.0, time.time() - epoch)
    if delta < 60:
        return "just now"
    if delta < 3600:
        n = int(delta // 60)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if delta < 86400:
        n = int(delta // 3600)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    if delta < 7 * 86400:
        n = int(delta // 86400)
        return f"{n} day{'s' if n != 1 else ''} ago"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _format_size(nbytes: int) -> str:
    """Human file size: 175907 -> '175.9KB', 1342177 -> '1.3MB'."""
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f}MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f}KB"
    return f"{nbytes}B"


_STATUS_DOTS = {"idle": "🟢", "busy": "🟡", "blocked": "🔴"}
_FOCUS_REMAP = {None: "focus", "live": "focus", "dim": "focus", "live_tag": "focus"}


class _SessionRow(ClickableRow):
    def __init__(self, session: SessionMeta, live_threshold: float,
                 is_running: bool = False, is_favorite: bool = False,
                 on_click: "Callable[[], None] | None" = None) -> None:
        self.session = session
        is_active = is_live(session, live_threshold)

        # Two-line layout:
        #   [⭐] [🟢] Title  [LIVE]
        #   <relative time> · <branch> · <size>
        title_markup: list = []
        if is_favorite:
            title_markup.append("⭐ ")
        title_markup.append(_STATUS_DOTS.get(session.status, "⚪"))
        title_markup.append(" ")
        title_markup.append("● " if is_running else "  ")
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
        row_attr = "live" if is_running else None
        super().__init__(urwid.AttrMap(body, row_attr, focus_map=_FOCUS_REMAP), on_click)


class _NewSessionRow(ClickableRow):
    def __init__(self, on_click: "Callable[[], None] | None" = None) -> None:
        super().__init__(urwid.AttrMap(urwid.Text("+ New session"), "dim", focus_map="focus"),
                         on_click)


class SessionsPane(urwid.WidgetWrap):
    def __init__(self, on_select: Callable[[SessionMeta | None], None], live_threshold: float) -> None:
        self._on_select = on_select
        self._live_threshold = live_threshold
        self._sessions: list[SessionMeta] = []
        self._project: Project | None = None
        self._filter = ""
        self._running_ids: set[str] = set()
        self._favorite_ids: set[str] = set()

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
        rows = [
            _SessionRow(
                s, self._live_threshold,
                is_running=(s.session_id in self._running_ids),
                is_favorite=(s.session_id in self._favorite_ids),
                on_click=lambda s=s: self._on_select(s),
            )
            for s in sessions
        ]
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
        return super().keypress(size, key)
