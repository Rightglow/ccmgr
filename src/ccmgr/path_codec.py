"""Encode/decode Claude's ~/.claude/projects/ directory names.

Claude encodes a project path like /home/user/foo into the directory name
-home-user-foo. Non-ASCII characters (Chinese, emoji, etc.) are replaced
with '-' during encoding, making the mapping lossy. Decoding resolves
ambiguity by checking filesystem existence and, when that fails, scanning
directory listings to recover segments with non-ASCII characters.
"""
from __future__ import annotations

import functools
from pathlib import Path


# Per-decode() filesystem memoization. Decoding a single name re-checks the
# same prefixes hundreds of times (e.g. the backtracker stats '/home' once per
# candidate segmentation). On a local disk that's cheap, but on an NFS home
# each stat is a network round-trip, turning one decode into seconds. These
# module-level dicts dedupe stat/iterdir within a single decode() call; decode()
# clears them on entry so results never go stale across calls.
_exists_memo: dict[str, bool] = {}
_is_dir_memo: dict[str, bool] = {}
_iterdir_memo: dict[str, list[Path]] = {}


def _m_exists(p: Path) -> bool:
    k = str(p)
    if k not in _exists_memo:
        _exists_memo[k] = p.exists()
    return _exists_memo[k]


def _m_is_dir(p: Path) -> bool:
    k = str(p)
    if k not in _is_dir_memo:
        _is_dir_memo[k] = p.is_dir()
    return _is_dir_memo[k]


def _m_iterdir(p: Path) -> list[Path]:
    k = str(p)
    if k not in _iterdir_memo:
        _iterdir_memo[k] = list(p.iterdir())
    return _iterdir_memo[k]


def encode(path: Path) -> str:
    """Encode an absolute filesystem path to Claude's project dir-name form."""
    abs_path = path.resolve()
    s = str(abs_path).rstrip("/")
    return s.replace("/", "-")


def _claude_encode_path(path: str) -> str:
    """Simulate Claude Code's path-to-directory-name encoding.

    '/'  -> '-'
    '.'  -> '-'
    '_'  -> '-'
    Non-ASCII characters -> '-'
    ASCII alphanumeric and '-' pass through unchanged.

    Note: Claude Code collapses '.', '_' and '/' all to '-', so the encoding
    is lossy for those characters too (not just non-ASCII). Decoding relies on
    filesystem scanning to recover the originals.
    """
    result = []
    for ch in path:
        if ord(ch) < 128 and (ch.isalnum() or ch == "-"):
            result.append(ch)
        else:
            result.append("-")
    return "".join(result)


def _verified_depth(segments: list[str]) -> int:
    """Count how many leading path components exist on disk."""
    p = Path("/")
    depth = 0
    for seg in segments:
        p = p / seg
        if _m_exists(p):
            depth += 1
        else:
            break
    return depth


def _scan_recover(encoded: str, best_path: Path) -> Path:
    """Filesystem-assisted recovery for paths with non-ASCII characters.

    When Claude replaces non-ASCII chars with dashes, the backtracking
    decoder cannot recover the original characters. This function walks
    the directory tree starting from the deepest verified prefix of
    *best_path*, matching encoded child directory names against the
    remaining portion of *encoded*.
    """
    parts = list(best_path.parts)  # ['/', 'mnt', 'c', ..., '1', 'CatWork']

    # Find the deepest prefix that actually exists on disk.
    for split_idx in range(len(parts) - 1, 0, -1):
        prefix = Path(*parts[: split_idx + 1])
        if not _m_is_dir(prefix):
            continue

        # Encode the prefix so we can locate where it ends in *encoded*.
        prefix_str = str(prefix)
        prefix_encoded = _claude_encode_path(prefix_str)

        if not encoded.startswith(prefix_encoded):
            continue

        # Everything after the known-good prefix is ambiguous — resolve it
        # by scanning directory children.
        tail = encoded[len(prefix_encoded):]  # e.g. "-----CatWork"
        resolved = _resolve_tail(prefix, tail)
        if resolved is not None:
            return resolved

        # Couldn't resolve past this prefix; fall through to best_path.
        break

    return best_path


