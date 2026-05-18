import argparse
import os
import sys
from pathlib import Path

from ccmgr import __version__
from ccmgr.config import load_config
from ccmgr import tmux_ctl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccmgr", description="Claude Code session manager TUI")
    parser.add_argument("--version", action="version", version=f"ccmgr {__version__}")
    parser.add_argument("--project", help="Launch focused on a single project path")
    parser.add_argument("--claude-home", default=str(Path.home() / ".claude"), help="Override ~/.claude location (testing)")
    parser.add_argument("--inside-tmux", action="store_true", help="Internal: skip the auto-tmux-launch step")
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
    app = App(claude_home=Path(args.claude_home), config=config, auto_launched=args.inside_tmux)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
