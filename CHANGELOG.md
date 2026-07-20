# Changelog

All notable changes to **railmux** will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-20

### Changed

- Document terminal-side right-click and F8/F9 forwarding, including the iTerm2
  Pointer setting required for Railmux's context menu.
- Wrap quit-confirmation choices to stay readable in a narrow sidebar, and
  document the bottom-left layout/Target-pane indicator in Help and README.
- Preserve idle agent sessions created by Railmux 0.1.3 and earlier, before
  durable tmux identity markers:
  conservatively migrate only detached, single-pane sessions whose immutable
  tmux identities, historical Railmux name, cwd, and launch command all agree,
  then install the current v2 marker plus compatibility binding for subsequent
  soft restarts and exact resolved-ID promotion.
- Give the two-line Sessions list half of the sidebar's vertical allocation,
  changing the Projects / Sessions / Running weights from 2:3:2 to 2:4:2.
- Replace the relative-age prefix on live Sessions rows with their current
  `idle`, `busy`, or `blocked` state, with actionable aborts shown as `aborted`.
- Replace branch and file-size metadata in both Claude Code and Codex session
  rows with compact logical-message and token counts. Keep this second line
  visually secondary and non-bold even while its row is focused or selected.
- Exclude tool results, harness-injected prompts, and duplicate Claude
  streaming records from logical-message counts; deduplicate Claude usage by
  provider message and include reported cache creation/read tokens.
- Add a second agent pane through `F8`, which can create an empty
  Pane 2 and cycles single, side-by-side, and stacked layouts globally while
  keeping the collapsed Pane 2 agent running. Any split orientation that cannot
  meet the minimum pane size is skipped, so the cycle uses only the layouts the
  current window supports. Empty agent panes now use a centered, resize-aware
  Railmux surface with compact interaction guidance; startup restoration uses
  the same visual language.
- Keep the sidebar at roughly 30% in single-agent layout and compact it to 20%
  in either dual-agent layout, with a 30-column floor. Returning to single
  restores the wider navigator, and ratio updates remain best-effort.
- Align `␣` and right-click Preview with single-click: preview stopped sessions,
  but switch/attach running sessions while sidebar focus stays put. Double-click
  and Enter open in the agent pane remembered from tmux focus and transfer
  focus. While the sidebar is active in a dual layout, agent borders return to
  honest gray and the status brand's compact workspace map identifies the
  exact neutral Target pane.
  Single-agent sidebar focus also uses a continuous gray divider, removing a
  stale per-pane target format that could leave half the line dim green after
  restart.
- Show a persistent one-cell workspace map after the provider name: `▣` for
  single, `◧`/`◨` for side-by-side, and `⬒`/`⬓` for stacked. The filled half
  identifies the Target pane across focus changes, including direct mouse
  movement between P1 and P2 without returning through the sidebar.
- Add `Ctrl-B Tab` as a direct Sidebar/Target-pane toggle so keyboard users can
  return from Pane 2 without passing through Pane 1 and changing the Target.
  Preserve any existing prefix-Tab binding outside Railmux, and make agent hints
  follow left/right side-by-side or up/down stacked geometry. Bindings that
  cannot be replayed faithfully are left untouched without disabling F8/F9.
- Establish **Target pane / 目标窗格** as the canonical name for the remembered
  agent pane where sidebar actions take effect, distinct from the **Focused
  pane / 焦点窗格** that currently receives keyboard input. The workspace model
  uses `target_slot_key`, `target`, and `set_target()` consistently; the
  previously released `active*` names remain compatibility views only.
- Disambiguate the shared green border in the side-by-side layout with inward
  tmux arrows that point at the exact focused agent pane. Directional markers
  are limited to agent focus, restore the prior window option on teardown, and
  degrade to colour-only borders on tmux versions older than 3.3. When Pane 1
  has focus, the hint bar shows `C-b → Pane 2`; Pane 2 names the matching
  `C-b ← Pane 1` route instead of calling it a direct return to Railmux.
- Retry partial tmux focus-border and directional-indicator updates during the
  normal refresh loop instead of caching them, preventing stale or missing
  green focus borders and old half-gray/half-green single-pane dividers.
- Resolve the Target pane from real tmux focus (including the last pane
  when returning to the sidebar). F9, transcript preview, terminal placement,
  status/attention targeting, scrolling, and soft-restart display selection no
  longer silently default to Pane 1. Moving directly between agent panes now
  briefly confirms `Agent Pane 1 focused` or `Agent Pane 2 focused`.
