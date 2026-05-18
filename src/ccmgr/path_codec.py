"""Encode/decode Claude's ~/.claude/projects/ directory names.

Claude encodes a project path like /home/user/foo into the directory name
-home-user-foo. Decoding is ambiguous because dashes inside the original path
collide with slash-replacements; we resolve by checking filesystem existence.
"""
from __future__ import annotations

import functools
from pathlib import Path


def encode(path: Path) -> str:
    """Encode an absolute filesystem path to Claude's project dir-name form."""
    abs_path = path.resolve()
    s = str(abs_path).rstrip("/")
    return s.replace("/", "-")


def _verified_depth(segments: list[str]) -> int:
    """Count how many leading path components exist on disk."""
    p = Path("/")
    depth = 0
    for seg in segments:
        p = p / seg
        if p.exists():
            depth += 1
        else:
            break
    return depth


@functools.lru_cache(maxsize=512)
def decode(encoded: str) -> Path:
    """Decode a Claude project dir name back to an absolute filesystem path.

    Strategy: the encoded string is dash-separated tokens, where each dash was
    originally either a '/' (segment boundary) or a literal '-' inside a
    segment. We use backtracking over all possible segmentations, scoring each
    candidate by how many leading path components actually exist on disk.
    The candidate with the deepest verified prefix wins; ties broken by
    most total segments (treat every dash as a slash).

    If nothing exists on disk, fall back to treating every dash as a slash.
    """
    if not encoded.startswith("-"):
        raise ValueError(f"encoded name must start with '-': {encoded!r}")

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
            leaf_exists = current_leaf.exists()
            new_confirmed = confirmed_depth + (1 if leaf_exists else 0)
        else:
            new_confirmed = 0
        backtrack(idx + 1, segments + [tok], new_confirmed)

        # Branch B: dash before tok is a literal '-' -> extend the last segment.
        if segments:
            extended = segments[:-1] + [segments[-1] + "-" + tok]
            backtrack(idx + 1, extended, confirmed_depth)

    backtrack(0, [], 0)
    assert best_path is not None
    return best_path
