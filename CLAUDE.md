# ccmgr

Terminal UI for Claude Code sessions — urwid left sidebar + tmux right pane.

## Non-obvious constraint

`_refresh()` (app.py) runs on a ~1s timer and rebuilds **every row widget** in all three
panes unconditionally — there is no dirty-check.  Therefore **never store transient
interaction state on row instances.**  Timers, click tracking, drag state, etc. must live
at class level and be keyed by row identity (`session_id`, `tmux_name`, `encoded_name`).

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

3. **State file keys need backward-compatible defaults.**  `/tmp/ccmgr-state-{uid}.json`
   is read by `_load_state` on startup.  Adding a required key to `_save_state`
   without a fallback in `_load_state` crashes the restore path (and deletes the
   state file before the crash, losing the saved project).
