# ccmgr — Claude Code session manager

A terminal UI to navigate, resume, and start [Claude Code](https://claude.com/claude-code) sessions across all your projects from one place. ccmgr lives in the left pane of a tmux window; the right pane shows the currently-active claude. Each claude session runs as its own detached tmux session in the background, so switching between sessions preserves all in-progress work — no responses or tool calls are interrupted.

## Install

```bash
pip install ccmgr
```

Requires Python 3.12+ and `tmux` on `PATH`.

## Run

```bash
ccmgr
```

If you're not already inside a tmux session, ccmgr will launch one automatically (`tmux new-session -A -s ccmgr`). The right pane shows the currently-active claude; each claude session also exists as a detached tmux session in the background. Switching sessions in ccmgr re-attaches the right pane to a different background claude.

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move selection within the focused pane |
| `Tab` / `Shift-Tab` | Switch focus between Projects and Sessions panes |
| `Enter` | Resume or start the selected session — claude opens (or re-attaches) in the right pane |
| `Ctrl-B` then `←` | Move focus back to ccmgr from the right pane |
| `Ctrl-B` then `→` | Move focus to the claude pane |
| `/` | Filter the focused pane |
| `i` | Popup with the focused session's details (title, project, msgs, tokens) |
| `?` | Full help popup |
| `q` or `Ctrl-C` | Quit (kills the right tmux pane + auto-launched tmux session) |

In the right pane, keystrokes go directly to claude — full color, full TUI. Tasks keep running even when you switch the right pane to another session, because each claude lives in its own detached tmux session. Press `Ctrl-B ←` to return to ccmgr on the left.

## How it works

`ccmgr` reads `~/.claude/projects/*` (Claude's per-project session history) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session named `cc-<short_id>` running `claude --resume <id>` doesn't already exist, ccmgr creates one with `tmux new-session -d`; (2) ccmgr's right pane runs `tmux attach -t cc-<short_id>` so you see and interact with that claude. Switching sessions just respawns the right pane to attach to a different background tmux session — the detached claudes keep running with all their state intact.

## Roadmap

Planned for future releases:

- Cross-session search across projects
- Cost and token-usage dashboard
- Mouse support
- Renaming and closing individual sessions from within ccmgr

Issues and pull requests welcome.

## Configuration

Optional config at `~/.config/ccmgr/config.toml`:

```toml
[claude]
binary = "claude"

[live]
poll_interval_ms = 1000
live_badge_seconds = 60
```
