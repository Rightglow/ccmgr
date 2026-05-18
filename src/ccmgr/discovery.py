"""Enumerate Claude project directories from ~/.claude/projects/."""
from __future__ import annotations

import os
from pathlib import Path

from ccmgr.models import Project
from ccmgr.path_codec import decode


# Module-level cache: (claude_home, projects_dir_mtime) -> list[Project]
_cache: dict[Path, tuple[float, list[Project]]] = {}


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
            try:
                real_path = decode(entry.name)
            except ValueError:
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

    results.sort(key=lambda p: p.last_activity_ts, reverse=True)
    _cache[claude_home] = (parent_mtime, results)
    return results


def _count_and_latest_mtime(claude_dir: Path) -> tuple[int, float]:
    """Count *.jsonl files and the max mtime in one scandir pass."""
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
