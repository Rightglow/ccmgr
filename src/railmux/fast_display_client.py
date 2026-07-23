"""Interactive latest-state SSH client for the complete Railmux tmux window.

The remote helper attaches one real tmux client inside a private PTY and
coalesces its output before sending a compressed keyframe followed by changed
rows over ordinary SSH. All input except Ctrl-] is delivered to that tmux
client, so native tmux and Railmux bindings remain authoritative. Ctrl-] is
always consumed locally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import selectors
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, replace
from enum import Enum
from typing import BinaryIO, NoReturn, Optional, Sequence

from packaging.version import InvalidVersion, Version

from railmux import __version__
from railmux.fast_display_protocol import (
    DISPLAY_MAGIC,
    HistoryBatch,
    HistorySnapshot,
    PROTOCOL_VERSION,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_ATTACH_BUSY,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    ScreenUpdate,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    encode_history_prefetch,
    encode_history_request,
    encode_heartbeat,
    encode_input,
    encode_keyframe_request,
    encode_resize,
)

LOCAL_ESCAPE = b"\x1d"  # Ctrl-]
_SGR_MOUSE_PREFIX = b"\x1b[<"
_SGR_STYLE_RE = re.compile(rb"\x1b\[[0-9;]*m")
_HISTORY_SCROLL_LINES = 3
_HISTORY_PREFETCH_LINES = 300
_HISTORY_FULL_LINES = 2000
_HISTORY_PREFETCH_INTERVAL = 3.0
_HISTORY_PREFETCH_TIMEOUT = 6.0
_HISTORY_CONTENT_PANES = 8
_REMOTE_HELLO_TIMEOUT = 60.0
_REMOTE_HELLO_LIMIT = 16 * 1024
_REMOTE_ATTACH_TIMEOUT = 30.0
_REMOTE_ATTACH_RETRY_DELAY = 0.2
_HEARTBEAT_INTERVAL = 5.0
_DISPLAY_MAGIC_PREFIX = b"RMUXD"
_REMOTE_VENV = ".local/share/railmux/ssh-venv"
_MIN_TERMINAL_COLUMNS = 40
_MIN_TERMINAL_LINES = 12
_MAX_TERMINAL_COLUMNS = 1000
_MAX_TERMINAL_LINES = 500
_TERMINAL_SIZE_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class AppliedScreen:
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    terminal_modes: TerminalMode
    rows: tuple[bytes, ...]
    changed_rows: tuple[int, ...]
    clear: bool


class ScreenModel:
    """Apply sequenced updates and reject patches without a valid base."""

    def __init__(self) -> None:
        self.sequence: int | None = None
        self.width = 0
        self.height = 0
        self.rows: list[bytes] = []

    def apply(
        self, update: ScreenUpdate, expected_size: os.terminal_size,
    ) -> AppliedScreen | None:
        if (update.width, update.height) != (
            expected_size.columns, expected_size.lines
        ):
            return None
        if update.kind is UpdateKind.KEYFRAME:
            rows = [b""] * update.height
            for index, row in update.rows:
                rows[index] = row
            self.rows = rows
            changed = tuple(range(update.height))
            clear = True
        else:
            expected_sequence = (
                None if self.sequence is None else (self.sequence + 1) & 0xFFFFFFFF
            )
            if (
                expected_sequence is None
                or update.sequence != expected_sequence
                or update.width != self.width
                or update.height != self.height
            ):
                return None
            for index, row in update.rows:
                self.rows[index] = row
            changed = tuple(index for index, _row in update.rows)
            clear = False
        self.sequence = update.sequence
        self.width = update.width
        self.height = update.height
        return AppliedScreen(
            width=update.width,
            height=update.height,
            cursor_x=update.cursor_x,
            cursor_y=update.cursor_y,
            cursor_visible=update.cursor_visible,
            terminal_modes=update.terminal_modes,
            rows=tuple(self.rows),
            changed_rows=changed,
            clear=clear,
        )


def full_repaint(screen: AppliedScreen) -> AppliedScreen:
    return AppliedScreen(
        width=screen.width,
        height=screen.height,
        cursor_x=screen.cursor_x,
        cursor_y=screen.cursor_y,
        cursor_visible=screen.cursor_visible,
        terminal_modes=screen.terminal_modes,
        rows=screen.rows,
        changed_rows=tuple(range(screen.height)),
        clear=True,
    )


def compact_status_row(screen: AppliedScreen) -> int | None:
    """Return the 1-based compact navigation row at either tmux bar edge."""
    prefixes = (
        b"[R][1][2] ",
        b"[Railmux][A1][A2] ",
        b"[Railmux][Agent 1][Agent 2] ",
    )
    for index in dict.fromkeys((0, screen.height - 1)):
        if not 0 <= index < len(screen.rows):
            continue
        plain = _SGR_STYLE_RE.sub(b"", screen.rows[index])
        if plain.startswith(prefixes):
            return index + 1
    return None


class ProbeError(RuntimeError):
    """A bounded, user-facing SSH display failure."""


@dataclass(frozen=True)
class RemoteHello:
    version: str
    protocol: int
    ready: bool
    tmux: bool = True


class RemoteStartKind(Enum):
    HELLO = "hello"
    MISSING = "missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RemoteAttachKind(Enum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class RemoteStartup:
    kind: RemoteStartKind
    hello: RemoteHello | None = None
    returncode: int | None = None


def await_remote_attach_status(
    process: subprocess.Popen,
    timeout: float = _REMOTE_ATTACH_TIMEOUT,
) -> RemoteAttachKind:
    """Read one post-start status without consuming the first display frame."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    received = bytearray()
    limit = max(len(REMOTE_ATTACH_ACCEPTED), len(REMOTE_ATTACH_BUSY)) + 2
    while len(received) < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RemoteAttachKind.TIMEOUT
        readable, _writable, _exceptional = select.select(
            [process.stdout.fileno()], [], [], remaining)
        if not readable:
            return RemoteAttachKind.TIMEOUT
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            process.wait()
            return RemoteAttachKind.FAILED
        received.extend(chunk)
        if chunk != b"\n":
            continue
        line = bytes(received)
        if line == REMOTE_ATTACH_ACCEPTED:
            return RemoteAttachKind.ACCEPTED
        if line == REMOTE_ATTACH_BUSY:
            return RemoteAttachKind.BUSY
        return RemoteAttachKind.FAILED
    return RemoteAttachKind.FAILED


def parse_remote_hello(line: bytes) -> RemoteHello:
    """Parse one bounded, untrusted compatibility line from the remote."""
    if not line.startswith(REMOTE_HELLO_PREFIX):
        raise ValueError("not a Railmux remote hello")
    payload = line[len(REMOTE_HELLO_PREFIX):].rstrip(b"\r\n")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid Railmux remote hello") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid Railmux remote hello")
    version = value.get("version")
    protocol = value.get("protocol")
    ready = value.get("ready")
    tmux = value.get("tmux")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 128
        or not isinstance(protocol, int)
        or isinstance(protocol, bool)
        or not 1 <= protocol <= 65535
        or not isinstance(ready, bool)
        or not isinstance(tmux, bool)
    ):
        raise ValueError("invalid Railmux remote hello")
    try:
        version.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid Railmux remote version") from exc
    return RemoteHello(version, protocol, ready, tmux)


