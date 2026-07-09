"""Load ccmgr configuration from TOML with sensible defaults."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    claude_binary: str = "claude"
    poll_interval_ms: int = 1000


def default_config_path() -> Path:
    return Path.home() / ".config" / "ccmgr" / "config.toml"


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        config_path = default_config_path()
    if not config_path.is_file():
        return Config()

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    claude = data.get("claude", {})
    live = data.get("live", {})

    return Config(
        claude_binary=claude.get("binary", "claude"),
        poll_interval_ms=int(live.get("poll_interval_ms", 1000)),
    )
