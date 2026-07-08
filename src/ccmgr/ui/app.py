"""Top-level urwid app: sidebar + status bar.

ccmgr runs in the left pane of a tmux window. The right pane hosts the
currently-selected claude session. Switching sessions in ccmgr respawns
the right pane with a new claude --resume. Press `i` for a session-info
popup.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import urwid

from ccmgr import tmux_ctl
from ccmgr.config import Config
from ccmgr.discovery import list_projects
from ccmgr.favorites import Favorites
from ccmgr.launcher import build_resume_command, build_new_session_command
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
from ccmgr.ui.statusbar import HelpBar, StatusBar


PALETTE = [
    ("statusbar", "yellow,bold", "default"),
    # Focus highlight: bold black on brown (amber) — warm, much easier on the
    # eyes than the previous bright-white "light gray" background.
    ("focus", "black,bold", "brown"),
    # Persistent "currently-active project" highlight, shown even when focus
    # is elsewhere. Cool tone so it doesn't compete with the warm focus color.
    ("selected", "black,bold", "dark cyan"),
    ("title", "white,bold", ""),
    ("dim", "dark gray", ""),
    ("live", "light green,bold", ""),
    ("live_tag", "yellow,bold", ""),
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
    # Help-hint buttons in the trailing bar.
    ("help_btn", "light gray", "dark gray"),
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


@dataclass
class _RightPaneState:
    """What to restore when exiting history-preview mode."""
    kind: str  # "empty" | "claude"
    tmux_name: str | None = None  # for "claude"


class App:
    def __init__(self, claude_home: Path, config: Config,
                 auto_launched: bool = False,
                 scroll_coalescing: bool = True) -> None:
        self._claude_home = claude_home
        self._config = config
        self._auto_launched = auto_launched
        self._status = StatusBar()
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
        self._last_screen_size: tuple[int, int] | None = None
        self._help_right_was_open: bool = False
        self._help_saved_width: str = ""
        # History-preview mode: when the right pane shows a session transcript
        # (less) instead of a Claude session.  We remember what was there before
        # so we can restore it when the user exits less.
        self._in_history_mode: bool = False
        self._restore_state: _RightPaneState | None = None
        self._right_pane_claude: str | None = None  # tmux_name of claude session in right pane
        self._ccmgr_pane_id: str | None = None  # set in run()
        self._has_less: bool = shutil.which("less") is not None
        self._less_mouse_flag: str = self._detect_less_mouse()
        self._scroll_manager = ScrollManager(enabled=scroll_coalescing)

        projects = list_projects(claude_home)
        self._projects_pane = ProjectsPane(projects, on_select=self._on_project_select,
                                           on_double_click=self._on_project_double_click)
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            live_threshold=float(config.live_badge_seconds),
            on_preview=self._on_session_preview,
            on_context=self._open_session_context_menu,
        )
        self._running_pane = RunningSessionsPane(
            on_select=self._on_running_select,
            on_context=self._on_running_context_menu,
        )
        # Warn early if dependencies are missing so the user doesn't
        # discover it by getting a cryptic error in the right pane.
        if not tmux_ctl.has_tmux():
            self._status.set_message(
                "ERROR: tmux not found on PATH — ccmgr cannot run without tmux")
        elif not shutil.which(self._config.claude_binary):
            self._status.set_message(
                f"WARNING: '{self._config.claude_binary}' not on PATH — sessions cannot launch")

        # Wrap each pane in AttrMap so its LineBox border highlights when
        # focused. The `pane`/`pane_focus` palette entries color only cells
        # with no explicit attribute (the border chars) — inner rows have
        # their own AttrMaps and are unaffected.
        self._sidebar = urwid.Pile([
            ("weight", 2, urwid.AttrMap(self._projects_pane, "pane", focus_map="pane_focus")),
            ("weight", 3, urwid.AttrMap(self._sessions_pane, "pane", focus_map="pane_focus")),
            ("weight", 1, urwid.AttrMap(self._running_pane, "pane", focus_map="pane_focus")),
        ])
        self._help_bar = HelpBar(
            on_help=self._open_help_modal,
            on_quit=self._open_quit_confirm,
            on_detach=self._on_detach,
        )
        footer = urwid.Pile([
            ("pack", self._help_bar),
            ("pack", self._status),
        ])
        self._frame = urwid.Frame(body=self._sidebar, footer=footer)
        # Auto-select the most recent project on startup.
        if projects:
            self._on_project_select(projects[0])

    # --- project / session selection callbacks ---

    def _on_project_select(self, project: Project | None) -> None:
        """Single-click / initial auto-select: show sessions, keep focus here."""
        if project is None:
            self._open_new_project_modal()
            return
        self._selected_project = project
        self._projects_pane.set_selected(project.encoded_name)
        sessions = self._session_cache.list_sessions(project)
        self._sessions_pane.set_sessions(project, sessions, running_ids=set(self._running),
                favorite_ids=self._favorites.get_ids())
        self._status.set_message(f"Project: {project.real_path}  ({len(sessions)} sessions)")

    def _on_project_double_click(self, project: Project | None) -> None:
        """Double-click / Enter on a project: show sessions AND move focus to them."""
        if project is None:
            self._open_new_project_modal()
            return
        self._on_project_select(project)
        if self._loop is not None:
            self._sidebar.focus_position = 1

    def _on_session_select(self, session: SessionMeta | None,
                            steal_focus: bool = True) -> None:
        # Opening a real session (or creating a new one) — clear any
        # history-preview state so the launch takes over the right pane.
        self._in_history_mode = False
        self._restore_state = None
        if session is None:
            self._launch_new_session()
            return
        self._launch_resume(session, steal_focus=steal_focus)

    def _on_running_select(self, entry: RunningEntry,
                            steal_focus: bool = True) -> None:
        # Re-attach the right pane to this already-running claude session AND
        # sync the Projects/Sessions panes to that session's project, so the
        # sidebar reflects what's actually showing on the right.
        self._in_history_mode = False
        self._restore_state = None
        ok = self._attach_in_right_pane(entry.tmux_name, steal_focus=steal_focus)
        if not ok:
            self._status.set_message("failed to re-attach")
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
        self._status.set_message(f"→ {entry.label}")

    # --- history preview (right pane shows transcript via less, not a Claude session) ---

    def _on_session_preview(self, session: SessionMeta) -> None:
        """Show session history in the right pane without launching Claude.

        Called by ``ClickableRow`` after the double-click window has
        passed, so this only fires on a genuine single-click.
        """
        if not self._has_less:
            self._status.set_message("'less' not installed — cannot preview history")
            return
        if not self._in_history_mode:
            self._save_restore_state()
        if self._show_transcript(session.jsonl_path):
            self._in_history_mode = True

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
        cmd = f"{_sys.executable} -m ccmgr.transcript {shlex.quote(str(jsonl_path))} | less -R +G {mouse}"
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            if not tmux_ctl.respawn_pane(self._right_pane_id, cmd):
                self._status.set_message("failed to respawn right pane for transcript")
                return False
        else:
            new_id = tmux_ctl.split_window_h(cmd, size_percent=70)
            if not new_id:
                self._status.set_message("failed to create right pane for transcript")
                return False
            self._right_pane_id = new_id
            tmux_ctl.set_window_option("pane-border-style", "fg=colour240")
            tmux_ctl.set_window_option("pane-active-border-style", "fg=cyan,bold")
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
        self._configure_scroll_acceleration(claude_tmux_name)
        # Fast path: the right pane is already showing this session.  Skip the
        # expensive respawn and just optionally move focus.  This also prevents
        # focus flicker on double-click (the first click's respawn kills and
        # restarts tmux attach, which briefly shifts focus).
        if (self._right_pane_id is not None
                and self._right_pane_claude == claude_tmux_name
                and tmux_ctl.pane_alive(self._right_pane_id)):
            if steal_focus:
                tmux_ctl.select_pane(self._right_pane_id)
            return True

        attach_cmd = f"TMUX= exec tmux attach-session -t {shlex.quote(claude_tmux_name)}"
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            ok = tmux_ctl.respawn_pane(self._right_pane_id, attach_cmd)
        else:
            # ccmgr (left) at 30%, claude (right, the new pane) at 70%.
            new_id = tmux_ctl.split_window_h(attach_cmd, size_percent=70)
            if not new_id:
                return False
            self._right_pane_id = new_id
            # tmux only draws pane borders once there are 2+ panes. We just
            # created the second one, so now is the moment to apply the
            # active/inactive border palette — matches the bright-cyan focus
            # highlight ccmgr uses on its own urwid panes.
            tmux_ctl.set_window_option("pane-border-style", "fg=colour240")
            tmux_ctl.set_window_option("pane-active-border-style", "fg=cyan,bold")
            ok = True
        if ok and self._right_pane_id and steal_focus:
            tmux_ctl.select_pane(self._right_pane_id)
        if ok:
            self._right_pane_claude = claude_tmux_name
            self._install_fullscreen_binding()
        return ok

    def _install_fullscreen_binding(self) -> None:
        """(Re)bind F3 to fullscreen-toggle the *claude* (right) pane.

        Unlike tmux's built-in ``Ctrl-B z`` — which zooms whichever pane is
        active and can therefore fullscreen the ccmgr sidebar by mistake — this
        targets the right pane's current id explicitly, so F3 always zooms
        Claude regardless of focus. Rebound whenever the right pane is
        (re)created because its id changes. Copy workflow: F3 → Shift-drag to
        select → Cmd/Ctrl+C → F3 to exit.
        """
        if not self._right_pane_id:
            return
        import subprocess as _sp
        _sp.run(
            ["tmux", "bind-key", "-n", "F3", "resize-pane", "-Z", "-t", self._right_pane_id],
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
                *, steal_focus: bool = True) -> bool:
        """Create (or reuse) the detached claude tmux session for `key`,
        register it, and attach it in the right pane. Returns success.

        Shared by resume / new-session / new-project so the tracking bookkeeping
        lives in exactly one place.
        """
        existing = self._running.get(key)
        tmux_name = existing.tmux_name if existing else self._claude_session_name(key)
        if not self._ensure_detached_claude(tmux_name, self._shellify(cmd, cwd=cwd)):
            self._status.set_message("failed to create detached claude session")
            return False
        self._running[key] = _Running(
            key=key, tmux_name=tmux_name, label=label, project=project,
            placeholder_path=placeholder_path,
            created_at=time.time() if placeholder_path is not None else 0.0,
        )
        if not self._attach_in_right_pane(tmux_name, steal_focus=steal_focus):
            self._status.set_message("failed to attach to claude session")
            return False
        return True

    def _launch_resume(self, session_meta: SessionMeta,
                        *, steal_focus: bool = True) -> None:
        cmd = build_resume_command(
            claude_binary=self._config.claude_binary,
            session_id=session_meta.session_id,
            cwd=session_meta.project.real_path,
        )
        label = f"{session_meta.project.display_name}/{session_meta.display_title}"
        if self._launch(session_meta.session_id, cmd, session_meta.project.real_path,
                        label, session_meta.project, steal_focus=steal_focus):
            self._status.set_message(
                f"→ {session_meta.display_title}  ({len(self._running)} session(s) running)")

    def _launch_new_session(self) -> None:
        if self._selected_project is None:
            self._status.set_message("Pick a project first.")
            return
        proj = self._selected_project
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        cmd = build_new_session_command(claude_binary=self._config.claude_binary, cwd=proj.real_path)
        if self._launch(placeholder, cmd, proj.real_path, f"{proj.display_name}/(new)",
                        proj, placeholder_path=proj.real_path):
            self._status.set_message(f"→ new session in {proj.display_name}")

    def _on_new_project_submit(self, path: Path) -> None:
        self._close_modal()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._status.set_message(str(e))
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        cmd = [self._config.claude_binary]
        if self._launch(placeholder, cmd, path, f"{path.name}/(new)",
                        None, placeholder_path=path):
            self._status.set_message(f"→ new project: {path}")

    @staticmethod
    def _shellify(argv: list[str], cwd: Path) -> str:
        import shlex
        quoted = " ".join(shlex.quote(a) for a in argv)
        return f"cd {shlex.quote(str(cwd))} && exec {quoted}"

    # --- modals ---

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
        # Temporarily shrink the right pane so help gets full terminal width.
        # Claude keeps running — it's in a detached tmux session.
        self._help_right_was_open = (
            self._right_pane_id is not None
            and tmux_ctl.pane_alive(self._right_pane_id)
        )
        if self._help_right_was_open:
            import subprocess as _sp
            # Save current width so we can restore it exactly.
            saved = _sp.check_output(
                ["tmux", "display-message", "-p", "-t", self._right_pane_id,
                 "-F", "#{pane_width}"],
                stderr=_sp.DEVNULL,
            )
            self._help_saved_width = saved.decode().strip()
            _sp.run(
                ["tmux", "resize-pane", "-t", self._right_pane_id, "-x", "1"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        modal = HelpModal(on_close=self._close_help_modal)
        self._show_overlay(modal, width=60, height=80,
                           click_outside_to_close=True,
                           on_click_outside=self._close_help_modal)

    def _close_help_modal(self) -> None:
        self._close_modal()
        if self._help_right_was_open and self._right_pane_id:
            import subprocess as _sp
            _sp.run(
                ["tmux", "resize-pane", "-t", self._right_pane_id, "-x",
                 self._help_saved_width],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
        self._help_right_was_open = False
        self._help_saved_width = ""

    def _open_quit_confirm(self) -> None:
        modal = QuitConfirmModal(
            on_confirm=self._confirm_quit,
            on_cancel=self._close_modal,
            running_count=len(self._running),
        )
        self._show_overlay(modal, width=50, height=30)

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
            self._status.set_message("no project focused/selected")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(proj.real_path))} && exec {shlex.quote(shell)}"
        # Split visibly in the same window: if a right pane (claude) exists,
        # put the terminal below it; otherwise split off the current pane.
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._status.set_message("failed to split for terminal")
            return
        # Auto-close the pane when the shell exits (default, but be explicit).
        _sp.run(
            ["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        tmux_ctl.select_pane(new_pane)
        self._status.set_message(f"terminal: {proj.display_name}  (Ctrl-B then arrow = move panes)")

    def _on_detach(self) -> None:
        """Detach from the ccmgr tmux session (keep all Claude sessions alive)."""
        import subprocess as _sp
        _sp.run(["tmux", "detach-client"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

    def _confirm_quit(self) -> None:
        self._close_modal()
        self._teardown_tmux()
        raise urwid.ExitMainLoop()

    def _show_overlay(self, modal: urwid.Widget, width: int, height: int,
                       *, click_outside_to_close: bool = False,
                       fixed_width: bool = False,
                       fixed_height: bool = False,
                       on_click_outside: Callable[[], None] | None = None) -> None:
        if self._loop is None:
            return
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
        # Keep tmux focus on ccmgr's pane so the next keystroke doesn't
        # accidentally land in Claude (can happen after mouse clicks).
        pane_id = tmux_ctl.current_pane_id()
        if pane_id:
            tmux_ctl.select_pane(pane_id)

    # --- key handling ---

    def _on_input(self, key: str) -> None:
        if key == "esc":
            # Esc navigates "up" the pane hierarchy:
            #   Running → Sessions → Projects
            if self._sidebar.focus_position == 2:
                self._sidebar.focus_position = 1
                return
            if self._sidebar.focus_position == 1:
                self._sidebar.focus_position = 0
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
                self._status.set_message("No filter on Running pane.")
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

    def _teardown_tmux(self) -> None:
        """Clean up on quit: kill right pane, every detached claude session, and our own session if we own it."""
        self._teardown_scroll_acceleration()
        # Remove the F3 fullscreen binding we installed (it's server-global).
        try:
            import subprocess as _sp
            _sp.run(["tmux", "unbind-key", "-n", "F3"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        if self._right_pane_id:
            try:
                tmux_ctl.kill_pane(self._right_pane_id)
            except Exception:
                pass
            self._right_pane_id = None
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
        # Swap just the status row (second item in the footer Pile) for an
        # Edit widget; the help-hint row above it stays visible.
        edit = urwid.Edit(caption="filter: ")
        footer_pile = self._frame.contents["footer"][0]
        footer_pile.contents[1] = (edit, footer_pile.options("pack"))
        footer_pile.focus_position = 1
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
                footer_pile.contents[1] = (self._status, footer_pile.options("pack"))
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
        projects = list_projects(self._claude_home)
        self._projects_pane.set_projects(projects)
        # Prune dead claude tmux sessions (e.g. claude exited via /quit).
        for key in list(self._running):
            if not tmux_ctl.session_exists(self._running[key].tmux_name):
                del self._running[key]

        # If the Claude session we were showing in the right pane has exited,
        # kill the right pane so the TUI returns to full-screen.  Must happen
        # before the pane_alive→restore check so we never try to restore a
        # dead session into a zombie pane.
        if self._right_pane_id and self._right_pane_claude:
            if not tmux_ctl.session_exists(self._right_pane_claude):
                tmux_ctl.kill_pane(self._right_pane_id)
                self._right_pane_id = None
                self._right_pane_claude = None

        # Detect when the right pane was closed (user pressed q in less, the
        # pane was cleaned up above, or it was killed externally).  In history
        # mode, restore whatever was there before; otherwise just clear our
        # tracking.
        if self._right_pane_id and not tmux_ctl.pane_alive(self._right_pane_id):
            if self._in_history_mode:
                self._in_history_mode = False
                self._restore_from_history_mode()
            else:
                self._right_pane_id = None
                self._right_pane_claude = None

        # Promote any `__new__-N` placeholders to their real session_id +
        # display title once claude has written the jsonl on disk.
        self._resolve_placeholders(projects)
        running_ids = set(self._running)
        if self._selected_project is not None:
            matched = next((p for p in projects if p.encoded_name == self._selected_project.encoded_name), None)
            if matched is not None:
                self._selected_project = matched
                sessions = self._session_cache.list_sessions(matched)
                self._sessions_pane.set_sessions(matched, sessions, running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())
            else:
                self._selected_project = None
                self._sessions_pane.set_sessions(None, [], running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())

        # Populate the bottom "Running" pane. Sync labels first so AI-
        # generated titles and renames appear without restarting ccmgr.
        for r in self._running.values():
            if r.is_placeholder or r.project is None:
                continue
            s = self._find_session_meta(r.key, r.project)
            if s is not None and s.title:
                r.label = f"{s.project.display_name}/{s.display_title}"
            if s is not None:
                r.status = s.status
        entries = [
            RunningEntry(tmux_name=r.tmux_name, label=r.label,
                         status=r.status)
            for r in self._running.values()
        ]
        self._running_pane.set_running(entries)

        if self._running:
            self._status.set_message("Ctrl-B ← returns focus to ccmgr")

    def _resolve_placeholders(self, projects: list[Project]) -> None:
        """Re-key any `__new__-N` placeholder to its real session_id.

        For each live placeholder, look at its project's sessions and pick the
        newest one created after the placeholder timestamp whose session_id is
        not already claimed by another running session.
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
                self._status.set_message("No running session selected.")
                return
            focus_w, _ = self._running_pane._walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._status.set_message("No running session selected.")
                return
            r = self._by_tmux(focus_w.entry.tmux_name)
            if r is None:
                self._status.set_message("Session not found in registry.")
                return
            if not tmux_ctl.session_exists(r.tmux_name):
                self._status.set_message(f"tmux session already gone: {r.tmux_name}")
                return
            tmux_ctl.kill_session(r.tmux_name)
            del self._running[r.key]
            self._status.set_message(f"Killed: {r.label}  (file kept)")
            return

        # Sessions pane (pos 1 or default).
        session = self._currently_focused_session_meta()
        if session is None:
            self._status.set_message("No session selected.")
            return
        r = self._running.get(session.session_id)
        if r is None:
            self._status.set_message(f"'{session.display_title}' is not running.")
            return
        if not tmux_ctl.session_exists(r.tmux_name):
            self._status.set_message(f"tmux session already gone: {r.tmux_name}")
            return
        tmux_ctl.kill_session(r.tmux_name)
        del self._running[session.session_id]
        self._status.set_message(f"Killed: {session.display_title}  (file kept)")

    def _on_delete_session(self) -> None:
        """Delete the focused session from the current pane (with confirmation)."""
        pos = self._sidebar.focus_position

        if pos == 1:
            # Sessions pane — delete the focused session (JSONL + tmux).
            session = self._currently_focused_session_meta()
            if session is None:
                self._status.set_message("No session selected to delete.")
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
                self._status.set_message("No running session selected.")
                return
            focus_w, _ = running_walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._status.set_message("No running session selected.")
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
            self._status.set_message("Use d on a session row or running-entry row to delete.")

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
        self._refresh()

        self._status.set_message(f"Deleted: {label}")

    @staticmethod
    def _remove_from_history(session_id: str) -> None:
        """Strip every line referencing *session_id* from ~/.claude/history.jsonl.

        Claude Code uses this file as a session index — when a JSONL is deleted
        but the history entry remains, Claude rebuilds an empty metadata stub on
        the next launch.  Removing the entry prevents that.
        """
        history_path = Path.home() / ".claude" / "history.jsonl"
        if not history_path.is_file():
            return
        try:
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
                history_path.write_text("\n".join(kept) + ("\n" if kept else ""))
            except OSError:
                pass

    # --- rename session ---

    def _on_rename_session(self) -> None:
        """Open the rename modal for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._status.set_message("No session selected to rename.")
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
            self._status.set_message(f"Failed to rename: {e}")
            return
        # Invalidate the cache and refresh so both Sessions and Running
        # panes pick up the new title immediately.
        self._session_cache.invalidate()
        self._refresh()
        self._status.set_message(f"Renamed to: {new_title}")

    # --- toggle favorite ---

    def _on_toggle_star(self) -> None:
        """Toggle star status for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._status.set_message("No session selected.")
            return
        now_star = self._favorites.toggle(session.session_id)
        label = "★" if now_star else "unstarred"
        self._status.set_message(f"{label} {session.display_title}")

    # --- context menu (right-click) ---

    def _on_running_context_menu(self, entry: RunningEntry) -> None:
        r = self._by_tmux(entry.tmux_name)
        if r is None or r.is_placeholder or r.project is None:
            return
        session = self._find_session_meta(r.key, r.project)
        if session is None:
            return
        self._running_pane.set_selected(entry.tmux_name)
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
        self._status.set_message(f"{label} {session.display_title}")

    def _do_context_kill(self, session: SessionMeta) -> None:
        r = self._running.get(session.session_id)
        if r and tmux_ctl.session_exists(r.tmux_name):
            tmux_ctl.kill_session(r.tmux_name)
        self._running.pop(session.session_id, None)
        self._status.set_message(f"Killed: {session.display_title}  (file kept)")

    def _do_context_term(self, session: SessionMeta) -> None:
        import os
        import shlex
        import subprocess as _sp
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(session.project.real_path))} && exec {shlex.quote(shell)}"
        target = self._right_pane_id if (self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._status.set_message("failed to split for terminal")
            return
        _sp.run(["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        tmux_ctl.select_pane(new_pane)
        self._status.set_message(f"terminal: {session.project.display_name}")

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
            self._status.set_message("No claude pane to resize against.")
            return
        direction = "-R" if expand_ccmgr else "-L"
        tmux_ctl.resize_pane(self._right_pane_id, direction, 5)

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
        self._loop = urwid.MainLoop(self._frame, palette=PALETTE, unhandled_input=self._on_input)
        from ccmgr.ui._widgets import ClickableRow
        ClickableRow._main_loop = self._loop
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
        try:
            self._loop.run()
        except KeyboardInterrupt:
            # Ctrl-C / SIGINT — fall through to teardown.
            pass
        finally:
            # Always clean up tmux, regardless of how we exited the loop.
            self._teardown_tmux()
