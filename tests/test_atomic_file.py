"""Tests for atomic ccmgr state-file writes."""
import os

import pytest

from ccmgr.atomic_file import atomic_write_text


def test_atomic_write_creates_parent_and_replaces_content(tmp_path):
    path = tmp_path / "nested" / "state.json"

    atomic_write_text(path, "first")
    atomic_write_text(path, "second")

    assert path.read_text() == "second"


def test_atomic_write_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch,
):
    path = tmp_path / "state.json"
    path.write_text("original")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(path, "replacement")

    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]
