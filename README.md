# Railmux — session manager for Claude Code & Codex

[![Tests](https://github.com/Rightglow/Railmux/actions/workflows/test.yml/badge.svg)](https://github.com/Rightglow/Railmux/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/railmux.svg)](https://pypi.org/project/railmux/)
[![Python](https://img.shields.io/pypi/pyversions/railmux.svg)](https://pypi.org/project/railmux/)
[![License](https://img.shields.io/github/license/Rightglow/Railmux.svg)](LICENSE)

A terminal UI to navigate, resume, and start
[Claude Code](https://claude.com/claude-code) and
[Codex](https://github.com/openai/codex) sessions across all your projects.
Railmux lives in the left pane of a tmux window; the right pane shows the active
agent. Each session runs in its own detached tmux session, so switching never
interrupts in-progress responses or tool calls.

- **Claude Code mode** — reads `~/.claude/projects/*`, lists sessions by project, resume with `claude --resume`
- **Codex mode** — reads `~/.codex/sessions/*`, same sidebar workflow for Codex sessions
- Press `m` to cycle through the available modes

## Demo

> **Demo GIF coming soon.** This slot is reserved for the interactive Railmux
> walkthrough and can be replaced without restructuring the README.

<!-- Replace the blockquote above with assets/railmux-demo.gif when available. -->

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

## Quick start

```bash
pip install railmux
# or: pip3 install railmux
railmux
```

Requires Python 3.9+, `tmux`, `less`, and at least one supported agent CLI on
`PATH`. Claude Code and Codex are independent: a missing provider does not stop
you from using the other one.

If `tmux` is missing, an interactive Railmux launch can offer to install it
with Homebrew on macOS or `apt-get` on Debian/Ubuntu/WSL. Railmux shows the
exact command and requires explicit confirmation (default: no); it never
installs Homebrew itself or modifies the system during non-interactive runs.
Other common Linux package managers receive a copyable installation command.

If you are not already inside tmux, Railmux launches its own tmux session. Run
`railmux --doctor` for a privacy-safe dependency and environment report when
setup does not behave as expected.

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
| `m` | Cycle through available agent modes |
| `F9` | Fullscreen the agent pane (toggle) for clean text selection |
| `?` | Full help popup with all keybindings |
| `q` or `Ctrl-C` | Quit with confirmation |

`+ New project` works in both Claude Code and Codex modes. Browse to an
existing directory and choose `. (use this path)`, or type a new relative,
absolute, or `~`-based path. When no existing entry matches, select the
explicit `+ create …` row (it is focused automatically) and press `Enter`;
railmux creates the directory before starting the agent.

When a mode has no projects or sessions, its empty state names the active
provider and points to `+ New project` or `n`, so an unavailable provider never
looks like data from the previous mode.

### Mouse

| Action | Effect |
|--------|--------|
| Left-click (non-running) | Preview session history in the right pane |
| Left-click (running) | Attach to the running session (focus stays left) |
| Double-click | Open/attach and move focus to the right pane |
| Right-click | Context menu (Open, Info, Rename, Star, Kill, Term, Delete) |

## History preview

Left-click a non-running session to view its conversation history in the right
pane without starting or resuming the agent. The preview reads the saved JSONL
directly, so it cannot send a message or mutate the session. It follows the
providers' conversation structure as closely as their saved data allows:
user/assistant messages, tool calls and abbreviated tool output are
colour-coded, while internal system context and encrypted reasoning are hidden.
Plaintext Codex reasoning summaries are shown when present.

The viewer uses `less`, the standard pager available on both Linux and macOS,
because neither provider currently exposes a native read-only history view.
It opens at the latest activity and streams at most the latest 2,000 JSONL
records for fast previews of large sessions. Press `/` to search, `n`/`N` to
move between matches, and `q` to exit; the right pane restores whatever was
there before. Shell/editor commands and pager history are disabled in preview
mode. Double-click to skip the preview and open the session directly.

Clicking a running session attaches to it immediately (focus stays left so you can keep browsing). Double-clicking steals focus to the right pane for both running and non-running sessions.

## Status indicators

Each running session shows a coloured ● reflecting its current state:

- **Green** — idle (assistant last responded normally)
- **Yellow** — busy (assistant is processing)
- **Red** — blocked (waiting for tool approval)

A grass-green title identifies a live tmux session independently of its status;
stopped sessions use a neutral hollow ○. The same grass green is used for the
focused pane chrome and tmux status bar. The current cursor uses a deeper green
background, while the session displayed in the agent pane remains marked in
neutral slate after keyboard focus moves away.

For Codex rollouts with lifecycle events, only `task_complete`, `turn_aborted`,
or `thread_rolled_back` ends an active turn; intermediate assistant messages and
tool results remain busy. Older rollouts without lifecycle records fall back to
their last user/assistant message. Because Codex has no reliable approval-wait
signal, a pending tool must remain unchanged for two minutes before it turns
red, reducing false alerts from normal long-running commands.

## How it works

`railmux` reads agent session files from `~/.claude/projects/*` (Claude Code) or `~/.codex/sessions/*` (Codex) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session for that session doesn't already exist, railmux creates one with `tmux new-session -d`; (2) railmux's right pane displays it so you see and interact with the agent. By default the display is a nested tmux client. Switching the display never transfers process ownership away from the detached agent session.

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

# Experimental: show the real agent pane through transactional tmux swaps.
# Default: "nested". Unsafe/unsupported situations fall back to nested.
agent_transport = "nested" # or "swap"
```

The experimental `swap` transport currently requires tmux 2.7 or newer and the
auto-launched `railmux` tmux session. It remains opt-in while real-provider SSH,
long-transcript reflow, and macOS measurements are completed. See
[`docs/DENESTED_AGENT_PANE.md`](docs/DENESTED_AGENT_PANE.md) for tested
lifecycle behavior, fallbacks, performance observations, and limitations.

## Diagnostics

```bash
railmux --doctor
```

The doctor command works even when `tmux` is missing. It reports component
versions, terminal capability hints, configuration health, and whether provider
data directories are accessible. Its output is designed for issue reports: it
does not include hostnames, usernames, session IDs, transcripts, credentials,
environment values, configured commands, or raw custom paths.

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
