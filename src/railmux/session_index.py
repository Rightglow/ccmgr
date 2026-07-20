"""Scan a project directory for sessions, extracting cheap metadata."""
from __future__ import annotations

import json
import time
from pathlib import Path

from railmux.models import Project, SessionMeta


# Duration (seconds) after which a running tool_use is presumed to be
# waiting for user approval rather than still executing.  Must be long
# enough to cover auto-approved tool runs (bash commands, API calls)
# but short enough that genuinely-blocked sessions surface quickly.
_TOOL_BLOCK_AGE_S = 10


def list_sessions(project: Project) -> list[SessionMeta]:
    """List all sessions in a project, sorted by mtime descending."""
    results: list[SessionMeta] = []
    for path in project.claude_dir.glob("*.jsonl"):
        meta = _scan_session(project, path)
        if meta is not None:
            results.append(meta)
    results.sort(key=lambda s: s.last_mtime, reverse=True)
    return results


def _extract_text(content) -> str | None:
    """Pull meaningful display text from a user-message content field.

    Returns None when the content is a system command, tool result, or
    other internal markup that isn't useful for display.
    """
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return None
        # Skip system commands injected by Claude Code harness.
        if s.startswith("<command-name>") or s.startswith("<local-command"):
            return None
        return s
    if isinstance(content, list):
        # Content blocks — prefer text blocks, skip tool results.
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_result":
                continue  # tool output, not user text
            if btype == "text":
                t = block.get("text", "")
                if isinstance(t, str) and t.strip():
                    return t.strip()
        return None
    return None


def _nonnegative_int(value: object) -> int:
    """Return a provider count only when it is a genuine non-negative int."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _claude_usage_total(usage: object) -> int:
    """Total billed/context tokens reported for one Claude API message."""
    if not isinstance(usage, dict):
        return 0
    return sum(_nonnegative_int(usage.get(field)) for field in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ))


def _scan_session(project: Project, jsonl_path: Path) -> SessionMeta | None:
    session_id = jsonl_path.stem
    if not _looks_like_uuid(session_id):
        return None

    title: str | None = None
    user_count = 0
    assistant_keys: set[str] = set()
    assistant_token_totals: dict[str, int] = {}
    anonymous_assistant_seq = 0
    git_branch: str | None = None
    last_user_message: str | None = None
    first_user_message: str | None = None
    last_rtype: str = ""        # "user" or "assistant" — last meaningful record
    last_stop_reason: str = ""  # only set for assistant records

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
                # Background-job sessions are not interactive — they can't be
                # resumed in a terminal and shouldn't appear in the sidebar.
                if rec.get("sessionKind") == "bg":
                    return None
                rtype = rec.get("type")
                if rtype == "ai-title":
                    title = rec.get("aiTitle") or title
                # NOTE: "last-prompt" is deliberately NOT treated as a turn.
                # Claude Code writes it *after* an assistant turn completes
                # (order in the JSONL is `assistant end_turn → last-prompt →
                # user`), so counting it as a user turn made every finished
                # session show "busy" until the next prompt was sent.
                elif rtype == "user":
                    last_rtype = "user"
                    last_stop_reason = ""
                    # Only real conversational text is a message. Tool results
                    # use the provider's ``user`` role too, but counting them
                    # makes tool-heavy sessions look many times larger.
                    msg = rec.get("message", {}) or {}
                    text = (_extract_text(msg.get("content", ""))
                            if isinstance(msg, dict) else None)
                    if text is not None:
                        user_count += 1
                        last_user_message = text
                        if first_user_message is None:
                            first_user_message = text
                elif rtype == "assistant":
                    last_rtype = "assistant"
                    msg = rec.get("message", {}) or {}
                    if not isinstance(msg, dict):
                        msg = {}
                    last_stop_reason = msg.get("stop_reason", "")

                    # Claude Code can persist several streaming/tool records
                    # for one API message. Deduplicate on the provider message
                    # id (record uuid as a fallback), and retain the largest
                    # observed usage snapshot for that logical message.
                    message_id = msg.get("id")
                    record_uuid = rec.get("uuid")
                    if isinstance(message_id, str) and message_id:
                        assistant_key = f"message:{message_id}"
                    elif isinstance(record_uuid, str) and record_uuid:
                        assistant_key = f"record:{record_uuid}"
                    else:
                        assistant_key = f"anonymous:{anonymous_assistant_seq}"
                        anonymous_assistant_seq += 1
                    assistant_keys.add(assistant_key)
                    usage_total = _claude_usage_total(msg.get("usage"))
                    assistant_token_totals[assistant_key] = max(
                        usage_total,
                        assistant_token_totals.get(assistant_key, 0),
                    )
                if git_branch is None:
                    gb = rec.get("gitBranch")
                    if isinstance(gb, str) and gb:
                        git_branch = gb
    except OSError:
        return None

    assistant_count = len(assistant_keys)
    message_count = user_count + assistant_count
    token_total = sum(assistant_token_totals.values())

    # Skip sessions that can't be meaningfully resumed:
    # 1. No messages → metadata stub. Claude may recreate a deleted JSONL with
    #    only an ai-title record, which is still not a resumable conversation.
    # 2. Has user messages but zero assistant replies → orphan (e.g. a fork
    #    that never received a response).  Claude Code cannot resume these.
    if message_count == 0:
        return None
    if user_count > 0 and assistant_count == 0:
        return None

    try:
        st = jsonl_path.stat()
    except OSError:
        return None
    mtime = st.st_mtime
    size_bytes = st.st_size

    # Determine status from the last meaningful record.
    pending_tool = last_rtype == "assistant" and last_stop_reason == "tool_use"
    if last_rtype == "user":
        status = "busy"
    elif pending_tool:
        # tool_use is ambiguous: Claude may still be running the tool or
        # waiting for approval.  Without the live process (SessionCache path)
        # we fall back to a time heuristic; auto-approved tools (bash, web
        # fetch) can run for many seconds, so only flag blocked once writing
        # has genuinely ceased.  Callers with the tmux session refine this.
        age = time.time() - mtime
        status = "blocked" if age > _TOOL_BLOCK_AGE_S else "busy"
    else:
        status = "idle"

    # Fallback title: first meaningful user message (truncated).
    if title is None and first_user_message:
        first_line = first_user_message.split("\n")[0]
        title = first_line[:60] + ("..." if len(first_line) > 60 else "")
    elif title is not None and len(title) > 80:
        # Claude Code ai-title can be a long sentence.  Truncate so the
        # InfoModal and sidebar rows stay readable; the full text is still
        # in the JSONL and can be seen via less preview (C-b →).
        title = title[:80] + "…"

    # Truncate last user message for display (keep first line, ~120 chars).
    preview: str | None = None
    if last_user_message:
        first_line = last_user_message.split("\n")[0]
        if len(first_line) > 120:
            preview = first_line[:117] + "..."
        else:
            preview = first_line

    return SessionMeta(
        project=project,
        session_id=session_id,
        jsonl_path=jsonl_path,
        title=title,
        message_count=message_count,
        token_total=token_total,
        last_mtime=mtime,
        size_bytes=size_bytes,
        git_branch=git_branch,
        last_user_message=preview,
        status=status,
        pending_tool=pending_tool,
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
