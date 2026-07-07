"""Modal overlay widgets."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import urwid

from ccmgr.models import Project, SessionMeta
from ccmgr.ui._widgets import ClickableRow


class ProjectInfoModal(urwid.WidgetWrap):
    """Read-only popup with details of the focused project."""

    def __init__(self, project: Project | None, on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        if project is None:
            body_lines = [urwid.Text("No project selected.")]
        else:
            from datetime import datetime, timezone
            ts = (
                datetime.fromtimestamp(project.last_activity_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if project.last_activity_ts > 0 else "—"
            )
            body_lines = [
                urwid.Text(("title", project.display_name)),
                urwid.Divider(),
                urwid.Text(f"path:           {project.real_path}"),
                urwid.Text(f"encoded:        {project.encoded_name}"),
                urwid.Text(f"sessions:       {project.session_count}"),
                urwid.Text(f"last activity:  {ts}"),
            ]
        body_lines.append(urwid.Divider())
        body_lines.append(urwid.Text(("dim", "Esc or Enter to close"), align="left"))
        super().__init__(urwid.LineBox(urwid.Filler(urwid.Pile(body_lines), valign="top"), title="Project info"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        return key


class QuitConfirmModal(urwid.WidgetWrap):
    """Confirm-quit popup. y/Y/Enter confirms; n/N/Esc cancels."""

    def __init__(self, on_confirm: Callable[[], None], on_cancel: Callable[[], None],
                 running_count: int = 0) -> None:
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel

        if running_count > 0:
            session_word = "session" if running_count == 1 else "sessions"
            summary = f"{running_count} Claude {session_word} still running.  Quit will kill them all."
        else:
            summary = "No running sessions."

        body = urwid.Pile([
            urwid.Text("Quit ccmgr?", align="center"),
            urwid.Divider(),
            urwid.Text(("live", summary), align="center"),
            urwid.Divider(),
            urwid.Text("This will kill the right tmux pane (claude) and the", align="center"),
            urwid.Text("auto-launched tmux session (if any).", align="center"),
            urwid.Divider(),
            urwid.Text(("dim", "y / Enter = yes,  n / Esc = no"), align="center"),
        ])
        super().__init__(urwid.LineBox(urwid.Filler(body, valign="middle"), title="Confirm quit"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("y", "Y", "enter"):
            self._on_confirm()
            return None
        if key in ("n", "N", "esc"):
            self._on_cancel()
            return None
        return key


class _Selectable(urwid.WidgetWrap):
    """Tiny wrapper that makes any widget selectable for ListBox navigation."""
    def __init__(self, widget):
        super().__init__(widget)

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


class HelpModal(urwid.WidgetWrap):
    """Read-only popup listing all keybindings. Esc or Enter dismisses."""

    SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
        ("Navigation", [
            ("↑↓", "Move within the focused pane"),
            ("Tab / Shift-Tab", "Switch between Projects, Sessions, Running panes"),
            ("Esc", "Clear filter / close popup / move up one pane level"),
        ]),
        ("Actions", [
            ("Enter", "Open or resume the selected session"),
            ("n", "Start a new Claude session in the current project"),
            ("/", "Filter the focused pane"),
            ("i", "Details of the focused project / session"),
            ("[ / ]", "Resize divider: shrink / expand ccmgr sidebar"),
            ("r", "Rename the focused session"),
            ("s", "Toggle star (pin to top of session list)"),
            ("k", "Kill the running Claude process (keeps session file)"),
            ("d", "Delete the focused session (prompts for confirmation)"),
            ("t", "Open a terminal in the active project"),
            ("q or Ctrl-C", "Quit ccmgr (prompts for confirmation)"),
        ]),
        ("Mouse", [
            ("Left click", "Select / preview (non-running) / attach (running)"),
            ("Double-click", "Open session and move focus to it"),
            ("Right-click", "Context menu for the session"),
            ("Shift + drag", "Select text to copy (bypasses app, uses terminal)"),
        ]),
        ("tmux", [
            ("Ctrl-B → / ←", "Move focus between ccmgr and Claude panes"),
            ("Ctrl-B d", "Detach from ccmgr (keep sessions alive)"),
        ]),
        ("", [
            ("Each session runs in its own detached tmux session.",
             "Switching keeps every Claude alive — no responses"),
            ("or tool calls are interrupted.",
             "Ctrl-B d detaches from ccmgr, everything keeps running."),
        ]),
    ]

    @staticmethod
    def _legend_rows() -> list:
        """Colour legend with palette markup so dots render in status colours."""
        return [
            urwid.Text([
                ("status_idle", "●"), " idle · ",
                ("status_busy", "●"), " busy · ",
                ("status_blocked", "●"), " blocked (waiting for input)",
            ]),
            urwid.Text("★ = starred (pinned to top of session list)"),
        ]

    def __init__(self, on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        rows: list = []
        for section_title, bindings in self.SECTIONS:
            rows.append(_Selectable(urwid.Text(("title", section_title))))
            for key, desc in bindings:
                rows.append(_Selectable(urwid.Columns([
                    ("fixed", 26, urwid.Text(key, align="left")),
                    urwid.Text(desc, align="left"),
                ], dividechars=1)))
            rows.append(_Selectable(urwid.Divider()))
        for legend_row in self._legend_rows():
            rows.append(_Selectable(legend_row))
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        super().__init__(urwid.LineBox(self._listbox, title="Help"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        if key in ("up", "down"):
            return self._listbox.keypress(size, key)
        return key


class SessionInfoModal(urwid.WidgetWrap):
    """Read-only popup showing details of the currently-focused session.

    Dismissed with Esc or Enter.
    """

    def __init__(self, session: SessionMeta | None, running_label: str | None, on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        if session is None:
            body_lines = [urwid.Text("No session selected.")]
        else:
            body_lines = [
                urwid.Text(("title", session.display_title)),
                urwid.Divider(),
                urwid.Text(f"project:   {session.project.real_path}"),
                urwid.Text(f"session id: {session.session_id}"),
                urwid.Text(f"messages:  {session.message_count}"),
                urwid.Text(f"tokens:    {session.token_total}"),
            ]
            if session.last_user_message:
                body_lines.append(urwid.Divider())
                body_lines.append(urwid.Text("last user input:"))
                body_lines.append(urwid.Text(("dim", f"  {session.last_user_message}"), wrap="clip"))
            if running_label:
                body_lines.append(urwid.Divider())
                body_lines.append(urwid.Text(("live", f"▶ running in tmux: {running_label}")))
        body_lines.append(urwid.Divider())
        body_lines.append(urwid.Text(("dim", "Esc or Enter to close"), align="left"))
        super().__init__(urwid.LineBox(urwid.Filler(urwid.Pile(body_lines), valign="top"), title="Session info"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        return key



class RunningInfoModal(urwid.WidgetWrap):
    """Read-only popup with details of a running session entry."""

    def __init__(self, label: str, tmux_name: str, project: "Project | None",
                 session: "SessionMeta | None", is_placeholder: bool,
                 on_close: Callable[[], None]) -> None:
        self._on_close = on_close

        body_lines: list = [
            urwid.Text(("title", label)),
            urwid.Divider(),
            urwid.Text(f"tmux session:  {tmux_name}"),
        ]
        if project is not None:
            body_lines.append(urwid.Text(f"project:       {project.real_path}"))
        else:
            body_lines.append(urwid.Text("project:       (unknown)"))

        if is_placeholder:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(("live", "(initializing — waiting for Claude to start)")))
        elif session is not None:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(f"session id:    {session.session_id}"))
            body_lines.append(urwid.Text(f"messages:      {session.message_count}"))
            body_lines.append(urwid.Text(f"tokens:        {session.token_total}"))
        else:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(("dim", "(session metadata not available)")))

        body_lines.append(urwid.Divider())
        body_lines.append(urwid.Text(("dim", "Esc or Enter to close"), align="left"))
        super().__init__(urwid.LineBox(urwid.Filler(urwid.Pile(body_lines), valign="top"), title="Running session"))


    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        return key


class DeleteConfirmModal(urwid.WidgetWrap):
    """Confirm-delete popup for a session. y/Y/Enter confirms; n/N/Esc cancels."""

    def __init__(self, title: str, detail: str,
                 on_confirm: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        body = urwid.Pile([
            urwid.Text(title, align="center"),
            urwid.Divider(),
            urwid.Text(detail, align="center"),
            urwid.Divider(),
            urwid.Text(("dim", "y / Enter = delete,  n / Esc = cancel"), align="center"),
        ])
        super().__init__(urwid.LineBox(urwid.Filler(body, valign="middle"), title="Confirm delete"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("y", "Y", "enter"):
            self._on_confirm()
            return None
        if key in ("n", "N", "esc"):
            self._on_cancel()
            return None
        return key


class RenameModal(urwid.WidgetWrap):
    """Inline rename popup. Enter submits the new title; Esc cancels."""

    def __init__(self, current_title: str,
                 on_submit: Callable[[str], None],
                 on_cancel: Callable[[], None]) -> None:
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._edit = urwid.Edit(caption="title: ", edit_text=current_title)
        body = urwid.Pile([
            urwid.Text("Rename session:"),
            urwid.Divider(),
            self._edit,
            urwid.Divider(),
            urwid.Text(("dim", "Enter to save, Esc to cancel"), align="left"),
        ])
        super().__init__(urwid.LineBox(urwid.Filler(body, valign="top"), title="Rename"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key == "enter":
            raw = self._edit.edit_text.strip()
            if raw:
                self._on_submit(raw)
            else:
                self._on_cancel()
            return None
        if key == "esc":
            self._on_cancel()
            return None
        return super().keypress(size, key)


class _BrowserRow(ClickableRow):
    """A selectable row for the directory browser (navigated by keyboard)."""
    def __init__(self, markup, attr):
        super().__init__(urwid.AttrMap(urwid.Text(markup, wrap="clip"),
                                       attr, focus_map="focus"))


class PathBrowser(urwid.WidgetWrap):
    """Directory browser: navigate with arrows, Enter to descend/confirm,
    type to filter entries in real time.

    First row is always ``. (use this path)`` — selecting it confirms
    the current directory.  Subdirectories are listed below with a
    ``/`` suffix.
    """

    def __init__(self, start_path: Path,
                 on_select: Callable[[Path], None]) -> None:
        self._path = start_path.expanduser().resolve()
        self._on_select = on_select
        self._items: list[Path] = []            # item 0 = current dir
        self._walker = urwid.SimpleFocusListWalker([])
        self._listbox = urwid.ListBox(self._walker)
        self._path_text = urwid.Text("", wrap="clip")
        self._filter_edit = urwid.Edit("filter: ")
        self._filter = ""

        urwid.connect_signal(self._filter_edit, "change", self._on_filter_change)

        self._header_pile = urwid.Pile([
            ("pack", self._path_text),
            ("pack", self._filter_edit),
            ("pack", urwid.Divider("─")),
            ("weight", 1, self._listbox),
            ("pack", urwid.Divider("─")),
            ("pack", urwid.Text(
                ("dim", "type to filter  ↑↓  ↵ enter  Esc = cancel"),
                align="left",
            )),
        ])
        self._header_pile.focus_position = 3  # the ListBox
        super().__init__(urwid.LineBox(self._header_pile, title="Choose directory"))
        self._refresh()

    # ── filter ────────────────────────────────────────────────────────

    def _on_filter_change(self, _widget, new_text: str) -> None:
        self._filter = new_text
        self._render_list()

    def _visible_entries(self) -> list[Path]:
        """Subset of scanned entries (children) matching the current filter."""
        needle = self._filter.lower()
        children = self._items[2:]  # skip . and ..
        if not needle:
            return children
        return [p for p in children if needle in p.name.lower()]

    # ── refresh / render ──────────────────────────────────────────────

    def _refresh(self) -> None:
        self._path_text.set_text(str(self._path))
        self._filter_edit.set_edit_text("")
        self._filter = ""
        self._header_pile.focus_position = 3  # back to ListBox
        self._items = [self._path, self._path.parent]
        try:
            entries = sorted(self._path.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            entries = []
        self._items.extend(entries)
        self._render_list()

    def _render_list(self) -> None:
        rows: list = [
            _BrowserRow(".  (use this path)", "live_tag"),
            _BrowserRow("..  (parent directory)", "live"),
        ]
        for p in self._visible_entries():
            label = p.name + ("/" if p.is_dir() else "")
            rows.append(_BrowserRow(
                "  " + label,
                "live" if p.is_dir() else "dim",
            ))
        if len(rows) == 2 and self._filter:
            rows.append(urwid.Text(("dim", "  (no matches)")))
        self._walker[:] = rows
        self._walker.set_focus(0)

    def _cur_path(self) -> Path | None:
        if not self._walker:
            return None
        idx = self._walker.focus
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None

    def selectable(self) -> bool:
        return True

    # ── keyboard ──────────────────────────────────────────────────────

    def keypress(self, size, key):
        # Printable characters go to the filter.
        if len(key) == 1 and key.isprintable():
            self._header_pile.focus_position = 1  # focus the filter Edit
            return self._filter_edit.keypress(size, key)

        if key in ("up", "down"):
            self._header_pile.focus_position = 3
            return self._listbox.keypress(size, key)
        if key == "enter":
            self._header_pile.focus_position = 3
            p = self._cur_path()
            if p is not None:
                if p == self._path:
                    self._on_select(self._path)
                    return None
                if p.is_dir():
                    self._path = p
                    self._refresh()
            return None
        if key == "backspace":
            self._header_pile.focus_position = 1
            return self._filter_edit.keypress(size, key)
        if key == "esc":
            if self._filter:
                self._filter_edit.set_edit_text("")
                self._filter = ""
                self._header_pile.focus_position = 3
                self._render_list()
                return None
            # No filter — let PathBrowserModal cancel.
            return super().keypress(size, key)
        return super().keypress(size, key)


class PathBrowserModal(urwid.WidgetWrap):
    """Overlay wrapper: Esc calls PathBrowser to go up one level, or
    cancels if already at the root."""

    def __init__(self, start_path: Path,
                 on_submit: Callable[[Path], None],
                 on_cancel: Callable[[], None]) -> None:
        self._on_cancel = on_cancel
        self._browser = PathBrowser(start_path, on_submit)
        super().__init__(self._browser)

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key == "esc":
            # Let the browser try first (e.g. to clear an active filter).
            if self._browser.keypress(size, key) is None:
                return None
            # Browser didn't consume it — cancel the modal.
            self._on_cancel()
            return None
        return super().keypress(size, key)


# ── context menu ───────────────────────────────────────────────────────

class ContextMenu(urwid.WidgetWrap):
    """A compact popup action list for right-click context menus.

    Each item is a ``(label, callback)`` pair.  Selecting an item fires
    its callback and then closes the menu.  Esc closes without acting.
    """

    def __init__(self, items: list[tuple[str, Callable[[], None]]],
                 on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        rows: list = []
        for label, cb in items:
            row = ClickableRow(
                urwid.AttrMap(urwid.Text(label), None, focus_map="focus"),
                on_click=lambda cb=cb: self._select(cb),
            )
            rows.append(row)
        self._walker = urwid.SimpleFocusListWalker(rows)
        super().__init__(urwid.LineBox(
            urwid.ListBox(self._walker), title=""))

    def _select(self, callback: Callable[[], None]) -> None:
        self._on_close()  # close menu first so the callback can show a new modal
        callback()

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key == "esc":
            self._on_close()
            return None
        if key == "enter":
            focus_w, _ = self._walker.get_focus()
            if isinstance(focus_w, ClickableRow) and focus_w._on_click is not None:
                focus_w._on_click()
            return None
        return super().keypress(size, key)
