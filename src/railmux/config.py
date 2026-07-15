"""Load railmux configuration from TOML with sensible defaults."""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9-3.10
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    claude_binary: str = "claude"
    codex_binary: str = "codex"
    codex_home: str = "~/.codex"
    poll_interval_ms: int = 1000
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


def load_config(config_path: Path | None = None) -> Config:
    if config_path is None:
        config_path = default_config_path()
    if not config_path.is_file():
        return Config()

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    claude = data.get("claude", {})
    codex = data.get("codex", {})
    live = data.get("live", {})
    projects = data.get("projects", {})

    return Config(
        claude_binary=claude.get("binary", "claude"),
        codex_binary=codex.get("binary", "codex"),
        codex_home=codex.get("home", "~/.codex"),
        poll_interval_ms=int(live.get("poll_interval_ms", 1000)),
        show_empty_projects=projects.get("show_empty_projects") is True,
    )
