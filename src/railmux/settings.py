"""App-mutable settings, persisted as a JSON sidecar.

Unlike :mod:`railmux.config` (a read-only ``config.toml`` the user owns),
these are flags railmux itself flips at runtime — currently the Codex
auto-run ("yolo") toggle and whether we've prompted for it. Stored at
``~/.config/railmux/settings.json``. Same atomic-write pattern as
:mod:`railmux.favorites` / :mod:`railmux.renames`.
"""
from __future__ import annotations

import json
from pathlib import Path

from railmux.atomic_file import atomic_write_text


def _settings_path() -> Path:
    return Path.home() / ".config" / "railmux" / "settings.json"


class Settings:
    """In-memory settings dict, backed by a JSON file."""

    def __init__(self) -> None:
        self._path = _settings_path()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            self._data = data

    def _save(self) -> bool:
        try:
            atomic_write_text(
                self._path, json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            return False
        return True

    def _update(self, **values: bool) -> bool:
        """Persist *values* as one transaction, rolling memory back on failure."""
        previous = self._data.copy()
        self._data.update(values)
        if self._save():
            return True
        self._data = previous
        return False

    # -- Codex auto-run (yolo) -------------------------------------------
    @property
    def codex_yolo(self) -> bool:
        """Whether Codex bypasses approvals+sandbox; unknown types fail closed."""
        return self._data.get("codex_yolo") is True

    def set_codex_yolo(self, value: bool) -> bool:
        return self._update(codex_yolo=value is True)

    @property
    def codex_yolo_prompted(self) -> bool:
        """True once the user has been asked whether to enable Codex auto-run."""
        return self._data.get("codex_yolo_prompted") is True

    def mark_codex_yolo_prompted(self) -> bool:
        return self._update(codex_yolo_prompted=True)

    def record_codex_yolo_choice(self, enabled: bool) -> bool:
        """Atomically persist both the YOLO choice and the prompted marker.

        A single write prevents the unsafe split state where YOLO was enabled
        but the prompted marker failed to persist. Non-bool inputs fail closed.
        """
        return self._update(
            codex_yolo=enabled is True,
            codex_yolo_prompted=True,
        )
