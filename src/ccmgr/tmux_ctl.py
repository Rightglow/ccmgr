"""Thin wrappers around the tmux CLI.

ccmgr uses tmux to host the claude side-pane. All tmux interaction goes
through this module so error handling and command shape stay consistent.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def has_tmux() -> bool:
    """True if tmux is installed and on PATH."""
    return shutil.which("tmux") is not None


def in_tmux() -> bool:
    """True if the current process is running inside tmux."""
    return os.environ.get("TMUX") is not None


def current_pane_id() -> str | None:
    """Return the tmux pane id of the current process, or None."""
    if not in_tmux():
        return None
    try:
        out = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def current_session_name() -> str | None:
    """Return the name of the tmux session we're in, or None."""
    if not in_tmux():
        return None
    try:
        out = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_name}"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def list_panes() -> list[str]:
    """Return list of pane ids in the current window."""
    if not in_tmux():
        return []
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL,
        )
        text = out.decode().strip()
        return text.splitlines() if text else []
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def split_window_h(cmd: str = "", target: str | None = None, size_percent: int | None = None) -> str | None:
    """Create a horizontal split (new pane to the right). Returns the new pane id.

    `size_percent` sets the new (right) pane's width as a percentage of the
    parent pane. E.g. size_percent=70 → ccmgr on the left at 30%, claude
    pane on the right at 70%.
    """
    if not in_tmux():
        return None
    args = ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"]
    if size_percent is not None:
        args.extend(["-l", f"{size_percent}%"])
    if target:
        args.extend(["-t", target])
    if cmd:
        args.append(cmd)
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def split_window_v(cmd: str = "", target: str | None = None) -> str | None:
    """Create a vertical split (new pane below). Returns the new pane id."""
    if not in_tmux():
        return None
    args = ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}"]
    if target:
        args.extend(["-t", target])
    if cmd:
        args.append(cmd)
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def respawn_pane(pane_id: str, cmd: str) -> bool:
    """Replace the running command in `pane_id` with `cmd`. -k kills any existing process."""
    try:
        subprocess.check_call(
            ["tmux", "respawn-pane", "-k", "-t", pane_id, cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def kill_pane(pane_id: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "kill-pane", "-t", pane_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def set_window_option(name: str, value: str) -> bool:
    """`tmux set-window-option -w <name> <value>` scoped to the current window.

    Used by ccmgr to enable a visible border highlight on the active tmux
    pane (so the claude pane lights up the same way ccmgr's urwid panes do
    when focused) without leaking the setting into the user's other windows.
    """
    if not in_tmux():
        return False
    try:
        subprocess.check_call(
            ["tmux", "set-window-option", name, value],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def select_pane(pane_id: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "select-pane", "-t", pane_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resize_pane(pane_id: str, direction: str, amount: int) -> bool:
    """Resize a pane. direction is -L/-R/-U/-D (tmux flags)."""
    try:
        subprocess.check_call(
            ["tmux", "resize-pane", "-t", pane_id, direction, str(amount)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def pane_alive(pane_id: str) -> bool:
    """True if the pane id still exists in any window."""
    if not in_tmux():
        return False
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL,
        )
        text = out.decode().strip()
        ids = text.splitlines() if text else []
        return pane_id in ids
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def new_detached_session(name: str, cmd: str) -> bool:
    """Create a detached tmux session running `cmd`. Used for background claudes."""
    try:
        subprocess.check_call(
            ["tmux", "new-session", "-d", "-s", name, cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Enable mouse + clipboard sync in the inner session.
        # - mouse on: scroll → tmux copy-mode, click → focus
        # - set-clipboard on: text selection → system clipboard
        # Right-click is intercepted by tmux copy-mode (extend selection);
        # use keyboard shortcuts for context-menu actions in Claude Code.
        subprocess.check_call(
            ["tmux", "set-option", "-t", name, "mouse", "on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["tmux", "set-option", "-t", name, "set-clipboard", "on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def session_exists(name: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def kill_session(name: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "kill-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def new_window(session: str, window_name: str, cmd: str) -> str | None:
    """Create a new window in `session` running `cmd`. Returns window id ('@N') or None.

    The new window becomes the active one (default tmux behavior).
    """
    try:
        out = subprocess.check_output(
            ["tmux", "new-window", "-t", session, "-n", window_name,
             "-P", "-F", "#{window_id}", cmd],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def select_window(window_id_or_target: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "select-window", "-t", window_id_or_target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def window_alive(window_id: str) -> bool:
    if not in_tmux():
        return False
    try:
        out = subprocess.check_output(
            ["tmux", "list-windows", "-a", "-F", "#{window_id}"],
            stderr=subprocess.DEVNULL,
        )
        return window_id in out.decode().strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def kill_window(window_id: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "kill-window", "-t", window_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
