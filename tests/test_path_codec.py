from pathlib import Path

import pytest

from ccmgr.path_codec import encode, decode


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
