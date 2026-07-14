from pathlib import Path

from railmux.config import Config, load_config


def test_load_with_no_file_uses_defaults(tmp_path):
    cfg = load_config(config_path=tmp_path / "does-not-exist.toml")
    assert cfg.claude_binary == "claude"
    assert cfg.poll_interval_ms == 1000


def test_load_partial_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[claude]\nbinary = \"/usr/local/bin/claude\"\n")
    cfg = load_config(config_path=p)
    assert cfg.claude_binary == "/usr/local/bin/claude"
    # Untouched values stay default.
    assert cfg.poll_interval_ms == 1000


def test_load_full_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[claude]\nbinary = \"/usr/local/bin/claude\"\n"
        "[live]\npoll_interval_ms = 2000\n"
    )
    cfg = load_config(config_path=p)
    assert cfg.claude_binary == "/usr/local/bin/claude"
    assert cfg.poll_interval_ms == 2000


def test_resolved_codex_home_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(codex_home="~/.codex")
    assert cfg.resolved_codex_home() == tmp_path / ".codex"


def test_resolved_codex_home_honours_non_default(tmp_path):
    cfg = Config(codex_home=str(tmp_path / "alt-codex"))
    assert cfg.resolved_codex_home() == tmp_path / "alt-codex"


def test_resolved_codex_home_makes_relative_path_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = Config(codex_home="state/codex")
    assert cfg.resolved_codex_home() == tmp_path / "state" / "codex"
