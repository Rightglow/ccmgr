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

Click intent must also survive controller redirects. In particular, opening a
Sessions row may discover that its provider is already live and redirect
through the Running action. Carry the explicit double-click intent through that
chain; `steal_focus=False` is not a substitute because ordinary single-click
selection uses the same value.

Portable soft-restart state writes the stable active `mode` key inside a
per-mode view map. The ownerless `codex_mode` boolean remains a read-only
migration fallback for Railmux 0.1.x files; it is never copied into new state.

The three lists use horizontal labelled rules instead of independent boxes, so
adjacent section borders do not consume duplicate terminal rows. There are no
duplicate per-section vertical borders: one shared rail on each side spans the
whole sidebar. The focused section owns green upper and lower horizontal rules
plus the matching height segments on both rails. Green corner glyphs join the
rails to each horizontal boundary so the focus outline closes without ordinary
vertical glyphs appearing to overrun the pane. When the next section's title row
doubles as that lower boundary, only the line and corners turn green and the next
title remains neutral. All other title rows and the final bottom rule use the
same subdued inactive gray. Neutral `┌┐` / `└┘` outer corners and `├┤` internal
junctions keep those rules joined to both rails when no section owns the
boundary. Weighted section heights are
deterministic for a given terminal size and must not change when focus moves.
The stable section name remains visible when dynamic title detail is truncated.
Wheel input over any title rule or the bottom rule is routed by pointer position
to that section's own `ListBox`.

## Restart state has two authorities

Instance-local recovery state lives under `XDG_RUNTIME_DIR` (or the existing
macOS-compatible `/tmp/railmux-UID` fallback). Its filename is derived from a
privacy-safe tmux server-lifetime digest plus the immutable outer pane ID. The
payload repeats that owner identity and is rejected unless it matches the live
instance. Session/window IDs are recorded as context but a move of the same
pane does not change ownership. Different panes, windows, sessions, and private
tmux servers therefore cannot overwrite or restore one another's local state.
The managed CLI session is the deliberate graceful-restart exception: its
controller pane exits with the session, so a private server-scoped handoff
points the replacement `railmux` session at that exact former owner. The
pointer is published only after the pane-owned snapshot validates, is accepted
only on the same tmux server after the former pane is dead, and is removed only
after restoration succeeds. Direct in-tmux instances retain strict
immutable-pane ownership and cannot consume this handoff.

The local schema may contain the right-pane target and validated running
bindings. It duplicates the current sidebar view so a shared portable
last-writer never changes an exact instance restart. Files are atomically
replaced as 0600 inside a verified user-owned 0700 runtime directory. Cleanup
is bounded and removes only recognized owners proven dead; unknown/newer state
and old but possibly-live private servers are retained.

Portable state lives beside `config.toml` and contains an active mode,
per-mode project/session selections and filters, plus an optional right-display
wish expressed only as provider mode, stable session ID, and project key. It
contains no tmux names, pane/process IDs, commands, environment values,
transcripts, or recovery authority. On restart Railmux may attach only after
the current tmux server independently rediscovers and validates that session as
live. If it is not live locally, the stable ID may select an existing transcript
for read-only preview but must never authorize resume, launch, kill, or process
adoption. A second node may therefore use it as a view default while ignoring
every node-local pane identity. The old fixed `railmux-state.json` has no owner
proof, so migration may extract only validated portable view fields;
right-pane and running-binding fields remain ignored and the legacy file is
left for manual cleanup.

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

Compound operations pin one generation, including both query methods and
`current_snapshot()`. Startup requests the first scan before recovery, but an
exact live tmux marker/stamp must remain visible in Running even while the
index is still at generation zero. Such an entry is provisional: the first
coherent generation removes and re-adopts it so metadata can refine its label
or reject a wrong cwd. A failed tmux probe, a generation with transient errors,
an unavailable initial source, or clean metadata that has not exposed the
actively-written rollout yet retains the provisional entry and instance
recovery file for a later generation; it must never publish a temporary empty
Running view.

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

`SessionMeta.message_count` is a provider-normalized logical conversation
count, not a raw JSONL-record count: exclude tool results and harness-injected
user context, and deduplicate provider records that share one assistant message
identity. `token_total` follows provider-reported usage. Codex token events are
cumulative, so the last valid total wins; Claude usage is summed once per
unique assistant message and includes reported input, output, cache-creation,
and cache-read tokens. The sidebar may compact these integers for display but
must retain the exact values in the immutable metadata snapshot.

## The agent workspace is independent of the sidebar mode

`AgentWorkspace` owns at most two `AgentSlot` objects: `primary` and
`secondary`. An agent slot owns every mutable fact about its outer display
pane: pane ID, attached background session, provider key, active session ID,
active project key, preview state, and preview restore target. Portable restart
state serializes only the stable provider/session/project subset of the active
slot; exact tmux display ownership remains instance-local.

