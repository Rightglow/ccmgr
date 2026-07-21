"""Remote half of the coalesced full-window SSH client.

This module deliberately attaches one real tmux client inside a private PTY.
tmux therefore remains the compositor and input authority for the Railmux
sidebar, borders, status line, and agent panes.  PTY output is consumed into a
headless terminal screen and only bounded latest-state frames cross SSH.

The server refuses to attach when the target session already has a client.  On
EOF it terminates only the exact tmux *client process* it created; it never
kills, resizes with tmux commands, or otherwise tears down the session.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Sequence

from railmux import __version__, restart_state, tmux_health, tmux_server
from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    InputKind,
    InputFrameDecoder,
    PROTOCOL_VERSION,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    ScreenUpdate,
    TerminalMode,
    UpdateKind,
    decode_history_prefetch,
    decode_history_request,
    encode_history_batch,
    encode_history_snapshot,
    encode_update,
)


class DisplayServerError(RuntimeError):
    """A bounded error safe to show through SSH stderr."""


_WATCHDOG_INTERVAL = 5.0
_WATCHDOG_FAILURES = 3
_START_HANDSHAKE_TIMEOUT = 300.0


def _fast_dependency_ready() -> bool:
    """Return whether the installed SSH display dependency is usable."""
    try:
        import pyte
        from pyte import modes as _modes  # noqa: F401

        _extended_pyte(pyte)
    except (ImportError, AttributeError, TypeError):
        return False
    return True


def _emit_remote_hello(ready: bool) -> None:
    """Describe compatibility before acquiring or attaching any tmux state."""
    payload = json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "ready": ready,
            "tmux": shutil.which("tmux") is not None,
            "version": __version__,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    sys.stdout.buffer.write(REMOTE_HELLO_PREFIX + payload + b"\n")
    sys.stdout.buffer.flush()


def _await_client_start(timeout: float = _START_HANDSHAKE_TIMEOUT) -> bool:
    """Wait for the compatible local client before attaching to tmux."""
    readable, _writable, _exceptional = select.select(
        [sys.stdin.buffer], [], [], timeout
    )
    if not readable:
        return False
    return sys.stdin.buffer.readline(len(REMOTE_START) + 1) == REMOTE_START


class _ExtendedScreenMixin:
    """Fill the row-mutating xterm gaps in pyte 0.8.2.

    tmux's ``xterm-256color`` client capabilities include parameterized
    scroll-up, scroll-down, and repeat-character sequences (CSI S/T/b). pyte
    0.8.2 silently dispatches all three to ``Screen.debug``, permanently
    diverging the headless display from tmux. Keep the compatibility layer
    local to the private remote display instead of changing the terminal
    advertised to tmux or forwarding raw control sequences to the client.
    """

    _last_graphic_character = ""
    _character_width = staticmethod(lambda _character: 1)

    def reset(self) -> None:
        self._last_graphic_character = ""
        super().reset()

    def draw(self, data: str) -> None:
        super().draw(data)
        # ByteStream has already removed control syntax. pyte stops drawing at
        # an unprintable character, so retain only the last graphic character
        # that can be repeated with the current rendition attributes.
        for character in reversed(data):
            if self._character_width(character) > 0:
                self._last_graphic_character = character
                break

    def scroll_up(self, count: int | None = None) -> None:
        """Scroll the current DECSTBM region up without moving the cursor."""
        top = 0 if self.margins is None else self.margins.top
        bottom = self.lines - 1 if self.margins is None else self.margins.bottom
        amount = min(max(1, count or 1), bottom - top + 1)
        for row in range(top, bottom - amount + 1):
            source = row + amount
            if source in self.buffer:
                self.buffer[row] = self.buffer[source]
            else:
                self.buffer.pop(row, None)
        for row in range(bottom - amount + 1, bottom + 1):
            self.buffer.pop(row, None)
        self.dirty.update(range(top, bottom + 1))

    def scroll_down(self, count: int | None = None) -> None:
        """Scroll the current DECSTBM region down without moving the cursor."""
        top = 0 if self.margins is None else self.margins.top
        bottom = self.lines - 1 if self.margins is None else self.margins.bottom
        amount = min(max(1, count or 1), bottom - top + 1)
        for row in range(bottom, top + amount - 1, -1):
            source = row - amount
            if source in self.buffer:
                self.buffer[row] = self.buffer[source]
            else:
                self.buffer.pop(row, None)
        for row in range(top, top + amount):
            self.buffer.pop(row, None)
        self.dirty.update(range(top, bottom + 1))

    def repeat_character(self, count: int | None = None) -> None:
        """Repeat the preceding graphic character (REP / CSI Ps b)."""
        if self._last_graphic_character:
            self.draw(self._last_graphic_character * max(1, count or 1))


@lru_cache(maxsize=4)
def _build_extended_pyte(pyte: object) -> object:
    """Return cached pyte-compatible types with Railmux's bounded fixes."""

    class ExtendedScreen(_ExtendedScreenMixin, pyte.Screen):
        _character_width = staticmethod(pyte.screens.wcwidth)

    class ExtendedDiffScreen(_ExtendedScreenMixin, pyte.DiffScreen):
        _character_width = staticmethod(pyte.screens.wcwidth)

    class ExtendedByteStream(pyte.ByteStream):
        csi = dict(pyte.ByteStream.csi)
        csi.update({
            "S": "scroll_up",
            "T": "scroll_down",
            "b": "repeat_character",
        })
        events = frozenset(
            set(pyte.ByteStream.events)
            | {"scroll_up", "scroll_down", "repeat_character"}
        )

    return SimpleNamespace(
        Screen=ExtendedScreen,
        DiffScreen=ExtendedDiffScreen,
        ByteStream=ExtendedByteStream,
        _railmux_extended=True,
    )