def await_remote_startup(
    process: subprocess.Popen,
    timeout: float = _REMOTE_HELLO_TIMEOUT,
) -> RemoteStartup:
    """Wait before raw mode until the remote proves its compatibility state."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    received = bytearray()
    line_start = 0
    while len(received) < _REMOTE_HELLO_LIMIT:
        magic_start = received.find(_DISPLAY_MAGIC_PREFIX)
        if magic_start >= 0:
            magic_end = magic_start + len(DISPLAY_MAGIC)
            if len(received) >= magic_end:
                return RemoteStartup(RemoteStartKind.FAILED)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        readable, _writable, _exceptional = select.select(
            [process.stdout.fileno()], [], [], remaining
        )
        if not readable:
            return RemoteStartup(RemoteStartKind.TIMEOUT)
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            returncode = process.wait()
            kind = (
                RemoteStartKind.MISSING
                if returncode == 127
                else RemoteStartKind.FAILED
            )
            return RemoteStartup(kind, returncode=returncode)
        received.extend(chunk)
        if chunk != b"\n":
            continue
        line = bytes(received[line_start:])
        line_start = len(received)
        marker = line.find(REMOTE_HELLO_PREFIX)
        if marker < 0:
            continue
        try:
            hello = parse_remote_hello(line[marker:])
        except ValueError:
            return RemoteStartup(RemoteStartKind.FAILED)
        return RemoteStartup(RemoteStartKind.HELLO, hello=hello)
    return RemoteStartup(RemoteStartKind.FAILED)


@dataclass(frozen=True)
class SgrMouseEvent:
    raw: bytes
    button: int
    x: int
    y: int
    pressed: bool

    @property
    def wheel_direction(self) -> int:
        base_button = self.button & 3
        if not self.pressed or not self.button & 64 or base_button not in (0, 1):
            return 0
        return -1 if base_button == 1 else 1

    def translated_y(self, offset: int) -> "SgrMouseEvent":
        """Translate a local projected row back into remote screen space."""
        if offset == 0:
            return self
        y = self.y + offset
        terminator = b"M" if self.pressed else b"m"
        raw = (
            _SGR_MOUSE_PREFIX
            + f"{self.button};{self.x};{y}".encode()
            + terminator
        )
        return replace(self, raw=raw, y=y)


class TerminalInputDecoder:
    """Split bounded SGR mouse reports from otherwise opaque terminal bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_since: float | None = None

    def _finish(
        self, parts: list[bytes | SgrMouseEvent],
    ) -> list[bytes | SgrMouseEvent]:
        if self._buffer:
            if self._pending_since is None:
                self._pending_since = time.monotonic()
        else:
            self._pending_since = None
        return parts

    @staticmethod
    def _append_bytes(parts: list[bytes | SgrMouseEvent], data: bytes) -> None:
        if not data:
            return
        if parts and isinstance(parts[-1], bytes):
            parts[-1] += data
        else:
            parts.append(data)

    def feed(self, data: bytes) -> list[bytes | SgrMouseEvent]:
        self._buffer.extend(data)
        parts: list[bytes | SgrMouseEvent] = []
        while self._buffer:
            marker = self._buffer.find(_SGR_MOUSE_PREFIX)
            if marker < 0:
                keep = 0
                for size in range(1, min(len(self._buffer), len(_SGR_MOUSE_PREFIX) - 1) + 1):
                    if self._buffer[-size:] == _SGR_MOUSE_PREFIX[:size]:
                        keep = size
                emit = len(self._buffer) - keep
                self._append_bytes(parts, bytes(self._buffer[:emit]))
                del self._buffer[:emit]
                return self._finish(parts)
            if marker:
                self._append_bytes(parts, bytes(self._buffer[:marker]))
                del self._buffer[:marker]
            end = next(
                (
                    index
                    for index, value in enumerate(self._buffer[len(_SGR_MOUSE_PREFIX):], len(_SGR_MOUSE_PREFIX))
                    if value in (ord("M"), ord("m"))
                ),
                None,
            )
            if end is None:
                if len(self._buffer) <= 64:
                    return self._finish(parts)
                self._append_bytes(parts, bytes((self._buffer[0],)))
                del self._buffer[0]
                continue
            raw = bytes(self._buffer[:end + 1])
            del self._buffer[:end + 1]
            fields = raw[len(_SGR_MOUSE_PREFIX):-1].split(b";")
            try:
                if len(fields) != 3:
                    raise ValueError
                button, x, y = (int(field) for field in fields)
                if not 0 <= button <= 255 or not 1 <= x <= 1000 or not 1 <= y <= 500:
                    raise ValueError
            except ValueError:
                self._append_bytes(parts, raw)
                continue
            parts.append(SgrMouseEvent(raw, button, x, y, raw[-1:] == b"M"))
        return self._finish(parts)

    def next_timeout(self, maximum: float = 0.1, delay: float = 0.02) -> float:
        if self._pending_since is None:
            return maximum
        remaining = delay - (time.monotonic() - self._pending_since)
        return max(0.0, min(maximum, remaining))

    def flush_pending(self, delay: float = 0.02) -> list[bytes]:
        if (
            not self._buffer
            or self._pending_since is None
            or time.monotonic() - self._pending_since < delay
        ):
            return []
        data = bytes(self._buffer)
        self._buffer.clear()
        self._pending_since = None
        return [data]


@dataclass(frozen=True)
class HistoryAction:
    protocol_frame: bytes = b""
    forwarded_input: bytes = b""
    render_history: bool = False
    restore_live: bool = False
    refresh_routes: bool = False


@dataclass
class _HistoryViewport:
    """One immutable pane snapshot plus its local offset from the bottom."""

    snapshot: HistorySnapshot
    offset: int


