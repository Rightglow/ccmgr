"""Tests for railmux.tmux_ctl — CLI wrappers (mocked, no real tmux needed)."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import railmux.tmux_ctl as tmux_ctl
from railmux.tmux_ctl import (
    _bindings_are_tmux_defaults,
    _read_key_binding,
    _set_scroll_bindings,
    descendant_pids,
    enable_clipboard_passthrough,
    install_scroll_bindings,
    open_rollout_uuids_for_pid,
    fit_session_to_pane,
    pane_size,
    pane_pid_for_session,
    prepare_scroll_bindings,
    process_has_child,
    restore_scroll_bindings,
    restore_owned_scroll_bindings,
    session_rollout_ids,
    set_window_border_style,
    set_window_user_option,
    session_has_child,
    session_process_ids,
    split_window_h,
    split_window_v,
    session_pane_id,
    scroll_lines_per_event,
    scroll_bindings_owned_by,
    server_snapshot,
    resize_session_window,
    session_attached_count,
    window_size,
    wait_window_user_option,
    wait_for_processes_exit,
)


def _mock_check_output(stdout: str):
    return patch("subprocess.check_output", return_value=stdout.encode())


def _mock_check_call():
    return patch("subprocess.check_call")


def test_clipboard_adds_override_when_missing():
    with _mock_check_output(""), _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should have appended the Ms override
        call.assert_called_once()
        args = call.call_args[0][0]
        assert args[0] == "tmux"
        assert "set-option" in args
        assert "-ga" in args
        assert "Ms=" in str(args)


def test_clipboard_idempotent_when_already_set():
    with _mock_check_output("stuff,Ms=something,more"), \
         _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should NOT have called check_call again
        call.assert_not_called()


def test_clipboard_handles_tmux_not_found():
    with _mock_check_output(""), \
         patch("subprocess.check_call", side_effect=FileNotFoundError):
        # Should not raise
        enable_clipboard_passthrough()


def test_clipboard_handles_check_output_error():
    with patch("subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "tmux")), \
         _mock_check_call() as call:
        enable_clipboard_passthrough()
        call.assert_called_once()


def test_server_snapshot_collects_sessions_and_panes():
    output = "cc-one\t%1\t101\ncc-one\t%2\t102\ncc-two\t%3\t201\n"
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_output(output) as call:
        snapshot = server_snapshot()

    assert snapshot is not None
    assert snapshot.sessions == frozenset({"cc-one", "cc-two"})
    assert snapshot.panes == frozenset({"%1", "%2", "%3"})
    assert snapshot.session_pids == (("cc-one", 101), ("cc-two", 201))
    assert snapshot.pane_pid_for("cc-one") == 101
    assert snapshot.pane_pid_for("missing") is None
    assert call.call_args.args[0] == [
        "tmux", "list-panes", "-a", "-F",
        "#{session_name}\t#{pane_id}\t#{pane_pid}",
    ]


def test_server_snapshot_rejects_malformed_output():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_output("cc-one %1\n"):
        assert server_snapshot() is None


def test_server_snapshot_returns_none_when_tmux_probe_fails():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         patch(
             "subprocess.check_output",
             side_effect=subprocess.CalledProcessError(1, "tmux"),
         ):
        assert server_snapshot() is None


def test_server_snapshot_skips_probe_outside_tmux():
    with patch("railmux.tmux_ctl.in_tmux", return_value=False), \
         patch("subprocess.check_output") as output:
        assert server_snapshot() is None

    output.assert_not_called()


def test_process_has_child_rejects_probe_errors():
    with patch("subprocess.run") as run:
        run.return_value.returncode = 2
        assert process_has_child(1234) is None
        assert run.call_args.args[0] == ["pgrep", "-P", "1234"]

    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert process_has_child(1234) is None


def test_session_pane_id_returns_first_pane():
    with _mock_check_output("%7\n%8\n"):
        assert session_pane_id("cc-example") == "%7"


def test_session_pane_id_handles_empty_session():
    with _mock_check_output(""):
        assert session_pane_id("cc-example") is None


def test_pane_identity_parses_stable_location_outside_tmux():
    output = "%7\t4242\tagent\t$2\t@3\t0\t91\t31\n"
    with _mock_check_output(output) as call:
        pane = tmux_ctl.pane_identity("%7")
    assert pane == tmux_ctl.PaneIdentity(
        "%7", 4242, "agent", "$2", "@3", False, 91, 31)
    assert call.call_args.args[0][:5] == [
        "tmux", "display-message", "-p", "-t", "%7"]


def test_session_topology_requires_exact_server_results(monkeypatch):
    outputs = iter((
        b"agent\t$2\n",
        b"0\n",
        b"@3\n",
        b"%7\t4242\tagent\t$2\t@3\t0\t91\t31\n",
    ))
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: next(outputs))
    topology = tmux_ctl.session_topology("agent")
    assert topology is not None
    assert topology.session_name == "agent"
    assert topology.single_live_pane.pane_id == "%7"
    assert topology.attached_clients == 0


def test_detached_single_pane_start_command_rejects_attached_session(
        monkeypatch):
    pane = tmux_ctl.PaneIdentity(
        "%7", 4242, "agent", "$2", "@3", False, 91, 31)
    topology = tmux_ctl.SessionTopology(
        "agent", "$2", 1, ("@3",), (pane,))
    monkeypatch.setattr(tmux_ctl, "session_topology", lambda _name: topology)
    with patch("subprocess.check_output") as output:
        assert tmux_ctl.detached_single_pane_start_command(
            "agent", session_id="$2", pane_id="%7") is None
    output.assert_not_called()


def test_swap_and_grouped_session_commands_are_tmux_27_compatible():
    with _mock_check_call() as call:
        assert tmux_ctl.swap_panes("%2", "%1")
        assert tmux_ctl.create_grouped_session("keeper", "railmux")
    assert call.call_args_list[0].args[0] == [
        "tmux", "swap-pane", "-d", "-s", "%2", "-t", "%1"]
    assert call.call_args_list[1].args[0] == [
        "tmux", "new-session", "-d", "-t", "railmux", "-s", "keeper"]


def test_list_window_user_options_preserves_empty_trailing_field():
    with _mock_check_output("@1\tvalue\t\n"):
        rows = tmux_ctl.list_window_user_options(("@one", "@two"))
    assert rows == [("@1", "value", "")]


# ── #12: exact child→rollout correlation via /proc ───────────────────────

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_pane_pid_for_session_reads_first_pane_pid():
    with _mock_check_output("4242\n4243\n"):
        assert pane_pid_for_session("cx-example") == 4242


def test_pane_pid_for_session_handles_empty_and_error():
    with _mock_check_output(""):
        assert pane_pid_for_session("cx-example") is None
    with patch("subprocess.check_output",
               side_effect=subprocess.CalledProcessError(1, "tmux")):
        assert pane_pid_for_session("cx-example") is None


def test_descendant_pids_walks_transitively():
    # 100 -> 200 -> 300 (codex is a grandchild of the pane shell).
    def fake_run(argv, **kw):
        parent = argv[2]
        table = {"100": "200\n", "200": "300\n", "300": ""}
        out = table.get(parent, "")
        return MagicMock(returncode=0 if out else 1, stdout=out.encode())

    with patch("subprocess.run", side_effect=fake_run):
        assert descendant_pids(100) == [200, 300]


def test_session_process_ids_includes_pane_and_descendants(monkeypatch):
    monkeypatch.setattr(tmux_ctl, "pane_pid_for_session", lambda _name: 100)
    monkeypatch.setattr(tmux_ctl, "descendant_pids", lambda _pid: [200, 300])
    assert session_process_ids("cc-example") == (100, 200, 300)


def test_wait_for_processes_exit(monkeypatch):
    probes = {100: 0, 200: 0}

    def fake_kill(pid, _signal):
        probes[pid] += 1
        if probes[pid] >= 2:
            raise ProcessLookupError

    monkeypatch.setattr(tmux_ctl.os, "kill", fake_kill)
    monkeypatch.setattr(tmux_ctl.time, "sleep", lambda _seconds: None)
    assert wait_for_processes_exit((100, 200), timeout=1.0)


def test_wait_for_processes_exit_times_out(monkeypatch):
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(tmux_ctl.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(tmux_ctl.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(tmux_ctl.time, "sleep", lambda _seconds: None)
    assert not wait_for_processes_exit((100,), timeout=0.5)


def test_open_rollout_uuids_filters_to_sessions_dir(tmp_path):
    sessions = tmp_path / "sessions"
    good = sessions / "2026" / "07" / "14" / f"rollout-2026-{_UUID_A}.jsonl"
    good.parent.mkdir(parents=True)
    good.write_text("{}")
    elsewhere = tmp_path / "other" / f"rollout-2026-{_UUID_B}.jsonl"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("{}")
    not_jsonl = sessions / "notes.txt"
    not_jsonl.write_text("x")

    links = {
        "3": str(good),        # matching rollout under sessions dir
        "4": str(elsewhere),   # a .jsonl OUTSIDE sessions dir → excluded
        "5": str(not_jsonl),   # not a rollout → excluded
    }
    with patch("os.listdir", return_value=list(links)), \
         patch("os.readlink", side_effect=lambda p: links[p.rsplit("/", 1)[1]]):
        ids = open_rollout_uuids_for_pid(999, sessions)
    assert ids == {_UUID_A}


def test_open_rollout_uuids_handles_missing_proc():
    with patch("os.listdir", side_effect=FileNotFoundError):
        assert open_rollout_uuids_for_pid(999, Path("/nope")) == set()


def test_session_rollout_ids_unions_pid_and_descendants(monkeypatch):
    monkeypatch.setattr(tmux_ctl, "pane_pid_for_session", lambda name: 100)
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: True)
    monkeypatch.setattr(tmux_ctl, "descendant_pids", lambda pid: [200, 300])
    opened = {100: set(), 200: {_UUID_A}, 300: set()}
    monkeypatch.setattr(tmux_ctl, "open_rollout_uuids_for_pid",
                        lambda pid, d: opened[pid])
    assert session_rollout_ids("cx-example", Path("/s")) == {_UUID_A}


def test_session_rollout_ids_empty_when_pane_pid_not_ready(monkeypatch):
    # procfs available but the pane pid isn't up yet → EMPTY set (transient),
    # NOT None: the caller must WAIT, not fall back to the heuristic (#12).
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: True)
    monkeypatch.setattr(tmux_ctl, "pane_pid_for_session", lambda name: None)
    assert session_rollout_ids("cx-example", Path("/s")) == set()


def test_session_rollout_ids_none_without_procfs(monkeypatch):
    # macOS: no /proc → unavailable → caller falls back to the heuristic.
    monkeypatch.setattr(tmux_ctl, "pane_pid_for_session", lambda name: 100)
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: False)
    assert session_rollout_ids("cx-example", Path("/s")) is None


def test_session_rollout_ids_empty_set_when_no_fd_open(monkeypatch):
    # procfs present but codex hasn't opened its rollout yet → empty set. The
    # caller must WAIT (not fall back) so it never binds an unrelated rollout.
    monkeypatch.setattr(tmux_ctl, "pane_pid_for_session", lambda name: 100)
    monkeypatch.setattr(tmux_ctl, "proc_fs_available", lambda: True)
    monkeypatch.setattr(tmux_ctl, "descendant_pids", lambda pid: [])
    monkeypatch.setattr(tmux_ctl, "open_rollout_uuids_for_pid",
                        lambda pid, d: set())
    assert session_rollout_ids("cx-example", Path("/s")) == set()


def test_scroll_bindings_target_agent_and_keep_default_fallback():
    backup = {
        (table, key): (
            f"bind-key -T {table} {key} select-pane \\; "
            f"send-keys -X -N 5 "
            f"{'scroll-up' if key == 'WheelUpPane' else 'scroll-down'}"
        )
        for table in ("copy-mode", "copy-mode-vi")
        for key in ("WheelUpPane", "WheelDownPane")
    }
    with _mock_check_call() as call:
        assert _set_scroll_bindings("%99", backup)
    assert call.call_count == 4
    for invocation in call.call_args_list:
        args = invocation.args[0]
        assert args[:4] == ["tmux", "bind-key", "-T", args[3]]
        assert "#{@railmux_scroll_agent}" in args
        assert any("send-keys -t %99" in arg for arg in args)
        assert any("send-keys -X -N 5" in arg for arg in args)


def test_read_key_binding_scans_whole_table_for_tmux_27():
    output = (
        "bind-key -T copy-mode C-a cursor-left\n"
        "bind-key -T copy-mode WheelUpPane select-pane \\; "
        "send-keys -X -N 2 scroll-up\n"
    )
    with _mock_check_output(output) as call:
        binding = _read_key_binding("copy-mode", "WheelUpPane")
    assert "WheelUpPane" in binding
    assert call.call_args.args[0] == ["tmux", "list-keys", "-T", "copy-mode"]


def test_custom_wheel_binding_disables_install_without_overwriting_it():
    custom = {
        ("copy-mode", "WheelUpPane"): "bind-key -T copy-mode WheelUpPane page-up",
        ("copy-mode", "WheelDownPane"): "bind-key -T copy-mode WheelDownPane page-down",
        ("copy-mode-vi", "WheelUpPane"): "bind-key -T copy-mode-vi WheelUpPane page-up",
        ("copy-mode-vi", "WheelDownPane"): "bind-key -T copy-mode-vi WheelDownPane page-down",
    }
    assert not _bindings_are_tmux_defaults(custom)
    with patch("railmux.tmux_ctl.read_scroll_bindings", return_value=custom), \
         patch("railmux.tmux_ctl._set_scroll_bindings") as install:
        assert install_scroll_bindings("%99") is None
    install.assert_not_called()


def test_tmux_older_than_27_disables_scroll_coalescing():
    with patch("railmux.tmux_ctl.tmux_version", return_value=(2, 6)), \
         patch("railmux.tmux_ctl.read_scroll_bindings") as read:
        assert prepare_scroll_bindings() is None
    read.assert_not_called()


def _default_root_wheel_backup():
    return {
        "WheelUpPane": (
            'bind-key -T root WheelUpPane if-shell -F '
            '"#{||:#{pane_in_mode},#{mouse_any_flag}}" '
            '{ send-keys -M } { copy-mode -e }'
        ),
        "WheelDownPane": None,
    }


def test_root_wheel_forwarding_requires_stock_bindings_and_tmux_27():
    backup = _default_root_wheel_backup()
    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 4)), \
         patch("railmux.tmux_ctl.read_root_wheel_bindings",
               return_value=backup):
        assert tmux_ctl.prepare_root_wheel_bindings() == backup

    custom = dict(backup, WheelDownPane=(
        "bind-key -T root WheelDownPane display-message custom"))
    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 4)), \
         patch("railmux.tmux_ctl.read_root_wheel_bindings",
               return_value=custom):
        assert tmux_ctl.prepare_root_wheel_bindings() is None

    with patch("railmux.tmux_ctl.tmux_version", return_value=(2, 6)), \
         patch("railmux.tmux_ctl.read_root_wheel_bindings") as read:
        assert tmux_ctl.prepare_root_wheel_bindings() is None
    read.assert_not_called()


def test_root_wheel_forwarding_installs_both_directions_with_owner_marker():
    backup = _default_root_wheel_backup()
    with _mock_check_call() as call:
        assert tmux_ctl.set_root_wheel_forwarding(backup, "owner123")

    assert call.call_count == 2
    up, down = [item.args[0] for item in call.call_args_list]
    assert up[:5] == ["tmux", "bind-key", "-T", "root", "WheelUpPane"]
    assert down[:5] == ["tmux", "bind-key", "-T", "root", "WheelDownPane"]
    assert all(
        any("railmux-wheel-forward-v1-owner123" in arg for arg in argv)
        for argv in (up, down)
    )
    assert any("copy-mode -e" in arg for arg in up)
    assert not any("copy-mode -e" in arg for arg in down)


def test_root_wheel_restore_does_not_overwrite_user_change():
    backup = _default_root_wheel_backup()
    owned = (
        "bind-key -T root WheelUpPane if-shell -F "
        '"railmux-wheel-forward-v1-owner123" "send-keys -M"'
    )
    current = {
        "WheelUpPane": owned,
        "WheelDownPane": (
            "bind-key -T root WheelDownPane display-message user-custom"),
    }
    with patch("railmux.tmux_ctl.read_root_wheel_bindings",
               return_value=current), _mock_check_call() as call:
        tmux_ctl.restore_root_wheel_bindings(backup, token="owner123")

    assert call.call_count == 1
    assert call.call_args.args[0][:2] == ["tmux", "source-file"]


def test_root_function_forwarding_captures_custom_and_unbound_originals():
    backup = {
        "F8": "bind-key -T root F8 display-message original",
        "F9": None,
    }
    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 4)), \
         patch("railmux.tmux_ctl.read_root_function_bindings",
               return_value=backup):
        assert tmux_ctl.prepare_root_function_bindings() == backup

    owned = dict(
        backup,
        F9=("bind-key -T root F9 if-shell -F "
            "railmux-function-forward-v1-old send-keys"),
    )
    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 4)), \
         patch("railmux.tmux_ctl.read_root_function_bindings",
               return_value=owned):
        assert tmux_ctl.prepare_root_function_bindings() is None


def test_root_function_forwarding_scopes_and_preserves_fallbacks():
    backup = {
        "F8": "bind-key -T root F8 display-message original",
        "F9": None,
    }
    with _mock_check_call() as call:
        assert tmux_ctl.set_root_function_forwarding(backup, "owner123")

    assert call.call_count == 2
    f8, f9 = [item.args[0] for item in call.call_args_list]
    assert f8[:5] == ["tmux", "bind-key", "-T", "root", "F8"]
    assert f9[:5] == ["tmux", "bind-key", "-T", "root", "F9"]
    assert all(
        any("railmux-function-forward-v1-owner123" in arg for arg in argv)
        for argv in (f8, f9)
    )
    assert all(
        any("@railmux_controller_pane" in arg for arg in argv)
        for argv in (f8, f9)
    )
    assert all(
        any('run-shell "tmux send-keys -t ' in arg for arg in argv)
        for argv in (f8, f9)
    )
    assert all(
        any("'#{@railmux_controller_pane}'" in arg for arg in argv)
        for argv in (f8, f9)
    )
    assert all(
        all(argv[index:index + 2] != ["-t", "="]
            for index in range(len(argv) - 1))
        for argv in (f8, f9)
    )
    assert f8[-1] == "display-message original"
    assert f9[-1] == "send-keys F9"


def test_root_function_restore_does_not_overwrite_user_change():
    backup = {
        "F8": "bind-key -T root F8 display-message original",
        "F9": None,
    }
    current = {
        "F8": (
            "bind-key -T root F8 if-shell -F "
            "railmux-function-forward-v1-owner123 send-keys"
        ),
        "F9": "bind-key -T root F9 display-message user-custom",
    }
    with patch("railmux.tmux_ctl.read_root_function_bindings",
               return_value=current), _mock_check_call() as call:
        tmux_ctl.restore_root_function_bindings(backup, token="owner123")

    assert call.call_count == 1
    assert call.call_args.args[0][:2] == ["tmux", "source-file"]


def test_root_right_click_selects_pointer_pane_only_in_railmux_window():
    backup = {
        "MouseDown3Pane": (
            "bind-key -T root MouseDown3Pane display-menu original"),
    }

    with _mock_check_call() as call:
        assert tmux_ctl.set_root_right_click_forwarding(backup, "owner123")

    argv = call.call_args.args[0]
    assert argv[:5] == [
        "tmux", "bind-key", "-T", "root", "MouseDown3Pane"]
    assert argv[5:9] == ["if-shell", "-F", "-t", "="]
    assert any("railmux-right-click-forward-v1-owner123" in arg
               for arg in argv)
    assert any(tmux_ctl.RAILMUX_CONTROLLER_OPTION in arg for arg in argv)
    assert "select-pane -t = ; send-keys -M" in argv
    assert argv[-1] == "display-menu original"


def test_root_right_click_restore_does_not_overwrite_user_change():
    backup = {"MouseDown3Pane": None}
    current = {
        "MouseDown3Pane": (
            "bind-key -T root MouseDown3Pane display-message user-custom"),
    }
    with patch("railmux.tmux_ctl.read_root_right_click_binding",
               return_value=current), _mock_check_call() as call:
        tmux_ctl.restore_root_right_click_binding(
            backup, token="owner123")

    call.assert_not_called()


def test_status_pane_range_requires_tmux_34_and_valid_pane():
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 3)):
        assert tmux_ctl.status_pane_range("%7", "[1]") == "[1]"
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 4)):
        assert tmux_ctl.status_pane_range("%7", "#[fg=white][1]") == (
            "#[range=user|%7]#[fg=white][1]#[norange]"
        )
        try:
            tmux_ctl.status_pane_range("session:0.1", "[1]")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe pane target was accepted")


def test_root_status_click_scopes_pane_range_and_keeps_zoom():
    backup = {
        "MouseDown1Status":
        "bind-key -T root MouseDown1Status select-window -t =",
    }
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 4)), \
            _mock_check_call() as call:
        assert tmux_ctl.set_root_status_click_forwarding(
            backup, "owner123")

    argv = call.call_args.args[0]
    assert argv[:5] == [
        "tmux", "bind-key", "-T", "root", "MouseDown1Status"]
    assert argv[5:9] == ["if-shell", "-F", "-t", "="]
    assert "mouse_status_range" in argv[9]
    assert tmux_ctl.RAILMUX_CONTROLLER_OPTION in argv[9]
    assert "railmux-status-pane-v1-owner123" in argv[9]
    assert "tmux select-pane -Z -t '#{mouse_status_range}'" in argv[10]
    assert argv[-1] == "select-window -t ="


def test_root_status_click_declines_old_tmux_and_preserves_user_reload():
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 3)), \
            patch.object(tmux_ctl, "read_root_status_click_binding") as read:
        assert tmux_ctl.prepare_root_status_click_binding() is None
        read.assert_not_called()

    backup = {"MouseDown1Status": None}
    current = {
        "MouseDown1Status": (
            "bind-key -T root MouseDown1Status display-message user-custom"),
    }
    with patch.object(
            tmux_ctl, "read_root_status_click_binding",
            return_value=current), _mock_check_call() as call:
        tmux_ctl.restore_root_status_click_binding(
            backup, token="owner123")
    call.assert_not_called()


def test_prefix_target_binding_scopes_toggle_and_preserves_fallback():
    backup = {
        "Tab": "bind-key -T prefix Tab display-message original-tab",
    }

    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 4)), \
            _mock_check_call() as call:
        assert tmux_ctl.set_prefix_target_binding(backup, "owner123")

    argv = call.call_args.args[0]
    assert argv[:5] == ["tmux", "bind-key", "-T", "prefix", "Tab"]
    assert any("railmux-target-toggle-v1-owner123" in arg for arg in argv)
    assert any(tmux_ctl.RAILMUX_CONTROLLER_OPTION in arg for arg in argv)
    assert any(tmux_ctl.RAILMUX_TARGET_OPTION in arg for arg in argv)
    assert "select-pane -Z" in argv[-2]
    assert argv[-1] == "display-message original-tab"


def test_prefix_target_pre_31_reapplies_zoom_only_when_needed():
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 0)), \
            _mock_check_call() as call:
        assert tmux_ctl.set_prefix_target_binding({"Tab": None}, "owner123")

    toggle = call.call_args.args[0][-2]
    assert "window_zoomed_flag" in toggle
    assert "tmux select-pane -t" in toggle
    assert "tmux resize-pane -Z" in toggle
    assert '!= 1' in toggle


def test_unbound_prefix_target_fallback_is_noop():
    with _mock_check_call() as call:
        assert tmux_ctl.set_prefix_target_binding({"Tab": None}, "owner123")

    assert call.call_args.args[0][-1] == 'run-shell "true"'


def test_prefix_target_binding_rejects_unpreservable_user_binding():
    repeat = "bind-key -r -T prefix Tab display-message repeated"
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 4)), \
            patch.object(tmux_ctl, "read_prefix_target_binding",
                         return_value={"Tab": repeat}):
        assert tmux_ctl.prepare_prefix_target_binding() is None

    annotated = "bind-key -T prefix Tab display-message annotated"
    with patch.object(tmux_ctl, "tmux_version", return_value=(3, 4)), \
            patch.object(tmux_ctl, "read_prefix_target_binding",
                         return_value={"Tab": annotated}), \
            patch.object(tmux_ctl.subprocess, "check_output",
                         return_value=b"Tab probe note\n"):
        assert tmux_ctl.prepare_prefix_target_binding() is None

    assert not tmux_ctl.set_prefix_target_binding(
        {"Tab": "not replayable tmux config"}, "owner123")


def test_unset_window_user_option_requires_exact_owner_value():
    with _mock_check_output("%1"), _mock_check_call() as call:
        assert tmux_ctl.unset_window_user_option_if_value(
            "%1", tmux_ctl.RAILMUX_CONTROLLER_OPTION, "%1")
    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "-u", "-t", "%1",
        tmux_ctl.RAILMUX_CONTROLLER_OPTION,
    ]

    with _mock_check_output("%other"), _mock_check_call() as call:
        assert not tmux_ctl.unset_window_user_option_if_value(
            "%1", tmux_ctl.RAILMUX_CONTROLLER_OPTION, "%1")
    call.assert_not_called()


def test_window_user_option_uses_tmux_27_compatible_command():
    with _mock_check_call() as call:
        assert set_window_user_option("cc-session", "@railmux_scroll_agent", "1")
    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "-t", "cc-session",
        "@railmux_scroll_agent", "1",
    ]


def test_scroll_distance_is_read_from_original_binding():
    backup = {
        ("copy-mode", "WheelUpPane"): "send-keys -X -N 5 scroll-up",
        ("copy-mode", "WheelDownPane"): "send-keys -X -N 5 scroll-down",
    }
    assert scroll_lines_per_event(backup) == 5


def test_inconsistent_scroll_distances_are_rejected():
    backup = {
        ("copy-mode", "WheelUpPane"): "send-keys -X -N 5 scroll-up",
        ("copy-mode", "WheelDownPane"): "send-keys -X -N 2 scroll-down",
    }
    assert scroll_lines_per_event(backup) == 0


def test_scroll_binding_ownership_checks_agent_target():
    wrapped = (
        'bind-key -T copy-mode WheelUpPane if-shell -F -t = '
        '"#{@railmux_scroll_agent}" "send-keys -t %9 U" "scroll-up"'
    )
    with patch("railmux.tmux_ctl.read_scroll_bindings", return_value={
        ("copy-mode", "WheelUpPane"): wrapped,
        ("copy-mode-vi", "WheelUpPane"): wrapped,
        ("copy-mode", "WheelDownPane"): wrapped.replace(" U", " D"),
        ("copy-mode-vi", "WheelDownPane"): wrapped.replace(" U", " D"),
    }):
        assert scroll_bindings_owned_by("%9")
        assert not scroll_bindings_owned_by("%10")


def test_wait_window_user_option_observes_ready_value():
    with _mock_check_output("1") as call:
        assert wait_window_user_option(
            "railmux-scroll-1", "@railmux_scroll_ready", "1", timeout=0.1)
    assert call.call_args.args[0] == [
        "tmux", "show-window-options", "-v", "-t", "railmux-scroll-1",
        "@railmux_scroll_ready",
    ]


def test_restore_scroll_bindings_replays_saved_binding_and_unbinds_missing():
    backup = {
        ("copy-mode", "WheelUpPane"): (
            "bind-key -T copy-mode WheelUpPane select-pane "
            r"\; send-keys -X -N 2 scroll-up"
        ),
        ("copy-mode", "WheelDownPane"): None,
    }
    with _mock_check_call() as call:
        restore_scroll_bindings(backup)
    assert call.call_args_list[0].args[0][:2] == ["tmux", "source-file"]
    assert call.call_args_list[1].args[0] == [
        "tmux", "unbind-key", "-T", "copy-mode", "WheelDownPane",
    ]


def test_restore_owned_scroll_bindings_preserves_custom_key():
    owned_up = (
        "bind-key -T copy-mode WheelUpPane if-shell -F "
        "#{@railmux_scroll_agent} 'send-keys -t %9 U' fallback"
    )
    owned_vi_up = owned_up.replace("copy-mode ", "copy-mode-vi ")
    owned_vi_down = (
        "bind-key -T copy-mode-vi WheelDownPane if-shell -F "
        "#{@railmux_scroll_agent} 'send-keys -t %9 D' fallback"
    )
    current = {
        ("copy-mode", "WheelUpPane"): owned_up,
        ("copy-mode", "WheelDownPane"): "bind-key -T copy-mode WheelDownPane custom",
        ("copy-mode-vi", "WheelUpPane"): owned_vi_up,
        ("copy-mode-vi", "WheelDownPane"): owned_vi_down,
    }
    backup = {
        ("copy-mode", "WheelUpPane"): "original-up",
        ("copy-mode", "WheelDownPane"): "original-down",
        ("copy-mode-vi", "WheelUpPane"): None,
        ("copy-mode-vi", "WheelDownPane"): "original-vi-down",
    }
    with patch("railmux.tmux_ctl.read_scroll_bindings", return_value=current), \
         patch("railmux.tmux_ctl.restore_scroll_bindings") as restore:
        restore_owned_scroll_bindings("%9", backup)

    restore.assert_called_once_with({
        ("copy-mode", "WheelUpPane"): "original-up",
        ("copy-mode-vi", "WheelUpPane"): None,
        ("copy-mode-vi", "WheelDownPane"): "original-vi-down",
    })


def test_restore_owned_scroll_bindings_ignores_another_helper():
    current = {
        ("copy-mode", "WheelUpPane"): (
            "bind-key -T copy-mode WheelUpPane if-shell -F "
            "#{@railmux_scroll_agent} 'send-keys -t %other U' fallback"
        ),
    }
    with patch("railmux.tmux_ctl.read_scroll_bindings", return_value=current), \
         patch("railmux.tmux_ctl.restore_scroll_bindings") as restore:
        restore_owned_scroll_bindings(
            "%ours", {("copy-mode", "WheelUpPane"): "original"})

    restore.assert_not_called()


def test_session_has_child_distinguishes_no_child_from_probe_error():
    with _mock_check_output("1234"):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 1
            assert session_has_child("cc-example") is False

            run.return_value.returncode = 2
            assert session_has_child("cc-example") is None


def test_session_has_child_reports_live_child():
    with _mock_check_output("1234"), patch("subprocess.run") as run:
        run.return_value.returncode = 0
        assert session_has_child("cc-example") is True


def test_session_has_child_returns_unknown_when_tmux_probe_fails():
    with patch("subprocess.check_output", side_effect=FileNotFoundError):
        assert session_has_child("cc-example") is None


def test_session_has_child_returns_unknown_without_pane_pid():
    with _mock_check_output(""):
        assert session_has_child("cc-example") is None


def test_window_border_style_updates_both_segments_in_one_tmux_call():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_call() as call:
        assert set_window_border_style("fg=cyan")

    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "pane-border-style", "fg=cyan",
        ";", "set-window-option", "pane-active-border-style", "fg=cyan",
    ]


def test_local_window_option_distinguishes_inheritance_from_failure():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_output(""):
        assert tmux_ctl.local_window_option("pane-border-indicators") == (
            True, None)

    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         patch("subprocess.check_output", side_effect=FileNotFoundError):
        assert tmux_ctl.local_window_option("pane-border-indicators") == (
            False, None)


def test_set_window_option_can_restore_inheritance():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_call() as call:
        assert tmux_ctl.set_window_option("pane-border-indicators", None)

    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "-u", "pane-border-indicators",
    ]


def test_split_window_h_can_leave_focus_on_current_pane():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         patch("railmux.tmux_ctl.tmux_version", return_value=(3, 4)), \
         _mock_check_output("%9") as output:
        assert split_window_h("cmd", size_percent=70, detached=True) == "%9"

    args = output.call_args.args[0]
    assert args[:6] == [
        "tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
    ]
    assert "-d" in args
    assert "-l" in args


def test_split_window_v_supports_equal_detached_layout():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         patch("railmux.tmux_ctl.tmux_version", return_value=(2, 7)), \
         _mock_check_output("%10") as output:
        assert split_window_v(
            "cmd", target="%9", size_percent=50, detached=True,
        ) == "%10"

    args = output.call_args.args[0]
    assert args[:6] == [
        "tmux", "split-window", "-v", "-P", "-F", "#{pane_id}",
    ]
    assert "-d" in args
    assert args[args.index("-p") + 1] == "50"
    assert args[args.index("-t") + 1] == "%9"


def test_last_pane_id_uses_tmux_previous_active_flag():
    with patch("railmux.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_output("\n%3\n") as output:
        assert tmux_ctl.last_pane_id("%1") == "%3"

    assert output.call_args.args[0] == [
        "tmux", "list-panes", "-t", "%1", "-F",
        "#{?pane_last,#{pane_id},}",
    ]


def test_toggle_pane_zoom_targets_exact_pane():
    with patch("subprocess.check_call") as call:
        assert tmux_ctl.toggle_pane_zoom("%3") is True

    assert call.call_args.args[0] == [
        "tmux", "resize-pane", "-Z", "-t", "%3",
    ]


def test_resize_pane_width_targets_exact_cells():
    with patch("subprocess.check_call") as call:
        assert tmux_ctl.resize_pane_width("%3", 32) is True

    assert call.call_args.args[0] == [
        "tmux", "resize-pane", "-t", "%3", "-x", "32",
    ]
    assert not tmux_ctl.resize_pane_width("%3", 0)


def test_pane_size_parses_exact_dimensions():
    with _mock_check_output("108\t38") as output:
        assert pane_size("%9") == (108, 38)

    assert output.call_args.args[0] == [
        "tmux", "display-message", "-p", "-t", "%9",
        "#{pane_width}\t#{pane_height}",
    ]


def test_window_size_parses_containing_workspace_dimensions():
    with _mock_check_output("155\t38") as output:
        assert window_size("%142") == (155, 38)

    assert output.call_args.args[0] == [
        "tmux", "display-message", "-p", "-t", "%142",
        "#{window_width}\t#{window_height}",
    ]


def test_window_size_rejects_invalid_dimensions():
    with _mock_check_output("0\t38"):
        assert window_size("%142") is None
    with _mock_check_output("not-a-size"):
        assert window_size("%142") is None


def test_resize_session_window_targets_detached_agent():
    with _mock_check_call() as call:
        assert resize_session_window("cx-agent", 108, 38) is True

    assert call.call_args.args[0] == [
        "tmux", "resize-window", "-t", "cx-agent",
        "-x", "108", "-y", "38",
    ]


def test_fit_session_to_pane_is_best_effort(monkeypatch):
    monkeypatch.setattr(
        tmux_ctl, "session_attached_count", lambda _session: 0)
    monkeypatch.setattr(tmux_ctl, "pane_size", lambda _pane: (90, 31))
    resize = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "resize_session_window", resize)

    assert fit_session_to_pane("cc-agent", "%4") is True
    resize.assert_called_once_with("cc-agent", 90, 31)

    monkeypatch.setattr(tmux_ctl, "pane_size", lambda _pane: None)
    assert fit_session_to_pane("cc-agent", "%4") is False


def test_use_smallest_window_size_is_window_scoped_on_modern_tmux(monkeypatch):
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (3, 5))
    call = MagicMock()
    monkeypatch.setattr(subprocess, "check_call", call)

    assert tmux_ctl.use_smallest_window_size("%4") is True

    call.assert_called_once_with(
        [
            "tmux", "set-window-option", "-t", "%4",
            "window-size", "smallest",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_use_smallest_window_size_is_native_before_tmux_29(monkeypatch):
    monkeypatch.setattr(tmux_ctl, "tmux_version", lambda: (2, 8))
    call = MagicMock()
    monkeypatch.setattr(subprocess, "check_call", call)

    assert tmux_ctl.use_smallest_window_size("%4") is True
    call.assert_not_called()


def test_resize_pane_height_rejects_invalid_and_is_exact(monkeypatch):
    call = MagicMock()
    monkeypatch.setattr(subprocess, "check_call", call)

    assert tmux_ctl.resize_pane_height("%4", 0) is False
    assert tmux_ctl.resize_pane_height("%4", 19) is True

    call.assert_called_once_with(
        ["tmux", "resize-pane", "-t", "%4", "-y", "19"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_session_attached_count_and_fit_guard(monkeypatch):
    with _mock_check_output("2"):
        assert session_attached_count("cc-agent") == 2

    monkeypatch.setattr(
        tmux_ctl, "session_attached_count", lambda _session: 1)
    resize = MagicMock()
    monkeypatch.setattr(tmux_ctl, "resize_session_window", resize)
    assert fit_session_to_pane("cc-agent", "%4") is False
    resize.assert_not_called()


# ── new_detached_session: inner-session options ──────────────────────────

def test_new_detached_session_hides_inner_status_bar():
    """The inner (agent) session's own status bar is turned off so it doesn't
    stack a redundant second bar above the outer railmux status bar; mouse and
    clipboard sync are enabled. All session-scoped on the railmux-owned session."""
    from railmux.tmux_ctl import new_detached_session
    with patch("subprocess.check_call") as call, \
            patch("subprocess.run") as run:
        # Health check: pane is alive (stdout "0").
        run.return_value.returncode = 0
        run.return_value.stdout = "0"
        assert new_detached_session("cc-abc", "claude --resume") == (True, None)

    argvs = [c.args[0] for c in call.call_args_list]
    assert ["tmux", "new-session", "-d", "-s", "cc-abc", "claude --resume"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "mouse", "on"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "set-clipboard", "on"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "status", "off"] in argvs


def test_new_detached_session_survives_tmux_missing():
    from railmux.tmux_ctl import new_detached_session
    with patch("subprocess.check_call", side_effect=FileNotFoundError):
        ok, err = new_detached_session("cc-abc", "claude")
        assert ok is False
        assert err is not None


def test_new_detached_session_passes_env_via_tmux_e_flag():
    """The non-secret CODEX_HOME is handed to tmux via ``-e KEY=VALUE`` (tmux
    >= 3.2, when ``new-session -e`` was added) so it lands in the session
    environment. railmux never passes a provider API key here (#8)."""
    from railmux.tmux_ctl import new_detached_session
    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 2)), \
            patch("subprocess.check_call") as call, \
            patch("subprocess.run") as run:
        # Health check: pane is alive.
        run.return_value.returncode = 0
        run.return_value.stdout = "0"
        assert new_detached_session(
            "cx-abc", "exec codex",
            env={"CODEX_HOME": "/home/u/.codex"},
        ) == (True, None)
    argvs = [c.args[0] for c in call.call_args_list]
    new_session = next(a for a in argvs if a[:3] == ["tmux", "new-session", "-d"])
    # -e pair is present, and precedes -s <name> <cmd>.
    assert "-e" in new_session
    assert "CODEX_HOME=/home/u/.codex" in new_session
    assert new_session[-3:] == ["-s", "cx-abc", "exec codex"]


