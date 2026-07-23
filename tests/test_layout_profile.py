"""Saved workspace geometry and exit-lifetime behavior."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from railmux.settings import LayoutProfile
from railmux.ui.app import App, _Running
from railmux.ui.modals import LayoutSaveModal
from railmux.ui.workspace import (
    AgentWorkspace,
    WorkspaceLayout,
    WorkspacePresentation,
)


def _app(layout: WorkspaceLayout = WorkspaceLayout.SINGLE) -> App:
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = layout
    app._workspace.primary.pane_id = "%2"
    if layout is not WorkspaceLayout.SINGLE:
        app._workspace.secondary.pane_id = "%3"
    app._railmux_pane_id = "%1"
    app._settings = MagicMock()
    app._settings.layout_save_policy = "ask"
    app._layout_profile = None
    app._layout_profile_applied = False
    app._layout_profile_fallback = False
    app._layout_geometry_user_owned = False
    app._active_sidebar_permille = None
    app._active_primary_permille = None
    app._set_status = MagicMock()
    return app


def test_capture_layout_uses_size_independent_proportions(monkeypatch):
    app = _app(WorkspaceLayout.SIDE_BY_SIDE)
    sizes = {"%1": (36, 40), "%2": (62, 40), "%3": (81, 40)}
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.window_size", lambda _pane: (180, 40))
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_size", lambda pane: sizes[pane])

    assert app._capture_layout_profile("always") == LayoutProfile(
        "always", "side-by-side", 200, 434)


def test_apply_one_time_stacked_profile_consumes_after_success(monkeypatch):
    app = _app(WorkspaceLayout.STACKED)
    profile = LayoutProfile("once", "stacked", 240, 650)
    app._layout_profile = profile
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(120, 41))
    app._layout_fits = MagicMock(return_value=True)
    app._settings.consume_layout_profile.return_value = True
    resized = MagicMock(return_value=True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.resize_pane_height", resized)

    assert app._apply_layout_profile(allow_create=True) is True

    resized.assert_called_once_with("%2", 26)
    app._settings.consume_layout_profile.assert_called_once_with(profile)
    assert app._layout_profile is None
    assert app._layout_profile_applied is True


def test_apply_saved_dual_layout_can_create_second_pane(monkeypatch):
    app = _app()
    profile = LayoutProfile("always", "side-by-side", 220, 600)
    app._layout_profile = profile
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(141, 40))
    app._layout_fits = MagicMock(return_value=True)
    transport = MagicMock()

    def create(layout):
        app._workspace.layout = layout
        app._workspace.secondary.pane_id = "%3"
        return True

    transport.create_secondary.side_effect = create
    app._display_transport_manager = transport
    resize = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.resize_pane_width", resize)

    assert app._apply_layout_profile(allow_create=True) is True

    transport.create_secondary.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE)
    resize.assert_called_once_with("%2", 84)
    assert app._workspace.layout is WorkspaceLayout.SIDE_BY_SIDE


def test_restore_transient_compact_profile_replays_both_dividers(monkeypatch):
    app = _app(WorkspaceLayout.SIDE_BY_SIDE)
    profile = LayoutProfile("always", "side-by-side", 220, 600)
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(141, 40))
    app._layout_fits = MagicMock(return_value=True)
    resize = MagicMock(return_value=True)
    monkeypatch.setattr("railmux.ui.app.tmux_ctl.resize_pane_width", resize)

    assert app._restore_transient_layout_profile(profile) is True

    assert app._active_sidebar_permille == 220
    assert app._active_primary_permille == 600
    app._resize_sidebar_for_layout.assert_called_once_with(
        WorkspaceLayout.SIDE_BY_SIDE)
    resize.assert_called_once_with("%2", 84)


def test_compact_transition_captures_and_restores_runtime_geometry(monkeypatch):
    app = _app(WorkspaceLayout.SIDE_BY_SIDE)
    profile = LayoutProfile("always", "side-by-side", 220, 600)
    app._capture_layout_profile = MagicMock(return_value=profile)
    app._select_workspace_page = MagicMock(return_value=True)
    app._restore_transient_layout_profile = MagicMock(return_value=True)
    app._apply_layout_profile = MagicMock(return_value=True)
    app._reconcile_focus_from_tmux = MagicMock()
    app._apply_tmux_bar = MagicMock()
    app._window_is_zoomed = MagicMock(side_effect=[False, True])
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id", lambda _pane: "%2")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.toggle_pane_zoom", lambda _pane: True)

    assert app._set_workspace_presentation(
        WorkspacePresentation.COMPACT) is True
    assert app._pre_compact_layout_profile == profile
    assert app._set_workspace_presentation(
        WorkspacePresentation.WIDE) is True

    app._restore_transient_layout_profile.assert_called_once_with(profile)
    assert app._pre_compact_layout_profile is None


def test_compact_exit_without_snapshot_restores_safe_dual_ratio(monkeypatch):
    app = _app(WorkspaceLayout.SIDE_BY_SIDE)
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._pre_compact_layout_profile = None
    app._pre_compact_wide_zoom_pane = None
    app._window_is_zoomed = MagicMock(return_value=False)
    app._restore_transient_layout_profile = MagicMock(return_value=True)
    app._apply_layout_profile = MagicMock(return_value=False)
    app._reconcile_focus_from_tmux = MagicMock()
    app._apply_tmux_bar = MagicMock()

    assert app._set_workspace_presentation(
        WorkspacePresentation.WIDE) is True

    app._restore_transient_layout_profile.assert_called_once_with(
        LayoutProfile("always", "side-by-side", 200, 500))
    assert app._pre_compact_layout_profile is None


def test_adaptive_single_view_keeps_secondary_target_attached_in_primary(
    monkeypatch,
):
    app = _app(WorkspaceLayout.SIDE_BY_SIDE)
    workspace = app._agent_workspace()
    workspace.primary.agent_tmux_name = "cx-primary"
    workspace.secondary.agent_tmux_name = "cx-target"
    workspace.set_target(AgentWorkspace.SECONDARY)
    primary_running = _Running(
        key="primary-session",
        tmux_name="cx-primary",
        label="project/primary",
    )
    target_running = _Running(
        key="target-session",
        tmux_name="cx-target",
        label="project/target",
    )
    app._running = {
        primary_running.key: primary_running,
        target_running.key: target_running,
    }
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda name: MagicMock(session_id=f"${name}"),
    )
    saved = {
        "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
        "target": AgentWorkspace.SECONDARY,
        "focus": AgentWorkspace.SECONDARY,
        "slots": {
            AgentWorkspace.PRIMARY: {
                "kind": "agent", "tmux": "cx-primary"},
            AgentWorkspace.SECONDARY: {
                "kind": "agent", "tmux": "cx-target"},
        },
    }
    app._workspace_recovery_state_data = MagicMock(return_value=saved)
    profile = LayoutProfile("always", "side-by-side", 200, 500)
    app._capture_layout_profile = MagicMock(return_value=profile)
    app._set_single_view_focus = MagicMock()

    def close_secondary(*, announce):
        assert announce is False
        workspace.secondary.clear_display()
        workspace.layout = WorkspaceLayout.SINGLE
        workspace.set_target(AgentWorkspace.PRIMARY)
        return True

    def replace(slot, content):
        assert slot is workspace.primary
        slot.agent_tmux_name = content["tmux"]
        return True

    app._close_secondary_split = MagicMock(side_effect=close_secondary)
    app._replace_slot_content = MagicMock(side_effect=replace)

    assert app._enter_adaptive_single_view() is True

    assert workspace.layout is WorkspaceLayout.SINGLE
    assert workspace.primary.agent_tmux_name == "cx-target"
    assert workspace.collapsed_secondary_agent == "cx-primary"
    assert app._adaptive_single_state == {
        "workspace": saved,
        "profile": profile,
        "visible": AgentWorkspace.SECONDARY,
    }
    assert app._adaptive_single_running_guards == {
        "cx-primary": (primary_running, "$cx-primary"),
        "cx-target": (target_running, "$cx-target"),
    }


def test_adaptive_dual_restore_returns_target_to_its_original_slot(monkeypatch):
    app = _app(WorkspaceLayout.SINGLE)
    workspace = app._agent_workspace()
    workspace.primary.agent_tmux_name = "cx-target"
    workspace.primary.active_session_id = "target-session"
    saved = {
        "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
        "target": AgentWorkspace.SECONDARY,
        "focus": AgentWorkspace.SECONDARY,
        "slots": {
            AgentWorkspace.PRIMARY: {
                "kind": "agent", "tmux": "cx-primary"},
            AgentWorkspace.SECONDARY: {
                "kind": "agent", "tmux": "cx-target"},
        },
    }
    profile = LayoutProfile("always", "side-by-side", 200, 500)
    app._adaptive_single_state = {
        "workspace": saved, "profile": profile}
    app._railmux_has_focus = False
    app._slot_recovery_state_data = MagicMock(return_value={
        "kind": "agent",
        "tmux": "cx-target",
        "session": "target-session",
    })
    app._restore_transient_layout_profile = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    transport = MagicMock()

    def create_secondary(layout):
        workspace.secondary.pane_id = "%3"
        workspace.layout = layout
        return True

    def replace(slot, content):
        slot.agent_tmux_name = content.get("tmux")
        slot.active_session_id = content.get("session")
        return True

    transport.create_secondary.side_effect = create_secondary
    app._display_transport_manager = transport
    app._replace_slot_content = MagicMock(side_effect=replace)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)

    assert app._restore_adaptive_dual_view() is True

    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.primary.agent_tmux_name == "cx-primary"
    assert workspace.secondary.agent_tmux_name == "cx-target"
    assert workspace.target is workspace.secondary
    assert app._adaptive_single_state is None


def test_compact_transition_rebuilds_deferred_dual_before_zoom(monkeypatch):
    app = _app(WorkspaceLayout.SINGLE)
    workspace = app._agent_workspace()
    workspace.primary.agent_tmux_name = "cx-primary"
    saved = {
        "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
        "target": AgentWorkspace.SECONDARY,
        "focus": "sidebar",
        "slots": {
            AgentWorkspace.PRIMARY: {
                "kind": "agent", "tmux": "cx-primary"},
            AgentWorkspace.SECONDARY: {
                "kind": "agent", "tmux": "cx-secondary"},
        },
    }
    profile = LayoutProfile("always", "side-by-side", 200, 500)
    app._adaptive_single_state = {
        "workspace": saved,
        "profile": profile,
        "visible": AgentWorkspace.PRIMARY,
    }
    app._adaptive_single_running_guards = {}
    app._railmux_has_focus = True
    app._slot_recovery_state_data = MagicMock(return_value={
        "kind": "agent", "tmux": "cx-primary",
    })
    app._replace_slot_content = MagicMock(return_value=True)
    app._set_railmux_focus = MagicMock()
    app._paint_slot_active_target = MagicMock()
    app._install_tmux_bindings = MagicMock()
    app._select_workspace_page = MagicMock(return_value=True)
    app._window_is_zoomed = MagicMock(side_effect=[False, True])
    app._restore_transient_layout_profile = MagicMock(return_value=True)
    app._apply_layout_profile = MagicMock(return_value=True)
    app._reconcile_focus_from_tmux = MagicMock()
    app._apply_tmux_bar = MagicMock()
    transport = MagicMock()

    def create_secondary(layout):
        workspace.secondary.pane_id = "%3"
        workspace.layout = layout
        return True

    transport.create_secondary.side_effect = create_secondary
    app._display_transport_manager = transport
    active = iter(("%2", "%1", "%1"))
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.active_pane_id",
        lambda _pane: next(active),
    )
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.select_pane", lambda _pane: True)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.toggle_pane_zoom", lambda _pane: True)

    assert app._set_workspace_presentation(
        WorkspacePresentation.COMPACT) is True

    assert workspace.presentation is WorkspacePresentation.COMPACT
    assert workspace.layout is WorkspaceLayout.SIDE_BY_SIDE
    assert workspace.secondary.pane_id == "%3"
    assert app._adaptive_single_state is None
    assert app._pre_compact_layout_profile == profile
    app._replace_slot_content.assert_called_once_with(
        workspace.secondary, saved["slots"][AgentWorkspace.SECONDARY])
    app._select_workspace_page.assert_called_once()

    assert app._set_workspace_presentation(
        WorkspacePresentation.WIDE) is True
    app._restore_transient_layout_profile.assert_called_once_with(profile)


def test_adaptive_recovery_updates_visible_primary_not_saved_secondary():
    app = _app(WorkspaceLayout.SINGLE)
    app._railmux_has_focus = False
    logical = {
        "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
        "target": AgentWorkspace.SECONDARY,
        "focus": "sidebar",
        "slots": {
            AgentWorkspace.PRIMARY: {
                "kind": "agent", "tmux": "cx-old-primary"},
            AgentWorkspace.SECONDARY: {
                "kind": "agent", "tmux": "cx-secondary"},
        },
    }
    app._adaptive_single_state = {
        "workspace": logical,
        "profile": LayoutProfile("always", "side-by-side", 200, 500),
        "visible": AgentWorkspace.PRIMARY,
    }
    app._slot_recovery_state_data = MagicMock(return_value={
        "kind": "agent",
        "tmux": "cx-new-primary",
        "session": "new-primary-session",
    })

    saved = app._workspace_recovery_state_data()

    assert saved["target"] == AgentWorkspace.PRIMARY
    assert saved["focus"] == AgentWorkspace.PRIMARY
    assert saved["slots"][AgentWorkspace.PRIMARY]["tmux"] == "cx-new-primary"
    assert saved["slots"][AgentWorkspace.SECONDARY]["tmux"] == "cx-secondary"


def test_adaptive_guard_recovers_hidden_running_entry_and_retries(monkeypatch):
    app = _app(WorkspaceLayout.SINGLE)
    session_id = "12345678-1234-1234-1234-1234567890ab"
    running = _Running(
        key=session_id,
        tmux_name="cx-hidden",
        label="project/hidden",
        session_type="codex",
    )
    app._running = {}
    app._adaptive_single_state = {
        "workspace": {
            "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
            "target": AgentWorkspace.PRIMARY,
            "focus": AgentWorkspace.PRIMARY,
            "slots": {
                AgentWorkspace.PRIMARY: {"kind": "agent", "tmux": "cx-primary"},
                AgentWorkspace.SECONDARY: {"kind": "agent", "tmux": "cx-hidden"},
            },
        },
        "profile": LayoutProfile("always", "side-by-side", 200, 500),
    }
    app._adaptive_single_running_guards = {
        "cx-hidden": (running, "$42"),
    }
    app._adaptive_single_failed_geometry = (136, 30)
    topology = MagicMock(session_id="$42")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda _name: topology,
    )
    app._agent_session_alive = MagicMock(return_value=True)

    assert app._repair_adaptive_running_guards() == 1

    assert app._running == {session_id: running}
    assert app._adaptive_single_failed_geometry is None


def test_adaptive_guard_rejects_reused_tmux_name(monkeypatch):
    app = _app(WorkspaceLayout.SINGLE)
    running = _Running(
        key="12345678-1234-1234-1234-1234567890ab",
        tmux_name="cx-hidden",
        label="project/hidden",
        session_type="codex",
    )
    app._running = {}
    app._adaptive_single_state = {"workspace": {}, "profile": None}
    app._adaptive_single_running_guards = {
        "cx-hidden": (running, "$42"),
    }
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_topology",
        lambda _name: MagicMock(session_id="$99"),
    )
    app._agent_session_alive = MagicMock(return_value=True)

    assert app._repair_adaptive_running_guards() == 0
    assert app._running == {}
    app._agent_session_alive.assert_not_called()


def test_adaptive_recovery_keeps_live_target_as_agent_not_preview():
    app = _app(WorkspaceLayout.SINGLE)
    app._railmux_has_focus = False
    logical = {
        "layout": WorkspaceLayout.SIDE_BY_SIDE.value,
        "target": AgentWorkspace.SECONDARY,
        "focus": AgentWorkspace.SECONDARY,
        "slots": {
            AgentWorkspace.PRIMARY: {
                "kind": "agent", "tmux": "cx-primary"},
            AgentWorkspace.SECONDARY: {
                "kind": "agent", "tmux": "cx-target"},
        },
    }
    app._adaptive_single_state = {
        "workspace": logical,
        "profile": LayoutProfile("always", "side-by-side", 200, 500),
    }
    app._slot_recovery_state_data = MagicMock(return_value={
        "kind": "agent",
        "tmux": "cx-target",
        "session": "target-session",
    })

    saved = app._workspace_recovery_state_data()

    assert saved["layout"] == WorkspaceLayout.SIDE_BY_SIDE.value
    assert saved["target"] == AgentWorkspace.SECONDARY
    assert saved["focus"] == AgentWorkspace.SECONDARY
    assert saved["slots"][AgentWorkspace.SECONDARY]["kind"] == "agent"
    assert saved["slots"][AgentWorkspace.SECONDARY]["tmux"] == "cx-target"


def test_failed_new_secondary_ratio_rolls_back_to_single(monkeypatch):
    app = _app()
    app._layout_profile = LayoutProfile(
        "always", "side-by-side", 220, 600)
    app._resize_sidebar_for_layout = MagicMock(return_value=True)
    app._agent_region_size = MagicMock(return_value=(141, 40))
    app._layout_fits = MagicMock(return_value=True)
    transport = MagicMock()

    def create(layout):
        app._workspace.layout = layout
        app._workspace.secondary.pane_id = "%3"
        return True

    def close(_slot):
        app._workspace.secondary.clear_display()
        return True

    transport.create_secondary.side_effect = create
    transport.close_slot.side_effect = close
    app._display_transport_manager = transport
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.resize_pane_width", lambda *_args: False)

    assert app._apply_layout_profile(allow_create=True) is False

    transport.close_slot.assert_called_once_with(app._workspace.secondary)
    assert app._workspace.layout is WorkspaceLayout.SINGLE
    assert app._layout_profile_fallback is True

    transport.reset_mock()
    assert app._apply_layout_profile(allow_create=True) is False
    transport.create_secondary.assert_not_called()
    transport.close_slot.assert_not_called()


def test_fallback_does_not_overwrite_good_always_profile_on_exit():
    app = _app()
    saved = LayoutProfile("always", "single", 300)
    current = LayoutProfile("always", "single", 500)
    app._layout_profile = saved
    app._layout_profile_fallback = True
    app._capture_layout_profile = MagicMock(return_value=current)
    app._commit_exit = MagicMock()

    app._request_exit(soft=True)

    app._settings.set_layout_save_policy.assert_not_called()
    app._commit_exit.assert_called_once_with(soft=True)


def test_explicit_geometry_after_fallback_refreshes_always_profile():
    app = _app()
    saved = LayoutProfile("always", "single", 300)
    current = LayoutProfile("always", "single", 500)
    app._layout_profile = saved
    app._layout_profile_fallback = True
    app._layout_geometry_user_owned = True
    app._capture_layout_profile = MagicMock(return_value=current)
    app._settings.layout_save_policy = "always"
    app._settings.set_layout_save_policy.return_value = True
    app._commit_exit = MagicMock()

    app._request_exit(soft=False)

    app._settings.set_layout_save_policy.assert_called_once_with(
        "always", current)
    app._commit_exit.assert_called_once_with(soft=False)


def test_this_time_exit_choice_saves_one_shot_profile():
    app = _app()
    app._layout_geometry_user_owned = True
    current = LayoutProfile("always", "single", 300)
    app._capture_layout_profile = MagicMock(return_value=current)
    app._settings.set_layout_save_policy.return_value = True
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=True)

    modal = app._show_preferred_height_modal.call_args.args[0]
    assert isinstance(modal, LayoutSaveModal)
    modal._on_this_time()
    app._settings.set_layout_save_policy.assert_called_once_with(
        "ask", LayoutProfile("once", "single", 300))
    app._commit_exit.assert_called_once_with(soft=True)


def test_never_exit_choice_persists_and_discards_saved_profile():
    app = _app()
    app._layout_geometry_user_owned = True
    app._layout_profile = LayoutProfile("once", "single", 250)
    current = LayoutProfile("always", "single", 300)
    app._capture_layout_profile = MagicMock(return_value=current)
    app._settings.set_layout_save_policy.return_value = True
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=True)

    modal = app._show_preferred_height_modal.call_args.args[0]
    modal._on_never()
    app._settings.set_layout_save_policy.assert_called_once_with("never")
    assert app._layout_profile is None
    app._commit_exit.assert_called_once_with(soft=True)


def test_failed_never_exit_choice_still_exits_without_discarding_profile():
    app = _app()
    app._layout_geometry_user_owned = True
    saved = LayoutProfile("once", "single", 250)
    app._layout_profile = saved
    app._capture_layout_profile = MagicMock(
        return_value=LayoutProfile("always", "single", 300)
    )
    app._settings.set_layout_save_policy.return_value = False
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=False)

    app._show_preferred_height_modal.call_args.args[0]._on_never()
    assert app._layout_profile == saved
    app._set_status.assert_called_once_with(
        "Could not save the Never layout preference; "
        "skipping this time only.",
        "error",
    )
    app._commit_exit.assert_called_once_with(soft=False)


def test_never_layout_policy_skips_exit_prompt():
    app = _app()
    app._layout_geometry_user_owned = True
    app._settings.layout_save_policy = "never"
    app._capture_layout_profile = MagicMock(
        return_value=LayoutProfile("always", "single", 300))
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=False)

    app._show_preferred_height_modal.assert_not_called()
    app._commit_exit.assert_called_once_with(soft=False)


def test_unchanged_default_layout_exits_without_save_prompt():
    app = _app()
    app._capture_layout_profile = MagicMock(
        return_value=LayoutProfile("always", "single", 300))
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=False)

    app._show_preferred_height_modal.assert_not_called()
    app._commit_exit.assert_called_once_with(soft=False)


def test_button_detach_refuses_ambiguous_multi_client_target(monkeypatch):
    app = _app()
    run = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.current_session_name", lambda: "railmux")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_attached_count", lambda _session: 2)
    monkeypatch.setattr(subprocess, "run", run)

    app._on_detach()

    run.assert_not_called()
    assert "Ctrl-B d" in app._set_status.call_args.args[0]
    assert app._set_status.call_args.args[1] == "warn"


def test_button_detach_fails_closed_when_client_count_is_unavailable(
    monkeypatch,
):
    app = _app()
    run = MagicMock()
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.current_session_name", lambda: "railmux")
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.session_attached_count", lambda _session: None)
    monkeypatch.setattr(subprocess, "run", run)

    app._on_detach()

    run.assert_not_called()
    assert "Ctrl-B d" in app._set_status.call_args.args[0]
    assert app._set_status.call_args.args[1] == "warn"
