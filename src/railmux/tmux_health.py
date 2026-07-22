"""Low-volume tmux health tracking shared by launchers and diagnostics."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from railmux import restart_state, tmux_server
from railmux.atomic_file import atomic_write_text


INCIDENT_SCHEMA_VERSION = 1
_CLEAN_EXIT_SCHEMA_VERSION = 1
_CLEAN_EXIT_MAX_AGE = 30
_SOFT_EXIT_SCHEMA_VERSION = 1
_SOFT_EXIT_MAX_AGE = 30
_INCIDENT_REASONS = {
    "launcher-watchdog-timeout",
    "launcher-server-exit",
    "remote-display-watchdog-timeout",
    "remote-display-server-exit",
    "startup-probe-timeout",
}
_INCIDENT_COMPONENTS = {"launcher", "remote-display"}


@dataclass(frozen=True)
class TmuxIncident:
    recorded_at: int
    component: str
    reason: str
    consecutive_failures: int


@dataclass
class FailureWatchdog:
    """Trigger only after bounded consecutive failed health observations."""

    interval: float
    failure_limit: int
    next_probe: float
    consecutive_failures: int = 0

    @classmethod
    def starting(
        cls, now: float, *, interval: float, failure_limit: int,
    ) -> FailureWatchdog:
        if interval <= 0 or failure_limit <= 0:
            raise ValueError("watchdog bounds must be positive")
        return cls(interval, failure_limit, now + interval)

    def due(self, now: float) -> bool:
        return now >= self.next_probe

    def observe(self, healthy: bool, now: float) -> bool:
        """Record one due observation and return whether the limit was hit."""
        self.next_probe = now + self.interval
        if healthy:
            self.consecutive_failures = 0
            return False
        self.consecutive_failures += 1
        return self.consecutive_failures >= self.failure_limit


def _incident_filename(label: str) -> str:
    return f"last-tmux-incident-{label}.json"


def _read_path(label: str) -> Path:
    return restart_state.runtime_base() / "railmux" / _incident_filename(label)


def _clean_exit_filename(label: str) -> str:
    return f"clean-tmux-exit-{label}.json"


def _clean_exit_path(label: str) -> Path:
    return restart_state.runtime_base() / "railmux" / _clean_exit_filename(label)


def _soft_exit_filename(label: str) -> str:
    return f"soft-tmux-exit-{label}.json"


def _soft_exit_path(label: str) -> Path:
    return restart_state.runtime_base() / "railmux" / _soft_exit_filename(label)


def record_clean_exit(*, server_pid: int, session_id: str) -> bool:
    """Publish one short-lived exact hard-quit intent for an SSH observer."""
    if (server_pid <= 0 or not session_id.startswith("$")
            or not session_id[1:].isdigit()):
        return False
    try:
        label = tmux_server.socket_label()
        atomic_write_text(
            restart_state.runtime_state_dir() / _clean_exit_filename(label),
            json.dumps(
                {
                    "schema_version": _CLEAN_EXIT_SCHEMA_VERSION,
                    "recorded_at": int(time.time()),
                    "server_pid": server_pid,
                    "session_id": session_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return True
    except (OSError, tmux_server.TmuxServerError):
        return False


def clear_clean_exit() -> None:
    """Remove a hard-quit intent when the corresponding kill did not commit."""
    try:
        _clean_exit_path(tmux_server.socket_label()).unlink(missing_ok=True)
    except (OSError, tmux_server.TmuxServerError):
        pass


def consume_clean_exit(*, server_pid: int, session_id: str) -> bool:
    """Consume and validate the one-shot intentional-exit sentinel."""
    try:
        path = _clean_exit_path(tmux_server.socket_label())
    except tmux_server.TmuxServerError:
        return False
    payload = restart_state.read_json_object(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if payload is None:
        return False
    recorded_at = payload.get("recorded_at")
    now = int(time.time())
    return bool(
        payload.get("schema_version") == _CLEAN_EXIT_SCHEMA_VERSION
        and isinstance(recorded_at, int)
        and now - _CLEAN_EXIT_MAX_AGE <= recorded_at <= now + 5
        and payload.get("server_pid") == server_pid
        and payload.get("session_id") == session_id
    )


def record_soft_exit(*, server_pid: int, session_id: str) -> bool:
    """Publish one exact soft-quit intent for every attached SSH observer."""
    if (server_pid <= 0 or not session_id.startswith("$")
            or not session_id[1:].isdigit()):
        return False
    try:
        label = tmux_server.socket_label()
        atomic_write_text(
            restart_state.runtime_state_dir() / _soft_exit_filename(label),
            json.dumps(
                {
                    "schema_version": _SOFT_EXIT_SCHEMA_VERSION,
                    "recorded_at": int(time.time()),
                    "server_pid": server_pid,
                    "session_id": session_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return True
    except (OSError, tmux_server.TmuxServerError):
        return False


def soft_exit_intended(*, server_pid: int, session_id: str) -> bool:
    """Validate a short-lived soft quit without consuming it.

    Soft quit closes every view attached to one managed session.  Each helper
    therefore needs to observe the same exact intent independently.
    """
    try:
        path = _soft_exit_path(tmux_server.socket_label())
    except tmux_server.TmuxServerError:
        return False
    payload = restart_state.read_json_object(path)
    if payload is None:
        return False
    recorded_at = payload.get("recorded_at")
    now = int(time.time())
    return bool(
        payload.get("schema_version") == _SOFT_EXIT_SCHEMA_VERSION
        and isinstance(recorded_at, int)
        and now - _SOFT_EXIT_MAX_AGE <= recorded_at <= now + 5
        and payload.get("server_pid") == server_pid
        and payload.get("session_id") == session_id
    )


def record_incident(
    *, component: str, reason: str, consecutive_failures: int,
) -> bool:
    """Persist a privacy-safe last failure without touching provider state."""
    if (
        component not in _INCIDENT_COMPONENTS
        or reason not in _INCIDENT_REASONS
        or not 1 <= consecutive_failures <= 100
    ):
        return False
    try:
        label = tmux_server.socket_label()
        target = restart_state.runtime_state_dir() / _incident_filename(label)
        payload = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "recorded_at": int(time.time()),
            "component": component,
            "reason": reason,
            "consecutive_failures": consecutive_failures,
        }
        atomic_write_text(
            target,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        return True
    except (OSError, tmux_server.TmuxServerError):
        return False


def read_last_incident() -> TmuxIncident | None:
    """Return one bounded, validated incident for the active socket label."""
    try:
        label = tmux_server.socket_label()
    except tmux_server.TmuxServerError:
        return None
    payload = restart_state.read_json_object(_read_path(label))
    if payload is None or payload.get("schema_version") != INCIDENT_SCHEMA_VERSION:
        return None
    recorded_at = payload.get("recorded_at")
    component = payload.get("component")
    reason = payload.get("reason")
    failures = payload.get("consecutive_failures")
    now = int(time.time())
    if (
        not isinstance(recorded_at, int)
        or not 0 < recorded_at <= now + 300
        or component not in _INCIDENT_COMPONENTS
        or reason not in _INCIDENT_REASONS
        or not isinstance(failures, int)
        or not 1 <= failures <= 100
    ):
        return None
    return TmuxIncident(recorded_at, component, reason, failures)


def incident_age(recorded_at: int, *, now: int | None = None) -> str:
    """Format a coarse shareable age without exposing machine details."""
    elapsed = max(0, (int(time.time()) if now is None else now) - recorded_at)
    if elapsed < 60:
        return "less than a minute ago"
    if elapsed < 3600:
        return f"{elapsed // 60} minutes ago"
    if elapsed < 86400:
        return f"{elapsed // 3600} hours ago"
    return f"{elapsed // 86400} days ago"
