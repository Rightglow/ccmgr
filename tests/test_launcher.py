import os
from pathlib import Path

import pytest

from ccmgr.launcher import build_resume_command, build_new_session_command, launch


def test_build_resume_command():
    cmd = build_resume_command(
        claude_binary="claude",
        session_id="abc-123",
        cwd=Path("/home/me/proj"),
    )
    assert cmd == ["claude", "--resume", "abc-123"]


def test_build_new_session_command_for_existing_project():
    cmd = build_new_session_command(
        claude_binary="claude",
        cwd=Path("/home/me/proj"),
    )
    assert cmd == ["claude"]


def test_launch_runs_subprocess_with_cwd_and_returns_exit_code(tmp_path):
    # Use /bin/sh as a stand-in for claude that we can fully control.
    target = tmp_path / "where_am_i"
    rc = launch(
        cmd=["/bin/sh", "-c", "pwd > where_am_i; exit 7"],
        cwd=tmp_path,
    )
    assert rc == 7
    assert target.read_text().strip() == str(tmp_path.resolve())


def test_launch_creates_cwd_if_missing(tmp_path):
    new_dir = tmp_path / "fresh"
    assert not new_dir.exists()
    rc = launch(
        cmd=["/bin/sh", "-c", "exit 0"],
        cwd=new_dir,
        create_cwd=True,
    )
    assert rc == 0
    assert new_dir.is_dir()


def test_launch_raises_when_cwd_missing_and_no_create(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        launch(
            cmd=["/bin/sh", "-c", "exit 0"],
            cwd=missing,
            create_cwd=False,
        )