class LocalHistoryView:
    """Keep bounded history content separate from visible pointer routes."""

    def __init__(self) -> None:
        self.viewports: dict[str, _HistoryViewport] = {}
        self._deep_pending: dict[int, tuple[int, str]] = {}
        self.prefetch_pending_id: int | None = None
        self.prefetch_pending_epoch: int | None = None
        self.prefetch_started = 0.0
        self.visible_routes: tuple[HistorySnapshot, ...] = ()
        self.content_cache: dict[str, HistorySnapshot] = {}
        self.route_epoch = 1
        self._local_pointer_capture = False
        self._forwarded_pointer_capture = False
        self._suppress_forwarded_drag = False
        self._next_request_id = 1

    @property
    def active(self) -> bool:
        return bool(self.viewports)

    @property
    def pending(self) -> bool:
        return bool(self._deep_pending)

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id = (request_id + 1) & 0xFFFFFFFF
        if self._next_request_id == 0:
            self._next_request_id = 1
        return request_id

    def begin_prefetch(self, now: float) -> bytes:
        if (
            self.prefetch_pending_id is not None
            and now - self.prefetch_started < _HISTORY_PREFETCH_TIMEOUT
        ):
            return b""
        request_id = self._allocate_request_id()
        self.prefetch_pending_id = request_id
        self.prefetch_pending_epoch = self.route_epoch
        self.prefetch_started = now
        return encode_history_prefetch(request_id, _HISTORY_PREFETCH_LINES)

    def accept_prefetch(self, batch: HistoryBatch) -> HistoryAction:
        if (
            batch.request_id != self.prefetch_pending_id
            or self.prefetch_pending_epoch != self.route_epoch
        ):
            return HistoryAction()
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        # Replacement is atomic: hidden/removed panes immediately stop being
        # pointer targets, while their bounded text may remain reusable.
        self.visible_routes = batch.snapshots
        for snapshot in batch.snapshots:
            if snapshot.pane_id is not None:
                self._remember_content(snapshot)
        routes = {
            route.pane_id: route
            for route in self.visible_routes
            if route.pane_id is not None
        }
        restore_live = False
        for pane_id, viewport in tuple(self.viewports.items()):
            route = routes.get(pane_id)
            if route is None or not self._same_geometry(
                viewport.snapshot, route
            ):
                self.cancel_pane(pane_id)
                restore_live = True
        return HistoryAction(restore_live=restore_live)

    def _remember_content(self, snapshot: HistorySnapshot) -> None:
        assert snapshot.pane_id is not None
        # Reinsert an existing pane to keep insertion order as recency order.
        self.content_cache.pop(snapshot.pane_id, None)
        self.content_cache[snapshot.pane_id] = snapshot
        while len(self.content_cache) > _HISTORY_CONTENT_PANES:
            del self.content_cache[next(iter(self.content_cache))]

    def invalidate_routes(self) -> bool:
        """Drop pointer authority without discarding bounded pane content."""
        was_active = self.cancel()
        self.route_epoch = (self.route_epoch + 1) & 0xFFFFFFFF
        if self.route_epoch == 0:
            self.route_epoch = 1
        self.visible_routes = ()
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        return was_active

    def clear_cache(self) -> None:
        self.invalidate_routes()
        self.content_cache.clear()

    def _route_at(self, event: SgrMouseEvent) -> HistorySnapshot | None:
        return self._route_at_position(event.x - 1, event.y - 1)

    @staticmethod
    def _contains_position(
        snapshot: HistorySnapshot, x: int, y: int,
    ) -> bool:
        return (
            snapshot.x <= x < snapshot.x + snapshot.width
            and snapshot.y <= y < snapshot.y + snapshot.height
        )

    def _route_at_position(self, x: int, y: int) -> HistorySnapshot | None:
        return next(
            (
                route
                for route in self.visible_routes
                if self._contains_position(route, x, y)
            ),
            None,
        )

    def pane_id_at_position(self, x: int, y: int) -> str | None:
        route = self._route_at_position(x, y)
        return None if route is None else route.pane_id

    @staticmethod
    def _same_geometry(left: HistorySnapshot, right: HistorySnapshot) -> bool:
        return (
            left.pane_id == right.pane_id
            and left.x == right.x
            and left.y == right.y
            and left.width == right.width
            and left.height == right.height
        )

    def _start_history(
        self,
        route: HistorySnapshot,
        event: SgrMouseEvent,
    ) -> HistoryAction:
        assert route.pane_id is not None
        cached = self.content_cache.get(route.pane_id, route)
        if not self._same_geometry(cached, route):
            cached = route
        maximum = max(0, len(cached.lines) - cached.height)
        if maximum == 0:
            return HistoryAction()
        self.cancel_pane(route.pane_id)
        self.viewports[route.pane_id] = _HistoryViewport(
            cached, min(maximum, _HISTORY_SCROLL_LINES)
        )
        request_id = self._allocate_request_id()
        self._deep_pending[request_id] = (self.route_epoch, route.pane_id)
        return HistoryAction(
            protocol_frame=encode_history_request(
                request_id, event.x, event.y, _HISTORY_FULL_LINES
            ),
            render_history=True,
        )

    def wheel(self, event: SgrMouseEvent) -> HistoryAction:
        direction = event.wheel_direction
        if direction == 0:
            return HistoryAction(forwarded_input=event.raw)
        route = self._route_at(event)
        if route is None:
            return HistoryAction(forwarded_input=event.raw)
        assert route.pane_id is not None
        viewport = self.viewports.get(route.pane_id)
        if viewport is not None:
            maximum = max(
                0, len(viewport.snapshot.lines) - viewport.snapshot.height
            )
            viewport.offset = max(
                0,
                min(
                    maximum,
                    viewport.offset + direction * _HISTORY_SCROLL_LINES,
                ),
            )
            if viewport.offset == 0:
                self.cancel_pane(route.pane_id)
                return HistoryAction(restore_live=True)
            return HistoryAction(render_history=True)
        # Once a pointer is known to be over an agent pane, the local history
        # layer exclusively owns vertical wheel input. This avoids also
        # triggering tmux copy-mode or its pane scroll bindings.
        if direction < 0:
            return HistoryAction()
        return self._start_history(route, event)

    def pointer_event(
        self,
        event: SgrMouseEvent,
        focused_pane_id: str | None = None,
        status_row: int | None = None,
    ) -> HistoryAction:
        if status_row is not None and event.y == status_row:
            # The tmux status line is navigation chrome, never agent history.
            # Forward it even if a prior local selection capture missed its
            # release or a stale pane route briefly overlaps the bottom row.
            # A press can switch compact pages, so invalidate route geometry
            # immediately; the next prefetch repopulates the new visible pane.
            changes_page = event.pressed and not event.button & 32
            restore_live = self.invalidate_routes() if changes_page else False
            return HistoryAction(
                forwarded_input=event.raw,
                restore_live=restore_live,
                refresh_routes=changes_page,
            )
        if self._forwarded_pointer_capture:
            if not event.pressed:
                self._forwarded_pointer_capture = False
                self._suppress_forwarded_drag = False
            elif self._suppress_forwarded_drag and event.wheel_direction:
                # Keep agent wheel input local even while a click capture is
                # active. If the pointer has moved to the sidebar, wheel()
                # finds no agent route and preserves normal forwarding.
                return self.wheel(event)
            elif self._suppress_forwarded_drag and event.button & 32:
                # A press that began over an agent is forwarded so tmux can
                # focus that pane. Do not forward its motion reports: tmux's
                # stock MouseDrag1Pane binding would otherwise enter copy-mode
                # implicitly. Explicit Ctrl-B [ remains opaque keyboard input.
                return HistoryAction()
            return HistoryAction(forwarded_input=event.raw)
        if self._local_pointer_capture:
            if not event.pressed:
                self._local_pointer_capture = False
            return HistoryAction()
        if event.wheel_direction:
            return self.wheel(event)
        frozen = next(
            (
                viewport.snapshot
                for viewport in self.viewports.values()
                if self._contains_position(
                    viewport.snapshot, event.x - 1, event.y - 1
                )
            ),
            None,
        )
        if frozen is not None:
            if event.pressed and not event.button & 32:
                if frozen.pane_id != focused_pane_id:
                    self._forwarded_pointer_capture = True
                    self._suppress_forwarded_drag = True
                    return HistoryAction(
                        forwarded_input=event.raw,
                        refresh_routes=True,
                    )
                self._local_pointer_capture = True
            return HistoryAction()
        if event.pressed and not event.button & 32:
            if self._route_at(event) is not None:
                self._forwarded_pointer_capture = True
                self._suppress_forwarded_drag = True
                return HistoryAction(
                    forwarded_input=event.raw,
                    refresh_routes=True,
                )
            restore_live = self.invalidate_routes()
            self._forwarded_pointer_capture = True
            self._suppress_forwarded_drag = False
            return HistoryAction(
                forwarded_input=event.raw,
                restore_live=restore_live,
                refresh_routes=True,
            )
        return HistoryAction()

    def accept(self, snapshot: HistorySnapshot) -> HistoryAction:
        pending = self._deep_pending.pop(snapshot.request_id, None)
        if pending is None:
            return HistoryAction()
        pending_epoch, pane_id = pending
        if pending_epoch != self.route_epoch or snapshot.pane_id != pane_id:
            return HistoryAction()
        route = next(
            (
                route
                for route in self.visible_routes
                if route.pane_id == snapshot.pane_id
            ),
            None,
        )
        if route is None or not self._same_geometry(route, snapshot):
            return HistoryAction()
        self._remember_content(snapshot)
        viewport = self.viewports.get(pane_id)
        if viewport is None:
            return HistoryAction()
        maximum = max(0, len(snapshot.lines) - snapshot.height)
        if maximum == 0:
            self.cancel_pane(pane_id)
            return HistoryAction(restore_live=True)
        anchor = self._visible_lines(viewport)
        aligned_offset = self._aligned_offset(snapshot, anchor)
        if aligned_offset is None:
            # The live pane moved while the deep capture was in flight and no
            # unique exact visible anchor survived. Keep the immutable hot
            # snapshot instead of jumping to newer or unrelated text.
            return HistoryAction()
        viewport.snapshot = snapshot
        viewport.offset = aligned_offset
        return HistoryAction(render_history=True)

    @staticmethod
    def _visible_lines(viewport: _HistoryViewport) -> tuple[bytes, ...]:
        snapshot = viewport.snapshot
        end = len(snapshot.lines) - viewport.offset
        start = max(0, end - snapshot.height)
        lines = snapshot.lines[start:end]
        if len(lines) < snapshot.height:
            lines = (b"",) * (snapshot.height - len(lines)) + lines
        return lines

    @staticmethod
    def _aligned_offset(
        snapshot: HistorySnapshot, anchor: tuple[bytes, ...],
    ) -> int | None:
        if not anchor or len(anchor) > len(snapshot.lines):
            return None
        matched_offset: int | None = None
        for start in range(len(snapshot.lines) - len(anchor), -1, -1):
            if snapshot.lines[start:start + len(anchor)] == anchor:
                if matched_offset is not None:
                    return None
                matched_offset = len(snapshot.lines) - (start + len(anchor))
        return matched_offset

    def overlays(
        self,
    ) -> tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...]:
        return tuple(
            (viewport.snapshot, self._visible_lines(viewport))
            for viewport in self.viewports.values()
        )

    def cancel_pane(self, pane_id: str) -> bool:
        was_active = self.viewports.pop(pane_id, None) is not None
        self._deep_pending = {
            request_id: pending
            for request_id, pending in self._deep_pending.items()
            if pending[1] != pane_id
        }
        return was_active

    def cancel_for_input(self, x: int, y: int) -> bool:
        """Restore only the input pane, or all panes if routing is unknown."""
        route = self._route_at_position(x, y)
        if route is None or route.pane_id is None:
            return self.cancel()
        return self.cancel_pane(route.pane_id)

    def cancel(self) -> bool:
        was_active = self.active
        self.viewports.clear()
        self._deep_pending.clear()
        self._local_pointer_capture = False
        self._forwarded_pointer_capture = False
        self._suppress_forwarded_drag = False
        return was_active


