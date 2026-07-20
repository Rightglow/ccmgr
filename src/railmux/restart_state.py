"""Versioned, instance-safe soft-restart state storage.

Process identities belong to one live tmux pane and stay in the runtime
directory.  Roamable sidebar preferences live separately in the Railmux
configuration directory and never authorize a pane or process operation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from railmux import tmux_ctl
from railmux.atomic_file import atomic_write_text
from railmux.config import default_config_path


SCHEMA_VERSION = 1
_MAX_STATE_BYTES = 2 * 1024 * 1024
_MAX_CLEANUP_FILES = 64
_DEAD_STATE_GRACE_S = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class OuterTmuxIdentity:
    """Immutable owner identity for one Railmux pane in one tmux server."""

    server_digest: str
    server_pid: int
    pane_id: str
    session_id: str
    window_id: str

    @property
    def storage_key(self) -> str:
        # A pane id is immutable and server-wide. Session/window are recorded as
        # context but deliberately excluded: moving the same pane must not lose
        # its restart state.
        material = f"{self.server_digest}\0{self.pane_id}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    def to_json(self) -> dict:
        return {
            "server_digest": self.server_digest,
            "server_pid": self.server_pid,
            "pane_id": self.pane_id,
            "session_id": self.session_id,
            "window_id": self.window_id,
        }


def capture_outer_identity() -> OuterTmuxIdentity | None:
    """Capture the current pane plus a privacy-safe tmux server lifetime key."""
    raw_tmux = os.environ.get("TMUX")
    pane_id = os.environ.get("TMUX_PANE")
    if not raw_tmux or not pane_id:
        return None
    fields = raw_tmux.rsplit(",", 2)
    if len(fields) != 3 or not fields[0]:
        return None
    socket_path, raw_pid, _client_id = fields
    try:
        server_pid = int(raw_pid)
    except ValueError:
        return None
    if server_pid <= 0:
        return None
    pane = tmux_ctl.pane_identity(pane_id)
    if pane is None or pane.pane_id != pane_id or pane.dead:
        return None

    # Socket inode plus an immutable process/socket birth token distinguishes a
    # restarted private server even if its pathname and OS pid are eventually
    # reused.  Do not use ``st_ctime`` here: Unix socket metadata can be
    # touched while the same tmux server accepts a later client, which used to
    # change this digest across a Railmux soft restart and strand live agents.
    # Persist only the hash.
    server_parts = [socket_path, str(server_pid)]
    try:
        socket_stat = os.stat(socket_path)
        server_parts.extend([
            str(socket_stat.st_dev),
            str(socket_stat.st_ino),
        ])
        birth_ns = getattr(socket_stat, "st_birthtime_ns", None)
        birth = getattr(socket_stat, "st_birthtime", None)
        if isinstance(birth_ns, int):
            server_parts.append(f"birth-ns:{birth_ns}")
        elif isinstance(birth, (int, float)):
            server_parts.append(f"birth:{birth!r}")
    except OSError:
        # tmux is already answering exact pane queries, so the socket+pid pair
        # remains a useful conservative identity when stat is unavailable.
        pass
    process_start = _process_start_token(server_pid)
    if process_start is not None:
        server_parts.append(process_start)
    server_digest = hashlib.sha256(
        "\0".join(server_parts).encode("utf-8")
    ).hexdigest()
    return OuterTmuxIdentity(
        server_digest=server_digest,
        server_pid=server_pid,
        pane_id=pane.pane_id,
        session_id=pane.session_id,
        window_id=pane.window_id,
    )


def _process_start_token(pid: int) -> str | None:
    """Return Linux's immutable process start tick without exposing proc data.

    ``/proc/<pid>/stat`` field 22 is the process start time in clock ticks
    since boot.  The command name (field 2) may contain spaces or parentheses,
    so split only after its final closing parenthesis.  Other platforms safely
    fall back to the socket inode and, where available, socket birth time.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        _head, separator, tail = raw.rpartition(")")
        fields = tail.strip().split() if separator else []
        # ``fields[0]`` is field 3 (state), making index 19 field 22.
        start_ticks = fields[19]
        if not start_ticks.isdecimal():
            return None
        return f"proc-start:{start_ticks}"
    except (OSError, UnicodeError, IndexError):
        return None