- Reconcile liveness and outer-pane disappearance across both slots and both
  providers. A lost Pane 2 collapses or rebuilds safely; if Pane 1 disappears,
  Railmux returns Pane 2 home before rebuilding or promoting its surviving
  agent, preserving slot-specific swap ownership.
- Manage the server-global F8/F9 wrappers as a crash-safe, multi-instance
  transaction. They forward only in Railmux windows, preserve prior behavior
  elsewhere, restore exact per-key originals on final teardown, and defer to
  any newer user tmux configuration.
- Restore the complete exact-owner agent workspace after a soft restart:
  layout, both validated pane contents, Target pane, keyboard focus, preview
  rollback target, and a collapsed secondary agent. Portable state remains a
  single stable display wish with no tmux process authority; invalid content or
  newly constrained geometry degrades to branded empty or single-pane UI while
  live agents remain discoverable in Running. Graceful restarts of the managed
  `railmux` tmux session now explicitly hand this snapshot to its replacement
  controller pane, whose immutable pane ID necessarily changes on relaunch.

### Fixed

- Reconcile terminal focus reports with tmux's actual active pane on every
  refresh, preventing a delayed `focus in` after a Pane 2 open/new-session
  action from leaving every agent border gray.
- Route right-click through a crash-safe, Railmux-window-only tmux wrapper that
  first selects the pane under the pointer, so an unfocused sidebar can open
  its context menu. Preserve and restore the exact prior right-click binding
  everywhere else.
- Size Rename from its wrapped title, keep modal action legends visible, make
  information popups scrollable, and clamp every overlay inside cramped
  sidebar dimensions.
- Keep each key-and-action hint together on one auto-flip page instead of
  separating combinations such as `C-b ←` from their destination.
- Route every displayed-session kill through the display transport, including
  ordinary resolved sessions. Swap panes now return home and nested clients
  detach before the exact tmux session is killed; the affected slot remains in
  the chosen dual-pane layout as a usable empty pane, failed kills stay in the
  Running registry, and stale display markers can no longer cascade errors.
- Quote and expand the controller pane correctly in the global F8/F9 tmux
  wrapper, preventing `-t expects an argument` when cycling layouts.

## [0.1.3] - 2026-07-17

### Changed

- Replace the three stacked sidebar boxes with labelled horizontal section
  rules inside one pair of shared vertical rails, reclaiming two rows while
  preserving pointer-local wheel routing. The focused section owns green upper
  and lower rules plus matching segments on both rails; focus changes no longer
  shift section heights, and narrow layouts keep every stable section name
  visible. Inactive section names and rules share one subdued gray when focus
  moves to the agent, while pinned-row separators remain secondary chrome. A
  shared lower boundary does not recolour the next section's title. Green corner
  glyphs join focused rail segments to their horizontal boundaries without
  visually overrunning them. Neutral outer corners and internal junctions also
  close the inactive frame cleanly, and the final rule uses the same inactive
  gray.
- Give modal action keys one shared high-contrast treatment across rename,
  quit, info, auto-run, help, path-browser, kill, and delete workflows. Rename
  now accepts `Ctrl-U` to clear a non-empty title without closing the popup,
  and visible Enter labels use the compact `↵` symbol.

### Fixed

- Preserve the active tmux pane during swap-transport moves, so a single click
  on a Sessions row no longer returns keyboard focus to the agent pane while
  previewing or attaching the selected session.

## [0.1.2] - 2026-07-17

### Added

- Add in-memory Running-pane filtering with plain fuzzy search, an optional
  `project:<name>` restriction, provider-aware empty states, per-mode queries,
  and exact tmux-identity focus retention across refreshes and sorting.
- Persist a bounded, versioned tmux marker before each new provider process
  starts. If Railmux exits before the provider exposes its UUID, restart now
  restores an explicit unresolved Running entry whose exact pane can be opened
  or stopped without guessing at or deleting provider history.
- Split soft-restart persistence into a portable per-mode sidebar view and
  exact-owner runtime recovery files, including isolated real-tmux coverage for
  multiple windows, sessions, and same-named sessions on private servers.
  On the one-time upgrade from the ownerless legacy file, only view preferences
  migrate; recovery bindings remain untouched and are not treated as authority.
- Add a source-tree-only, repeatable private-tmux benchmark for direct, nested,
  and swap output pipelines, A/B server-side switch timing, aggregate Linux CPU
  ticks, and diagnostic scroll-scheduling models. Document raw local results
  and their strict limitation: marker observation is not terminal paint or a
  real-provider/SSH measurement.
