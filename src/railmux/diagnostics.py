"""Privacy-safe, non-interactive environment diagnostics."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from railmux import __version__
from railmux.config import Config, ConfigError, default_config_path, load_config


_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){1,3}(?:[A-Za-z]|[-+][0-9A-Za-z.-]+)?)"
)


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


def _version(binary: str, *version_args: str) -> str:
    """Return only a numeric version token from a configured executable."""
    try:
        found = shutil.which(binary)
    except (OSError, TypeError):
        found = None
    if found is None:
        return "not found"
    try:
        result = subprocess.run(
            [binary, *(version_args or ("--version",))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "available (version timed out)"
    except OSError:
        return "available (version unavailable)"
    text = f"{result.stdout}\n{result.stderr}"
    match = _VERSION_RE.search(text)
    return match.group(1) if match else "available (version unavailable)"


def _directory_status(path: Path) -> str:
    try:
        exists = path.is_dir()
        readable = exists and os.access(path, os.R_OK)
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = readable = writable = False
    return (
        f"{_display_path(path)}; exists={_yes_no(exists)}, "
        f"readable={_yes_no(readable)}, writable={_yes_no(writable)}"
    )


def _terminal_capabilities(environ: dict[str, str]) -> str:
    term = environ.get("TERM", "").lower()
    colorterm = environ.get("COLORTERM", "").lower()
    colours_256 = "256color" in term
    truecolour = colorterm in {"truecolor", "24bit"}
    return f"256-colour={_yes_no(colours_256)}, true-colour={_yes_no(truecolour)}"


def run_doctor(
    *,
    claude_home: Path,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Print a shareable diagnostic report without exposing user data."""
    stdout = sys.stdout if stdout is None else stdout
    env = dict(os.environ if environ is None else environ)
    config_path = default_config_path()
    if config_path.is_file():
        try:
            config = load_config(config_path)
            config_status = f"{_display_path(config_path)}; valid=yes"
        except ConfigError as exc:
            config = Config()
            config_status = (
                f"{_display_path(config_path)}; valid=no ({exc})")
    else:
        config = Config()
        config_status = f"{_display_path(config_path)}; file=absent (defaults active)"

    system = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    python_version = platform.python_version()

    lines = (
        "Railmux diagnostics",
        f"Railmux: {__version__}",
        f"Python: {python_version}",
        f"Platform: {system} ({machine})",
        f"tmux: {_version('tmux', '-V')}",
        f"Claude Code: {_version(config.claude_binary)}",
        f"Codex: {_version(config.codex_binary)}",
        f"Inside tmux: {_yes_no(bool(env.get('TMUX')))}",
        f"SSH transport: {_yes_no(is_ssh_session(env))}",
        f"Terminal capabilities: {_terminal_capabilities(env)}",
        f"Config: {config_status}",
        f"Preferred agent display: {config.agent_transport}",
        f"Claude data: {_directory_status(claude_home)}",
        f"Codex data: {_directory_status(Path(config.codex_home).expanduser())}",
        (
            "Privacy: session IDs, transcript content, credentials, hostnames, "
            "and raw custom paths are omitted; review before sharing."
        ),
    )
    print("\n".join(lines), file=stdout)
    return 0
