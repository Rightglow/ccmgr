"""Modal overlay widgets."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import urwid

from ccmgr.models import Project, SessionMeta


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

    def __init__(self, on_confirm: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        body = urwid.Pile([
            urwid.Text("Quit ccmgr?", align="center"),
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


class HelpModal(urwid.WidgetWrap):
    """Read-only popup listing all keybindings. Esc or Enter dismisses."""

    SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
        ("Navigation", [
            ("↑ / ↓ (or j / k)", "Move within the focused pane"),
            ("Tab / Shift-Tab", "Switch between Projects and Sessions panes"),
        ]),
        ("Actions", [
            ("Enter", "Open or resume the selected session"),
            ("Enter on '+ New project'", "Prompt for a path, start fresh claude there"),
            ("Enter on '+ New session'", "Start a fresh claude in the current project"),
            ("/", "Filter the focused pane; Enter or Esc exits filter mode"),
            ("i", "Popup with details of the focused project / session"),
            ("c", "Open the active project in VS Code (`code <path>`)"),
            ("t", "Open a terminal in the active project (new tmux window)"),
            ("?", "This help"),
            ("q", "Quit ccmgr (kills the right tmux pane + auto-launched session)"),
        ]),
        ("tmux pane switching (outer prefix)", [
            ("Ctrl-B then →", "Move focus to claude (right pane)"),
            ("Ctrl-B then ←", "Move focus back to ccmgr (left pane)"),
            ("Ctrl-B then o", "Cycle through panes"),
        ]),
        ("Notes", [
            ("State preservation", "Each session runs in its own detached tmux"),
            ("", "session. Switching keeps every claude alive — no"),
            ("", "responses or tool calls are interrupted."),
            ("", ""),
            ("Inner tmux prefix", "Press Ctrl-B twice (Ctrl-B Ctrl-B) to send"),
            ("", "tmux commands to the inner (claude) session."),
        ]),
    ]

    def __init__(self, on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        rows: list = []
        for section_title, bindings in self.SECTIONS:
            rows.append(urwid.Text(("title", section_title)))
            for key, desc in bindings:
                rows.append(urwid.Columns([
                    ("fixed", 28, urwid.Text(key, align="left")),
                    urwid.Text(desc, align="left"),
                ], dividechars=1))
            rows.append(urwid.Divider())
        rows.append(urwid.Text(("dim", "Esc or Enter to close"), align="left"))
        super().__init__(urwid.LineBox(urwid.Filler(urwid.Pile(rows), valign="top"), title="Help"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
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


class NewProjectModal(urwid.WidgetWrap):
    """Prompts for a directory path; calls `on_submit(path)` or `on_cancel()`."""

    def __init__(self, on_submit: Callable[[Path], None], on_cancel: Callable[[], None]) -> None:
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._edit = urwid.Edit(caption="path: ", edit_text=str(Path.home()) + "/")
        body = urwid.Pile([
            urwid.Text("Create a new project at:"),
            urwid.Divider(),
            self._edit,
            urwid.Divider(),
            urwid.Text("Enter to submit, Esc to cancel.", align="left"),
        ])
        super().__init__(urwid.LineBox(urwid.Filler(body, valign="top"), title="New Project"))

    def keypress(self, size, key):
        if key == "enter":
            raw = self._edit.edit_text.strip()
            if not raw:
                return None
            expanded = Path(raw).expanduser()
            self._on_submit(expanded)
            return None
        if key == "esc":
            self._on_cancel()
            return None
        return super().keypress(size, key)
