"""Single source of truth for ccmgr's action keybindings.

Drives both the key dispatch in ``App._on_input`` and the always-visible hint
bar (``statusbar.HELP_HINT``) so the two cannot drift — previously a key's
behaviour and the bar describing it were maintained separately.

Navigation / pane keys and keys that need a pane guard or an argument
(arrows, Tab, Esc, Ctrl-C, ``/``, ``[``, ``]``) are handled inline in App;
their entries here have ``action=None`` and exist only so the hint bar lists
them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Binding:
    keys: tuple[str, ...]       # keys that trigger this action
    hint: str                   # short label for the hint bar, e.g. "n"
    desc: str                   # short description for the hint bar
    action: str | None = None   # App method name to call; None = handled inline


BINDINGS: list[Binding] = [
    Binding(("up", "down"), "↑↓", "move"),
    Binding(("tab", "shift tab"), "Tab", "pane"),
    Binding(("enter",), "↵", "open"),
    Binding(("n", "N"), "n", "new", "_launch_new_session"),
    Binding(("t", "T"), "t", "term", "_open_terminal_for_active_project"),
    Binding(("c", "C"), "c", "code", "_open_editor_for_active_project"),
    Binding(("/",), "/", "filter"),
    Binding(("i", "I"), "i", "info", "_open_info_modal"),
    Binding(("r", "R"), "r", "rename", "_on_rename_session"),
    Binding(("f", "F"), "f", "fav", "_on_toggle_favorite"),
    Binding(("d", "D"), "d", "del", "_on_delete_session"),
    Binding(("?",), "?", "help", "_open_help_modal"),
    Binding(("q", "Q"), "q", "quit", "_open_quit_confirm"),
]


def hint_text() -> str:
    """The one-line reference shown in the persistent hint bar."""
    return " · ".join(f"{b.hint} {b.desc}" for b in BINDINGS)


def action_for(key: str) -> str | None:
    """App method name for a dispatched action key, or None if not dispatched
    here (navigation / inline-handled / unknown)."""
    for b in BINDINGS:
        if b.action and key in b.keys:
            return b.action
    return None
