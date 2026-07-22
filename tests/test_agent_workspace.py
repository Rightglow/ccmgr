"""Bounded agent-workspace state and legacy primary-slot accessors."""
import json
from pathlib import Path
from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.display_transport import KillPreparation
from railmux.ui.app import App, _Running
from railmux.ui.workspace import (
    AgentWorkspace,
    SlotRestoreState,
    SwapState,
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
    app._install_tmux_bindings = MagicMock()
    app._set_railmux_focus = MagicMock()
    app._sync_sidebar_to_agent_project = MagicMock()
    app._attach_agent_slot = MagicMock(return_value=True)
    transport = MagicMock()
    transport.close_slot.side_effect = (
        lambda slot: slot.clear_display() or True)
    transport.create_primary.side_effect = lambda: (
        setattr(workspace.primary, "pane_id", "%5") or True)
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


def test_secondary_agent_exit_keeps_dual_layout_with_empty_target(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    workspace.set_target(AgentWorkspace.SECONDARY)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    app._reconcile_display_slots(
        lambda name: name == "cc-primary",
        lambda _pane: True,
    )

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.agent_tmux_name == "cc-primary"
    assert workspace.secondary.pane_id == "%4"
    assert workspace.secondary.agent_tmux_name is None
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    app._set_status.assert_called_with(
        "Pane 2 exited; kept the layout with an empty pane.", "warn")


def test_primary_agent_exit_rebuilds_empty_primary_without_moving_secondary(
        monkeypatch):
    app, workspace, transport = _reconcile_app()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    def attach(slot, name, *, steal_focus):
        slot.pane_id = "%5" if slot is workspace.primary else "%4"
        slot.agent_tmux_name = name
        return True

    app._attach_agent_slot.side_effect = attach
    app._reconcile_display_slots(
        lambda name: name == "cx-secondary",
        lambda _pane: True,
    )

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.pane_id == "%5"
    assert workspace.primary.agent_tmux_name is None
    assert workspace.secondary.pane_id == "%4"
    assert workspace.secondary.agent_tmux_name == "cx-secondary"
    transport.close_slot.assert_called_once_with(workspace.secondary)
    app._set_status.assert_called_with(
        "Pane 1 exited; kept the layout with an empty pane.", "warn")


def test_rebuilt_live_pane_status_does_not_claim_it_is_empty(monkeypatch):
    app, workspace, _transport = _reconcile_app()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.kill_pane", lambda _pane: True)

    def attach(slot, name, *, steal_focus):
        slot.agent_tmux_name = name
        return True

    app._attach_agent_slot.side_effect = attach
    app._reconcile_display_slots(
        lambda _name: True,
        lambda pane: pane != "%3",
    )

    assert workspace.secondary.agent_tmux_name == "cx-secondary"
    app._set_status.assert_called_with(
        "Pane 2 was rebuilt and its agent was restored.", "warn")


def test_exact_displayed_swap_binding_recovers_missing_running_entry(
        monkeypatch):
    app, workspace, _transport = _reconcile_app()
    app._running = {}
    app._project_snapshot = []
    app._codex_index = MagicMock()
    app._codex_index.all_cwds.return_value = {}
    app._codex_index.get.return_value = None
    app._renames = MagicMock()
    app._renames.get.return_value = "Recovered"
    app._valid_running_binding = MagicMock(
        wraps=app._valid_running_binding)
    workspace.primary.agent_tmux_name = "cx-displayed"
    workspace.primary.swap_state = SwapState(
        transaction_id="tx",
        agent_tmux_name="cx-displayed",
        agent_pane_id="%9",
        agent_pane_pid=909,
        home_window_id="@8",
        placeholder_pane_id="%8",
        display_window_id="@1",
        keeper_session="keeper",
        keeper_session_id="$8",
        outer_session_name="railmux",
        outer_session_id="$1",
        owner_pane_id="%1",
    )
    identity = tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=909, session_name="railmux",
        session_id="$1", window_id="@1", dead=False, width=80, height=24,
    )
    current_identity = [tmux_ctl.PaneIdentity(
        pane_id="%9", pane_pid=908, session_name="railmux",
        session_id="$1", window_id="@1", dead=False, width=80, height=24,
    )]
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_identity",
        lambda _pane: current_identity[0],
    )
    binding = {
        "key": "session-id",
        "tmux_name": "cx-displayed",
        "session_type": "codex",
        "cwd": "/project",
    }
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.show_session_user_option",
        lambda *_args: json.dumps(binding),
    )
    server = tmux_ctl.ServerSnapshot(
        sessions=frozenset({"cx-displayed"}),
        panes=frozenset({"%9"}),
    )

    # A reused pane id with the wrong process identity is not sufficient
    # authority to adopt a session merely because its name and binding match.
    assert app._recover_unrepresented_displayed_agents(server) == 0
    assert app._running == {}
    app._valid_running_binding.assert_not_called()

    current_identity[0] = identity
    assert app._recover_unrepresented_displayed_agents(server) == 1
    assert set(app._running) == {"session-id"}
    assert app._running["session-id"].tmux_name == "cx-displayed"
    assert app._running["session-id"].label.endswith("/Recovered")
    args = app._valid_running_binding.call_args.args
    assert args[0] == binding
    assert args[1] == {"cx-displayed": (Path("/project"), 0)}
    assert app._valid_running_binding.call_args.kwargs == {
        "allow_missing_codex_metadata": True,
        "probe_live_writer": False,
    }


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