def test_new_detached_session_drops_env_on_old_tmux():
    """On tmux < 3.2 (no ``-e`` support — e.g. 3.1) the session is created
    WITHOUT the env, and there is exactly one new-session call — no blind retry."""
    from railmux.tmux_ctl import new_detached_session
    calls = []

    def fake_check_call(argv, **kw):
        calls.append(argv)
        return 0

    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 1)), \
            patch("subprocess.check_call", side_effect=fake_check_call), \
            patch("subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "0"
        assert new_detached_session("cx-abc", "exec codex",
                                    env={"K": "v"}) == (True, None)
    new_sessions = [a for a in calls if a[:3] == ["tmux", "new-session", "-d"]]
    assert len(new_sessions) == 1
    assert "-e" not in new_sessions[0]


def test_new_detached_session_does_not_retry_on_real_failure():
    """A genuine new-session failure on a capable tmux (>= 3.2) is surfaced as
    False, NOT masked by a broad env-dropping retry (the previous behavior)."""
    from railmux.tmux_ctl import new_detached_session
    calls = []

    def fake_check_call(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["tmux", "new-session", "-d"]:
            raise subprocess.CalledProcessError(1, "tmux")
        return 0

    with patch("railmux.tmux_ctl.tmux_version", return_value=(3, 3)), \
            patch("subprocess.check_call", side_effect=fake_check_call):
        ok, err = new_detached_session("cx-abc", "exec codex",
                                       env={"K": "v"})
        assert ok is False
        assert isinstance(err, str)
    new_sessions = [a for a in calls if a[:3] == ["tmux", "new-session", "-d"]]
    # Attempted exactly once (with -e); no second env-less retry.
    assert len(new_sessions) == 1
    assert "-e" in new_sessions[0]


def test_new_detached_session_detects_disappeared_session():
    """Default tmux removes a pane/session when its command exits.  A failed
    list-panes probe plus a missing target is therefore a launch failure, not a
    healthy session with no dead-pane marker."""
    from railmux.tmux_ctl import new_detached_session
    probe = subprocess.CompletedProcess(
        ["tmux", "list-panes"], 1, stdout="", stderr="can't find session")
    with patch("subprocess.check_call"), \
            patch("subprocess.run", return_value=probe), \
            patch("railmux.tmux_ctl.session_exists", return_value=False):
        ok, err = new_detached_session("cc-gone", "missing-agent")

    assert ok is False
    assert err is not None
    assert "exited immediately" in err


def test_new_detached_session_keeps_live_session_on_probe_error():
    """A health-probe hiccup is best-effort when tmux still has the session."""
    from railmux.tmux_ctl import new_detached_session
    probe = subprocess.CompletedProcess(
        ["tmux", "list-panes"], 1, stdout="", stderr="server busy")
    with patch("subprocess.check_call"), \
            patch("subprocess.run", return_value=probe), \
            patch("railmux.tmux_ctl.session_exists", return_value=True):
        assert new_detached_session(
            "cc-live", "claude") == (True, None)


def test_new_detached_session_health_timeout_is_best_effort():
    from railmux.tmux_ctl import new_detached_session
    with patch("subprocess.check_call"), \
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                ["tmux", "list-panes"], 2)):
        assert new_detached_session(
            "cc-slow-probe", "claude") == (True, None)


def test_new_detached_session_sanitizes_tmux_error():
    from railmux.tmux_ctl import new_detached_session
    failure = subprocess.CalledProcessError(
        1, ["tmux", "new-session"],
        stderr=b"\x1b[31mfailed\x1b[0m\nsecond line",
    )
    with patch("subprocess.check_call", side_effect=failure):
        ok, err = new_detached_session("cc-bad", "claude")

    assert ok is False
    assert err is not None
    assert "\x1b" not in err
    assert "\n" not in err
    assert "failed" in err and "second line" in err
