# railmux — session manager for Claude Code & Codex

A terminal UI to navigate, resume, and start [Claude Code](https://claude.com/claude-code) and [Codex](https://github.com/openai/codex) sessions across all your projects. railmux lives in the left pane of a tmux window; the right pane shows the active agent. Each session runs as its own detached tmux session in the background — switching preserves all in-progress work, no responses or tool calls are interrupted.

- **Claude Code mode** — reads `~/.claude/projects/*`, lists sessions by project, resume with `claude --resume`
- **Codex mode** — reads `~/.codex/sessions/*`, same sidebar workflow for Codex sessions
- Press `m` to toggle between modes

## Install

```bash
pip install railmux
```

Requires Python 3.12+, `tmux`, and `less` on `PATH`.

## Run

```bash
railmux
```

If you're not already inside a tmux session, railmux will launch one automatically. The most recent project is auto-selected on startup.

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
| `n` | Start a fresh session in the current project |
| `i` | Popup with session details |
| `r` | Rename the focused session |
| `s` | Toggle star — starred sessions pinned to top with ⭐ |
| `k` | Kill the running agent process (keeps session file) |
| `d` | Delete the focused session (prompts for confirmation) |
| `t` | Open a terminal in the active project directory |
| `m` | Toggle between Claude Code and Codex modes |
| `F9` | Fullscreen the agent pane (toggle) for clean text selection |
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

Left-click a non-running session to view its conversation history in the right pane without starting the agent. The transcript is colour-coded (user = cyan, assistant = green, tool use = yellow) and displayed via `less`. Press `q` to exit — the right pane restores whatever was there before. Double-click to skip the preview and open the session directly.

Clicking a running session attaches to it immediately (focus stays left so you can keep browsing). Double-clicking steals focus to the right pane for both running and non-running sessions.

## Status indicators

Each session shows a coloured ● reflecting its current state:

- **Green** — idle (assistant last responded normally)
- **Yellow** — busy (assistant is processing)
- **Red** — blocked (waiting for tool approval)

## How it works

`railmux` reads agent session files from `~/.claude/projects/*` (Claude Code) or `~/.codex/sessions/*` (Codex) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session for that session doesn't already exist, railmux creates one with `tmux new-session -d`; (2) railmux's right pane attaches to it so you see and interact with the agent. Switching sessions just respawns the right pane — the detached sessions keep running with all their state intact.

## SSH / remote use

railmux works over SSH and benefits from a few tweaks for responsiveness and scrollback:

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

## Copying text from the agent pane

Selecting text is awkward under tmux: the sidebar and agent share the screen,
and over SSH your clipboard lives on the *local* machine.

- **OSC 52** (iTerm2, kitty, WezTerm, Alacritty, foot, Windows Terminal):
  drag-select in the agent pane — copies to local clipboard automatically,
  even over SSH. No Shift needed. (iTerm2: enable *Settings → General →
  Selection → "Applications in terminal may access clipboard"*.)
- **Without OSC 52** (Terminal.app, etc.): **F9** to fullscreen the agent →
  **Shift-drag** to select → `Cmd/C` to copy → **F9** to return.
`Ctrl-B z` also toggles fullscreen (built into tmux) but zooms whichever pane
has focus, so it may fullscreen the sidebar instead.

## Configuration

Optional config at `~/.config/railmux/config.toml`:

```toml
[claude]
# Path to the claude binary (default: "claude")
binary = "claude"

[codex]
# Path to the codex binary (default: "codex")
binary = "codex"
home = "~/.codex"

[live]
# How often to refresh the session list (ms)
poll_interval_ms = 1000
```

## Acknowledgements

The tmux sidebar idea and initial architecture came from [regmi-saugat/ccmgr](https://github.com/regmi-saugat/ccmgr). railmux extends it with Codex support, session history preview, starring, in-app renaming, mouse interaction, and a status bar integrated into the tmux status line.
