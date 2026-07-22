"""App-mutable settings, persisted as a JSON sidecar.

Unlike :mod:`railmux.config` (a read-only ``config.toml`` the user owns),
these are values railmux itself changes at runtime — currently the Codex
auto-run ("yolo") choice and a versioned layout preference. Stored at
``~/.config/railmux/settings.json``. Same atomic-write pattern as
:mod:`railmux.favorites` / :mod:`railmux.renames`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from railmux.atomic_file import atomic_write_text


def _settings_path() -> Path:
    return Path.home() / ".config" / "railmux" / "settings.json"


@dataclass(frozen=True)
class LayoutProfile:
    """Validated, size-independent outer-workspace geometry preference."""

    scope: str
    layout: str
    sidebar_permille: int
    primary_permille: int | None = None

    def to_json(self) -> dict:
        data = {
            "version": 1,
            "scope": self.scope,
            "layout": self.layout,
            "sidebar_permille": self.sidebar_permille,
        }
        if self.primary_permille is not None:
            data["primary_permille"] = self.primary_permille
        return data


def _decode_layout_profile(raw: object) -> LayoutProfile | None:
    if not isinstance(raw, dict) or len(raw) > 5 or raw.get("version") != 1:
        return None
    if not set(raw).issubset({
        "version", "scope", "layout", "sidebar_permille", "primary_permille",
    }):
        return None
    scope = raw.get("scope")
    layout = raw.get("layout")
    sidebar = raw.get("sidebar_permille")
    primary = raw.get("primary_permille")
    if scope not in {"always", "once"}:
        return None
    if layout not in {"single", "side-by-side", "stacked"}:
        return None
    if (not isinstance(sidebar, int) or isinstance(sidebar, bool)
            or not 50 <= sidebar <= 800):
        return None
    if primary is not None and (
        not isinstance(primary, int)
        or isinstance(primary, bool)
        or not 100 <= primary <= 900
    ):
        return None
    if layout == "single" and primary is not None:
        return None
    return LayoutProfile(scope, layout, sidebar, primary)


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

    def _replace(self, data: dict) -> bool:
        """Persist a complete replacement, rolling memory back on failure."""
        previous = self._data
        self._data = data
        if self._save():
            return True
        self._data = previous
        return False

    def _update(self, **values: object) -> bool:
        """Persist *values* as one transaction, rolling memory back on failure."""
        updated = self._data.copy()
        updated.update(values)
        return self._replace(updated)

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

    # -- Saved outer-workspace geometry ---------------------------------
    @property
    def layout_profile(self) -> LayoutProfile | None:
        return _decode_layout_profile(self._data.get("layout_profile"))

    def save_layout_profile(self, profile: LayoutProfile) -> bool:
        """Atomically store one already-validated geometry profile."""
        decoded = _decode_layout_profile(profile.to_json())
        if decoded != profile:
            return False
        return self._update(layout_profile=profile.to_json())

    def clear_layout_profile(self) -> bool:
        if "layout_profile" not in self._data:
            return True
        updated = self._data.copy()
        del updated["layout_profile"]
        return self._replace(updated)

    def consume_layout_profile(self, expected: LayoutProfile) -> bool:
        """Remove only the same one-shot profile the caller applied."""
        if expected.scope != "once" or self.layout_profile != expected:
            return False
        return self.clear_layout_profile()
