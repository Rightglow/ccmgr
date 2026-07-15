"""Enumerate Claude project directories from ~/.claude/projects/."""
from __future__ import annotations

import json
import os
from pathlib import Path

from railmux.atomic_file import atomic_write_text
from railmux.models import Project
from railmux.path_codec import decode
from railmux.session_index import _looks_like_uuid, _scan_session


# The top-level cache avoids repeatedly decoding Claude's encoded project names.
# Session validity is cached separately per JSONL signature so an existing file
# can transition from an empty startup stub to a resumable conversation without
# forcing every session in every project to be reparsed on each poll.
_cache: dict[Path, tuple[int, list[Project]]] = {}
_session_validity: dict[
    Path, dict[Path, tuple[tuple[int, int, int], bool]]
] = {}


def invalidate_session(path: Path) -> None:
    """Forget cached validity for one Claude JSONL after app-owned mutation."""
    cached = _session_validity.get(path.parent)
    if cached is None:
        return
    cached.pop(path, None)
    if not cached:
        _session_validity.pop(path.parent, None)


def _path_cache_file() -> Path:
    """Location of the persistent encoded-name -> real-path cache.

    Decoding an encoded project name is expensive on an NFS home (see
    path_codec), and the mapping is stable, so we persist it across launches.
    """
    return Path.home() / ".config" / "railmux" / "path-cache.json"


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

    Uses a parent-dir-mtime cache to avoid repeatedly decoding project names.
    Existing project directories still receive a cheap signature scan so
    session counts and recency track files created, changed, or removed below
    the cached top-level directory.
    """
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return []

    try:
        parent_mtime = projects_dir.stat().st_mtime_ns
    except OSError:
        return []

    cached = _cache.get(claude_home)
    if cached is not None and cached[0] == parent_mtime:
        # Parent dir hasn't changed shape; refresh per-project session metadata
        # without re-decoding every project name.
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

    # Avoid retaining per-session signatures for project metadata directories
    # that were removed while railmux was running.
    for claude_dir in list(_session_validity):
        if claude_dir.parent == projects_dir and claude_dir.name not in seen:
            del _session_validity[claude_dir]

    results.sort(key=lambda p: p.last_activity_ts, reverse=True)
    _cache[claude_home] = (parent_mtime, results)
    return results


def _count_and_latest_mtime(claude_dir: Path, real_path: Path) -> tuple[int, float]:
    """Count sessions that pass every ``_scan_session`` filter and return the max
    JSONL mtime.  Uses ``_scan_session`` so the count exactly matches the
    sidebar: background jobs, metadata stubs, and orphans are all excluded.

    JSONL validity is cached by nanosecond mtime + size. The common refresh path
    still performs the directory stat walk needed for recency, but opens only
    files that were added or changed. This is important for new sessions: Claude
    creates the JSONL before its first complete turn, so its validity can change
    without the top-level ``projects/`` directory mtime changing.
    """
    # Minimal project object — only .real_path is used by _scan_session (and
    # only for the SessionMeta return value, which we discard).
    temp_project = Project(
        real_path=real_path, encoded_name="", claude_dir=claude_dir,
        session_count=0, last_activity_ts=0.0,
    )
    count = 0
    latest = 0.0
    current_paths: set[Path] = set()
    validity = _session_validity.setdefault(claude_dir, {})
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
            path = Path(entry.path)
            current_paths.add(path)
            try:
                stat = entry.stat()
            except OSError:
                continue
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = validity.get(path)
            if cached is not None and cached[0] == signature:
                valid = cached[1]
            elif (cached is not None and cached[1]
                  and cached[0][0] == stat.st_ino
                  and stat.st_size > cached[0][2]):
                # Claude session JSONLs are append-only. Once a file is valid,
                # a same-inode size increase cannot make the completed turns
                # disappear, so avoid re-reading a potentially multi-MB active
                # conversation every refresh. Replacement, truncation, or an
                # app-owned delete is rescanned (the latter is invalidated
                # explicitly via ``invalidate_session``).
                valid = True
                validity[path] = (signature, True)
            else:
                valid = _scan_session(temp_project, path) is not None
                validity[path] = (signature, valid)
            if not valid:
                continue
            count += 1
            m = stat.st_mtime
            if m > latest:
                latest = m
    for stale in set(validity) - current_paths:
        del validity[stale]
    if not validity:
        _session_validity.pop(claude_dir, None)
    return count, latest


def _refresh_activity(projects: list[Project]) -> list[Project]:
    """Refresh counts and activity while reusing decoded project paths.

    A top-level ``projects/`` mtime only tells us whether project directories
    changed. Files are created and removed one level below it, and existing
    startup stubs become valid through content writes, so counts must be
    refreshed independently. ``_count_and_latest_mtime`` opens only JSONLs
    whose signatures changed.
    """
    out: list[Project] = []
    for p in projects:
        count, latest = _count_and_latest_mtime(p.claude_dir, p.real_path)
        if latest == 0.0:
            try:
                latest = p.claude_dir.stat().st_mtime
            except OSError:
                latest = p.last_activity_ts
        out.append(Project(
            real_path=p.real_path,
            encoded_name=p.encoded_name,
            claude_dir=p.claude_dir,
            session_count=count,
            last_activity_ts=latest,
        ))
    out.sort(key=lambda x: x.last_activity_ts, reverse=True)
    return out
