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
from dataclasses import replace
from pathlib import Path

from railmux.models import Project, SessionMeta
from railmux.renames import Renames

# Same threshold as session_index.py — a pending function_call that hasn't
# written in this many seconds is presumed blocked on user approval.
_TOOL_BLOCK_AGE_S = 10


FileSignature = tuple[int, int]  # (mtime_ns, size)


class _ScanError:
    """Sentinel returned by :func:`_scan_codex_session` for a *transient*
    failure — an IO/OSError or an unexpected exception raised while reading a
    rollout — as distinct from ``None``, which marks a *deterministic* skip
    (a filtered codex_exec/subagent rollout, a missing cwd, an empty session,
    or a malformed session_meta header).

    The distinction drives the negative cache: deterministic skips are safe to
    remember by file signature so they aren't reopened every refresh, but a
    transient error must NOT be permanently negative-cached.  Otherwise a
    one-off NFS read glitch on an otherwise-stable rollout would hide it
    indefinitely (until its mtime+size changed or the index was invalidated).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "SCAN_ERROR"


# Module-level singleton — compared with ``is`` at the call sites.
SCAN_ERROR = _ScanError()


class CodexIndex:
    """mtime-keyed cache of all Codex sessions under ``codex_home/sessions/``."""

    def __init__(self, codex_home: Path, renames: Renames | None = None) -> None:
        self._codex_home = codex_home
        self._sessions_dir = codex_home / "sessions"
        # path -> (file signature captured before parsing, metadata)
        self._entries: dict[Path, tuple[FileSignature, SessionMeta]] = {}
        # Negative cache: files that scanned to ``None`` (filtered codex_exec /
        # subagent rollouts, empty/cwd-less files, unparseable JSON).  Keyed by
        # the signature they had when scanned so they aren't reopened every
        # refresh until their signature changes.  See ``_refresh``.
        self._negative: dict[Path, FileSignature] = {}
        # User-assigned titles, overlaid at read time (see railmux.renames).
        self._renames = renames

    def _with_override(self, meta: SessionMeta) -> SessionMeta:
        """Overlay a user rename onto *meta*'s title, if one exists."""
        if self._renames is None:
            return meta
        override = self._renames.get(meta.session_id)
        return replace(meta, title=override) if override else meta

    def _canonical(self) -> dict[str, SessionMeta]:
        """Map ``session_id -> newest cached entry`` for that id.

        Multiple rollout files can share one ``session_id`` (copies, migrated
        state, resumed threads).  Every query goes through this map so counts,
        lists and single-session lookups agree and the newest metadata always
        wins deterministically (instead of depending on ``os.walk`` order).
        """
        canon: dict[str, SessionMeta] = {}
        for _, meta in self._entries.values():
            if meta.project is None:
                continue
            sid = meta.session_id
            cur = canon.get(sid)
            if (cur is None
                    or (meta.last_mtime, str(meta.jsonl_path))
                    > (cur.last_mtime, str(cur.jsonl_path))):
                canon[sid] = meta
        return canon

    # -- public API -------------------------------------------------------

    def refresh(self) -> None:
        """Refresh cached metadata once for a group of related queries."""
        self._refresh()

    def all_cwds(self, *, refresh: bool = True) -> dict[Path, int]:
        """Map from cwd to Codex session count for every cwd that has at
        least one Codex session.

        Used to filter the Projects pane in Codex mode — only projects whose
        ``real_path`` is a key in this dict are shown, and the count is used
        for the sidebar badge.
        """
        if refresh:
            self._refresh()
        counts: dict[Path, int] = {}
        for meta in self._canonical().values():
            cwd = meta.project.real_path
            counts[cwd] = counts.get(cwd, 0) + 1
        return counts

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
        results: list[SessionMeta] = []
        for meta in self._canonical().values():
            try:
                mc = meta.project.real_path.resolve()
            except OSError:
                mc = meta.project.real_path
            if mc == target:
                results.append(self._with_override(meta))
        results.sort(key=lambda s: s.last_mtime, reverse=True)
        return results

    def get(self, session_id: str, *, refresh: bool = True) -> SessionMeta | None:
        """Look up a single Codex session by its UUID."""
        if refresh:
            self._refresh()
        meta = self._canonical().get(session_id)
        return self._with_override(meta) if meta is not None else None

    def invalidate(self) -> None:
        self._entries.clear()
        self._negative.clear()

    # -- internal ---------------------------------------------------------

    def _refresh(self) -> None:
        """Stat cached files and re-scan any whose mtime changed (or new files)."""
        sessions_dir = self._sessions_dir
        if not sessions_dir.is_dir():
            return

        now = time.time()
        current_paths: set[Path] = set()
        walk_failed = False

        def _walk_error(_error: OSError) -> None:
            nonlocal walk_failed
            walk_failed = True

        # Walk the date hierarchy: sessions/YYYY/MM/DD/*.jsonl
        try:
            for root, _dirs, files in os.walk(
                    sessions_dir, onerror=_walk_error):
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
                        # Only pending-tool status is time-dependent. Once its
                        # age crosses the threshold, update cached metadata;
                        # reopening an unchanged (possibly huge) rollout cannot
                        # reveal anything new and creates needless NFS I/O.
                        if (meta.pending_tool and meta.status == "busy"
                                and now - meta.last_mtime > _TOOL_BLOCK_AGE_S):
                            self._entries[path] = (
                                signature, replace(meta, status="blocked"))
                        continue
                    elif cached is None:
                        # Negative cache: a file that previously scanned to
                        # None (filtered / empty / unparseable) isn't reopened
                        # until its signature changes.
                        neg = self._negative.get(path)
                        if neg is not None and neg == signature:
                            continue
                    result = _scan_codex_session(path)
                    if isinstance(result, SessionMeta):
                        self._entries[path] = (signature, result)
                        self._negative.pop(path, None)
                    elif result is None:
                        # Deterministic skip (filtered codex_exec/subagent,
                        # missing cwd, empty, or malformed header): remember the
                        # miss by signature so we don't re-parse next tick, and
                        # drop any now-stale cached entry (file was reclassified
                        # or corrupted after its signature changed).
                        self._negative[path] = signature
                        self._entries.pop(path, None)
                    else:
                        # SCAN_ERROR — a transient IO/parse error.  Do NOT
                        # negative-cache it: that would hide an otherwise-stable
                        # rollout until its signature changed.  Leave existing
                        # state untouched so the next refresh retries this file
                        # (its signature won't match a live entry and it isn't
                        # in the negative cache), and it reappears once the
                        # transient error clears.
                        pass
        except OSError:
            walk_failed = True

        # Evict deleted files only after a complete traversal. A partial NFS or
        # permission failure must not make an entire skipped subtree disappear.
        if not walk_failed:
            for stale in list(self._entries):
                if stale not in current_paths:
                    del self._entries[stale]
            for stale in list(self._negative):
                if stale not in current_paths:
                    del self._negative[stale]


