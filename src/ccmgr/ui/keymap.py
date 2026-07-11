"""Single source of truth for ccmgr's action keybindings.

Drives both the key dispatch in ``App._on_input`` and the always-visible hint
bar (``HelpBar`` / ``hint_text_for``) so the two cannot drift — previously a key's
behaviour and the bar describing it were maintained separately.

Navigation / pane keys and keys that need a pane guard or an argument
(arrows, Tab, Esc, Ctrl-C, ``/``, ``[``, ``]``) are handled inline in App;
their entries here have ``action=None`` and exist only so the hint bar lists
them.
"""
from __future__ import annotations

from dataclasses import dataclass


# Context names passed to hint_text_for(); match sidebar focus positions.
CTX_PROJECTS = "projects"
CTX_SESSIONS = "sessions"
CTX_RUNNING = "running"
CTX_AGENT = "agent"  # right-hand agent pane has focus
_ALL_CTX = (CTX_PROJECTS, CTX_SESSIONS, CTX_RUNNING)


@dataclass(frozen=True)
class Binding:
    keys: tuple[str, ...]       # keys that trigger this action
    hint: str                   # short label for the hint bar, e.g. "n"
    desc: str                   # short description for the hint bar
    action: str | None = None   # App method name to call; None = handled inline
    # Sidebar pane contexts this binding is visible in.  None / empty = always.
    contexts: tuple[str, ...] | None = None


BINDINGS: list[Binding] = [
    # Sidebar navigation — not shown when the agent pane has focus.
    Binding(("up", "down"), "↑↓", "move",
            contexts=_ALL_CTX),
    Binding(("tab", "shift tab"), "Tab", "pane",
            contexts=_ALL_CTX),
    Binding(("enter",), "↵", "open",
            contexts=_ALL_CTX),
    # Projects & Sessions — creating a new session needs a project.
    Binding(("n", "N"), "n", "new", "_launch_new_session",
            contexts=(CTX_PROJECTS, CTX_SESSIONS)),
    # Projects & Sessions — Running pane doesn't support filtering.
    Binding(("/",), "/", "filter",
            contexts=(CTX_PROJECTS, CTX_SESSIONS)),
    # All three — _open_info_modal adapts to the focused pane.
    Binding(("i", "I"), "i", "info", "_open_info_modal",
            contexts=_ALL_CTX),
    # Sessions only — rename operates on the focused session.
    Binding(("r", "R"), "r", "rename", "_on_rename_session",
            contexts=(CTX_SESSIONS,)),
    # Sessions only — star/favorite targets a session.
    Binding(("s", "S"), "s", "star", "_on_toggle_star",
            contexts=(CTX_SESSIONS,)),
    # Sessions & Running — kill works on focused session or running entry.
    Binding(("k", "K"), "k", "kill", "_on_kill_session",
            contexts=(CTX_SESSIONS, CTX_RUNNING)),
    # Sessions only — delete removes the session JSONL.
    Binding(("d", "D"), "d", "del", "_on_delete_session",
            contexts=(CTX_SESSIONS, CTX_RUNNING)),
    # All three — opens a shell in the active project's directory.
    Binding(("t", "T"), "t", "term", "_open_terminal_for_active_project",
            contexts=_ALL_CTX),
    # Display-only: handled by a tmux root binding, not ccmgr.
    Binding((), "F9", "fullscreen"),
    # Agent pane — shown only when the right-hand agent has focus.
    Binding((), "C-b ←", "back",
            contexts=(CTX_AGENT,)),
]

_TRAILING: list[Binding] = [
    Binding(("?",), "?", "help", "_open_help_modal",
            contexts=_ALL_CTX),
    Binding(("q", "Q"), "q", "quit", "_open_quit_confirm",
            contexts=_ALL_CTX),
    Binding((), "C-b d", "detach",
            contexts=_ALL_CTX),
]

_ALL = BINDINGS + _TRAILING


def _visible_in(binding: Binding, context: str | None) -> bool:
    """True when *binding* should appear in *context*.

    ``context=None`` means "show everything" (legacy / no filter).
    """
    if not binding.contexts or context is None:
        return True
    return context in binding.contexts


def hint_text_for(context: str | None = None) -> str:
    """Two-line reference: main actions for *context* on the first line,
    utility/exit actions (always the same) on the second.

    Pass ``None`` (or omit) for the legacy all-keys view.
    """
    main = " · ".join(
        f"{b.hint} {b.desc}"
        for b in BINDINGS
        if _visible_in(b, context)
    )
    trail = " · ".join(
        f"{b.hint} {b.desc}"
        for b in _TRAILING
        if _visible_in(b, context)
    )
    return f"{main}\n{trail}"


def hint_text() -> str:
    """Legacy entry point (no context → all keys)."""
    return hint_text_for()


def action_for(key: str, context: str | None = None) -> str | None:
    """App method name for a dispatched action key, or None if not dispatched
    here (navigation / inline-handled / unknown).

    When *context* is passed the binding's ``contexts`` field is checked so
    rename/star are never dispatched from the Running pane (where they would
    act on a stale Sessions-pane row)."""
    for b in _ALL:
        if b.action and key in b.keys and _visible_in(b, context):
            return b.action
    return None
