"""Thin wrappers around the tmux CLI.

railmux uses tmux to host the claude side-pane. All tmux interaction goes
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
from pathlib import Path
from typing import Optional


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

    railmux runs a nested tmux (the railmux session's right pane attaches the
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


# A Codex rollout is stored as ``rollout-<timestamp>-<uuid>.jsonl``; the trailing
# UUID is the session_id railmux binds a placeholder to (#12). Match the standard
# 8-4-4-4-12 hex form anchored to the ``.jsonl`` suffix.
_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


def pane_pid_for_session(session_name: str) -> int | None:
    """Return the first pane's PID for *session_name*, or None.

    railmux launches each agent as its own detached session with a single pane,
    so the first pane PID is the login+interactive shell hosting the agent (the
    real codex is a descendant of it — see :func:`descendant_pids`)."""
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    if not out:
        return None
    try:
        return int(out.splitlines()[0])
    except ValueError:
        return None


def proc_fs_available() -> bool:
    """True when a Linux-style ``/proc`` with fd symlinks is present.

    Correlation (#12) reads ``/proc/<pid>/fd/*``; macOS and other platforms
    without procfs return False so callers fall back to the heuristic."""
    return os.path.isdir("/proc")


def descendant_pids(pid: int) -> list[int]:
    """All transitive descendant PIDs of *pid* (breadth-first via ``pgrep -P``).

    The real codex process is a grandchild of the pane PID (the pane runs
    ``$SHELL -li -c 'exec codex …'``), so a single ``pgrep -P`` is not enough.
    Returns an empty list on error or when there are no descendants. Never
    raises."""
    seen: list[int] = []
    visited = {pid}
    frontier = [pid]
    while frontier:
        nxt: list[int] = []
        for p in frontier:
            try:
                res = subprocess.run(
                    ["pgrep", "-P", str(p)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                continue
            if res.returncode != 0:
                continue
            for tok in res.stdout.decode(errors="ignore").split():
                try:
                    child = int(tok)
                except ValueError:
                    continue
                if child not in visited:
                    visited.add(child)
                    seen.append(child)
                    nxt.append(child)
        frontier = nxt
    return seen


def session_process_ids(session_name: str) -> tuple[int, ...]:
    """Snapshot the pane process and all of its current descendants.

    Capturing this before ``kill-session`` lets destructive cleanup wait until
    the agent writer has actually exited instead of assuming tmux's command
    return means every child has finished its shutdown writes.
    """
    pid = pane_pid_for_session(session_name)
    if pid is None:
        return ()
    return (pid, *descendant_pids(pid))


def wait_for_processes_exit(pids: tuple[int, ...], timeout: float = 2.0,
                            poll_interval: float = 0.02) -> bool:
    """Wait briefly for a captured process set to disappear.

    Returns False on timeout. Permission errors count as "still alive"; other
    lookup errors count as gone. The bounded wait is normally only one poll but
    protects session files from a late Claude/Codex shutdown flush.
    """
    if not pids:
        return True
    deadline = time.monotonic() + timeout
    remaining = set(pids)
    while remaining:
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.remove(pid)
            except PermissionError:
                pass
            except OSError:
                remaining.remove(pid)
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
    return True


def open_rollout_uuids_for_pid(pid: int, sessions_dir: Path) -> set[str]:
    """UUIDs of ``*.jsonl`` rollout files under *sessions_dir* that *pid* holds
    open, read from ``/proc/<pid>/fd/*``.

    Returns an empty set when the process is gone, ``/proc`` fd access is denied,
    or nothing matches. Never raises — correlation is strictly best-effort."""
    fd_dir = os.path.join("/proc", str(pid), "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return set()
    root = os.path.realpath(sessions_dir)
    ids: set[str] = set()
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if not target.endswith(".jsonl"):
            continue
        real = os.path.realpath(target)
        # Fence to the codex sessions dir so an unrelated open .jsonl elsewhere
        # can never be mistaken for a rollout.
        if real != root and not real.startswith(root + os.sep):
            continue
        m = _ROLLOUT_UUID_RE.search(os.path.basename(real))
        if m:
            ids.add(m.group(1))
    return ids


def session_rollout_ids(session_name: str, sessions_dir: Path) -> set[str] | None:
    """Correlate *session_name*'s pane to the rollout UUID(s) its codex process
    currently holds open under *sessions_dir* (#12).

    This is the exact child→rollout link: railmux launched the placeholder in
    this tmux session, the codex running in its pane holds its OWN rollout
    ``*.jsonl`` open, and that file's UUID is the placeholder's real session_id.

    Returns:
      * ``None`` only when correlation is UNAVAILABLE on this PLATFORM (no
        procfs, e.g. macOS) — the caller may then fall back to the heuristic;
      * a set of UUIDs (possibly EMPTY) when procfs IS available. An empty set
        means "correlation ran but codex hasn't opened its rollout fd yet (or
        the pane pid isn't up yet)" — a TRANSIENT state; the caller must WAIT
        for the next tick, NOT fall back (falling back could bind an unrelated
        rollout that appeared first — the staggered race, #12).

    Never raises."""
    # Platform gate first: only a missing procfs is a true (permanent)
    # unavailability. A not-yet-ready pane pid on a procfs system is transient.
    if not proc_fs_available():
        return None
    pid = pane_pid_for_session(session_name)
    if pid is None:
        return set()  # transient: pane not up yet → caller waits, not fallback
    ids: set[str] = set()
    for p in (pid, *descendant_pids(pid)):
        ids |= open_rollout_uuids_for_pid(p, sessions_dir)
    return ids


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
    parent pane. E.g. size_percent=70 → railmux on the left at 30%, claude
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

    Used by railmux to enable a visible border highlight on the active tmux
    pane (so the claude pane lights up the same way railmux's urwid panes do
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

    railmux's detached Claude sessions contain one pane per window, so a window
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
ScrollBindingBackup = dict[tuple[str, str], Optional[str]]


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
                or "#{@railmux_scroll_agent}" not in binding
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
                     "if-shell", "-F", "-t", "=", "#{@railmux_scroll_agent}",
                     accelerated, fallback],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def install_scroll_bindings(agent_pane_id: str) -> ScrollBindingBackup | None:
    """Install copy-mode wheel bindings that coalesce railmux pane scrolling.

    Root-table mouse handling is deliberately untouched: the outer tmux keeps
    forwarding mouse events to its nested tmux client, and mouse-aware Claude
    applications keep receiving their events normally. Only once the inner
    Claude pane is in tmux copy-mode do these bindings redirect wheel deltas to
    the persistent coalescing agent named by ``@railmux_scroll_agent``.

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
            fd, path = tempfile.mkstemp(prefix="railmux-scroll-", suffix=".conf")
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


def _single_line_error(text: str, limit: int = 500) -> str:
    """Make subprocess output safe and compact for a one-line UI error bar."""
    clean = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in text).split()
    )
    if len(clean) > limit:
        return clean[:limit - 1] + "…"
    return clean


def new_detached_session(name: str, cmd: str,
                         env: dict[str, str] | None = None) -> tuple[bool, str | None]:
    """Create a detached tmux session running `cmd`. Used for background claudes.

    Returns ``(True, None)`` on success, ``(False, reason)`` on failure where
    *reason* is a human-readable error string (e.g. "claude: command not found").

    *env* entries are passed to tmux via ``-e KEY=VALUE`` so they land in the
    session environment (inherited by the launched process). railmux uses this
    only for the NON-secret ``CODEX_HOME``; provider API keys are deliberately
    never passed here (tmux RETAINS ``-e`` values in the session environment,
    queryable via ``tmux show-environment``, so ``-e`` would leak a secret just
    as much as embedding it — see App._codex_env). The launched Codex runs under
    a login+interactive shell that sources the user's profile and loads the key
    the normal way.

    ``-e`` on ``new-session`` requires tmux >= 3.2, so we gate on the detected
    version rather than blindly retrying without env on ANY failure — a broad
    retry would silently mask unrelated launch errors. On older tmux the env is
    dropped (callers also carry non-secret values like CODEX_HOME in the command
    string as a fallback); a genuine failure on a capable tmux surfaces as a
    ``(False, reason)`` instead of being swallowed by the fallback path.
    """
    # `new-session -e KEY=VALUE` was added in tmux 3.2 (not 3.0/3.1).
    use_env = bool(env) and tmux_version() >= (3, 2)
    args = ["tmux", "new-session", "-d"]
    if use_env and env:
        for k, v in env.items():
            args.extend(["-e", f"{k}={v}"])
    args.extend(["-s", name, cmd])

    # Step 1 — create the session, capturing stderr on failure.
    try:
        subprocess.check_call(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "tmux: command not found"
    except subprocess.CalledProcessError as e:
        if isinstance(e.stderr, bytes):
            raw_err = e.stderr.decode("utf-8", errors="replace")
        else:
            raw_err = str(e.stderr or "")
        err = _single_line_error(raw_err)
        if not err:
            err = f"tmux new-session failed (exit {e.returncode})"
        return False, err
    except OSError as e:
        return False, _single_line_error(f"tmux new-session failed: {e}")

    # Step 2 — post-creation setup: mouse, clipboard, inner status bar.
    # Best-effort — don't lose a working session over a setup failure.
    try:
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
        # outer railmux status bar — a redundant second line (the sidebar already
        # tracks every session, and the outer bar is railmux's status surface).
        # Turning it off reclaims a row for the agent's output.
        subprocess.check_call(
            ["tmux", "set-option", "-t", name, "status", "off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        pass  # setup is best-effort

    # Step 3 — health check: did the agent command exit immediately?
    # This catches "claude: command not found", "claude --resume <bad-id>",
    # codex CLI crashes, etc. — cases where tmux creates the session
    # successfully but the command inside dies before the user sees anything.
    try:
        dead = subprocess.run(
            ["tmux", "list-panes", "-t", name, "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=2,
        )
        if dead.returncode != 0:
            # With tmux's default ``remain-on-exit off``, an immediately
            # failing command destroys its only pane (and usually its session),
            # so there is no dead pane left to report as ``1``.  Distinguish
            # that high-confidence launch failure from a transient list-panes
            # error while an otherwise healthy session still exists.
            if not session_exists(name):
                return False, (
                    "agent command exited immediately "
                    "(tmux session disappeared)"
                )
            return True, None
        if dead.stdout.strip() == "1":
            capture = subprocess.run(
                ["tmux", "capture-pane", "-t", name, "-p", "-S", "-20"],
                capture_output=True, text=True, timeout=2,
            )
            pane_content = capture.stdout.strip() if capture.returncode == 0 else ""
            # Clean up the dead session — it can never recover.
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if pane_content:
                lines = [ln.strip() for ln in pane_content.splitlines()
                         if ln.strip()]
                detail = _single_line_error("; ".join(lines[-3:]))
                reason = "agent command failed: " + (
                    detail if detail else "exited immediately")
            else:
                reason = "agent command exited immediately (no output)"
            return False, reason
    except (subprocess.SubprocessError, OSError):
        pass  # health check itself failed; assume the session is ok

    return True, None


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
