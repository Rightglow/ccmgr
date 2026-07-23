import os
from unittest.mock import MagicMock, patch

import pytest

from railmux import tmux_server
from railmux.cli import (
    _run_tmux_client_with_watchdog,
    _show_startup_message,
    is_ssh_session,
    main,
)
from railmux.config import ConfigError
from railmux.tmux_server import TmuxServerTarget


@pytest.fixture(autouse=True)
def tmux_preflight_succeeds(monkeypatch):
    """CLI behaviour tests must not depend on the host's tmux installation."""
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", lambda: True)
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", lambda *_args: None
    )
    target = TmuxServerTarget("/tmp/tmux-test/railmux", 123)
    monkeypatch.setattr("railmux.cli.tmux_server.discover_target", lambda: target)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: True
    )


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

    result = main(["doctor", "--claude-home", str(tmp_path)])

    assert result == 0
    doctor.assert_called_once_with(claude_home=tmp_path, json_output=False)
    preflight.assert_not_called()


def test_doctor_json_is_forwarded_before_tmux_preflight(monkeypatch, tmp_path):
    doctor = MagicMock(return_value=0)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.cli.run_doctor", doctor)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["doctor", "--json", "--claude-home", str(tmp_path)])

    assert result == 0
    doctor.assert_called_once_with(claude_home=tmp_path, json_output=True)
    preflight.assert_not_called()


def test_legacy_doctor_flag_is_removed():
    with pytest.raises(SystemExit) as exc:
        main(["--doctor"])

    assert exc.value.code == 2


def test_ssh_subcommand_dispatches_before_local_tmux_preflight(monkeypatch):
    ssh_main = MagicMock(return_value=7)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.fast_display_client.main", ssh_main)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["ssh", "example", "--fps", "30"])

    assert result == 7
    ssh_main.assert_called_once_with(["example", "--fps", "30"])
    preflight.assert_not_called()


def test_remote_server_subcommand_dispatches_to_internal_helper(monkeypatch):
    server_main = MagicMock(return_value=9)
    preflight = MagicMock(return_value=False)
    monkeypatch.setattr("railmux.fast_display_server.main", server_main)
    monkeypatch.setattr("railmux.cli.ensure_tmux_available", preflight)

    result = main(["remote-server", "--protocol", "4"])

    assert result == 9
    server_main.assert_called_once_with(["--protocol", "4"])
    preflight.assert_not_called()


def test_inside_tmux_fails_closed_on_a_foreign_server(monkeypatch, capsys):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )

    with patch("railmux.ui.app.App") as app_cls:
        result = main(["--inside-tmux"])

    assert result == 2
    assert "reserved for Railmux's dedicated" in capsys.readouterr().err
    app_cls.assert_not_called()


def test_foreign_tmux_launches_dedicated_server_with_clean_environment(
    monkeypatch,
):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: None
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-user/default,456,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr("railmux.cli.sys.argv", ["/bin/railmux"])
    run_client = MagicMock(return_value=17)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client
    )

    assert main(["--project", "/work"]) == 17

    argv, env = run_client.call_args.args
    assert run_client.call_args.kwargs == {
        "expected_target": None,
        "expected_session_id": None,
    }
    assert argv == [
        "tmux", "-L", "railmux", "new-session", "-A", "-s", "railmux",
        "/bin/railmux", "--inside-tmux", "--project", "/work",
    ]
    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    # The caller's environment is unchanged; only the replacement process is
    # detached from the foreign tmux identity.
    assert os.environ["TMUX"] == "/tmp/tmux-user/default,456,0"
    assert os.environ["TMUX_PANE"] == "%9"


def test_outer_launcher_checks_for_update_once(monkeypatch):
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: None
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    update = MagicMock()
    monkeypatch.setattr(
        "railmux.self_update.maybe_upgrade_before_launch", update
    )
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog",
        MagicMock(return_value=0),
    )

    assert main(["--project", "/work"]) == 0

    assert update.call_count == 1
    raw_args, settings = update.call_args.args
    assert raw_args == ["--project", "/work"]
    assert settings.update_policy == "ask"


def test_prelaunch_recovery_is_scoped_to_the_dedicated_server(monkeypatch):
    target = TmuxServerTarget("/tmp/tmux-private/railmux", 789)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda: target
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.is_current_server", lambda _target: False
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-user/default,456,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    observed = {}

    def recover():
        observed["tmux"] = os.environ.get("TMUX")
        observed["pane"] = os.environ.get("TMUX_PANE")
        return MagicMock(unresolved=0)

    monkeypatch.setattr(
        "railmux.display_transport.recover_interrupted_swaps", recover
    )
    monkeypatch.setattr(
        "railmux.cli.tmux_server.target_session_id", lambda *_a, **_kw: "$7"
    )
    run_client = MagicMock(return_value=0)
    monkeypatch.setattr(
        "railmux.cli._run_tmux_client_with_watchdog", run_client
    )

    assert main([]) == 0

    assert observed == {"tmux": "/tmp/tmux-private/railmux,789,0", "pane": None}
    assert run_client.call_args.kwargs == {
        "expected_target": target,
        "expected_session_id": "$7",
    }
    assert os.environ["TMUX"] == "/tmp/tmux-user/default,456,0"
    assert os.environ["TMUX_PANE"] == "%9"


def test_local_tmux_watchdog_exits_and_records_after_consecutive_failures(
    monkeypatch, capsys,
):
    class FrozenClient:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    client = FrozenClient()
    monkeypatch.setattr("railmux.cli.subprocess.Popen", lambda *_a, **_k: client)
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("railmux.cli.time.sleep", lambda _seconds: None)
    times = iter((0.0, 5.0, 10.0, 15.0))
    monkeypatch.setattr("railmux.cli.time.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target",
        MagicMock(side_effect=tmux_server.TmuxServerUnresponsive("frozen")),
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    main_result = _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}
    )
    assert main_result == 2
    assert client.terminated
    record.assert_called_once_with(
        component="launcher",
        reason="launcher-watchdog-timeout",
        consecutive_failures=3,
    )
    assert "stopped responding" in capsys.readouterr().err


def test_abrupt_tmux_client_exit_records_disappeared_server(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: ExitedClient())
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("railmux.cli.tmux_server.discover_target", lambda **_kw: None)
    record = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)
    monkeypatch.setattr(
        "railmux.cli.tmux_health.consume_clean_exit", lambda **_kwargs: False)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 1
    record.assert_called_once_with(
        component="launcher",
        reason="launcher-server-exit",
        consecutive_failures=1,
    )


def test_intentional_hard_quit_does_not_record_launcher_incident(monkeypatch):
    target = TmuxServerTarget("/tmp/railmux", 77)

    class ExitedClient:
        returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        "railmux.cli.subprocess.Popen", lambda *_args, **_kwargs: ExitedClient())
    monkeypatch.setattr("railmux.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "railmux.cli.tmux_server.discover_target", lambda **_kw: None)
    consume = MagicMock(return_value=True)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.cli.tmux_health.consume_clean_exit", consume)
    monkeypatch.setattr("railmux.cli.tmux_health.record_incident", record)

    assert _run_tmux_client_with_watchdog(
        ["tmux", "-L", "railmux"], {}, expected_target=target,
        expected_session_id="$7",
    ) == 1
    consume.assert_called_once_with(server_pid=77, session_id="$7")
    record.assert_not_called()


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
