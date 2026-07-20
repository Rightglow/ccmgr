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
| `/` | Filter the focused Projects, Sessions, or Running pane by name |

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
| `␣` | Preview stopped or switch running target (like single-click) |
| `F8` | Cycle agent layout: single → side-by-side → stacked |
| `F9` | Fullscreen the agent pane (toggle) for clean text selection |
| `?` | Full help popup with all keybindings |
| `q` or `Ctrl-C` | Quit with confirmation |

`+ New project` works in both Claude Code and Codex modes. Browse to an
existing directory and choose `. (use this path)`, or type a new relative,
absolute, or `~`-based path. When no existing entry matches, select the
explicit `+ create …` row (it is focused automatically) and press `Enter`;
railmux creates the directory before starting the agent.

The rename popup starts with the current title pre-filled. Press
`Ctrl-U` to clear the entire input, `Enter` to save a non-empty title, or `Esc`
to cancel.

### Dual-agent split

Railmux distinguishes the **Focused pane** from the **Target pane**. The Focused
pane currently receives keyboard input. The Target pane is the remembered P1 or
P2 where actions started from the sidebar take effect; Chinese documentation
uses **焦点窗格** and **目标窗格** respectively.

Open the first agent normally, then press `F8` to create Pane 2 and cycle single,
side-by-side, and stacked layouts even while an agent has keyboard focus. A new
Pane 2 may remain empty until you use it; on narrower terminals F8 skips an
orientation that cannot provide two usable panes. Returning to single remembers
Pane 2 for the next cycle and leaves its agent running in the background. A
same-instance soft restart restores the layout, both pane contents, Target pane,
and keyboard focus after validating the exact local tmux identities. If one
content wish is no longer valid, Railmux keeps the usable layout with a branded
empty pane; if the terminal can no longer fit the split, it falls back to single
and keeps the displaced live agent in Running. Killing a displayed session
safely detaches it first and keeps the chosen layout open; its agent pane returns
to the resize-aware Railmux empty surface instead of disappearing or retaining a
stale interactive client.

The sidebar uses roughly 30% of the window in single-agent mode. Dual-agent
layouts reduce it to roughly 20%, with a 30-column floor, so both agents gain
working width without making session metadata unusably narrow. Returning to
single restores the 30% sidebar.

The bottom brand keeps a one-cell workspace indicator after the provider name:
`▣` is single-pane; `◧`/`◨` are side-by-side with the filled half naming the
left/right Target pane; `⬒`/`⬓` are stacked with the filled half naming the
top/bottom Target pane. For example, `Railmux · Codex · ◨` means a side-by-side
layout targeting the right pane. The indicator remains visible across focus
changes. When focus returns to Railmux, agent borders become gray while the
filled half continues to identify the pane used for stopped-session preview,
running-session switching,
Enter/double-click open, F9, terminal placement, status, and attention state.
Move through an empty Pane 2 and back to Railmux to target it; no extra
split-specific menu action is needed. In a single-agent layout P1 is the only
possible target, so no separate inactive-target marker is needed.

In the side-by-side layout, arrows on the green shared borders point inward at
the agent pane that actually owns keyboard focus. They disappear when focus
returns to Railmux and do not alter the stacked layout. tmux versions before
3.3 retain the same focus colours without directional arrows.

When a mode has no projects or sessions, its empty state names the active
provider and points to `+ New project` or `n`, so an unavailable provider never
looks like data from the previous mode.

Running-pane filtering is in-memory and never reads transcript bodies. Plain
text matches the visible session label, project, and provider; add
`project:<name>` to restrict a search to one project. Claude Code and Codex keep
independent Running filters. Blocked sessions move ahead of other Running rows
on the existing throttled recency sort, while their red status dots still
update immediately.

### Mouse

| Action | Effect |
|--------|--------|
| Left-click (non-running) | Preview session history in the last agent pane |
| Left-click (running) | Switch the last agent pane to that session |
| Double-click | Open/attach in the last agent pane and move focus there |
| Right-click | Context menu (Open, Preview, Info, Rename, Star, Kill, Term, Delete) |