def _resolve_tail(base: Path, tail: str) -> Path | None:
    """Recursively resolve an encoded tail string under *base*.

    *tail* is the portion of the encoded project name after the
    deepest verified prefix (e.g. ``"-----CatWork"``).  Each leading
    dash may be a path separator **or** a non-ASCII character
    replacement, so we cannot simply strip them.  Instead we iterate
    through directory children, encode each child name, and try to
    match it against *tail* with an optional single separator dash
    before it.
    """
    if not tail:
        return base

    try:
        children = sorted(_m_iterdir(base), key=lambda p: p.name)
    except OSError:
        return None

    for child in children:
        # Filter by name first (a free string compare) and only pay for the
        # is_dir() stat on children that could actually match — this avoids a
        # stat per entry in large directories, which matters on NFS.
        child_encoded = _claude_encode_path(child.name)
        if not (tail == child_encoded
                or tail.startswith("-" + child_encoded)
                or tail.startswith(child_encoded + "-")):
            continue
        if not _m_is_dir(child):
            continue

        # Case 1: "-" + child_encoded  (a single separator dash followed
        #          by the child, the normal case).
        if tail.startswith("-" + child_encoded):
            remaining = tail[1 + len(child_encoded):]
            if not remaining:
                return child
            sub = _resolve_tail(child, remaining)
            if sub is not None:
                return sub
            # This child matched but deeper resolution failed;
            # keep trying other children (e.g. "a" vs "ab" prefix
            # ambiguity).  Fall through to Case 2 / next child.

        # Case 2: child_encoded with no separator (first segment after
        #         the prefix, or consecutive dashes collapsed).
        if tail == child_encoded:
            return child
        if tail.startswith(child_encoded + "-"):
            remaining = tail[len(child_encoded):]  # keeps leading "-"
            sub = _resolve_tail(child, remaining)
            if sub is not None:
                return sub

    return None


@functools.lru_cache(maxsize=512)
def decode(encoded: str) -> Path:
    """Decode a Claude project dir name back to an absolute filesystem path.

    Strategy: the encoded string is dash-separated tokens, where each dash
    was originally either a ``/`` (segment boundary), a literal ``-`` inside
    a segment, or a non-ASCII character replacement.  We use backtracking
    over all possible segmentations, scoring each candidate by how many
    leading path components actually exist on disk.  The candidate with the
    deepest verified prefix wins; ties are broken by most total segments
    (treating every dash as a slash).

    If the winning candidate does **not** exist on disk we fall back to a
    filesystem scan — see ``_scan_recover`` — to recover segments whose
    non-ASCII characters were replaced by dashes during encoding.
    """
    if not encoded.startswith("-"):
        raise ValueError(f"encoded name must start with '-': {encoded!r}")

    # Fresh filesystem view for this decode; dedupes the many repeated stats
    # the backtracker/scanner make on the same prefixes (see memo helpers).
    _exists_memo.clear()
    _is_dir_memo.clear()
    _iterdir_memo.clear()

    tokens = encoded[1:].split("-")
    if not tokens or tokens == [""]:
        return Path("/")

    n = len(tokens)
    best_path: Path | None = None
    best_score: tuple[int, int] = (-1, -1)

    def consider(segments: list[str]) -> None:
        nonlocal best_path, best_score
        depth = _verified_depth(segments)
        s = (depth, len(segments))
        if s > best_score:
            best_score = s
            best_path = Path("/" + "/".join(segments))

    def backtrack(idx: int, segments: list[str], confirmed_depth: int) -> None:
        """
        idx             -- next token index to process
        segments        -- segments committed so far
        confirmed_depth -- number of segments[0..k-1] verified to exist,
                           where k is the number of COMPLETE segments
                           (all but the last, which may still be extended).
                           Used only for upper-bound pruning.
        """
        if idx == n:
            consider(segments)
            return

        # Upper-bound pruning: best possible depth from here is
        # confirmed_depth + 1 (for the current last segment, if it exists)
        # + (n - idx) more new segments that all exist.
        max_possible_depth = confirmed_depth + 1 + (n - idx)
        if max_possible_depth < best_score[0]:
            return

        tok = tokens[idx]

        # Branch A: dash before tok is a '/' separator -> start a new segment.
        # First, lock in the current last segment: check if it exists.
        if segments:
            current_leaf = Path("/" + "/".join(segments))
            if _m_exists(current_leaf):
                backtrack(idx + 1, segments + [tok], confirmed_depth + 1)
            else:
                # current_leaf doesn't exist, so no path below it can exist
                # either. Every completion that starts a new segment here shares
                # the same (frozen) verified depth, so the best-scoring one just
                # maximizes segment count — split all remaining tokens as
                # separate segments. Emit that single candidate and skip the
                # whole 2^(remaining) doomed subtree.
                consider(segments + tokens[idx:])
        else:
            backtrack(idx + 1, [tok], 0)

        # Branch B: dash before tok is a literal '-' -> extend the last segment.
        # Always explored: an extension may exist even when the shorter prefix
        # does not (e.g. 'foo' absent but 'foo-bar' present).
        if segments:
            extended = segments[:-1] + [segments[-1] + "-" + tok]
            backtrack(idx + 1, extended, confirmed_depth)

    backtrack(0, [], 0)
    assert best_path is not None

    if _m_is_dir(best_path) or _m_exists(best_path):
        return best_path

    # Non-ASCII recovery: characters like Chinese were replaced by dashes
    # during Claude's encoding.  Scan the filesystem to find the real path.
    return _scan_recover(encoded, best_path)
