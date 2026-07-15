# Railmux — session manager for Claude Code & Codex

A terminal UI to navigate, resume, and start [Claude Code](https://claude.com/claude-code) and [Codex](https://github.com/openai/codex) sessions across all your projects. railmux lives in the left pane of a tmux window; the right pane shows the active agent. Each session runs as its own detached tmux session in the background — switching preserves all in-progress work, no responses or tool calls are interrupted.

- **Claude Code mode** — reads `~/.claude/projects/*`, lists sessions by project, resume with `claude --resume`
- **Codex mode** — reads `~/.codex/sessions/*`, same sidebar workflow for Codex sessions
- Press `m` to toggle between modes

## Why Railmux?

Without Railmux, managing multiple agent sessions means manually tracking tmux
windows, remembering which session lives where, and copy-pasting session IDs.
Sessions pile up across projects, context gets lost, and switching between
them is friction.

Railmux replaces all of that with a single keystroke:

- **One sidebar, all sessions** — browse every Claude Code and Codex session
  across every project, filter by name, star favourites
- **Instant switching** — press Enter and the right pane attaches to a different
  background tmux session; every agent keeps running, no responses lost
- **Zero manual bookkeeping** — no more `tmux ls | grep cc-` or hunting through
  `~/.claude/projects/`

## Install

```bash
pip install railmux
# or: pip3 install railmux
```

Requires Python 3.9+, `tmux`, and `less` on `PATH`.

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

`+ New project` works in both Claude Code and Codex modes. Browse to an
existing directory and choose `. (use this path)`, or type a new relative,
absolute, or `~`-based path. When no existing entry matches, select the
explicit `+ create …` row (it is focused automatically) and press `Enter`;
railmux creates the directory before starting the agent.

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

[projects]
# Show projects with no resumable sessions (default: false)
show_empty_projects = false

[live]
# How often to refresh the session list (ms)
poll_interval_ms = 1000
```

## FAQ

### 1. How do I copy text from the agent pane?

Under tmux the sidebar and agent share the screen, and over SSH your clipboard
lives on the *local* machine.

**OSC 52** (iTerm2, kitty, WezTerm, Alacritty, foot, Windows Terminal):
drag-select in the agent pane copies to the local clipboard automatically,
even over SSH, no Shift needed. (iTerm2: enable *Settings → General →
Selection → "Applications in terminal may access clipboard"*.)

**Without OSC 52** (Terminal.app, etc.): press **F9** to fullscreen the agent →
**Shift‑drag** to select → `Cmd+C` / `Ctrl+C` to copy → **F9** to return.

> `Ctrl-B z` also toggles fullscreen (built into tmux) but zooms whichever
> pane has focus — it may fullscreen the sidebar instead of the agent.

### 2. Mouse clicks or F9 don't work — what's wrong?

These are usually terminal‑side settings, not tmux or railmux.

**Mouse**: your terminal must forward mouse events to tmux. Check your terminal's
profile settings for "Report mouse events" or "Mouse reporting" and make sure
it's enabled. tmux's `set -g mouse on` should already be in place (railmux
sets it for its own session).

**F9 (fullscreen)**: on macOS, F9 is often captured by Mission Control.
Fix: *System Settings → Keyboard → "Use F1, F2, etc. keys as standard function
keys"*. On Windows laptops the Fn‑lock key (`Fn+Esc`) toggles function‑key
behaviour. If your terminal has a "Pass function keys to terminal" option,
enable it.

### 3. Using railmux over SSH

railmux works over SSH out of the box — the scroll‑agent integration is
SSH‑transparent, so mouse scrolling in the agent pane works the same as
locally. These tweaks improve responsiveness and scrollback:

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

If the connection is so slow that the sidebar can't refresh one frame per
second, skip the mouse and use keyboard navigation — `↑↓ / Tab / Enter`
cover every operation and don't depend on a fast redraw.

### 4. Will automated review sessions pollute my session list?

**Codex**: sessions created by `codex exec` (headless automation, pre‑commit
hooks, CI) are filtered automatically — railmux only shows interactive
sessions (`codex-tui`, `codex_cli_rs`).

**Claude Code**: for one-shot automated reviews, disable session persistence so
the consultation never appears in `/resume`:

```bash
# Print mode only; the review is not saved as a resumable session
claude -p --no-session-persistence "review this diff"
```

(`--no-session-persistence` is a Claude Code print-mode option.)

### 5. pip reports "externally-managed-environment"

Create a virtual environment, then install Railmux with that environment's
`pip`. This works on macOS, Linux, and WSL without modifying the system Python:

```bash
python3 -m venv ~/.venvs/railmux
source ~/.venvs/railmux/bin/activate
pip install railmux
```

`pipx install railmux` is an optional convenience for a globally available CLI;
it is not required on macOS or any other platform.

## Acknowledgements

The tmux sidebar idea and initial architecture came from [regmi-saugat/ccmgr](https://github.com/regmi-saugat/ccmgr). railmux extends it with Codex support, session history preview, starring, in-app renaming, mouse interaction, and a status bar integrated into the tmux status line.
