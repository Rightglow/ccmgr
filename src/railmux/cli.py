from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from railmux import __version__
from railmux.config import ConfigError, default_config_path, load_config
from railmux.diagnostics import is_ssh_session, run_doctor
from railmux.pane_surface import render_startup_surface
from railmux import tmux_ctl
from railmux.system_deps import ensure_tmux_available


def _show_startup_message() -> None:
    """Paint immediate feedback before App performs its initial discovery."""
    if not sys.stdout.isatty():
        return
    size = shutil.get_terminal_size((80, 24))
    sys.stdout.write(render_startup_surface(size.columns, size.lines))
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railmux",
        description="Terminal workspace for Claude Code and Codex sessions",
    )
    parser.add_argument("--version", action="version", version=f"railmux {__version__}")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print privacy-safe environment diagnostics and exit",
    )
    parser.add_argument("--project", help="Launch focused on a single project path")
    parser.add_argument("--claude-home", default=str(Path.home() / ".claude"), help="Override ~/.claude location (testing)")
    parser.add_argument("--inside-tmux", action="store_true", help="Internal: skip the auto-tmux-launch step")
    scroll_group = parser.add_mutually_exclusive_group()
    scroll_group.add_argument(
        "--scroll-coalescing",
        dest="scroll_coalescing",
        action="store_true",
        default=None,
        help="Force-enable tmux copy-mode wheel event coalescing",
    )
    scroll_group.add_argument(
        "--no-scroll-coalescing",
        dest="scroll_coalescing",
        action="store_false",
        help="Force-disable tmux copy-mode wheel event coalescing",
    )
    args = parser.parse_args(argv)

    if args.doctor:
        return run_doctor(claude_home=Path(args.claude_home))

    # tmux is required even when TMUX is already set: an inherited TMUX value
    # with no tmux binary on PATH otherwise enters a TUI whose controls cannot
    # work. Keep this preflight ahead of every TUI startup path.
    if not ensure_tmux_available():
        return 2

    # A swap transaction survives a killed Railmux Python process in tmux
    # metadata. Repair it before ``new-session -A``: otherwise that command
    # would merely attach to the stranded display window and never start App.
    from railmux.display_transport import recover_interrupted_swaps
    recovery = recover_interrupted_swaps()
    if recovery.unresolved:
        print(
            "warning: an interrupted agent display could not be repaired; "
            "the marked pane was left untouched",
            file=sys.stderr,
        )

    # If we're not in tmux and not told we're already inside, re-exec ourselves under tmux.
    if not args.inside_tmux and not tmux_ctl.in_tmux():
        # Find this railmux binary to re-exec.
        railmux_path = sys.argv[0]
        cmd = ["tmux", "new-session", "-A", "-s", "railmux",
               railmux_path, "--inside-tmux"]
        # If extra args were passed, forward them.
        for a in sys.argv[1:]:
            cmd.append(a)
        os.execvp("tmux", cmd)
        # unreachable

    # Inside tmux now.
    try:
        config = load_config()
    except ConfigError as exc:
        path = default_config_path()
        try:
            display_path = f"~/{path.relative_to(Path.home()).as_posix()}"
        except ValueError:
            display_path = "the Railmux configuration file"
        print(f"error: {display_path}: {exc}", file=sys.stderr)
        return 2
    # App construction performs bounded initial provider/tmux discovery before
    # Urwid can paint its first frame. A tiny terminal-native surface prevents
    # that interval from looking like a hung empty tmux pane.
    _show_startup_message()
    # Lazy import so non-TUI invocations (--version etc) don't pull urwid.
    from railmux.ui.app import App
    app = App(
        claude_home=Path(args.claude_home),
        config=config,
        auto_launched=args.inside_tmux,
        scroll_coalescing=(
            is_ssh_session() if args.scroll_coalescing is None
            else args.scroll_coalescing
        ),
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
