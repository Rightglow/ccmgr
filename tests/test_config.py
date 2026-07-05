from pathlib import Path

from ccmgr.config import Config, load_config


def test_load_with_no_file_uses_defaults(tmp_path):
    cfg = load_config(config_path=tmp_path / "does-not-exist.toml")
    assert cfg.claude_binary == "claude"
    assert cfg.poll_interval_ms == 1000
    assert cfg.live_badge_seconds == 3


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
        "[live]\npoll_interval_ms = 2000\nlive_badge_seconds = 30\n"
    )
    cfg = load_config(config_path=p)
    assert cfg.claude_binary == "/usr/local/bin/claude"
    assert cfg.poll_interval_ms == 2000
    assert cfg.live_badge_seconds == 30