- Add a de-nested agent display transport using
  transactional cross-session pane swaps, durable tmux recovery markers, and a
  zero-extra-pane session-group keeper. It returns real panes before preview,
  close, quit, or delete; repairs interrupted swaps; preserves agents across a
  direct outer-session kill; and falls back to nested attach for independent
  clients, unsupported topology, unmanaged sessions, or failed validation.
- Extend isolated real-tmux smoke coverage with swap/home, A/B switching,
  direct outer-session kill recovery, and independent-client fallback. The
  implementation path is verified on Linux with tmux 2.7 and 3.4 and remains in
  the existing Linux/macOS CI matrix.
- Added a provider-derived attention state independent of tmux liveness and
  idle/busy/blocked activity. Sessions and Running rows use a separate `!`
  badge, info popups show sanitized details, and active errors receive a concise
  retry-aware status message without changing attach/preview actions.
- Added provider-neutral mode and at-most-two-slot agent-workspace foundations,
  plus internal architecture/roadmap guidance for future providers and dual
  agent panes. Current releases still expose the original single-agent layout.
- Warn when the outer workspace is below the recommended 120x30 layout size, or an
  individual agent pane is below 80x20, with stronger non-blocking warnings
  below 80x20 and 50x12 respectively.
- Missing-`tmux` startup checks now offer an explicit, default-no installation
  prompt for Homebrew on macOS and `apt-get` on Debian/Ubuntu/WSL. Other common
  Linux package managers receive an actionable manual command, while
  non-interactive launches never attempt to modify the system.
- Add `railmux --doctor`, a privacy-safe diagnostic report for provider,
  terminal, tmux, configuration, and data-directory health that works even
  when tmux is unavailable.
- Add provider-aware project/session onboarding text and non-blocking,
  path-safe warnings when the active mode's executable is unavailable.
- Add an isolated real-tmux smoke test on Linux and macOS CI, alongside Ruff
  lint and package build validation gates.

### Changed

- Make the validated `swap` display the default for managed Railmux sessions;
  `nested` remains an explicit compatibility choice and automatic safe fallback.
- Show an immediate startup surface while initial provider and tmux discovery
  runs, reuse the already-built project snapshot during orphan recovery, and
  avoid leaving a newly-created terminal pane apparently blank.
- Size destructive confirmation dialogs from their wrapped content, cap long
  bodies to a scrollable viewport, and render their action keys with an
  explicit high-contrast style.
- Make the one-line Button Bar responsive at narrow sidebar widths and paint a
  short pressed state before synchronous actions, so remote clicks receive an
  immediate visual acknowledgement without adding another focusable widget.
- Keep mode switching in the Button Bar and remove its duplicate `m Mode`
  entry from the context-sensitive Hint Bar; the `m` keyboard shortcut remains.
- Clarify the final `railmux --doctor` privacy note and remind users to review
  the redacted report before sharing it.
- Group blocked Running sessions ahead of other activity states during the
  existing throttled recency sort, without changing status-dot semantics or
  causing per-poll row movement.
- Move Codex history tree walking and rollout parsing off the UI thread into a
  single rate-limited worker. Sidebar refreshes now read immutable generation
  snapshots, coalesce repeated requests, retain the last good view on scan
  failure, and bound shutdown even when filesystem IO is stuck.
- Raise copy-mode wheel coalescing from 2 FPS to 10 FPS over SSH while keeping
  the immediate leading update, native scroll distance, and both nested and
  swap transport lifecycles unchanged.
- Use one grass-green focus system (`#5FAF00`): bright pane chrome and tmux
  status bar, a deep-green cursor row, a neutral slate persistent target, and
  grass-green live-session titles. Red/yellow/green status dots retain their
  meaning across cursor and target backgrounds, while stopped sessions use a
  neutral hollow marker. True-colour terminals receive the exact accent and
  other terminals use an automatically downsampled fallback.
- Use provider-neutral product copy throughout the shared UI and expand the
  README with status badges, a quick-start path, diagnostics guidance, and a
  reserved demo-GIF slot.

### Fixed

- Resolve a tmux topology target back to its actual session name when callers
  use an immutable `$id`, so a recovered marked Running entry is not falsely
  rejected as having changed identity.
- Remove the obsolete in-pane error row above the Button Bar. Errors now use
  the full-width tmux status bar exclusively, like warnings, tips, and other
  status messages, without resizing the sidebar footer.
- Keep the tmux server lifetime identity stable when its socket metadata is
  touched by a later client, and safely migrate exact legacy markers on the
  same live server. Soft restart no longer hides a surviving resolved Claude
  session from the Running pane.
