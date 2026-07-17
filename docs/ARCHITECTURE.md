# Railmux architecture invariants

This document records constraints that should survive implementation changes.
It is intentionally separate from the user-facing README. Read it before
changing providers, mode switching, outer tmux panes, previews, or restore
state.

## Modes are registered providers, not a boolean

`railmux.modes.ModeRegistry` is the ordered source of shared mode metadata.
The application stores a stable `_active_mode_key`; `m` cycles registry order.
Do not reintroduce paired fields such as `claude_selection` / `codex_selection`
or a new `is_<provider>` boolean.

Each mode owns its sidebar view state through `_ModeViewState`, keyed by its
stable registry key. Project selection, and future filters/cursors, must be
resolved only against that mode's currently visible objects. A mode with no
projects must never retain another mode's project as a hidden action target.

Provider-specific backends remain responsible for discovery/indexing, launch,
resume/delete, transcript parsing, and status inference. Shared UI code should
branch on declared capabilities (`project_source`, `login_shell`, etc.), not on
the assumption that exactly Claude and Codex exist. Adding a truly new backend
will require a backend adapter, but must not require redesigning mode cycling or
per-mode state.

Soft-restart state writes the stable `mode` key. The legacy `codex_mode` boolean
is also written temporarily for downgrade compatibility and must remain a
read fallback while state files from Railmux 0.1.x may exist.

## The agent workspace is independent of the sidebar mode

`AgentWorkspace` owns at most two `AgentSlot` objects: `primary` and
`secondary`. An agent slot owns every mutable fact about its outer display
pane: pane ID, attached background session, provider key, active session ID,
preview state, and preview restore target.

The currently browsed sidebar mode and the providers displayed in agent slots
are independent. Switching the sidebar from Claude to Codex must not replace,
close, or reinterpret an already displayed Claude agent. Do not put display
pane fields back onto `App` as parallel scalars; the old `_right_pane_*`
properties exist only as compatibility shims backed by the primary slot.

Current releases intentionally expose only the primary slot. Code should keep
that behavior until the dual-agent interaction below is approved and tested.

## Agent display transports preserve one ownership model

The default `nested` transport runs a tmux client in the outer display pane and
attaches it to the persistent agent session. The experimental `swap` transport
moves the real pane into the display window. Both are provider-neutral and are
selected behind `AgentDisplayTransport`; attach, preview, close, delete,
liveness, and teardown must not bypass that boundary with a destructive
`respawn-pane`, `kill-pane`, or `kill-session`.

In swap mode `AgentSlot.pane_id` is the pane physically visible in that slot.
It is the placeholder while idle/previewing and the real provider pane while
displayed. `SwapState` owns the immutable real-pane/PID, home window,
placeholder, display window, outer session, keeper, slot, and transaction phase.
The same real pane may be owned by only one slot.

Before a real pane moves, a detached tmux session group shares the outer window.
This keeper adds no pane or PTY and prevents a direct kill of the original outer
session from destroying a displayed agent. Versioned, slot-specific tmux window
user options record every transaction. Startup recovery may move only exact
marked identities; it must never infer ownership from a `cc-*`, `cx-*`, pane
title, or session-name resemblance.

Every swap is validate -> mark prepared -> move -> verify -> mark displayed.
Return is mark returning -> move home -> verify -> clear. A failed post-move
rollback retains its marker and keeper and forbids destructive fallback. An
external attached client, unsupported topology, incomplete identity, old tmux,
or unowned outer session uses nested display. Controlled preview, close, soft
quit, hard quit, and delete return the real pane before replacing a display
placeholder or killing its home session.

The experimental floor is tmux 2.7. tmux 2.7 and 2.8 lack `resize-window`, so
their native swap geometry may reflow a long inline transcript. This is a
performance/visual limitation, not permission to alter provider history or
alternate-screen behavior. Full evidence and remaining gates are in
`docs/DENESTED_AGENT_PANE.md`.

## Dual-agent interaction target

The first version should remain bounded to two slots and preserve all existing
single-pane behavior:

- Enter opens/replaces the primary slot, exactly as today.
- `Open in split` creates or replaces the secondary slot.
- `Close split` removes only the outer secondary pane; it never kills the
  detached background agent session.
- The same background tmux session should not be attached in both slots.
- Layout names are `stacked` and `side-by-side`, avoiding ambiguous
  horizontal/vertical terminology.
- Automatic orientation is chosen once when the split is created and must not
  flip during terminal resize. Users can explicitly rotate it.
- Narrow screens should prefer stacked panes because three side-by-side columns
  make agent TUIs unusably narrow.

Preview, terminal placement, F9 fullscreen, focus styling, scroll management,
liveness, teardown, and soft restart must all operate on an explicit slot when
the secondary slot becomes user-visible.

## Size and attach invariants

The current single-pane layout recommends at least 120x30 cells and treats
anything below 80x20 as critically cramped. Size warnings must never trap a
remote user or disable resize/quit controls. "Outer size" means the containing
tmux window, not Urwid's TTY size: after a split the latter is only the narrow
sidebar. Read the window size at startup and on terminal resize events rather
than polling tmux every second.

Each agent display pane independently recommends 80x20 and treats anything
below 50x12 as critically cramped. Check it after attach, explicit divider
movement, and terminal-size transitions; do not poll tmux for dimensions every
second when the outer size is unchanged.

Detached agent sessions commonly begin at 80x24. Before attaching a nested
tmux client, create/identify the outer display pane, read its exact dimensions,
and best-effort resize the inner session window to match. This ordering avoids
an attach-time resize that can make Codex visibly replay or reflow long history.
Failure to pre-size is non-fatal: attach must retain its previous fallback.
Never pre-size a session that already has another attached client, because that
would resize an independently viewed workspace.

## Focus colour semantics

Grass green (`#5FAF00`, with an automatically downsampled terminal fallback)
means keyboard/pane focus: bright on pane chrome and deep behind the current
cursor row. Give pane bodies an explicit neutral attribute so the outer focus
map cannot colour ordinary text. A persistent right-pane target uses slate,
live tmux rows use grass-green bold titles, and green/yellow/red agent status
dots retain their colours on normal, cursor, and target backgrounds. Stopped
sessions use a neutral hollow marker instead of a stale lifecycle colour.

With exactly two outer tmux panes, tmux intentionally assigns the active-border
colour to only half of their shared divider. The single-agent layout therefore
sets active and inactive border styles to the same green while the agent is
focused, producing one continuous line, and sets both gray for the sidebar.
Do not assume `pane-active-border-style` can outline one future agent slot: the
multi-agent layout needs an explicit border/focus design for shared edges.

## Liveness, activity, and attention are separate axes

Detached tmux/process ownership determines whether a session is running.
Provider lifecycle records determine conversational activity (`idle`, `busy`,
or `blocked`). An optional attention value records the last actionable terminal
outcome without changing either of those facts. Provider errors and aborts must
never prune a live registry entry or reuse the red blocked dot.

Attention summaries come only from dedicated provider error/lifecycle fields and
must be short and sanitized. Never classify an error from user prompts,
assistant messages, tool output, or titles. A newer turn start and a newer
successful turn clear stale attention. User interrupts and explicit rollbacks
are not provider failures.

Running-pane filtering is a view over the live registry, never a mutation of
that registry. The pane retains the complete provider-scoped entry snapshot,
keys focus and callbacks by exact tmux session name, and performs fuzzy/project
matching only against indexed display metadata. Filter edits therefore cannot
hide a session from liveness management or trigger transcript I/O.