The currently browsed sidebar mode and the providers displayed in agent slots
are independent. Switching the sidebar from Claude to Codex must not replace,
close, or reinterpret an already displayed Claude agent. Do not put display
pane fields back onto `App` as parallel scalars; the old `_right_pane_*`
properties exist only as compatibility shims backed by the primary slot.
An empty Projects or Sessions view must name the currently browsed provider and
offer its relevant new-project/session action; it must never retain content from
the previously browsed provider.

Exact-owner local restart state serializes the layout, both slot contents,
Target pane, keyboard focus, preview rollback target, and any live collapsed
secondary agent. Restoration validates every agent against current discovery,
then rebuilds primary, layout, secondary, Target, and focus in that order. A
content failure degrades to the branded empty surface without falsely claiming
the old agent; an unusable split degrades to single while retaining a validated
secondary agent in Running. Portable restoration deliberately remains a single
stable Target display wish and never carries tmux identity or process authority.

## Agent display transports preserve one ownership model

The default `swap` transport moves the real agent pane into the display window.
The `nested` transport runs a tmux client in the outer display pane and remains
both an explicit compatibility choice and the automatic fallback whenever swap
cannot be proven safe. Both are provider-neutral and are selected behind
`AgentDisplayTransport`; attach, preview, close, delete, liveness, and teardown
must not bypass that boundary with a destructive `respawn-pane`, `kill-pane`,
or `kill-session`.

In swap mode `AgentSlot.pane_id` is the pane physically visible in that slot.
It is the placeholder while idle/previewing and the real provider pane while
displayed. `SwapState` owns the immutable real-pane/PID, home window,
placeholder, display window, outer session, keeper, slot, and transaction phase.
The same real pane may be owned by only one slot.

An intentional session kill is a display transaction, not a raw
`kill-session`: the transport first returns a swap-owned real pane home or
replaces a nested attach client, then respawns the retained outer pane with the
idle surface and clears only that slot's content state. The caller may remove
the Running entry only after the exact tmux identity is confirmed dead. A
failed kill therefore leaves a truthful empty display slot and a still-live
Running entry that can be reopened; it must not collapse an explicitly chosen
dual layout.

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

Pane movement preserves each window's active pane (`swap-pane -d`). Only an
explicit user-intent path may select the agent display, so a single-click
preview or attach cannot undo the mouse-selected sidebar focus as a side effect
of returning or displaying a real pane.

Soft quit may release UI-only resources and return displayed panes home, but it
must branch before the detached-session kill loop. Hard-quit destruction must
remain below that explicit decision so adding teardown work cannot silently
turn a soft restart into loss of live agents.

User-requested exit paints a non-interactive progress surface before any
synchronous pane/session cleanup. Core cleanup runs while Urwid still owns the
sidebar, so the sidebar cannot disappear while the agent pane remains alive.
Core and outer-session phases are separately idempotent: the visible path may
complete core cleanup, while `run()`'s `finally` retries an interrupted phase
and performs only the remaining outer-session cleanup.

The swap floor is tmux 2.7. tmux 2.7 and 2.8 lack `resize-window`, so
their native swap geometry may reflow a long inline transcript. This is a
performance/visual limitation, not permission to alter provider history or
alternate-screen behavior. Full evidence and remaining gates are in
`docs/DENESTED_AGENT_PANE.md`.

## Dual-agent interaction target

The first version should remain bounded to two slots and preserve all existing
single-pane behavior:

### Focus and target terminology

These are separate state axes and their names are a durable product contract:

- **Focused pane / 焦点窗格** is the pane currently receiving keyboard input.
  When the sidebar is focused, neither agent pane is the Focused pane.
- **Target pane / 目标窗格** is the remembered agent pane where actions started
  from the sidebar take effect. Preview, open, running-session switching, F9,
  terminal placement, status, and attention routing all use it.
- While an agent pane is focused it is also the Target pane. Moving focus back
  to the sidebar clears agent focus but does not change the Target pane.
- `AgentWorkspace.target_slot_key`, `AgentWorkspace.target`, and `set_target()`
  are the model names; code and documentation must not use “active” to mean the
  remembered sidebar action target. The previously released `active_slot_key`,
  `active`, and `activate()` names remain thin compatibility views only.
- `AgentWorkspace` remains the Target authority. App-level Target transitions
  project its current outer pane ID into `@railmux_target_pane` solely for the
  managed `Ctrl-B Tab` binding; tmux pane history and the projection never
  become independent sources of Target state.

