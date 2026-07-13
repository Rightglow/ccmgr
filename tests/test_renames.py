"""Tests for ccmgr.renames — the user-rename sidecar store."""
from __future__ import annotations

import json

import pytest

from ccmgr.renames import Renames


@pytest.fixture
def renames(tmp_path, monkeypatch):
    """A Renames store backed by a throwaway JSON file."""
    path = tmp_path / "renames.json"
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    return Renames(), path


def test_set_get_round_trip(renames):
    store, _ = renames
    assert store.get("sid-1") is None
    store.set("sid-1", "My Session")
    assert store.get("sid-1") == "My Session"


def test_set_persists_to_disk(renames):
    store, path = renames
    store.set("sid-1", "中文标题")
    # A fresh store reads the same value back (and Chinese stays unescaped).
    assert "中文标题" in path.read_text()
    assert Renames().get("sid-1") == "中文标题"


def test_set_strips_and_empty_clears(renames):
    store, _ = renames
    store.set("sid-1", "  Trimmed  ")
    assert store.get("sid-1") == "Trimmed"
    store.set("sid-1", "   ")  # whitespace-only → clear
    assert store.get("sid-1") is None


def test_clear_removes_and_persists(renames):
    store, path = renames
    store.set("sid-1", "Name")
    store.clear("sid-1")
    assert store.get("sid-1") is None
    assert Renames().get("sid-1") is None
    # Clearing a missing key is a no-op, not an error.
    store.clear("never-existed")


def test_load_ignores_malformed_file(tmp_path, monkeypatch):
    path = tmp_path / "renames.json"
    path.write_text("{ this is not json")
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    store = Renames()  # must not raise
    assert store.get("anything") is None


def test_load_skips_non_string_values(tmp_path, monkeypatch):
    path = tmp_path / "renames.json"
    path.write_text(json.dumps({"good": "Title", "bad": 42, "empty": ""}))
    monkeypatch.setattr("ccmgr.renames._renames_path", lambda: path)
    store = Renames()
    assert store.get("good") == "Title"
    assert store.get("bad") is None
    assert store.get("empty") is None


def test_codex_rename_does_not_pollute_rollout_file(tmp_path, monkeypatch):
    """A Codex session rename writes only the sidecar; the rollout file
    (jsonl_path) must not be appended to — it's a different schema owned by
    the Codex CLI."""
    from unittest.mock import MagicMock

    from ccmgr.models import SessionMeta, Project
    from ccmgr.renames import Renames
    from ccmgr.ui.app import App

    # Build a minimal App with the Renames sidecar wired in.
    app = App.__new__(App)
    app._renames = Renames()
    app._session_cache = MagicMock()
    app._codex_index = MagicMock()
    app._invalidate_project_snapshot = lambda: None
    app._refresh = lambda: None
    app._close_modal = lambda: None
    st = [None]
    app._set_status = lambda msg: st.__setitem__(0, msg)

    # A Codex session's rollout file — must stay untouched.
    rollout = tmp_path / "rollout-codex.jsonl"
    rollout.write_text('{"existing":"content"}\n')

    fake_claude_home = tmp_path / ".claude"
    fake_claude_home.mkdir()
    proj = Project(real_path=tmp_path / "fake",
                   encoded_name="fake",
                   claude_dir=fake_claude_home,
                   session_count=1,
                   last_activity_ts=0.0)
    # SessionMeta with session_type="codex" — the key discriminator.
    session = SessionMeta(
        project=proj,
        session_id="sid-codex",
        jsonl_path=rollout,
        title="Old Title",
        message_count=1,
        token_total=10,
        last_mtime=0.0,
        session_type="codex",
    )

    # Capture mtime before rename.
    mtime_before = rollout.stat().st_mtime
    size_before = rollout.stat().st_size

    app._do_rename(session, "New Codex Name")

    # Sidecar must hold the new title.
    assert app._renames.get("sid-codex") == "New Codex Name"
    # Rollout file untouched.
    assert rollout.stat().st_mtime == mtime_before
    assert rollout.stat().st_size == size_before
    assert rollout.read_text() == '{"existing":"content"}\n'
    # Status message reflects success (not a sync failure).
    assert st[0] == "Renamed to: New Codex Name"


def test_claude_rename_still_writes_jsonl_echo(tmp_path):
    """Claude session rename still appends the ai-title to the JSONL (regression)."""
    from unittest.mock import MagicMock

    from ccmgr.models import SessionMeta, Project
    from ccmgr.renames import Renames
    from ccmgr.ui.app import App

    app = App.__new__(App)
    app._renames = Renames()
    app._session_cache = MagicMock()
    app._codex_index = MagicMock()
    app._invalidate_project_snapshot = lambda: None
    app._refresh = lambda: None
    app._set_status = lambda msg: None
    app._close_modal = lambda: None

    jsonl = tmp_path / "session-claude.jsonl"
    jsonl.write_text('{"existing":"content"}\n')

    fake_claude_home = tmp_path / ".claude"
    fake_claude_home.mkdir()
    proj = Project(real_path=tmp_path / "fake",
                   encoded_name="fake",
                   claude_dir=fake_claude_home,
                   session_count=1,
                   last_activity_ts=0.0)
    session = SessionMeta(
        project=proj,
        session_id="sid-claude",
        jsonl_path=jsonl,
        title="Old Title",
        message_count=1,
        token_total=10,
        last_mtime=0.0,
        session_type="claude",
    )

    app._do_rename(session, "New Claude Name")

    assert app._renames.get("sid-claude") == "New Claude Name"
    content = jsonl.read_text()
    assert '{"existing":"content"}' in content
    assert '\"aiTitle\": \"New Claude Name\"' in content
