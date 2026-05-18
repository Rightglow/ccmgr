"""mtime-keyed cache wrapping ccmgr.session_index.list_sessions."""
from __future__ import annotations

import os
from pathlib import Path

from ccmgr.models import Project, SessionMeta
from ccmgr.session_index import _scan_session


_DEFAULT_TOP_N = 30


class SessionCache:
    def __init__(self) -> None:
        self._entries: dict[Path, tuple[float, SessionMeta]] = {}

    def list_sessions(self, project: Project, top_n: int = _DEFAULT_TOP_N) -> list[SessionMeta]:
        """Return up to `top_n` most-recent sessions for `project`.

        Older sessions beyond `top_n` exist on disk but are not parsed here.
        This keeps heavy-traffic projects (30+ sessions) snappy on cold cache
        fills. Set `top_n=0` for no cap (full scan).
        """
        # Phase 1: scandir for mtimes (cheap).
        candidates: list[tuple[float, Path]] = []
        try:
            scan = os.scandir(project.claude_dir)
        except OSError:
            return []
        with scan:
            for entry in scan:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                candidates.append((mtime, Path(entry.path)))

        # Phase 2: sort by mtime desc, optionally cap.
        candidates.sort(key=lambda t: t[0], reverse=True)
        if top_n > 0:
            candidates = candidates[:top_n]

        # Phase 3: parse (with cache).
        current_paths: set[Path] = set()
        results: list[SessionMeta] = []
        for mtime, path in candidates:
            current_paths.add(path)
            cached = self._entries.get(path)
            if cached is not None and cached[0] == mtime:
                results.append(cached[1])
                continue
            meta = _scan_session(project, path)
            if meta is None:
                continue
            self._entries[path] = (meta.last_mtime, meta)
            results.append(meta)

        # Evict stale cache entries for files no longer in top-N or deleted.
        for stale in list(self._entries.keys()):
            if stale not in current_paths:
                del self._entries[stale]

        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def invalidate(self) -> None:
        self._entries.clear()
