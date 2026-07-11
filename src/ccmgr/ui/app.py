"""Top-level urwid app: sidebar + status bar.

ccmgr runs in the left pane of a tmux window. The right pane hosts the
currently-selected claude session. Switching sessions in ccmgr respawns
the right pane with a new claude --resume. Press `i` for a session-info
popup.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

import urwid

from ccmgr import tmux_ctl
from ccmgr.atomic_file import atomic_write_text
from ccmgr.codex_index import CodexIndex
from ccmgr.config import Config
from ccmgr.discovery import list_projects
from ccmgr.favorites import Favorites
from ccmgr.launcher import (
    build_codex_new_command,
    build_codex_resume_command,
    build_new_session_command,
    build_resume_command,
)
from ccmgr.models import Project, SessionMeta
from ccmgr.session_cache import SessionCache
from ccmgr.scroll_manager import ScrollManager
from ccmgr.ui import keymap
from ccmgr.ui.modals import (
    ContextMenu,
    DeleteConfirmModal,
    HelpModal,
    PathBrowserModal,
    ProjectInfoModal,
    QuitConfirmModal,
    RenameModal,
    RunningInfoModal,
    SessionInfoModal,
)
from ccmgr.ui.projects_pane import ProjectsPane
from ccmgr.ui.running_pane import RunningEntry, RunningSessionsPane
from ccmgr.ui.sessions_pane import SessionsPane
from ccmgr.ui.statusbar import ButtonBar, HintBar, StatusBar, TIPS


PALETTE = [
    # Status-bar message levels. Idle tips are dim; info neutral green;
    # warn/error escalate so failures stand out from routine feedback.
    ("status_info", "light green", "default"),
    ("status_warn", "yellow,bold", "default"),
    ("status_error", "light red,bold", "default"),
    ("status_tip", "dark gray", "default"),
    # Row focus: bold black on brown. Pane focus also remaps otherwise-unstyled
    # titles to cyan, making the active pane easier to scan.
    ("focus", "black,bold", "brown"),
    # Persistent "currently-active project" highlight, shown even when focus
    # is elsewhere. Cool tone so it doesn't compete with the warm focus color.
    ("selected", "black,bold", "dark cyan"),
    ("title", "white,bold", ""),
    ("dim", "dark gray", ""),
    ("live", "light green,bold", ""),
    ("current_path", "yellow,bold", ""),
    # Status dots — the ● glyph carries its own palette attribute so it keeps
    # its colour on any row background. Each status has three background
    # variants so it blends into normal / focused (brown) / selected (cyan)
    # rows; the highlight variants use brighter foregrounds to stay readable
    # on those backgrounds. (The star is plain text — no colour — so it just
    # inherits whatever the row's highlight is.)
    ("status_idle", "dark green,bold", ""),
    ("status_idle_focus", "light green,bold", "brown"),
    ("status_idle_sel", "light green,bold", "dark cyan"),
    ("status_busy", "yellow,bold", ""),
    ("status_busy_focus", "yellow,bold", "brown"),
    ("status_busy_sel", "yellow,bold", "dark cyan"),
    ("status_blocked", "dark red,bold", ""),
    ("status_blocked_focus", "light red,bold", "brown"),
    ("status_blocked_sel", "light red,bold", "dark cyan"),
    # Pane border. Dim by default; bright cyan + bold when the pane is focused
    # so it's obvious which pane Tab/Shift-Tab landed on.
    ("pane", "dark gray", ""),
    ("pane_focus", "light cyan,bold", ""),
]


@dataclass
class _Running:
    """One claude session opened by this ccmgr instance.

    Replaces the four parallel dicts that previously tracked running sessions
    (tmux name, label, project, placeholder) and had to be kept in sync by hand.
    Keyed in ``App._running`` by ``key``: the real session_id, or a
    ``__new__-N`` placeholder until the session's JSONL appears on disk.
    """
    key: str
    tmux_name: str
    label: str
    project: Project | None = None
    placeholder_path: Path | None = None  # cwd to resolve against, while a placeholder
    created_at: float = 0.0                # launch time, for placeholder resolution
    status: str = "idle"                   # "idle" | "busy" | "blocked"

    @property
    def is_placeholder(self) -> bool:
        return self.key.startswith("__new__-")


class _CloseOnClickOverlay(urwid.Overlay):
    """An ``urwid.Overlay`` that calls *on_click_outside* when the user
    left-clicks anywhere outside the overlay's area."""

    def __init__(self, top_w: urwid.Widget, bottom_w: urwid.Widget,
                 align, width, valign, height,
                 on_click_outside: Callable[[], None]) -> None:
        self._on_click_outside = on_click_outside
        super().__init__(top_w, bottom_w, align, width, valign, height)

    def mouse_event(self, size, event, button, col, row, focus):
        # Let Overlay dispatch first.  A click inside the overlay area
        # goes to the top widget and returns True if handled.
        handled = super().mouse_event(size, event, button, col, row, focus)
        if not handled and event == "mouse press" and button == 1:
            self._on_click_outside()
            return True
        return handled


class _FocusAwareFrame(urwid.Frame):
    """Frame that can suppress all descendant focus maps when tmux focus leaves."""

    def __init__(self, *args, **kwargs) -> None:
        self._window_active = True
        super().__init__(*args, **kwargs)

    def set_window_active(self, active: bool) -> None:
        if self._window_active == active:
            return
        self._window_active = active
        self._invalidate()

    def render(self, size, focus: bool = False):
        return super().render(size, focus=focus and self._window_active)


@dataclass
class _RightPaneState:
    """What to restore when exiting history-preview mode."""
    kind: str  # "empty" | "claude"
    tmux_name: str | None = None  # for "claude"


