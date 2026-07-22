from __future__ import annotations

from pathlib import Path

from railmux.help_workspace import (
    help_workspace_path,
    is_help_workspace,
    materialize_help_workspace,
)


def test_help_workspace_respects_absolute_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    expected = tmp_path / "xdg" / "railmux" / "help"
    assert help_workspace_path() == expected
    assert is_help_workspace(expected)
    assert not is_help_workspace(expected / "project")


def test_relative_xdg_data_home_falls_back_to_user_data(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert help_workspace_path() == (
        tmp_path / ".local" / "share" / "railmux" / "help")


def test_materialize_help_workspace_refreshes_reference_and_wrappers(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    root = materialize_help_workspace(
        guide="# User guide\n\nUse `railmux doctor`.", version="9.8.7")

    reference = (root / "RAILMUX_HELP.md").read_text()
    assert "Installed Railmux version: `9.8.7`" in reference
    assert "# User guide" in reference
    for name in ("AGENTS.md", "CLAUDE.md"):
        instructions = (root / name).read_text()
        assert "Read `RAILMUX_HELP.md`" in instructions
        assert "Read and search" in instructions
        assert "do not modify files" in instructions
        assert "trying to escalate" in instructions