def runtime_base() -> Path:
    run_dir = os.environ.get("XDG_RUNTIME_DIR")
    if run_dir:
        return Path(run_dir)
    return Path(f"/tmp/railmux-{os.getuid()}")


def instances_dir() -> Path:
    return runtime_base() / "railmux" / "instances"


def instance_state_path(identity: OuterTmuxIdentity) -> Path:
    return instances_dir() / f"instance-{identity.storage_key}.json"


def portable_state_path() -> Path:
    return default_config_path().parent / "view-state.json"


def legacy_state_path() -> Path:
    return runtime_base() / "railmux-state.json"


def _ensure_private_dir(path: Path) -> None:
    """Create and verify a Railmux-owned 0700 runtime directory."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise OSError("Railmux runtime state directory is not private")


def _ensure_private_runtime_tree() -> None:
    """Protect every Railmux-owned layer of the production runtime path."""
    base = runtime_base()
    if not os.environ.get("XDG_RUNTIME_DIR"):
        # The /tmp fallback itself is Railmux-owned; unlike an externally
        # managed XDG runtime root, it must not retain umask-default 0755.
        _ensure_private_dir(base)
    _ensure_private_dir(base / "railmux")
    _ensure_private_dir(base / "railmux" / "instances")


def runtime_state_dir() -> Path:
    """Return the verified private directory for runtime coordination."""
    _ensure_private_runtime_tree()
    return runtime_base() / "railmux"


def read_json_object(path: Path) -> dict | None:
    """Read a bounded JSON object, treating every malformed shape as absent."""
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_portable(payload: dict, path: Path | None = None) -> bool:
    target = path or portable_state_path()
    try:
        atomic_write_text(
            target,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def write_instance(
    identity: OuterTmuxIdentity,
    payload: dict,
    path: Path | None = None,
) -> bool:
    target = path or instance_state_path(identity)
    try:
        if target.parent == instances_dir():
            _ensure_private_runtime_tree()
        else:
            _ensure_private_dir(target.parent)
        atomic_write_text(
            target,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def owner_matches(raw: object, identity: OuterTmuxIdentity) -> bool:
    """Validate the immutable owner; session/window remain moveable context."""
    if not isinstance(raw, dict):
        return False
    return (
        raw.get("server_digest") == identity.server_digest
        and raw.get("server_pid") == identity.server_pid
        and not isinstance(raw.get("server_pid"), bool)
        and raw.get("pane_id") == identity.pane_id
        and isinstance(raw.get("session_id"), str)
        and len(raw["session_id"]) <= 64
        and isinstance(raw.get("window_id"), str)
        and len(raw["window_id"]) <= 64
    )


def _bounded_string(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def _validate_workspace_slot(raw: object) -> dict | None:
    """Validate one exact-owner display wish without granting new authority."""
    if not isinstance(raw, dict) or len(raw) > 8:
        return None
    kind = _bounded_string(raw.get("kind"), 16)
    if kind not in {"empty", "agent", "preview"}:
        return None
    slot: dict = {"kind": kind}
    fields = {
        "tmux": 256,
        "session": 256,
        "mode": 64,
        "project": 512,
    }
    for key, limit in fields.items():
        value = _bounded_string(raw.get(key), limit)
        if value is not None:
            slot[key] = value
    if kind == "agent" and "tmux" not in slot:
        return None
    if kind == "preview" and not {"session", "mode"} <= slot.keys():
        return None
    restore = raw.get("restore")
    if restore is not None:
        if not isinstance(restore, dict) or len(restore) > 2:
            return None
        restore_kind = _bounded_string(restore.get("kind"), 16)
        restore_tmux = _bounded_string(restore.get("tmux"), 256)
        if restore_kind == "empty":
            slot["restore"] = {"kind": "empty"}
        elif restore_kind == "agent" and restore_tmux:
            slot["restore"] = {"kind": "agent", "tmux": restore_tmux}
        else:
            return None
    return slot


def _validate_workspace(raw: object) -> dict | None:
    """Decode the optional full workspace owned by this exact outer pane."""
    if not isinstance(raw, dict) or len(raw) > 7 or raw.get("version") != 1:
        return None
    layout = _bounded_string(raw.get("layout"), 32)
    target = _bounded_string(raw.get("target"), 16)
    focus = _bounded_string(raw.get("focus"), 16)
    if layout not in {"single", "side-by-side", "stacked"}:
        return None
    if target not in {"primary", "secondary"}:
        return None
    if focus not in {"sidebar", "primary", "secondary"}:
        return None
    if focus != "sidebar" and focus != target:
        return None
    if layout == "single" and (target != "primary" or focus == "secondary"):
        return None
    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, dict) or set(raw_slots) != {
        "primary", "secondary",
    }:
        return None
    primary = _validate_workspace_slot(raw_slots["primary"])
    secondary = _validate_workspace_slot(raw_slots["secondary"])
    if primary is None or secondary is None:
        return None
    if layout == "single" and secondary["kind"] != "empty":
        return None
    workspace = {
        "version": 1,
        "layout": layout,
        "target": target,
        "focus": focus,
        "slots": {"primary": primary, "secondary": secondary},
    }
    raw_collapsed = raw.get("collapsed_secondary")
    if raw_collapsed is not None:
        if not isinstance(raw_collapsed, dict) or len(raw_collapsed) > 3:
            return None
        collapsed = {
            key: _bounded_string(raw_collapsed.get(key), limit)
            for key, limit in (
                ("tmux", 256), ("session", 256), ("mode", 64),
            )
        }
        if not all(collapsed.values()):
            return None
        workspace["collapsed_secondary"] = collapsed
    return workspace


def validate_view(raw: object) -> dict | None:
    """Flatten the active mode from a validated, extensible view schema."""
    if not isinstance(raw, dict):
        return None
    mode = _bounded_string(raw.get("active_mode"), 64)
    if not mode:
        return None
    modes = raw.get("modes")
    if not isinstance(modes, dict) or len(modes) > 32:
        return None
    active = modes.get(mode)
    if not isinstance(active, dict):
        return None
    view: dict = {"mode": mode}
    for key, limit in (("project", 512), ("session", 256)):
        value = _bounded_string(active.get(key), limit)
        if value is not None:
            view[key] = value
    filters = active.get("filters", {})
    if not isinstance(filters, dict) or len(filters) > 16:
        return None
    for source, output in (
        ("projects", "project_filter"),
        ("sessions", "session_filter"),
    ):
        value = _bounded_string(filters.get(source), 512)
        if value is not None:
            view[output] = value
    display = active.get("display")
    if display is not None:
        if not isinstance(display, dict) or len(display) > 8:
            return None
        kind = _bounded_string(display.get("kind"), 16)
        display_mode = _bounded_string(display.get("mode"), 64)
        session = _bounded_string(display.get("session"), 256)
        if kind not in {"agent", "preview"} or not display_mode or not session:
            return None
        view.update({
            "right_kind": kind,
            "right_mode": display_mode,
            "right_session": session,
        })
        project = _bounded_string(display.get("project"), 512)
        if project is not None:
            view["right_project"] = project
    return view


def build_view(flat: dict) -> dict:
    """Encode App's active flat view into the per-mode portable schema."""
    mode = flat["mode"]
    active: dict = {}
    for key in ("project", "session"):
        value = flat.get(key)
        if isinstance(value, str):
            active[key] = value
    filters: dict = {}
    for source, target in (
        ("project_filter", "projects"),
        ("session_filter", "sessions"),
    ):
        value = flat.get(source)
        if isinstance(value, str):
            filters[target] = value
    if filters:
        active["filters"] = filters
    kind = flat.get("right_kind")
    display_mode = flat.get("right_mode")
    session = flat.get("right_session")
    if (kind in {"agent", "preview"}
            and isinstance(display_mode, str) and display_mode
            and isinstance(session, str) and session):
        display = {
            "kind": kind,
            "mode": display_mode,
            "session": session,
        }
        project = flat.get("right_project")
        if isinstance(project, str):
            display["project"] = project
        active["display"] = display
    return {"active_mode": mode, "modes": {mode: active}}


