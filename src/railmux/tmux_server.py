"""Fail-closed routing for Railmux's dedicated tmux server."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, MutableMapping, Sequence


DEFAULT_SOCKET_LABEL = "railmux"
SOCKET_LABEL_ENV = "RAILMUX_TMUX_LABEL"
HISTORY_SOURCE_OPTION = "@railmux_history_source_v1"
_LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_HISTORY_SOURCE_SCHEMA = 1


class TmuxServerError(RuntimeError):
    """The dedicated tmux target is invalid or not safely addressable."""


class TmuxServerUnresponsive(TmuxServerError):
    """The dedicated tmux socket exists but did not answer promptly."""


@dataclass(frozen=True)
class TmuxServerTarget:
    socket_path: str
    server_pid: int


def target_argv(target: TmuxServerTarget, *args: str) -> list[str]:
    """Address one already-discovered server by its exact socket path."""
    if not target.socket_path or target.server_pid <= 0:
        raise TmuxServerError("invalid tmux server target")
    return ["tmux", "-S", target.socket_path, *args]


def _discover_label_target(
    label: str, *, timeout: float,
) -> TmuxServerTarget | None:
    """Resolve *label* without allowing the caller's ``TMUX`` to redirect it."""
    try:
        raw = subprocess.check_output(
            ["tmux", "-L", label, "display-message", "-p",
             "#{socket_path}\t#{pid}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ).strip()
    except subprocess.TimeoutExpired as exc:
        raise TmuxServerUnresponsive(
            f"the tmux server '{label}' is not responding"
        ) from exc
    except (OSError, subprocess.CalledProcessError):
        return None
    fields = raw.split("\t", 1)
    if len(fields) != 2 or not fields[0]:
        raise TmuxServerError("tmux returned an invalid server identity")
    try:
        server_pid = int(fields[1])
    except ValueError as exc:
        raise TmuxServerError("tmux returned an invalid server identity") from exc
    if server_pid <= 0:
        raise TmuxServerError("tmux returned an invalid server identity")
    return TmuxServerTarget(fields[0], server_pid)


def socket_label(env: Mapping[str, str] | None = None) -> str:
    """Return a safe non-default socket label.

    The environment override exists for isolated tests and intentionally
    separate Railmux instances.  It can never opt back into tmux's shared
    ``default`` socket.
    """
    source = os.environ if env is None else env
    label = source.get(SOCKET_LABEL_ENV, DEFAULT_SOCKET_LABEL)
    if not _LABEL_RE.fullmatch(label) or label == "default":
        raise TmuxServerError(
            f"{SOCKET_LABEL_ENV} must be 1-64 ASCII letters, digits, '.', "
            "'_' or '-', and must not be 'default'"
        )
    return label


def tmux_argv(*args: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Build an argv that can never fall back to tmux's default socket."""
    return ["tmux", "-L", socket_label(env), *args]


def current_socket_path(env: Mapping[str, str] | None = None) -> str | None:
    """Parse the exact socket path from tmux's ``TMUX`` environment value."""
    source = os.environ if env is None else env
    raw = source.get("TMUX")
    if not raw:
        return None
    fields = raw.rsplit(",", 2)
    if len(fields) != 3 or not fields[0]:
        return None
    return fields[0]


def discover_target(*, timeout: float = 2.0) -> TmuxServerTarget | None:
    """Resolve the live dedicated server without starting a new server."""
    return _discover_label_target(socket_label(), timeout=timeout)


def discover_legacy_target(*, timeout: float = 1.0) -> TmuxServerTarget | None:
    """Resolve tmux's historical ``default`` server without starting it."""
    return _discover_label_target("default", timeout=timeout)


def target_is_live(
    target: TmuxServerTarget, *, timeout: float = 1.0,
) -> bool:
    """Revalidate an immutable server identity through its exact socket."""
    try:
        raw = subprocess.check_output(
            target_argv(target, "display-message", "-p", "#{pid}"),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return raw == str(target.server_pid)


def target_has_session(
    target: TmuxServerTarget,
    session_id: str,
    *,
    timeout: float = 1.0,
) -> bool:
    """Check one immutable session identity on one immutable server."""
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        return False
    try:
        raw = subprocess.check_output(
            target_argv(
                target, "display-message", "-t", session_id, "-p",
                "#{pid}\t#{session_id}",
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return raw == f"{target.server_pid}\t{session_id}"


def target_session_id(
    target: TmuxServerTarget,
    session_name: str,
    *,
    timeout: float = 0.5,
) -> str | None:
    """Resolve one exact session name to its immutable ID on *target*."""
    if not session_name or "\t" in session_name or "\n" in session_name:
        return None
    try:
        output = subprocess.check_output(
            target_argv(
                target, "list-sessions", "-F",
                "#{pid}\t#{session_name}\t#{session_id}",
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    matches: list[str] = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[:2] != [str(target.server_pid), session_name]:
            continue
        session_id = fields[2]
        if session_id.startswith("$") and session_id[1:].isdigit():
            matches.append(session_id)
    return matches[0] if len(matches) == 1 else None


def encode_history_source(
    target: TmuxServerTarget,
    session_id: str,
    *,
    legacy: bool,
) -> str | None:
    """Encode a non-authoritative locator for a nested display's history."""
    if (
        target.server_pid <= 0
        or not session_id.startswith("$")
        or not session_id[1:].isdigit()
    ):
        return None
    return json.dumps(
        {
            "schema_version": _HISTORY_SOURCE_SCHEMA,
            "scope": "legacy" if legacy else "dedicated",
            "server_pid": target.server_pid,
            "session_id": session_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def resolve_history_source(
    raw: str,
    *,
    timeout: float = 0.5,
) -> tuple[TmuxServerTarget, str] | None:
    """Resolve and revalidate one bounded nested-history locator."""
    if not raw or len(raw) > 256:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "scope", "server_pid", "session_id",
    }:
        return None
    scope = payload.get("scope")
    server_pid = payload.get("server_pid")
    session_id = payload.get("session_id")
    if (
        payload.get("schema_version") != _HISTORY_SOURCE_SCHEMA
        or scope not in ("dedicated", "legacy")
        or not isinstance(server_pid, int)
        or isinstance(server_pid, bool)
        or server_pid <= 0
        or not isinstance(session_id, str)
        or not session_id.startswith("$")
        or not session_id[1:].isdigit()
    ):
        return None
    try:
        target = (
            discover_legacy_target(timeout=timeout)
            if scope == "legacy"
            else discover_target(timeout=timeout)
        )
    except TmuxServerError:
        return None
    if (
        target is None
        or target.server_pid != server_pid
        or not target_has_session(target, session_id, timeout=timeout)
    ):
        return None
    return target, session_id


def target_single_pane_id(
    target: TmuxServerTarget,
    session_id: str,
    *,
    timeout: float = 0.5,
) -> str | None:
    """Return the sole live pane of an exact target session, fail closed."""
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        return None
    try:
        output = subprocess.check_output(
            target_argv(
                target, "list-panes", "-s", "-t", session_id, "-F",
                "#{pid}\t#{session_id}\t#{pane_id}\t#{pane_dead}",
            ),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    panes: list[str] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[:2] != [str(target.server_pid), session_id]:
            return None
        pane_id, dead = fields[2:]
        if (
            dead != "0"
            or not pane_id.startswith("%")
            or not pane_id[1:].isdigit()
        ):
            return None
        panes.append(pane_id)
    return panes[0] if len(panes) == 1 else None


def kill_target_session(
    target: TmuxServerTarget,
    session_id: str,
    *,
    timeout: float = 2.0,
) -> bool:
    """Kill exactly one revalidated session on an explicitly chosen server."""
    if not target_has_session(target, session_id, timeout=timeout):
        return False
    try:
        subprocess.run(
            target_argv(target, "kill-session", "-t", session_id),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return not target_has_session(target, session_id, timeout=timeout)


def is_current_server(target: TmuxServerTarget | None = None) -> bool:
    """Whether ``TMUX`` addresses the same Unix socket as the dedicated server."""
    current = current_socket_path()
    if current is None:
        return False
    resolved = discover_target() if target is None else target
    if resolved is None:
        return False
    try:
        return Path(current).samefile(resolved.socket_path)
    except OSError:
        # Identity must be proven.  A matching basename or unresolved path is
        # insufficient because a foreign ``tmux -S .../railmux`` can spoof it.
        return False


def exec_environment(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the environment for nesting into the dedicated tmux server."""
    result = dict(os.environ if env is None else env)
    result.pop("TMUX", None)
    result.pop("TMUX_PANE", None)
    return result


@contextmanager
def scoped_target_environment(
    target: TmuxServerTarget,
    env: MutableMapping[str, str] | None = None,
) -> Iterator[None]:
    """Temporarily route legacy bare tmux helpers to one proven target.

    This is used only by the single-threaded CLI before ``exec`` so the
    interrupted-swap repair can run on the dedicated server without touching
    the caller's server.
    """
    target_env = os.environ if env is None else env
    saved = {name: target_env.get(name) for name in ("TMUX", "TMUX_PANE")}
    target_env["TMUX"] = f"{target.socket_path},{target.server_pid},0"
    target_env.pop("TMUX_PANE", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                target_env.pop(name, None)
            else:
                target_env[name] = value


def launcher_argv(
    launch_prefix: Sequence[str],
    forwarded_args: Sequence[str],
) -> list[str]:
    """Build the only supported entry into the dedicated Railmux workspace."""
    return tmux_argv(
        "new-session",
        "-A",
        "-s",
        "railmux",
        *launch_prefix,
        "--inside-tmux",
        *forwarded_args,
    )