User-facing English should say **Target pane** and Chinese documentation should
say **目标窗格**. The compact status UI uses a workspace map rather than text:
`▣` means single; `◧`/`◨` mean side-by-side with P1/P2 targeted; `⬒`/`⬓` mean
stacked with P1/P2 targeted. Do not substitute “active pane / 活动窗格”,
“selected pane / 选中窗格”, or “last pane / 上一个窗格”: each conflates target
routing with focus, selection, or history.

- F8 creates an inert secondary slot before any session is chosen and advances
  through the layout cycle by selecting the next orientation that meets the
  minimum size. An unavailable side-by-side or stacked layout is skipped. If
  neither split fits when starting from single, F8 keeps single-pane layout and
  reports the size limit.
- Single-click, `␣`, and context Preview share one action: preview a stopped
  row, or switch a running row while sidebar focus stays put. Enter and
  double-click share the focus-transferring open action. Every path uses the
  Target pane remembered from tmux focus.
- Cycling back to single removes only the outer secondary pane, remembers its
  exact instance-local tmux target, and never kills the detached agent session.
- The same background tmux session should not be attached in both slots.
- Layout names are `stacked` and `side-by-side`, avoiding ambiguous
  horizontal/vertical terminology.
- Orientation changes only through F8 and must not flip during terminal resize.
  Single-agent layout assigns about 30% of the outer width to the sidebar;
  either dual layout assigns about 20%, clamped to at least 30 columns. Ratio
  changes are best-effort and must not make layout creation or recovery fail.
- Narrow screens should prefer stacked panes because three side-by-side columns
  make agent TUIs unusably narrow.
- Railmux globally routes `F8` to the sidebar controller and cycles
  single → side-by-side → stacked even while an agent owns keyboard focus.
  `F9` similarly reaches the controller and uses the Target pane resolved
  from real tmux focus.
- A crash-safe managed prefix-table `Ctrl-B Tab` binding toggles directly
  between the sidebar controller and the projected Target pane. It must gate
  on the Railmux window before inspecting pane IDs, preserve any prior prefix
  Tab behavior elsewhere, no-op when no Target pane exists, and restore only
  bindings/options still owned by its transaction. Arrow navigation remains
  spatial and is never reinterpreted as a Target-preserving shortcut. An
  existing repeatable or annotated prefix-Tab binding cannot be wrapped
  faithfully by one server-global conditional binding, so Railmux leaves it
  untouched, reports the unavailable toggle, and keeps F8/F9 forwarding active.
- Each projected agent pane must be at least 50x12. Side-by-side is preferred
  only when both projected panes reach 80x20; otherwise the best valid layout
  wins, with stacked breaking a tie.
- While an agent owns keyboard focus, native tmux borders show it in bright
  green. A side-by-side agent focus also enables inward border arrows so the
  shared Pane 1 / Pane 2 edge identifies its owner; arrows are omitted on tmux
  versions before 3.3. When Railmux regains focus, arrows are removed and all
  agent borders become gray. The status brand's one-cell workspace map remains
  visible across focus changes and its filled half names the Target pane without
  presenting it as current input focus. A single layout uses `▣` because P1 is
  the only possible target. While side-by-side Pane 1 has keyboard focus, the
  hint bar includes `C-b → Pane 2`; Pane 2 shows `C-b ← Pane 1`. Direct P1/P2
  focus changes refresh that hint with the workspace map and briefly confirm
  `Agent Pane 1 focused` or `Agent Pane 2 focused`. Teardown restores the exact
  inherited or explicit `pane-border-indicators` window option. Border
  colours and indicators form one applied state: if either tmux update fails,
  the periodic refresh retries both until the visible focus state converges.
  Hint-bar directions follow geometry: left/right names side-by-side neighbors,
  up/down names stacked neighbors, and `Ctrl-B Tab` always names the direct
  Sidebar/Target route.

Attach/resume, replacement, display-transport ownership, duplicate prevention,
close/rotate, per-pane size checks, preview/restore, terminal placement,
liveness, status/attention targeting, scrolling, F9, persistence selection, and
teardown operate on explicit slots. Direct agent focus is resolved from tmux's
active pane while the sidebar is unfocused and from `pane_last` when focus
returns. A direct P1/P2 focus change must repaint the workspace map when that
resolution changes the Target pane; it must not wait for sidebar focus to
return. Terminal `focus in`/`focus out` reports are advisory because hosts may
deliver them after a programmatic pane transition; both event handling and the
normal refresh converge on tmux's actual active pane. Preview/open actions use
that Target pane; the primary compatibility entry points remain only for
established single-pane integrations.

If secondary disappears, restore its live target into the same orientation or
collapse truthfully to single. If primary disappears while secondary survives,
return secondary home before rebuilding primary or promoting the survivor; do
not relabel slot-specific swap ownership in memory. A recovery ambiguity must
leave the agent in Running rather than destroy a pane. Soft restart persists the
full exact-owner workspace after bounded field validation; shared portable state
continues to restore only one stable display wish into primary.

