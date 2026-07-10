"""mtime-keyed cache wrapping ccmgr.session_index.list_sessions."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ccmgr.models import Project, SessionMeta
from ccmgr.session_index import _scan_session, _TOOL_BLOCK_AGE_S


_DEFAULT_TOP_N = 30


FileSignature = tuple[int, int]  # (mtime_ns, size)


class SessionCache:
    def __init__(self) -> None:
        self._entries: dict[Path, tuple[FileSignature, SessionMeta]] = {}

    def list_sessions(self, project: Project, top_n: int = _DEFAULT_TOP_N) -> list[SessionMeta]:
        """Return up to `top_n` most-recent sessions for `project`.

        Older sessions beyond `top_n` exist on disk but are not parsed here.
        This keeps heavy-traffic projects (30+ sessions) snappy on cold cache
        fills. Set `top_n=0` for no cap (full scan).
        """
        # Phase 1: scandir for mtimes (cheap).
        candidates: list[tuple[FileSignature, Path]] = []
        try:
            scan = os.scandir(project.claude_dir)
        except OSError:
            return []
        with scan:
            for entry in scan:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                candidates.append((signature, Path(entry.path)))

        # Phase 2: sort by mtime desc, optionally cap.
        candidates.sort(key=lambda item: item[0][0], reverse=True)
        if top_n > 0:
            candidates = candidates[:top_n]

        # Phase 3: parse (with cache).
        now = time.time()
        current_paths: set[Path] = set()
        results: list[SessionMeta] = []
        for signature, path in candidates:
            current_paths.add(path)
            meta = self._meta_for(project, path, signature, now)
            if meta is not None:
                results.append(meta)

        # Evict stale entries from this project only. Other projects may have
        # running sessions whose metadata should remain warm between polls.
        for stale in list(self._entries.keys()):
            if stale.parent == project.claude_dir and stale not in current_paths:
                del self._entries[stale]

        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def get(self, project: Project, session_id: str) -> SessionMeta | None:
        """Cache-backed lookup of a single session's metadata by id.

        Used by the Running pane so its status comes from the same source as
        the Sessions pane (no separate scan path to drift out of sync)."""
        path = project.claude_dir / f"{session_id}.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size)
        return self._meta_for(project, path, signature, time.time())

    def _meta_for(self, project: Project, path: Path, signature: FileSignature,
                  now: float) -> SessionMeta | None:
        """Cached-or-scanned SessionMeta for `path`.

        A cached "busy" entry whose age has crossed the block window is
        re-scanned even though its signature is unchanged. Scan results are
        keyed to the signature captured before reading, so an append during
        the read forces another scan on the next poll.
        """
        cached = self._entries.get(path)
        if cached is not None and cached[0] == signature:
            meta = cached[1]
            if (meta.status != "busy"
                    or now - meta.last_mtime <= _TOOL_BLOCK_AGE_S):
                return meta
        meta = _scan_session(project, path)
        if meta is not None:
            self._entries[path] = (signature, meta)
        return meta

    def invalidate(self) -> None:
        self._entries.clear()