- Paint a clicked session as the sole active sidebar target before beginning
  the synchronous agent transport transaction, so the previous session cannot
  linger as a second grey selection. Failed attaches restore the confirmed old
  target or reconcile to the transport's retained recovery state.
- Restore the most recently displayed stable agent session or transcript after
  a soft restart even when the outer tmux pane is recreated. Portable state
  carries only provider/session/project view identity: live processes must be
  rediscovered and validated locally, otherwise Railmux opens a read-only
  preview and never resumes or launches a provider implicitly.
- Keep double-click intent intact when a Sessions row redirects through an
  already-running entry, preventing the delayed right-pane focus transfer from
  being cancelled and bouncing back to the sidebar.
- Recreate a failed scroll helper against the exact displayed pane in swap
  mode, and restore copy-mode wheel bindings per key so a user tmux reload is
  preserved without leaving other wrappers pointed at a dead helper.
- Keep delete/kill confirmation controls visible for long ASCII or CJK session
  names by showing the name once in a scrollable body, pinning the action keys,
  and allocating more vertical space in the narrow sidebar.
- Deliver both macOS trackpad and mouse-wheel directions to every scrollable
  sidebar list, even when the pointer is over pane chrome or keyboard focus is
  elsewhere. Server-global tmux bindings are shared crash-safely, installed
  only over stock behavior, and restored without overwriting later user config.
- Keep an `Exiting…` progress surface visible while synchronous tmux cleanup
  runs, and split teardown into idempotent core/outer phases so the sidebar no
  longer disappears before the agent pane or repeats destructive cleanup.
- Preserve exact Codex sessions in Running across a soft restart while the
  background history index publishes its first generation. Startup recovery
  now pins one immutable generation, shows exact provisional entries instead
  of a false empty list, and revalidates them without dropping them on transient
  index/tmux failures or temporary rollout visibility delays.

- Close the crash window in which a new provider could outlive Railmux before
  receiving recovery metadata. Placeholder resolution now uses Linux rollout
  file-descriptor correlation when available, stays unresolved on ambiguity,
  commits the exact UUID to tmux before re-keying memory, and revalidates
  immutable tmux identity before unresolved attach or kill actions.

- Prevent simultaneous Railmux instances from overwriting or restoring one
  another's right pane and running bindings. Local state is namespaced by the
  tmux server lifetime and immutable outer pane, atomically written with
  restrictive permissions, and stale cleanup removes only owners proven dead.

- Read the containing tmux window rather than the narrow Urwid sidebar when
  evaluating workspace dimensions, so a restored split no longer reports a
  full-screen terminal as critically small. Rechecks are resize-event driven
  instead of adding a tmux query to every poll tick.
- Paint both tmux border styles together so the two-pane shared divider changes
  as one continuous line instead of showing only its lower half in focus green.
- Pre-size detached agent tmux windows to the exact outer pane dimensions before
  attach, preventing an immediate Codex resize from visibly replaying/reflowing
  long history when switching running sessions.
- Check for the `tmux` executable before every TUI startup path, including an
  inherited or explicitly forced inside-tmux launch, instead of entering a TUI
  whose controls cannot work when `TMUX` is set but the binary is absent.
- Remember each agent mode's project selection independently. Switching through
  a mode with no projects no longer leaves a hidden actionable project or loses
  the previous mode's Sessions view after the next refresh tick; deleted
  remembered projects fall back only to a currently visible project.
- Report malformed or invalid configuration as a concise actionable error
  instead of exposing a Python traceback.

## [0.1.1] - 2026-07-15

### Added

- New Project now works in Codex mode and can create missing relative,
  absolute, or `~`-based directories before launching the first session.
- Status bar now cycles short idle tips when there's no active message, and
  soft-wraps long messages across both lines (ellipsis only past two lines)
  instead of clipping at line one.
- Hint bar is now context-sensitive: it lists only the action keys valid for
  the focused sidebar pane (Projects / Sessions / Running), sourced from the
  keymap so it can't drift from dispatch. Project/session filtering also matches
  fuzzily instead of requiring a contiguous substring.

### Changed

- Stopped-session preview remains a zero-extra-dependency `less` viewer, but
  now identifies itself as read-only, documents its recent-record window,
  shows abbreviated Claude tool results and plaintext Codex reasoning
  summaries, and filters Codex-injected system context from user turns.
- History preview now sanitizes terminal control sequences, quotes the Python
  executable, disables `less` shell/editor/log/history features, and treats an
  early pager exit as a normal broken pipe instead of showing a traceback.
