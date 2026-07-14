"""Format a Claude Code session JSONL as human-readable ANSI text for ``less -R``.

Usage::

    python3 -m railmux.transcript <jsonl_path>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── ANSI styles ──────────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"

# ── Record-type filters ──────────────────────────────────────────────────
_SKIP_TYPES = frozenset({
    "mode", "permission-mode", "file-history-snapshot", "attachment",
    "system", "ai-title", "last-prompt", "summary",
})

# Top-level record types that only ever appear in a Codex rollout JSONL.  The
# preview pipeline in the UI runs ``tail -n 2000`` before piping into this
# module, so for rollouts longer than 2000 lines the leading ``session_meta``
# header is dropped and the first record seen here is a ``response_item`` /
# ``event_msg`` / ``turn_context``.  Detect Codex by the *presence* of any of
# these types rather than by a leading ``session_meta`` (see issue #5).
_CODEX_TYPES = frozenset({
    "session_meta", "response_item", "event_msg", "turn_context",
    "compacted", "world_state",
})
_CLAUDE_TYPES = _SKIP_TYPES | frozenset({"user", "assistant"})


def _is_real_user(record: dict) -> bool:
    """True when *record* is a genuine user message (not a synthetic tool-result)."""
    if record.get("type") != "user":
        return False
    msg = record.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return False
        # Drop Claude Code system-command / local-command echo lines.
        if stripped.startswith("<command-name") or stripped.startswith("<local-command"):
            return False
        return True
    if isinstance(content, list):
        # A user message whose content is a list of blocks — treat as real
        # only when at least one block is *not* a tool_result.
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result"
            for block in content
        )
    return False


def _render_user(record: dict) -> str | None:
    msg = record["message"]
    content = msg["content"]
    header = f"\n{CYAN}{BOLD}───── User ─────{RESET}\n"
    if isinstance(content, str):
        return header + content.strip() + "\n"
    # Mixed blocks — emit text blocks only (tool_result blocks are noise).
    parts = [header]
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            parts.append(block["text"].strip() + "\n")
    return "".join(parts) if len(parts) > 1 else None


def _render_assistant_blocks(record: dict):
    """Yield formatted strings for each display-worthy block in an assistant message."""
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return

    usage = message.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    tokens = None
    if usage:
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type", "")
        if bt == "text":
            token_str = f"  ({tokens:,} tokens)" if tokens else ""
            yield f"\n{GREEN}{BOLD}───── Assistant{token_str} ─────{RESET}\n"
            yield block.get("text", "") + "\n"
            tokens = None  # only show on first text block
        elif bt == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input", {})
            yield f"\n{YELLOW}{BOLD}───── Tool: {name} ─────{RESET}\n"
            for key, value in inp.items():
                val_str = str(value)
                if len(val_str) > 300:
                    val_str = val_str[:300] + f"{DIM}…{RESET}"
                yield f"  {DIM}{key}:{RESET} {val_str}\n"


def format_transcript(source: Path | object, fmt: str | None = None):
    """Read a session JSONL and yield ANSI-formatted strings.

    *source* may be a ``Path`` or any file-like object (e.g. ``sys.stdin``).

    *fmt* is an optional explicit format hint (``"codex"`` or ``"claude"``).
    When omitted (the default), the format is auto-detected from the record
    types actually present in the stream, so a Codex rollout whose leading
    ``session_meta`` header was stripped by ``tail`` still renders as Codex.

    Callers should write each chunk to stdout, e.g.::

        for chunk in format_transcript(path_or_file):
            sys.stdout.write(chunk)
    """
    own = False
    fh = None
    # When True we're rendering a Codex rollout JSONL; when False, Claude.
    _codex: bool | None = None
    if fmt == "codex":
        _codex = True
    elif fmt == "claude":
        _codex = False
    # call_id -> tool name, so Codex tool outputs can be labelled with the
    # call that produced them (issue #3).
    codex_calls: dict[str, str] = {}
    try:
        if isinstance(source, Path):
            fh = open(source, "r", encoding="utf-8")
            own = True
        else:
            fh = source
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            rtype = record.get("type", "")

            # Unknown metadata should not lock detection to Claude: a tailed
            # Codex rollout can begin with a future record type before the next
            # response_item/event_msg establishes the format.
            if _codex is None:
                if rtype in _CODEX_TYPES:
                    _codex = True
                elif rtype in _CLAUDE_TYPES:
                    _codex = False
                else:
                    continue

            if _codex:
                yield from _render_codex(record, codex_calls)
                continue

            # -- Claude format (legacy) --------------------------------
            if rtype in _SKIP_TYPES:
                continue

            if _is_real_user(record):
                rendered = _render_user(record)
                if rendered:
                    yield rendered
            elif rtype == "assistant":
                yield from _render_assistant_blocks(record)
    except OSError:
        yield f"{YELLOW}Could not read: {source}{RESET}\n"
    finally:
        if own and fh is not None:
            fh.close()


# ── Codex format rendering ──────────────────────────────────────────────


def _render_codex(record: dict, calls: dict[str, str] | None = None):
    """Dispatch a single Codex JSONL record to the right renderer.

    *calls* accumulates ``call_id -> tool name`` so that tool-output records
    can be attributed back to the call that produced them.
    """
    if calls is None:
        calls = {}
    rtype = record.get("type", "")
    if rtype == "session_meta":
        yield from _render_codex_session_meta(record)
    elif rtype == "response_item":
        yield from _render_codex_response_item(record, calls)
    # event_msg, turn_context — skip (token counts, lifecycle noise)


def _render_codex_session_meta(record: dict):
    """Show session metadata as a header."""
    payload = record.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return
    sid = payload.get("id", "?")
    cwd = payload.get("cwd", "?")
    version = payload.get("cli_version", "")
    provider = payload.get("model_provider", "")
    yield f"\n{CYAN}{BOLD}───── Codex Session ─────{RESET}\n"
    yield f"  {DIM}id:{RESET}    {sid}\n"
    yield f"  {DIM}cwd:{RESET}   {cwd}\n"
    if provider:
        yield f"  {DIM}model:{RESET}  {provider}"
        if version:
            yield f"  (CLI {version})"
        yield "\n"


def _render_codex_response_item(record: dict, calls: dict[str, str] | None = None):
    """Render a response_item.

    Handles ``message`` plus both tool-call families:
    ``function_call`` / ``function_call_output`` and the (far more common)
    ``custom_tool_call`` / ``custom_tool_call_output`` — see issue #3.
    """
    if calls is None:
        calls = {}
    payload = record.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return
    pt = payload.get("type", "")
    if pt == "message":
        yield from _render_codex_message(payload)
    elif pt in ("function_call", "custom_tool_call"):
        yield from _render_codex_tool_call(payload, calls)
    elif pt in ("function_call_output", "custom_tool_call_output"):
        yield from _render_codex_tool_output(payload, calls)


def _render_codex_message(payload: dict):
    """Render a user or assistant message."""
    role = payload.get("role", "")
    content = payload.get("content", [])
    if not isinstance(content, list):
        return

    if role == "user":
        # Extract text from input_text blocks.
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            yield f"\n{CYAN}{BOLD}───── User ─────{RESET}\n"
            yield "\n".join(parts) + "\n"
    elif role == "assistant":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    yield f"\n{GREEN}{BOLD}───── Assistant ─────{RESET}\n"
                    yield text + "\n"
    # role == "developer" — skip (system instructions, permissions)
    # role == "tool" — skip (tool results)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"{DIM}…{RESET}"


def _render_codex_tool_call(payload: dict, calls: dict[str, str]):
    """Render a ``function_call`` or ``custom_tool_call``.

    ``function_call`` carries its parameters in ``arguments`` (a JSON string);
    ``custom_tool_call`` carries them in ``input``.  The ``call_id`` is recorded
    so the matching output can be attributed back to this call.
    """
    name = payload.get("name", "?")
    call_id = payload.get("call_id")
    if isinstance(call_id, str) and call_id:
        calls[call_id] = name
    yield f"\n{YELLOW}{BOLD}───── Tool: {name} ─────{RESET}\n"
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if arguments:
        yield f"  {DIM}args:{RESET} {_truncate(str(arguments), 300)}\n"


def _codex_output_text(output) -> str:
    """Flatten a Codex tool-output value into plain text.

    ``output`` is either a plain string or a list of ``{type, text}`` blocks.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for block in output:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(block, str) and block:
                parts.append(block)
        return "".join(parts)
    return ""


