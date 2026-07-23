"""Privacy-safe, non-interactive environment diagnostics."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from railmux import __version__
from railmux import legacy_sessions, tmux_health, tmux_server
from railmux.config import Config, ConfigError, default_config_path, load_config


_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,3}(?:[A-Za-z]|[-+][0-9A-Za-z.-]+)?)"
)
DOCTOR_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ToolDiagnostic:
    status: str
    version: str | None = None


@dataclass(frozen=True)
class TmuxServerDiagnostic:
    status: str
    context: str | None = None
    candidate_count: int | None = None
    restart_recommended: bool = False


@dataclass(frozen=True)
class IncidentDiagnostic:
    status: str
    category: str | None = None
    consecutive_failures: int | None = None
    age: str | None = None


@dataclass(frozen=True)
class ConfigDiagnostic:
    path: str
    status: str
    error_category: str | None = None


@dataclass(frozen=True)
class DirectoryDiagnostic:
    path: str
    exists: bool
    readable: bool
    writable: bool


@dataclass(frozen=True)
class DoctorSnapshot:
    """Versioned, privacy-safe authority shared by text and JSON output."""

    schema_version: int
    railmux_version: str
    python_version: str
    platform_system: str
    platform_machine: str
    tools: dict[str, ToolDiagnostic]
    dedicated_tmux: TmuxServerDiagnostic
    legacy_tmux: TmuxServerDiagnostic
    watchdog_enabled: bool
    last_tmux_incident: IncidentDiagnostic
    inside_tmux: bool
    ssh_transport: bool
    terminal_256_colour: bool
    terminal_true_colour: bool
    config: ConfigDiagnostic
    preferred_agent_display: str
    data_directories: dict[str, DirectoryDiagnostic]


def is_ssh_session(environ: dict[str, str] | None = None) -> bool:
    """Return whether common OpenSSH transport markers are present."""
    env = os.environ if environ is None else environ
    return any(env.get(name) for name in (
        "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _display_path(path: Path) -> str:
    """Show home-relative paths, but never reveal an unrelated custom path."""
    try:
        path = path.expanduser().absolute()
        home = Path.home().absolute()
        relative = path.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return "<custom>"
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _tool_diagnostic(binary: str, *version_args: str) -> ToolDiagnostic:
    """Return a bounded tool status without retaining configured commands."""
    try:
        found = shutil.which(binary)
    except (OSError, TypeError):
        found = None
    if found is None:
        return ToolDiagnostic("missing")
    try:
        result = subprocess.run(
            [binary, *(version_args or ("--version",))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolDiagnostic("timeout")
    except OSError:
        return ToolDiagnostic("unavailable")
    text = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_RE.search(text)
    return ToolDiagnostic(
        "available" if match else "unavailable",
        match.group(1) if match else None,
    )


def _directory_diagnostic(path: Path) -> DirectoryDiagnostic:
    try:
        exists = path.is_dir()
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = readable = writable = False
    return DirectoryDiagnostic(
        path=_display_path(path),
        exists=exists,
        readable=readable,
        writable=writable,
    )


def _terminal_diagnostic(environ: dict[str, str]) -> tuple[bool, bool]:
    term = environ.get("TERM", "").lower()
    colorterm = environ.get("COLORTERM", "").lower()
    colours_256 = "256color" in term
    truecolour = colorterm in {"truecolor", "24bit"}
    return colours_256, truecolour


def _dedicated_tmux_diagnostic() -> TmuxServerDiagnostic:
    """Return a bounded health result without exposing the socket pathname."""
    if shutil.which("tmux") is None:
        return TmuxServerDiagnostic("unavailable")
    try:
        target = tmux_server.discover_target(timeout=1.0)
    except tmux_server.TmuxServerUnresponsive:
        return TmuxServerDiagnostic("unresponsive")
    except tmux_server.TmuxServerError:
        return TmuxServerDiagnostic("configuration_error")
    if target is None:
        return TmuxServerDiagnostic("not_running")
    context = (
        "inside"
        if tmux_server.is_current_server(target)
        else "outside"
    )
    return TmuxServerDiagnostic("healthy", context=context)


def _legacy_tmux_diagnostic() -> TmuxServerDiagnostic:
    """Report only a bounded count; never expose session names or paths."""
    target, sessions, complete = legacy_sessions.discover(timeout=1.0)
    if not complete:
        return TmuxServerDiagnostic("unavailable")
    if target is None:
        return TmuxServerDiagnostic("not_running")
    count = sum(
        session.name.startswith(("cc-", "cx-")) for session in sessions
    )
    return TmuxServerDiagnostic(
        "healthy",
        candidate_count=count,
        restart_recommended=bool(count),
    )


def _last_tmux_incident_diagnostic() -> IncidentDiagnostic:
    incident = tmux_health.read_last_incident()
    if incident is None:
        return IncidentDiagnostic("none")
    return IncidentDiagnostic(
        status="recorded",
        category=incident.reason,
        consecutive_failures=(
            None
            if incident.reason.endswith("-server-exit")
            else incident.consecutive_failures
        ),
        age=tmux_health.incident_age(incident.recorded_at),
    )


def collect_doctor_snapshot(
    *,
    claude_home: Path,
    environ: dict[str, str] | None = None,
) -> DoctorSnapshot:
    """Collect one bounded diagnostic snapshot for every output renderer."""
    env = dict(os.environ if environ is None else environ)
    config_path = default_config_path()
    if config_path.is_file():
        try:
            config = load_config(config_path)
            config_diagnostic = ConfigDiagnostic(
                path=_display_path(config_path),
                status="valid",
            )
        except ConfigError as exc:
            config = Config()
            config_diagnostic = ConfigDiagnostic(
                path=_display_path(config_path),
                status="invalid",
                error_category=(
                    "invalid_toml"
                    if str(exc) == "invalid TOML"
                    else "invalid_config"
                ),
            )
    else:
        config = Config()
        config_diagnostic = ConfigDiagnostic(
            path=_display_path(config_path),
            status="absent",
        )

    colours_256, truecolour = _terminal_diagnostic(env)
    return DoctorSnapshot(
        schema_version=DOCTOR_SCHEMA_VERSION,
        railmux_version=__version__,
        python_version=platform.python_version(),
        platform_system=platform.system() or "unknown",
        platform_machine=platform.machine() or "unknown",
        tools={
            "tmux": _tool_diagnostic("tmux", "-V"),
            "claude_code": _tool_diagnostic(config.claude_binary),
            "codex": _tool_diagnostic(config.codex_binary),
        },
        dedicated_tmux=_dedicated_tmux_diagnostic(),
        legacy_tmux=_legacy_tmux_diagnostic(),
        watchdog_enabled=True,
        last_tmux_incident=_last_tmux_incident_diagnostic(),
        inside_tmux=bool(env.get("TMUX")),
        ssh_transport=is_ssh_session(env),
        terminal_256_colour=colours_256,
        terminal_true_colour=truecolour,
        config=config_diagnostic,
        preferred_agent_display=config.agent_transport,
        data_directories={
            "claude": _directory_diagnostic(claude_home),
            "codex": _directory_diagnostic(
                Path(config.codex_home).expanduser()
            ),
        },
    )


def _tool_text(diagnostic: ToolDiagnostic) -> str:
    if diagnostic.status == "missing":
        return "not found"
    if diagnostic.version is not None:
        return diagnostic.version
    if diagnostic.status == "timeout":
        return "available (version timed out)"
    return "available (version unavailable)"


def _dedicated_tmux_text(diagnostic: TmuxServerDiagnostic) -> str:
    if diagnostic.status == "unavailable":
        return "unavailable (tmux not found)"
    if diagnostic.status == "unresponsive":
        return "unresponsive (watchdog will not kill or restart it)"
    if diagnostic.status == "configuration_error":
        return "configuration error"
    if diagnostic.status == "not_running":
        return "not running"
    context = (
        "current process is inside it"
        if diagnostic.context == "inside"
        else "current process is outside it"
    )
    return f"healthy ({context})"


def _legacy_tmux_text(diagnostic: TmuxServerDiagnostic) -> str:
    if diagnostic.status == "unavailable":
        return "unavailable (inventory timed out or changed)"
    if diagnostic.status == "not_running":
        return "not running"
    if diagnostic.candidate_count:
        return (
            f"healthy ({diagnostic.candidate_count} Railmux candidate(s); "
            "restart recommended)"
        )
    return "healthy (no Railmux candidates)"


def _incident_text(diagnostic: IncidentDiagnostic) -> str:
    if diagnostic.status == "none":
        return "none recorded"
    descriptions = {
        "launcher-watchdog-timeout": "local client watchdog timeout",
        "launcher-server-exit": "dedicated tmux server exited",
        "remote-display-watchdog-timeout": "SSH display watchdog timeout",
        "remote-display-server-exit": "SSH tmux server exited",
        "startup-probe-timeout": "startup health probe timeout",
    }
    description = descriptions.get(
        diagnostic.category or "", "tmux health failure"
    )
    if diagnostic.consecutive_failures is None:
        return f"{description}; {diagnostic.age}"
    return (
        f"{description}; {diagnostic.consecutive_failures} consecutive failures; "
        f"{diagnostic.age}"
    )


def _config_text(diagnostic: ConfigDiagnostic) -> str:
    if diagnostic.status == "valid":
        return f"{diagnostic.path}; valid=yes"
    if diagnostic.status == "invalid":
        detail = (
            "invalid TOML"
            if diagnostic.error_category == "invalid_toml"
            else "invalid configuration"
        )
        return f"{diagnostic.path}; valid=no ({detail})"
    return f"{diagnostic.path}; file=absent (defaults active)"


def _directory_text(diagnostic: DirectoryDiagnostic) -> str:
    return (
        f"{diagnostic.path}; exists={_yes_no(diagnostic.exists)}, "
        f"readable={_yes_no(diagnostic.readable)}, "
        f"writable={_yes_no(diagnostic.writable)}"
    )


def render_doctor_text(snapshot: DoctorSnapshot) -> str:
    """Render the stable human report from the structured authority."""
    lines = (
        "Railmux diagnostics",
        f"Railmux: {snapshot.railmux_version}",
        f"Python: {snapshot.python_version}",
        f"Platform: {snapshot.platform_system} ({snapshot.platform_machine})",
        f"tmux: {_tool_text(snapshot.tools['tmux'])}",
        (
            "Dedicated Railmux tmux: "
            f"{_dedicated_tmux_text(snapshot.dedicated_tmux)}"
        ),
        f"Legacy default tmux: {_legacy_tmux_text(snapshot.legacy_tmux)}",
        "Tmux watchdog: enabled; reports and exits, never auto-kills or restarts",
        f"Last tmux incident: {_incident_text(snapshot.last_tmux_incident)}",
        f"Claude Code: {_tool_text(snapshot.tools['claude_code'])}",
        f"Codex: {_tool_text(snapshot.tools['codex'])}",
        f"Inside tmux: {_yes_no(snapshot.inside_tmux)}",
        f"SSH transport: {_yes_no(snapshot.ssh_transport)}",
        (
            "Terminal capabilities: "
            f"256-colour={_yes_no(snapshot.terminal_256_colour)}, "
            f"true-colour={_yes_no(snapshot.terminal_true_colour)}"
        ),
        f"Config: {_config_text(snapshot.config)}",
        f"Preferred agent display: {snapshot.preferred_agent_display}",
        f"Claude data: {_directory_text(snapshot.data_directories['claude'])}",
        f"Codex data: {_directory_text(snapshot.data_directories['codex'])}",
        (
            "Privacy: session IDs, transcript content, credentials, hostnames, "
            "and raw custom paths are omitted; review before sharing."
        ),
    )
    return "\n".join(lines)


def run_doctor(
    *,
    claude_home: Path,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
    json_output: bool = False,
) -> int:
    """Print a shareable diagnostic report without exposing user data."""
    stdout = sys.stdout if stdout is None else stdout
    snapshot = collect_doctor_snapshot(
        claude_home=claude_home,
        environ=environ,
    )
    if json_output:
        json.dump(asdict(snapshot), stdout, indent=2, sort_keys=True)
        print(file=stdout)
    else:
        print(render_doctor_text(snapshot), file=stdout)
    return 0
