from pathlib import Path

import pytest

from railmux.path_codec import encode, decode


def test_encode_simple():
    assert encode(Path("/home/user/project")) == "-home-user-project"


def test_encode_preserves_dashes_in_segments():
    assert encode(Path("/home/user/claude-chat")) == "-home-user-claude-chat"


def test_encode_trailing_slash_stripped():
    assert encode(Path("/home/user/project/")) == "-home-user-project"


def test_decode_unambiguous_with_filesystem(tmp_path):
    real = tmp_path / "foo" / "bar"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_with_dashes_in_segment(tmp_path):
    real = tmp_path / "claude-chat"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_nonexistent_splits_every_dash(tmp_path, monkeypatch):
    # Pick a token unlikely to exist as a real top-level dir.
    encoded = "-zzz-foo-bar"
    result = decode(encoded)
    assert result == Path("/zzz/foo/bar"), result


def test_decode_with_dashes_in_intermediate_segment(tmp_path):
    real = tmp_path / "claude-chat" / "src"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_two_dashed_segments(tmp_path):
    real = tmp_path / "foo-bar" / "baz-qux"
    real.mkdir(parents=True)
    encoded = encode(real)
    assert decode(encoded) == real


def test_decode_non_ascii_recovery(tmp_path):
    """Chinese characters replaced by dashes during Claude encoding are recovered."""
    real = tmp_path / "项目" / "src"
    real.mkdir(parents=True)
    # Claude's encoding replaces non-ASCII chars with dashes.
    # Simulate what Claude would write to ~/.claude/projects/.
    from railmux.path_codec import _claude_encode_path
    encoded = _claude_encode_path(str(real))
    # encoded should contain more dashes than the ASCII-only fallback.
    assert decode(encoded) == real


def test_decode_non_ascii_nested(tmp_path):
    """Recovery works through multiple levels of non-ASCII directories."""
    real = tmp_path / "数据" / "无尽夏" / "CatWork"
    real.mkdir(parents=True)
    from railmux.path_codec import _claude_encode_path
    encoded = _claude_encode_path(str(real))
    assert decode(encoded) == real


def test_decode_non_ascii_fallback_when_no_match(tmp_path):
    """When filesystem scan finds nothing, return the best-guess path without crashing."""
    from railmux.path_codec import _claude_encode_path
    encoded = _claude_encode_path("/zzz/你好/世界")
    result = decode(encoded)
    # Falls back to best-guess from backtracking (all dashes as separators).
    assert result is not None
    assert not result.exists()  # path doesn't exist, but decode didn't crash


def test_claude_encode_collapses_dot_and_underscore():
    """Claude Code encodes '.', '_' and '/' all to '-' (lossy for those too)."""
    from railmux.path_codec import _claude_encode_path
    assert (
        _claude_encode_path("/home/scratch.user_inf/workspace")
        == "-home-scratch-user-inf-workspace"
    )


def test_decode_recovers_dot_and_underscore(tmp_path):
    """A segment with '.' and '_' (both encoded to '-') is recovered via fs scan."""
    from railmux.path_codec import _claude_encode_path
    real = tmp_path / "scratch.user_name" / "workspace"
    real.mkdir(parents=True)
    encoded = _claude_encode_path(str(real))
    assert "." not in encoded and "_" not in encoded  # lossy: collapsed to '-'
    assert decode(encoded) == real



# --- Differential test: the pruned backtracker must match the original one ---

def _reference_decode(encoded: str):
    """The original (un-pruned) decode algorithm, kept here as an oracle so we
    can prove the optimized decode() in path_codec returns identical results."""
    from pathlib import Path
    from railmux import path_codec as pc

    if not encoded.startswith("-"):
        raise ValueError(encoded)
    tokens = encoded[1:].split("-")
    if not tokens or tokens == [""]:
        return Path("/")
    n = len(tokens)
    best_path = None
    best_score = (-1, -1)

    def consider(segments):
        nonlocal best_path, best_score
        depth = 0
        q = Path("/")
        for seg in segments:
            q = q / seg
            if q.exists():
                depth += 1
            else:
                break
        s = (depth, len(segments))
        if s > best_score:
            best_score = s
            best_path = Path("/" + "/".join(segments))

    def backtrack(idx, segments, confirmed_depth):
        if idx == n:
            consider(segments)
            return
        if confirmed_depth + 1 + (n - idx) < best_score[0]:
            return
        tok = tokens[idx]
        if segments:
            current_leaf = Path("/" + "/".join(segments))
            new_confirmed = confirmed_depth + (1 if current_leaf.exists() else 0)
        else:
            new_confirmed = 0
        backtrack(idx + 1, segments + [tok], new_confirmed)
        if segments:
            extended = segments[:-1] + [segments[-1] + "-" + tok]
            backtrack(idx + 1, extended, confirmed_depth)

    backtrack(0, [], 0)
    if best_path.is_dir() or best_path.exists():
        return best_path
    return pc._scan_recover(encoded, best_path)


@pytest.fixture
def clean_root():
    """A root dir whose absolute path has NO '-', '.' or '_' so the oracle's
    exponential backtracker resolves the prefix cheaply; only the tricky tree
    under it should cost anything. (pytest's tmp_path contains dashes, which
    would make the un-pruned oracle take minutes.)"""
    import os
    import shutil
    root = Path("/tmp") / f"railmuxdiff{os.getpid()}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _build_tricky_tree(root: Path):
    """A filesystem with dashes, dots, underscores, ambiguous siblings and
    non-ASCII names — the cases where segmentation is genuinely ambiguous."""
    dirs = [
        "s.u_v/w/T",
        "s.u_v/w/T-L",
        "s.u_v/w/T-L-2",
        "s.u_v/a/b",       # ambiguous with a-b below
        "s.u_v/a-b",
        "m/f/g",           # ambiguous with f-g below
        "m/f-g",
        "d.x_y/项目/src",  # non-ASCII segment
    ]
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)


def test_decode_matches_reference_over_tricky_tree(clean_root):
    """Optimized decode() == original algorithm for every dir in a tricky tree."""
    from railmux.path_codec import decode, _claude_encode_path
    _build_tricky_tree(clean_root)

    checked = 0
    for sub in sorted(p for p in clean_root.rglob("*") if p.is_dir()):
        encoded = _claude_encode_path(str(sub))
        decode.cache_clear()
        got = decode(encoded)
        ref = _reference_decode(encoded)
        assert got == ref, f"mismatch for {sub}: got {got}, ref {ref}"
        assert got.is_dir(), f"{encoded} did not resolve to a real dir: {got}"
        checked += 1
    assert checked >= 8


def test_decode_matches_reference_for_missing_paths(clean_root):
    """Match the oracle even when the path (or its tail) doesn't exist on disk."""
    from railmux.path_codec import decode, _claude_encode_path
    _build_tricky_tree(clean_root)
    base = _claude_encode_path(str(clean_root / "s.u_v" / "w"))
    cases = [
        base + "-ghost-child",             # existing prefix, missing tail
        base + "-T-L-9-z_z",               # partially matching, then missing
        _claude_encode_path(str(clean_root)) + "-no_pe.nope-deep",
        "-completely-bogus_path.that-is-absent",
    ]
    for encoded in cases:
        decode.cache_clear()
        got = decode(encoded)
        ref = _reference_decode(encoded)
        assert got == ref, f"mismatch for {encoded!r}: got {got}, ref {ref}"
