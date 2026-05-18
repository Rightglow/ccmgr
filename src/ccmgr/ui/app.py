"""Top-level urwid app: sidebar + status bar.

ccmgr runs in the left pane of a tmux window. The right pane hosts the
currently-selected claude session. Switching sessions in ccmgr respawns
the right pane with a new claude --resume. Press `i` for a session-info
popup.
"""
from __future__ import annotations

from pathlib import Path

import urwid

from ccmgr import tmux_ctl
from ccmgr.config import Config
from ccmgr.discovery import list_projects
from ccmgr.launcher import build_resume_command, build_new_session_command
from ccmgr.models import Project, SessionMeta
from ccmgr.session_cache import SessionCache
from ccmgr.ui.modals import HelpModal, NewProjectModal, ProjectInfoModal, QuitConfirmModal, SessionInfoModal
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
]


class App:
    def __init__(self, claude_home: Path, config: Config, auto_launched: bool = False) -> None:
        self._claude_home = claude_home
        self._config = config
        self._auto_launched = auto_launched
        self._status = StatusBar()
        self._selected_project: Project | None = None
        self._session_cache = SessionCache()
        # session_id (or "__new__-N" placeholder) -> detached tmux session name
        self._claude_tmux_sessions: dict[str, str] = {}
        # detached tmux session name -> human-readable label for the Running pane
        self._running_labels: dict[str, str] = {}
        self._new_session_counter: int = 0
        # The right pane in ccmgr's window; runs `tmux attach -t <claude_session>`.
        self._right_pane_id: str | None = None
        self._loop: urwid.MainLoop | None = None
        self._last_screen_size: tuple[int, int] | None = None

        projects = list_projects(claude_home)
        self._projects_pane = ProjectsPane(projects, on_select=self._on_project_select)
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            live_threshold=float(config.live_badge_seconds),
        )
        self._running_pane = RunningSessionsPane(on_select=self._on_running_select)

        self._sidebar = urwid.Pile([
            ("weight", 2, self._projects_pane),
            ("weight", 3, self._sessions_pane),
            ("weight", 1, self._running_pane),
        ])
        self._help_bar = HelpBar()
        footer = urwid.Pile([
            ("pack", self._help_bar),
            ("pack", self._status),
        ])
        self._frame = urwid.Frame(body=self._sidebar, footer=footer)
        # No auto-select — user must press Enter on a project to load its sessions.

    # --- project / session selection callbacks ---

    def _on_project_select(self, project: Project | None) -> None:
        if project is None:
            self._open_new_project_modal()
            return
        self._selected_project = project
        self._projects_pane.set_selected(project.encoded_name)
        sessions = self._session_cache.list_sessions(project)
        self._sessions_pane.set_sessions(project, sessions, running_ids=set(self._claude_tmux_sessions.keys()))
        self._status.set_message(f"Project: {project.real_path}  ({len(sessions)} sessions)")
        # Auto-focus the sessions pane below so the user can j/k into a session
        # without pressing Tab. Only do this when triggered by an actual Enter
        # press, not from the initial-project auto-select during App.__init__.
        if self._loop is not None:
            self._sidebar.focus_position = 1

    def _on_session_select(self, session: SessionMeta | None) -> None:
        if session is None:
            self._launch_new_session()
            return
        self._launch_resume(session)

    def _on_running_select(self, entry: RunningEntry) -> None:
        # Just re-attach the right pane to this already-running claude session.
        ok = self._attach_in_right_pane(entry.tmux_name)
        if ok:
            self._status.set_message(f"→ {entry.label}")
        else:
            self._status.set_message("failed to re-attach")

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

    def _attach_in_right_pane(self, claude_tmux_name: str) -> bool:
        """Make the right pane display the named claude tmux session.

        Either creates the right-pane split (first time) or respawns the existing
        right pane to attach to the new claude session. Either way the previous
        claude tmux session stays alive, detached.

        TMUX= prefix clears the env var so the nested `tmux attach` works; tmux
        otherwise refuses to attach from within another tmux session.
        """
        import shlex
        attach_cmd = f"TMUX= exec tmux attach-session -t {shlex.quote(claude_tmux_name)}"
        if self._right_pane_id and tmux_ctl.pane_alive(self._right_pane_id):
            ok = tmux_ctl.respawn_pane(self._right_pane_id, attach_cmd)
        else:
            new_id = tmux_ctl.split_window_h(attach_cmd)
            if not new_id:
                return False
            self._right_pane_id = new_id
            ok = True
        if ok and self._right_pane_id:
            tmux_ctl.select_pane(self._right_pane_id)
        return ok

    def _launch_resume(self, session_meta: SessionMeta) -> None:
        sid = session_meta.session_id
        claude_tmux_name = self._claude_tmux_sessions.get(sid) or self._claude_session_name(sid)
        cmd = build_resume_command(
            claude_binary=self._config.claude_binary,
            session_id=sid,
            cwd=session_meta.project.real_path,
        )
        shell_cmd = self._shellify(cmd, cwd=session_meta.project.real_path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached claude session")
            return
        self._claude_tmux_sessions[sid] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{session_meta.project.display_name}/{session_meta.display_title}"
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach to claude session")
            return
        self._status.set_message(f"→ {session_meta.display_title}  ({len(self._claude_tmux_sessions)} session(s) running)")

    def _launch_new_session(self) -> None:
        if self._selected_project is None:
            self._status.set_message("Pick a project first.")
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        claude_tmux_name = self._claude_session_name(placeholder)
        cmd = build_new_session_command(
            claude_binary=self._config.claude_binary,
            cwd=self._selected_project.real_path,
        )
        shell_cmd = self._shellify(cmd, cwd=self._selected_project.real_path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached session")
            return
        self._claude_tmux_sessions[placeholder] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{self._selected_project.display_name}/(new)"
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach")
            return
        self._status.set_message(f"→ new session in {self._selected_project.display_name}")

    def _on_new_project_submit(self, path: Path) -> None:
        self._close_modal()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._status.set_message(str(e))
            return
        self._new_session_counter += 1
        placeholder = f"__new__-{self._new_session_counter}"
        claude_tmux_name = self._claude_session_name(placeholder)
        cmd = [self._config.claude_binary]
        shell_cmd = self._shellify(cmd, cwd=path)
        if not self._ensure_detached_claude(claude_tmux_name, shell_cmd):
            self._status.set_message("failed to create detached session")
            return
        self._claude_tmux_sessions[placeholder] = claude_tmux_name
        self._running_labels[claude_tmux_name] = f"{path.name}/(new)"
        if not self._attach_in_right_pane(claude_tmux_name):
            self._status.set_message("failed to attach")
            return
        self._status.set_message(f"→ new project: {path}")

    @staticmethod
    def _shellify(argv: list[str], cwd: Path) -> str:
        import shlex
        quoted = " ".join(shlex.quote(a) for a in argv)
        return f"cd {shlex.quote(str(cwd))} && exec {quoted}"

    # --- modals ---

    def _open_new_project_modal(self) -> None:
        modal = NewProjectModal(on_submit=self._on_new_project_submit, on_cancel=self._close_modal)
        self._show_overlay(modal, width=50, height=30)

    def _open_info_modal(self) -> None:
        """Show info for whichever pane has focus: Project (top) or Session (bottom)."""
        if self._sidebar.focus_position == 0:
            proj = self._projects_pane.focused_project()
            modal = ProjectInfoModal(project=proj, on_close=self._close_modal)
            self._show_overlay(modal, width=60, height=40)
            return
        session = self._currently_focused_session_meta()
        running_label = None
        if session is not None:
            tmux_name = self._claude_tmux_sessions.get(session.session_id)
            if tmux_name and tmux_ctl.session_exists(tmux_name):
                running_label = f"detached as '{tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label, on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40)

    def _open_help_modal(self) -> None:
        modal = HelpModal(on_close=self._close_modal)
        self._show_overlay(modal, width=70, height=80)

    def _open_quit_confirm(self) -> None:
        modal = QuitConfirmModal(on_confirm=self._confirm_quit, on_cancel=self._close_modal)
        self._show_overlay(modal, width=50, height=30)

    # --- project shortcuts: editor + terminal ---

    def _active_project(self) -> Project | None:
        """Project to act on for c/t shortcuts.

        Prefer the focused project in the Projects pane; fall back to the
        currently-selected (loaded-into-Sessions) project.
        """
        if self._sidebar.focus_position == 0:
            focused = self._projects_pane.focused_project()
            if focused is not None:
                return focused
        return self._selected_project

    def _open_editor_for_active_project(self) -> None:
        import shutil
        import subprocess
        proj = self._active_project()
        if proj is None:
            self._status.set_message("no project focused/selected")
            return
        if shutil.which("code") is None:
            self._status.set_message("'code' not found on PATH (install VS Code)")
            return
        try:
            subprocess.Popen(
                ["code", str(proj.real_path)],
                cwd=str(proj.real_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._status.set_message(f"opened VS Code: {proj.real_path}")
        except OSError as e:
            self._status.set_message(f"failed to open code: {e}")

    def _open_terminal_for_active_project(self) -> None:
        import os
        import shlex
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
        tmux_ctl.select_pane(new_pane)
        self._status.set_message(f"terminal: {proj.display_name}  (Ctrl-B then arrow = move panes)")

    def _confirm_quit(self) -> None:
        self._close_modal()
        self._teardown_tmux()
        raise urwid.ExitMainLoop()

    def _show_overlay(self, modal: urwid.Widget, width: int, height: int) -> None:
        if self._loop is None:
            return
        overlay = urwid.Overlay(
            modal,
            self._frame,
            align="center", width=("relative", width),
            valign="middle", height=("relative", height),
        )
        self._loop.widget = overlay

    def _close_modal(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._frame

    # --- key handling ---

    def _on_input(self, key: str) -> None:
        if key == "ctrl c":
            self._open_quit_confirm()
            return
        if key in ("q", "Q"):
            self._teardown_tmux()
            raise urwid.ExitMainLoop()
        if key in ("tab", "shift tab"):
            self._rotate_focus(reverse=(key == "shift tab"))
            return
        if key == "/":
            self._enter_filter_mode()
            return
        if key in ("i", "I"):
            self._open_info_modal()
            return
        if key == "?":
            self._open_help_modal()
            return
        if key in ("c", "C"):
            self._open_editor_for_active_project()
            return
        if key in ("t", "T"):
            self._open_terminal_for_active_project()
            return

    def _rotate_focus(self, reverse: bool = False) -> None:
        n = len(self._sidebar.contents)
        if n <= 1:
            return
        cur = self._sidebar.focus_position
        self._sidebar.focus_position = (cur - 1) % n if reverse else (cur + 1) % n

    def _teardown_tmux(self) -> None:
        """Clean up on quit: kill right pane, every detached claude session, and our own session if we own it."""
        if self._right_pane_id:
            try:
                tmux_ctl.kill_pane(self._right_pane_id)
            except Exception:
                pass
            self._right_pane_id = None
        for _, name in list(self._claude_tmux_sessions.items()):
            try:
                tmux_ctl.kill_session(name)
            except Exception:
                pass
        self._claude_tmux_sessions.clear()
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
        projects = list_projects(self._claude_home)
        self._projects_pane.set_projects(projects)
        # Prune dead claude tmux sessions (e.g. claude exited via /quit).
        for sid in list(self._claude_tmux_sessions.keys()):
            if not tmux_ctl.session_exists(self._claude_tmux_sessions[sid]):
                tmux_name = self._claude_tmux_sessions.pop(sid)
                self._running_labels.pop(tmux_name, None)
        running_ids = set(self._claude_tmux_sessions.keys())
        if self._selected_project is not None:
            matched = next((p for p in projects if p.encoded_name == self._selected_project.encoded_name), None)
            if matched is not None:
                self._selected_project = matched
                sessions = self._session_cache.list_sessions(matched)
                self._sessions_pane.set_sessions(matched, sessions, running_ids=running_ids)
            else:
                self._selected_project = None
                self._sessions_pane.set_sessions(None, [], running_ids=running_ids)

        # Populate the bottom "Running" pane.
        entries = [
            RunningEntry(tmux_name=name, label=self._running_labels.get(name, name))
            for name in self._claude_tmux_sessions.values()
        ]
        self._running_pane.set_running(entries)

        if self._claude_tmux_sessions:
            n = len(self._claude_tmux_sessions)
            self._status.set_message(f"● = running ({n}) · Enter on a row re-attaches it · Ctrl-B ← = ccmgr")

    def _currently_focused_session_meta(self) -> SessionMeta | None:
        if not self._sessions_pane._walker:
            return None
        focus_w, _ = self._sessions_pane._walker.get_focus()
        from ccmgr.ui.sessions_pane import _SessionRow
        if isinstance(focus_w, _SessionRow):
            return focus_w.session
        return None

    def _on_tick(self, loop, _user_data) -> None:
        self._refresh()
        loop.set_alarm_in(self._config.poll_interval_ms / 1000.0, self._on_tick)

    # --- lifecycle ---

    def run(self) -> None:
        self._loop = urwid.MainLoop(self._frame, palette=PALETTE, unhandled_input=self._on_input)
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
