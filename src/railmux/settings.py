"""App-mutable preferences in Railmux's single ``config.toml``.

Users and the Options UI share ``~/.config/railmux/config.toml``. TOMLKit
preserves comments, ordering, formatting, and unknown keys while Railmux
atomically updates only the settings it owns. Current-run choices stay in
memory; a next-launch layout profile is removed after it is consumed.
"""
from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from railmux.atomic_file import atomic_write_text
from railmux.config import default_config_path


OPTION_POLICIES = frozenset({"always", "ask", "never"})


def _config_path() -> Path:
    return default_config_path()


@dataclass(frozen=True)
class LayoutProfile:
    """Validated, size-independent outer-workspace geometry preference."""

    scope: str
    layout: str
    sidebar_permille: int
    primary_permille: int | None = None

    def to_toml(self) -> dict[str, object]:
        data: dict[str, object] = {
            "version": 1,
            "scope": self.scope,
            "layout": self.layout,
            "sidebar_permille": self.sidebar_permille,
        }
        if self.primary_permille is not None:
            data["primary_permille"] = self.primary_permille
        return data


def _plain(value: object) -> object:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _decode_layout_profile(raw: object) -> LayoutProfile | None:
    plain = _plain(raw)
    if not isinstance(plain, dict) or len(plain) > 5 or plain.get("version") != 1:
        return None
    if not set(plain).issubset({
        "version", "scope", "layout", "sidebar_permille", "primary_permille",
    }):
        return None
    scope = plain.get("scope")
    layout = plain.get("layout")
    sidebar = plain.get("sidebar_permille")
    primary = plain.get("primary_permille")
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


def _inline_table(values: dict[str, object]) -> tomlkit.items.InlineTable:
    result = tomlkit.inline_table()
    for key, value in values.items():
        result[key] = value
    return result


class Settings:
    """Validated mutable subset of the shared TOML configuration."""

    def __init__(self) -> None:
        self._path = _config_path()
        self._document = tomlkit.document()
        self._load()

    def _read_document(self):
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return tomlkit.document()
        except (OSError, UnicodeError):
            return None
        try:
            return tomlkit.parse(text)
        except TOMLKitError:
            return None

    def _load(self) -> None:
        document = self._read_document()
        if document is not None:
            self._document = document

    def _get(self, section_name: str, key: str) -> object | None:
        section = self._document.get(section_name)
        if not isinstance(section, MutableMapping):
            return None
        return _plain(section.get(key))

    @staticmethod
    def _table(document, section_name: str) -> MutableMapping | None:
        section = document.get(section_name)
        if section is None:
            section = tomlkit.table()
            document[section_name] = section
        return section if isinstance(section, MutableMapping) else None

    def _replace(self, document) -> bool:
        try:
            atomic_write_text(self._path, tomlkit.dumps(document))
        except OSError:
            return False
        self._document = document
        return True

    def _update_section(
        self,
        section_name: str,
        values: dict[str, Any],
        *,
        remove: tuple[str, ...] = (),
    ) -> bool:
        # Re-read immediately before every mutation. This preserves valid
        # manual edits and changes made by another Railmux process after this
        # instance started instead of rewriting a stale startup snapshot.
        updated = self._read_document()
        if updated is None:
            return False
        section = self._table(updated, section_name)
        if section is None:
            return False
        for key in remove:
            section.pop(key, None)
        for key, value in values.items():
            section[key] = value
        return self._replace(updated)

    # -- Codex auto-run --------------------------------------------------
    @property
    def codex_yolo_policy(self) -> str:
        policy = self._get("codex", "auto_run")
        return policy if policy in OPTION_POLICIES else "ask"

    def set_codex_yolo_policy(self, policy: str) -> bool:
        if policy not in OPTION_POLICIES:
            return False
        return self._update_section("codex", {"auto_run": policy})

    # -- Saved outer-workspace geometry ---------------------------------
    @property
    def layout_save_policy(self) -> str:
        policy = self._get("ui", "layout_retention")
        return policy if policy in OPTION_POLICIES else "ask"

    @property
    def layout_profile(self) -> LayoutProfile | None:
        return _decode_layout_profile(self._get("ui", "layout_profile"))

    def set_layout_save_policy(
        self,
        policy: str,
        profile: LayoutProfile | None = None,
    ) -> bool:
        if policy not in OPTION_POLICIES:
            return False
        if profile is not None:
            decoded = _decode_layout_profile(profile.to_toml())
            if decoded != profile:
                return False
            if (policy == "always" and profile.scope != "always") or (
                policy == "ask" and profile.scope != "once"
            ) or policy == "never":
                return False
        values: dict[str, Any] = {"layout_retention": policy}
        remove: tuple[str, ...] = ()
        if profile is None:
            remove = ("layout_profile",)
        else:
            values["layout_profile"] = _inline_table(profile.to_toml())
        return self._update_section("ui", values, remove=remove)

    def consume_layout_profile(self, expected: LayoutProfile) -> bool:
        if expected.scope != "once":
            return False
        document = self._read_document()
        if document is None:
            return False
        section = document.get("ui")
        if not isinstance(section, MutableMapping):
            return False
        if _decode_layout_profile(section.get("layout_profile")) != expected:
            return False
        section.pop("layout_profile")
        return self._replace(document)
