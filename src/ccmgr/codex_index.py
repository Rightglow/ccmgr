"""Scan ~/.codex/sessions/ for Codex CLI sessions, extracting cheap metadata.

Codex stores sessions as date-hierarchical JSONL files::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Each file begins with a ``session_meta`` record, followed by ``response_item``
(conversation turns) and ``event_msg`` (token counts, lifecycle events).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ccmgr.models import Project, SessionMeta

# Same threshold as session_index.py — a pending function_call that hasn't
# written in this many seconds is presumed blocked on user approval.
_TOOL_BLOCK_AGE_S = 10


FileSignature = tuple[int, int]  # (mtime_ns, size)


class CodexIndex:
    """mtime-keyed cache of all Codex sessions under ``codex_home/sessions/``."""

    def __init__(self, codex_home: Path) -> None:
        self._codex_home = codex_home
        self._sessions_dir = codex_home / "sessions"
        # path -> (file signature captured before parsing, metadata)
        self._entries: dict[Path, tuple[FileSignature, SessionMeta]] = {}

    # -- public API -------------------------------------------------------

    def refresh(self) -> None:
        """Refresh cached metadata once for a group of related queries."""
        self._refresh()

    def all_cwds(self, *, refresh: bool = True) -> set[Path]:
        """Set of cwds that have at least one Codex session.

        Used to filter the Projects pane in Codex mode — only projects whose
        ``real_path`` is in this set are shown.
        """
        if refresh:
            self._refresh()
        return {meta.project.real_path
                for _, meta in self._entries.values()
                if meta.project is not None}

    def sessions_for_cwd(
        self, cwd: Path, *, refresh: bool = True,
    ) -> list[SessionMeta]:
        """All Codex sessions whose ``cwd`` matches *cwd*, sorted by mtime desc."""
        if refresh:
            self._refresh()
        try:
            target = cwd.resolve()
        except OSError:
            target = cwd
        seen: set[str] = set()
        results: list[SessionMeta] = []
        for _, meta in self._entries.values():
            if meta.project is None:
                continue
            sid = meta.session_id
            if sid in seen:
                continue
            try:
                mc = meta.project.real_path.resolve()
            except OSError:
                mc = meta.project.real_path
            if mc == target:
                seen.add(sid)
                results.append(meta)
        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def get(self, session_id: str, *, refresh: bool = True) -> SessionMeta | None:
        """Look up a single Codex session by its UUID."""
        if refresh:
            self._refresh()
        for _, meta in self._entries.values():
            if meta.session_id == session_id:
                return meta
        return None

    def invalidate(self) -> None:
        self._entries.clear()

    # -- internal ---------------------------------------------------------

    def _refresh(self) -> None:
        """Stat cached files and re-scan any whose mtime changed (or new files)."""
        sessions_dir = self._sessions_dir
        if not sessions_dir.is_dir():
            return

        now = time.time()
        current_paths: set[Path] = set()
        # Walk the date hierarchy: sessions/YYYY/MM/DD/*.jsonl
        try:
            for root, _dirs, files in os.walk(sessions_dir):
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(root) / name
                    current_paths.add(path)
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    signature = (stat.st_mtime_ns, stat.st_size)
                    cached = self._entries.get(path)
                    if cached is not None and cached[0] == signature:
                        meta = cached[1]
                        # "busy" is time-dependent — re-scan if stale.
                        if (meta.status != "busy"
                                or now - meta.last_mtime <= _TOOL_BLOCK_AGE_S):
                            continue
                    meta = _scan_codex_session(path)
                    if meta is not None:
                        self._entries[path] = (signature, meta)
        except OSError:
            pass

        # Evict deleted files.
        for stale in list(self._entries):
            if stale not in current_paths:
                del self._entries[stale]


def _scan_codex_session(path: Path) -> SessionMeta | None:
    """Extract metadata from a single Codex rollout JSONL file.

    Returns ``None`` when the file is unreadable, has no session_meta header,
    or contains zero meaningful user/assistant messages.
    """
    # -- read first line for session_meta -------------------------------
    try:
        f = path.open("r", encoding="utf-8")
    except OSError:
        return None
    try:
        first_line = f.readline().strip()
        if not first_line:
            f.close()
            return None
        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            f.close()
            return None
        if first.get("type") != "session_meta":
            f.close()
            return None
        payload = first.get("payload", {}) or {}
        session_id = payload.get("id")
        if not session_id:
            f.close()
            return None
        cwd_str = payload.get("cwd", "")
        cwd = Path(cwd_str) if cwd_str else None

        # -- scan remaining lines for messages, events -------------------
        title: str | None = None
        message_count = 0
        token_total = 0
        first_user_message: str | None = None
        # For status inference we track the last meaningful record type.
        last_rtype: str = ""

        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = rec.get("type", "")
            if rtype == "response_item":
                rp = rec.get("payload", {}) or {}
                pt = rp.get("type", "")
                if pt == "message":
                    role = rp.get("role", "")
                    if role == "user":
                        message_count += 1
                        last_rtype = "user"
                        content = rp.get("content", [])
                        text = _extract_codex_text(content)
                        if text is not None and first_user_message is None:
                            if not _is_codex_synthetic_message(text):
                                first_user_message = text
                    elif role == "assistant":
                        message_count += 1
                        last_rtype = "assistant"
                elif pt == "function_call":
                    last_rtype = "function_call"
            elif rtype == "event_msg":
                ep = rec.get("payload", {}) or {}
                event = ep.get("event", {}) or {}
                if event.get("type") == "token_count":
                    info = event.get("info", {}) or {}
                    total = info.get("total_token_usage", {}) or {}
                    token_total += int(total.get("input_tokens", 0) or 0)
                    token_total += int(total.get("output_tokens", 0) or 0)
    finally:
        f.close()

    # -- skip empty sessions --------------------------------------------
    if message_count == 0:
        return None

    # -- file stat -------------------------------------------------------
    try:
        st = path.stat()
    except OSError:
        return None
    mtime = st.st_mtime
    size_bytes = st.st_size

    # -- status ----------------------------------------------------------
    pending_tool = last_rtype == "function_call"
    if last_rtype == "user":
        status = "busy"
    elif pending_tool:
        age = time.time() - mtime
        status = "blocked" if age > _TOOL_BLOCK_AGE_S else "busy"
    else:
        status = "idle"

    # -- title fallback: first user message, first line ------------------
    if first_user_message:
        first_line = first_user_message.split("\n")[0]
        title = first_line[:60] + ("..." if len(first_line) > 60 else "")

    # -- preview: first line of first user message ------------------------
    preview: str | None = None
    if first_user_message:
        first_line = first_user_message.split("\n")[0]
        preview = first_line[:117] + ("..." if len(first_line) > 120 else "") if len(first_line) > 120 else first_line

    # Synthesize a Project from the cwd.
    cwd_path = cwd or Path("/")
    try:
        resolved = cwd_path.resolve()
    except OSError:
        resolved = cwd_path
    project = Project(
        real_path=resolved,
        encoded_name=_safe_encoded_name(resolved),
        claude_dir=Path(),  # unused for Codex sessions
        session_count=0,
        last_activity_ts=0.0,
    )

    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=path,
        title=title,
        message_count=message_count,
        token_total=token_total,
        last_mtime=mtime,
        size_bytes=size_bytes,
        git_branch=None,
        last_user_message=preview,
        status=status,
        pending_tool=pending_tool,
        session_type="codex",
    )


def _extract_codex_text(content: list) -> str | None:
    """Pull meaningful display text from Codex content blocks.

    Codex uses ``input_text`` for user messages and ``output_text`` for
    assistant messages.  Both are regular strings (not markdown blocks).
    """
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("input_text", "output_text"):
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _is_codex_synthetic_message(text: str) -> bool:
    """True when *text* is a system-generated placeholder, not a real user message.

    Codex prepends several synthetic user messages at the start of every
    session: ``<environment_context>``, ``# AGENTS.md instructions``,
    ``<permissions instructions>``, ``<collaboration_mode>``, etc.
    These make terrible titles — skip them so the first *real* user
    message becomes the display title.
    """
    return (text.startswith("<") or text.startswith("# AGENTS.md"))


def _safe_encoded_name(cwd: Path) -> str:
    """Stable encoded name for a cwd — used as a synthetic Project key."""
    # Use a simple scheme: replace separators and special chars with hyphens.
    s = str(cwd.resolve())
    out = "".join(c if c.isalnum() or c in "/." else "-" for c in s)
    # Prefix with "-" so it doesn't collide with Claude's path-encoded names
    # (which also start with "-").
    return "-cx-" + out.replace("/", "-").strip("-")[:120]
