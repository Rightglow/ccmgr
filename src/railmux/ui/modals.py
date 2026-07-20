"""Modal overlay widgets."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import urwid

from railmux.models import AttentionState, Project, SessionMeta
from railmux.ui._widgets import ClickableRow


def _action_legend(
    actions: list[tuple[str, str]],
    *,
    align: str = "left",
    separator: str = "\n",
    wrap: str = "clip",
) -> urwid.Text:
    """Render modal actions with keys visually distinct from descriptions."""
    markup: list = []
    for index, (keys, description) in enumerate(actions):
        if index:
            markup.append(separator)
        markup.extend([("modal_key", keys), f" = {description}"])
    return urwid.Text(markup, align=align, wrap=wrap)


def _attention_lines(attention: AttentionState | None) -> list:
    """Compact, generic rendering for known and future attention categories."""
    if attention is None:
        return []
    raw_category = getattr(attention.category, "value", attention.category)
    category = str(raw_category).replace("_", " ")
    lines = [
        urwid.Divider(),
        urwid.Text(("attention", f"! attention: {category}"), wrap="clip"),
        urwid.Text(f"  {attention.summary}", wrap="clip"),
    ]
    if attention.retryable is True:
        lines.append(urwid.Text(("dim", "  Retrying is likely safe."), wrap="clip"))
    elif attention.retryable is False:
        lines.append(urwid.Text(("dim", "  Retry is unlikely to help."), wrap="clip"))
    return lines


class _ReadOnlyInfoModal(urwid.WidgetWrap):
    """Scrollable details with an always-visible close legend."""

    _NAV_KEYS = {"up", "down", "page up", "page down", "home", "end"}

    def __init__(self, rows: list, *, title: str,
                 on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        footer = urwid.Pile([
            urwid.Divider(),
            _action_legend([("↵ / Esc", "close")]),
        ])
        self._footer = footer
        frame = urwid.Frame(
            body=self._listbox,
            footer=footer,
            focus_part="body",
        )
        super().__init__(urwid.LineBox(frame, title=title))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        if key in self._NAV_KEYS:
            inner_cols = max(1, size[0] - 2)
            footer_rows = self._footer.rows((inner_cols,))
            inner_rows = max(1, size[1] - 2 - footer_rows)
            self._listbox.keypress((inner_cols, inner_rows), key)
            return None
        return key


class ProjectInfoModal(_ReadOnlyInfoModal):
    """Read-only popup with details of the focused project."""

    def __init__(self, project: Project | None, on_close: Callable[[], None]) -> None:
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
        super().__init__(body_lines, title="Project info", on_close=on_close)


class QuitConfirmModal(urwid.WidgetWrap):
    """Confirm-quit popup.  y=kill-all, s=soft (keep sessions), n=cancel."""

    def __init__(self, on_confirm: Callable[[], None],
                 on_soft_quit: Callable[[], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 running_count: int = 0) -> None:
        self._on_confirm = on_confirm
        self._on_soft_quit = on_soft_quit
        self._on_cancel = on_cancel

        if running_count > 0:
            session_word = "session" if running_count == 1 else "sessions"
            summary = f"{running_count} agent {session_word} still running."
        else:
            summary = "No running sessions."

        actions = [("y / ↵", "quit and kill all sessions")]
        if on_soft_quit is not None:
            actions.append(("s", "soft quit (keep sessions alive)"))
        actions.append(("n / Esc", "cancel"))
        self._title = urwid.Text("Quit railmux?", align="center")
        self._summary = urwid.Text(("live", summary), align="center")
        self._actions = _action_legend(
            actions,
            align="center",
            wrap="space",
        )
        body = urwid.Pile([
            self._title,
            urwid.Divider(),
            self._summary,
            urwid.Divider(),
            self._actions,
        ])
        super().__init__(urwid.LineBox(urwid.Filler(body, valign="middle"), title="Confirm quit"))

    def preferred_height(self, maxcol: int) -> int:
        """Fit every wrapped choice while keeping the confirmation compact."""
        inner = max(1, maxcol - 2)  # LineBox left/right border
        body_rows = (
            self._title.rows((inner,))
            + 1
            + self._summary.rows((inner,))
            + 1
            + self._actions.rows((inner,))
        )
        return max(8, body_rows + 2)  # LineBox top/bottom borders

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("y", "Y", "enter"):
            self._on_confirm()
            return None
        if key in ("s", "S") and self._on_soft_quit is not None:
            self._on_soft_quit()
            return None
        if key in ("n", "N", "esc"):
            if self._on_cancel is not None:
                self._on_cancel()
            return None
        return key


class ExitProgressModal(urwid.WidgetWrap):
    """Non-interactive status shown while synchronous teardown completes."""

    def __init__(self, running_count: int, *, soft: bool) -> None:
        if soft:
            detail = (
                f"Keeping {running_count} agent session"
                f"{'s' if running_count != 1 else ''} running."
            )
        else:
            detail = (
                f"Stopping {running_count} agent session"
                f"{'s' if running_count != 1 else ''}."
            )
        body = urwid.Filler(urwid.Pile([
            urwid.Text(("title", "Exiting…"), align="center"),
            urwid.Divider(),
            urwid.Text(("dim", detail), align="center"),
        ]), valign="middle")
        super().__init__(urwid.LineBox(body, title="Railmux"))


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
            ("↵", "Open or resume the selected session"),
            ("n", "Start a new agent session in the current project"),
            ("/", "Filter the focused pane"),
            ("i", "Details of the focused project / session"),
            ("[ / ]", "Resize divider: shrink / expand railmux sidebar"),
            ("r", "Rename the focused session"),
            ("s", "Toggle star (pin to top of session list)"),
            ("k", "Kill the running agent process (keeps session file)"),
            ("d", "Delete the focused session (prompts for confirmation)"),
            ("m", "Cycle through available agent modes"),
            ("t", "Open a terminal in the active project"),
            ("␣", "Preview stopped / switch running session"),
            ("F8", "Cycle layout: single / side-by-side / stacked"),
            ("F9", "Fullscreen the agent pane (toggle) for clean text copy"),
            ("q or Ctrl-C", "Quit railmux (prompts for confirmation)"),
        ]),
        ("Mouse", [
            ("Left click", "Preview stopped / switch running session"),
            ("Double-click", "Open session and move focus to it"),
            ("Right-click", "Context menu for the session"),
        ]),
        ("Copy text from the agent", [
            ("Drag-select",
             "OSC 52 terminals copy to local clipboard automatically"),
            ("F9",
             "Fullscreen agent → Shift-drag select → Cmd/Ctrl+C → F9"),
        ]),
        ("tmux", [
            ("Ctrl-B Tab", "Toggle between sidebar and the Target pane"),
            ("Ctrl-B arrows", "Move spatially between sidebar / agent panes"),
            ("F8", "Cycle layout even while an agent pane has focus"),
            ("Ctrl-B d", "Detach from railmux (keep sessions alive)"),
        ]),
        ("Workspace indicator (bottom-left)", [
            ("▣", "Single agent pane"),
            ("◧ / ◨", "Side-by-side; filled half is the Target pane"),
            ("⬒ / ⬓", "Stacked; filled half is the Target pane"),
        ]),
        ("", [
            ("Each session runs in its own detached tmux session.",
             "Switching keeps every agent alive — no responses"),
            ("or tool calls are interrupted.",
             "Ctrl-B d detaches from railmux, everything keeps running."),
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
            rows.append(urwid.Text(("title", section_title)))
            for key, desc in bindings:
                key_widget = (
                    urwid.Text(("modal_key", key), align="left")
                    if section_title else urwid.Text(key, align="left")
                )
                rows.append(urwid.Columns([
                    ("fixed", 26, key_widget),
                    urwid.Text(desc, align="left"),
                ], dividechars=1))
            rows.append(urwid.Divider())
        for legend_row in self._legend_rows():
            rows.append(legend_row)
        # Deliberately no _Selectable wrappers: when every row is selectable,
        # ListBox._keypress_down moves focus row-by-row through them all before
        # ever scrolling the viewport.  With bare widgets (none selectable)
        # the ListBox enters the "must scroll" branch immediately — one
        # keypress / wheel tick scrolls one line.
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        super().__init__(urwid.LineBox(self._listbox, title="Help"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("enter", "esc"):
            self._on_close()
            return None
        if key in ("up", "down", "page up", "page down", "home", "end"):
            # Keyboard nav goes directly to the ListBox.  We must bypass the
            # LineBox → WidgetDecoration chain because WidgetDecoration.keypress
            # returns the key unconditionally and never delegates to the inner
            # widget.  Swallow the return so boundary-overflow never leaks to
            # the frame below.
            #
            # Adjust size for the LineBox border (1 char each side) so the
            # ListBox's visibility calculations match what's actually on screen.
            inner = (max(1, size[0] - 2), max(1, size[1] - 2))
            self._listbox.keypress(inner, key)
            return None
        return key

    # No mouse_event override — the standard delegation chain (WidgetWrap →
    # DelegateToWidgetMixin → LineBox → ListBox) correctly adjusts coordinates
    # for the LineBox border before forwarding to the ListBox.  Bypassing it
    # would pass wrong coordinates and break hit-testing / scroll handling.
    # _CloseOnClickOverlay independently handles the "click inside must not
    # dismiss" contract by checking the overlay's screen-space rectangle.


class SessionInfoModal(_ReadOnlyInfoModal):
    """Read-only popup showing details of the currently-focused session.

    Dismissed with Esc or Enter.
    """

    def __init__(self, session: SessionMeta | None, running_label: str | None, on_close: Callable[[], None]) -> None:
        if session is None:
            body_lines = [urwid.Text("No session selected.")]
        else:
            body_lines = [
                urwid.Text(("title", session.display_title), wrap="clip"),
                urwid.Divider(),
                urwid.Text(f"project:   {session.project.real_path}"),
                urwid.Text(f"session id: {session.session_id}"),
                urwid.Text(f"messages:  {session.message_count}"),
                urwid.Text(f"tokens:    {session.token_total}"),
            ]
            body_lines.extend(_attention_lines(session.attention))
            if session.last_user_message:
                body_lines.append(urwid.Divider())
                body_lines.append(urwid.Text("last user input:"))
                body_lines.append(urwid.Text(("dim", f"  {session.last_user_message}"), wrap="clip"))
            if running_label:
                body_lines.append(urwid.Divider())
                body_lines.append(urwid.Text(("live", f"▶ running in tmux: {running_label}")))
        super().__init__(body_lines, title="Session info", on_close=on_close)



class RunningInfoModal(_ReadOnlyInfoModal):
    """Read-only popup with details of a running session entry."""

    def __init__(self, label: str, tmux_name: str, project: "Project | None",
                 session: "SessionMeta | None", is_placeholder: bool,
                 on_close: Callable[[], None]) -> None:
        body_lines: list = [
            urwid.Text(("title", label), wrap="clip"),
            urwid.Divider(),
            urwid.Text(f"tmux session:  {tmux_name}"),
        ]
        if project is not None:
            body_lines.append(urwid.Text(f"project:       {project.real_path}"))
        else:
            body_lines.append(urwid.Text("project:       (unknown)"))

        if is_placeholder:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(("live", "(initializing — waiting for the agent to start)")))
        elif session is not None:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(f"session id:    {session.session_id}"))
            body_lines.append(urwid.Text(f"messages:      {session.message_count}"))
            body_lines.append(urwid.Text(f"tokens:        {session.token_total}"))
            body_lines.extend(_attention_lines(session.attention))
        else:
            body_lines.append(urwid.Divider())
            body_lines.append(urwid.Text(("dim", "(session metadata not available)")))

        super().__init__(
            body_lines, title="Running session", on_close=on_close)


class DeleteConfirmModal(urwid.WidgetWrap):
    """Confirm-delete popup for a session. y/Y/Enter confirms; n/N/Esc cancels.

    The potentially long session name and consequences live in a scrollable
    body. The destructive-action keys stay in a fixed footer so a narrow
    sidebar can never hide how to confirm or cancel.
    """

    _NAV_KEYS = {"up", "down", "page up", "page down", "home", "end"}

    def __init__(self, action: str, session_name: str, detail: str,
                 on_confirm: Callable[[], None], on_cancel: Callable[[], None]) -> None:
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._action = action
        self._session_name = session_name
        self._detail = detail
        rows = [
            _Selectable(urwid.Text(("title", f"{action}?"), align="center")),
            _Selectable(urwid.Divider()),
            _Selectable(urwid.Text(session_name, align="center")),
            _Selectable(urwid.Divider()),
            _Selectable(urwid.Text(detail, align="center")),
        ]
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        footer = urwid.Pile([
            urwid.Divider(),
            _action_legend([
                ("y / ↵", "confirm"),
                ("n / Esc", "cancel"),
            ], align="center"),
        ])
        self._frame = urwid.Frame(
            body=self._listbox,
            footer=footer,
            focus_part="body",
        )
        super().__init__(urwid.LineBox(self._frame, title="Confirm action"))

    def preferred_height(self, maxcol: int) -> int:
        """Return a compact height while leaving long content scrollable."""
        inner = max(1, maxcol - 2)  # LineBox left/right border
        body_rows = (
            urwid.Text(f"{self._action}?").rows((inner,))
            + 1
            + urwid.Text(self._session_name).rows((inner,))
            + 1
            + urwid.Text(self._detail).rows((inner,))
        )
        # LineBox borders (2) + footer divider/action rows (3). Cap the body so
        # pathological titles use the existing ListBox instead of growing into
        # a nearly full-height confirmation window.
        return min(16, max(8, body_rows + 5))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("y", "Y", "enter"):
            self._on_confirm()
            return None
        if key in ("n", "N", "esc"):
            self._on_cancel()
            return None
        if key in self._NAV_KEYS:
            # Delegate through LineBox -> Frame so each decoration adjusts the
            # inner ListBox geometry correctly before navigation.
            super().keypress(size, key)
            return None
        return key


class YoloConfirmModal(urwid.WidgetWrap):
    """First-time Codex prompt: offer to enable auto-run ("yolo") mode.

    Only y/Y enables; Enter/n/N/Esc keeps it off. The dangerous choice never
    has a default-key shortcut.
    """

    def __init__(self, on_confirm: Callable[[], None],
                 on_cancel: Callable[[], None]) -> None:
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        rows = [
            urwid.Text("Enable Codex auto-run (YOLO)?", align="center"),
            urwid.Divider(),
            urwid.Text(
                "Codex will run shell commands WITHOUT approval prompts and "
                "WITHOUT sandboxing — full access to your files. Only enable "
                "this if you trust what you run here.", align="center"),
            urwid.Divider(),
            urwid.Text(
                ("dim", "Change later in ~/.config/railmux/settings.json"),
                align="center"),
        ]
        footer = urwid.Pile([
            urwid.Divider(),
            _action_legend([
                ("y", "enable"),
                ("↵ / n / Esc", "keep off"),
            ], align="center", wrap="space"),
        ])
        self._listbox = urwid.ListBox(urwid.SimpleFocusListWalker(rows))
        self._footer = footer
        frame = urwid.Frame(body=self._listbox, footer=footer, focus_part="body")
        super().__init__(urwid.LineBox(frame, title="Codex auto-run"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        if key in ("y", "Y"):
            self._on_confirm()
            return None
        if key in ("n", "N", "esc", "enter"):
            self._on_cancel()
            return None
        if key in ("up", "down", "page up", "page down", "home", "end"):
            inner_cols = max(1, size[0] - 2)
            footer_rows = self._footer.rows((inner_cols,))
            inner_rows = max(1, size[1] - 2 - footer_rows)
            self._listbox.keypress((inner_cols, inner_rows), key)
            return None
        return key


class RenameModal(urwid.WidgetWrap):
    """Scrollable rename field with an always-visible action legend."""

    def __init__(self, current_title: str,
                 on_submit: Callable[[str], None],
                 on_cancel: Callable[[], None]) -> None:
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._edit = urwid.Edit(
            caption="title: ", edit_text=current_title, wrap="any")
        self._intro = urwid.Text("Rename session:")
        self._walker = urwid.SimpleFocusListWalker([
            self._intro,
            urwid.Divider(),
            self._edit,
        ])
        self._walker.set_focus(2)
        self._listbox = urwid.ListBox(self._walker)
        self._actions = _action_legend([
            ("↵", "save"),
            ("Ctrl+U", "clear"),
            ("Esc", "cancel"),
        ])
        footer = urwid.Pile([
            urwid.Divider(),
            self._actions,
        ])
        body = urwid.Frame(
            body=self._listbox,
            footer=footer,
            focus_part="body",
        )
        super().__init__(urwid.LineBox(body, title="Rename"))

    def preferred_height(self, maxcol: int) -> int:
        """Grow for wrapped titles, then scroll while keeping actions visible."""
        inner = max(1, maxcol - 2)
        body_rows = self._intro.rows((inner,)) + 1 + self._edit.rows((inner,))
        footer_rows = 1 + self._actions.rows((inner,))
        return min(18, max(10, body_rows + footer_rows + 2))

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
        if key == "ctrl u":
            if self._edit.edit_text:
                self._edit.set_edit_text("")
                self._edit.set_edit_pos(0)
            return None
        return super().keypress(size, key)


class _BrowserRow(ClickableRow):
    """A selectable row for the directory browser.  Stores its ``path`` so
    that lookup works correctly even when the list is filtered."""

    def __init__(self, markup, attr, path: Path | None = None,
                 on_click=None):
        self.path = path
        super().__init__(
            urwid.AttrMap(urwid.Text(markup, wrap="clip"),
                          attr, focus_map="focus"),
            on_click=on_click,
            click_key=str(path) if path is not None else None,
        )


class PathBrowser(urwid.WidgetWrap):
    """Directory browser: navigate with arrows, Enter to descend/confirm,
    type to filter entries in real time.

    First row is always ``. (use this path)`` — selecting it confirms
    the current directory.  Subdirectories are listed below with a
    ``/`` suffix.
    """

    def __init__(self, start_path: Path,
                 on_select: Callable[[Path], None],
                 allow_create: bool = False) -> None:
        self._path = start_path.expanduser().resolve()
        self._on_select = on_select
        self._allow_create = allow_create
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
            ("pack", urwid.Text([
                "type to filter/create  " if allow_create else "type to filter  ",
                ("modal_key", "↑↓"), " move  ",
                ("modal_key", "↵"), " open  ",
                ("modal_key", "Esc"), " cancel",
            ], align="left")),
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

    def _create_candidate(self) -> Path | None:
        """Return the non-existent path represented by the filter, if any.

        Relative text creates below the directory currently being browsed;
        absolute paths and ``~`` are accepted so New Project can target a path
        that does not have any existing parent visible in the browser.
        """
        raw = self._filter.strip()
        if not self._allow_create or not raw:
            return None
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self._path / candidate
            candidate = candidate.resolve(strict=False)
            if candidate.exists():
                return None
        except (OSError, RuntimeError):
            return None
        return candidate

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
            _BrowserRow(".  (use this path)", "current_path",
                        path=self._path,
                        on_click=lambda: self._enter_path(self._path)),
            _BrowserRow("..  (parent directory)", "live",
                        path=self._path.parent,
                        on_click=lambda: self._enter_path(self._path.parent)),
        ]
        visible = self._visible_entries()
        for p in visible:
            label = p.name + ("/" if p.is_dir() else "")
            rows.append(_BrowserRow(
                "  " + label,
                "live" if p.is_dir() else "dim",
                path=p,
                on_click=lambda p=p: self._enter_path(p),
            ))
        candidate = self._create_candidate()
        create_index: int | None = None
        if candidate is not None:
            create_index = len(rows)
            rows.append(_BrowserRow(
                f"+  create {candidate}/", "current_path", path=candidate,
                on_click=lambda p=candidate: self._on_select(p),
            ))
        if len(rows) == 2 and self._filter:
            rows.append(urwid.Text(("dim", "  (no matches)")))
        self._walker[:] = rows
        # When nothing existing matches, make the explicit create row the
        # Enter target. With matches present, retain the conservative current-
        # directory focus and require the user to select the create row.
        if create_index is not None and not visible:
            self._walker.set_focus(create_index)
        elif self._filter and len(visible) == 1:
            # A unique existing match (including a file) is safer than leaving
            # Enter on `. (use this path)`, which would silently submit the
            # parent directory when the typed target cannot be created.
            self._walker.set_focus(2)
        else:
            self._walker.set_focus(0)

    def _cur_path(self) -> Path | None:
        """Return the path of the focused row (works correctly when filtered)."""
        if not self._walker:
            return None
        w, _ = self._walker.get_focus()
        if isinstance(w, _BrowserRow):
            return w.path
        return None

    def _enter_path(self, p: Path) -> None:
        """Handle selection of a path: confirm current dir, or descend into subdir."""
        if p == self._path:
            self._on_select(self._path)
        elif p.is_dir():
            self._path = p
            self._refresh()
        elif p == self._create_candidate():
            self._on_select(p)

    def selectable(self) -> bool:
        return True

    # ── keyboard ──────────────────────────────────────────────────────

    def keypress(self, size, key):
        # Printable characters go to the filter.
        if len(key) == 1 and key.isprintable():
            self._header_pile.focus_position = 1  # focus the filter Edit
            return self._filter_edit.keypress(size, key)

        if key in ("up", "down", "page up", "page down", "home", "end"):
            self._header_pile.focus_position = 3
            self._listbox.keypress(size, key)
            return None
        if key == "enter":
            self._header_pile.focus_position = 3
            p = self._cur_path()
            if p is not None:
                self._enter_path(p)
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
                 on_cancel: Callable[[], None],
                 allow_create: bool = False) -> None:
        self._on_cancel = on_cancel
        self._browser = PathBrowser(
            start_path, on_submit, allow_create=allow_create)
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
