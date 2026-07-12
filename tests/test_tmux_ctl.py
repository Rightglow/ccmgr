"""Tests for ccmgr.tmux_ctl — CLI wrappers (mocked, no real tmux needed)."""

import subprocess
from unittest.mock import patch, MagicMock

from ccmgr.tmux_ctl import (
    _bindings_are_tmux_defaults,
    _read_key_binding,
    _set_scroll_bindings,
    enable_clipboard_passthrough,
    install_scroll_bindings,
    prepare_scroll_bindings,
    process_has_child,
    restore_scroll_bindings,
    set_window_border_style,
    set_window_user_option,
    session_has_child,
    split_window_h,
    session_pane_id,
    scroll_lines_per_event,
    scroll_bindings_owned_by,
    server_snapshot,
    wait_window_user_option,
)


def _mock_check_output(stdout: str):
    return patch("subprocess.check_output", return_value=stdout.encode())


def _mock_check_call():
    return patch("subprocess.check_call")


def test_clipboard_adds_override_when_missing():
    with _mock_check_output("") as out, _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should have appended the Ms override
        call.assert_called_once()
        args = call.call_args[0][0]
        assert args[0] == "tmux"
        assert "set-option" in args
        assert "-ga" in args
        assert "Ms=" in str(args)


def test_clipboard_idempotent_when_already_set():
    with _mock_check_output("stuff,Ms=something,more") as out, \
         _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should NOT have called check_call again
        call.assert_not_called()


def test_clipboard_handles_tmux_not_found():
    with _mock_check_output("") as out, \
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
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=True), \
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
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_output("cc-one %1\n"):
        assert server_snapshot() is None


def test_server_snapshot_returns_none_when_tmux_probe_fails():
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=True), \
         patch(
             "subprocess.check_output",
             side_effect=subprocess.CalledProcessError(1, "tmux"),
         ):
        assert server_snapshot() is None


def test_server_snapshot_skips_probe_outside_tmux():
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=False), \
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
        assert "#{@ccmgr_scroll_agent}" in args
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
    with patch("ccmgr.tmux_ctl.read_scroll_bindings", return_value=custom), \
         patch("ccmgr.tmux_ctl._set_scroll_bindings") as install:
        assert install_scroll_bindings("%99") is None
    install.assert_not_called()


def test_tmux_older_than_27_disables_scroll_coalescing():
    with patch("ccmgr.tmux_ctl.tmux_version", return_value=(2, 6)), \
         patch("ccmgr.tmux_ctl.read_scroll_bindings") as read:
        assert prepare_scroll_bindings() is None
    read.assert_not_called()


def test_window_user_option_uses_tmux_27_compatible_command():
    with _mock_check_call() as call:
        assert set_window_user_option("cc-session", "@ccmgr_scroll_agent", "1")
    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "-t", "cc-session",
        "@ccmgr_scroll_agent", "1",
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
        '"#{@ccmgr_scroll_agent}" "send-keys -t %9 U" "scroll-up"'
    )
    with patch("ccmgr.tmux_ctl.read_scroll_bindings", return_value={
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
            "ccmgr-scroll-1", "@ccmgr_scroll_ready", "1", timeout=0.1)
    assert call.call_args.args[0] == [
        "tmux", "show-window-options", "-v", "-t", "ccmgr-scroll-1",
        "@ccmgr_scroll_ready",
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
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=True), \
         _mock_check_call() as call:
        assert set_window_border_style("fg=cyan")

    assert call.call_args.args[0] == [
        "tmux", "set-window-option", "pane-border-style", "fg=cyan",
        ";", "set-window-option", "pane-active-border-style", "fg=cyan",
    ]


def test_split_window_h_can_leave_focus_on_current_pane():
    with patch("ccmgr.tmux_ctl.in_tmux", return_value=True), \
         patch("ccmgr.tmux_ctl.tmux_version", return_value=(3, 4)), \
         _mock_check_output("%9") as output:
        assert split_window_h("cmd", size_percent=70, detached=True) == "%9"

    args = output.call_args.args[0]
    assert args[:6] == [
        "tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
    ]
    assert "-d" in args
    assert "-l" in args


# ── new_detached_session: inner-session options ──────────────────────────

def test_new_detached_session_hides_inner_status_bar():
    """The inner (agent) session's own status bar is turned off so it doesn't
    stack a redundant second bar above the outer ccmgr status bar; mouse and
    clipboard sync are enabled. All session-scoped on the ccmgr-owned session."""
    from ccmgr.tmux_ctl import new_detached_session
    with patch("subprocess.check_call") as call:
        assert new_detached_session("cc-abc", "claude --resume") is True

    argvs = [c.args[0] for c in call.call_args_list]
    assert ["tmux", "new-session", "-d", "-s", "cc-abc", "claude --resume"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "mouse", "on"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "set-clipboard", "on"] in argvs
    assert ["tmux", "set-option", "-t", "cc-abc", "status", "off"] in argvs


def test_new_detached_session_survives_tmux_missing():
    from ccmgr.tmux_ctl import new_detached_session
    with patch("subprocess.check_call", side_effect=FileNotFoundError):
        assert new_detached_session("cc-abc", "claude") is False
