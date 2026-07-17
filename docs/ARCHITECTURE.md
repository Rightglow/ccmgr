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

## Sidebar rows are disposable views

The periodic refresh publishes value snapshots to Projects, Sessions, and
Running panes. Each pane skips an unchanged snapshot but may discard and rebuild
all row widgets as soon as any rendered value changes. A row therefore has no
stable lifetime: never store timers, click tracking, drag state, or other
interaction authority on a row instance.

State that must survive refresh belongs on the pane/application or in a shared
controller keyed by stable identity (`encoded_name`, `session_id`, or exact
tmux name). `ClickableRow`'s class-level double-click state and `click_key` are
the reference pattern. Rendering caches are an optimization only and must not
become a second state authority.

Portable soft-restart state writes the stable active `mode` key inside a
per-mode view map. The ownerless `codex_mode` boolean remains a read-only
migration fallback for Railmux 0.1.x files; it is never copied into new state.

## Restart state has two authorities

Instance-local recovery state lives under `XDG_RUNTIME_DIR` (or the existing
macOS-compatible `/tmp/railmux-UID` fallback). Its filename is derived from a
privacy-safe tmux server-lifetime digest plus the immutable outer pane ID. The
payload repeats that owner identity and is rejected unless it matches the live
instance. Session/window IDs are recorded as context but a move of the same
pane does not change ownership. Different panes, windows, sessions, and private
tmux servers therefore cannot overwrite or restore one another's local state.

The local schema may contain the right-pane target and validated running
bindings. It duplicates the current sidebar view so a shared portable
last-writer never changes an exact instance restart. Files are atomically
replaced as 0600 inside a verified user-owned 0700 runtime directory. Cleanup
is bounded and removes only recognized owners proven dead; unknown/newer state
and old but possibly-live private servers are retained.

Portable state lives beside `config.toml` and contains only an active mode plus
per-mode project/session selections and filters. It contains no tmux names,
pane/process IDs, commands, environment values, transcripts, or recovery
authority. A second node may use it as a view default but must ignore every
node-local pane identity. The old fixed `railmux-state.json` has no owner proof,
so migration may extract only validated portable view fields; right-pane and
running-binding fields remain ignored and the legacy file is left for manual
cleanup.

Detached-session tmux stamps and swap-transport markers retain their own exact
lifetimes and validation. Runtime JSON is a cache and must not become a
competing authority for adopting, killing, or replacing an agent pane.

Legacy detached-session discovery still derives truncated tmux names with
`App._safe_name` and resolves them with `_resolve_truncated_id`. Their character
normalization and width must remain in lockstep; changing either side requires
updating the other and its recovery tests. Exact orphan and swap markers remain
the stronger authority and must never fall back to name resemblance.

New-session recovery uses `@railmux_orphan_v2`, a bounded session option written
onto an inert, finite-lifetime holder before the provider command is respawned
into its exact pane. Its schema contains only mode, placeholder key, immutable
tmux session/pane IDs, exact outer owner, normalized cwd, timestamp/random
token, resolution phase, and (after proof) provider UUID. Commands,
environment, prompts, transcripts, and credentials are forbidden.

The lifecycle is `launching -> unresolved -> resolved`. Startup may adopt only
a marker whose live immutable tmux objects and supported mode validate. A live
different outer owner fences concurrent Railmux windows; if that exact owner
pane is absent from a successful full-server snapshot, a new instance may take
over only after a crash-safe compare/write/readback owner claim. Snapshot or
claim failure stays unresolved, and concurrent claimants cannot both adopt. Linux
resolution requires descendant/open-rollout correlation where available; a
procfs error is ambiguity, not permission to guess. Without exact correlation,
only one candidate fenced by a complete pre-launch snapshot may resolve.

Resolution commits the marker's UUID before changing the in-memory registry,
so interruption is idempotent. Until that commit, attach and stop callbacks
carry the marker token and recheck live session/pane identity. Stopping an
unresolved entry may kill only that exact tmux identity and cannot delete a
provider file because no provider UUID is authorized.

## Session indexes publish immutable generations

The Codex history tree is owned by one `BackgroundCodexIndex` worker. Urwid
ticks only query its latest immutable `IndexSnapshot`; they must never call the
underlying `CodexIndex` tree walk or rollout parser. Repeated requests coalesce,
ordinary scans are rate-limited, and placeholder discovery may request a
shorter bounded interval without creating another worker or an unbounded scan
loop.

Each successful publication increments a generation and carries complete
frozen `SessionMeta` values. Do not reconstruct a selected subset of their
fields at this boundary: provider-specific fields such as attention state must
survive unchanged. Renames are a read-time overlay; delete uses a temporary ID
tombstone until a later generation confirms removal. Neither mutates a
published snapshot.

A failed or incomplete tree walk retains the last known-good generation. A
transient per-file error retains that file's cached metadata, publishes the
otherwise coherent generation with a bounded warning, and retries later. A
partial failure with no usable snapshot is not published as successful empty
state. Shutdown signals the daemon and waits only for a bounded interval, since
filesystem IO cannot be cancelled portably; a late worker may not publish
after close.

Claude's `SessionCache` remains a separate source. Its UI path scans only the
selected project directory, caps cold parsing to the newest entries, and parses
only changed files. Moving that bounded per-project source to another worker is
not required by the Codex whole-tree invariant, but any future worker must use
the same last-known-good generation rules.

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

Soft quit may release UI-only resources and return displayed panes home, but it
must branch before the detached-session kill loop. Hard-quit destruction must
remain below that explicit decision so adding teardown work cannot silently
turn a soft restart into loss of live agents.

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
