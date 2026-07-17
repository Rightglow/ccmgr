"""Thin wrappers around the tmux CLI.

Railmux uses tmux to host detached agents and their display panes. All tmux
interaction goes through this module so error handling and command shape stay
consistent.
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


@dataclass(frozen=True)
class PaneIdentity:
    """Stable identity and current location of one tmux pane."""

    pane_id: str
    pane_pid: int
    session_name: str
    session_id: str
    window_id: str
    dead: bool
    width: int
    height: int


@dataclass(frozen=True)
class SessionTopology:
    """Exact session shape used by the de-nested display safety gate."""

    session_name: str
    session_id: str
    attached_clients: int
    window_ids: tuple[str, ...]
    panes: tuple[PaneIdentity, ...]

    @property
    def single_live_pane(self) -> PaneIdentity | None:
        if (len(self.window_ids) == 1 and len(self.panes) == 1
                and not self.panes[0].dead):
            return self.panes[0]
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

    Railmux runs a nested tmux (the display pane attaches an agent session),
    and over SSH the only channel that can reach the real
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


def current_session_id() -> str | None:
    """Return the immutable tmux session ID for the current pane."""
    try:
        out = subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out or None


def pane_identity(pane_id: str) -> PaneIdentity | None:
    """Return identity and current location for *pane_id*.

    Unlike UI-only helpers this intentionally works outside tmux. Startup
    recovery runs before the CLI attaches to (or creates) its outer session.
    """
    fmt = (
        "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{session_id}\t"
        "#{window_id}\t#{pane_dead}\t#{pane_width}\t#{pane_height}"
    )
    try:
        raw = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", pane_id, fmt],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        fields = raw.split("\t")
        if len(fields) != 8:
            return None
        return PaneIdentity(
            pane_id=fields[0],
            pane_pid=int(fields[1]),
            session_name=fields[2],
            session_id=fields[3],
            window_id=fields[4],
            dead=fields[5] == "1",
            width=int(fields[6]),
            height=int(fields[7]),
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        return None


def session_topology(session_name: str) -> SessionTopology | None:
    """Return the exact window/pane topology for one session name or ID."""
    pane_fmt = (
        "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{session_id}\t"
        "#{window_id}\t#{pane_dead}\t#{pane_width}\t#{pane_height}"
    )
    try:
        session_raw = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", session_name,
             "#{session_name}\t#{session_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        session_fields = session_raw.split("\t")
        if len(session_fields) != 2:
            return None
        actual_name, session_id = session_fields
        attached = session_attached_count(session_name)
        windows_text = subprocess.check_output(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        panes_text = subprocess.check_output(
            ["tmux", "list-panes", "-s", "-t", session_name, "-F", pane_fmt],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if not actual_name or not session_id or attached is None:
            return None
        windows = tuple(line for line in windows_text.splitlines() if line)
        panes: list[PaneIdentity] = []
        for raw in panes_text.splitlines():
            fields = raw.split("\t")
            if len(fields) != 8:
                return None
            panes.append(PaneIdentity(
                pane_id=fields[0],
                pane_pid=int(fields[1]),
                session_name=fields[2],
                session_id=fields[3],
                window_id=fields[4],
                dead=fields[5] == "1",
                width=int(fields[6]),
                height=int(fields[7]),
            ))
        return SessionTopology(
            session_name=actual_name,
            session_id=session_id,
            attached_clients=attached,
            window_ids=windows,
            panes=tuple(panes),
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        return None


def session_ids() -> frozenset[str] | None:
    """Return all immutable tmux session IDs, including outside tmux."""
    try:
        text = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    return frozenset(line for line in text.splitlines() if line)


def session_has_window(session_name: str, window_id: str) -> bool:
    try:
        text = subprocess.check_output(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_id}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return False
    return window_id in text.splitlines()


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


def pane_size(pane_id: str) -> tuple[int, int] | None:
    """Return ``(width, height)`` for an outer display pane."""
    try:
        out = subprocess.check_output(
            [
                "tmux", "display-message", "-p", "-t", pane_id,
                "#{pane_width}\t#{pane_height}",
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        raw_width, raw_height = out.split("\t", 1)
        width, height = int(raw_width), int(raw_height)
        if width <= 0 or height <= 0:
            return None
        return width, height
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        return None


def window_size(pane_id: str) -> tuple[int, int] | None:
    """Return the drawable outer-window size containing *pane_id*.

    This is deliberately different from :func:`pane_size`: a process running
    in Railmux's sidebar sees only that pane's TTY dimensions, while layout
    suitability depends on the full tmux window shared by the sidebar and
    agent workspace.
    """
    try:
        out = subprocess.check_output(
            [
                "tmux", "display-message", "-p", "-t", pane_id,
                "#{window_width}\t#{window_height}",
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        raw_width, raw_height = out.split("\t", 1)
        width, height = int(raw_width), int(raw_height)
        if width <= 0 or height <= 0:
            return None
        return width, height
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        return None


def resize_session_window(session_name: str, width: int, height: int) -> bool:
    """Pre-size a detached agent window before a nested client attaches.

    Detached tmux sessions commonly start at 80x24. Attaching one directly to a
    differently-sized outer pane sends an immediate resize to the agent TUI,
    which can replay/reflow a long Codex transcript visibly. Matching the inner
    window first makes the subsequent attach size-stable.
    """
    if width <= 0 or height <= 0:
        return False
    try:
        subprocess.check_call(
            [
                "tmux", "resize-window", "-t", session_name,
                "-x", str(width), "-y", str(height),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def session_attached_count(session_name: str) -> int | None:
    """Number of clients attached to *session_name*, or None on probe failure."""
    try:
        out = subprocess.check_output(
            [
                "tmux", "display-message", "-p", "-t", session_name,
                "#{session_attached}",
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        count = int(out)
        return count if count >= 0 else None
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
        return None


def wait_session_detached(session_name: str, timeout: float = 1.0) -> bool:
    """Wait for all clients to detach from *session_name*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session_attached_count(session_name) == 0:
            return True
        time.sleep(0.02)
    return session_attached_count(session_name) == 0


