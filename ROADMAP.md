# Railmux roadmap

This is a set of design candidates, not a release commitment. Architecture
invariants already agreed are recorded in `docs/ARCHITECTURE.md`.

## Proven: unresolved new-session recovery

New provider launches now receive a versioned tmux marker before the provider
process starts. The marker records bounded identity facts only: provider mode,
immutable tmux session/pane IDs, outer owner, normalized cwd, creation token,
transaction phase, and the exact provider UUID once proven. A crash before UUID
discovery leaves a visible unresolved Running entry rather than an invisible
live agent. Linux uses exact open-rollout correlation; no-procfs ambiguity stays
unresolved. Unknown provider history is never deleted.

Private-socket tmux coverage proves marker-before-provider ordering and that an
old identity cannot kill a newly created session reusing the same name. The
macOS/no-procfs path deliberately favors a recoverable unresolved entry over a
heuristic adoption when its complete pre-launch fence is unavailable.

## Follow-up candidates

### Dual-agent workspace follow-ups

Railmux 0.2 ships the bounded two-slot workspace, layout cycling, Target-pane
routing, responsive sidebar sizing, native focus presentation, and full managed
soft-restart recovery described in `docs/ARCHITECTURE.md`. Explicitly swapping
the physical primary/secondary positions is deferred because it does not yet
solve a demonstrated workflow problem. Revisit only if field use shows that the
current spatial navigation is insufficient. The 50x12 minimum and 80x20
preferred pane thresholds may be tuned from real-agent feedback without
changing the workspace model.

### Default swap transport follow-up

`swap` is the shipped default; `nested` remains an explicit compatibility
choice and the automatic fallback whenever exact pane ownership cannot be
proven. Its transaction and recovery invariants are authoritative in
`docs/ARCHITECTURE.md`, with reproducible evidence in
`docs/DENESTED_AGENT_PANE.md`. Remaining work is field evidence, not a second
default-selection experiment:

- Acceptable real-provider geometry/reflow, especially on tmux 2.7/2.8 where
  `resize-window` is unavailable and with long inline Codex transcripts.
- How much Claude Code improves when de-nested, since its alternate-screen,
  application-owned mouse path cannot use Codex's copy-mode batching unchanged.
- Same-link SSH measurements for first wheel paint, burst drain, sustained
  output, clipboard/mouse behavior, and CPU. A reproducible local synthetic
  server benchmark now places swap close to direct and consistently ahead of
  nested marker observation. It cannot observe client terminal paint or real
  providers, so it must not be cited as proof of perceived latency.
- Whether real measurements justify changing the current 100 ms copy-mode
  coalescing frame. Do not infer a redraw budget from ping or SSH/TCP metrics:
  the server cannot observe local terminal paint.

### Codex interrupt transcript replay

Codex currently consolidates an incomplete streamed answer after Esc by
clearing and rebuilding its canonical inline transcript. Railmux does not see
or forward that Esc, and the attach-time pre-sizing path is not involved, but a
nested tmux client can make the upstream rebuild visibly sweep from old content
back to the prompt.

Do not silently force alternate-screen mode or truncate Codex history to hide
this: both change native scrollback/copy behavior. Possible experiments are an
explicit, documented Codex reflow-row limit, a future tmux version with proven
application synchronized-output support, and the existing swap transport.
Any workaround must remain opt-in until its history tradeoff and Codex
version compatibility are clear.

### Provider adapters

The mode registry now supports a third stable mode and independent view state.
Extract backend operations behind a provider adapter before adding a provider
whose discovery/launch/delete model differs from both existing backends.

## Completed foundations

### Responsive compact workspace

Cramped terminals use one full-window page at a time without changing the
logical one/two-agent layout. The tmux status map provides portable
sidebar/A1/A2 navigation, an intermediate projection temporarily hides the
second agent before full compact mode, and widening restores slot identities,
Target, orientation, and proportional dividers. Short mobile terminals keep
their remote logical size while a soft keyboard locally projects the bottom of
the live screen.

### Latest-state SSH display

The version-negotiated SSH transport coalesces screen state, provides bounded
styled local history per agent pane, supports safe remote user installation,
and keeps each helper behind an exact heartbeat lease. An opt-in bounded
reconnect loop can replace an unexpectedly lost display without takeover or
remote mutation. Diagnostics expose the same privacy-safe snapshot as human
text or versioned JSON.

### Multi-instance restart state

Soft-restart persistence is split between exact-owner runtime recovery and a
portable per-mode view. Runtime files are keyed by tmux server lifetime and
immutable pane ID, so same-named sessions on private servers and multiple
windows on one server cannot restore one another. Legacy ownerless files
migrate view fields only; process recovery remains fail-closed. The uniquely
managed CLI session uses a same-server, dead-owner handoff because its
controller pane is recreated during a graceful restart.

### Dual-agent workspace

The primary/secondary workspace ships with single, side-by-side, and stacked
layouts, one explicit Target pane, shared mouse/keyboard actions, responsive
sidebar sizing, liveness reconciliation, and managed soft-restart restoration.
The same detached agent cannot be displayed in both slots.

### Unified sidebar chrome

Projects, Sessions, and Running share one pair of vertical rails and labelled
horizontal boundaries. Focus outlines, pointer-local scrolling, stable weighted
heights, narrow-title truncation, and neutral agent-focus presentation no
longer depend on three independent boxes.

### Background Codex session index

Codex tree walking and rollout parsing now run in one rate-limited background
worker. The UI consumes immutable, monotonically numbered last-known-good
snapshots; requests coalesce, placeholder discovery can accelerate the next
scan, transient failures retain live rows, and shutdown never waits
indefinitely for filesystem IO. Synthetic local delay measurements and their
limitations are recorded in `docs/BACKGROUND_SESSION_INDEX.md`.

### Focus and status colour semantics

Railmux now uses distinct meanings for grass-green pane chrome and live-session
titles, the deep-green cursor, the slate persistent target, and red/yellow/green
agent status dots. The focused agent uses native tmux border colour and, where
supported, directional indicators; sidebar focus returns agent borders to gray
while the compact workspace map retains the Target pane.
