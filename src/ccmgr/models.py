"""Plain dataclasses passed between modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Project:
    real_path: Path
    encoded_name: str
    claude_dir: Path  # ~/.claude/projects/<encoded_name>
    session_count: int
    last_activity_ts: float  # epoch seconds; 0.0 if no sessions

    @property
    def display_name(self) -> str:
        return self.real_path.name or str(self.real_path)


@dataclass(frozen=True)
class SessionMeta:
    project: Project
    session_id: str  # UUID from filename
    jsonl_path: Path
    title: str | None  # from ai-title; None if no title record yet
    message_count: int
    token_total: int
    last_mtime: float
    size_bytes: int = 0  # JSONL file size, captured at scan time (see session_index)
    # Git branch name recorded in the JSONL (claude writes `gitBranch` on each
    # record). None if the session has no git context. Mirrors the branch
    # column in `claude --resume`.
    git_branch: str | None = None
    # Last user message content, truncated.  None if the session has no user
    # messages yet (e.g. a freshly created session).
    last_user_message: str | None = None
    # Current state derived from the last JSONL record:
    #   "idle"    — last assistant turn ended normally (end_turn / stop_sequence)
    #   "busy"    — last record is a user message (assistant is processing)
    #   "blocked" — assistant is waiting for tool approval (stop_reason=tool_use)
    status: str = "idle"

    @property
    def display_title(self) -> str:
        return self.title or f"session {self.session_id[:8]}"
