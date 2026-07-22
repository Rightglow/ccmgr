"""Saved workspace geometry and exit-lifetime behavior."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from railmux.settings import LayoutProfile
from railmux.ui.app import App
from railmux.ui.modals import LayoutSaveModal
from railmux.ui.workspace import AgentWorkspace, WorkspaceLayout


def _app(layout: WorkspaceLayout = WorkspaceLayout.SINGLE) -> App:
    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = layout
    app._workspace.primary.pane_id = "%2"
    if layout is not WorkspaceLayout.SINGLE:
        app._workspace.secondary.pane_id = "%3"
    app._railmux_pane_id = "%1"
    app._settings = MagicMock()
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

    app._settings.save_layout_profile.assert_not_called()
    app._commit_exit.assert_called_once_with(soft=True)


def test_explicit_geometry_after_fallback_refreshes_always_profile():
    app = _app()
    saved = LayoutProfile("always", "single", 300)
    current = LayoutProfile("always", "single", 500)
    app._layout_profile = saved
    app._layout_profile_fallback = True
    app._layout_geometry_user_owned = True
    app._capture_layout_profile = MagicMock(return_value=current)
    app._settings.save_layout_profile.return_value = True
    app._commit_exit = MagicMock()

    app._request_exit(soft=False)

    app._settings.save_layout_profile.assert_called_once_with(current)
    app._commit_exit.assert_called_once_with(soft=False)


def test_this_time_exit_choice_saves_one_shot_profile():
    app = _app()
    app._layout_geometry_user_owned = True
    current = LayoutProfile("always", "single", 300)
    app._capture_layout_profile = MagicMock(return_value=current)
    app._settings.save_layout_profile.return_value = True
    app._show_preferred_height_modal = MagicMock()
    app._commit_exit = MagicMock()

    app._request_exit(soft=True)

    modal = app._show_preferred_height_modal.call_args.args[0]
    assert isinstance(modal, LayoutSaveModal)
    modal._on_this_time()
    app._settings.save_layout_profile.assert_called_once_with(
        LayoutProfile("once", "single", 300))
    app._commit_exit.assert_called_once_with(soft=True)


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
