
import pytest

from railmux.config import Config, ConfigError, load_config


def test_load_with_no_file_uses_defaults(tmp_path):
    cfg = load_config(config_path=tmp_path / "does-not-exist.toml")
    assert cfg.claude_binary == "claude"
    assert cfg.poll_interval_ms == 1000
    assert cfg.agent_transport == "swap"
    assert cfg.show_empty_projects is False
    assert cfg.ssh_history_lines == 5000


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
        "[projects]\nshow_empty_projects = true\n"
    )
    cfg = load_config(config_path=p)
    assert cfg.claude_binary == "/usr/local/bin/claude"
    assert cfg.poll_interval_ms == 2000
    assert cfg.show_empty_projects is True


def test_show_empty_projects_non_boolean_fails_closed(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[projects]\nshow_empty_projects = "yes"\n')
    assert load_config(config_path=p).show_empty_projects is False


def test_malformed_toml_raises_safe_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[claude\nbinary = 'secret'")

    with pytest.raises(ConfigError, match="invalid TOML") as error:
        load_config(config_path=path)

    assert "secret" not in str(error.value)


@pytest.mark.parametrize("value", ['"nope"', "0", "-1", "true"])
def test_invalid_poll_interval_is_rejected(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[live]\npoll_interval_ms = {value}\n")

    with pytest.raises(ConfigError, match="positive integer"):
        load_config(config_path=path)


def test_nested_transport_can_be_selected_explicitly(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[live]\nagent_transport = "nested"\n')
    assert load_config(config_path=path).agent_transport == "nested"


def test_invalid_agent_transport_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[live]\nagent_transport = "teleport"\n')
    with pytest.raises(ConfigError, match="live.agent_transport"):
        load_config(config_path=path)


@pytest.mark.parametrize("value", (2000, 5000, 20000))
def test_ssh_history_limit_accepts_the_documented_range(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f"[ssh]\nhistory_lines = {value}\n")

    assert load_config(config_path=path).ssh_history_lines == value


@pytest.mark.parametrize("value", ("1999", "20001", "true", '"5000"'))
def test_ssh_history_limit_rejects_values_outside_its_integer_range(
    tmp_path, value,
):
    path = tmp_path / "config.toml"
    path.write_text(f"[ssh]\nhistory_lines = {value}\n")

    with pytest.raises(ConfigError, match="ssh.history_lines"):
        load_config(config_path=path)


def test_provider_binary_must_be_a_non_empty_string(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[codex]\nbinary = ''\n")

    with pytest.raises(ConfigError, match="codex.binary"):
        load_config(config_path=path)


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
