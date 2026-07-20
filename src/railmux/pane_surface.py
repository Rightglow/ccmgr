"""Small terminal-native surfaces shown before an interactive pane is ready."""
from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import sys
from collections.abc import Sequence


_CLEAR = "\033[2J\033[H"
_RESET = "\033[0m"
_ACCENT = "\033[1;38;5;70m"
_HEADING = "\033[1;37m"
_MUTED = "\033[38;5;244m"


def _centered_surface(
    rows: Sequence[tuple[str, str]], width: int, height: int,
) -> str:
    """Render styled rows centered without counting ANSI bytes as columns."""
    width = max(1, width)
    height = max(1, height)
    top = max(0, (height - len(rows)) // 2)
    rendered = [""] * top
    for style, text in rows:
        left = max(0, (width - len(text)) // 2)
        rendered.append(f"{' ' * left}{style}{text}{_RESET}")
    return _CLEAR + "\n".join(rendered) + "\n"


def render_empty_surface(pane_number: int, width: int, height: int) -> str:
    """Return the idle agent-pane surface."""
    if width >= 48:
        actions = ((_MUTED, "click / ␣  show    ·    ↵  open & focus"),)
    elif width >= 28:
        actions = (
            (_MUTED, "click / ␣  show"),
            (_MUTED, "↵  open & focus"),
        )
    else:
        actions = ((_MUTED, "␣ show  ·  ↵ open"),)
    return _centered_surface(
        (
            (_ACCENT, f"RAILMUX  ·  PANE {pane_number}"),
            (_HEADING, "Ready for another agent"),
            ("", ""),
            (_MUTED, "Choose a session in Railmux"),
            *actions,
        ),
        width,
        height,
    )


def render_startup_surface(width: int, height: int) -> str:
    """Return immediate startup feedback while workspace recovery runs."""
    return _centered_surface(
        (
            (_ACCENT, "RAILMUX"),
            (_HEADING, "Restoring your workspace"),
            ("", ""),
            (_MUTED, "Reconnecting sessions and panes…"),
        ),
        width,
        height,
    )


def _terminal_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
    except OSError:
        size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


def _run_empty_surface(pane_number: int) -> None:
    """Keep an empty surface clean, redrawing it whenever tmux resizes it."""
    import termios

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    quiet_attrs = termios.tcgetattr(fd)
    quiet_attrs[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSADRAIN, quiet_attrs)

    def redraw(_signum=None, _frame=None) -> None:
        width, height = _terminal_size()
        sys.stdout.write(render_empty_surface(pane_number, width, height))
        sys.stdout.flush()

    previous = signal.signal(signal.SIGWINCH, redraw)
    try:
        redraw()
        while True:
            ready, _, _ = select.select([fd], [], [], 3600)
            if ready and not os.read(fd, 4096):
                break
    finally:
        signal.signal(signal.SIGWINCH, previous)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        sys.stdout.write(_RESET)
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--empty", type=int, choices=(1, 2), required=True)
    args = parser.parse_args(argv)
    _run_empty_surface(args.empty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
