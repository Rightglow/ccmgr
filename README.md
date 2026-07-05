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

If you're not already inside a tmux session, ccmgr will launch one automatically (`tmux new-session -A -s ccmgr`). The most recent project is auto-selected on startup.

## Keys

### Navigation

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `j` / `k`) | Move selection within the focused pane |
| `Tab` / `Shift-Tab` | Cycle focus through Projects, Sessions, Running panes |
| `Esc` | Move focus up: Running → Sessions → Projects |
| `Ctrl-B` `←` | Move focus from claude back to ccmgr sidebar |
| `Ctrl-B` `→` | Move focus from ccmgr to claude (right pane) |
| `Ctrl-B` `d` | Detach from ccmgr — keep all sessions alive, return to bash |
| `/` | Filter the focused pane by name |
| **Mouse** | Click any row to select it (same as Enter) |

### Session actions

| Key | Action |
|-----|--------|
| `Enter` | Resume or start the selected session in the right pane |
| `r` | Rename the focused session (writes a new title to the JSONL) |
| `f` | Toggle favorite — favorited sessions are pinned to the top with a ⭐ |
| `d` | Delete the focused session (prompts for confirmation; removes JSONL + kills tmux) |
| `n` | Start a fresh claude session in the current project |

### Info & tools

| Key | Action |
|-----|--------|
| `i` | Popup with session details: title, project, messages, tokens, last user input |
| `?` | Full help popup with all keybindings |
| `c` | Open the active project in VS Code (`code <path>`) |
| `t` | Open a terminal in the active project directory |

### Quit

| Key | Action |
|-----|--------|
| `q` or `Ctrl-C` | Quit with confirmation (shows how many sessions will be killed) |

## Session list features

### Status indicators

Each session shows a colored dot reflecting its current state (read from the last JSONL record):

- 🟢 **idle** — assistant last responded normally
- 🟡 **busy** — assistant is processing a user message
- 🔴 **blocked** — assistant is waiting for tool approval (needs your input)


### Favorites

Press `f` to pin a session to the top of the list. Favorites persist across restarts in `~/.config/ccmgr/favorites.json`.

### Session management

- **Rename** (`r`): edit the session title. If no AI-generated title exists, the first user message is used as a fallback.
- **Delete** (`d`): permanently removes the session file from disk and kills its background tmux session. Requires confirmation.
- **New session** (`n` or `Enter` on `+ New session`): starts a fresh `claude` in the current project.
- **New project** (`Enter` on `+ New project`): prompt for a directory path, create it if needed, and start claude there.

## How it works

`ccmgr` reads `~/.claude/projects/*` (Claude's per-project session history) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session named `cc-<short_id>` running `claude --resume <id>` doesn't already exist, ccmgr creates one with `tmux new-session -d`; (2) ccmgr's right pane runs `tmux attach -t cc-<short_id>` so you see and interact with that claude. Switching sessions just respawns the right pane to attach to a different background tmux session — the detached claudes keep running with all their state intact.

## Roadmap

Planned for future releases:

- Cross-session search across projects
- Cost and token-usage dashboard

Issues and pull requests welcome.

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
