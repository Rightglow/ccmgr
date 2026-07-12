"""Thin wrappers around the tmux CLI.

ccmgr uses tmux to host the claude side-pane. All tmux interaction goes
through this module so error handling and command shape stay consistent.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ServerSnapshot:
    """Session and pane identities captured by one tmux server query."""

    sessions: frozenset[str]
    panes: frozenset[str]
    session_pids: tuple[tuple[str, int], ...] = ()

    def pane_pid_for(self, session_name: str) -> int | None:
        """Return the first pane PID captured for *session_name*."""
        for name, pid in self.session_pids:
            if name == session_name:
                return pid
        return None


def has_tmux() -> bool:
    """True if tmux is installed and on PATH."""
    return shutil.which("tmux") is not None


@lru_cache(maxsize=1)
def tmux_version() -> tuple[int, int]:
    """Return the installed tmux (major, minor) version, or (0, 0) if unknown."""
    try:
        out = subprocess.check_output(
            ["tmux", "-V"], stderr=subprocess.DEVNULL
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return (0, 0)
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def enable_clipboard_passthrough() -> None:
    """Force-enable OSC 52 clipboard passthrough so selected text reaches the
    user's *local* system clipboard.

    ccmgr runs a nested tmux (the ccmgr session's right pane attaches the
    claude session), and over SSH the only channel that can reach the real
    terminal's clipboard is the OSC 52 escape sequence. ``set-clipboard on``
    turns that on, but tmux only emits OSC 52 when the terminal's terminfo
    advertises the ``Ms`` capability — many capable terminals (iTerm2, kitty,
    WezTerm, Alacritty, foot, Windows Terminal, …) don't advertise it. This
    force-declares ``Ms`` globally so the drag-select → copy path works
    regardless of terminfo. Idempotent (only appends the override once).
    """
    try:
        cur = subprocess.check_output(
            ["tmux", "show-options", "-gv", "terminal-overrides"],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        cur = ""
    if "Ms=" in cur:
        return
    try:
        subprocess.check_call(
            ["tmux", "set-option", "-ga", "terminal-overrides",
             r",*:Ms=\E]52;%p1%s;%p2%s\007"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


def in_tmux() -> bool:
    """True if the current process is running inside tmux."""
    return os.environ.get("TMUX") is not None


def server_snapshot() -> ServerSnapshot | None:
    """Return all session names and pane IDs using one tmux process.

    None means the snapshot was unavailable or malformed. Callers can then
    fall back to targeted probes instead of treating a transient tmux failure
    as an empty server and pruning live state.
    """
    if not in_tmux():
        return None
    try:
        out = subprocess.check_output(
            [
                "tmux", "list-panes", "-a", "-F",
                "#{session_name}\t#{pane_id}\t#{pane_pid}",
            ],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None

    sessions: set[str] = set()
    panes: set[str] = set()
    session_pids: dict[str, int] = {}
    for line in out.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or not all(fields):
            return None
        session_name, pane_id, raw_pid = fields
        try:
            pane_pid = int(raw_pid)
        except ValueError:
            return None
        sessions.add(session_name)
        panes.add(pane_id)
        session_pids.setdefault(session_name, pane_pid)
    return ServerSnapshot(
        frozenset(sessions),
        frozenset(panes),
        tuple(sorted(session_pids.items())),
    )


def process_has_child(pid: int) -> bool | None:
    """Whether *pid* currently has at least one direct child process."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def session_has_child(session_name: str) -> bool | None:
    """Whether the Claude process has live child processes.

    Returns ``None`` when the probe cannot determine the answer.

    Children == Claude is actively executing a tool (bash, curl, pip, …), not
    waiting for approval (approval prompts run inside Claude's own Node.js
    process and spawn nothing).  Used to tell a running tool from a blocked one.

    Requires procps-ng >= 3.3.12 (``pgrep -P <pid>`` with no pattern).  On
    older pgrep the probe is unavailable, so callers fall back to the JSONL
    time heuristic.  Exit status 1 is the only definitive "no children"
    result; command/setup errors return ``None``.
    """
    try:
        pid = subprocess.check_output(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if not pid:
            return None
        return process_has_child(int(pid))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


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


def split_window_h(cmd: str = "", target: str | None = None,
                   size_percent: int | None = None,
                   detached: bool = False) -> str | None:
    """Create a horizontal split (new pane to the right). Returns the new pane id.

    `size_percent` sets the new (right) pane's width as a percentage of the
    parent pane. E.g. size_percent=70 → ccmgr on the left at 30%, claude
    pane on the right at 70%.
    """
    if not in_tmux():
        return None
    args = ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"]
    if detached:
        args.append("-d")
    if size_percent is not None:
        # `-l <N>%` is only understood by tmux >= 3.1. Older tmux (e.g. 2.7,
        # shipped on many stable distros) rejects it with "size invalid" and
        # the split silently fails, so the claude pane never opens. Fall back
        # to the pre-3.1 `-p <N>` percentage flag there.
        if tmux_version() >= (3, 1):
            args.extend(["-l", f"{size_percent}%"])
        else:
            args.extend(["-p", str(size_percent)])
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


def set_window_border_style(value: str) -> bool:
    """Set active and inactive pane borders to one window-scoped style.

    tmux assigns sections of a shared border to different panes. Updating both
    options together keeps the full divider one colour and uses one tmux client
    process per focus transition.
    """
    if not in_tmux():
        return False
    try:
        subprocess.check_call(
            [
                "tmux", "set-window-option", "pane-border-style", value,
                ";", "set-window-option", "pane-active-border-style", value,
            ],
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


def session_pane_id(session_name: str) -> str | None:
    """Return the first pane id in *session_name*.

    Each detached Claude session currently owns one pane. Keeping this lookup
    here makes the distinction between the outer display pane and the inner
    Claude pane explicit for mouse/copy-mode handling.
    """
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.splitlines()[0] if out else None


def start_scroll_agent(session_name: str, cmd: str) -> str | None:
    """Start a detached scroll-coalescing agent and return its pane id."""
    try:
        out = subprocess.check_output(
            ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}",
             "-s", session_name, cmd],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def set_scroll_agent_target(agent_pane_id: str, target_pane_id: str) -> bool:
    """Tell a running scroll agent which inner Claude pane is visible."""
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-l", "-t", agent_pane_id,
             f"T{target_pane_id}\n"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def set_window_user_option(target_window: str, name: str, value: str | None) -> bool:
    """Set or unset a user option on *target_window*.

    ccmgr's detached Claude sessions contain one pane per window, so a window
    marker is equivalent to a pane marker for this feature. Unlike pane-scoped
    options (``set-option -p``), window user options work on tmux 2.7.
    """
    args = ["tmux", "set-window-option", "-t", target_window]
    if value is None:
        args.extend(["-u", name])
    else:
        args.extend([name, value])
    try:
        subprocess.check_call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def wait_window_user_option(target_window: str, name: str, value: str,
                            timeout: float = 1.0) -> bool:
    """Wait until a tmux window user option reaches *value*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = subprocess.check_output(
                ["tmux", "show-window-options", "-v", "-t", target_window, name],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if current == value:
                return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        time.sleep(0.02)
    return False


_SCROLL_TABLES = ("copy-mode", "copy-mode-vi")
_SCROLL_KEYS = ("WheelUpPane", "WheelDownPane")
ScrollBindingBackup = dict[tuple[str, str], str | None]


def _read_key_binding(table: str, key: str) -> str | None:
    """Return a binding as replayable tmux config, or None when unbound."""
    try:
        text = subprocess.check_output(
            # tmux 2.7 cannot filter list-keys by an individual key, so read
            # the table and select the binding ourselves.
            ["tmux", "list-keys", "-T", table],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    pattern = re.compile(
        rf"^bind-key\s+(?:-r\s+)?-T\s+{re.escape(table)}\s+"
        rf"{re.escape(key)}(?:\s|$)"
    )
    return next((line for line in text.splitlines() if pattern.match(line)), None)


def read_scroll_bindings() -> ScrollBindingBackup:
    """Capture the four wheel bindings changed by scroll coalescing."""
    return {
        (table, key): _read_key_binding(table, key)
        for table in _SCROLL_TABLES
        for key in _SCROLL_KEYS
    }


def _bindings_are_tmux_defaults(backup: ScrollBindingBackup) -> bool:
    """Only wrap stock bindings; custom bindings must remain untouched."""
    for (table, key), binding in backup.items():
        direction = "scroll-up" if key == "WheelUpPane" else "scroll-down"
        pattern = re.compile(
            rf"^bind-key\s+-T\s+{re.escape(table)}\s+{re.escape(key)}\s+"
            rf"select-pane\s+\\;\s+send-keys\s+-X\s+-N\s+\d+\s+"
            rf"{direction}$"
        )
        if binding is None or not pattern.match(" ".join(binding.split())):
            return False
    return True


def prepare_scroll_bindings() -> ScrollBindingBackup | None:
    """Validate capabilities and return bindings safe to wrap.

    tmux 2.7 is the oldest supported implementation of the copy-mode command
    shape used here. User-customized bindings are deliberately rejected.
    """
    if tmux_version() < (2, 7):
        return None
    backup = read_scroll_bindings()
    if not _bindings_are_tmux_defaults(backup):
        return None
    return backup if scroll_lines_per_event(backup) > 0 else None


def scroll_lines_per_event(backup: ScrollBindingBackup) -> int:
    """Extract tmux's configured/default wheel distance from a backup."""
    counts: set[int] = set()
    for binding in backup.values():
        if binding:
            match = re.search(r"\bsend-keys\s+-X\s+-N\s+(\d+)\b", binding)
            if match:
                counts.add(int(match.group(1)))
    return counts.pop() if len(counts) == 1 else 0


def scroll_bindings_owned_by(agent_pane_id: str) -> bool:
    """True when all active wrappers still point at this manager's agent."""
    current = read_scroll_bindings()
    for (_table, key), binding in current.items():
        direction = "U" if key == "WheelUpPane" else "D"
        if (not binding
                or "#{@ccmgr_scroll_agent}" not in binding
                or f"send-keys -t {agent_pane_id} {direction}" not in binding):
            return False
    return True


def _binding_command(binding: str) -> str:
    """Extract a list-keys binding body for use as an if-shell branch."""
    match = re.match(r"^bind-key\s+(?:-r\s+)?-T\s+\S+\s+\S+\s+(.*)$", binding)
    if not match:
        raise ValueError(f"unrecognized tmux binding: {binding}")
    return match.group(1).replace(r"\;", ";")


def _set_scroll_bindings(agent_pane_id: str,
                         backup: ScrollBindingBackup) -> bool:
    """Point managed copy-mode wheel events at *agent_pane_id*."""
    try:
        for table in _SCROLL_TABLES:
            for key in _SCROLL_KEYS:
                binding = backup.get((table, key))
                if binding is None:
                    return False
                direction = "U" if key == "WheelUpPane" else "D"
                accelerated = f"send-keys -t {agent_pane_id} {direction}"
                fallback = _binding_command(binding)
                subprocess.check_call(
                    ["tmux", "bind-key", "-T", table, key,
                     "if-shell", "-F", "-t", "=", "#{@ccmgr_scroll_agent}",
                     accelerated, fallback],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def install_scroll_bindings(agent_pane_id: str) -> ScrollBindingBackup | None:
    """Install copy-mode wheel bindings that coalesce ccmgr pane scrolling.

    Root-table mouse handling is deliberately untouched: the outer tmux keeps
    forwarding mouse events to its nested tmux client, and mouse-aware Claude
    applications keep receiving their events normally. Only once the inner
    Claude pane is in tmux copy-mode do these bindings redirect wheel deltas to
    the persistent coalescing agent named by ``@ccmgr_scroll_agent``.

    The previous bindings are returned so the caller can restore user config.
    """
    backup = prepare_scroll_bindings()
    if backup is None:
        return None

    if not _set_scroll_bindings(agent_pane_id, backup):
        restore_scroll_bindings(backup)
        return None
    return backup


def rebind_scroll_agent(agent_pane_id: str,
                        backup: ScrollBindingBackup) -> bool:
    """Update installed bindings after an agent pane is recreated."""
    return _set_scroll_bindings(agent_pane_id, backup)


def restore_scroll_bindings(backup: ScrollBindingBackup) -> None:
    """Restore bindings returned by :func:`install_scroll_bindings`."""
    configured = [binding for binding in backup.values() if binding]
    if configured:
        path = None
        try:
            # tmux 2.7 source-file does not document stdin ("-") support.
            # A short-lived 0600 config file is portable across supported tmux.
            fd, path = tempfile.mkstemp(prefix="ccmgr-scroll-", suffix=".conf")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(configured) + "\n")
            subprocess.check_call(
                ["tmux", "source-file", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError, FileNotFoundError):
            pass
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    for (table, key), binding in backup.items():
        if binding is None:
            try:
                subprocess.check_call(
                    ["tmux", "unbind-key", "-T", table, key],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass


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
        # Hide this inner session's own status bar. The right pane attaches to it
        # via nested tmux, so its bar would otherwise stack directly above the
        # outer ccmgr status bar — a redundant second line (the sidebar already
        # tracks every session, and the outer bar is ccmgr's status surface).
        # Turning it off reclaims a row for the agent's output. Session-scoped on
        # this ccmgr-owned session, so the user's global tmux config is untouched.
        subprocess.check_call(
            ["tmux", "set-option", "-t", name, "status", "off"],
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
