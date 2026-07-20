"""Tests for F8/F9 dispatch and managed binding lifecycle."""
from unittest.mock import MagicMock

from railmux.config import Config
from railmux.ui.app import App
from railmux.ui.workspace import AgentWorkspace, WorkspaceLayout


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
    app._root_function_key_manager = MagicMock()
    for k, v in attrs.items():
        setattr(app, k, v)
    return app


def test_install_uses_shared_function_key_manager():
    manager = MagicMock()
    app = _bare_app(_root_function_key_manager=manager)

    app._install_function_key_bindings()

    manager.open.assert_called_once_with()


def test_install_skips_without_function_key_manager():
    app = _bare_app(_root_function_key_manager=None)

    app._install_function_key_bindings()  # must not raise


def test_f8_dispatches_rotate_without_sidebar_action_lookup():
    app = _bare_app()
    app._rotate_split = MagicMock()

    app._on_input("f8")

    app._rotate_split.assert_called_once_with()


def test_f8_restores_remembered_secondary_as_side_by_side(monkeypatch):
    app = _bare_app()
    workspace = app._agent_workspace()
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-primary"
    workspace.collapsed_secondary_agent = "cx-secondary"
    app._agent_session_alive = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(160, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._install_function_key_bindings = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._set_status = MagicMock()

    def rebuild(layout, agent_tmux_name):
        workspace.layout = layout
        workspace.secondary.pane_id = "%2"
        workspace.secondary.agent_tmux_name = agent_tmux_name
        return True

    app._rebuild_secondary = MagicMock(side_effect=rebuild)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    app._rotate_split()

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.target_slot_key == AgentWorkspace.PRIMARY
    assert workspace.secondary.agent_tmux_name == "cx-secondary"
    app._rebuild_secondary.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE, "cx-secondary")


def test_f8_creates_empty_secondary_without_preselected_session(monkeypatch):
    app = _bare_app()
    workspace = app._agent_workspace()
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-primary"
    app._agent_session_alive = MagicMock(return_value=False)
    app._agent_region_size = MagicMock(return_value=(160, 40))
    app._layout_fits = MagicMock(return_value=True)
    app._install_function_key_bindings = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._set_status = MagicMock()

    def rebuild(layout, agent_tmux_name):
        workspace.layout = layout
        workspace.secondary.pane_id = "%2"
        workspace.secondary.agent_tmux_name = agent_tmux_name
        return True

    app._rebuild_secondary = MagicMock(side_effect=rebuild)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    app._rotate_split()

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.secondary.agent_tmux_name is None
    app._rebuild_secondary.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE, None)


def test_f8_skips_unusable_columns_and_opens_stacked(monkeypatch):
    app = _bare_app()
    workspace = app._agent_workspace()
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-primary"
    app._agent_session_alive = MagicMock(return_value=False)
    app._agent_region_size = MagicMock(return_value=(84, 29))
    app._layout_fits = MagicMock(
        side_effect=lambda _region, layout:
        layout is WorkspaceLayout.STACKED)
    app._install_function_key_bindings = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._set_status = MagicMock()

    def rebuild(layout, agent_tmux_name):
        workspace.layout = layout
        workspace.secondary.pane_id = "%2"
        return True

    app._rebuild_secondary = MagicMock(side_effect=rebuild)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    app._rotate_split()

    app._rebuild_secondary.assert_called_once_with(
        WorkspaceLayout.STACKED, None)


def test_f8_keeps_dual_layout_when_secondary_cannot_close():
    app = _bare_app()
    workspace = app._agent_workspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.pane_id = "%1"
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "cx-secondary"
    app._set_status = MagicMock()
    transport = MagicMock()
    transport.close_slot.return_value = False
    app._display_transport_manager = transport

    app._rotate_split()

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.secondary.pane_id == "%2"
    assert workspace.secondary.agent_tmux_name == "cx-secondary"


def test_teardown_releases_managed_function_bindings(monkeypatch):
    manager = MagicMock()
    app = _bare_app(_root_function_key_manager=manager)
    monkeypatch.setattr(
        "railmux.ui.app.atomic_write_text", lambda *args, **kwargs: None)

    app._teardown_tmux()

    manager.close.assert_called_once_with()


def test_reinstall_is_delegated_idempotently_to_manager():
    manager = MagicMock()
    app = _bare_app(_root_function_key_manager=manager)

    app._install_function_key_bindings()
    app._right_pane_id = "%17"
    app._install_function_key_bindings()

    assert manager.open.call_count == 2


def test_fullscreen_uses_actual_focused_secondary(monkeypatch):
    app = _bare_app(_railmux_pane_id="%1")
    workspace = app._agent_workspace()
    workspace.primary.pane_id = "%2"
    workspace.secondary.pane_id = "%3"
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    toggled = []
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _target: "%3")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_alive", lambda _pane: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.toggle_pane_zoom",
        lambda pane: toggled.append(pane) or True,
    )

    app._toggle_agent_fullscreen()

    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    assert toggled == ["%3"]


def test_f9_remains_global_while_modal_is_open():
    app = _bare_app()
    app._loop = MagicMock()
    app._loop.widget = object()
    app._frame = object()
    app._toggle_agent_fullscreen = MagicMock()

    app._on_input("f9")

    app._toggle_agent_fullscreen.assert_called_once_with()


def test_fast_path_still_rebinds(monkeypatch):
    """Fast path in _attach_in_right_pane also re-installs the binding."""
    app = _bare_app(
        _railmux_pane_id="%1",
        _right_pane_id="%5",
        _right_pane_claude="cc-test",
    )
    app._check_agent_slot_size = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_alive", lambda _pid: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pid: True)

    result = app._attach_in_right_pane("cc-test", steal_focus=False)
    assert result is True
    app._root_function_key_manager.open.assert_called_once_with()


def test_attach_presizes_existing_outer_pane_before_respawn(monkeypatch):
    app = _bare_app(_right_pane_id="%5", _right_pane_claude="cc-old")
    app._set_active_tmux_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._schedule_scroll_acceleration = MagicMock()
    app._install_function_key_bindings = MagicMock()
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
    app._install_function_key_bindings = MagicMock()
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
