"""Scan a project directory for sessions, extracting cheap metadata."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ccmgr.models import Project, SessionMeta


def list_sessions(project: Project) -> list[SessionMeta]:
    """List all sessions in a project, sorted by mtime descending."""
    results: list[SessionMeta] = []
    for path in project.claude_dir.glob("*.jsonl"):
        meta = _scan_session(project, path)
        if meta is not None:
            results.append(meta)
    results.sort(key=lambda s: s.last_mtime, reverse=True)
    return results


def is_live(session: SessionMeta, threshold_seconds: float) -> bool:
    """True if the session's JSONL was modified within `threshold_seconds`."""
    return (time.time() - session.last_mtime) <= threshold_seconds


def _scan_session(project: Project, jsonl_path: Path) -> SessionMeta | None:
    session_id = jsonl_path.stem
    if not _looks_like_uuid(session_id):
        return None

    title: str | None = None
    message_count = 0
    token_total = 0

    try:
        with jsonl_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "ai-title":
                    title = rec.get("aiTitle") or title
                elif rtype == "user":
                    message_count += 1
                elif rtype == "assistant":
                    message_count += 1
                    usage = rec.get("message", {}).get("usage", {}) or {}
                    token_total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    except OSError:
        return None

    try:
        mtime = jsonl_path.stat().st_mtime
    except OSError:
        return None

    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=jsonl_path,
        title=title,
        message_count=message_count,
        token_total=token_total,
        last_mtime=mtime,
    )


def _looks_like_uuid(s: str) -> bool:
    # 8-4-4-4-12 hex pattern
    parts = s.split("-")
    if len(parts) != 5:
        return False
    lengths = [8, 4, 4, 4, 12]
    if [len(p) for p in parts] != lengths:
        return False
    try:
        for p in parts:
            int(p, 16)
    except ValueError:
        return False
    return True
