from unittest.mock import MagicMock, patch

import pytest

from railmux.cli import _show_startup_message, is_ssh_session, main
from railmux.config import ConfigError


@pytest.fixture(autouse=True)
def tmux_preflight_succeeds(monkeypatch):
    """CLI behaviour tests must not depend on the host's tmux installation."""
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", lambda: True)


def test_no_scroll_coalescing_flag_reaches_app(tmp_path):
    with patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--no-scroll-coalescing",
        ])

    assert result == 0
    app_cls.assert_called_once()
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False
    app_cls.return_value.run.assert_called_once()


def test_scroll_coalescing_is_enabled_automatically_over_ssh(tmp_path):
    with patch.dict("os.environ", {"SSH_CONNECTION": "client 1 server 2"}), \
         patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_scroll_coalescing_is_disabled_automatically_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False


def test_force_enable_scroll_coalescing_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--scroll-coalescing",
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_is_ssh_session_recognizes_common_markers():
    assert is_ssh_session({"SSH_CONNECTION": "client 1 server 2"})
    assert is_ssh_session({"SSH_CLIENT": "client 1 2"})
    assert is_ssh_session({"SSH_TTY": "/dev/pts/1"})
    assert not is_ssh_session({})


def test_tmux_preflight_also_runs_for_inside_tmux(monkeypatch, tmp_path):
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", lambda: False)
    with patch("railmux.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 2
    app_cls.assert_not_called()


def test_doctor_runs_before_tmux_preflight(monkeypatch, tmp_path):
    doctor = MagicMock(return_value=0)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.cli.run_doctor", doctor)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["--doctor", "--claude-home", str(tmp_path)])

    assert result == 0
    doctor.assert_called_once_with(claude_home=tmp_path)
    preflight.assert_not_called()


def test_invalid_config_is_actionable_without_traceback(
    monkeypatch, tmp_path, capsys,
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(
        "railmux.cli.load_config",
        MagicMock(side_effect=ConfigError("invalid TOML")),
    )
    monkeypatch.setattr(
        "railmux.cli.default_config_path", lambda: config_path)

    with patch("railmux.ui.app.App") as app_cls:
        result = main(["--inside-tmux"])

    stderr = capsys.readouterr().err
    assert result == 2
    assert "invalid TOML" in stderr
    assert "Traceback" not in stderr
    app_cls.assert_not_called()


def test_startup_message_paints_and_flushes_only_on_a_tty(monkeypatch):
    output = MagicMock()
    output.isatty.return_value = True
    monkeypatch.setattr("railmux.cli.sys.stdout", output)

    _show_startup_message()

    surface = output.write.call_args.args[0]
    assert surface.startswith("\033[2J\033[H")
    assert "RAILMUX" in surface
    assert "Restoring your workspace" in surface
    assert "Reconnecting sessions and panes…" in surface
    output.flush.assert_called_once_with()

    output.reset_mock()
    output.isatty.return_value = False
    _show_startup_message()
    output.write.assert_not_called()