The terminal must report mouse buttons to applications for these actions to
reach Railmux. Right-click reporting is sometimes a separate setting from
ordinary mouse reporting; see [FAQ 2](#2-mouse-buttons-or-f8f9-dont-work--whats-wrong).

## History preview

For a stopped session, left-click or press `␣` to view conversation history in
the last agent pane without starting or resuming the agent. The preview reads the saved JSONL
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

For a running session, single-click or `␣` switches the remembered agent pane
to it while focus stays in Railmux. For a stopped session, the same inputs open
a read-only preview. Double-click or Enter opens either kind and transfers
focus. The context-menu Preview action follows the single-click/`␣` rule.

## Status indicators

Each running session shows a coloured ● reflecting its current state:

- **Green** — idle (assistant last responded normally)
- **Yellow** — busy (assistant is processing)
- **Red** — blocked (waiting for tool approval)

An independent magenta **!** marks the last provider outcome that still needs
attention, such as a generic abort or provider error. It does not replace the
activity dot: a live session can be idle and still show `!`, while a stopped
historical session keeps its neutral `○` liveness marker alongside the badge.
Session Info and Running Info show the sanitized category and summary.

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

Codex error details are read only from dedicated lifecycle/error records, never
from user or assistant transcript text. Current observed rollouts do not persist
a reliable capacity/rate-limit reason, so Railmux labels those records
generically instead of guessing from message content.

## How it works

`railmux` reads agent session files from `~/.claude/projects/*` (Claude Code) or `~/.codex/sessions/*` (Codex) and lists everything. Pressing `Enter` on a session does two things: (1) if a detached tmux session for that session doesn't already exist, railmux creates one with `tmux new-session -d`; (2) railmux's right pane displays it so you see and interact with the agent. By default Railmux transactionally swaps the real agent pane into the display window, while its detached home session stays alive behind a placeholder. Unsupported or unverified environments automatically use the compatibility nested-tmux display instead.

Soft quit keeps detached agents alive. On restart, process-bearing recovery
state is isolated to the exact outer tmux pane, so separate Railmux windows or
private tmux servers cannot restore one another's display. Mode, project, and
sidebar filters are also saved as non-process view preferences for use on a
later login; those portable preferences never authorize an attach or kill.
If Railmux stops while a new provider is still creating its session UUID, the
Running pane restores it as an explicit unresolved entry. It can be reopened or
stopped by exact tmux identity, but Railmux will not guess at or delete an
unknown provider history file.

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

# Prefer the real agent pane through validated transactional tmux swaps.
# Default: "swap". Set "nested" to force the compatibility display.
agent_transport = "swap" # or "nested"
```

The `swap` transport requires tmux 2.7 or newer and the auto-launched `railmux`
tmux session. Railmux automatically falls back to `nested` for old tmux,
unmanaged sessions, independent clients, unsupported agent topology, or any
unverified transition. See
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

### 2. Mouse buttons or F8/F9 don't work — what's wrong?

These are usually terminal‑side settings, not tmux or railmux.

**Mouse**: enable your terminal's “Report mouse events” or “Mouse reporting”
setting. Railmux already enables tmux mouse support for its own sessions, but it
cannot receive an event that the terminal keeps for its own UI.

Right-click may have a separate forwarding switch. In iTerm2, open *Settings →
Pointer → General*, then enable **“Right click reported to apps, does not open
menu.”** Without it, iTerm2 opens its own menu instead of sending the click to
Railmux.

![iTerm2 Pointer settings with “Right click reported to apps, does not open menu” enabled](https://raw.githubusercontent.com/Rightglow/Railmux/main/docs/assets/iterm2-right-click.png)

**F8 (layout) and F9 (fullscreen)**: the operating system or terminal may
consume function keys before tmux sees them. On macOS, either hold `Fn` when
pressing the key or enable *System Settings → Keyboard → “Use F1, F2, etc. keys
as standard function keys”*; also remove any Mission Control shortcut using the
same key. On Windows laptops, `Fn+Esc` commonly toggles Fn Lock. If the terminal
has its own shortcut or key-mapping editor, remove the conflicting mapping or
configure it to send the corresponding F8/F9 function-key sequence to the
terminal session.

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