def coalesce_forwarded_wheel(
    action: HistoryAction,
    event: SgrMouseEvent,
    forwarded_directions: set[int],
) -> HistoryAction:
    """Bound one read's remote vertical-wheel burst without a time heuristic."""
    direction = event.wheel_direction
    if not action.forwarded_input or direction == 0:
        return action
    if direction in forwarded_directions:
        return replace(action, forwarded_input=b"")
    forwarded_directions.add(direction)
    return action


def input_may_change_routes(data: bytes, *, routes_visible: bool) -> bool:
    """Recognize bounded Railmux layout/modal keys without taxing agent typing."""
    if b"\x1b[19~" in data or b"\x1b[20~" in data or data == b"?":
        return True
    return not routes_visible and data in (b"\x1b", b"\r", b"\n")


def split_local_escape(data: bytes) -> tuple[bytes, bool]:
    """Return bytes before Ctrl-] and whether an emergency exit was found."""
    escape_at = data.find(LOCAL_ESCAPE)
    if escape_at < 0:
        return data, False
    return data[:escape_at], True


def _terminal_size_is_usable(size: os.terminal_size) -> bool:
    return (
        size.columns >= _MIN_TERMINAL_COLUMNS
        and size.lines >= _MIN_TERMINAL_LINES
    )


def _terminal_size_exceeds_limits(size: os.terminal_size) -> bool:
    return (
        size.columns > _MAX_TERMINAL_COLUMNS
        or size.lines > _MAX_TERMINAL_LINES
    )


def wait_for_usable_terminal_size(fd: int) -> os.terminal_size:
    """Wait in cooked mode for a soft-keyboard-sized terminal to recover."""
    reported: os.terminal_size | None = None
    while True:
        size = os.get_terminal_size(fd)
        if _terminal_size_exceeds_limits(size):
            raise ProbeError(
                "local terminal reports "
                f"{size.columns}x{size.lines}; SSH display limits are "
                f"{_MAX_TERMINAL_COLUMNS}x{_MAX_TERMINAL_LINES}"
            )
        if _terminal_size_is_usable(size):
            if reported is not None:
                print(
                    "railmux ssh: local terminal is now "
                    f"{size.columns}x{size.lines}; continuing",
                    file=sys.stderr,
                )
            return size
        if size.columns < _MIN_TERMINAL_COLUMNS:
            raise ProbeError(
                "local terminal reports "
                f"{size.columns}x{size.lines}; SSH display requires at least "
                f"{_MIN_TERMINAL_COLUMNS}x{_MIN_TERMINAL_LINES}"
            )
        if size != reported:
            print(
                "railmux ssh: local terminal reports "
                f"{size.columns}x{size.lines}; waiting for at least "
                f"{_MIN_TERMINAL_COLUMNS}x{_MIN_TERMINAL_LINES} "
                "(hide the soft keyboard; Ctrl-C cancels)",
                file=sys.stderr,
            )
            reported = size
        time.sleep(_TERMINAL_SIZE_POLL_INTERVAL)


def _is_soft_keyboard_projection(
    physical_size: os.terminal_size,
    logical_size: os.terminal_size,
) -> bool:
    """Recognize the same-width, short-height resize used by soft keyboards."""
    return (
        physical_size.columns == logical_size.columns
        and 0 < physical_size.lines < _MIN_TERMINAL_LINES
    )


class RawTerminal:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: Optional[list[object]] = None

    def __enter__(self) -> "RawTerminal":
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.saved = None