def decode_portable(data: object) -> dict | None:
    if (not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or data.get("kind") != "portable"):
        return None
    return validate_view(data.get("view"))


def decode_instance(
    data: object,
    identity: OuterTmuxIdentity,
) -> dict | None:
    """Decode one exact owner's local state into App's legacy flat shape."""
    if (not isinstance(data, dict)
            or data.get("schema_version") != SCHEMA_VERSION
            or data.get("kind") != "instance"
            or not owner_matches(data.get("owner"), identity)):
        return None
    view = validate_view(data.get("view"))
    recovery = data.get("recovery")
    if view is None or not isinstance(recovery, dict):
        return None
    kind = recovery.get("right_kind")
    if kind not in {"empty", "agent", "preview", "claude"}:
        return None
    decoded = dict(view)
    decoded["right_kind"] = kind
    string_fields = {
        "right_tmux": 256,
        "right_session": 256,
        "right_mode": 64,
        "right_project": 512,
    }
    for key, limit in string_fields.items():
        value = _bounded_string(recovery.get(key), limit)
        if value is not None:
            decoded[key] = value
    raw_workspace = recovery.get("workspace")
    if raw_workspace is not None:
        workspace = _validate_workspace(raw_workspace)
        if workspace is None:
            return None
        decoded["workspace"] = workspace
    bindings = recovery.get("running_bindings")
    version = recovery.get("running_bindings_version")
    if version == 1 and isinstance(bindings, list) and len(bindings) <= 10000:
        decoded["running_bindings_version"] = 1
        decoded["running_bindings"] = bindings
    return decoded


