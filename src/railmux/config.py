"""Load railmux configuration from TOML with sensible defaults."""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9-3.10
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A safe, user-facing configuration error without file contents."""


@dataclass(frozen=True)
class Config:
    claude_binary: str = "claude"
    codex_binary: str = "codex"
    codex_home: str = "~/.codex"
    poll_interval_ms: int = 1000
    agent_transport: str = "swap"
    show_empty_projects: bool = False

    def resolved_codex_home(self) -> Path:
        """The one resolved ``CODEX_HOME`` directory.

        Single source of truth so listing (CodexIndex), launching (new/resume),
        deleting (``codex delete``) and config/env-key reading all hit the same
        directory even when ``[codex] home`` is non-default. ``~`` is expanded
        and relative paths are made absolute before a launched Codex changes to
        its project cwd. The directory is not required to exist.
        """
        path = Path(self.codex_home).expanduser()
        try:
            return path.resolve()
        except OSError:
            return path.absolute()


def default_config_path() -> Path:
    return Path.home() / ".config" / "railmux" / "config.toml"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _string(table: dict[str, Any], key: str, default: str, label: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        config_path = default_config_path()
    if not config_path.is_file():
        return Config()

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("invalid TOML") from exc
    except OSError as exc:
        raise ConfigError("configuration file could not be read") from exc

    claude = _table(data, "claude")
    codex = _table(data, "codex")
    live = _table(data, "live")
    projects = _table(data, "projects")

    poll_value = live.get("poll_interval_ms", 1000)
    if isinstance(poll_value, bool):
        raise ConfigError("live.poll_interval_ms must be a positive integer")
    try:
        poll_interval_ms = int(poll_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "live.poll_interval_ms must be a positive integer") from exc
    if poll_interval_ms <= 0:
        raise ConfigError("live.poll_interval_ms must be a positive integer")

    agent_transport = live.get("agent_transport", "swap")
    if agent_transport not in ("nested", "swap"):
        raise ConfigError(
            'live.agent_transport must be either "nested" or "swap"')

    return Config(
        claude_binary=_string(
            claude, "binary", "claude", "claude.binary"),
        codex_binary=_string(codex, "binary", "codex", "codex.binary"),
        codex_home=_string(codex, "home", "~/.codex", "codex.home"),
        poll_interval_ms=poll_interval_ms,
        agent_transport=agent_transport,
        show_empty_projects=projects.get("show_empty_projects") is True,
    )