def _render_codex_tool_output(payload: dict, calls: dict[str, str]):
    """Render a ``function_call_output`` / ``custom_tool_call_output``.

    Paired to the originating call via ``call_id`` where available.
    """
    text = _codex_output_text(payload.get("output")).strip()
    if not text:
        return
    call_id = payload.get("call_id")
    name = calls.pop(call_id, None) if isinstance(call_id, str) else None
    label = f"Tool output: {name}" if name else "Tool output"
    yield f"\n{DIM}───── {label} ─────{RESET}\n"
    yield _truncate(text, 500) + "\n"


# ── CLI entry point ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv
    # Optional explicit format hint: ``--format codex|claude`` (or ``--format=…``).
    # Kept back-compatible: when absent the format is auto-detected.
    fmt: str | None = None
    args: list[str] = []
    it = iter(argv[1:])
    for tok in it:
        if tok == "--format":
            fmt = next(it, None)
        elif tok.startswith("--format="):
            fmt = tok.split("=", 1)[1]
        else:
            args.append(tok)
    if not args:
        print(
            "Usage: python3 -m railmux.transcript [--format codex|claude] "
            "<jsonl_path|- for stdin>",
            file=sys.stderr,
        )
        sys.exit(1)
    source = args[0]
    if source == "-":
        for chunk in format_transcript(sys.stdin, fmt):
            sys.stdout.write(chunk)
    else:
        jsonl_path = Path(source)
        if not jsonl_path.exists():
            print(f"File not found: {jsonl_path}", file=sys.stderr)
            sys.exit(1)
        for chunk in format_transcript(jsonl_path, fmt):
            sys.stdout.write(chunk)


if __name__ == "__main__":
    main()
