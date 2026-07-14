"""Persistent, user-assigned session titles (renames).

Claude Code rewrites its own ``ai-title`` record almost every turn, so a rename
appended to the session JSONL is clobbered on the very next turn (title parsing
keeps the *last* ai-title).  railmux therefore owns renames in a sidecar JSON file
keyed by ``session_id`` — globally-unique UUIDs, so no per-project namespacing —
and overlays them onto ``SessionMeta.title`` at read time, immune to Claude's
re-titling.  Same pattern as :mod:`railmux.favorites`.
"""
from __future__ import annotations

import json
from pathlib import Path

from railmux.atomic_file import atomic_write_text, migrate_from_ccmgr


def _renames_path() -> Path:
    return Path.home() / ".config" / "railmux" / "renames.json"


class Renames:
    """In-memory ``session_id -> title`` map, backed by a JSON file."""

    def __init__(self) -> None:
        self._titles: dict[str, str] = {}
        self._path = _renames_path()
        self._load()

    def _load(self) -> None:
        migrate_from_ccmgr(self._path)
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            self._titles = {
                str(k): v for k, v in data.items()
                if isinstance(v, str) and v
            }

    def _save(self) -> None:
        try:
            atomic_write_text(
                self._path,
                json.dumps(self._titles, ensure_ascii=False,
                           indent=2, sort_keys=True),
            )
        except OSError:
            pass

    def get(self, session_id: str) -> str | None:
        """The user-assigned title for *session_id*, or ``None``."""
        return self._titles.get(session_id)

    def set(self, session_id: str, title: str) -> None:
        """Assign *title* to *session_id*.  An empty title clears the rename."""
        title = title.strip()
        if not title:
            self.clear(session_id)
            return
        if self._titles.get(session_id) == title:
            return
        self._titles[session_id] = title
        self._save()

    def clear(self, session_id: str) -> None:
        """Drop any rename for *session_id* (revert to the auto title)."""
        if session_id in self._titles:
            del self._titles[session_id]
            self._save()
