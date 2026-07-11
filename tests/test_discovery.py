import json
from pathlib import Path

from ccmgr.discovery import list_projects


def test_list_projects_empty_when_no_claude_dir(tmp_path):
    fake_home = tmp_path / "no-claude"
    assert list_projects(fake_home) == []


def test_list_projects_empty_when_no_projects(claude_home):
    assert list_projects(claude_home) == []


def test_list_projects_returns_one(claude_home, write_session_fixture, tmp_path):
    # Make a real dir on disk so the path codec can decode unambiguously.
    real = tmp_path / "real_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(encoded, "00000000-0000-0000-0000-000000000001", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    projects = list_projects(claude_home)
    assert len(projects) == 1
    assert projects[0].real_path == real
    assert projects[0].session_count == 1
    assert projects[0].last_activity_ts > 0


def test_list_projects_sorted_by_recency(claude_home, write_session_fixture, tmp_path):
    import os
    import time

    real_a = tmp_path / "alpha"
    real_b = tmp_path / "beta"
    real_a.mkdir()
    real_b.mkdir()
    enc_a = str(real_a).replace("/", "-")
    enc_b = str(real_b).replace("/", "-")

    p_a = write_session_fixture(enc_a, "11111111-1111-1111-1111-111111111111", [{"type": "user", "message": {"role": "user", "content": "a"}}])
    time.sleep(0.05)
    p_b = write_session_fixture(enc_b, "22222222-2222-2222-2222-222222222222", [{"type": "user", "message": {"role": "user", "content": "b"}}])

    projects = list_projects(claude_home)
    assert [p.real_path for p in projects] == [real_b, real_a]


def test_list_projects_skips_missing_dir(claude_home, write_session_fixture, tmp_path):
    """Projects whose decoded directory no longer exists on disk are not listed."""
    gone = tmp_path / "deleted_project"  # deliberately never created on disk
    encoded = str(gone).replace("/", "-")
    write_session_fixture(encoded, "33333333-3333-3333-3333-333333333333", [
        {"type": "user", "message": {"role": "user", "content": "x"}},
    ])
    assert list_projects(claude_home) == []


def test_path_cache_persists_and_is_reused(claude_home, write_session_fixture, tmp_path, monkeypatch):
    """Second scan resolves via the persistent cache without calling decode()."""
    import ccmgr.discovery as discovery

    real = tmp_path / "cached_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(encoded, "44444444-4444-4444-4444-444444444444", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    # First scan populates the persistent cache.
    discovery._cache.clear()
    assert [p.real_path for p in discovery.list_projects(claude_home)] == [real]
    assert discovery._load_path_cache().get(encoded) == str(real)

    # Second scan (in-process cache cleared) must NOT call decode — it should
    # resolve straight from the persistent cache.
    discovery._cache.clear()
    monkeypatch.setattr(discovery, "decode", lambda name: (_ for _ in ()).throw(
        AssertionError("decode() should not be called on a cache hit")))
    assert [p.real_path for p in discovery.list_projects(claude_home)] == [real]


def test_path_cache_prunes_vanished_projects(claude_home, write_session_fixture, tmp_path):
    """Cache entries for projects whose dir disappeared are pruned on rescan."""
    import shutil
    import ccmgr.discovery as discovery

    real = tmp_path / "temp_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    proj_entry = write_session_fixture(encoded, "55555555-5555-5555-5555-555555555555", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
    ])

    discovery._cache.clear()
    discovery.list_projects(claude_home)
    assert encoded in discovery._load_path_cache()

    # Remove the real project dir AND its .claude/projects entry, then rescan.
    real.rmdir()
    shutil.rmtree(proj_entry.parent)
    discovery._cache.clear()
    assert discovery.list_projects(claude_home) == []
    assert encoded not in discovery._load_path_cache()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_list_projects_excludes_bg_sessions(claude_home, write_session_fixture, tmp_path):
    """session_count must not include bg sessions."""
    real = tmp_path / "mixed_project"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    # Two normal sessions and one bg session.
    write_session_fixture(encoded, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
        {"type": "user", "message": {"role": "user", "content": "normal A"}},
    ])
    write_session_fixture(encoded, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        {"type": "user", "message": {"role": "user", "content": "normal B"}},
    ])
    bg_dir = claude_home / "projects" / encoded
    bg_path = bg_dir / "cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl"
    _write_jsonl(bg_path, [
        {"type": "user", "message": {"role": "user", "content": "bg job"}, "sessionKind": "bg"},
    ])

    projects = list_projects(claude_home)
    assert len(projects) == 1
    assert projects[0].session_count == 2  # only the two normal sessions
