"""Tests for _install_fullscreen_binding and _teardown_tmux cleanup."""
import subprocess
from unittest.mock import MagicMock

import pytest

from railmux.config import Config
from railmux.ui.app import App


def _bare_app(**attrs):
    """Create a minimal App skeleton with just the attributes needed."""
    app = App.__new__(App)
    app._right_pane_id = None
    app._right_pane_claude = None
    # These focused pane-binding tests model the compatibility transport; the
    # product default is asserted independently in test_config.py.
    app._config = Config(agent_transport="nested")
    app._loop = None
    app._running = {}
    app._scroll_manager = MagicMock()
    app._session_cache = MagicMock()
    app._favorites = MagicMock()
    app._sessions_pane = MagicMock()
    app._running_pane = MagicMock()
    app._railmux_has_focus = True
    app._divider_active = None
    app._frame = MagicMock()
    app._accel_alarm = None
    app._auto_launched = False
    app._soft_quit_flag = False
    app._in_history_mode = False
    app._restore_state = None
    app._active_session_id = None
    app._double_focus_visual_pending = False
    for k, v in attrs.items():
        setattr(app, k, v)
    return app


def test_install_binds_f9_to_right_pane(monkeypatch):
    """_install_fullscreen_binding targets the right pane with F9."""
    app = _bare_app(_right_pane_id="%42")
    calls = []

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))

    app._install_fullscreen_binding()

    assert len(calls) == 1
    assert calls[0] == ["tmux", "bind-key", "-n", "F9", "resize-pane",
                        "-Z", "-t", "%42"]


def test_install_skips_when_no_right_pane(monkeypatch):
    """When no session is launched yet, don't touch tmux bindings."""
    app = _bare_app(_right_pane_id=None)

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: pytest.fail("tmux should not be called"))

    app._install_fullscreen_binding()  # must not raise


def test_teardown_unbinds_f9(monkeypatch):
    """_teardown_tmux cleans up the F9 binding."""
    app = _bare_app()
    unbind_calls = []

    def capture(cmd, **kw):
        if cmd[0] == "tmux" and "unbind-key" in cmd:
            unbind_calls.append(cmd)

    monkeypatch.setattr(subprocess, "run", capture)
    monkeypatch.setattr(
        "railmux.ui.app.atomic_write_text", lambda *args, **kwargs: None)

    app._teardown_tmux()

    assert any("F9" in cmd for cmd in unbind_calls), (
        f"F9 unbind not found in: {unbind_calls}")


def test_reinstall_updates_pane_id(monkeypatch):
    """When the right pane is recreated, binding targets the new pane id."""
    app = _bare_app()
    calls = []

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd[-1]))

    app._right_pane_id = "%9"
    app._install_fullscreen_binding()
    assert calls == ["%9"]

    app._right_pane_id = "%17"
    app._install_fullscreen_binding()
    assert calls == ["%9", "%17"]


def test_fast_path_still_rebinds(monkeypatch):
    """Fast path in _attach_in_right_pane also re-installs the binding."""
    app = _bare_app(_right_pane_id="%5", _right_pane_claude="cc-test")
    app._check_agent_slot_size = MagicMock()
    bind_calls = []

    def capture(cmd, **kw):
        if "bind-key" in cmd:
            bind_calls.append(cmd)

    monkeypatch.setattr(subprocess, "run", capture)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_alive", lambda _pid: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pid: True)

    result = app._attach_in_right_pane("cc-test", steal_focus=False)
    assert result is True
    assert any("F9" in str(cmd) for cmd in bind_calls), (
        f"F9 bind not found in fast path: {bind_calls}")


def test_attach_presizes_existing_outer_pane_before_respawn(monkeypatch):
    app = _bare_app(_right_pane_id="%5", _right_pane_claude="cc-old")
    app._set_active_tmux_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._install_fullscreen_binding = MagicMock()
    app._check_agent_slot_size = MagicMock()
    events = []

    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_alive", lambda _pane: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.fit_session_to_pane",
        lambda session, pane: events.append(("fit", session, pane)) or True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.respawn_pane",
        lambda pane, _cmd: events.append(("respawn", pane)) or True,
    )

    assert app._attach_in_right_pane("cx-new", steal_focus=False) is True
    assert events == [
        ("fit", "cx-new", "%5"),
        ("respawn", "%5"),
    ]
    assert app._primary_slot.agent_tmux_name == "cx-new"
    assert app._primary_slot.mode_key == "codex"


def test_first_attach_creates_detached_pane_then_fits_and_respawns(monkeypatch):
    app = _bare_app()
    app._set_active_tmux_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._install_fullscreen_binding = MagicMock()
    app._check_agent_slot_size = MagicMock()
    events = []

    def split(cmd, **kwargs):
        events.append(("split", cmd, kwargs))
        return "%8"

    monkeypatch.setattr("railmux.ui.app.tmux_ctl.split_window_h", split)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.fit_session_to_pane",
        lambda session, pane: events.append(("fit", session, pane)) or True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.respawn_pane",
        lambda pane, _cmd: events.append(("respawn", pane)) or True,
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane",
        lambda pane: events.append(("select", pane)) or True,
    )

    assert app._attach_in_right_pane("cc-new") is True
    assert events == [
        ("split", "while :; do sleep 3600; done",
         {"size_percent": 70, "detached": True}),
        ("fit", "cc-new", "%8"),
        ("respawn", "%8"),
        ("select", "%8"),
    ]


def test_failed_first_respawn_removes_new_outer_pane(monkeypatch):
    app = _bare_app()
    app._check_agent_slot_size = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.split_window_h",
        lambda _cmd, **_kwargs: "%8")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.fit_session_to_pane", lambda *_args: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.respawn_pane", lambda *_args: False)
    killed = []
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane",
        lambda pane: killed.append(pane) or True,
    )

    assert app._attach_in_right_pane("cc-new") is False
    assert killed == ["%8"]
    assert app._primary_slot.pane_id is None


def test_keymap_references_f9():
    """Verify the keymap entry was updated from F3 to F9."""
    from railmux.ui.keymap import BINDINGS
    fullscreen = next(b for b in BINDINGS if b.desc == "fullscreen")
    assert fullscreen.hint == "F9"