class App:
    # Double-backtick toggle for Codex developer mode.
    _DOUBLE_BACKTICK_INTERVAL = 0.5
    _last_backtick_ts: float = 0.0

    # tmux may apply DoubleClick1Pane after the application's double callback.
    # Wait past that multi-click window before selecting the right pane.
    _DOUBLE_CLICK_FOCUS_DELAY = 0.35
    _double_focus_alarm: object | None = None
    _double_focus_visual_pending: bool = False
    # Global project counts/order are less latency-sensitive than the selected
    # session list and are expensive on NFS homes.
    _PROJECT_SCAN_INTERVAL = 3.0
    _project_snapshot: list[Project] | None = None
    _project_snapshot_at: float = 0.0
    # Status-bar state defaults at class scope so methods invoked on a bare
    # ``App.__new__(App)`` (e.g. in unit tests) don't hit AttributeError before
    # ``__init__`` runs. ``__init__`` reassigns these per instance.
    _status_text: str | None = None
    _status_level: str = "info"
    _status_since: float = 0.0
    _tip_index: int = 0
    _tip_since: float = 0.0

    def __init__(self, claude_home: Path, config: Config,
                 auto_launched: bool = False,
                 scroll_coalescing: bool = True) -> None:
        self._claude_home = claude_home
        self._config = config
        self._auto_launched = auto_launched
        self._status = StatusBar()
        # Status-bar state machine. An explicit message (info/warn/error) holds
        # the bar for a level-dependent TTL, then it falls back to cycling idle
        # tips. This is what stops one-shot messages ("→ opened X") from being
        # clobbered by the poll tick before the user can read them.
        self._status_text: str | None = None
        self._status_level: str = "info"
        self._status_since: float = 0.0
        self._tip_index: int = 0
        self._tip_since: float = 0.0
        self._selected_project: Project | None = None
        self._session_cache = SessionCache()
        self._favorites = Favorites()
        # Every claude session this ccmgr instance has opened, keyed by
        # session_id (or a "__new__-N" placeholder until the JSONL appears).
        self._running: dict[str, _Running] = {}
        self._new_session_counter: int = 0
        # The right pane in ccmgr's window; runs `tmux attach -t <claude_session>`.
        self._right_pane_id: str | None = None
        self._loop: urwid.MainLoop | None = None
        self._pending_restore_state: dict | None = None
        self._pending_project: Project | None = None
        self._pending_scroll_session: str | None = None
        self._scroll_alarm_pending: bool = False
        self._double_focus_alarm: object | None = None
        self._double_focus_visual_pending: bool = False
        self._last_screen_size: tuple[int, int] | None = None
        # History-preview mode: when the right pane shows a session transcript
        # (less) instead of a Claude session.  We remember what was there before
        # so we can restore it when the user exits less.
        self._in_history_mode: bool = False
        self._restore_state: _RightPaneState | None = None
        self._right_pane_claude: str | None = None  # tmux_name of claude session in right pane
        self._active_session_id: str | None = None
        self._ccmgr_pane_id: str | None = None  # set in run()
        self._ccmgr_has_focus: bool = True
        self._divider_active: bool | None = None
        self._has_less: bool = shutil.which("less") is not None
        self._less_mouse_flag: str = self._detect_less_mouse()
        self._scroll_manager = ScrollManager(enabled=scroll_coalescing)
        self._soft_quit_flag: bool = False
        # Codex mode (developer toggle, double-tap backtick).
        self._codex_mode: bool = False
        self._codex_index = CodexIndex(
            Path(config.codex_home).expanduser())
        self._codex_project_filter: set[Path] = set()  # cwds with Codex sessions

        projects = list_projects(claude_home)
        self._project_snapshot = projects
        self._project_snapshot_at = time.monotonic()
        self._projects_pane = ProjectsPane(projects, on_select=self._on_project_select,
                                           on_double_click=self._on_project_double_click)
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            on_preview=self._on_session_preview,
            on_context=self._open_session_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
        )
        self._running_pane = RunningSessionsPane(
            on_select=self._on_running_select,
            on_context=self._on_running_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
        )
        # Warn early if dependencies are missing so the user doesn't
        # discover it by getting a cryptic error in the right pane.
        if not tmux_ctl.has_tmux():
            self._set_status(
                "ERROR: tmux not found on PATH — ccmgr cannot run without tmux")
        elif not shutil.which(self._config.claude_binary):
            self._set_status(
                f"WARNING: '{self._config.claude_binary}' not on PATH — sessions cannot launch")

        # The outer AttrMaps highlight both LineBox borders and otherwise-
        # unstyled titles in the focused pane. A one-column gutter keeps those
        # right edges visually separate from tmux's center divider.
        self._sidebar = urwid.Pile([
            ("weight", 2, urwid.AttrMap(self._projects_pane, "pane", focus_map="pane_focus")),
            ("weight", 3, urwid.AttrMap(self._sessions_pane, "pane", focus_map="pane_focus")),
            ("weight", 1, urwid.AttrMap(self._running_pane, "pane", focus_map="pane_focus")),
        ])
        self._sidebar_body = urwid.Padding(self._sidebar, right=1)
        self._hint_bar = HintBar()
        # Start on the focused pane's key set (sidebar defaults to Projects) so
        # the bar is correct before the first refresh tick.
        self._hint_bar.set_context(self._help_context())
        self._button_bar = ButtonBar(
            on_help=self._open_help_modal,
            on_quit=self._open_quit_confirm,
            on_detach=self._on_detach,
        )
        # Footer: context key hints, then the constant button row, then the
        # status/tips line. The status line is index 2 — filter mode swaps it.
        footer = urwid.Pile([
            ("pack", self._hint_bar),
            ("pack", self._button_bar),
            ("pack", self._status),
        ])
        self._frame = _FocusAwareFrame(body=self._sidebar_body, footer=footer)
        # Recover sessions left alive from a previous soft-quit.
        self._discover_orphans()
        # Restore the view from a previous soft-quit, or auto-select the
        # most recent project as usual.
        state = self._load_state()
        initial_project: Project | None = None
        if state:
            proj_name = state.get("project")
            if proj_name:
                initial_project = next(
                    (p for p in projects if p.encoded_name == proj_name), None)
        if initial_project is None and projects:
            initial_project = projects[0]
        if initial_project is not None:
            self._selected_project = initial_project
            self._projects_pane.set_selected(initial_project.encoded_name)
            self._pending_project = initial_project
        # Paint discovered orphans without parsing their JSONLs. Full labels
        # and statuses are refined after MainLoop renders the first frame.
        self._render_running_pane()
        # Re-open the right pane after MainLoop paints the sidebar's first frame.
        self._pending_restore_state = state

    def _set_active_target(self, session_id: str | None,
                           tmux_name: str | None) -> None:
        """Update persistent highlights for whatever the right pane displays."""
        self._active_session_id = session_id
        self._sessions_pane.set_active_session(session_id)
        self._running_pane.set_active(tmux_name)

    def _set_active_tmux_target(self, tmux_name: str) -> None:
        running = self._by_tmux(tmux_name)
        session_id = None
        if running is not None and not running.is_placeholder:
            session_id = running.key
        self._set_active_target(session_id, tmux_name)

    def _set_divider_active(self, active: bool, *, force: bool = False) -> None:
        """Highlight tmux's divider only while a non-ccmgr pane has focus."""
        if not force and self._divider_active == active:
            return
        self._divider_active = active
        style = "fg=cyan" if active else "fg=colour240"
        tmux_ctl.set_window_border_style(style)

    def _set_ccmgr_focus(self, active: bool, *, force_border: bool = False) -> None:
        """Synchronize urwid focus maps and the tmux center divider."""
        self._ccmgr_has_focus = active
        self._frame.set_window_active(active)
        self._set_divider_active(not active, force=force_border)
        if hasattr(self, "_hint_bar"):
            self._hint_bar.set_context(self._help_context())

    def _schedule_right_pane_focus_after_double(self) -> None:
        """Show the right-focus state now, then move tmux focus once settled."""
        self._cancel_pending_double_focus(restore_visual=False)
        self._double_focus_visual_pending = True
        self._set_ccmgr_focus(False)
        self._redraw_focus_state_now()
        if self._loop is None:
            self._apply_right_pane_focus_after_double(None, None)
            return
        self._double_focus_alarm = self._loop.set_alarm_in(
            self._DOUBLE_CLICK_FOCUS_DELAY,
            self._apply_right_pane_focus_after_double,
        )

    def _apply_right_pane_focus_after_double(self, _loop, _user_data) -> None:
        self._double_focus_alarm = None
        pane_id = self._right_pane_id
        if pane_id is None or not tmux_ctl.select_pane(pane_id):
            self._double_focus_visual_pending = False
            self._set_ccmgr_focus(True)
            self._redraw_focus_state_now()
            return
        self._double_focus_visual_pending = False
        self._set_ccmgr_focus(False)
        self._redraw_focus_state_now()

    def _cancel_pending_double_focus(self, *, restore_visual: bool = True) -> None:
        alarm = self._double_focus_alarm
        if alarm is not None and self._loop is not None:
            self._loop.remove_alarm(alarm)
        self._double_focus_alarm = None
        visual_pending = self._double_focus_visual_pending
        self._double_focus_visual_pending = False
        if visual_pending and restore_visual:
            self._set_ccmgr_focus(True)
            self._redraw_focus_state_now()

    def _redraw_focus_state_now(self) -> None:
        """Flush a focus-only transition instead of waiting for the next tick."""
        if self._loop is not None:
            self._loop.draw_screen()

    def _filter_input(self, keys: list, _raw: list[int]) -> list:
        """Consume terminal focus reports before normal key dispatch."""
        filtered = []
        for key in keys:
            if key == "focus in":
                # Ignore the late left-pane report while a double-click transfer
                # is pending; cancellation restores it for newer sidebar input.
                if not self._double_focus_visual_pending:
                    self._set_ccmgr_focus(True)
            elif key == "focus out":
                self._set_ccmgr_focus(False)
            else:
                filtered.append(key)
        return filtered

    def _load_pending_project(self, _loop, _user_data) -> None:
        """Load initial session metadata after the sidebar's first frame."""
        project = self._pending_project
        self._pending_project = None
        if (project is not None
                and self._selected_project is not None
                and project.encoded_name == self._selected_project.encoded_name):
            self._on_project_select(project)

    def _restore_pending_right_pane(self, _loop, _user_data) -> None:
        """Restore persisted state, retaining its file if restoration raises."""
        state = self._pending_restore_state
        self._pending_restore_state = None
        if state is None:
            return
        self._restore_right_pane(state)
        try:
            self._state_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _schedule_scroll_acceleration(self, claude_tmux_name: str) -> None:
        """Configure scrolling after the pane switch has had a chance to draw."""
        if self._loop is None:
            self._configure_scroll_acceleration(claude_tmux_name)
            return
        self._pending_scroll_session = claude_tmux_name
        if self._scroll_alarm_pending:
            return
        self._scroll_alarm_pending = True
        self._loop.set_alarm_in(0.05, self._apply_pending_scroll_acceleration)

    def _apply_pending_scroll_acceleration(self, _loop, _user_data) -> None:
        self._scroll_alarm_pending = False
        claude_tmux_name = self._pending_scroll_session
        self._pending_scroll_session = None
        if (claude_tmux_name is not None
                and self._right_pane_claude == claude_tmux_name):
            self._configure_scroll_acceleration(claude_tmux_name)

    # --- project / session selection callbacks ---

    def _on_project_select(self, project: Project | None) -> None:
        """Single-click / initial auto-select: show sessions, keep focus here."""
        self._cancel_pending_double_focus()
        self._pending_project = None
        if project is None:
            if self._codex_mode:
                self._set_status("New project only in Claude mode (double-tap `)")
                return
            self._open_new_project_modal()
            return
        self._selected_project = project
        self._projects_pane.set_selected(project.encoded_name)
        if self._codex_mode:
            sessions = self._codex_index.sessions_for_cwd(project.real_path)
        else:
            sessions = self._session_cache.list_sessions(project)
        self._sessions_pane.set_sessions(project, sessions, running_ids=set(self._running),
                favorite_ids=self._favorites.get_ids())
        self._set_status(f"Project: {project.real_path}  ({len(sessions)} sessions)")

    def _on_project_double_click(self, project: Project | None) -> None:
        """Double-click / Enter on a project: show sessions AND move focus to them."""
        if project is None:
            self._open_new_project_modal()
            return
        self._on_project_select(project)
        if self._loop is not None:
            self._sidebar.focus_position = 1

    def _on_session_select(self, session: SessionMeta | None,
                            steal_focus: bool = True,
                            from_double: bool = False) -> None:
        if not from_double:
            self._cancel_pending_double_focus()
        # Opening a real session (or creating a new one) — clear any
        # history-preview state so the launch takes over the right pane.
        self._in_history_mode = False
        self._restore_state = None
        if session is None:
            self._launch_new_session()
            return
        self._launch_resume(session, steal_focus=steal_focus)

    def _on_running_select(self, entry: RunningEntry,
                            steal_focus: bool = True,
                            from_double: bool = False) -> None:
        if not from_double:
            self._cancel_pending_double_focus()
        # Re-attach the right pane to this already-running claude session AND
        # sync the Projects/Sessions panes to that session's project, so the
        # sidebar reflects what's actually showing on the right.
        self._in_history_mode = False
        self._restore_state = None
        ok = self._attach_in_right_pane(entry.tmux_name, steal_focus=steal_focus)
        if not ok:
            self._set_status("failed to re-attach")
            return
        r = self._by_tmux(entry.tmux_name)
        project = r.project if r else None
        if project is not None and (
            self._selected_project is None
            or self._selected_project.encoded_name != project.encoded_name
        ):
            self._selected_project = project
            self._projects_pane.set_selected(project.encoded_name)
            sessions = self._session_cache.list_sessions(project)
            self._sessions_pane.set_sessions(
                project,
                sessions,
                running_ids=set(self._running),
                favorite_ids=self._favorites.get_ids(),
            )
        self._set_status(f"→ {entry.label}")

    # --- history preview (right pane shows transcript via less, not a Claude session) ---

    def _on_session_preview(self, session: SessionMeta) -> None:
        """Show session history in the right pane without launching Claude.

        Stopped-session clicks preview immediately. On a double-click the first
        press may briefly preview before the second press opens the session;
        both operations reuse the same right pane.
        """
        self._cancel_pending_double_focus()
        if not self._has_less:
            self._set_status("'less' not installed — cannot preview history")
            return
        if not self._in_history_mode:
            self._save_restore_state()
        if self._show_transcript(session.jsonl_path):
            self._in_history_mode = True
            self._set_active_target(session.session_id, None)

    def _save_restore_state(self) -> None:
        """Remember what's in the right pane before taking it over for history."""
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            if self._right_pane_claude and tmux_ctl.session_exists(self._right_pane_claude):
                self._restore_state = _RightPaneState("claude", tmux_name=self._right_pane_claude)
                return
        self._restore_state = _RightPaneState("empty")

    @staticmethod
    def _detect_less_mouse() -> str:
        """Return ``"--mouse --wheel-lines=3"`` if less supports it, else ``""``."""
        import subprocess as _sp
        try:
            out = _sp.check_output(
                ["less", "--version"], stderr=_sp.STDOUT, text=True, timeout=3)
            # "less 668 (GNU regular expressions)" → major version
            ver = int(out.strip().split()[1].split(".")[0])
            if ver >= 590:
                return "--mouse --wheel-lines=3"
        except Exception:
            pass
        return ""

    def _show_transcript(self, jsonl_path: Path) -> bool:
        """Create or respawn the right pane with a ``less`` transcript viewer.

        Mouse-wheel scrolling works after focusing the right pane (double-click
        or Ctrl-B →) when less ≥ 590 is installed.

        Returns True on success.
        """
        import shlex
        import sys as _sys
        mouse = self._less_mouse_flag
        # Tail the last 2000 lines so large sessions appear instantly.
        path = shlex.quote(str(jsonl_path))
        cmd = (f"tail -n 2000 {path} | "
               f"{_sys.executable} -m ccmgr.transcript - | "
               f"less -R +G {mouse}")
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            if not tmux_ctl.respawn_pane(self._right_pane_id, cmd):
                self._set_status("failed to respawn right pane for transcript")
                return False
        else:
            new_id = tmux_ctl.split_window_h(cmd, size_percent=70, detached=True)
            if not new_id:
                self._set_status("failed to create right pane for transcript")
                return False
            self._right_pane_id = new_id
            self._set_ccmgr_focus(self._ccmgr_has_focus, force_border=True)
        # Right pane is now showing a transcript, not a Claude session.
        self._right_pane_claude = None
        self._install_fullscreen_binding()
        return True

    def _restore_from_history_mode(self) -> None:
        """Restore whatever was in the right pane before we entered history mode."""
        restore = self._restore_state
        self._restore_state = None
        if restore is None or restore.kind == "empty":
            return
        if restore.kind == "claude" and restore.tmux_name:
            if tmux_ctl.session_exists(restore.tmux_name):
                self._attach_in_right_pane(restore.tmux_name)
                # Sync the sidebar to the restored session's project so the
                # user doesn't see a stale project after less exits.
                r = self._by_tmux(restore.tmux_name)
                if r is not None and r.project is not None:
                    proj = r.project
                    if (self._selected_project is None
                            or self._selected_project.encoded_name != proj.encoded_name):
                        self._selected_project = proj
                        self._projects_pane.set_selected(proj.encoded_name)
                        sess = self._session_cache.list_sessions(proj)
                        self._sessions_pane.set_sessions(
                            proj, sess,
                            running_ids=set(self._running),
                            favorite_ids=self._favorites.get_ids(),
                        )

    # --- tmux integration (detached session per claude + attach in right pane) ---

    @staticmethod
    def _safe_name(s: str, n: int = 12) -> str:
        out = "".join(c if c.isalnum() else "-" for c in s)
        return (out.strip("-") or "x")[:n]

    def _claude_session_name(self, key: str) -> str:
        """Stable tmux session name for a given claude session key."""
        return f"cc-{self._safe_name(key, 16)}"

    def _session_name(self, key: str) -> str:
        """Stable tmux session name, using the right prefix for the active mode.

        Claude sessions: cc-<id>; Codex sessions: cx-<id>.
        In Codex mode the key comes from a Codex session; otherwise from Claude.
        """
        prefix = "cx-" if self._codex_mode else "cc-"
        return f"{prefix}{self._safe_name(key, 16)}"

    def _ensure_detached_claude(self, name: str, shell_cmd: str) -> bool:
        """Create the detached tmux session running claude, if it doesn't already exist."""
        if tmux_ctl.session_exists(name):
            return True
        return tmux_ctl.new_detached_session(name, shell_cmd)

    def _configure_scroll_acceleration(self, claude_tmux_name: str) -> None:
        """Configure coalescing for the inner pane of the nested tmux client."""
        self._scroll_manager.configure(claude_tmux_name)

    def _teardown_scroll_acceleration(self) -> None:
        self._scroll_manager.close()

    def _attach_in_right_pane(self, claude_tmux_name: str, *,
                               steal_focus: bool = True) -> bool:
        """Make the right pane display the named claude tmux session.

        Either creates the right-pane split (first time) or respawns the existing
        right pane to attach to the new claude session. Either way the previous
        claude tmux session stays alive, detached.

        When *steal_focus* is False the right pane content is updated but tmux
        focus stays on the ccmgr pane so the user can keep browsing the sidebar.

        TMUX= prefix clears the env var so the nested ``tmux attach`` works; tmux
        otherwise refuses to attach from within another tmux session.
        """
        import shlex
        # Fast path: the right pane is already showing this session.  Skip the
        # expensive respawn and just optionally move focus.  This also prevents
        # focus flicker on double-click (the first click's respawn kills and
        # restarts tmux attach, which briefly shifts focus).
        if (self._right_pane_id is not None
                and self._right_pane_claude == claude_tmux_name
                and tmux_ctl.pane_alive(self._right_pane_id)):
            if steal_focus:
                tmux_ctl.select_pane(self._right_pane_id)
                self._set_ccmgr_focus(False)
            self._set_active_tmux_target(claude_tmux_name)
            # Re-assert the F9 fullscreen binding: it's server-global and may
            # have been overwritten by another pane's attach since we last set
            # it, even though this pane's id is unchanged.
            self._install_fullscreen_binding()
            return True

        attach_cmd = f"TMUX= exec tmux attach-session -t {shlex.quote(claude_tmux_name)}"
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            ok = tmux_ctl.respawn_pane(self._right_pane_id, attach_cmd)
        else:
            # ccmgr (left) at 30%, claude (right, the new pane) at 70%.
            new_id = tmux_ctl.split_window_h(
                attach_cmd, size_percent=70, detached=not steal_focus)
            if not new_id:
                return False
            self._right_pane_id = new_id
            ok = True
        if ok and self._right_pane_id and steal_focus:
            tmux_ctl.select_pane(self._right_pane_id)
        if ok:
            self._right_pane_claude = claude_tmux_name
            self._set_active_tmux_target(claude_tmux_name)
            self._set_ccmgr_focus(
                not steal_focus and not self._double_focus_visual_pending,
                force_border=True,
            )
            self._schedule_scroll_acceleration(claude_tmux_name)
            self._install_fullscreen_binding()
        return ok

    def _install_fullscreen_binding(self) -> None:
        """(Re)bind F9 to fullscreen-toggle the *agent* (right) pane.

        Unlike tmux's built-in ``Ctrl-B z`` — which zooms whichever pane is
        active and can therefore fullscreen the ccmgr sidebar by mistake — this
        targets the right pane's current id explicitly, so F9 always zooms the
        agent pane regardless of focus. Rebound whenever the right pane is
        (re)created because its id changes. Copy workflow: F9 → Shift-drag to
        select → Cmd/Ctrl+C → F9 to exit.
        """
        if not self._right_pane_id:
            return
        import subprocess as _sp
        _sp.run(
            ["tmux", "bind-key", "-n", "F9", "resize-pane", "-Z", "-t", self._right_pane_id],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )

    def _by_tmux(self, tmux_name: str) -> "_Running | None":
        """Find the running session backed by a given tmux session name."""
        for r in self._running.values():
            if r.tmux_name == tmux_name:
                return r
        return None

    def _launch(self, key: str, cmd: list[str], cwd: Path, label: str,
                project: Project | None, placeholder_path: Path | None = None,
                *, steal_focus: bool = True,
                env: dict[str, str] | None = None,
                login_shell: bool = False) -> bool:
        """Create (or reuse) the detached claude tmux session for `key`,
        register it, and attach it in the right pane. Returns success.

        Shared by resume / new-session / new-project so the tracking bookkeeping
        lives in exactly one place.
        """
        existing = self._running.get(key)
        tmux_name = existing.tmux_name if existing else self._session_name(key)
        if not self._ensure_detached_claude(tmux_name, self._shellify(cmd, cwd=cwd, env=env, login_shell=login_shell)):
            self._set_status("failed to create detached agent session")
            return False
        self._running[key] = _Running(
            key=key, tmux_name=tmux_name, label=label, project=project,
            placeholder_path=placeholder_path,
            created_at=time.time() if placeholder_path is not None else 0.0,
        )
        if not self._attach_in_right_pane(tmux_name, steal_focus=steal_focus):
            self._set_status("failed to attach to agent session")
            return False
        return True

    def _launch_resume(self, session_meta: SessionMeta,
                        *, steal_focus: bool = True) -> None:
        cwd = session_meta.project.real_path
        env: dict[str, str] | None = None
        if session_meta.session_type == "codex":
            cmd = build_codex_resume_command(
                codex_binary=self._config.codex_binary,
                session_id=session_meta.session_id,
                cwd=cwd,
            )
            env = self._codex_env()
        else:
            cmd = build_resume_command(
                claude_binary=self._config.claude_binary,
                session_id=session_meta.session_id,
                cwd=cwd,
            )
        label = f"{session_meta.project.display_name}/{session_meta.display_title}"
        if self._launch(session_meta.session_id, cmd, cwd,
                        label, session_meta.project, steal_focus=steal_focus,
                        env=env, login_shell=session_meta.session_type == "codex"):
            self._set_status(
                f"→ {session_meta.display_title}  ({len(self._running)} session(s) running)")

    def _launch_new_session(self) -> None:
        if self._selected_project is None:
            self._set_status("Pick a project first.")
            return
        proj = self._selected_project
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        env: dict[str, str] | None = None
        if self._codex_mode:
            cmd = build_codex_new_command(
                codex_binary=self._config.codex_binary,
                cwd=proj.real_path,
            )
            env = self._codex_env()
        else:
            cmd = build_new_session_command(
                claude_binary=self._config.claude_binary,
                cwd=proj.real_path,
            )
        if self._launch(placeholder, cmd, proj.real_path, f"{proj.display_name}/(new)",
                        proj, placeholder_path=proj.real_path, env=env,
                        login_shell=self._codex_mode):
            self._set_status(f"→ new session in {proj.display_name}")

    def _on_new_project_submit(self, path: Path) -> None:
        self._close_modal()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._set_status(str(e))
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        cmd = [self._config.claude_binary]
        if self._launch(placeholder, cmd, path, f"{path.name}/(new)",
                        None, placeholder_path=path):
            self._set_status(f"→ new project: {path}")

    @staticmethod
    def _shellify(argv: list[str], cwd: Path,
                   env: dict[str, str] | None = None,
                   login_shell: bool = False) -> str:
        import shlex
        quoted = " ".join(shlex.quote(a) for a in argv)
        # Codex needs env vars (e.g. DEEPSEEK_API_KEY) that are typically
        # set in ~/.bashrc.  ``bash -l`` alone doesn't source .bashrc
        # (it's only read for *interactive* shells); ``-i`` forces that.
        if login_shell:
            exports = ""
            if env:
                for k, v in env.items():
                    exports += f"export {shlex.quote(k)}={shlex.quote(v)} && "
            return (f"cd {shlex.quote(str(cwd))} && "
                    f"exec $SHELL -li -c {shlex.quote(exports + 'exec ' + quoted)}")
        exports = ""
        if env:
            for k, v in env.items():
                exports += f"export {shlex.quote(k)}={shlex.quote(v)} && "
        return f"{exports}cd {shlex.quote(str(cwd))} && exec {quoted}"

    # --- modals ---

    def _right_pane_open(self) -> bool:
        """True when the tmux right pane exists (ccmgr sidebar is ~30% width)."""
        return self._right_pane_id is not None and tmux_ctl.pane_alive(self._right_pane_id)

    def _open_new_project_modal(self) -> None:
        modal = PathBrowserModal(
            start_path=Path.home(),
            on_submit=self._on_new_project_submit,
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=54, height=60)

    def _open_info_modal(self) -> None:
        """Show info for whichever pane has focus: Project, Sessions, or Running."""
        if self._sidebar.focus_position == 0:
            proj = self._projects_pane.focused_project()
            modal = ProjectInfoModal(project=proj, on_close=self._close_modal)
            self._show_overlay(modal, width=60, height=40)
            return
        if self._sidebar.focus_position == 2:
            # Running pane — show info from the focused running entry.
            from ccmgr.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if running_walker:
                focus_w, _ = running_walker.get_focus()
                if isinstance(focus_w, _RunningRow):
                    entry = focus_w.entry
                    r = self._by_tmux(entry.tmux_name)
                    project = r.project if r else None
                    label = r.label if r else entry.tmux_name
                    is_placeholder = r.is_placeholder if r else False
                    # Session metadata only exists once the placeholder resolved.
                    sid = r.key if (r and not r.is_placeholder) else None
                    session = self._find_session_meta(sid, project) if sid else None
                    modal = RunningInfoModal(
                        label=label,
                        tmux_name=entry.tmux_name,
                        project=project,
                        session=session,
                        is_placeholder=is_placeholder,
                        on_close=self._close_modal,
                    )
                    self._show_overlay(modal, width=60, height=35)
            return
        session = self._currently_focused_session_meta()
        if session is not None:
            self._sessions_pane.set_selected_session(session.session_id)
        running_label = None
        if session is not None:
            r = self._running.get(session.session_id)
            if r and tmux_ctl.session_exists(r.tmux_name):
                running_label = f"detached as '{r.tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label, on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40,
                           click_outside_to_close=True)

    def _open_help_modal(self) -> None:
        # Zoom the left (ccmgr) pane fullscreen so the help modal has the
        # entire terminal.  Tmux resize-pane -Z toggles — the second call in
        # _close_help_modal restores the original split layout.  This is
        # much cleaner than shrinking the right pane: it doesn't force a
        # reflow in the agent pane, so no history corruption.
        if self._ccmgr_pane_id:
            import subprocess as _sp
            _sp.run(
                ["tmux", "resize-pane", "-Z", "-t", self._ccmgr_pane_id],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        modal = HelpModal(on_close=self._close_help_modal)
        self._show_overlay(modal, width=60, height=80,
                           click_outside_to_close=True,
                           on_click_outside=self._close_help_modal,
                           fixed_width=True, fixed_height=True)

    def _close_help_modal(self) -> None:
        self._close_modal()
        # Un-zoom — restore the previous tmux layout (any splits come back).
        if self._ccmgr_pane_id:
            import subprocess as _sp
            _sp.run(
                ["tmux", "resize-pane", "-Z", "-t", self._ccmgr_pane_id],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

    def _open_quit_confirm(self) -> None:
        self._save_state()
        modal = QuitConfirmModal(
            on_confirm=self._confirm_quit,
            on_soft_quit=self._soft_quit,
            on_cancel=self._close_modal,
            running_count=len(self._running),
        )
        self._show_overlay(modal, width=50, height=40)

    # --- project shortcut: terminal ---

    def _active_project(self) -> Project | None:
        """Project to act on for the terminal shortcut.

        Prefer the focused project in the Projects pane; fall back to the
        currently-selected (loaded-into-Sessions) project.
        """
        if self._sidebar.focus_position == 0:
            focused = self._projects_pane.focused_project()
            if focused is not None:
                return focused
        return self._selected_project

    def _open_terminal_for_active_project(self) -> None:
        import os
        import shlex
        import subprocess as _sp
        proj = self._active_project()
        if proj is None:
            self._set_status("no project focused/selected")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(proj.real_path))} && exec {shlex.quote(shell)}"
        # Split visibly in the same window: if a right pane (claude) exists,
        # put the terminal below it; otherwise split off the current pane.
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        # Auto-close the pane when the shell exits (default, but be explicit).
        _sp.run(
            ["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        tmux_ctl.select_pane(new_pane)
        self._set_ccmgr_focus(False)
        self._set_status(f"terminal: {proj.display_name}  (Ctrl-B then arrow = move panes)")

    def _on_detach(self) -> None:
        """Detach from the ccmgr tmux session (keep all Claude sessions alive)."""
        import subprocess as _sp
        _sp.run(["tmux", "detach-client"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

    def _confirm_quit(self) -> None:
        """Hard quit: close modal, tear down everything in ``finally``."""
        self._close_modal()
        raise urwid.ExitMainLoop()

    def _soft_quit(self) -> None:
        """Soft quit: set flag so ``_teardown_tmux`` skips session kill."""
        self._soft_quit_flag = True
        self._close_modal()
        raise urwid.ExitMainLoop()

    # --- state file (for restart-after-soft-quit) --------------------------

    @staticmethod
    def _state_path() -> Path:
        import os as _os
        run_dir = _os.environ.get("XDG_RUNTIME_DIR", f"/tmp/ccmgr-{_os.getuid()}")
        return Path(run_dir) / "ccmgr-state.json"

    def _save_state(self) -> None:
        """Persist enough state to restore the current view after a restart."""
        data: dict = {}
        if self._selected_project is not None:
            data["project"] = self._selected_project.encoded_name
        # Focused session in the sidebar.
        session = self._currently_focused_session_meta()
        if session is not None:
            data["session"] = session.session_id
        # What's in the right pane — so we can re-open the same thing.
        if self._in_history_mode:
            data["right_kind"] = "preview"
            if self._active_session_id is not None:
                data["right_session"] = self._active_session_id
        elif self._right_pane_claude is not None:
            data["right_kind"] = "claude"
            data["right_tmux"] = self._right_pane_claude
        else:
            data["right_kind"] = "empty"
        import json
        path = self._state_path()
        try:
            atomic_write_text(
                path, json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def _load_state(self) -> dict | None:
        """Return persisted state dict, or None if unavailable / stale."""
        import json
        path = self._state_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _restore_right_pane(self, state: dict) -> None:
        """Re-open the right pane to its state at soft-quit time."""
        kind = state.get("right_kind")
        if kind == "claude":
            tmux_name = state.get("right_tmux")
            if tmux_name and tmux_ctl.session_exists(tmux_name):
                self._attach_in_right_pane(tmux_name, steal_focus=False)
        elif kind == "preview":
            sess_id = state.get("right_session")
            if sess_id and self._selected_project is not None:
                meta = self._session_cache.get(self._selected_project, sess_id)
                if meta is not None and self._show_transcript(meta.jsonl_path):
                    self._in_history_mode = True
                    self._set_active_target(meta.session_id, None)

    def _discover_orphans(self) -> None:
        """Find detached ``cc-*`` and ``cx-*`` tmux sessions and rebuild ``_running``.

        Called at startup so a soft-quit → restart cycle picks up every
        session that was left alive.

        tmux session names are truncated (``_safe_name``, 16 chars), so
        we must resolve each truncated name back to the full session_id
        by scanning the project's sessions — otherwise the truncated key
        will not match ``SessionMeta.session_id`` elsewhere.
        """
        import subprocess as _sp
        try:
            out = _sp.check_output(
                ["tmux", "list-sessions", "-F",
                 "#{session_name}\t#{pane_current_path}"],
                stderr=_sp.DEVNULL, text=True,
            )
        except (OSError, _sp.CalledProcessError):
            return
        projects = {p.real_path: p for p in list_projects(self._claude_home)}
        found = 0
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, cwd_str = parts
            is_claude = name.startswith("cc-")
            is_codex = name.startswith("cx-")
            if not (is_claude or is_codex):
                continue
            prefix_len = 3  # "cc-" or "cx-"
            truncated = name[prefix_len:]
            if truncated.startswith("__new__-"):
                continue
            cwd = Path(cwd_str)
            project = projects.get(cwd)
            if project is None:
                continue
            # Resolve the truncated key back to the full session_id.
            if is_codex:
                # For Codex sessions, look up in the codex index.
                full_id = self._resolve_truncated_codex_id(truncated, cwd)
            else:
                full_id = self._resolve_truncated_id(truncated, project)
            if full_id is None:
                continue
            if full_id in self._running:
                continue
            self._running[full_id] = _Running(
                key=full_id,
                tmux_name=name,
                label=f"{project.display_name}/{full_id[:8]}",
                project=project,
            )
            found += 1
        if found:
            self._set_status(
                f"Found {found} running session(s)")

    @staticmethod
    def _resolve_truncated_id(truncated: str, project: Project) -> str | None:
        """Find the full session_id whose ``_safe_name`` matches *truncated*."""
        import os as _os
        try:
            with _os.scandir(project.claude_dir) as scan:
                for entry in scan:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    full_id = entry.name[:-6]  # strip ".jsonl"
                    if App._safe_name(full_id, 16) == truncated:
                        return full_id
        except OSError:
            pass
        return None

    def _resolve_truncated_codex_id(self, truncated: str, cwd: Path) -> str | None:
        """Find a Codex session_id whose ``_safe_name`` matches *truncated*,
        restricted to sessions whose cwd matches."""
        for meta in self._codex_index.sessions_for_cwd(cwd):
            if self._safe_name(meta.session_id, 16) == truncated:
                return meta.session_id
        return None

    def _show_overlay(self, modal: urwid.Widget, width: int, height: int,
                       *, click_outside_to_close: bool = False,
                       fixed_width: bool = False,
                       fixed_height: bool = False,
                       on_click_outside: Callable[[], None] | None = None) -> None:
        if self._loop is None:
            return
        # When the right pane is open the ccmgr sidebar is only ~30% of the
        # terminal.  Bump relative dimensions so overlays stay readable.
        # Fixed-pixel overlays (context menus) are left alone.
        if not fixed_width and self._right_pane_open():
            width = int(width * 1.6)
        if not fixed_height and self._right_pane_open():
            height = int(height * 1.35)
        width_spec = width if fixed_width else ("relative", width)
        height_spec = height if fixed_height else ("relative", height)
        overlay_cls = _CloseOnClickOverlay if click_outside_to_close else urwid.Overlay
        kw = {}
        if click_outside_to_close:
            kw["on_click_outside"] = on_click_outside or self._close_modal
        overlay = overlay_cls(
            modal, self._frame,
            align="center", width=width_spec,
            valign="middle", height=height_spec,
            **kw,
        )
        self._loop.widget = overlay

    def _close_modal(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._frame
        self._sessions_pane.set_selected_session(None)
        self._running_pane.set_selected(None)

    # --- key handling ---

    def _on_input(self, key: str) -> None:
        if key == "`":
            self._on_backtick()
            return
        if key == "esc":
            # Esc navigates "up" the pane hierarchy:
            #   Running → Sessions → Projects
            if self._sidebar.focus_position == 2:
                self._sidebar.focus_position = 1
                self._hint_bar.set_context(self._help_context())
                return
            if self._sidebar.focus_position == 1:
                self._sidebar.focus_position = 0
                self._hint_bar.set_context(self._help_context())
                return
            return
        if key == "ctrl c":
            self._open_quit_confirm()
            return
        if key in ("tab", "shift tab"):
            self._rotate_focus(reverse=(key == "shift tab"))
            return
        if key == "/":
            # Running pane doesn't support filtering — skip.
            if self._sidebar.focus_position == 2:
                self._set_status("No filter on Running pane.")
                return
            self._enter_filter_mode()
            return
        if key in ("[", "]"):
            self._resize_divider(key == "]")
            return
        # Simple action keys are dispatched from the shared keymap (single
        # source of truth shared with the hint bar) so the two can't drift.
        action = keymap.action_for(key)
        if action is not None:
            getattr(self, action)()
            return

    def _on_backtick(self) -> None:
        """Double-tap `` ` `` within the interval → toggle Codex developer mode."""
        now = time.monotonic()
        if (now - App._last_backtick_ts < App._DOUBLE_BACKTICK_INTERVAL
                and App._last_backtick_ts > 0):
            App._last_backtick_ts = 0.0
            self._toggle_codex_mode()
        else:
            App._last_backtick_ts = now

    def _toggle_codex_mode(self) -> None:
        """Switch between Claude and Codex views."""
        self._codex_mode = not self._codex_mode
        if self._codex_mode:
            self._codex_project_filter = self._codex_index.all_cwds()
            self._projects_pane.set_projects(self._visible_projects())
            if not self._codex_project_filter:
                self._set_status("Codex mode — no Codex sessions found  (double-tap ` to exit)")
                self._sessions_pane.set_sessions(None, [],
                    running_ids=set(self._running),
                    favorite_ids=self._favorites.get_ids())
                return
            self._set_status("Codex mode  (double-tap ` to exit)")
            # Switch to a project that has Codex sessions, if available.
            if self._selected_project is not None and self._selected_project.real_path in self._codex_project_filter:
                self._on_project_select(self._selected_project)
            else:
                matched = self._first_codex_project()
                if matched is not None:
                    self._on_project_select(matched)
                else:
                    self._sessions_pane.set_sessions(None, [],
                        running_ids=set(self._running),
                        favorite_ids=self._favorites.get_ids())
        else:
            self._projects_pane.set_projects(self._visible_projects())
            self._set_status("Claude mode  (double-tap ` for Codex)")
            if self._selected_project is not None:
                self._on_project_select(self._selected_project)

    def _codex_env(self) -> dict[str, str]:
        """Capture environment variables needed by Codex.

        Codex uses ``env_key`` from its config to name the API key variable
        (e.g. ``DEEPSEEK_API_KEY``).  First check the current process
        environment; when that misses (e.g. ccmgr was launched without a
        login shell), probe the user's shell profile via ``bash -lic``.
        """
        import os as _os
        import subprocess as _sp
        result: dict[str, str] = {}
        env_key = self._read_codex_env_key()
        if not env_key:
            return result
        val = _os.environ.get(env_key)
        if not val:
            # Not in the current process — probe the user's shell profile.
            # ``-l`` sources ~/.bash_profile; ``-i`` sources ~/.bashrc;
            # together they cover every common setup.
            try:
                out = _sp.run(
                    ["bash", "-lic", f"echo ${{{env_key}}}"],
                    capture_output=True, text=True, timeout=5,
                )
                val = out.stdout.strip()
            except (OSError, _sp.TimeoutExpired):
                pass
        if val:
            result[env_key] = val
        return result

    @staticmethod
    def _read_codex_env_key() -> str | None:
        """Parse ``env_key`` from ``~/.codex/config.toml``, if present."""
        import os as _os
        try:
            import tomllib
        except ImportError:
            return None
        config_path = Path(_os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        try:
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        # The env_key lives under [model_providers.<name>].
        providers = data.get("model_providers", {})
        for _name, cfg in providers.items():
            if isinstance(cfg, dict):
                key = cfg.get("env_key")
                if key:
                    return str(key)
        return None

    def _first_codex_project(self) -> Project | None:
        """First Claude project whose cwd has Codex sessions."""
        projects = self._visible_projects(force=True)
        return projects[0] if projects else None

    def _visible_projects(self, *, force: bool = False) -> list[Project]:
        """Projects for the current mode.

        Claude mode: all projects (unchanged). Codex mode: only projects
        whose ``real_path`` has at least one Codex session.
        """
        now = time.monotonic()
        projects = self._project_snapshot
        if (force or projects is None
                or now - self._project_snapshot_at >= self._PROJECT_SCAN_INTERVAL):
            projects = list_projects(self._claude_home)
            self._project_snapshot = projects
            self._project_snapshot_at = now
        if not self._codex_mode:
            return projects
        # Build a resolve-safe lookup: real_path → project.
        by_resolved: dict[Path, Project] = {}
        for p in projects:
            try:
                by_resolved[p.real_path.resolve()] = p
            except OSError:
                by_resolved[p.real_path] = p
        visible: list[Project] = []
        seen_encoded: set[str] = set()
        for cwd in self._codex_project_filter:
            try:
                key = cwd.resolve()
            except OSError:
                key = cwd
            existing = by_resolved.get(key)
            if existing is not None:
                if existing.encoded_name not in seen_encoded:
                    seen_encoded.add(existing.encoded_name)
                    visible.append(existing)
            else:
                # Codex-only directory — synthesise a project entry so the
                # user can browse and launch sessions here.
                synth = self._synthesise_codex_project(cwd)
                if synth.encoded_name not in seen_encoded:
                    seen_encoded.add(synth.encoded_name)
                    visible.append(synth)
        # Sort by recency: Claude projects by last_activity_ts, synthetic
        # ones by their most recent Codex session.
        def _sort_key(p: Project) -> float:
            ts = p.last_activity_ts
            if ts == 0.0:
                sessions = self._codex_index.sessions_for_cwd(p.real_path)
                if sessions:
                    ts = sessions[0].last_mtime
            return -ts
        visible.sort(key=lambda p: _sort_key(p))
        return visible

    def _invalidate_project_snapshot(self) -> None:
        self._project_snapshot_at = 0.0

    @staticmethod
    def _synthesise_codex_project(cwd: Path) -> Project:
        """Create a synthetic Project for a Codex-only directory."""
        from ccmgr.codex_index import _safe_encoded_name
        try:
            resolved = cwd.resolve()
        except OSError:
            resolved = cwd
        return Project(
            real_path=resolved,
            encoded_name=_safe_encoded_name(resolved),
            claude_dir=Path(),  # no Claude sessions directory
            session_count=0,
            last_activity_ts=0.0,
        )

    def _rotate_focus(self, reverse: bool = False) -> None:
        """Tab / Shift-Tab cycle through the three ccmgr sidebar panes.

        Jumping in/out of the claude pane uses tmux's native nav (Ctrl-B ←/→)
        so Tab keeps its normal meaning inside claude (autocomplete).
        """
        n = len(self._sidebar.contents)
        if n <= 1:
            return
        cur = self._sidebar.focus_position
        self._sidebar.focus_position = (cur - 1) % n if reverse else (cur + 1) % n
        self._hint_bar.set_context(self._help_context())

    def _teardown_tmux(self) -> None:
        """Clean up on quit.

        Called exactly once, from ``run()``'s ``finally`` block, for both
        hard and soft quit.  On soft quit (``_soft_quit_flag`` is set) the
        detached Claude sessions and outer tmux session are left alive.
        """
        self._teardown_scroll_acceleration()
        # Remove the F9 fullscreen binding we installed (it's server-global).
        try:
            import subprocess as _sp
            _sp.run(["tmux", "unbind-key", "-n", "F9"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        if self._right_pane_id:
            try:
                tmux_ctl.kill_pane(self._right_pane_id)
            except Exception:
                pass
            self._right_pane_id = None
        if self._soft_quit_flag:
            return  # <-- soft quit: leave cc-* and outer tmux session alive
        for r in list(self._running.values()):
            try:
                tmux_ctl.kill_session(r.tmux_name)
            except Exception:
                pass
        self._running.clear()
        if self._auto_launched:
            session_name = tmux_ctl.current_session_name()
            if session_name == "ccmgr":
                try:
                    tmux_ctl.kill_session("ccmgr")
                except Exception:
                    pass

    def _enter_filter_mode(self) -> None:
        # Swap the status row (footer index 2) for a filter Edit, keeping the
        # 2-line height so the sidebar doesn't jump.
        edit = urwid.Edit(caption="filter: ")
        filter_body = urwid.Pile([edit, urwid.Text("")])
        footer_pile = self._frame.contents["footer"][0]
        footer_pile.contents[2] = (filter_body, footer_pile.options("pack"))
        footer_pile.focus_position = 2
        self._frame.focus_position = "footer"

        def on_change(widget, new_text):
            current_idx = self._sidebar.focus_position
            if current_idx == 0:
                self._projects_pane.set_filter(new_text)
            elif current_idx == 1:
                self._sessions_pane.set_filter(new_text)
            # No filter for the Running pane (small list, not worth filtering).

        urwid.connect_signal(edit, "change", on_change)

        def restore(key):
            if key in ("enter", "esc"):
                footer_pile.contents[2] = (self._status, footer_pile.options("pack"))
                self._frame.focus_position = "body"
                return None
            return key

        original_keypress = edit.keypress
        def new_keypress(size, key):
            handled = restore(key)
            if handled is None:
                return None
            return original_keypress(size, key)
        edit.keypress = new_keypress

    # --- periodic refresh ---

    def _refresh(self) -> None:
        self._scroll_manager.maintain()
        prefix = "cx-" if self._codex_mode else "cc-"
        needs_liveness = self._right_pane_id is not None or any(
            r.tmux_name.startswith(prefix) for r in self._running.values())
        server = tmux_ctl.server_snapshot() if needs_liveness else None
        child_probes: dict[str, bool | None] = {}

        def session_is_alive(name: str) -> bool:
            if server is not None:
                return name in server.sessions
            return tmux_ctl.session_exists(name)

        def pane_is_alive(pane_id: str) -> bool:
            if server is not None:
                return pane_id in server.panes
            return tmux_ctl.pane_alive(pane_id)

        # A refresh may need several Codex views. Walk its session tree once,
        # then serve each view from the same mtime-keyed index snapshot.
        refresh_codex = self._codex_mode or any(
            r.tmux_name.startswith("cx-") for r in self._running.values())
        if refresh_codex:
            self._codex_index.refresh()

        # Refresh the Codex project filter so newly-created Codex sessions
        # make their cwd appear as a project in Codex mode.
        if self._codex_mode:
            self._codex_project_filter = self._codex_index.all_cwds(refresh=False)
        # Placeholder resolution must discover its JSONL without extra delay.
        force_projects = any(r.is_placeholder for r in self._running.values())
        projects = self._visible_projects(force=force_projects)
        self._projects_pane.set_projects(projects)
        # Prune dead tmux sessions (e.g. claude/codex exited via /quit).
        for key in list(self._running):
            if self._running[key].tmux_name.startswith(prefix):
                if not session_is_alive(self._running[key].tmux_name):
                    del self._running[key]

        # If the session we were showing in the right pane has exited,
        # kill the right pane so the TUI returns to full-screen.
        if self._right_pane_id and self._right_pane_claude:
            if not session_is_alive(self._right_pane_claude):
                tmux_ctl.kill_pane(self._right_pane_id)
                self._right_pane_id = None
                self._right_pane_claude = None
                self._set_active_target(None, None)

        # Detect when the right pane was closed (user pressed q in less, the
        # pane was cleaned up above, or it was killed externally).
        if self._right_pane_id and not pane_is_alive(self._right_pane_id):
            self._right_pane_id = None
            self._right_pane_claude = None
            if self._in_history_mode:
                self._in_history_mode = False
                self._set_active_target(None, None)
                self._restore_from_history_mode()
            else:
                self._set_active_target(None, None)

        # Promote any `__new__-N` placeholders to their real session id — in
        # BOTH Claude and Codex mode. While a session stays a placeholder its
        # real-UUID row (filled from the on-disk scan) looks "not running", so
        # clicking it spawns a duplicate session; and `force_projects` above
        # stays stuck True, defeating the 3s project-scan cache. Codex
        # resolution must run too or neither ever clears.
        self._resolve_placeholders(projects)
        running_ids = set(self._running)
        if self._selected_project is not None:
            matched = next((p for p in projects if p.encoded_name == self._selected_project.encoded_name), None)
            if matched is not None:
                self._selected_project = matched
                if self._codex_mode:
                    sessions = self._codex_index.sessions_for_cwd(
                        matched.real_path,
                        refresh=False,
                    )
                else:
                    sessions = [self._refine_status(s, child_probes, server)
                                for s in self._session_cache.list_sessions(matched)]
                self._sessions_pane.set_sessions(matched, sessions, running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())
                # Correct the project's session_count to match what we actually
                # listed (filters bg sessions; in Codex mode uses the Codex index).
                # The file-based count from discovery is a rough upper bound.
                real_count = len(sessions)
                if matched.session_count != real_count:
                    corrected = replace(matched, session_count=real_count)
                    # Update snapshot and view so the sidebar counter updates.
                    if self._project_snapshot:
                        for i, p in enumerate(self._project_snapshot):
                            if p.encoded_name == matched.encoded_name:
                                self._project_snapshot[i] = corrected
                                break
                    self._selected_project = corrected
                    projects = [corrected if p.encoded_name == matched.encoded_name
                                else p for p in projects]
                    self._projects_pane.set_projects(projects)
            else:
                self._selected_project = None
                self._projects_pane.set_selected(None)
                self._sessions_pane.set_sessions(None, [], running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())

        self._update_running_pane(child_probes, server)
        # Advance the status-bar state machine (TTL expiry + idle tip rotation)
        self._update_status()
        # Keep the hint bar showing only the keys valid for the focused pane.
        self._hint_bar.set_context(self._help_context())

    _HELP_CONTEXTS = (keymap.CTX_PROJECTS, keymap.CTX_SESSIONS, keymap.CTX_RUNNING)

    def _help_context(self) -> str:
        """Map the focused sidebar pane (0/1/2) to a keymap context name.

        When the right-hand agent pane has focus (via Ctrl-B →), return the
        agent context so the help bar shows only the two keys that matter:
        Ctrl-B ← (back to sidebar) and F9 (fullscreen)."""
        if not self._ccmgr_has_focus:
            return keymap.CTX_AGENT
        pos = self._sidebar.focus_position
        if 0 <= pos < len(self._HELP_CONTEXTS):
            return self._HELP_CONTEXTS[pos]
        return keymap.CTX_PROJECTS

    def _effective_status(
        self,
        meta: SessionMeta,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> str:
        """Displayed status, refined by the live process when we own the session.

        For a session ccmgr has opened, a pending ``tool_use`` with a live
        child process means a tool is actively running (busy); no child means
        Claude is waiting for approval (blocked). Probe failures fall back to
        ``meta.status`` (the JSONL time heuristic). Used by both panes so the
        same session never shows two different dots.
        """
        if meta.pending_tool:
            r = self._running.get(meta.session_id)
            if r is not None and not r.is_placeholder:
                if child_probes is not None and r.tmux_name in child_probes:
                    has_child = child_probes[r.tmux_name]
                else:
                    pane_pid = (
                        server.pane_pid_for(r.tmux_name)
                        if server is not None else None)
                    if pane_pid is not None:
                        has_child = tmux_ctl.process_has_child(pane_pid)
                    else:
                        has_child = tmux_ctl.session_has_child(r.tmux_name)
                    if child_probes is not None:
                        child_probes[r.tmux_name] = has_child
                if has_child is not None:
                    return "busy" if has_child else "blocked"
        return meta.status

    def _refine_status(
        self,
        meta: SessionMeta,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> SessionMeta:
        """Return `meta` with its status refined, copying only when it changes."""
        status = self._effective_status(meta, child_probes, server)
        return meta if status == meta.status else replace(meta, status=status)

    def _update_running_pane(
        self,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> None:
        """Sync labels/status and repopulate the Running pane."""
        for r in self._running.values():
            if r.is_placeholder or r.project is None:
                continue
            if r.tmux_name.startswith("cx-"):
                meta = self._codex_index.get(r.key, refresh=False)
            else:
                meta = self._session_cache.get(r.project, r.key)
            if meta is not None:
                if meta.title:
                    r.label = f"{meta.project.display_name}/{meta.display_title}"
                r.status = self._effective_status(meta, child_probes, server)
        self._render_running_pane()

    def _render_running_pane(self) -> None:
        """Render registry values without doing metadata or process I/O.

        In Claude mode only ``cc-*`` sessions are shown; in Codex mode only
        ``cx-*`` sessions are shown.  The other type's sessions still run,
        but they don't belong in the current view.
        """
        prefix = "cx-" if self._codex_mode else "cc-"
        entries = [
            RunningEntry(tmux_name=r.tmux_name, label=r.label, status=r.status)
            for r in self._running.values()
            if r.tmux_name.startswith(prefix)
        ]
        self._running_pane.set_running(entries)

    def _resolve_placeholders(self, projects: list[Project]) -> None:
        """Re-key any `__new__-N` placeholder to its real session_id.

        For each live placeholder, look at its project's sessions and pick the
        newest one created after the placeholder timestamp whose session_id is
        not already claimed by another running session.

        Works in both modes: Claude placeholders resolve against the Claude
        session cache, Codex placeholders against the Codex index (already
        walked once this refresh, so served snapshot-only).
        """
        placeholders = [r for r in self._running.values() if r.is_placeholder]
        if not placeholders:
            return
        # Index projects by real_path for cheap lookup. New-project flow may
        # create a project dir that didn't exist when ccmgr started, so we
        # rely on `projects` being a fresh list_projects() result.
        by_path = {p.real_path: p for p in projects}
        claimed = set(self._running)
        for r in placeholders:
            project = by_path.get(r.placeholder_path)
            if project is None:
                continue
            if self._codex_mode:
                # Codex index was already refreshed once this tick (see
                # _refresh); serve from that snapshot rather than re-walking
                # the tree, and don't use the Claude-only session cache.
                sessions = self._codex_index.sessions_for_cwd(
                    project.real_path, refresh=False)
            else:
                sessions = self._session_cache.list_sessions(project)
            # Newest session created since this placeholder was launched, not
            # already in use by another running session.
            candidate: SessionMeta | None = None
            for s in sessions:
                if s.session_id in claimed:
                    continue
                if s.last_mtime + 1.0 < r.created_at:
                    continue
                if candidate is None or s.last_mtime > candidate.last_mtime:
                    candidate = s
            if candidate is None:
                continue
            # Re-key the entry from the placeholder to the real session_id.
            del self._running[r.key]
            r.key = candidate.session_id
            r.label = f"{candidate.project.display_name}/{candidate.display_title}"
            r.project = candidate.project
            r.placeholder_path = None
            r.created_at = 0.0
            self._running[candidate.session_id] = r
            claimed.add(candidate.session_id)
            if self._right_pane_claude == r.tmux_name:
                self._set_active_target(candidate.session_id, r.tmux_name)

    def _currently_focused_session_meta(self) -> SessionMeta | None:
        if not self._sessions_pane._walker:
            return None
        focus_w, _ = self._sessions_pane._walker.get_focus()
        from ccmgr.ui.sessions_pane import _SessionRow
        if isinstance(focus_w, _SessionRow):
            return focus_w.session
        return None

    def _find_session_meta(self, session_id: str, project: Project | None = None) -> SessionMeta | None:
        """Look up session metadata by ID, optionally scoped to a project."""
        if project is None:
            return None
        from ccmgr.session_index import _scan_session
        jsonl_path = project.claude_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return None
        return _scan_session(project, jsonl_path)

    # --- kill / delete session ---

    def _on_kill_session(self) -> None:
        """Kill the running Claude process without deleting the JSONL file.

        Works from both Sessions pane (pos 1) and Running pane (pos 2).
        """
        pos = self._sidebar.focus_position
        if pos == 2:
            # Running pane — kill the focused running entry.
            from ccmgr.ui.running_pane import _RunningRow
            if not self._running_pane._walker:
                self._set_status("No running session selected.")
                return
            focus_w, _ = self._running_pane._walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._set_status("No running session selected.")
                return
            r = self._by_tmux(focus_w.entry.tmux_name)
            if r is None:
                self._set_status("Session not found in registry.")
                return
            if not tmux_ctl.session_exists(r.tmux_name):
                self._set_status(f"tmux session already gone: {r.tmux_name}")
                return
            tmux_ctl.kill_session(r.tmux_name)
            del self._running[r.key]
            self._set_status(f"Killed: {r.label}  (file kept)")
            return

        # Sessions pane (pos 1 or default).
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected.")
            return
        r = self._running.get(session.session_id)
        if r is None:
            self._set_status(f"'{session.display_title}' is not running.")
            return
        if not tmux_ctl.session_exists(r.tmux_name):
            self._set_status(f"tmux session already gone: {r.tmux_name}")
            return
        tmux_ctl.kill_session(r.tmux_name)
        del self._running[session.session_id]
        self._set_status(f"Killed: {session.display_title}  (file kept)")

    def _on_delete_session(self) -> None:
        """Delete the focused session from the current pane (with confirmation)."""
        pos = self._sidebar.focus_position

        if pos == 1:
            # Sessions pane — delete the focused session (JSONL + tmux).
            session = self._currently_focused_session_meta()
            if session is None:
                self._set_status("No session selected to delete.")
                return
            title = session.display_title
            detail = f"Permanently delete '{title}'?\n\nThis removes the session file from disk\nand kills its background tmux session."
            modal = DeleteConfirmModal(
                title=f"Delete '{title}'?",
                detail=detail,
                on_confirm=lambda: self._do_delete_session(session),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

        elif pos == 2:
            # Running pane — kill the detached tmux session.
            from ccmgr.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if not running_walker:
                self._set_status("No running session selected.")
                return
            focus_w, _ = running_walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._set_status("No running session selected.")
                return
            entry = focus_w.entry
            r = self._by_tmux(entry.tmux_name)
            label = r.label if r else entry.tmux_name
            # Real session_id (and project) only exist once resolved — needed
            # to also delete the JSONL.
            session_id = r.key if (r and not r.is_placeholder) else None
            project = r.project if r else None
            if session_id:
                detail = (f"Kill '{label}'?\n\n"
                          "The detached tmux session will be killed.\n"
                          "The session file will be deleted from disk.")
            else:
                detail = f"Kill '{label}'?\n\nThe detached tmux session will be killed."
            modal = DeleteConfirmModal(
                title=f"Kill '{label}'?",
                detail=detail,
                on_confirm=lambda: self._do_kill_running(entry.tmux_name, session_id, project),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

        else:
            self._set_status("Use d on a session row or running-entry row to delete.")

    def _do_delete_session(self, session: SessionMeta) -> None:
        """Delete a session completely: kill tmux, remove JSONL, clean up
        session-env, and refresh the UI so pane rows stay aligned."""
        self._close_modal()
        self._cleanup_session(
            session_id=session.session_id,
            jsonl_path=session.jsonl_path,
            label=session.display_title,
        )

    def _do_kill_running(self, tmux_name: str, session_id: str | None,
                         project: Project | None) -> None:
        """Kill a detached tmux session; delete JSONL + session-env if known."""
        self._close_modal()
        jsonl_path: Path | None = None
        if session_id and not session_id.startswith("__new__-") and project:
            jsonl_path = project.claude_dir / f"{session_id}.jsonl"
        self._cleanup_session(
            session_id=session_id, jsonl_path=jsonl_path,
            tmux_name=tmux_name, label=tmux_name,
        )

    def _cleanup_session(self, session_id: str | None = None,
                         jsonl_path: Path | None = None,
                         tmux_name: str | None = None,
                         label: str = "") -> None:
        """Unified session cleanup: kill tmux → remove files → refresh UI."""
        # 1. Kill the detached tmux session first (avoid race conditions).
        if tmux_name is None and session_id is not None:
            r = self._running.get(session_id)
            tmux_name = r.tmux_name if r else None
        if tmux_name and tmux_ctl.session_exists(tmux_name):
            tmux_ctl.kill_session(tmux_name)

        # 2. Remove from our running-session registry.
        if session_id is not None:
            self._running.pop(session_id, None)
        if tmux_name is not None:
            for key in [k for k, r in self._running.items() if r.tmux_name == tmux_name]:
                del self._running[key]

        # 3. Delete the JSONL file (conversation history).
        if jsonl_path is not None:
            try:
                jsonl_path.unlink(missing_ok=True)
            except OSError:
                pass

        # 4. Remove Claude's session-env directory (session metadata).
        if session_id is not None and not session_id.startswith("__new__-"):
            env_dir = Path.home() / ".claude" / "session-env" / session_id
            if env_dir.is_dir():
                shutil.rmtree(env_dir, ignore_errors=True)

        # 5. Remove from Claude's history index so it doesn't recreate a
        #    metadata stub (Claude rebuilds missing JSONLs from this index).
        if session_id is not None and not session_id.startswith("__new__-"):
            self._remove_from_history(session_id)

        # 6. Invalidate caches and refresh so the UI reflects the deletion
        #    immediately — no stale rows that point to deleted sessions.
        self._session_cache.invalidate()
        self._invalidate_project_snapshot()
        self._refresh()

        self._set_status(f"Deleted: {label}")

    @staticmethod
    def _remove_from_history(
        session_id: str, _attempts: int = 3,
    ) -> None:
        """Strip every line referencing *session_id* from ~/.claude/history.jsonl.

        Claude Code uses this file as a session index — when a JSONL is deleted
        but the history entry remains, Claude rebuilds an empty metadata stub on
        the next launch.  Removing the entry prevents that.
        """
        history_path = Path.home() / ".claude" / "history.jsonl"
        if not history_path.is_file():
            return
        try:
            source_stat = history_path.stat()
            lines = history_path.read_text().splitlines()
        except OSError:
            return
        kept = []
        changed = False
        import json as _json
        for line in lines:
            line_s = line.strip()
            if not line_s:
                continue
            try:
                rec = _json.loads(line_s)
            except (ValueError, _json.JSONDecodeError):
                kept.append(line)
                continue
            if rec.get("sessionId") == session_id:
                changed = True
                continue
            kept.append(line)
        if changed:
            try:
                current_stat = history_path.stat()
                source_signature = (
                    source_stat.st_ino, source_stat.st_mtime_ns, source_stat.st_size)
                current_signature = (
                    current_stat.st_ino, current_stat.st_mtime_ns, current_stat.st_size)
                if current_signature != source_signature:
                    if _attempts > 1:
                        App._remove_from_history(session_id, _attempts - 1)
                    return
                atomic_write_text(
                    history_path, "\n".join(kept) + ("\n" if kept else ""))
            except OSError:
                pass

    # --- rename session ---

    def _on_rename_session(self) -> None:
        """Open the rename modal for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected to rename.")
            return
        modal = RenameModal(
            current_title=session.display_title,
            on_submit=lambda new_title, s=session: self._do_rename(s, new_title),
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=50, height=22)

    def _do_rename(self, session: SessionMeta, new_title: str) -> None:
        """Write a new ai-title record to the session JSONL."""
        self._close_modal()
        import json
        record = json.dumps({"type": "ai-title", "aiTitle": new_title})
        try:
            with session.jsonl_path.open("a") as f:
                f.write(record + "\n")
        except OSError as e:
            self._set_status(f"Failed to rename: {e}")
            return
        # Invalidate the cache and refresh so both Sessions and Running
        # panes pick up the new title immediately.
        self._session_cache.invalidate()
        self._invalidate_project_snapshot()
        self._refresh()
        self._set_status(f"Renamed to: {new_title}")

    # --- toggle favorite ---

    def _on_toggle_star(self) -> None:
        """Toggle star status for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected.")
            return
        now_star = self._favorites.toggle(session.session_id)
        label = "★" if now_star else "unstarred"
        self._set_status(f"{label} {session.display_title}")

    # --- context menu (right-click) ---

    def _on_running_context_menu(self, entry: RunningEntry) -> None:
        r = self._by_tmux(entry.tmux_name)
        if r is None or r.project is None:
            return
        self._running_pane.set_selected(entry.tmux_name)
        # Ensure tmux focus is on our pane so the 200 ms poll doesn't
        # auto-close the menu (can happen if focus was on the right pane).
        if self._ccmgr_pane_id:
            tmux_ctl.select_pane(self._ccmgr_pane_id)
        if r.is_placeholder:
            # Placeholder: no SessionMeta yet, but we can still kill the
            # running tmux session or switch to it.
            tmux = r.tmux_name
            label = r.label
            items: list[tuple[str, Callable[[], None]]] = [
                (" Open      ↵", lambda: self._attach_in_right_pane(tmux,
                                         steal_focus=True)),
                (" Kill       k", lambda: self._kill_tmux_session(tmux, label)),
            ]
            if r.project is not None:
                proj = r.project
                items.append(
                    (" Term       t", lambda: self._open_terminal_for_project(proj)))
            menu = ContextMenu(items, on_close=self._close_modal)
            self._show_overlay(menu, width=32, height=12,
                               click_outside_to_close=True,
                               fixed_width=True, fixed_height=True)
            return
        session = self._find_session_meta(r.key, r.project)
        if session is None:
            return
        self._open_session_context_menu(session)

    def _open_session_context_menu(self, session: SessionMeta) -> None:
        # Ensure tmux focus is on our pane so the 200 ms poll doesn't
        # auto-close the menu (can happen if focus was on the right pane).
        if self._ccmgr_pane_id:
            tmux_ctl.select_pane(self._ccmgr_pane_id)
        self._sessions_pane.set_selected_session(session.session_id)
        r = self._running.get(session.session_id)
        is_alive = r is not None and not r.is_placeholder
        is_starred = session.session_id in self._favorites.get_ids()
        items: list[tuple[str, Callable[[], None]]] = [
            (" Open      ↵", lambda s=session: self._do_context_open(s)),
            (" Info       i", lambda s=session: self._do_context_info(s)),
            (" Rename     r", lambda s=session: self._do_context_rename(s)),
            (" Unstar    s" if is_starred else " Star      s",
             lambda s=session: self._do_context_star(s)),
            (" Kill       k", lambda s=session: self._do_context_kill(s)
             if is_alive else None),
            (" Term       t", lambda s=session: self._do_context_term(s)),
            (" Delete     d", lambda s=session: self._do_context_delete(s)),
        ]
        # Filter out None callbacks (e.g. Kill for non-running sessions).
        items = [(label, cb) for label, cb in items if cb is not None]
        menu = ContextMenu(items, on_close=self._close_modal)
        self._show_overlay(menu, width=32, height=14,
                           click_outside_to_close=True,
                           fixed_width=True, fixed_height=True)

    def _do_context_open(self, session: SessionMeta) -> None:
        self._on_session_select(session, steal_focus=True)

    def _do_context_rename(self, session: SessionMeta) -> None:
        modal = RenameModal(
            current_title=session.display_title,
            on_submit=lambda new_title, s=session: self._do_rename(s, new_title),
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=50, height=22)

    def _do_context_info(self, session: SessionMeta) -> None:
        r = self._running.get(session.session_id)
        running_label = None
        if r and tmux_ctl.session_exists(r.tmux_name):
            running_label = f"detached as '{r.tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label,
                                 on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40,
                           click_outside_to_close=True)

    def _do_context_star(self, session: SessionMeta) -> None:
        now_star = self._favorites.toggle(session.session_id)
        self._session_cache.invalidate()
        self._refresh()
        label = "★" if now_star else "unstarred"
        self._set_status(f"{label} {session.display_title}")

    def _do_context_kill(self, session: SessionMeta) -> None:
        r = self._running.get(session.session_id)
        if r and tmux_ctl.session_exists(r.tmux_name):
            tmux_ctl.kill_session(r.tmux_name)
        self._running.pop(session.session_id, None)
        self._set_status(f"Killed: {session.display_title}  (file kept)")

    def _kill_tmux_session(self, tmux_name: str, label: str) -> None:
        """Kill a running tmux session by name (no SessionMeta needed)."""
        if tmux_ctl.session_exists(tmux_name):
            tmux_ctl.kill_session(tmux_name)
        # Remove any _running entry keyed by this tmux name.
        for key in [k for k, r in self._running.items() if r.tmux_name == tmux_name]:
            del self._running[key]
        self._set_status(f"Killed: {label}  (file kept)")

    def _open_terminal_for_project(self, project: Project) -> None:
        """Open a terminal in the given project directory."""
        import os
        import shlex
        import subprocess as _sp
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(project.real_path))} && exec {shlex.quote(shell)}"
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        _sp.run(["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        tmux_ctl.select_pane(new_pane)
        self._set_ccmgr_focus(False)
        self._set_status(f"terminal: {project.display_name}")

    def _do_context_term(self, session: SessionMeta) -> None:
        import os
        import shlex
        import subprocess as _sp
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(session.project.real_path))} && exec {shlex.quote(shell)}"
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        _sp.run(["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        tmux_ctl.select_pane(new_pane)
        self._set_ccmgr_focus(False)
        self._set_status(f"terminal: {session.project.display_name}")

    def _do_context_delete(self, session: SessionMeta) -> None:
        title = session.display_title
        detail = f"Permanently delete '{title}'?\n\nThis removes the session file from disk\nand kills its background tmux session."
        modal = DeleteConfirmModal(
            title=f"Delete '{title}'?",
            detail=detail,
            on_confirm=lambda s=session: self._do_delete_session(s),
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=54, height=30)

    # --- resize divider ---

    def _resize_divider(self, expand_ccmgr: bool) -> None:
        """Move the vertical divider: [ shrinks ccmgr, ] expands it."""
        if not self._right_pane_id or not tmux_ctl.pane_alive(self._right_pane_id):
            self._set_status("No agent pane to resize against.")
            return
        direction = "-R" if expand_ccmgr else "-L"
        tmux_ctl.resize_pane(self._right_pane_id, direction, 5)

    # --- status bar ---

    # How long an explicit message holds the bar before it falls back to idle
    # tips. Errors are sticky (cleared only by the next message or action);
    # warnings linger; routine info is brief. Tips rotate on their own cadence.
    _STATUS_TTL = {"error": None, "warn": 12.0, "info": 6.0}
    _TIP_INTERVAL = 20.0

    # Minimum time a message is protected from being overwritten by a *lower*
    # severity message, so a genuine error/warning isn't clobbered by a routine
    # "Project: …" the very next tick. A message of equal-or-higher severity
    # always wins immediately. Info has no floor (routine, freely replaceable).
    _STATUS_MIN_HOLD = {"error": 4.0, "warn": 2.0}
    _LEVEL_PRIORITY = {"tip": 0, "info": 1, "warn": 2, "error": 3}

    def _set_status(self, msg: str, level: str | None = None) -> None:
        """Show an explicit status message.

        ``level`` is auto-classified from the message prefix when omitted, so
        the ~40 existing call sites keep working unchanged: ``ERROR…`` → error,
        ``WARNING…``/``Failed…``/``failed…`` → warn, everything else → info.
        """
        if level is None:
            if msg.startswith("ERROR"):
                level = "error"
            elif msg.startswith(("WARNING", "Failed", "failed")):
                level = "warn"
            else:
                level = "info"
        # Don't let a still-fresh higher-severity message be overwritten by a
        # lower-severity one within its minimum-hold window.
        if self._status_text is not None:
            hold = self._STATUS_MIN_HOLD.get(self._status_level)
            if (hold is not None
                    and time.monotonic() - self._status_since < hold
                    and self._LEVEL_PRIORITY.get(level, 1)
                    < self._LEVEL_PRIORITY.get(self._status_level, 1)):
                return
        self._status_text = msg
        self._status_level = level
        self._status_since = time.monotonic()
        self._status.set_message(msg, level)

    def _update_status(self) -> None:
        """Advance the status-bar state machine once per tick.

        Holds an explicit message for its TTL, then falls back to cycling idle
        tips. Called from ``_refresh`` instead of the old unconditional
        ``set_message`` that clobbered one-shot messages every poll.
        """
        now = time.monotonic()
        if self._status_text is not None:
            ttl = self._STATUS_TTL.get(self._status_level, 6.0)
            if ttl is None or now - self._status_since < ttl:
                return
            # Expired → clear and fall through so the idle branch shows the next
            # tip on this same tick (and advances the index uniformly, avoiding
            # the first post-message tip lingering for two intervals).
            self._status_text = None
            self._tip_since = 0.0
        # Idle: rotate tips on their own cadence.
        if not TIPS:
            return
        if self._tip_since == 0.0 or now - self._tip_since >= self._TIP_INTERVAL:
            self._status.set_message(TIPS[self._tip_index], "tip")
            self._tip_index = (self._tip_index + 1) % len(TIPS)
            self._tip_since = now

    # --- periodic refresh ---

    def _on_tick(self, loop, _user_data) -> None:
        self._refresh()
        # When a click-outside overlay is showing OR we're in history mode
        # (less running in the right pane), poll faster so the user sees a
        # quick response when pressing q in less or clicking the right pane.
        fast_poll = (
            self._in_history_mode
            or (self._ccmgr_pane_id is not None
                and self._loop is not None
                and isinstance(self._loop.widget, _CloseOnClickOverlay))
        )
        if fast_poll:
            if (self._ccmgr_pane_id is not None
                    and self._loop is not None
                    and isinstance(self._loop.widget, _CloseOnClickOverlay)):
                if tmux_ctl.current_pane_id() != self._ccmgr_pane_id:
                    self._close_modal()
            interval_s = 0.2
        else:
            interval_s = self._config.poll_interval_ms / 1000.0
        loop.set_alarm_in(interval_s, self._on_tick)

    # --- lifecycle ---

    def run(self) -> None:
        # - mouse on: tmux switches pane focus on clicks so keyboard input
        #   tracks the active pane and the border colour updates accordingly.
        # - set-clipboard on: text selection in either pane is copied to the
        #   system clipboard.
        import subprocess as _sp
        if tmux_ctl.in_tmux():
            sess = tmux_ctl.current_session_name() or "ccmgr"
            _sp.run(
                ["tmux", "set-option", "-t", sess, "set-clipboard", "on"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            # Force OSC 52 passthrough so a left-drag selection in either pane
            # copies to the *local* system clipboard (works over SSH / nested
            # tmux on OSC-52-capable terminals). Pairs with set-clipboard on.
            tmux_ctl.enable_clipboard_passthrough()
            _sp.run(
                ["tmux", "set-option", "-t", sess, "focus-events", "on"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            _sp.run(
                ["tmux", "set-option", "-t", sess, "mouse", "on"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            # Unbind tmux's built-in right-click context menu so right-click
            # passes through to the application (Claude / less) instead of
            # flashing display-menu.  Left-click (MouseDown1Pane) is left at
            # its default: tmux switches pane focus then forwards the event.
            _sp.run(
                ["tmux", "unbind-key", "-T", "root", "MouseDown3Pane"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        self._ccmgr_pane_id = tmux_ctl.current_pane_id()
        self._set_ccmgr_focus(True, force_border=True)
        screen = urwid.raw_display.Screen(focus_reporting=True)
        self._loop = urwid.MainLoop(
            self._frame,
            palette=PALETTE,
            screen=screen,
            input_filter=self._filter_input,
            unhandled_input=self._on_input,
        )
        from ccmgr.ui._widgets import ClickableRow
        ClickableRow._main_loop = self._loop
        self._hint_bar.set_loop(self._loop)
        try:
            self._loop.screen.set_terminal_properties(colors=256)
        except Exception:
            pass
        # Intercept Ctrl-C as a regular keypress so we can show a confirm-quit
        # popup instead of slamming out via SIGINT. Ctrl-\ (quit) is left
        # active as an emergency hard-exit.
        try:
            self._loop.screen.tty_signal_keys(intr="undefined")
        except Exception:
            pass
        # Right pane is created lazily on first session launch — startup is
        # ccmgr-only, no empty pane.
        self._loop.set_alarm_in(self._config.poll_interval_ms / 1000.0, self._on_tick)
        if self._pending_project is not None:
            self._loop.set_alarm_in(
                0.05, self._load_pending_project)
        if self._pending_restore_state is not None:
            self._loop.set_alarm_in(
                0.1, self._restore_pending_right_pane)
        try:
            self._loop.run()
        except KeyboardInterrupt:
            # Ctrl-C / SIGINT — fall through to teardown.
            pass
        finally:
            # Always clean up tmux, regardless of how we exited the loop.
            self._teardown_tmux()