## Global bindings preserve user tmux configuration

F8/F9 are root-table bindings, so Railmux manages them as a server-wide,
crash-safe transaction rather than unconditionally overwriting and unbinding
them. The wrapper is shared by every Railmux instance on the server and reads a
window-local `@railmux_controller_pane` option at keypress time. It forwards
only inside a Railmux window; elsewhere it replays the exact captured command,
or sends the function key through when it was originally unbound. Each owner
sets and conditionally clears only its own controller option. The final live
owner restores each original binding only while that key still carries the
transaction marker, so a user tmux configuration reload takes precedence.
Dead owners and interrupted installs are repaired by the next instance under a
non-blocking, server-keyed runtime lock.

`MouseDown3Pane` shares that controller-scoped transaction. Inside a Railmux
window, a mouse-aware pane is selected by pointer location before the event is
forwarded, matching tmux's stock left-click routing and allowing an unfocused
sidebar to receive its context-menu click. Other windows replay the exact prior
right-click command. Teardown restores it only while Railmux's marker still
owns the binding, so a user configuration reload remains newer authority.

tmux routes wheel events by pointer location rather than keyboard focus. Each
sidebar pane therefore consumes buttons 4/5 at its outer widget boundary and
routes them to its own `ListBox`, including events over titles, borders,
dividers, and pinned action rows.

For tmux to deliver both directions to Urwid, Railmux temporarily wraps the
server-global root `WheelUpPane` and `WheelDownPane` bindings. This is allowed
only on tmux 2.7+ when the root bindings match stock behavior; a custom binding
disables forwarding without mutation. All Railmux panes on one tmux server
share a versioned transaction in the private runtime directory, keyed by the
server lifetime and owned by immutable pane IDs. The final live owner restores
only per-key wrappers still carrying its random marker, so a user configuration
reload always wins. A later instance may prune dead owners and repair or remove
an interrupted transaction, but must never infer ownership from command shape
alone.

Copy-mode coalescing follows the same user-configuration rule. Its helper keeps
the exact currently displayed pane target, including a real pane moved by the
swap transport, and must reuse that target if the helper is recreated. On
teardown, restore each copy-mode wheel binding independently only while it
still targets that exact helper pane; a binding changed by the user is newer
authority and must remain untouched.

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

Modal overlays must remain inside the current sidebar pane after responsive
scaling. Long editable or read-only content scrolls within the modal while its
action legend remains visible; confirmation heights continue to derive from
wrapped content and clamp to the available terminal rows.

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
In a dual-agent layout, inactive borders stay gray and the active border turns
green. Stacked panes use the resulting horizontal divider and matching left
segment directly. Side-by-side Pane 1 necessarily colours both adjacent shared
borders, so tmux 3.3+ adds arrows pointing inward at the exact active pane.
When the sidebar owns focus, arrows are removed, every dual-agent border is
gray, and the status brand's filled layout glyph names the remembered target.
Glyph and colour changes must preserve that distinction between keyboard focus
and the remembered Target pane across supported tmux/terminal combinations.

## Liveness, activity, and attention are separate axes

Detached tmux/process ownership determines whether a session is running.
Provider lifecycle records determine conversational activity (`idle`, `busy`,
or `blocked`). An optional attention value records the last actionable terminal
outcome without changing either of those facts. Provider errors and aborts must
never prune a live registry entry or reuse the red blocked dot.

For Codex rollouts with lifecycle events, only `task_complete`, `turn_aborted`,
or `thread_rolled_back` ends an active turn; intermediate assistant messages and
tool results remain busy. Older rollouts without lifecycle records fall back to
the last user/assistant message. Codex does not persist a reliable approval-wait
signal, so a pending tool must remain unchanged for two minutes before the
session becomes blocked. This delay avoids classifying ordinary long-running
commands as approval waits.

Attention summaries come only from dedicated provider error/lifecycle fields and
must be short and sanitized. Never classify an error from user prompts,
assistant messages, tool output, or titles. A newer turn start and a newer
successful turn clear stale attention. User interrupts and explicit rollbacks
are not provider failures.

Current observed Codex rollouts do not persist a reliable capacity or rate-limit
reason. Such lifecycle errors remain generic unless a dedicated provider field
supplies a safe category; message text must never be used to guess one.

Running-pane filtering is a view over the live registry, never a mutation of
that registry. The pane retains the complete provider-scoped entry snapshot,
keys focus and callbacks by exact tmux session name, and performs fuzzy/project
matching only against indexed display metadata. Filter edits therefore cannot
hide a session from liveness management or trigger transcript I/O.
