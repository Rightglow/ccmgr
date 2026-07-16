"""Coalesce bursts of tmux copy-mode wheel events.

The agent runs in a tiny detached tmux session. Managed copy-mode bindings
send it ``U`` and ``D`` bytes; it sums those deltas and submits at most one
scroll operation per frame. SSH therefore carries the latest useful viewport
instead of a backlog of short-lived intermediate viewports.
"""
from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import termios
import time
import tty


FRAME_SECONDS = 0.1  # 10 FPS balances smooth wheel motion and SSH redraw volume.


class ScrollAccumulator:
    """Accumulate signed wheel deltas between render frames."""

    def __init__(self, lines_per_event: int = 2) -> None:
        self.pending = 0
        self.lines_per_event = lines_per_event

    def feed(self, data: bytes) -> None:
        self.pending += data.count(b"U") * self.lines_per_event
        self.pending -= data.count(b"D") * self.lines_per_event

    def drain(self) -> int:
        pending = self.pending
        self.pending = 0
        return pending


class ScrollInput:
    """Parse wheel bytes and target-change messages from the control PTY."""

    def __init__(self, target_pane: str, lines_per_event: int = 2) -> None:
        self.target_pane = target_pane
        self.accumulator = ScrollAccumulator(lines_per_event)
        self.next_flush: float | None = None
        self._last_flush: float | None = None
        self._target_buffer: bytearray | None = None

    def feed(self, data: bytes, now: float, frame_seconds: float) -> int:
        """Consume input and return a delta that should render immediately.

        The first wheel input when no frame cooldown is active is a leading-edge
        update. Further input inside the same frame remains accumulated until
        ``next_flush`` so a burst produces at most one redraw per frame.
        """
        for byte in data:
            if self._target_buffer is not None:
                if byte in (10, 13):
                    candidate = self._target_buffer.decode(errors="ignore").strip()
                    if candidate.startswith("%"):
                        self.target_pane = candidate
                    self._target_buffer = None
                else:
                    self._target_buffer.append(byte)
            elif byte == ord("T"):
                # Never carry wheel intent or a frame deadline across session
                # switches. The new target's first wheel event should render
                # immediately instead of inheriting the old target's cooldown.
                self.accumulator.drain()
                self.next_flush = None
                self._last_flush = None
                self._target_buffer = bytearray()
            elif byte in (ord("U"), ord("D")):
                self.accumulator.feed(bytes((byte,)))

        if not self.accumulator.pending:
            self.next_flush = None
            return 0

        deadline = (
            None if self._last_flush is None
            else self._last_flush + frame_seconds
        )
        if deadline is None or now >= deadline:
            return self.flush(now)

        self.next_flush = deadline
        return 0

    def drain(self) -> int:
        """Drain pending distance without changing the frame clock."""
        delta = self.accumulator.drain()
        self.next_flush = None
        return delta

    def flush(self, now: float) -> int:
        """Drain pending distance and start a new bounded-render frame."""
        delta = self.drain()
        if delta:
            self._last_flush = now
        return delta


def apply_scroll(target_pane: str, delta: int) -> bool:
    """Apply one aggregated copy-mode scroll operation."""
    if not delta:
        return True
    command = "scroll-up" if delta > 0 else "scroll-down"
    result = subprocess.run(
        ["tmux", "send-keys", "-X", "-N", str(abs(delta)),
         "-t", target_pane, command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def run(target_pane: str, frame_seconds: float = FRAME_SECONDS,
        lines_per_event: int = 2, ready_session: str | None = None) -> None:
    """Read wheel events from stdin and render only the latest viewport."""
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    state = ScrollInput(target_pane, lines_per_event)
    try:
        tty.setcbreak(fd)
        if ready_session:
            subprocess.run(
                ["tmux", "set-window-option", "-t", ready_session,
                 "@railmux_scroll_ready", "1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
        while True:
            now = time.monotonic()
            timeout = (None if state.next_flush is None
                       else max(0.0, state.next_flush - now))
            readable, _, _ = select.select([fd], [], [], timeout)
            if readable:
                data = os.read(fd, 4096)
                if not data:
                    delta = state.drain()
                    if delta:
                        apply_scroll(state.target_pane, delta)
                    return
                delta = state.feed(data, time.monotonic(), frame_seconds)
                if delta:
                    apply_scroll(state.target_pane, delta)

            now = time.monotonic()
            if state.next_flush is not None and now >= state.next_flush:
                delta = state.flush(now)
                if delta:
                    apply_scroll(state.target_pane, delta)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--frame-ms", type=float, default=FRAME_SECONDS * 1000)
    parser.add_argument("--lines-per-event", type=int, default=2)
    parser.add_argument("--ready-session")
    args = parser.parse_args(argv)
    try:
        run(
            args.target,
            max(0.005, args.frame_ms / 1000.0),
            max(1, args.lines_per_event),
            args.ready_session,
        )
    except (OSError, termios.error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
