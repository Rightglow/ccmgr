"""Bounded agent-workspace state and legacy primary-slot accessors."""
from unittest.mock import MagicMock

from railmux.display_transport import KillPreparation
from railmux.ui.app import App, _Running
from railmux.ui.workspace import (
    AgentWorkspace,
    SlotRestoreState,
    WorkspaceLayout,
    next_workspace_layout,
)


def test_workspace_slots_are_independent_and_bounded_to_two():
    workspace = AgentWorkspace()
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-one"
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "cx-two"

    assert len(workspace.slots) == 2
    assert workspace.slot_for_pane("%2") is workspace.secondary
    assert workspace.slot_for_agent("cc-one") is workspace.primary
    assert workspace.can_display(workspace.primary, "cc-one") is True
    assert workspace.can_display(workspace.secondary, "cc-one") is False
    assert workspace.primary.agent_tmux_name == "cc-one"
    assert workspace.secondary.agent_tmux_name == "cx-two"


def test_target_pane_is_canonical():
    workspace = AgentWorkspace()

    assert workspace.target is workspace.primary
    assert workspace.target_slot_key == AgentWorkspace.PRIMARY
    assert workspace.set_target(AgentWorkspace.SECONDARY) is workspace.secondary
    assert workspace.target is workspace.secondary
    workspace.set_target(AgentWorkspace.PRIMARY)
    assert workspace.target is workspace.primary


def test_released_active_names_delegate_to_target_model():
    workspace = AgentWorkspace()

    workspace.active_slot_key = AgentWorkspace.SECONDARY
    assert workspace.active is workspace.secondary
    assert workspace.activate(AgentWorkspace.PRIMARY) is workspace.primary
    assert workspace.target_slot_key == AgentWorkspace.PRIMARY


def test_collapse_resets_only_secondary_and_returns_outer_pane():
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.pane_id = "%1"
    workspace.primary.agent_tmux_name = "cc-one"
    workspace.secondary.pane_id = "%2"
    workspace.secondary.agent_tmux_name = "cx-two"
    workspace.set_target(AgentWorkspace.SECONDARY)

    assert workspace.collapse_to_primary() == "%2"
    assert workspace.layout == WorkspaceLayout.SINGLE
    assert workspace.target is workspace.primary
    assert workspace.primary.agent_tmux_name == "cc-one"
    assert workspace.secondary.pane_id is None
    assert workspace.secondary.agent_tmux_name is None


def test_legacy_right_pane_properties_are_primary_slot_views():
    app = App.__new__(App)
    app._right_pane_id = "%7"
    app._right_pane_claude = "cx-agent"
    app._active_session_id = "session-id"
    app._in_history_mode = True

    slot = app._agent_workspace().primary
    assert slot.pane_id == "%7"
    assert slot.agent_tmux_name == "cx-agent"
    assert slot.active_session_id == "session-id"
    assert slot.in_history_mode is True


def test_workspace_layout_cycles_single_columns_rows_single():
    layout = WorkspaceLayout.SINGLE
    layout = next_workspace_layout(layout)
    assert layout is WorkspaceLayout.SIDE_BY_SIDE
    layout = next_workspace_layout(layout)
    assert layout is WorkspaceLayout.STACKED
    assert next_workspace_layout(layout) is WorkspaceLayout.SINGLE


def _reconcile_app() -> tuple[App, AgentWorkspace, MagicMock]:
    app = App.__new__(App)
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = "%2"
    workspace.primary.agent_tmux_name = "cc-primary"
    workspace.secondary.pane_id = "%3"
    workspace.secondary.agent_tmux_name = "cx-secondary"
    app._workspace = workspace
    app._running = {
        "primary": _Running(
            key="primary", tmux_name="cc-primary", label="Primary"),
        "secondary": _Running(
            key="secondary", tmux_name="cx-secondary", label="Secondary"),
    }
    app._railmux_has_focus = True
    app._set_status = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_function_key_bindings = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._sync_sidebar_to_agent_project = MagicMock()
    app._attach_agent_slot = MagicMock(return_value=True)
    transport = MagicMock()
    transport.close_slot.side_effect = (
        lambda slot: slot.clear_display() or True)
    transport.create_secondary.side_effect = lambda layout: (
        setattr(workspace, "layout", layout)
        or setattr(workspace.secondary, "pane_id", "%4")
        or True
    )
    app._display_transport_manager = transport
    return app, workspace, transport


def _kill_app() -> tuple[App, AgentWorkspace, MagicMock]:
    app = App.__new__(App)
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.STACKED
    workspace.primary.pane_id = "%2"
    workspace.primary.agent_tmux_name = "cc-primary"
    workspace.secondary.pane_id = "%3"
    workspace.secondary.agent_tmux_name = "cc-target"
    workspace.set_target(AgentWorkspace.SECONDARY)
    app._workspace = workspace
    app._running = {
        "target-id": _Running(
            key="target-id", tmux_name="cc-target", label="Target"),
    }
    app._railmux_has_focus = True
    app._paint_slot_active_target = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._set_status = MagicMock()
    app._refresh = MagicMock()
    transport = MagicMock()

    def prepare(_name):
        workspace.secondary.clear_content()
        return True

    transport.prepare_kill.side_effect = prepare
    app._display_transport_manager = transport
    return app, workspace, transport


