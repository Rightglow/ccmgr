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


# Codex flag that skips all approval prompts AND disables the sandbox.
# `--yolo` is its (hidden) alias; the explicit name is clearer and stable.
_CODEX_YOLO_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def build_codex_resume_command(codex_binary: str, session_id: str,
                               cwd: Path, *, yolo: bool = False) -> list[str]:
    """Build the argv to resume an existing Codex session.

    With *yolo*, prepend the bypass flag before the subcommand (it's a
    top-level option, so it must precede ``resume``).
    """
    prefix = [codex_binary] + ([_CODEX_YOLO_FLAG] if yolo else [])
    return prefix + ["resume", session_id, "-C", str(cwd)]


def build_codex_new_command(codex_binary: str, cwd: Path,
                            *, yolo: bool = False) -> list[str]:
    """Build the argv to start a fresh Codex session in `cwd`."""
    cmd = [codex_binary, "-C", str(cwd)]
    if yolo:
        cmd.append(_CODEX_YOLO_FLAG)
    return cmd


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
