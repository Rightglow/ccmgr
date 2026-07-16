# Changelog

All notable changes to **railmux** will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add an experimental, default-off de-nested agent display transport using
  transactional cross-session pane swaps, durable tmux recovery markers, and a
  zero-extra-pane session-group keeper. It returns real panes before preview,
  close, quit, or delete; repairs interrupted swaps; preserves agents across a
  direct outer-session kill; and falls back to nested attach for independent
  clients, unsupported topology, unmanaged sessions, or failed validation.
- Extend isolated real-tmux smoke coverage with swap/home, A/B switching,
  direct outer-session kill recovery, and independent-client fallback. The
  implementation path is verified on Linux with tmux 2.7 and 3.4 and remains in
  the existing Linux/macOS CI matrix.
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
  instead of clipping at line one. _(2026-07-10 02:12 +0800)_
- Hint bar is now context-sensitive: it lists only the action keys valid for
  the focused sidebar pane (Projects / Sessions / Running), sourced from the
  keymap so it can't drift from dispatch. Project/session filtering also matches
  fuzzily instead of requiring a contiguous substring. _(2026-07-10 17:59 +0800)_

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
  _(2026-07-10 02:12 +0800)_
- User-facing text now says "agent" instead of "Claude" where either agent may
  run (session counts, the right pane, error messages, tips, help) now that
  Codex sessions are supported; "Claude mode" / "Codex mode" toggle labels are
  kept as-is. Fullscreen toggle is F9 across the hint bar, help, and the tmux
  binding (previously the binding and the displayed key had drifted apart).
  _(2026-07-10 17:59 +0800)_
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

[Unreleased]: https://github.com/Rightglow/Railmux/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Rightglow/Railmux/releases/tag/v0.1.1
[0.1.0]: https://pypi.org/project/railmux/0.1.0/