# Lifecycle events (``event_msg.payload.type``) that end a turn — after any of
# these the session is no longer "busy" unless a *newer* signal reopens it.
_CODEX_TURN_END = frozenset({"task_complete", "turn_aborted", "thread_rolled_back"})
# Tool-call / output record pairs, matched by ``call_id``.  Real Codex 0.144.x
# rollouts are dominated by ``custom_tool_call`` (exec, apply_patch, …); plain
# ``function_call`` is a minority.  Both must be paired to detect a pending tool.
_CODEX_TOOL_CALLS = frozenset({"function_call", "custom_tool_call"})
_CODEX_TOOL_OUTPUTS = frozenset({"function_call_output", "custom_tool_call_output"})


def _scan_codex_session(path: Path) -> SessionMeta | None | _ScanError:
    """Extract metadata from a single Codex rollout JSONL file.

    Tri-state result:

    * ``SessionMeta`` — a valid, indexable session.
    * ``None`` — a *deterministic* skip: the file is filtered (codex_exec /
      subagent), has no valid session_meta header / cwd, or contains zero
      meaningful messages.  Safe to negative-cache by signature.
    * ``SCAN_ERROR`` — a *transient* failure: the file couldn't be opened, or
      an unexpected exception was raised while reading it.  Must NOT be
      permanently negative-cached (see :class:`_ScanError`); the index retries
      it on the next refresh.

    Any *malformed record* (list/string/non-numeric where a dict/number is
    expected) is skipped inline rather than raising, so one bad line never
    aborts a scan and a structurally-bad rollout still yields a deterministic
    ``None`` — only genuinely unexpected errors surface as ``SCAN_ERROR``.
    """
    try:
        f = path.open("r", encoding="utf-8")
    except OSError:
        # Transient: the file may be mid-write, briefly unreadable, or on a
        # flaky NFS mount.  Signal ERROR so the index retries it rather than
        # hiding it behind the negative cache.
        return SCAN_ERROR
    try:
        return _parse_codex_session(path, f)
    except Exception:
        # A single corrupt / unexpected rollout must never crash the whole
        # index refresh — isolate it (#13).  Treat it as transient (retryable)
        # rather than a permanent skip, so a passing IO error can't hide the
        # file forever.
        return SCAN_ERROR
    finally:
        f.close()


