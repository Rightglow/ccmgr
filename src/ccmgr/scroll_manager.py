"""Lifecycle and recovery for SSH scroll coalescing.

tmux key tables are server-global. This manager therefore serializes ownership
per tmux server, persists enough state to recover after an ungraceful ccmgr
exit, and refuses to enable when the user has customized wheel bindings.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from ccmgr import tmux_ctl


class ScrollManager:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._lock_fd: int | None = None
        self._state_path: Path | None = None
        self._agent_session: str | None = None
        self._agent_pane: str | None = None
        self._bindings_backup: tmux_ctl.ScrollBindingBackup | None = None
        self._target_windows: set[str] = set()
        self._active_session: str | None = None
        self._recovered_state = False

    @staticmethod
    def _server_key() -> str:
        socket_path = os.environ.get("TMUX", "default").split(",", 1)[0]
        return hashlib.sha256(socket_path.encode()).hexdigest()[:16]

    def _acquire(self) -> bool:
        if self._lock_fd is not None:
            return True
        key = self._server_key()
        prefix = f"ccmgr-scroll-{os.getuid()}-{key}"
        lock_path = Path(tempfile.gettempdir()) / f"{prefix}.lock"
        state_path = Path(tempfile.gettempdir()) / f"{prefix}.json"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._lock_fd = fd
        self._state_path = state_path
        return True

    @staticmethod
    def _decode_backup(raw: list[list[str | None]]) -> tmux_ctl.ScrollBindingBackup:
        return {(str(table), str(key)): binding for table, key, binding in raw}

    @staticmethod
    def _encode_backup(backup: tmux_ctl.ScrollBindingBackup) -> list[list[str | None]]:
        return [[table, key, binding] for (table, key), binding in backup.items()]

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._agent_session = data["agent_session"]
            self._agent_pane = data["agent_pane"]
            self._bindings_backup = self._decode_backup(data["bindings_backup"])
            self._target_windows = set(data.get("target_windows", []))
            self._recovered_state = True
        except (OSError, KeyError, TypeError, ValueError):
            self._agent_session = None
            self._agent_pane = None
            self._bindings_backup = None
            self._target_windows.clear()
            self._recovered_state = False

    def _save_state(self) -> bool:
        if not self._state_path or not self._bindings_backup:
            return False
        data = {
            "owner_pid": os.getpid(),
            "agent_session": self._agent_session,
            "agent_pane": self._agent_pane,
            "bindings_backup": self._encode_backup(self._bindings_backup),
            "target_windows": sorted(self._target_windows),
        }
        tmp: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=self._state_path.name + ".", dir=self._state_path.parent)
            tmp = Path(raw_path)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._state_path)
            return True
        except OSError:
            if tmp:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def _remove_state(self) -> None:
        if self._state_path:
            try:
                self._state_path.unlink()
            except OSError:
                pass

    def _start_agent(self, target_pane: str) -> bool:
        if self._agent_session:
            tmux_ctl.kill_session(self._agent_session)
        self._agent_session = f"ccmgr-scroll-{os.getpid()}"
        if tmux_ctl.session_exists(self._agent_session):
            tmux_ctl.kill_session(self._agent_session)
        lines_per_event = tmux_ctl.scroll_lines_per_event(
            self._bindings_backup or {})
        cmd = (
            f"{shlex.quote(sys.executable)} -m ccmgr.scroll_agent "
            f"--target {shlex.quote(target_pane)} "
            f"--lines-per-event {lines_per_event} "
            f"--ready-session {shlex.quote(self._agent_session)}"
        )
        self._agent_pane = tmux_ctl.start_scroll_agent(self._agent_session, cmd)
        if self._agent_pane is None:
            return False
        return tmux_ctl.wait_window_user_option(
            self._agent_session, "@ccmgr_scroll_ready", "1")

    def configure(self, claude_tmux_name: str) -> bool:
        """Enable or retarget coalescing for a detached Claude session."""
        if not self.enabled or not self._acquire():
            return False
        if self._bindings_backup is None:
            self._load_state()

        target_pane = tmux_ctl.session_pane_id(claude_tmux_name)
        if not target_pane:
            return False

        agent_alive = bool(self._agent_pane and tmux_ctl.pane_alive(self._agent_pane))
        if agent_alive and self._recovered_state:
            assert self._agent_pane is not None
            assert self._bindings_backup is not None
            # The prior process may have died after persisting recovery state
            # but immediately before installing the wrappers. Reapply them
            # idempotently whenever state is adopted.
            if not tmux_ctl.rebind_scroll_agent(
                    self._agent_pane, self._bindings_backup):
                return False
            self._recovered_state = False
        if not agent_alive:
            prepared_now = False
            if self._bindings_backup is None:
                backup = tmux_ctl.prepare_scroll_bindings()
                if backup is None:
                    return False
                self._bindings_backup = backup
                prepared_now = True
            if not self._start_agent(target_pane):
                if prepared_now:
                    self._bindings_backup = None
                return False
            assert self._agent_pane is not None
            if prepared_now:
                # Persist recovery data before touching the server-global key
                # tables. A replacement ccmgr can now recover at every point.
                if not self._save_state():
                    tmux_ctl.kill_session(self._agent_session or "")
                    self._agent_session = self._agent_pane = None
                    self._bindings_backup = None
                    return False
            if not tmux_ctl.rebind_scroll_agent(
                    self._agent_pane, self._bindings_backup):
                if self._bindings_backup is not None:
                    tmux_ctl.restore_scroll_bindings(self._bindings_backup)
                tmux_ctl.kill_session(self._agent_session or "")
                self._remove_state()
                self._agent_session = self._agent_pane = None
                self._bindings_backup = None
                return False
        assert self._agent_pane is not None
        # Retarget first. A failed control message must not leave a pane marked
        # as accelerated while the agent is still operating on another pane.
        if not tmux_ctl.set_scroll_agent_target(self._agent_pane, target_pane):
            return False
        self._target_windows.add(claude_tmux_name)
        self._active_session = claude_tmux_name
        # Persist the intended marker before creating it. A crash can now only
        # leave an extra cleanup entry, never an untracked active marker.
        if not self._save_state():
            self._target_windows.discard(claude_tmux_name)
            self._active_session = None
            return False
        if not tmux_ctl.set_window_user_option(
                claude_tmux_name, "@ccmgr_scroll_agent", "1"):
            self._target_windows.discard(claude_tmux_name)
            self._active_session = None
            self._save_state()
            return False
        return True

    def maintain(self) -> None:
        """Recreate a failed agent while the same Claude session is visible."""
        if (self.enabled and self._active_session and self._lock_fd is not None
                and (not self._agent_pane or not tmux_ctl.pane_alive(self._agent_pane))):
            self.configure(self._active_session)

    def close(self) -> None:
        """Restore tmux state and release this server's ownership lock."""
        if self._lock_fd is None:
            return
        for target_window in self._target_windows:
            tmux_ctl.set_window_user_option(
                target_window, "@ccmgr_scroll_agent", None)
        if (self._bindings_backup is not None and self._agent_pane
                and tmux_ctl.scroll_bindings_owned_by(self._agent_pane)):
            tmux_ctl.restore_scroll_bindings(self._bindings_backup)
        if self._agent_session:
            tmux_ctl.kill_session(self._agent_session)
        self._remove_state()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None
        self._agent_session = self._agent_pane = None
        self._bindings_backup = None
        self._target_windows.clear()
        self._active_session = None
