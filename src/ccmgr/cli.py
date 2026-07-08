import argparse
import os
import sys
from pathlib import Path

from ccmgr import __version__
from ccmgr.config import load_config
from ccmgr import tmux_ctl


def is_ssh_session(environ: dict[str, str] | None = None) -> bool:
    """Best-effort detection of a process reached through an SSH transport.

    OpenSSH exports all three variables below. tmux normally refreshes
    ``SSH_CONNECTION`` when a client attaches, so this also works when ccmgr is
    launched from an existing tmux session. Explicit CLI flags remain available
    for terminals or gateways that strip these variables.
    """
    env = os.environ if environ is None else environ
    return any(env.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccmgr", description="Claude Code session manager TUI")
    parser.add_argument("--version", action="version", version=f"ccmgr {__version__}")
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

    # If we're not in tmux and not told we're already inside, re-exec ourselves under tmux.
    if not args.inside_tmux and not tmux_ctl.in_tmux():
        if not tmux_ctl.has_tmux():
            print("error: tmux is required but not found on PATH. Install tmux to use ccmgr.", file=sys.stderr)
            return 2
        # Find this ccmgr binary to re-exec.
        ccmgr_path = sys.argv[0]
        cmd = ["tmux", "new-session", "-A", "-s", "ccmgr",
               ccmgr_path, "--inside-tmux"]
        # If extra args were passed, forward them.
        for a in sys.argv[1:]:
            cmd.append(a)
        os.execvp("tmux", cmd)
        # unreachable

    # Inside tmux now.
    config = load_config()
    # Lazy import so non-TUI invocations (--version etc) don't pull urwid.
    from ccmgr.ui.app import App
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