def _parse_codex_session(path: Path, f) -> SessionMeta | None:
    # -- read first line for session_meta -------------------------------
    first_line = f.readline().strip()
    if not first_line:
        return None
    try:
        first = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    if not isinstance(payload, dict):
        return None
    # Skip non-interactive "codex exec" rollouts — review/automation
    # threads that would otherwise flood the sidebar.  Blocklist (not
    # allowlist) so any interactive originator, missing field, or future
    # value is still shown.
    if payload.get("originator") == "codex_exec":
        return None
    # Skip subagent-produced rollouts.  A single Codex multi-agent run
    # spawns one rollout file per subagent, each with a distinct file
    # UUID/``id`` but sharing the parent conversation's ``session_id`` and
    # first user message — so without this they surface as hundreds of
    # duplicate sidebar entries for one logical conversation.  These are
    # marked by ``thread_source == "subagent"`` (vs ``"user"``) and by a
    # dict ``source`` like ``{"subagent": {...}}`` (vs a plain string such
    # as ``"cli"``).  Blocklist, consistent with the codex_exec skip above.
    source = payload.get("source")
    if (payload.get("thread_source") == "subagent"
            or (isinstance(source, dict) and "subagent" in source)):
        return None
    session_id = payload.get("id")
    if not session_id or not isinstance(session_id, str):
        return None
    # A rollout with no usable cwd can't be mapped to a project or resumed —
    # skip it rather than falling back to root "/" and creating a bogus
    # sidebar project rooted at the filesystem root.
    cwd_str = payload.get("cwd")
    if not isinstance(cwd_str, str) or not cwd_str.strip():
        return None
    cwd = Path(cwd_str)

    # -- scan remaining lines for messages, events -------------------
    title: str | None = None
    message_count = 0
    token_total = 0
    first_user_message: str | None = None
    # Tool-call state machine: a call_id is added when its call record is seen
    # and removed when its matching output arrives, so only genuinely unpaired
    # calls remain "pending".  Calls lacking a call_id get a synthetic key so
    # they still register as pending (they can never be paired).
    pending_calls: set[str] = set()
    nocid_seq = 0
    # Last status-relevant signal, in file order, used to derive busy/idle.
    #   "user"/"task_started" -> a turn is (re)active -> busy
    #   "assistant"/turn-end lifecycle -> turn settled -> idle
    last_signal: str = ""

    for raw in f:
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue

        rtype = rec.get("type", "")
        if rtype == "response_item":
            rp = rec.get("payload")
            if not isinstance(rp, dict):
                continue
            pt = rp.get("type", "")
            if pt == "message":
                role = rp.get("role", "")
                if role == "user":
                    message_count += 1
                    last_signal = "user"
                    content = rp.get("content")
                    if isinstance(content, list):
                        text = _extract_codex_text(content)
                        if (text is not None and first_user_message is None
                                and not _is_codex_synthetic_message(text)):
                            first_user_message = text
                elif role == "assistant":
                    message_count += 1
                    last_signal = "assistant"
            elif pt in _CODEX_TOOL_CALLS:
                cid = rp.get("call_id")
                if isinstance(cid, str) and cid:
                    pending_calls.add(cid)
                else:
                    pending_calls.add(f"\0nocid{nocid_seq}")
                    nocid_seq += 1
            elif pt in _CODEX_TOOL_OUTPUTS:
                cid = rp.get("call_id")
                if isinstance(cid, str) and cid:
                    pending_calls.discard(cid)
        elif rtype == "event_msg":
            ep = rec.get("payload")
            if not isinstance(ep, dict):
                continue
            et = ep.get("type")
            if et == "token_count":
                # Direct schema: payload.info.total_token_usage is CUMULATIVE,
                # so keep the last value rather than summing across events.
                tok = _codex_cumulative_tokens(ep.get("info"))
                if tok is not None:
                    token_total = tok
            elif et == "task_started":
                last_signal = "task_started"
            elif et in _CODEX_TURN_END:
                # task_complete / turn_aborted / thread_rolled_back: the turn is
                # over and any dangling tool calls are dead — clear them so an
                # aborted/rolled-back session never reads as busy/blocked.
                last_signal = et
                pending_calls.clear()

    # -- skip empty sessions --------------------------------------------
    if message_count == 0:
        return None

    # -- file stat -------------------------------------------------------
    # A stat failure here (e.g. the file was deleted mid-scan) is transient —
    # let it propagate so _scan_codex_session returns SCAN_ERROR and the file
    # is retried, rather than being negative-cached as a deterministic skip.
    st = path.stat()
    mtime = st.st_mtime
    size_bytes = st.st_size

    # -- status ----------------------------------------------------------
    # Priority: an unpaired tool call means we're mid-tool (busy, or blocked on
    # approval once stale); otherwise the last lifecycle/message signal decides.
    pending_tool = bool(pending_calls)
    if pending_tool:
        age = time.time() - mtime
        status = "blocked" if age > _TOOL_BLOCK_AGE_S else "busy"
    elif last_signal in ("user", "task_started"):
        status = "busy"
    else:
        # "assistant", task_complete, turn_aborted, thread_rolled_back, or none.
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
    try:
        resolved = cwd.resolve()
    except OSError:
        resolved = cwd
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


def _codex_cumulative_tokens(info: object) -> int | None:
    """Return the cumulative total token count from a ``token_count`` event's
    ``info`` block, or ``None`` if it carries no usable number.

    Real schema (Codex 0.144.x)::

        event_msg.payload.info.total_token_usage.total_tokens

    ``total_tokens`` is preferred; if absent, fall back to
    ``input_tokens + output_tokens``.  Non-numeric values are ignored so a
    malformed event can't raise.
    """
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    have = False
    acc = 0
    for v in (inp, out):
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            acc += v
            have = True
    return acc if have else None


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
