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
