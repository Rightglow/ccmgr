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

## Under discussion

### Dual-agent workspace

Expose the prepared primary/secondary `AgentWorkspace` model through a small
Pane menu: `Open selected in split`, `Close split`, and `Rotate split`. Decide
the direct keyboard shortcut only after the menu interaction has been used in
practice. Validate Claude+Claude, Codex+Codex, and mixed-provider layouts on
macOS and Linux before enabling restoration of the secondary pane.

Open questions:

- Whether F9 uses the focused agent slot, or primary when the sidebar is focused.
- Whether transcript preview always uses primary or may use an existing secondary.
- Whether swapping primary/secondary belongs in the first public iteration.
- Exact minimum per-agent width/height for choosing stacked vs side-by-side.

### De-nested agent pane rendering

An experimental, default-off transport now replaces the right-side nested
`tmux attach-session` client with the real agent pane. It uses a tracked
placeholder plus cross-session `swap-pane`, durable tmux metadata, and a
zero-extra-pane session-group keeper. The keeper preserves a displayed agent
even if the original outer session is directly killed; startup repair returns
only exact marked panes. Nested attach remains the default and the fallback for
unsupported topology, an external client, an unmanaged outer session, or any
unproven operation. See `docs/DENESTED_AGENT_PANE.md` for evidence.

This is primarily a responsiveness project, not just an internal refactor.
Codex over the same SSH connection should feel close to a directly launched
Codex: the first wheel input should paint without a fixed 500 ms delay, a burst
should render only the newest useful viewport at roughly 20--30 FPS, and
scrolling must stop promptly when input stops instead of replaying queued
intermediate frames. Benchmark the current nested path, the prototype, and a
direct Codex baseline at the same pane size and SSH link before choosing the
default frame budget.

The low-risk scheduling step remains independent of pane migration:
copy-mode now renders the leading wheel update immediately and coalesces the
remainder of a burst on a 100 ms frame (10 FPS). A faster adaptive or
user-configurable frame still requires real-provider measurements.

Lifecycle invariants for the prototype:

- Detached agent sessions remain the source of process persistence; removing
  the nested display client must not make an agent a child of Railmux.
- Graceful close and soft restart swap every displayed agent back to its home
  session before the sidebar exits.
- Persist enough pane/home/placeholder identity to recover after SIGKILL and
  return a stranded agent pane on the next launch without killing it.
- Switching, closing a display slot, transcript preview, terminal placement,
  F9, and future dual-agent layouts must never kill the background agent.
- Refuse or safely fall back to nested attach when the agent session has an
  independent attached client or its pane topology is not the supported
  single-agent shape.
- Keep scroll routing scoped to marked agent panes. Evolve the current fixed
  100 ms frame toward a configurable/adaptive 33--50 ms interval only after
  measurements justify it; disabling coalescing remains a diagnostic fallback,
  not the intended performance solution.

Proven implementation facts:

- Cross-session swap and direct-outer-kill recovery pass on Linux with tmux 2.7
  and 3.4. The CI smoke runs the same private-socket path on Linux and macOS.
- Primary and secondary ownership records are separate, and one real pane
  cannot be claimed by both slots; the public UI still exposes only primary.
- Total PTY count does not fall because the hidden home placeholder replaces
  the visible nested-client PTY. The visible update path does remove the nested
  client/parser/composition hop.

Remaining gates before considering a default change:

- Acceptable real-provider geometry/reflow, especially on tmux 2.7/2.8 where
  `resize-window` is unavailable and with long inline Codex transcripts.
- Confirmed macOS CI evidence after an approved push.
- How much Claude Code improves when de-nested, since its alternate-screen,
  application-owned mouse path cannot use Codex's copy-mode batching unchanged.
- Same-link SSH measurements for first wheel paint, burst drain, sustained
  output, clipboard/mouse behavior, and CPU. A reproducible local synthetic
  server benchmark now places swap close to direct and consistently ahead of
  nested marker observation, supporting continued opt-in testing. It cannot
  observe client terminal paint or real providers, so nested remains default.
  If same-link client-paint results show parity or regression for both Codex
  and Claude, remove swap rather than carry its lifecycle complexity forever.

### Codex interrupt transcript replay

Codex currently consolidates an incomplete streamed answer after Esc by
clearing and rebuilding its canonical inline transcript. Railmux does not see
or forward that Esc, and the attach-time pre-sizing path is not involved, but a
nested tmux client can make the upstream rebuild visibly sweep from old content
back to the prompt.

Do not silently force alternate-screen mode or truncate Codex history to hide
this: both change native scrollback/copy behavior. Possible experiments are an
explicit, documented Codex reflow-row limit, a future tmux version with proven
application synchronized-output support, and the de-nested pane prototype
above. Any workaround must remain opt-in until its history tradeoff and Codex
version compatibility are clear.

### Compact/portrait navigation

For a narrow or portrait terminal, consider showing sidebar and agent as two
exclusive views instead of squeezing them side by side. Activating an item in
the sidebar would switch to the agent view. A very small top menu/status pane
could preserve mode, current project/session, and a clear `Back to sidebar`
action without pretending the agent is fullscreen.

This can be a good responsive layout, but should not be implemented as an
implicit resize side effect until these questions are answered:

- Is the switch triggered only below a startup threshold, or manually?
- How does mouse/keyboard focus return without intercepting agent input?
- Does F9 mean terminal fullscreen or merely hide the compact top menu?
- Can tmux rearrange the panes without resizing/reflowing a running agent TUI?
- What state is preserved when moving between compact and regular layouts?

A promising implementation is two outer tmux windows (`sidebar` and `agent`)
rather than two panes squeezed to near-zero width. Window switching keeps both
processes alive and gives each view the full terminal. The existing Railmux tmux
status line could move to the top in compact mode and act as the small feedback/
navigation surface; a dedicated menu pane would cost space and add another
focus target. This remains a hypothesis to prototype, not an agreed design.

### Provider adapters

The mode registry now supports a third stable mode and independent view state.
Extract backend operations behind a provider adapter before adding a provider
whose discovery/launch/delete model differs from both existing backends.

## Completed foundations

### Multi-instance restart state

Soft-restart persistence is split between exact-owner runtime recovery and a
portable per-mode view. Runtime files are keyed by tmux server lifetime and
immutable pane ID, so same-named sessions on private servers and multiple
windows on one server cannot restore one another. Legacy ownerless files
migrate view fields only; process recovery remains fail-closed.

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
agent status dots. The shared two-pane divider is painted continuously. A
dual-agent layout must still prototype border ownership rather than assuming
tmux active-border style can outline one slot.
