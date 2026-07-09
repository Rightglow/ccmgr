"""Launch claude or codex via subprocess, inheriting the parent's terminal."""
from __future__ import annotations

import subprocess
from pathlib import Path


def build_resume_command(claude_binary: str, session_id: str, cwd: Path) -> list[str]:
    """Build the argv to resume an existing Claude session."""
    return [claude_binary, "--resume", session_id]


def build_new_session_command(claude_binary: str, cwd: Path) -> list[str]:
    """Build the argv to start a fresh Claude session in `cwd`."""
    return [claude_binary]


def build_codex_resume_command(codex_binary: str, session_id: str,
                               cwd: Path) -> list[str]:
    """Build the argv to resume an existing Codex session."""
    return [codex_binary, "resume", session_id, "-C", str(cwd)]


def build_codex_new_command(codex_binary: str, cwd: Path) -> list[str]:
    """Build the argv to start a fresh Codex session in `cwd`."""
    return [codex_binary, "-C", str(cwd)]


def launch(cmd: list[str], cwd: Path, create_cwd: bool = False) -> int:
    """Run `cmd` with `cwd`, inheriting our stdin/stdout/stderr. Block until exit.

    Returns the child's exit code. Caller is responsible for suspending any
    TUI screen before calling and restoring it afterwards.
    """
    if create_cwd:
        cwd.mkdir(parents=True, exist_ok=True)
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")

    result = subprocess.run(cmd, cwd=cwd)
    return result.returncode