def legacy_portable_view(data: object) -> dict | None:
    """Extract only non-authoritative view fields from the ownerless schema."""
    if not isinstance(data, dict) or "schema_version" in data:
        return None
    raw: dict = {}
    mode = data.get("mode")
    if isinstance(mode, str):
        raw["mode"] = mode
    elif data.get("codex_mode") is True:
        raw["mode"] = "codex"
    else:
        raw["mode"] = "claude"
    for key in ("project", "session"):
        if key in data:
            raw[key] = data[key]
    # Legacy is flat; validate through the same new schema so migration cannot
    # accidentally acquire extra authority.
    return validate_view(build_view(raw))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def cleanup_stale_instances(
    current: OuterTmuxIdentity,
    *,
    now: float | None = None,
) -> int:
    """Remove only recognized state whose exact outer owner is proven dead.

    Work per invocation is bounded. Unknown/newer schemas and any owner whose
    liveness cannot be disproved are retained, including old but live servers.
    """
    root = instances_dir()
    try:
        candidates = sorted(
            root.glob("instance-*.json"),
            key=lambda item: item.stat().st_mtime,
        )[:_MAX_CLEANUP_FILES]
    except OSError:
        return 0
    current_path = instance_state_path(current)
    cutoff = (time.time() if now is None else now) - _DEAD_STATE_GRACE_S
    removed = 0
    for path in candidates:
        if path == current_path:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        data = read_json_object(path)
        if (not data
                or data.get("schema_version") != SCHEMA_VERSION
                or data.get("kind") != "instance"):
            continue
        owner = data.get("owner")
        if not isinstance(owner, dict):
            continue
        server_digest = owner.get("server_digest")
        server_pid = owner.get("server_pid")
        pane_id = owner.get("pane_id")
        if (not isinstance(server_digest, str)
                or not isinstance(server_pid, int)
                or isinstance(server_pid, bool)
                or not isinstance(pane_id, str)):
            continue
        if server_digest == current.server_digest:
            # This query is scoped to the current exact server through TMUX.
            dead = tmux_ctl.pane_identity(pane_id) is None
        else:
            # For a different private server, PID death is the only fact the
            # current socket can prove. PID reuse keeps the file conservatively.
            dead = not _pid_alive(server_pid)
        if not dead:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