- Status-bar messages are levelled (info / warn / error) with distinct colours,
  and one-shot messages ("→ opened X", "Renamed to: …", "Killed: …") now persist
  for a level-dependent time (errors are sticky) instead of being overwritten by
  the next poll tick — fixing messages that previously flashed by unreadably.
- User-facing text now says "agent" instead of "Claude" where either agent may
  run (session counts, the right pane, error messages, tips, help) now that
  Codex sessions are supported; "Claude mode" / "Codex mode" toggle labels are
  kept as-is. Fullscreen toggle is F9 across the hint bar, help, and the tmux
  binding (previously the binding and the displayed key had drifted apart).
- Pane focus now follows the actual tmux input target: the sidebar drops focus
  styling while another pane is active, while the selected conversation and
  status colours remain visible. Shared tmux dividers now switch as one solid
  colour instead of mixing active and inactive segments.
- Removed the redundant `[LIVE]` badge; running state, status dots, and relative
  activity time remain the session activity indicators.
- Project and running-session single clicks now act immediately; initial session
  metadata loading, right-pane restoration, and scroll-acceleration setup are
  deferred until after the first sidebar frame so startup and pane switching
  remain responsive.
- Unchanged project, session, and running-session snapshots no longer rebuild
  their rows every poll, reducing terminal redraws on SSH while still updating
  relative-time labels when their displayed value changes. Live child-process
  probes are shared between both session views during each refresh.
- Running-session and pane liveness now share one on-demand tmux server
  snapshot, with targeted probes retained as a failure fallback. Codex session
  metadata is scanned once per poll and reused across the project, session, and
  running views.
- Project counts and global recency ordering now use a three-second snapshot,
  while selected-session and running status keep their original poll cadence.
  Placeholder resolution, deletion, and rename still force immediate discovery.
- Live child-process checks reuse pane PIDs from the tmux server snapshot
  instead of launching another tmux query per pending session.
- Raised the minimum supported Urwid version to 2.6.16 for focus reporting.

### Fixed

- Keep Codex turns busy until an explicit lifecycle end event. Intermediate
  assistant messages, completed tools, and continued reasoning no longer make
  a still-running turn flash green; legacy rollouts retain last-role fallback.
- Do not apply Claude's child-process status heuristic to Codex, whose wrapper,
  native client, and MCP/code-mode children are permanent and cannot identify
  an approval wait.
- Delay Codex's stale pending-tool red indicator from 10 seconds to two minutes,
  so ordinary builds, SSH commands, and other long tools do not demand user
  attention prematurely.
- Hide the optional in-pane launch-error row completely while empty, detect an
  immediately vanished tmux agent session as a launch failure, and sanitize
  captured subprocess errors before displaying them.
- Start the idle-tip cadence when the first post-message tip is rendered, so a
  following refresh tick cannot replace it before a full rotation interval.
- Keep Claude project counts synchronized when a startup stub becomes a real
  conversation or the last JSONL is deleted; empty projects are hidden by
  default and can be shown with `[projects] show_empty_projects = true`.
- Make unresolved New Project entries actionable from the Running-pane context
  menu, and wait for the agent writer to exit before deleting Claude history so
  shutdown cannot recreate a visible title-only stub.
- Unknown child-process probe results now fall back to JSONL-derived status.
- Removed stale project selection when its project disappears during refresh.
- Preserve soft-quit state until deferred right-pane restoration completes.
- Defer right-pane focus until tmux's late DoubleClick1Pane binding completes,
  preventing focus from bouncing back to the sidebar.
- Pre-paint the right-pane focus state as soon as a double-click is detected,
  so the sidebar highlight and center divider switch together while the real
  tmux focus transfer remains safely delayed.
- Keep status-bar truncation within a one-column viewport and clarify that F9
  targets the agent pane.
- Keep session metadata caches scoped by project and key them by nanosecond
  mtime plus size, ensuring appends during a Claude or Codex scan are picked up
  on the next poll.
- Persist soft-quit state, favorites, and the project path cache with atomic
  replacement, including creation of a missing fallback runtime directory.
- Retry history cleanup when Claude appends concurrently and never replace a
  history file whose signature changed during the read.

## [0.1.0] - 2026-07-14

### Added

- Initial PyPI release under the Railmux name.

[Unreleased]: https://github.com/Rightglow/Railmux/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Rightglow/Railmux/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/Rightglow/Railmux/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Rightglow/Railmux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Rightglow/Railmux/releases/tag/v0.1.1
[0.1.0]: https://pypi.org/project/railmux/0.1.0/
