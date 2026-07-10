"""Enumerate Claude project directories from ~/.claude/projects/."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ccmgr.atomic_file import atomic_write_text
from ccmgr.models import Project
from ccmgr.path_codec import decode
from ccmgr.session_index import _looks_like_uuid


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
            session_count, last_ts = _count_and_latest_mtime(claude_dir)
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


def _count_and_latest_mtime(claude_dir: Path) -> tuple[int, float]:
    """Count UUID-named *.jsonl session files and the max mtime in one scandir pass."""
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
            count += 1
            try:
                m = entry.stat().st_mtime
            except OSError:
                continue
            if m > latest:
                latest = m
    return count, latest


def _refresh_activity(projects: list[Project]) -> list[Project]:
    """Cheap re-stat: update last_activity_ts using the project dir mtime only.

    The project dir mtime updates whenever a JSONL is added/removed/renamed. It
    does NOT update on plain content writes, so we ALSO read the project's
    JSONL set quickly via scandir and pick the max mtime. This is still much
    cheaper than the full path_codec decode + iterdir done on a cache miss.
    """
    out: list[Project] = []
    for p in projects:
        sessions_count, latest = _count_and_latest_mtime(p.claude_dir)
        if sessions_count == 0:
            try:
                latest = p.claude_dir.stat().st_mtime
            except OSError:
                latest = p.last_activity_ts
        out.append(Project(
            real_path=p.real_path,
            encoded_name=p.encoded_name,
            claude_dir=p.claude_dir,
            session_count=sessions_count,
            last_activity_ts=latest,
        ))
    out.sort(key=lambda x: x.last_activity_ts, reverse=True)
    return out
