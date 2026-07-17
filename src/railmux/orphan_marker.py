"""Strict tmux-local identity marker for unresolved provider launches."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import hashlib
import fcntl
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from railmux import restart_state


SCHEMA_VERSION = 2
OPTION_NAME = "@railmux_orphan_v2"
MAX_BYTES = 4096
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_PHASES = frozenset({"launching", "unresolved", "resolved"})
_KEYS = frozenset({
    "version", "kind", "mode", "placeholder", "tmux_name",
    "tmux_session_id", "tmux_pane_id", "owner", "cwd", "created_at",
    "creation_token", "phase", "session_id",
})


@dataclass(frozen=True)
class Marker:
    mode_key: str
    placeholder_key: str
    tmux_name: str
    tmux_session_id: str
    tmux_pane_id: str
    owner: restart_state.OuterTmuxIdentity
    cwd: Path
    created_at: float
    creation_token: str
    phase: str
    session_id: str | None = None

    def resolved(self, session_id: str) -> "Marker":
        return replace(self, phase="resolved", session_id=session_id)

    def with_phase(self, phase: str) -> "Marker":
        return replace(self, phase=phase)

    def with_owner(
        self, owner: restart_state.OuterTmuxIdentity,
    ) -> "Marker":
        return replace(self, owner=owner)


def encode(marker: Marker) -> str:
    raw = json.dumps(
        {
            "version": SCHEMA_VERSION,
            "kind": "unresolved-provider-launch",
            "mode": marker.mode_key,
            "placeholder": marker.placeholder_key,
            "tmux_name": marker.tmux_name,
            "tmux_session_id": marker.tmux_session_id,
            "tmux_pane_id": marker.tmux_pane_id,
            "owner": marker.owner.to_json(),
            "cwd": str(marker.cwd),
            "created_at": marker.created_at,
            "creation_token": marker.creation_token,
            "phase": marker.phase,
            "session_id": marker.session_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("orphan marker is too large")
    return raw


def _string(data: dict, key: str, limit: int) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def decode(raw: str | None) -> Marker | None:
    try:
        size = len(raw.encode("utf-8")) if raw is not None else 0
    except UnicodeError:
        return None
    if raw is None or size > MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return None
    if (not isinstance(data, dict)
            or frozenset(data) != _KEYS
            or data.get("version") != SCHEMA_VERSION
            or data.get("kind") != "unresolved-provider-launch"):
        return None
    mode = _string(data, "mode", 64)
    placeholder = _string(data, "placeholder", 256)
    tmux_name = _string(data, "tmux_name", 256)
    tmux_session_id = _string(data, "tmux_session_id", 64)
    tmux_pane_id = _string(data, "tmux_pane_id", 64)
    cwd_raw = _string(data, "cwd", 4096)
    token = _string(data, "creation_token", 64)
    phase = _string(data, "phase", 32)
    created_at = data.get("created_at")
    session_id = data.get("session_id")
    owner = data.get("owner")
    if not all((mode, placeholder, tmux_name, tmux_session_id,
                tmux_pane_id, cwd_raw, token, phase)):
        return None
    if (not placeholder.startswith("__new__-")
            or not tmux_session_id.startswith("$")
            or not tmux_pane_id.startswith("%")
            or not _TOKEN_RE.fullmatch(token)
            or phase not in _PHASES
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not math.isfinite(float(created_at))
            or float(created_at) <= 0
            or not isinstance(owner, dict)):
        return None
    cwd = Path(cwd_raw)
    if (not cwd.is_absolute()
            or Path(os.path.normpath(cwd_raw)) != cwd):
        return None
    owner_values = (
        owner.get("server_digest"), owner.get("pane_id"),
        owner.get("session_id"), owner.get("window_id"),
    )
    server_pid = owner.get("server_pid")
    if (not all(isinstance(value, str) and value and len(value) <= 128
                for value in owner_values)
            or not isinstance(server_pid, int)
            or isinstance(server_pid, bool)
            or server_pid <= 0):
        return None
    if phase == "resolved":
        if (not isinstance(session_id, str) or not session_id
                or len(session_id) > 256
                or session_id.startswith("__new__-")):
            return None
    elif session_id is not None:
        return None
    return Marker(
        mode_key=mode,
        placeholder_key=placeholder,
        tmux_name=tmux_name,
        tmux_session_id=tmux_session_id,
        tmux_pane_id=tmux_pane_id,
        owner=restart_state.OuterTmuxIdentity(
            server_digest=owner_values[0],
            server_pid=server_pid,
            pane_id=owner_values[1],
            session_id=owner_values[2],
            window_id=owner_values[3],
        ),
        cwd=cwd,
        created_at=float(created_at),
        creation_token=token,
        phase=phase,
        session_id=session_id,
    )


def same_live_tmux(marker: Marker, pane: object) -> bool:
    """Match only immutable identity; pane PID is intentionally not authority."""
    return bool(
        getattr(pane, "pane_id", None) == marker.tmux_pane_id
        and getattr(pane, "session_id", None) == marker.tmux_session_id
        and getattr(pane, "session_name", None) == marker.tmux_name
        and not getattr(pane, "dead", True)
    )


def owner_available(
    marker: Marker,
    current: restart_state.OuterTmuxIdentity | None,
    live_panes: frozenset[str] | None,
) -> bool:
    """Permit same owner, or takeover after a full snapshot proves absence."""
    if current is None or marker.owner.server_digest != current.server_digest:
        return False
    if marker.owner.pane_id == current.pane_id:
        return True
    return live_panes is not None and marker.owner.pane_id not in live_panes


def _claim_lock_path(marker: Marker) -> Path:
    material = (
        f"{marker.owner.server_digest}\0{marker.tmux_session_id}\0"
        f"{marker.creation_token}"
    ).encode("utf-8")
    key = hashlib.sha256(material).hexdigest()
    return restart_state.runtime_base() / "railmux" / "claims" / key


def claim_owner(
    marker: Marker,
    current: restart_state.OuterTmuxIdentity,
    load: Callable[[str, str], str | None],
    store: Callable[[str, str, str | None], bool],
) -> Marker | None:
    """Non-blocking compare/write/readback owner claim under a crash-safe lock."""
    lock_path = _claim_lock_path(marker)
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = lock_path.parent.stat()
        if (not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077):
            return None
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return None
        # Compare while holding the lock. A contender that read the same dead
        # owner before us must observe our claim and fail instead of overwriting.
        saved = decode(load(marker.tmux_session_id, OPTION_NAME))
        if saved != marker:
            return None
        claimed = marker.with_owner(current)
        try:
            raw = encode(claimed)
        except ValueError:
            return None
        if not store(claimed.tmux_session_id, OPTION_NAME, raw):
            return None
        return claimed if decode(load(
            claimed.tmux_session_id, OPTION_NAME)) == claimed else None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