class TerminalSurface:
    """Paint a server-rendered screen and unconditionally restore the TTY."""

    def __init__(self, stream: BinaryIO, *, mouse: bool = True) -> None:
        self.stream = stream
        self.mouse = mouse
        self.active = False
        self.terminal_modes = TerminalMode.NONE
        self.physical_size: os.terminal_size | None = None

    def set_physical_size(self, size: os.terminal_size) -> None:
        """Set the local viewport without changing the remote screen geometry."""
        self.physical_size = size

    def _projection(self, screen_height: int) -> tuple[int, int]:
        visible_height = screen_height
        if self.physical_size is not None:
            visible_height = min(visible_height, self.physical_size.lines)
        visible_height = max(0, visible_height)
        return screen_height - visible_height, visible_height

    def translate_mouse_event(
        self,
        event: SgrMouseEvent,
        *,
        logical_height: int,
    ) -> SgrMouseEvent:
        """Map an SGR report from the bottom-anchored viewport to tmux."""
        top, _visible_height = self._projection(logical_height)
        return event.translated_y(top)

    def start(self) -> None:
        if self.active:
            return
        controls = [b"\033[?1049h\033[2J\033[H\033[?25l"]
        if self.mouse:
            # Button-event tracking includes wheel and drag events. SGR mode
            # preserves coordinates beyond the legacy X10 limit.
            controls.append(b"\033[?1002h\033[?1006h")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.active = True

    def _reconcile_terminal_modes(
        self, requested: TerminalMode,
    ) -> TerminalMode:
        """Mirror only input-affecting modes explicitly carried by protocol v7."""
        disabled = self.terminal_modes & ~requested
        enabled = requested & ~self.terminal_modes
        controls: list[bytes] = []
        for mode, disable, enable in (
            (TerminalMode.BRACKETED_PASTE, b"\033[?2004l", b"\033[?2004h"),
            (TerminalMode.FOCUS_EVENTS, b"\033[?1004l", b"\033[?1004h"),
        ):
            if disabled & mode:
                controls.append(disable)
            if enabled & mode:
                controls.append(enable)
        if controls:
            self.stream.write(b"".join(controls))
            self.stream.flush()
        self.terminal_modes = requested
        return enabled

    @staticmethod
    def _cursor_is_covered(
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
    ) -> bool:
        return any(
            snapshot.x <= screen.cursor_x < snapshot.x + snapshot.width
            and snapshot.y <= screen.cursor_y < snapshot.y + snapshot.height
            for snapshot, _lines in overlays
        )

    @staticmethod
    def _append_overlay_rows(
        rendered: list[bytes],
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        *,
        projection_top: int = 0,
        visible_height: int | None = None,
        changed_rows: frozenset[int] | None = None,
    ) -> None:
        for snapshot, lines in overlays:
            for index in range(snapshot.height):
                row = snapshot.y + index
                if (
                    visible_height is not None
                    and not projection_top
                    <= row
                    < projection_top + visible_height
                ):
                    continue
                if changed_rows is not None and row not in changed_rows:
                    continue
                line = lines[index] if index < len(lines) else b""
                rendered.extend((
                    (
                        f"\033[{row - projection_top + 1};"
                        f"{snapshot.x + 1}H"
                    ).encode(),
                    f"\033[{snapshot.width}X".encode(),
                    line,
                ))

    @classmethod
    def _append_cursor(
        cls,
        rendered: list[bytes],
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
        *,
        projection_top: int = 0,
        visible_height: int | None = None,
    ) -> None:
        cursor_in_projection = (
            visible_height is None
            or projection_top
            <= screen.cursor_y
            < projection_top + visible_height
        )
        rendered.extend((
            b"\033[0m\033[?7h",
            (
                f"\033[{screen.cursor_y - projection_top + 1};"
                f"{screen.cursor_x + 1}H"
            ).encode()
            if cursor_in_projection
            else b"\033[1;1H",
            (
                b"\033[?25h"
                if screen.cursor_visible
                and cursor_in_projection
                and not cls._cursor_is_covered(screen, overlays)
                else b"\033[?25l"
            ),
        ))

    def paint(
        self,
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...] = (),
    ) -> bool:
        """Paint a screen and report whether focus reporting was just enabled."""
        self.start()
        enabled_modes = self._reconcile_terminal_modes(screen.terminal_modes)
        projection_top, visible_height = self._projection(screen.height)
        rendered = [b"\033[?7l"]
        if screen.clear:
            rendered.append(b"\033[0m\033[2J")
        for row_index in screen.changed_rows:
            if not (
                projection_top
                <= row_index
                < projection_top + visible_height
            ):
                continue
            rendered.extend((
                f"\033[{row_index - projection_top + 1};1H".encode(),
                b"\033[2K",
                screen.rows[row_index],
            ))
        self._append_overlay_rows(
            rendered,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
            changed_rows=frozenset(screen.changed_rows),
        )
        self._append_cursor(
            rendered,
            screen,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()
        return bool(enabled_modes & TerminalMode.FOCUS_EVENTS)

    def paint_overlays(
        self,
        screen: AppliedScreen,
        overlays: tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...],
    ) -> None:
        self.start()
        projection_top, visible_height = self._projection(screen.height)
        rendered: list[bytes] = [b"\033[?7l"]
        self._append_overlay_rows(
            rendered,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self._append_cursor(
            rendered,
            screen,
            overlays,
            projection_top=projection_top,
            visible_height=visible_height,
        )
        self.stream.write(b"".join(rendered))
        self.stream.flush()

    def close(self) -> None:
        if not self.active:
            return
        controls = [b"\033[0m\033[?7h\033[?25h"]
        if self.terminal_modes & TerminalMode.BRACKETED_PASTE:
            controls.append(b"\033[?2004l")
        if self.terminal_modes & TerminalMode.FOCUS_EVENTS:
            controls.append(b"\033[?1004l")
        if self.mouse:
            controls.append(b"\033[?1002l\033[?1006l")
        controls.append(b"\033[?1049l")
        self.stream.write(b"".join(controls))
        self.stream.flush()
        self.terminal_modes = TerminalMode.NONE
        self.active = False


def _remote_server_args(
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    replace_existing_client: bool = False,
) -> list[str]:
    args = [
        "remote-server",
        "--protocol", str(PROTOCOL_VERSION),
        "--session", session,
        "--width", str(width),
        "--height", str(height),
        "--fps", str(fps),
    ]
    if replace_existing_client:
        args.append("--replace-existing-client")
    return args


def _remote_launch_command(server_args: Sequence[str]) -> str:
    direct = shlex.join(["railmux", *server_args])
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_args = shlex.join(["-m", "railmux", *server_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -c 'import railmux' >/dev/null 2>&1; "
        f"then exec {managed_python} {managed_args}",
        "elif command -v railmux >/dev/null 2>&1; "
        f"then exec {direct}",
    ]
    for python in ("python3", "python"):
        probe = shlex.join([python, "-c", "import railmux"])
        launch = shlex.join([python, "-m", "railmux", *server_args])
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {probe} >/dev/null 2>&1; then exec {launch}"
        )
    branches.append("else exit 127; fi")
    return "; ".join(branches)


def build_ssh_argv(
    destination: str,
    *,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
    replace_existing_client: bool = False,
) -> list[str]:
    server_args = _remote_server_args(
        session=session,
        width=width,
        height=height,
        fps=fps,
        replace_existing_client=replace_existing_client,
    )
    command = _remote_launch_command(server_args)
    return ["ssh", "-T", *ssh_args, destination, command]


def build_ssh_install_argv(
    destination: str,
    *,
    version: str,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
) -> list[str]:
    """Install into the remote user environment, then exec the same session."""
    server_args = _remote_server_args(
        session=session, width=width, height=height, fps=fps
    )
    requirement = f"railmux[ssh]=={version}"
    managed_python = f'"$HOME/{_REMOTE_VENV}/bin/python"'
    managed_install = shlex.join([
        "-m", "pip", "install", "--upgrade", requirement,
    ])
    managed_launch = shlex.join(["-m", "railmux", *server_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {managed_install} 1>&2 "
        f"&& exec {managed_python} {managed_launch}; exit $?"
    ]
    candidates = (
        (("python3", "-m", "pip"), ("python3", "-m", "railmux")),
        (("python", "-m", "pip"), ("python", "-m", "railmux")),
        (("pip3",), ("python3", "-m", "railmux")),
        (("pip",), ("python", "-m", "railmux")),
    )
    for installer, runner in candidates:
        executable = installer[0]
        runner_executable = runner[0]
        pip_probe = shlex.join([*installer, "--version"])
        condition = (
            f"command -v {executable} >/dev/null 2>&1 "
            f"&& {pip_probe} >/dev/null 2>&1"
        )
        if runner_executable != executable:
            condition += (
                f" && command -v {runner_executable} >/dev/null 2>&1"
            )
        install = shlex.join([
            *installer,
            "install",
            "--user",
            "--upgrade",
            requirement,
        ])
        launch = shlex.join([*runner, *server_args])
        branches.append(
            f"elif {condition}; then {install} 1>&2 "
            f"&& exec {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python/pip, python3/pip3, or pip was found' "
        ">&2; exit 127; fi"
    )
    return ["ssh", "-T", *ssh_args, destination, "; ".join(branches)]


def build_ssh_private_venv_install_argv(
    destination: str,
    *,
    version: str,
    session: str,
    width: int,
    height: int,
    fps: float,
    ssh_args: Sequence[str],
) -> list[str]:
    """Create Railmux's private remote venv, install, and start it."""
    server_args = _remote_server_args(
        session=session, width=width, height=height, fps=fps
    )
    requirement = f"railmux[ssh]=={version}"
    managed_dir = f'"$HOME/{_REMOTE_VENV}"'
    managed_python = f"{managed_dir}/bin/python"
    install = shlex.join([
        "-m", "pip", "install", "--upgrade", requirement,
    ])
    launch = shlex.join(["-m", "railmux", *server_args])
    branches = [
        f"if [ -x {managed_python} ] "
        f"&& {managed_python} -m pip --version >/dev/null 2>&1; "
        f"then {managed_python} {install} 1>&2 "
        f"&& exec {managed_python} {launch}; exit $?"
    ]
    for python in ("python3", "python"):
        branches.append(
            f"elif command -v {python} >/dev/null 2>&1 "
            f"&& {python} -m venv {managed_dir} 1>&2; then "
            f"{managed_python} {install} 1>&2 "
            f"&& exec {managed_python} {launch}; exit $?"
        )
    branches.append(
        "else echo 'error: no usable python3 or python was found to create "
        "the private Railmux environment' >&2; exit 127; fi"
    )
    return ["ssh", "-T", *ssh_args, destination, "; ".join(branches)]


def remote_install_help(destination: str, version: str) -> str:
    requirement = shlex.quote(f"railmux[ssh]=={version}")
    return (
        f"Install it manually on {destination}, then retry:\n"
        f"  python3 -m pip install --user --upgrade {requirement}\n"
        f"or:\n  pip3 install --user --upgrade {requirement}\n"
        "These commands use per-user site packages and do not modify the "
        "system Python. If site policy still rejects them, use a private "
        "Railmux environment:\n"
        f"  python3 -m venv ~/{_REMOTE_VENV}\n"
        f"  ~/{_REMOTE_VENV}/bin/python -m pip install --upgrade {requirement}\n"
        "If that version is not published, copy the matching wheel or source "
        "checkout to the remote host and install it with the same Python."
    )


def remote_tmux_help(destination: str) -> str:
    return (
        f"tmux is not installed or not on PATH on {destination}. Install it "
        "with the remote operating system's package manager, then retry. "
        "Railmux will not run sudo or install system packages automatically."
    )


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _stop_unstarted_remote(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _local_upgrade_argv(version: str) -> list[str]:
    from railmux.self_update import upgrade_argv
    return upgrade_argv(version)


def _upgrade_local_and_restart(version: str, raw_args: Sequence[str]) -> NoReturn:
    argv = _local_upgrade_argv(version)
    print(
        f"railmux ssh: upgrading local Railmux to {version}...",
        file=sys.stderr,
    )
    try:
        result = subprocess.run(argv, check=False)
    except OSError as exc:
        raise ProbeError(
            f"could not start local pip: {exc}\nRun manually:\n  "
            f"{shlex.join(argv)}"
        ) from exc
    if result.returncode:
        raise ProbeError(
            "local Railmux upgrade failed. Run manually, then retry:\n  "
            f"{shlex.join(argv)}"
        )
    restart = [sys.executable, "-m", "railmux", "ssh", *raw_args]
    print("railmux ssh: local upgrade succeeded; restarting...", file=sys.stderr)
    try:
        os.execv(sys.executable, restart)
    except OSError as exc:
        raise ProbeError(
            "local upgrade succeeded but Railmux could not restart; run:\n  "
            f"{shlex.join(restart)}"
        ) from exc
    raise AssertionError("os.execv returned unexpectedly")


def _spawn_remote(argv: Sequence[str]) -> subprocess.Popen:
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
    except OSError as exc:
        raise ProbeError(f"could not start ssh: {exc}") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    return process


def _install_remote_and_start(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    version: str,
) -> tuple[subprocess.Popen, RemoteStartup]:
    install_argv = build_ssh_install_argv(
        args.destination,
        version=version,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
    )
    process = _spawn_remote(install_argv)
    startup = await_remote_startup(process)
    return process, startup


def _install_remote_private_venv_and_start(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    version: str,
) -> tuple[subprocess.Popen, RemoteStartup]:
    install_argv = build_ssh_private_venv_install_argv(
        args.destination,
        version=version,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
    )
    process = _spawn_remote(install_argv)
    startup = await_remote_startup(process)
    return process, startup


def _version_pair(remote_version: str) -> tuple[Version, Version] | None:
    try:
        return Version(__version__), Version(remote_version)
    except InvalidVersion:
        return None


def _confirm_remote_install(
    args: argparse.Namespace,
    reason: str,
    version: str,
) -> bool:
    return _confirm(
        f"{reason} Install Railmux {version} with SSH support into "
        f"the remote user environment on {args.destination}?"
    )


def _confirm_remote_private_venv_install(
    args: argparse.Namespace,
    version: str,
) -> bool:
    return _confirm(
        "Remote user-site installation failed or timed out. Create the isolated "
        f"~/{_REMOTE_VENV} environment and install Railmux {version} there? "
        "This does not use sudo or modify the system Python."
    )


def _send_start(process: subprocess.Popen) -> None:
    try:
        process.stdin.write(REMOTE_START)
        process.stdin.flush()
    except BrokenPipeError as exc:
        raise ProbeError(
            "remote Railmux exited before accepting the display"
        ) from exc


def _reconnect_remote_attach(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    *,
    replace_existing_client: bool,
) -> tuple[subprocess.Popen, RemoteAttachKind]:
    """Start one already-negotiated helper and return its attach status."""
    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
        replace_existing_client=replace_existing_client,
    )
    process = _spawn_remote(argv)
    try:
        startup = await_remote_startup(process)
        hello = startup.hello
        if (startup.kind is not RemoteStartKind.HELLO
                or hello is None
                or hello.protocol != PROTOCOL_VERSION
                or not hello.ready
                or not hello.tmux):
            raise ProbeError(
                "reconnect could not start a compatible remote display; "
                "the Railmux session and agents were left intact"
            )
        _send_start(process)
        status = await_remote_attach_status(process)
    except Exception:
        _stop_unstarted_remote(process)
        raise
    return process, status


def _finish_remote_attach(
    args: argparse.Namespace,
    current_size: os.terminal_size,
    process: subprocess.Popen,
) -> subprocess.Popen:
    """Complete the cooked-mode attach handshake and one consented takeover."""
    try:
        _send_start(process)
    except ProbeError:
        _stop_unstarted_remote(process)
        raise
    status = await_remote_attach_status(process)
    if status is RemoteAttachKind.ACCEPTED:
        return process
    if status is RemoteAttachKind.TIMEOUT:
        _stop_unstarted_remote(process)
        raise ProbeError("timed out waiting for the remote display to attach")
    if status is not RemoteAttachKind.BUSY:
        _stop_unstarted_remote(process)
        raise ProbeError("remote display helper failed before attaching")

    _stop_unstarted_remote(process)
    # A current v7 helper holds the mutex only while registering its exact tmux
    # child. Give that ordinary race one fresh SSH process before presenting
    # the explicit legacy-lock takeover choice.
    time.sleep(_REMOTE_ATTACH_RETRY_DELAY)
    retry, retry_status = _reconnect_remote_attach(
        args, current_size, replace_existing_client=False)
    if retry_status is RemoteAttachKind.ACCEPTED:
        return retry
    _stop_unstarted_remote(retry)
    if retry_status is RemoteAttachKind.TIMEOUT:
        raise ProbeError("timed out while retrying the remote display attach")
    if retry_status is not RemoteAttachKind.BUSY:
        raise ProbeError("remote display helper failed while retrying attach")

    if not _confirm(
        "Another display helper is persistently holding the attach lock. "
        "Replace "
        "it? This detaches every terminal currently attached to the same "
        "managed Railmux session, but keeps the session and agents alive."
    ):
        raise ProbeError(
            "remote Railmux is still owned by an older display client; "
            "retry after it exits, or reconnect and approve replacement"
        )

    replacement, replacement_status = _reconnect_remote_attach(
        args, current_size, replace_existing_client=True)
    if replacement_status is not RemoteAttachKind.ACCEPTED:
        _stop_unstarted_remote(replacement)
        raise ProbeError(
            "the old display client did not release in time; the Railmux "
            "session and agents remain intact, so retry shortly"
        )
    return replacement


def prepare_remote_process(
    args: argparse.Namespace,
    current_size: os.terminal_size,
) -> subprocess.Popen:
    """Resolve compatibility and consent before the remote attaches to tmux."""
    argv = build_ssh_argv(
        args.destination,
        session=args.session,
        width=current_size.columns,
        height=current_size.lines,
        fps=args.fps,
        ssh_args=args.ssh_arg,
    )
    process = _spawn_remote(argv)
    startup = await_remote_startup(process)
    install_version = __version__
    optional_compatible_upgrade = False

    install_reason: str | None = None
    if startup.kind is RemoteStartKind.MISSING:
        install_reason = "Railmux is not installed or discoverable remotely."
    elif startup.kind is RemoteStartKind.TIMEOUT:
        _stop_unstarted_remote(process)
        raise ProbeError(
            "timed out waiting for the remote Railmux compatibility handshake"
        )
    elif startup.kind is RemoteStartKind.FAILED:
        _stop_unstarted_remote(process)
        if startup.returncode == 255:
            raise ProbeError("ssh could not connect to the remote host")
        install_reason = (
            "The remote Railmux does not support the compatibility handshake."
        )
    else:
        assert startup.hello is not None
        hello = startup.hello
        versions = _version_pair(hello.version)
        remote_is_newer = bool(
            versions is not None and versions[1] > versions[0]
        )
        remote_is_older = bool(
            versions is not None and versions[1] < versions[0]
        )
        if remote_is_newer:
            protocol_note = (
                f" and requires SSH protocol v{hello.protocol}"
                if hello.protocol != PROTOCOL_VERSION else ""
            )
            if _confirm(
                f"Remote Railmux {hello.version} is newer than local "
                f"{__version__}{protocol_note}. Upgrade local "
                f"Railmux to {hello.version}?"
            ):
                _stop_unstarted_remote(process)
                _upgrade_local_and_restart(hello.version, args.raw_argv)
            if hello.protocol != PROTOCOL_VERSION:
                _stop_unstarted_remote(process)
                raise ProbeError(
                    "the newer remote Railmux uses an incompatible SSH "
                    "protocol; upgrade local Railmux and retry"
                )
            print(
                f"warning: continuing with local Railmux {__version__} "
                f"and compatible remote {hello.version}",
                file=sys.stderr,
            )
            install_version = hello.version
        if hello.protocol > PROTOCOL_VERSION:
            _stop_unstarted_remote(process)
            raise ProbeError(
                f"remote Railmux {hello.version} reports newer SSH protocol "
                f"v{hello.protocol}, but its package version is not newer "
                f"than local {__version__}; refusing an unsafe automatic "
                "local downgrade. Install matching Railmux builds manually."
            )
        if hello.protocol < PROTOCOL_VERSION:
            install_reason = (
                f"Remote Railmux {hello.version} uses older SSH protocol "
                f"v{hello.protocol}; local {__version__} requires "
                f"v{PROTOCOL_VERSION}."
            )
        elif not hello.tmux:
            _stop_unstarted_remote(process)
            raise ProbeError(remote_tmux_help(args.destination))
        elif not hello.ready:
            install_reason = (
                f"Remote Railmux {hello.version} is missing its SSH display "
                "dependency."
            )
        elif remote_is_older:
            install_reason = (
                f"Remote Railmux {hello.version} is older than local "
                f"{__version__}, although SSH protocol v{PROTOCOL_VERSION} "
                "is compatible."
            )
            optional_compatible_upgrade = True
        else:
            if not remote_is_newer and hello.version != __version__:
                print(
                    f"warning: remote Railmux {hello.version} differs from "
                    f"local {__version__}, but SSH protocol "
                    f"v{PROTOCOL_VERSION} is compatible",
                    file=sys.stderr,
                )
            return _finish_remote_attach(args, current_size, process)

    assert install_reason is not None
    if not _confirm_remote_install(args, install_reason, install_version):
        if optional_compatible_upgrade:
            print(
                f"warning: continuing with compatible remote Railmux "
                f"{startup.hello.version}",
                file=sys.stderr,
            )
            return _finish_remote_attach(args, current_size, process)
        _stop_unstarted_remote(process)
        raise ProbeError(remote_install_help(args.destination, install_version))
    _stop_unstarted_remote(process)
    process, startup = _install_remote_and_start(
        args, current_size, install_version
    )
    if (
        startup.kind is RemoteStartKind.HELLO
        and startup.hello is not None
        and not startup.hello.tmux
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(remote_tmux_help(args.destination))
    if startup.kind in (
        RemoteStartKind.MISSING,
        RemoteStartKind.FAILED,
        RemoteStartKind.TIMEOUT,
    ):
        _stop_unstarted_remote(process)
        if not _confirm_remote_private_venv_install(args, install_version):
            raise ProbeError(
                "remote user-site installation failed or timed out.\n"
                f"{remote_install_help(args.destination, install_version)}"
            )
        process, startup = _install_remote_private_venv_and_start(
            args, current_size, install_version
        )
        if (
            startup.kind is RemoteStartKind.HELLO
            and startup.hello is not None
            and not startup.hello.tmux
        ):
            _stop_unstarted_remote(process)
            raise ProbeError(remote_tmux_help(args.destination))
    if (
        startup.kind is not RemoteStartKind.HELLO
        or startup.hello is None
        or startup.hello.version != install_version
        or startup.hello.protocol != PROTOCOL_VERSION
        or not startup.hello.ready
        or not startup.hello.tmux
    ):
        _stop_unstarted_remote(process)
        raise ProbeError(
            "automatic remote installation did not produce a compatible "
            f"Railmux.\n{remote_install_help(args.destination, install_version)}"
        )
    return _finish_remote_attach(args, current_size, process)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="railmux ssh",
        description=(
            "Connect to Railmux with a version-negotiated latest-state SSH "
            "display"
        ),
        epilog=(
            "Before attaching, missing or incompatible remote packages can "
            "be installed after confirmation; automatic setup never uses sudo."
        ),
    )
    parser.add_argument("destination", help="SSH destination or configured host alias")
    parser.add_argument("--session", default="railmux")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument(
        "--no-mouse", action="store_true",
        help="do not capture mouse events (allows ordinary terminal selection)",
    )
    parser.add_argument(
        "--ssh-arg", action="append", default=[],
        help="extra ssh argument; repeat and use --ssh-arg=VALUE",
    )
    args = parser.parse_args(raw_argv)
    args.raw_argv = tuple(raw_argv)
    if not 1.0 <= args.fps <= 60.0:
        parser.error("--fps must be between 1 and 60")
    return args


def run(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProbeError("stdin and stdout must both be interactive terminals")
    if shutil.which("ssh") is None:
        raise ProbeError("ssh is not installed or not on PATH")

    try:
        current_size = wait_for_usable_terminal_size(sys.stdout.fileno())
    except KeyboardInterrupt:
        print("\nrailmux ssh: cancelled while waiting for terminal size",
              file=sys.stderr)
        return 130
    process = prepare_remote_process(args, current_size)

    surface = TerminalSurface(sys.stdout.buffer, mouse=not args.no_mouse)
    surface.set_physical_size(current_size)
    local_size = current_size
    decoder = ServerMessageDecoder()
    model = ScreenModel()
    terminal_input = TerminalInputDecoder()
    history = LocalHistoryView()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout.fileno(), selectors.EVENT_READ, "remote")
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "local")
    started = time.monotonic()
    next_history_prefetch = started
    next_heartbeat = started + _HEARTBEAT_INTERVAL
    frames = 0
    painted_rows = 0
    wire_bytes = 0
    local_exit = False
    remote_closed = False
    awaiting_keyframe = False
    latest_screen: AppliedScreen | None = None
    route_refresh_needed = False

    def send_protocol_frame(frame: bytes) -> None:
        process.stdin.write(frame)
        process.stdin.flush()

    def apply_history_action(action: HistoryAction) -> None:
        nonlocal route_refresh_needed
        overlays = history.overlays()
        if action.restore_live and latest_screen is not None:
            surface.paint(full_repaint(latest_screen), overlays)
        elif action.render_history and latest_screen is not None:
            surface.paint_overlays(latest_screen, overlays)
        if action.protocol_frame:
            send_protocol_frame(action.protocol_frame)
        if action.forwarded_input:
            send_protocol_frame(encode_input(action.forwarded_input))
        if action.refresh_routes:
            route_refresh_needed = True

    def handle_terminal_part(
        part: bytes | SgrMouseEvent,
        forwarded_wheels: set[int],
    ) -> None:
        nonlocal route_refresh_needed
        if isinstance(part, SgrMouseEvent):
            displayed_height = (
                latest_screen.height
                if latest_screen is not None
                else current_size.lines
            )
            part = surface.translate_mouse_event(
                part, logical_height=displayed_height
            )
            # Keep a frozen viewport stable across reported clicks and drags.
            # Terminal-native selection overrides never arrive here.
            focused_pane_id = (
                None
                if latest_screen is None
                else history.pane_id_at_position(
                    latest_screen.cursor_x, latest_screen.cursor_y
                )
            )
            action = history.pointer_event(
                part,
                focused_pane_id,
                status_row=(
                    compact_status_row(latest_screen)
                    if latest_screen is not None else None
                ),
            )
            apply_history_action(
                coalesce_forwarded_wheel(action, part, forwarded_wheels)
            )
            return
        if not part:
            return
        may_change_routes = input_may_change_routes(
            part, routes_visible=bool(history.visible_routes)
        )
        if history.active or history.pending:
            if part == b"\x1b":
                restore = history.cancel()
                apply_history_action(HistoryAction(restore_live=restore))
                return
            if part not in (b"\x1b[I", b"\x1b[O"):
                if may_change_routes:
                    restore = history.invalidate_routes()
                    route_refresh_needed = True
                elif latest_screen is not None:
                    restore = history.cancel_for_input(
                        latest_screen.cursor_x, latest_screen.cursor_y
                    )
                else:
                    restore = history.cancel()
                apply_history_action(HistoryAction(
                    forwarded_input=part,
                    restore_live=restore,
                ))
                return
        if may_change_routes:
            history.invalidate_routes()
            route_refresh_needed = True
        send_protocol_frame(encode_input(part))

    print(
        "railmux ssh: Ctrl-] disconnects locally; Ctrl-B d detaches; "
        f"mouse forwarding is {'off' if args.no_mouse else 'on'}",
        file=sys.stderr,
    )
    try:
        with RawTerminal(sys.stdin.fileno()):
            while True:
                observed_size = os.get_terminal_size(sys.stdout.fileno())
                if observed_size != local_size:
                    if _terminal_size_exceeds_limits(observed_size):
                        raise ProbeError(
                            "resized terminal reports "
                            f"{observed_size.columns}x{observed_size.lines}; "
                            "SSH display limits are "
                            f"{_MAX_TERMINAL_COLUMNS}x{_MAX_TERMINAL_LINES}"
                        )
                    if _is_soft_keyboard_projection(
                        observed_size, current_size
                    ):
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                            )
                    elif not _terminal_size_is_usable(observed_size):
                        raise ProbeError(
                            "resized terminal reports "
                            f"{observed_size.columns}x{observed_size.lines}; "
                            "the minimum is "
                            f"{_MIN_TERMINAL_COLUMNS}x"
                            f"{_MIN_TERMINAL_LINES}"
                        )
                    elif observed_size == current_size:
                        # The soft keyboard closed. Restore the complete
                        # logical screen even if no remote patch is pending.
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if latest_screen is not None:
                            surface.paint(
                                full_repaint(latest_screen),
                                history.overlays(),
                            )
                    else:
                        surface.set_physical_size(observed_size)
                        local_size = observed_size
                        if history.active and latest_screen is not None:
                            surface.paint(full_repaint(latest_screen))
                        history.clear_cache()
                        route_refresh_needed = True
                        process.stdin.write(encode_resize(
                            observed_size.columns, observed_size.lines
                        ))
                        process.stdin.flush()
                        current_size = observed_size
                        awaiting_keyframe = True
                events = selector.select(timeout=terminal_input.next_timeout())
                for key, _mask in events:
                    if key.data == "remote":
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            remote_closed = True
                            break
                        wire_bytes += len(chunk)
                        saw_screen_update = False
                        for message in decoder.feed(chunk):
                            if isinstance(message, HistoryBatch):
                                apply_history_action(
                                    history.accept_prefetch(message)
                                )
                                continue
                            if isinstance(message, HistorySnapshot):
                                apply_history_action(history.accept(message))
                                continue
                            update = message
                            applied = model.apply(update, current_size)
                            if applied is None:
                                if not awaiting_keyframe:
                                    process.stdin.write(encode_keyframe_request())
                                    process.stdin.flush()
                                    awaiting_keyframe = True
                                continue
                            saw_screen_update = True
                            if update.kind is UpdateKind.KEYFRAME:
                                awaiting_keyframe = False
                            latest_screen = applied
                            focus_reporting_started = surface.paint(
                                applied, history.overlays()
                            )
                            if focus_reporting_started:
                                # Enabling DECSET 1004 does not require a
                                # terminal to report its already-focused state.
                                # On SSH reconnect that can leave tmux and the
                                # active agent believing the client is still
                                # unfocused until the user changes windows.
                                send_protocol_frame(encode_input(b"\033[I"))
                            frames += 1
                            painted_rows += len(applied.changed_rows)
                        if saw_screen_update and route_refresh_needed:
                            prefetch = history.begin_prefetch(time.monotonic())
                            if prefetch:
                                send_protocol_frame(prefetch)
                            if history.prefetch_pending_id is not None:
                                route_refresh_needed = False
                                next_history_prefetch = (
                                    time.monotonic() + _HISTORY_PREFETCH_INTERVAL
                                )
                    else:
                        data = os.read(sys.stdin.fileno(), 4096)
                        if not data:
                            local_exit = True
                            break
                        data, emergency_exit = split_local_escape(data)
                        if emergency_exit:
                            local_exit = True
                        forwarded_wheels: set[int] = set()
                        for part in terminal_input.feed(data):
                            handle_terminal_part(part, forwarded_wheels)
                        if local_exit:
                            break
                if not local_exit:
                    for part in terminal_input.flush_pending():
                        handle_terminal_part(part, set())
                if local_exit:
                    break
                if remote_closed:
                    break
                if process.poll() is not None and not events:
                    break
                now = time.monotonic()
                if now >= next_heartbeat:
                    send_protocol_frame(encode_heartbeat())
                    next_heartbeat = now + _HEARTBEAT_INTERVAL
                if (
                    not args.no_mouse
                    and latest_screen is not None
                    and now >= next_history_prefetch
                ):
                    prefetch = history.begin_prefetch(now)
                    if prefetch:
                        send_protocol_frame(prefetch)
                        route_refresh_needed = False
                    next_history_prefetch = now + _HISTORY_PREFETCH_INTERVAL
    except KeyboardInterrupt:
        # Raw mode normally forwards Ctrl-C. This only handles an external
        # signal and follows the conventional shell exit status.
        return 130
    except BrokenPipeError:
        remote_closed = True
    finally:
        selector.close()
        surface.close()
        if local_exit and process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    elapsed = max(0.001, time.monotonic() - started)
    print(
        f"railmux ssh: painted {frames} coalesced updates / "
        f"{painted_rows} rows in {elapsed:.1f}s; "
        f"received {wire_bytes / 1024:.1f} KiB",
        file=sys.stderr,
    )
    known_exit = {
        int(RemoteExit.DETACHED): "detached; the Railmux session is still running",
        int(RemoteExit.SOFT_QUIT): "soft-quit; agent sessions were left running",
        int(RemoteExit.HARD_QUIT): "hard-quit; the managed Railmux session ended",
    }
    if process.returncode in known_exit:
        print(f"railmux ssh: {known_exit[process.returncode]}", file=sys.stderr)
        return 0
    if frames == 0 and not local_exit:
        raise ProbeError("remote display helper exited before its first frame")
    if not local_exit and process.returncode:
        print(
            "railmux ssh: remote display failed; run 'railmux doctor' on "
            "the remote host for tmux health and the last recorded incident",
            file=sys.stderr,
        )
        return process.returncode
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
