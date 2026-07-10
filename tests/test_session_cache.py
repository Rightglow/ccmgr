import os
import time
from pathlib import Path

import pytest

from ccmgr.discovery import list_projects
from ccmgr.session_cache import SessionCache


def _make_project(claude_home, tmp_path, write_session_fixture, sessions,
                  name="proj"):
    real = tmp_path / name
    real.mkdir()
    encoded = str(real).replace("/", "-")
    for sid, records in sessions:
        write_session_fixture(encoded, sid, records)
    return list_projects(claude_home)[0]


def test_cache_returns_same_metadata(claude_home, write_session_fixture, tmp_path):
    project = _make_project(claude_home, tmp_path, write_session_fixture, [
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "ai-title", "aiTitle": "Hello"},
        ]),
    ])
    cache = SessionCache()
    first = cache.list_sessions(project)
    second = cache.list_sessions(project)
    assert [s.session_id for s in first] == [s.session_id for s in second]
    assert first[0].title == second[0].title


def test_cache_skips_reparse_when_mtime_unchanged(claude_home, write_session_fixture, tmp_path, monkeypatch):
    project = _make_project(claude_home, tmp_path, write_session_fixture, [
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
            {"type": "user", "message": {"role": "user", "content": "x"}},
        ]),
    ])
    cache = SessionCache()
    cache.list_sessions(project)  # populate

    import builtins
    real_open = builtins.open
    opens: list[str] = []
    def spy_open(path, *args, **kwargs):
        opens.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", spy_open)

    cache.list_sessions(project)

    jsonl_opens = [p for p in opens if p.endswith(".jsonl")]
    assert jsonl_opens == [], f"expected zero JSONL re-reads, got: {jsonl_opens}"


def test_cache_reparses_when_mtime_changes(claude_home, write_session_fixture, tmp_path):
    project = _make_project(claude_home, tmp_path, write_session_fixture, [
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", [
            {"type": "user", "message": {"role": "user", "content": "v1"}},
        ]),
    ])
    cache = SessionCache()
    first = cache.list_sessions(project)
    # 1 user record + 1 auto-injected assistant record (see conftest.py).
    assert first[0].message_count == 2

    jsonl = first[0].jsonl_path
    with jsonl.open("a") as f:
        f.write('{"type": "user", "message": {"role": "user", "content": "v2"}}\n')
    new_mtime = time.time() + 1
    os.utime(jsonl, (new_mtime, new_mtime))

    second = cache.list_sessions(project)
    assert second[0].message_count == 3


def test_cache_drops_entries_for_deleted_sessions(claude_home, write_session_fixture, tmp_path):
    project = _make_project(claude_home, tmp_path, write_session_fixture, [
        ("dddddddd-dddd-dddd-dddd-dddddddddddd", [{"type": "user", "message": {"role": "user", "content": "x"}}]),
        ("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", [{"type": "user", "message": {"role": "user", "content": "y"}}]),
    ])
    cache = SessionCache()
    first = cache.list_sessions(project)
    assert len(first) == 2

    first[0].jsonl_path.unlink()

    second = cache.list_sessions(project)
    assert len(second) == 1


def test_cache_keeps_other_projects_warm(
    claude_home, write_session_fixture, tmp_path, monkeypatch,
):
    first_project = _make_project(
        claude_home, tmp_path, write_session_fixture,
        [(
            "11111111-1111-1111-1111-111111111111",
            [{"type": "user", "message": {"role": "user", "content": "one"}}],
        )],
        name="one",
    )
    second_project = _make_project(
        claude_home, tmp_path, write_session_fixture,
        [(
            "22222222-2222-2222-2222-222222222222",
            [{"type": "user", "message": {"role": "user", "content": "two"}}],
        )],
        name="two",
    )
    cache = SessionCache()
    cache.list_sessions(first_project)
    cache.list_sessions(second_project)

    monkeypatch.setattr(
        "ccmgr.session_cache._scan_session",
        lambda *_args: pytest.fail("other project cache was evicted"),
    )

    assert len(cache.list_sessions(first_project)) == 1
    assert len(cache.list_sessions(second_project)) == 1


def test_append_during_scan_forces_next_poll_rescan(
    claude_home, write_session_fixture, tmp_path, monkeypatch,
):
    project = _make_project(
        claude_home, tmp_path, write_session_fixture,
        [(
            "33333333-3333-3333-3333-333333333333",
            [{"type": "user", "message": {"role": "user", "content": "initial"}}],
        )],
    )
    import ccmgr.session_cache as cache_module
    real_scan = cache_module._scan_session
    calls = []

    def scan_then_append(project, path):
        meta = real_scan(project, path)
        calls.append(path)
        if len(calls) == 1:
            with path.open("a") as stream:
                stream.write('{"type":"ai-title","aiTitle":"Late title"}\n')
        return meta

    monkeypatch.setattr(cache_module, "_scan_session", scan_then_append)
    cache = SessionCache()

    assert cache.list_sessions(project)[0].title != "Late title"
    assert cache.list_sessions(project)[0].title == "Late title"
    assert len(calls) == 2