def _extended_pyte(pyte: object) -> object:
    """Idempotently adapt one imported pyte module for tmux's VT stream."""
    if getattr(pyte, "_railmux_extended", False):
        return pyte
    return _build_extended_pyte(pyte)


@dataclass(frozen=True)
class _ScreenState:
    sequence: int
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    terminal_modes: TerminalMode
    rows: tuple[bytes, ...]


@dataclass(frozen=True)
class _PaneGeometry:
    pane_id: str
    x: int
    y: int
    width: int
    height: int
    history_server: tmux_server.TmuxServerTarget | None = None
    history_pane_id: str | None = None


_ANSI_FG = {
    "default": 39,
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "brightblack": 90,
    "brightred": 91,
    "brightgreen": 92,
    "brightbrown": 93,
    "brightblue": 94,
    "brightmagenta": 95,
    "bfightmagenta": 95,  # pyte 0.8.2 compatibility typo
    "brightcyan": 96,
    "brightwhite": 97,
}
_ANSI_BG = {
    "default": 49,
    "black": 40,
    "red": 41,
    "green": 42,
    "brown": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "brightblack": 100,
    "brightred": 101,
    "brightgreen": 102,
    "brightbrown": 103,
    "brightblue": 104,
    "brightmagenta": 105,
    "bfightmagenta": 105,
    "brightcyan": 106,
    "brightwhite": 107,
}


