from pathlib import Path

from ccmgr.config import Config, load_config


def test_load_with_no_file_uses_defaults(tmp_path):
    cfg = load_config(config_path=tmp_path / "does-not-exist.toml")
    assert cfg.leave_focus_key == "ctrl a"
    assert cfg.max_concurrent_sessions == 10
    assert cfg.claude_binary == "claude"
    assert cfg.poll_interval_ms == 1000
    assert cfg.live_badge_seconds == 3


def test_load_partial_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[keys]\nleave_focus = \"ctrl space\"\n"
        "[limits]\nmax_concurrent_sessions = 10\n"
    )
    cfg = load_config(config_path=p)
    assert cfg.leave_focus_key == "ctrl space"
    assert cfg.max_concurrent_sessions == 10
    # Untouched values stay default.
    assert cfg.claude_binary == "claude"


def test_load_full_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[keys]\nleave_focus = \"ctrl b\"\n"
        "[limits]\nmax_concurrent_sessions = 3\nscrollback_lines = 8000\n"
        "[claude]\nbinary = \"/usr/local/bin/claude\"\n"
        "[live]\npoll_interval_ms = 2000\nlive_badge_seconds = 30\n"
    )
    cfg = load_config(config_path=p)
    assert cfg.leave_focus_key == "ctrl b"
    assert cfg.max_concurrent_sessions == 3
    assert cfg.claude_binary == "/usr/local/bin/claude"
    assert cfg.poll_interval_ms == 2000
    assert cfg.live_badge_seconds == 30
