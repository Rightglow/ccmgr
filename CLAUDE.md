# ccmgr

Terminal UI for Claude Code sessions — urwid left sidebar + tmux right pane.

## Non-obvious constraint

`_refresh()` (app.py) runs on a ~1s timer and pushes fresh data into all three panes.
Each pane dirty-checks: `set_projects` / `set_sessions` / `set_running` compare a signature
of the incoming data (frozen-dataclass value equality plus the rendered relative-time
labels — see each pane's `_rendered_data` / `_all_projects`) against the last render and
**skip the rebuild when nothing changed**, otherwise they discard and rebuild *all* rows.

So a row widget's lifetime is nondeterministic: it may survive untouched for many ticks,
or be thrown away and rebuilt the instant *any* field changes (e.g. a status dot flipping
busy→idle). Do **not** rely on rows being rebuilt every tick, and do **not** rely on them
persisting.  Therefore **never store transient interaction state on row instances.**
Timers, click tracking, drag state, etc. must live at class level and be keyed by row
identity (`session_id`, `tmux_name`, `encoded_name`).

See `ClickableRow`'s class-level `_last_click_*` / `_pending_*` fields and the `click_key`
parameter for the canonical pattern.

## Soft restart: fragile contracts

Three things that, if changed without updating the counterpart, silently break
soft quit → restart:

1. **`_safe_name` truncation and `_resolve_truncated_id` must stay in lockstep.**
   If the truncation length or character filter in `_safe_name` changes,
   `_resolve_truncated_id` must be updated to match, or orphan discovery will
   never map tmux session names back to full UUIDs.

2. **`_teardown_tmux` flag check ordering.**  The `if self._soft_quit_flag: return`
   guard must execute *before* the `for r in self._running: kill_session` loop.
   Don't insert new cleanup code that destroys user state above that guard.

3. **State file keys need backward-compatible defaults.**  `$XDG_RUNTIME_DIR/ccmgr-state.json`
   is read by `_load_state` on startup.  Adding a required key to `_save_state`
   without a fallback in `_load_state` crashes the restore path (and deletes the
   state file before the crash, losing the saved project).
