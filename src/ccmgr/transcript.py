"""Format a Claude Code session JSONL as human-readable ANSI text for ``less -R``.

Usage::

    python3 -m ccmgr.transcript <jsonl_path>
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


def _is_real_user(record: dict) -> bool:
    """True when *record* is a genuine user message (not a synthetic tool-result)."""
    if record.get("type") != "user":
        return False
    msg = record.get("message")
    if not msg or msg.get("role") != "user":
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
        return any(b.get("type") != "tool_result" for b in content)
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
        if block.get("type") == "text" and block.get("text", "").strip():
            parts.append(block["text"].strip() + "\n")
    return "".join(parts) if len(parts) > 1 else None


def _render_assistant_blocks(record: dict):
    """Yield formatted strings for each display-worthy block in an assistant message."""
    content = record.get("message", {}).get("content")
    if not isinstance(content, list):
        return

    usage = record.get("message", {}).get("usage", {})
    tokens = None
    if usage:
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    for block in content:
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


def format_transcript(source: Path | object):
    """Read a session JSONL and yield ANSI-formatted strings.

    *source* may be a ``Path`` or any file-like object (e.g. ``sys.stdin``).

    Callers should write each chunk to stdout, e.g.::

        for chunk in format_transcript(path_or_file):
            sys.stdout.write(chunk)
    """
    own = False
    fh = None
    # When True we're rendering a Codex rollout JSONL; when False, Claude.
    _codex: bool | None = None
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

            rtype = record.get("type", "")

            # -- auto-detect format on first record ---------------------
            if _codex is None:
                _codex = rtype == "session_meta"

            if _codex:
                yield from _render_codex(record)
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


def _render_codex(record: dict):
    """Dispatch a single Codex JSONL record to the right renderer."""
    rtype = record.get("type", "")
    if rtype == "session_meta":
        yield from _render_codex_session_meta(record)
    elif rtype == "response_item":
        yield from _render_codex_response_item(record)
    # event_msg, turn_context — skip (token counts, lifecycle noise)


def _render_codex_session_meta(record: dict):
    """Show session metadata as a header."""
    payload = record.get("payload", {}) or {}
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


def _render_codex_response_item(record: dict):
    """Render a response_item — message, function_call, or function_call_output."""
    payload = record.get("payload", {}) or {}
    pt = payload.get("type", "")
    if pt == "message":
        yield from _render_codex_message(payload)
    elif pt == "function_call":
        yield from _render_codex_function_call(payload)


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


def _render_codex_function_call(payload: dict):
    """Render a tool / function call."""
    name = payload.get("name", "?")
    arguments = payload.get("arguments", "")
    yield f"\n{YELLOW}{BOLD}───── Tool: {name} ─────{RESET}\n"
    if arguments:
        arg_str = str(arguments)
        if len(arg_str) > 300:
            arg_str = arg_str[:300] + f"{DIM}…{RESET}"
        yield f"  {DIM}args:{RESET} {arg_str}\n"


# ── CLI entry point ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv
    if len(argv) < 2:
        print("Usage: python3 -m ccmgr.transcript <jsonl_path|- for stdin>", file=sys.stderr)
        sys.exit(1)
    source = argv[1]
    if source == "-":
        for chunk in format_transcript(sys.stdin):
            sys.stdout.write(chunk)
    else:
        jsonl_path = Path(source)
        if not jsonl_path.exists():
            print(f"File not found: {jsonl_path}", file=sys.stderr)
            sys.exit(1)
        for chunk in format_transcript(jsonl_path):
            sys.stdout.write(chunk)


if __name__ == "__main__":
    main()
