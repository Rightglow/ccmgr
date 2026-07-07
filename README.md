# ccmgr — Claude Code session manager

A terminal UI to navigate, resume, and start [Claude Code](https://claude.com/claude-code) sessions across all your projects from one place. ccmgr lives in the left pane of a tmux window; the right pane shows the currently-active claude. Each claude session runs as its own detached tmux session in the background, so switching between sessions preserves all in-progress work — no responses or tool calls are interrupted.

> **This is a fork** of [regmi-saugat/ccmgr](https://github.com/regmi-saugat/ccmgr) (v0.1.5), developed with agent-assisted programming using [Claude Code](https://claude.ai/claude-code).

## Install

```bash
pip install ccmgr
```

Requires Python 3.12+, `tmux`, and `less` on `PATH`.

## Run

```bash
ccmgr
```

If you're not already inside a tmux session, ccmgr will launch one automatically. The most recent project is auto-selected on startup.

## Keys

### Navigation

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move selection within the focused pane |
| `Tab` / `Shift-Tab` | Cycle focus through Projects, Sessions, Running panes |
| `Esc` | Move focus up: Running → Sessions → Projects |
| `/` | Filter the focused pane by name |

### Session actions

| Key | Action |
|-----|--------|
| `Enter` | Resume or start the selected session |
| `n` | Start a fresh claude session in the current project |
| `i` | Popup with session details |
| `r` | Rename the focused session |
| `s` | Toggle star — starred sessions pinned to top with ⭐ |
| `k` | Kill the running Claude process (keeps session file) |
| `d` | Delete the focused session (prompts for confirmation) |
| `t` | Open a terminal in the active project directory |
| `?` | Full help popup with all keybindings |
| `q` or `Ctrl-C` | Quit with confirmation |

### Mouse

| Action | Effect |
|--------|--------|
| Left-click (non-running) | Preview session history in the right pane |
| Left-click (running) | Attach to the running session (focus stays left) |
| Double-click | Open/attach and move focus to the right pane |
| Right-click | Context menu (Open, Info, Rename, Star, Kill, Term, Delete) |

## History preview

Left-click a non-running session to view its conversation history in the right pane without starting Claude. The transcript is colour-coded (user = cyan, assistant = green, tool use = yellow) and displayed via `less`. Press `q` to exit — the right pane restores whatever was there before. Double-click to skip the preview and open the session directly.

Clicking a running session attaches to it immediately (focus stays left so you can keep browsing). Double-clicking steals focus to the right pane for both running and non-running sessions.

## Status indicators

Each session shows a coloured ● reflecting its current state:

- **Green** — idle (assistant last responded normally)
- **Yellow** — busy (assistant is processing)
- **Red** — blocked (waiting for tool approval)

## How it works

`ccmgr` reads `~/.claude/projects/*` (Claude's per-project session history) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session running `claude --resume <id>` doesn't already exist, ccmgr creates one with `tmux new-session -d`; (2) ccmgr's right pane runs `tmux attach -t cc-<id>` so you see and interact with that claude. Switching sessions just respawns the right pane to attach to a different background tmux session — the detached claudes keep running with all their state intact.

## SSH / remote use

ccmgr works over SSH and benefits from a few tweaks for responsiveness and scrollback:

**Server** (`~/.tmux.conf` on the remote machine):

```tmux
set -sg escape-time 0         # eliminate delay after Escape key
set -g  history-limit 10000   # generous scrollback per pane
```

**Client** (`~/.ssh/config` on your local machine):

```
Host your-server
    Compression yes           # smoother tmux pane scrolling over SSH
```

## Copying text from the Claude pane

Selecting text is awkward under tmux: the sidebar and Claude share the screen,
and over SSH your clipboard lives on the *local* machine.

- **Terminals with OSC 52** (iTerm2, kitty, WezTerm, Alacritty, foot, Windows
  Terminal): just left-drag to select in the Claude pane — it copies to your
  local clipboard automatically, even over SSH. (iTerm2: enable *Settings →
  General → Selection → "Applications in terminal may access clipboard"*.)
- **Terminal.app and others without OSC 52**: press **F3** to fullscreen the
  Claude pane, Shift-drag to select, `Cmd/Ctrl+C` to copy, then **F3** again to
  return. Fullscreening hides the sidebar so the native selection grabs only
  Claude's text.

On a default Mac, F3 is Mission Control — enable *System Settings → Keyboard →
"Use F1, F2, etc. keys as standard function keys"*, or press **Fn+F3**.
`Ctrl-B z` also toggles fullscreen (built into tmux) but zooms whichever pane
has focus, so it may fullscreen the sidebar instead.

## Configuration

Optional config at `~/.config/ccmgr/config.toml`:

```toml
[claude]
# Path to the claude binary (default: "claude")
binary = "claude"

[live]
# How often to refresh the session list (ms)
poll_interval_ms = 1000
# How long the [LIVE] badge stays after last activity (seconds)
live_badge_seconds = 60
```
