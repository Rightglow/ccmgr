import os
import time
from pathlib import Path

from ccmgr.discovery import list_projects
from ccmgr.session_cache import SessionCache


def _make_project(claude_home, tmp_path, write_session_fixture, sessions):
    real = tmp_path / "proj"
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
    assert first[0].message_count == 1

    jsonl = first[0].jsonl_path
    with jsonl.open("a") as f:
        f.write('{"type": "user", "message": {"role": "user", "content": "v2"}}\n')
    new_mtime = time.time() + 1
    os.utime(jsonl, (new_mtime, new_mtime))

    second = cache.list_sessions(project)
    assert second[0].message_count == 2


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
