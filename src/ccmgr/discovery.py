"""Enumerate Claude project directories from ~/.claude/projects/."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ccmgr.atomic_file import atomic_write_text
from ccmgr.models import Project
from ccmgr.path_codec import decode
from ccmgr.session_index import _looks_like_uuid, _scan_session


# Module-level cache: (claude_home, projects_dir_mtime) -> list[Project]
_cache: dict[Path, tuple[float, list[Project]]] = {}


def _path_cache_file() -> Path:
    """Location of the persistent encoded-name -> real-path cache.

    Decoding an encoded project name is expensive on an NFS home (see
    path_codec), and the mapping is stable, so we persist it across launches.
    """
    return Path.home() / ".config" / "ccmgr" / "path-cache.json"


def _load_path_cache() -> dict[str, str]:
    try:
        data = json.loads(_path_cache_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_path_cache(cache: dict[str, str]) -> None:
    path = _path_cache_file()
    try:
        atomic_write_text(path, json.dumps(cache, indent=0))
    except OSError:
        pass


def list_projects(claude_home: Path) -> list[Project]:
    """Return every project directory under <claude_home>/projects/, sorted by recency.

    Uses a parent-dir-mtime cache: if no project was added/removed since the last
    call, we skip the full scan and just refresh each project's last_activity_ts
    via a single stat per project dir (instead of stat-ing every JSONL).
    """
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return []

    try:
        parent_mtime = projects_dir.stat().st_mtime
    except OSError:
        return []

    cached = _cache.get(claude_home)
    if cached is not None and cached[0] == parent_mtime:
        # Parent dir hasn't changed shape; just refresh last_activity_ts.
        refreshed = _refresh_activity(cached[1])
        _cache[claude_home] = (parent_mtime, refreshed)
        return refreshed

    # Full scan needed.
    path_cache = _load_path_cache()
    original_cache = dict(path_cache)
    seen: set[str] = set()
    results: list[Project] = []
    try:
        scan = os.scandir(projects_dir)
    except OSError:
        return []
    with scan:
        for entry in scan:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not entry.name.startswith("-"):
                continue
            seen.add(entry.name)

            # Resolve the encoded name to a real path. Prefer the persistent
            # cache (a single is_dir() check) and only fall back to the costly
            # decode() when the cache misses or is stale.
            real_path: Path | None = None
            cached = path_cache.get(entry.name)
            if cached is not None and Path(cached).is_dir():
                real_path = Path(cached)
            else:
                try:
                    decoded = decode(entry.name)
                except ValueError:
                    decoded = None
                # Skip projects whose original directory no longer exists on
                # this machine (deleted or moved). They can't be launched —
                # `cd` into a missing dir fails — so don't list them, and drop
                # any stale cache entry for them.
                if decoded is not None and decoded.is_dir():
                    real_path = decoded
                    path_cache[entry.name] = str(decoded)
                else:
                    path_cache.pop(entry.name, None)
            if real_path is None:
                continue

            claude_dir = Path(entry.path)
            session_count, last_ts = _count_and_latest_mtime(claude_dir, real_path)
            if session_count == 0:
                try:
                    last_ts = entry.stat().st_mtime
                except OSError:
                    last_ts = 0.0

            results.append(Project(
                real_path=real_path,
                encoded_name=entry.name,
                claude_dir=claude_dir,
                session_count=session_count,
                last_activity_ts=last_ts,
            ))

    # Drop cache entries for project dirs that have since vanished, then
    # persist only if something actually changed (avoid needless writes).
    for gone in set(path_cache) - seen:
        del path_cache[gone]
    if path_cache != original_cache:
        _save_path_cache(path_cache)

    results.sort(key=lambda p: p.last_activity_ts, reverse=True)
    _cache[claude_home] = (parent_mtime, results)
    return results


def _count_and_latest_mtime(claude_dir: Path, real_path: Path) -> tuple[int, float]:
    """Count sessions that pass every ``_scan_session`` filter and return the max
    JSONL mtime.  Uses ``_scan_session`` so the count exactly matches the
    sidebar: background jobs, metadata stubs, and orphans are all excluded.

    Only called on the **full-scan** path (a session file was added or removed
    and the parent directory mtime changed).  On the common refresh path we
    reuse the last count and only re-stat mtimes, avoiding an open storm.
    """
    # Minimal project object — only .real_path is used by _scan_session (and
    # only for the SessionMeta return value, which we discard).
    temp_project = Project(
        real_path=real_path, encoded_name="", claude_dir=claude_dir,
        session_count=0, last_activity_ts=0.0,
    )
    count = 0
    latest = 0.0
    try:
        scan = os.scandir(claude_dir)
    except OSError:
        return 0, 0.0
    with scan:
        for entry in scan:
            if not entry.name.endswith(".jsonl"):
                continue
            if not _looks_like_uuid(Path(entry.name).stem):
                continue
            if _scan_session(temp_project, Path(entry.path)) is None:
                continue
            count += 1
            try:
                m = entry.stat().st_mtime
            except OSError:
                continue
            if m > latest:
                latest = m
    return count, latest


def _stat_jsonls_mtime(claude_dir: Path) -> float:
    """Fast path: max mtime among UUID-named JSONL files without opening any
    of them.  Used on every poll tick so the parent-dir-mtime cache keeps its
    promise of zero file reads when nothing was added or removed."""
    latest = 0.0
    try:
        scan = os.scandir(claude_dir)
    except OSError:
        return 0.0
    with scan:
        for entry in scan:
            if not entry.name.endswith(".jsonl"):
                continue
            if not _looks_like_uuid(Path(entry.name).stem):
                continue
            try:
                m = entry.stat().st_mtime
            except OSError:
                continue
            if m > latest:
                latest = m
    return latest


def _refresh_activity(projects: list[Project]) -> list[Project]:
    """Cheap re-stat: update ``last_activity_ts`` from the max JSONL mtime.

    The parent directory mtime hasn't changed (otherwise we'd be on the
    full-scan path), so no files were added or removed — the count is
    stable.  We only re-stat JSONL files (no reads) to catch content-only
    writes, which keep the fast path truly cheap.
    """
    out: list[Project] = []
    for p in projects:
        latest = _stat_jsonls_mtime(p.claude_dir)
        if latest == 0.0:
            try:
                latest = p.claude_dir.stat().st_mtime
            except OSError:
                latest = p.last_activity_ts
        out.append(Project(
            real_path=p.real_path,
            encoded_name=p.encoded_name,
            claude_dir=p.claude_dir,
            session_count=p.session_count,  # stable — no files added/removed
            last_activity_ts=latest,
        ))
    out.sort(key=lambda x: x.last_activity_ts, reverse=True)
    return out
