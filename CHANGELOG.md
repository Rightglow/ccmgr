# Changelog

All notable changes to **ccmgr** will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.5] - 2026-05-22

### Added

- Active-pane focus highlight: bold-cyan border on the focused urwid pane and on the active tmux pane (window-scoped, so it does not leak into other tmux windows).
- Sessions row redesign to mirror `claude --resume`: title on top, dim `<time ago> · <branch> · <size>` below (branch sourced from the JSONL's `gitBranch`, size from the file's stat).
- Running-pane → sidebar sync: picking a running session now also switches the Projects and Sessions panes to that session's project.
- `release.yml` workflow that publishes to PyPI via Trusted Publishing (OIDC) when a `v*` tag is pushed.

### Changed

- Default ccmgr/claude split is now 30/70 (was 50/50).
- `__new__-N` placeholders in the Running pane resolve to the real session id and title on the next refresh tick instead of staying labeled `[project]/(new)`.
- Sessions row drops the ambiguous `38m` message-count chip and the unused token figure.

## [0.1.3] - 2026-05-18

### Added

- Initial public release of ccmgr — a tmux-backed terminal UI to navigate, resume, and start [Claude Code](https://claude.com/claude-code) sessions across projects.
- Projects and Sessions panes that read from `~/.claude/projects/*`.
- Per-session detached tmux sessions (`cc-<short_id>`) so in-progress claude work survives switching panes.
- Key bindings for navigation, focus switching, filtering (`/`), session details popup (`i`), help (`?`), and quit (`q` / `Ctrl-C`).
- `ccmgr --version` flag.

[Unreleased]: https://github.com/regmi-saugat/ccmgr/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/regmi-saugat/ccmgr/compare/v0.1.3...v0.1.5
[0.1.3]: https://github.com/regmi-saugat/ccmgr/releases/tag/v0.1.3
