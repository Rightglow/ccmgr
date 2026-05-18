"""Load ccmgr configuration from TOML with sensible defaults."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    leave_focus_key: str = "ctrl a"
    quit_key: str = "q"
    filter_key: str = "/"
    max_concurrent_sessions: int = 10
    scrollback_lines: int = 5000
    claude_binary: str = "claude"
    poll_interval_ms: int = 1000
    live_badge_seconds: int = 3


def default_config_path() -> Path:
    return Path.home() / ".config" / "ccmgr" / "config.toml"


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        config_path = default_config_path()
    if not config_path.is_file():
        return Config()

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    keys = data.get("keys", {})
    limits = data.get("limits", {})
    claude = data.get("claude", {})
    live = data.get("live", {})

    return Config(
        leave_focus_key=keys.get("leave_focus", "ctrl a"),
        quit_key=keys.get("quit", "q"),
        filter_key=keys.get("filter", "/"),
        max_concurrent_sessions=int(limits.get("max_concurrent_sessions", 10)),
        scrollback_lines=int(limits.get("scrollback_lines", 5000)),
        claude_binary=claude.get("binary", "claude"),
        poll_interval_ms=int(live.get("poll_interval_ms", 1000)),
        live_badge_seconds=int(live.get("live_badge_seconds", 3)),
    )