def test_resolved_session_kill_empties_its_slot_before_tmux_kill(monkeypatch):
    app, workspace, transport = _kill_app()
    events: list[str] = []
    alive = True

    def prepare(name):
        events.append("prepare")
        workspace.secondary.clear_content()
        return True

    def session_exists(_name):
        return alive

    def kill(_name):
        nonlocal alive
        events.append("kill")
        alive = False
        return True

    transport.prepare_kill.side_effect = prepare
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", session_exists)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.kill_session", kill)

    app._kill_tmux_session("cc-target", "Target")

    assert events == ["prepare", "kill"]
    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.secondary.pane_id == "%3"
    assert workspace.secondary.agent_tmux_name is None
    assert app._running == {}
    app._paint_slot_active_target.assert_called_once_with(
        workspace.secondary, None, None)
    app._refresh.assert_called_once_with()


def test_failed_tmux_kill_keeps_registry_but_leaves_slot_safely_empty(
        monkeypatch):
    app, workspace, _transport = _kill_app()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_exists", lambda _name: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_session", lambda _name: False)

    app._kill_tmux_session("cc-target", "Target")

    assert "target-id" in app._running
    assert workspace.secondary.pane_id == "%3"
    assert workspace.secondary.agent_tmux_name is None
    app._refresh.assert_not_called()
    assert app._set_status.call_args.args[-1] == "error"


def test_swap_surface_failure_repaints_partial_safe_return():
    app, workspace, transport = _kill_app()

    def fail_after_return(_name):
        workspace.secondary.clear_content()
        return KillPreparation(False, "returned home; surface failed")

    transport.prepare_kill.side_effect = fail_after_return

    assert not app._return_agent_before_kill("cc-target")

    app._paint_slot_active_target.assert_called_once_with(
        workspace.secondary, None, None)
    app._set_railmux_focus.assert_called_once_with(True, force_border=True)
    assert app._set_status.call_args.args == (
        "returned home; surface failed", "error")


def test_secondary_agent_exit_collapses_to_truthful_single_layout(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    app._reconcile_display_slots(
        lambda name: name == "cc-primary",
        lambda _pane: True,
    )

    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.primary.agent_tmux_name == "cc-primary"
    assert workspace.secondary.pane_id is None
    assert workspace.target_slot_key == AgentWorkspace.PRIMARY


def test_primary_exit_promotes_surviving_secondary_safely(monkeypatch):
    app, workspace, transport = _reconcile_app()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    def attach(slot, name, *, steal_focus):
        slot.pane_id = "%5"
        slot.agent_tmux_name = name
        return True

    app._attach_agent_slot.side_effect = attach
    app._reconcile_display_slots(
        lambda name: name == "cx-secondary",
        lambda _pane: True,
    )

    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.primary.agent_tmux_name == "cx-secondary"
    assert workspace.secondary.pane_id is None
    transport.close_slot.assert_called_once_with(workspace.secondary)


def test_single_pane_close_returns_to_sidebar_without_rebuild(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    workspace.layout = WorkspaceLayout.SINGLE
    workspace.secondary.clear_display()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    app._reconcile_display_slots(
        lambda _name: True,
        lambda _pane: False,
    )

    assert workspace.primary.pane_id is None
    assert workspace.primary.agent_tmux_name is None
    app._attach_agent_slot.assert_not_called()
    app._set_status.assert_not_called()
    app._paint_slot_active_target.assert_called_once_with(
        workspace.primary, None, None)
    app._set_railmux_focus.assert_called_once_with(True, force_border=True)


def test_single_history_close_restores_agent_silently(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    workspace.layout = WorkspaceLayout.SINGLE
    workspace.secondary.clear_display()
    workspace.primary.agent_tmux_name = None
    workspace.primary.in_history_mode = True
    workspace.primary.restore_state = SlotRestoreState(
        "agent", tmux_name="cc-primary")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    def attach(slot, name, *, steal_focus):
        slot.pane_id = "%5"
        slot.agent_tmux_name = name
        slot.active_session_id = "session-primary"
        return True

    app._attach_agent_slot.side_effect = attach
    app._reconcile_display_slots(
        lambda _name: True,
        lambda _pane: False,
    )

    app._attach_agent_slot.assert_called_once_with(
        workspace.primary, "cc-primary", steal_focus=False)
    app._sync_sidebar_to_agent_project.assert_called_once_with("cc-primary")
    app._set_status.assert_not_called()


def test_reaped_active_swap_slot_clears_sidebar_selection():
    app, workspace, transport = _reconcile_app()
    workspace.layout = WorkspaceLayout.SINGLE
    workspace.secondary.clear_display()

    def reap(slot):
        if slot is workspace.primary:
            slot.clear_display()
            return "cc-primary"
        return None

    transport.reap_dead_display.side_effect = reap

    assert app._reap_dead_display_slots(transport) == {"cc-primary"}
    app._paint_slot_active_target.assert_called_once_with(
        workspace.primary, None, None)


def test_history_restore_resynchronizes_sidebar_project(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    workspace.primary.agent_tmux_name = None
    workspace.primary.in_history_mode = True
    workspace.primary.restore_state = SlotRestoreState(
        "agent", tmux_name="cc-primary")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    def attach(slot, name, *, steal_focus):
        slot.pane_id = "%5" if slot is workspace.primary else "%6"
        slot.agent_tmux_name = name
        return True

    app._attach_agent_slot.side_effect = attach
    app._reconcile_display_slots(
        lambda _name: True,
        lambda pane: pane != "%2",
    )

    app._sync_sidebar_to_agent_project.assert_called_once_with("cc-primary")