def _tmux_output(*args: str) -> str:
    try:
        return subprocess.check_output(
            tmux_server.tmux_argv(*args),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise DisplayServerError("tmux command failed") from exc


def _try_session_id(session: str) -> str | None:
    """Resolve a named session without treating absence as a server failure."""
    try:
        value = subprocess.check_output(
            tmux_server.tmux_argv(
                "display-message", "-p", "-t", session, "#{session_id}"
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).strip()
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None
    if not value.startswith("$") or not value[1:].isdigit():
        return None
    return value


def _live_controller(session_id: str) -> str | None:
    """Return the controller pane only when both stored identities are live."""
    try:
        controller = _tmux_output(
            "show-window-options", "-v", "-t", session_id,
            "@railmux_controller_pane",
        )
        identity = _tmux_output(
            "display-message", "-p", "-t", controller,
            "#{session_id}\t#{pane_id}",
        )
    except DisplayServerError:
        return None
    if (
        controller.startswith("%")
        and controller[1:].isdigit()
        and identity == f"{session_id}\t{controller}"
    ):
        return controller
    return None


def _ensure_railmux_session(session: str, timeout: float = 15.0) -> str:
    """Return a session ID, starting the default Railmux session if absent."""
    session_id = _try_session_id(session)
    if session_id is not None:
        return session_id
    if session != "railmux":
        raise DisplayServerError(
            f"Railmux session is not available: {session}; only the default "
            "railmux session can be started automatically"
        )

    railmux_command = shlex.join(
        [sys.executable, "-m", "railmux", "--inside-tmux"]
    )
    try:
        result = subprocess.run(
            tmux_server.tmux_argv(
                "new-session", "-d", "-s", session, railmux_command
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DisplayServerError("could not start the Railmux tmux session") from exc

    # A concurrent client may have won the new-session race. In either case,
    # accept only the live session that can now be resolved by immutable ID.
    deadline = time.monotonic() + (1.0 if result.returncode else timeout)
    while time.monotonic() < deadline:
        session_id = _try_session_id(session)
        if session_id is not None and _live_controller(session_id) is not None:
            return session_id
        time.sleep(0.05)
    raise DisplayServerError("Railmux did not become ready after it was started")


def _validate_unattached_railmux(session: str) -> str:
    """Return the immutable session ID after conservative validation."""
    try:
        session_id = _tmux_output(
            "display-message", "-p", "-t", session, "#{session_id}"
        )
    except DisplayServerError as exc:
        raise DisplayServerError(
            f"Railmux session is not available: {session}"
        ) from exc
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        raise DisplayServerError("tmux returned an invalid session identity")

    controller = _tmux_output(
        "show-window-options", "-v", "-t", session_id,
        "@railmux_controller_pane",
    )
    if not controller.startswith("%") or not controller[1:].isdigit():
        raise DisplayServerError(
            "the target is not a live managed Railmux window"
        )
    controller_identity = _tmux_output(
        "display-message", "-p", "-t", controller,
        "#{session_id}\t#{pane_id}",
    )
    if controller_identity != f"{session_id}\t{controller}":
        raise DisplayServerError("the Railmux controller identity changed")

    try:
        raw_clients = subprocess.check_output(
            tmux_server.tmux_argv(
                "list-clients", "-F", "#{session_id}\t#{client_name}"
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        raw_clients = ""
    attached = [
        row for row in raw_clients.splitlines()
        if row.split("\t", 1)[0] == session_id
    ]
    if attached:
        raise DisplayServerError(
            "Railmux already has an attached client; detach it with Ctrl-B d "
            "before starting railmux ssh"
        )
    return session_id


def _classify_remote_exit(session_id: str) -> RemoteExit:
    """Classify normal tmux-client exit without mutating any session state."""
    if _try_session_id(session_id) != session_id:
        return RemoteExit.HARD_QUIT
    if _live_controller(session_id) is not None:
        return RemoteExit.DETACHED
    return RemoteExit.SOFT_QUIT


def _classify_observed_exit(
    session_id: str, target: tmux_server.TmuxServerTarget,
) -> RemoteExit:
    """Distinguish an intentional hard quit from an abrupt tmux loss."""
    exit_kind = _classify_remote_exit(session_id)
    if exit_kind is not RemoteExit.HARD_QUIT:
        return exit_kind
    if tmux_health.consume_clean_exit(
            server_pid=target.server_pid, session_id=session_id):
        return exit_kind
    tmux_health.record_incident(
        component="remote-display",
        reason="remote-display-server-exit",
        consecutive_failures=1,
    )
    raise DisplayServerError(
        "the managed tmux session disappeared unexpectedly; run "
        "'railmux doctor' for diagnostics"
    )


def _pane_at_pointer(
    session_id: str, x: int, y: int,
) -> _PaneGeometry | None:
    """Resolve a non-controller pane from 1-based client coordinates."""
    pointer_x, pointer_y = x - 1, y - 1
    for pane in _list_agent_panes(session_id):
        if (
            pane.x <= pointer_x < pane.x + pane.width
            and pane.y <= pointer_y < pane.y + pane.height
        ):
            return pane
    return None


def _list_agent_panes(session_id: str) -> tuple[_PaneGeometry, ...]:
    """Return one coherent, fail-closed generation of visible agent panes."""
    controller = _live_controller(session_id)
    if controller is None:
        return ()
    try:
        output = subprocess.check_output(
            tmux_server.tmux_argv(
                "list-panes", "-t", session_id,
                "-F", "#{session_id}\t#{window_zoomed_flag}\t#{pane_active}\t"
                "#{pane_id}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t"
                f"#{{pane_height}}\t#{{{tmux_server.HISTORY_SOURCE_OPTION}}}",
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ()
    rows: list[tuple[bool, bool, _PaneGeometry]] = []
    seen: set[str] = set()
    for raw_row in output.splitlines():
        fields = raw_row.split("\t")
        if (
            len(fields) != 9
            or fields[0] != session_id
            or fields[1] not in ("0", "1")
            or fields[2] not in ("0", "1")
        ):
            return ()
        pane_id = fields[3]
        try:
            left, top, width, height = map(int, fields[4:8])
        except ValueError:
            return ()
        if (
            pane_id in seen
            or not pane_id.startswith("%")
            or not pane_id[1:].isdigit()
            or left < 0
            or top < 0
            or width <= 0
            or height <= 0
        ):
            return ()
        seen.add(pane_id)
        history_server = None
        history_pane_id = None
        marker = fields[8]
        if marker:
            source = tmux_server.resolve_history_source(marker, timeout=0.25)
            if source is not None:
                history_server, history_session_id = source
                history_pane_id = tmux_server.target_single_pane_id(
                    history_server, history_session_id, timeout=0.25)
                if history_pane_id is None:
                    history_server = None
        rows.append((
            fields[1] == "1",
            fields[2] == "1",
            _PaneGeometry(
                pane_id, left, top, width, height,
                history_server, history_pane_id,
            ),
        ))
    if not rows or len({zoomed for zoomed, _active, _pane in rows}) != 1:
        return ()
    active = [pane for _zoomed, is_active, pane in rows if is_active]
    if len(active) != 1:
        return ()
    if rows[0][0]:
        # tmux retains the old unzoomed geometry on hidden panes. Only the
        # active pane actually occupies the client when the window is zoomed.
        return () if active[0].pane_id == controller else (active[0],)
    return tuple(pane for _zoomed, _active, pane in rows if pane.pane_id != controller)


def _render_history_line(pyte: object, line: bytes, width: int) -> bytes:
    """Parse one physical tmux line and emit only allowlisted SGR styling."""
    screen = pyte.Screen(width, 1)
    stream = pyte.ByteStream(screen)
    stream.feed(line)
    return render_rows(screen)[0]


def _capture_pane_history(
    pyte: object,
    pane: _PaneGeometry,
    request_id: int,
    max_lines: int,
) -> HistorySnapshot | None:
    try:
        if pane.history_server is not None and pane.history_pane_id is not None:
            if not tmux_server.target_is_live(
                    pane.history_server, timeout=0.25):
                return None
            argv = tmux_server.target_argv(
                pane.history_server,
                "capture-pane", "-p", "-e", "-t", pane.history_pane_id,
                "-S", f"-{max_lines}",
            )
        else:
            argv = tmux_server.tmux_argv(
                "capture-pane", "-p", "-e", "-t", pane.pane_id,
                "-S", f"-{max_lines}",
            )
        output = subprocess.check_output(
            argv,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    raw_lines = output.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    lines = tuple(
        _render_history_line(pyte, line, pane.width)
        for line in raw_lines[-4096:]
    )
    if len(lines) < pane.height:
        blank = _render_history_line(pyte, b"", pane.width)
        lines += (blank,) * (pane.height - len(lines))
    return HistorySnapshot(
        request_id=request_id,
        pane_id=pane.pane_id,
        x=pane.x,
        y=pane.y,
        width=pane.width,
        height=pane.height,
        lines=lines,
    )


def capture_history_snapshot(
    session_id: str,
    request_id: int,
    x: int,
    y: int,
    max_lines: int,
    pyte: object | None = None,
) -> HistorySnapshot:
    """Capture bounded styled history without entering tmux copy-mode."""
    pane = _pane_at_pointer(session_id, x, y)
    if pane is None:
        return HistorySnapshot(request_id, None)
    try:
        if pyte is None:
            import pyte as pyte_module

            pyte = pyte_module
        pyte = _extended_pyte(pyte)
        snapshot = _capture_pane_history(
            pyte, pane, request_id, max_lines
        )
    except (ImportError, ValueError, IndexError):
        return HistorySnapshot(request_id, None)
    return snapshot or HistorySnapshot(request_id, None)


def capture_history_batch(
    pyte: object,
    session_id: str,
    request_id: int,
    max_lines: int,
) -> HistoryBatch:
    """Atomically describe and warm-cache every visible agent pane."""
    pyte = _extended_pyte(pyte)
    snapshots = tuple(
        snapshot
        for pane in _list_agent_panes(session_id)
        if (
            snapshot := _capture_pane_history(
                pyte, pane, request_id, max_lines
            )
        ) is not None
    )
    return HistoryBatch(request_id, snapshots)


def _acquire_display_lock(session_id: str) -> int:
    """Serialize the validation-and-attach boundary for one tmux session."""
    key = session_id[1:] if session_id.startswith("$") else "invalid"
    if not key.isdigit():
        raise DisplayServerError("invalid session identity for display lock")
    try:
        socket_path = _tmux_output(
            "display-message", "-p", "-t", session_id, "#{socket_path}"
        )
        if not socket_path.startswith("/"):
            raise OSError("invalid tmux socket path")
        socket_key = hashlib.sha256(socket_path.encode()).hexdigest()[:16]
        path = (
            restart_state.runtime_state_dir()
            / f"fast-display-{socket_key}-{key}.lock"
        )
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise OSError("unsafe display lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        try:
            os.close(fd)
        except (NameError, OSError):
            pass
        raise DisplayServerError(
            "another full-window client is already starting or attached"
        ) from exc
    return fd


def _release_display_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _set_winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def _spawn_tmux_client(session_id: str, width: int, height: int) -> tuple[int, int]:
    """Start an exact tmux attach client and return ``(pid, master_fd)``."""
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, width, height)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised only by a real PTY smoke test
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for target_fd in (0, 1, 2):
                os.dup2(slave_fd, target_fd)
            if slave_fd > 2:
                os.close(slave_fd)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env.setdefault("COLORTERM", "truecolor")
            argv = tmux_server.tmux_argv(
                "attach-session", "-t", session_id, env=env
            )
            os.execvpe(
                "tmux", argv, env
            )
        except BaseException:
            os._exit(127)
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _wait_until_attached(session_id: str, pid: int, timeout: float = 2.0) -> bool:
    """Do not expose a frame until tmux has registered the private client."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return False
        try:
            clients = subprocess.check_output(
                tmux_server.tmux_argv(
                    "list-clients", "-F", "#{session_id}"
                ),
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.5,
            ).splitlines()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            clients = []
        if session_id in clients:
            return True
        time.sleep(0.01)
    return False


def _colour_codes(value: str, *, foreground: bool) -> list[str]:
    named = _ANSI_FG if foreground else _ANSI_BG
    if value in named:
        return [str(named[value])]
    if len(value) == 6:
        try:
            red, green, blue = (
                int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
            )
        except ValueError:
            pass
        else:
            return ["38" if foreground else "48", "2", str(red), str(green), str(blue)]
    return ["39" if foreground else "49"]


def _style(char: object) -> bytes:
    codes: list[str] = ["0"]
    for enabled, code in (
        (char.bold, "1"),
        (char.italics, "3"),
        (char.underscore, "4"),
        (char.blink, "5"),
        (char.reverse, "7"),
        (char.strikethrough, "9"),
    ):
        if enabled:
            codes.append(code)
    codes.extend(_colour_codes(char.fg, foreground=True))
    codes.extend(_colour_codes(char.bg, foreground=False))
    return f"\033[{';'.join(codes)}m".encode()


def _style_key(char: object) -> tuple[object, ...]:
    return (
        char.fg,
        char.bg,
        char.bold,
        char.italics,
        char.underscore,
        char.strikethrough,
        char.reverse,
        char.blink,
    )


def render_rows(screen: object) -> tuple[bytes, ...]:
    """Render independently paintable rows with allowlisted SGR controls."""
    rendered_rows: list[bytes] = []
    for row_index in range(screen.lines):
        rendered = [b"\033[0m"]
        previous_style: tuple[object, ...] | None = None
        row = screen.buffer[row_index]
        for column in range(screen.columns):
            char = row[column]
            style = _style_key(char)
            if style != previous_style:
                rendered.append(_style(char))
                previous_style = style
            # pyte represents the second cell of a wide glyph as empty data;
            # the real terminal already advances two columns for the glyph.
            if char.data:
                safe_data = "".join(
                    value
                    if value >= " "
                    and value != "\x7f"
                    and not "\x80" <= value <= "\x9f"
                    else "�"
                    for value in char.data
                )
                rendered.append(safe_data.encode("utf-8", errors="replace"))
        rendered.append(b"\033[0m")
        rendered_rows.append(b"".join(rendered))
    return tuple(rendered_rows)


def terminal_modes_for_screen(screen: object) -> TerminalMode:
    """Project pyte's private-mode set onto the bounded v6 wire allowlist."""
    terminal_modes = TerminalMode.NONE
    if 2004 << 5 in screen.mode:
        terminal_modes |= TerminalMode.BRACKETED_PASTE
    if 1004 << 5 in screen.mode:
        terminal_modes |= TerminalMode.FOCUS_EVENTS
    return terminal_modes


def _child_exited(pid: int) -> bool:
    try:
        found, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return found == pid


def _stop_client(pid: int, master_fd: int) -> None:
    """Stop only the private attach client; never address the tmux session."""
    try:
        os.close(master_fd)
    except OSError:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _child_exited(pid):
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _remote_watchdog_tripped(
    watchdog: tmux_health.FailureWatchdog,
    session_id: str,
    expected_server_pid: int,
    now: float,
) -> bool:
    """Run one due low-frequency probe and persist only a terminal failure."""
    if not watchdog.due(now):
        return False
    try:
        raw_identity = _tmux_output(
            "display-message", "-p", "-t", session_id,
            "#{pid}\t#{session_id}",
        )
    except DisplayServerError:
        raw_identity = ""
    healthy = raw_identity == f"{expected_server_pid}\t{session_id}"
    if not watchdog.observe(healthy, now):
        return False
    tmux_health.record_incident(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=watchdog.consecutive_failures,
    )
    return True


def serve(session: str, width: int, height: int, fps: float) -> int:
    try:
        import pyte
        from pyte import modes
    except ImportError as exc:
        raise DisplayServerError(
            "pyte is required remotely; install railmux[ssh]"
        ) from exc
    pyte = _extended_pyte(pyte)

    initial_session_id = _ensure_railmux_session(session)
    lock_fd = _acquire_display_lock(initial_session_id)
    try:
        session_id = _validate_unattached_railmux(session)
        if session_id != initial_session_id:
            raise DisplayServerError("Railmux session changed while attaching")
        return _serve_attached(pyte, modes, session_id, width, height, fps)
    finally:
        _release_display_lock(lock_fd)


def _serve_attached(
    pyte: object,
    modes: object,
    session_id: str,
    width: int,
    height: int,
    fps: float,
) -> int:
    pid, master_fd = _spawn_tmux_client(session_id, width, height)
    if not _wait_until_attached(session_id, pid):
        _stop_client(pid, master_fd)
        raise DisplayServerError("the private tmux client failed to attach")
    screen = pyte.DiffScreen(width, height)
    stream = pyte.ByteStream(screen)
    input_decoder = InputFrameDecoder()
    stdin_fd = sys.stdin.buffer.fileno()
    stdout_fd = sys.stdout.buffer.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    interval = 1.0 / fps
    next_frame = time.monotonic()
    screen_changed = True
    force_keyframe = True
    pty_open = True
    delivered: _ScreenState | None = None
    pending_packet: bytes | None = None
    pending_offset = 0
    pending_state: _ScreenState | None = None
    control_packets: deque[bytes] = deque()
    input_closed = False
    watchdog = tmux_health.FailureWatchdog.starting(
        time.monotonic(),
        interval=_WATCHDOG_INTERVAL,
        failure_limit=_WATCHDOG_FAILURES,
    )
    try:
        target = tmux_server.discover_target(timeout=2.0)
    except tmux_server.TmuxServerError as exc:
        _stop_client(pid, master_fd)
        raise DisplayServerError(
            "dedicated tmux server stopped responding after attach"
        ) from exc
    if target is None:
        _stop_client(pid, master_fd)
        raise DisplayServerError("dedicated tmux server disappeared after attach")

    def discard_unsent_update() -> None:
        nonlocal pending_packet, pending_offset, pending_state
        if (
            pending_packet is not None
            and pending_state is not None
            and pending_offset == 0
        ):
            pending_packet = None
            pending_state = None

    def queue_control_packet(packet: bytes) -> None:
        if len(control_packets) >= 4:
            return
        discard_unsent_update()
        control_packets.append(packet)

    def activate_control_packet() -> None:
        nonlocal pending_packet, pending_offset, pending_state
        if pending_packet is None and control_packets:
            pending_packet = control_packets.popleft()
            pending_offset = 0
            pending_state = None

    def apply_resize(new_width: int, new_height: int) -> None:
        nonlocal width, height, force_keyframe, screen_changed
        if not 40 <= new_width <= 1000 or not 12 <= new_height <= 500:
            return
        if (new_width, new_height) == (width, height):
            return
        discard_unsent_update()
        _set_winsize(master_fd, new_width, new_height)
        screen.resize(lines=new_height, columns=new_width)
        width, height = new_width, new_height
        force_keyframe = True
        screen_changed = True

    def schedule_latest_update(now: float) -> None:
        nonlocal pending_packet, pending_state, screen_changed
        nonlocal force_keyframe, next_frame
        if pending_packet is not None or not (screen_changed or force_keyframe):
            return
        rows = render_rows(screen)
        cursor_x = min(screen.cursor.x, width - 1)
        cursor_y = min(screen.cursor.y, height - 1)
        cursor_visible = modes.DECTCEM in screen.mode
        terminal_modes = terminal_modes_for_screen(screen)
        keyframe = (
            force_keyframe
            or delivered is None
            or delivered.width != width
            or delivered.height != height
        )
        if keyframe:
            changed_rows = tuple(enumerate(rows))
            kind = UpdateKind.KEYFRAME
        else:
            assert delivered is not None
            changed_rows = tuple(
                (index, row)
                for index, row in enumerate(rows)
                if row != delivered.rows[index]
            )
            kind = UpdateKind.PATCH
            if not changed_rows and (
                cursor_x == delivered.cursor_x
                and cursor_y == delivered.cursor_y
                and cursor_visible == delivered.cursor_visible
                and terminal_modes == delivered.terminal_modes
            ):
                screen_changed = False
                next_frame = now + interval
                return
        sequence = 1 if delivered is None else (delivered.sequence + 1) & 0xFFFFFFFF
        update = ScreenUpdate(
            kind=kind,
            sequence=sequence,
            width=width,
            height=height,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            cursor_visible=cursor_visible,
            rows=changed_rows,
            terminal_modes=terminal_modes,
        )
        pending_packet = encode_update(update)
        pending_state = _ScreenState(
            sequence=sequence,
            width=width,
            height=height,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            cursor_visible=cursor_visible,
            terminal_modes=terminal_modes,
            rows=rows,
        )
        screen_changed = False
        force_keyframe = False
        next_frame = now + interval

    try:
        while pty_open and not _child_exited(pid):
            activate_control_packet()
            now = time.monotonic()
            timeout = (
                0.25 if pending_packet is not None
                else max(0.0, min(0.25, next_frame - now))
            )
            writable_fds = [stdout_fd] if pending_packet is not None else []
            readable, writable, _ = select.select(
                [master_fd, stdin_fd], writable_fds, [], timeout
            )
            if stdin_fd in readable:
                try:
                    packet = os.read(stdin_fd, 65536)
                except BlockingIOError:
                    packet = None
                if packet == b"":
                    input_closed = True
                    break
                for message in input_decoder.feed(packet or b""):
                    if message.kind is InputKind.RESIZE:
                        apply_resize(*struct.unpack(">HH", message.data))
                        continue
                    if message.kind is InputKind.REQUEST_KEYFRAME:
                        discard_unsent_update()
                        force_keyframe = True
                        screen_changed = True
                        continue
                    if message.kind is InputKind.REQUEST_HISTORY:
                        if len(control_packets) < 4:
                            try:
                                request = decode_history_request(message.data)
                            except ValueError:
                                continue
                            snapshot = capture_history_snapshot(
                                session_id, *request, pyte=pyte
                            )
                            queue_control_packet(
                                encode_history_snapshot(snapshot)
                            )
                        continue
                    if message.kind is InputKind.PREFETCH_HISTORY:
                        if len(control_packets) < 4:
                            try:
                                request_id, max_lines = decode_history_prefetch(
                                    message.data
                                )
                            except ValueError:
                                continue
                            batch = capture_history_batch(
                                pyte, session_id, request_id, max_lines
                            )
                            queue_control_packet(encode_history_batch(batch))
                        continue
                    view = memoryview(message.data)
                    while view:
                        try:
                            written = os.write(master_fd, view)
                        except BlockingIOError:
                            select.select([], [master_fd], [], 0.05)
                            continue
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                pty_open = False
                                break
                            raise
                        view = view[written:]
            if master_fd in readable:
                try:
                    output = os.read(master_fd, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        pty_open = False
                        output = b""
                    else:
                        raise
                if not output:
                    pty_open = False
                else:
                    stream.feed(output)
                    screen_changed = True

            if stdout_fd in writable and pending_packet is not None:
                try:
                    written = os.write(stdout_fd, pending_packet[pending_offset:])
                except BlockingIOError:
                    written = 0
                except BrokenPipeError:
                    return 0
                pending_offset += written
                if pending_offset == len(pending_packet):
                    if pending_state is not None:
                        # This is the only place the diff base advances.
                        # Replacing a wholly unsent display packet therefore
                        # recomputes against the last successfully sent state.
                        delivered = pending_state
                    pending_packet = None
                    pending_offset = 0
                    pending_state = None

            now = time.monotonic()
            if _remote_watchdog_tripped(
                watchdog, session_id, target.server_pid, now
            ):
                raise DisplayServerError(
                    "dedicated tmux server stopped responding; run "
                    "'railmux doctor' for diagnostics"
                )
            if (
                pending_packet is not None
                and pending_state is not None
                and pending_offset == 0
                and screen_changed
                and now >= next_frame
            ):
                pending_packet = None
                pending_state = None
                schedule_latest_update(now)
            elif (
                pending_packet is None
                and not control_packets
                and now >= next_frame
            ):
                schedule_latest_update(now)
        if input_closed:
            return 0
        return int(_classify_observed_exit(session_id, target))
    finally:
        _stop_client(pid, master_fd)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="railmux remote-server",
        description="Internal coalesced full-window Railmux display server"
    )
    parser.add_argument("--protocol", type=int, required=True)
    parser.add_argument("--session", default="railmux")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.protocol != PROTOCOL_VERSION:
        parser.error(
            f"protocol mismatch: server requires version {PROTOCOL_VERSION}"
        )
    if not 40 <= args.width <= 1000:
        parser.error("--width must be between 40 and 1000")
    if not 12 <= args.height <= 500:
        parser.error("--height must be between 12 and 500")
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps must be between 1 and 60")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    ready = _fast_dependency_ready()
    _emit_remote_hello(ready)
    args = parse_args(argv)
    if not ready:
        print(
            "remote display: pyte is unavailable; install "
            "'railmux[ssh]'",
            file=sys.stderr,
        )
        return 2
    if not _await_client_start():
        print(
            "remote display: compatible client did not confirm startup",
            file=sys.stderr,
        )
        return 2
    try:
        tmux_server.socket_label()
        return serve(args.session, args.width, args.height, args.fps)
    except (DisplayServerError, tmux_server.TmuxServerError) as exc:
        print(f"fast display server: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