def swap_panes(source_pane: str, target_pane: str) -> bool:
    """Swap two panes without changing either window's active pane.

    ``swap-pane`` normally follows one of the moved panes and can therefore
    undo a mouse click that just focused the Railmux sidebar.  Display focus is
    owned by the caller's explicit ``select-pane`` path, so every transport
    swap uses ``-d`` to preserve the current active panes.
    """
    try:
        subprocess.check_call(
            ["tmux", "swap-pane", "-d", "-s", source_pane, "-t", target_pane],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def create_grouped_session(name: str, target_session: str) -> bool:
    """Create a detached session sharing *target_session*'s windows.

    A grouped session adds no pane or PTY. It is the second owner that keeps a
    display window alive if the original outer session is killed directly.
    """
    try:
        subprocess.check_call(
            ["tmux", "new-session", "-d", "-t", target_session, "-s", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def set_session_user_option(
    target_session: str, name: str, value: str | None,
) -> bool:
    args = ["tmux", "set-option", "-t", target_session]
    if value is None:
        args.extend(["-u", name])
    else:
        args.extend([name, value])
    try:
        subprocess.check_call(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def show_session_user_option(target_session: str, name: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["tmux", "show-options", "-v", "-t", target_session, name],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    return value or None


def fit_session_to_pane(session_name: str, pane_id: str) -> bool:
    """Best-effort size synchronization used immediately before attach.

    Only detached sessions are resized. An independently attached client owns
    its current dimensions and must not be disrupted merely because Railmux is
    about to become another client.
    """
    if session_attached_count(session_name) != 0:
        return False
    size = pane_size(pane_id)
    return bool(size and resize_session_window(session_name, *size))


def split_window_h(cmd: str = "", target: str | None = None,
                   size_percent: int | None = None,
                   detached: bool = False) -> str | None:
    """Create a horizontal split (new pane to the right). Returns the new pane id.

    `size_percent` sets the new (right) pane's width as a percentage of the
    parent pane. E.g. size_percent=70 → Railmux on the left at 30%, agent
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
        # the split silently fails, so the agent pane never opens. Fall back
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
    pane (so the agent pane lights up the same way Railmux's Urwid panes do
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


def set_window_border_styles(inactive: str, active: str) -> bool:
    """Set inactive and active border styles in one window-scoped tmux call."""
    if not in_tmux():
        return False
    try:
        subprocess.check_call(
            [
                "tmux", "set-window-option", "pane-border-style", inactive,
                ";", "set-window-option", "pane-active-border-style", active,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def set_window_border_style(value: str) -> bool:
    """Paint active and inactive border segments with one continuous colour."""
    return set_window_border_styles(value, value)


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

    Each detached agent session currently owns one pane. Keeping this lookup
    here makes the distinction between the outer display pane and the inner
    agent pane explicit for mouse/copy-mode handling.
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
    """Tell a running scroll agent which inner provider pane is visible."""
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

    Railmux's detached agent sessions contain one pane per window, so a window
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


def show_window_user_option(target_window: str, name: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["tmux", "show-window-options", "-v", "-t", target_window, name],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    return value or None


def list_window_user_options(names: tuple[str, ...]) -> list[tuple[str, ...]] | None:
    """Return window IDs plus selected user-option values server-wide.

    Linked windows may be listed once per owning session; callers should dedupe
    identical records by window/transaction identity.
    """
    if not names:
        return []
    fmt = "\t".join(("#{window_id}", *(f"#{{{name}}}" for name in names)))
    try:
        text = subprocess.check_output(
            ["tmux", "list-windows", "-a", "-F", fmt],
            stderr=subprocess.DEVNULL,
        ).decode().rstrip("\n")
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    rows: list[tuple[str, ...]] = []
    for line in text.splitlines():
        fields = tuple(line.split("\t"))
        if len(fields) != len(names) + 1:
            return None
        rows.append(fields)
    return rows


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
RootWheelBindingBackup = dict[str, Optional[str]]
_ROOT_WHEEL_KEYS = ("WheelUpPane", "WheelDownPane")
_ROOT_WHEEL_MARKER = "railmux-wheel-forward-v1"


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


def _scroll_binding_owned_by(
    agent_pane_id: str, key: str, binding: str | None,
) -> bool:
    """Whether one live copy-mode binding targets this exact helper pane."""
    direction = "U" if key == "WheelUpPane" else "D"
    return bool(
        binding
        and "#{@railmux_scroll_agent}" in binding
        and f"send-keys -t {agent_pane_id} {direction}" in binding
    )


def scroll_bindings_owned_by(agent_pane_id: str) -> bool:
    """True when all active wrappers still point at this manager's agent."""
    return all(
        _scroll_binding_owned_by(agent_pane_id, key, binding)
        for (_table, key), binding in read_scroll_bindings().items()
    )


def _binding_command(binding: str) -> str:
    """Extract a list-keys binding body for use as an if-shell branch."""
    match = re.match(r"^bind-key\s+(?:-r\s+)?-T\s+\S+\s+\S+\s+(.*)$", binding)
    if not match:
        raise ValueError(f"unrecognized tmux binding: {binding}")
    return match.group(1).replace(r"\;", ";")


def read_root_wheel_bindings() -> RootWheelBindingBackup:
    """Capture root-table wheel bindings changed by sidebar forwarding."""
    return {key: _read_key_binding("root", key) for key in _ROOT_WHEEL_KEYS}


def _root_wheel_bindings_are_defaults(
    backup: RootWheelBindingBackup,
) -> bool:
    """Accept only tmux's stock root wheel behavior.

    Root bindings affect every window on the server.  A user-customized wheel
    command therefore disables Railmux forwarding instead of being embedded in
    a wrapper whose command grammar may change across supported tmux versions.
    """
    down = backup.get("WheelDownPane")
    if down is not None:
        return False
    up = backup.get("WheelUpPane")
    if up is None:
        return False
    try:
        body = " ".join(_binding_command(up).split())
    except ValueError:
        return False
    return (
        "mouse_any_flag" in body
        and "send-keys -M" in body
        and "copy-mode -e" in body
        and _ROOT_WHEEL_MARKER not in body
    )


def prepare_root_wheel_bindings() -> RootWheelBindingBackup | None:
    """Return stock root wheel bindings when they are safe to wrap."""
    if tmux_version() < (2, 7):
        return None
    backup = read_root_wheel_bindings()
    return backup if _root_wheel_bindings_are_defaults(backup) else None


def set_root_wheel_forwarding(
    backup: RootWheelBindingBackup,
    token: str,
) -> bool:
    """Forward both wheel directions to mouse-aware pane applications.

    The literal marker makes crash recovery able to distinguish Railmux's
    wrapper from a binding the user installed while Railmux was running.
    Non-mouse-aware panes retain the exact stock fallback; WheelDownPane was
    unbound by default and therefore has no false branch.
    """
    marker = f"{_ROOT_WHEEL_MARKER}-{token}"
    condition = (
        "#{&&:#{mouse_any_flag},"
        f"#{{==:{marker},{marker}}}}}"
    )
    try:
        for key in _ROOT_WHEEL_KEYS:
            args = [
                "tmux", "bind-key", "-T", "root", key,
                "if-shell", "-F", "-t", "=", condition,
                "send-keys -M",
            ]
            binding = backup.get(key)
            if binding is not None:
                args.append(_binding_command(binding))
            subprocess.check_call(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def root_wheel_bindings_owned_by(token: str) -> bool:
    """Whether both live root bindings are this manager's wrappers."""
    marker = f"{_ROOT_WHEEL_MARKER}-{token}"
    return all(
        binding is not None
        and marker in binding
        and "mouse_any_flag" in binding
        and "send-keys -M" in binding
        for binding in read_root_wheel_bindings().values()
    )


def root_wheel_binding_is_original_or_owned(
    key: str,
    binding: str | None,
    original: str | None,
    token: str,
) -> bool:
    """Crash-recovery guard for an interrupted two-binding install."""
    if binding == original:
        return True
    marker = f"{_ROOT_WHEEL_MARKER}-{token}"
    return bool(binding and marker in binding and "send-keys -M" in binding)


def restore_root_wheel_bindings(
    backup: RootWheelBindingBackup,
    *,
    token: str,
) -> None:
    """Restore only bindings still owned by *token*.

    A user may reload tmux configuration while Railmux is open.  Per-key
    ownership checks ensure teardown never overwrites that newer choice.
    """
    current = read_root_wheel_bindings()
    marker = f"{_ROOT_WHEEL_MARKER}-{token}"
    for key in _ROOT_WHEEL_KEYS:
        live = current.get(key)
        if live is None or marker not in live:
            continue
        original = backup.get(key)
        try:
            if original is None:
                subprocess.check_call(
                    ["tmux", "unbind-key", "-T", "root", key],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                fd, path = tempfile.mkstemp(
                    prefix="railmux-root-wheel-", suffix=".conf")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(original + "\n")
                    subprocess.check_call(
                        ["tmux", "source-file", path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                finally:
                    os.unlink(path)
        except (OSError, subprocess.CalledProcessError, FileNotFoundError):
            pass


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
    forwarding mouse events to its nested tmux client, and mouse-aware agent
    applications keep receiving their events normally. Only once the inner
    agent pane is in tmux copy-mode do these bindings redirect wheel deltas to
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


def restore_owned_scroll_bindings(
    agent_pane_id: str,
    backup: ScrollBindingBackup,
) -> None:
    """Restore only wrappers still owned by *agent_pane_id*.

    A user may reload tmux configuration while Railmux is open. Copy-mode key
    tables are server-global, so teardown must preserve every binding changed
    after Railmux installed its wrapper while still removing the other wrappers
    before their helper pane is killed.
    """
    current = read_scroll_bindings()
    owned = {
        binding_key: backup.get(binding_key)
        for binding_key, binding in current.items()
        if binding_key in backup and _scroll_binding_owned_by(
            agent_pane_id, binding_key[1], binding)
    }
    if owned:
        restore_scroll_bindings(owned)


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
    """Create a detached tmux session running `cmd` for a background agent.

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


def create_detached_holder(
    name: str, env: dict[str, str] | None = None,
) -> tuple[PaneIdentity | None, str | None]:
    """Create a short-lived inert session so identity can be marked first.

    The finite holder closes the only unmarked crash window: if the creator
    dies between ``new-session`` and ``set-option``, tmux removes the holder
    automatically instead of retaining an unowned process indefinitely.
    """
    use_env = bool(env) and tmux_version() >= (3, 2)
    args = ["tmux", "new-session", "-d"]
    if use_env and env:
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
    args.extend(["-s", name, "sleep 30"])
    try:
        subprocess.check_call(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return None, "tmux: command not found"
    except subprocess.CalledProcessError as exc:
        raw = (exc.stderr.decode("utf-8", errors="replace")
               if isinstance(exc.stderr, bytes) else str(exc.stderr or ""))
        return None, _single_line_error(raw) or (
            f"tmux new-session failed (exit {exc.returncode})")
    except OSError as exc:
        return None, _single_line_error(f"tmux new-session failed: {exc}")

    topology = session_topology(name)
    pane = topology.single_live_pane if topology is not None else None
    if pane is None:
        kill_session(name)
        return None, "created tmux session has no unique live pane"
    try:
        for option, value in (
            ("mouse", "on"), ("set-clipboard", "on"), ("status", "off"),
        ):
            subprocess.check_call(
                ["tmux", "set-option", "-t", pane.session_id, option, value],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError):
        # Presentation setup remains best effort; identity is the safety gate.
        pass
    return pane, None


def exact_pane_alive(identity: PaneIdentity) -> bool:
    current = pane_identity(identity.pane_id)
    return bool(
        current is not None
        and not current.dead
        and current.session_id == identity.session_id
        and current.session_name == identity.session_name
    )


def kill_session_identity(identity: PaneIdentity) -> bool:
    """Kill an exact immutable tmux session, never a reused session name."""
    current = pane_identity(identity.pane_id)
    if (current is None
            or current.session_id != identity.session_id
            or current.session_name != identity.session_name):
        return False
    try:
        subprocess.check_call(
            ["tmux", "kill-session", "-t", identity.session_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def start_detached_holder(
    identity: PaneIdentity, cmd: str,
) -> tuple[bool, str | None]:
    """Replace an exactly identified, already-marked holder with provider."""
    if not exact_pane_alive(identity):
        return False, "marked tmux holder disappeared before provider start"
    try:
        # Enable this only after the caller has durably marked the holder. An
        # unmarked holder must self-reap when its finite sleep exits. This is a
        # window option on the single-pane holder and works on tmux 2.7.
        subprocess.check_call(
            ["tmux", "set-window-option", "-t", identity.pane_id,
             "remain-on-exit", "on"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        kill_session_identity(identity)
        return False, "tmux could not enable provider launch diagnostics"
    if not respawn_pane(identity.pane_id, cmd):
        kill_session_identity(identity)
        return False, "tmux could not start the provider in the marked pane"

    # Port the legacy immediate-exit diagnosis to the two-step launch path.
    try:
        dead = subprocess.run(
            ["tmux", "display-message", "-p", "-t", identity.pane_id,
             "#{pane_dead}"],
            capture_output=True, text=True, timeout=2,
        )
        if dead.returncode == 0 and dead.stdout.strip() == "1":
            capture = subprocess.run(
                ["tmux", "capture-pane", "-t", identity.pane_id,
                 "-p", "-S", "-20"],
                capture_output=True, text=True, timeout=2,
            )
            lines = [line.strip() for line in capture.stdout.splitlines()
                     if line.strip()] if capture.returncode == 0 else []
            kill_session_identity(identity)
            detail = _single_line_error("; ".join(lines[-3:]))
            return False, "agent command failed: " + (
                detail or "exited immediately")
        if dead.returncode != 0 and not exact_pane_alive(identity):
            return False, "agent command exited immediately"
        subprocess.run(
            ["tmux", "set-window-option", "-t", identity.pane_id,
             "remain-on-exit", "off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # A diagnostic failure cannot authorize a different pane; retain the
        # exact identity and let normal liveness polling make the next decision.
        if not exact_pane_alive(identity):
            return False, "provider pane disappeared during launch"
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
