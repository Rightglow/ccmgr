"""Non-blocking warnings for cramped terminal layouts."""
from unittest.mock import MagicMock

from railmux.ui.app import App, PALETTE


def _app():
    app = App.__new__(App)
    app._last_workspace_size = None
    app._last_size_class = None
    app._set_status = MagicMock()
    return app


def test_critical_and_recommended_size_transitions_report_once():
    app = _app()

    app._check_terminal_size((39, 11))
    assert app._set_status.call_args.args[1] == "error"

    # Remaining in the same class must not spam the one-second refresh loop.
    app._check_terminal_size((35, 10))
    assert app._set_status.call_count == 1

    app._check_terminal_size((79, 19))
    assert app._set_status.call_args.args[1] == "warn"

    app._check_terminal_size((120, 30))
    assert app._set_status.call_args.args[1] == "info"
    assert app._set_status.call_count == 3


def test_grass_green_is_the_high_colour_focus_accent():
    pane_focus = next(entry for entry in PALETTE if entry[0] == "pane_focus")

    assert pane_focus[1] == "light green,bold"
    assert pane_focus[4] == "#5faf00,bold"


def test_live_title_and_status_dots_keep_semantic_colours():
    palette = {entry[0]: entry for entry in PALETTE}

    assert palette["live"][4] == "#5faf00,bold"
    assert palette["focus"][5] == "#005200"
    assert palette["selected"][5] == "#3a3a3a"

    expected = {
        "status_idle": "#5faf00,bold",
        "status_busy": "#ffd700,bold",
        "status_blocked": "#ff5f5f,bold",
    }
    for name, colour in expected.items():
        assert palette[name][4] == colour
        assert palette[f"{name}_focus"][4] == colour
        assert palette[f"{name}_sel"][4] == colour


def test_terminal_check_uses_outer_workspace_not_sidebar_tty(monkeypatch):
    app = _app()
    app._railmux_pane_id = "%142"
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (46, 38)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.window_size", lambda _pane: (155, 38))

    app._check_terminal_size()

    assert app._last_workspace_size == (155, 38)
    app._set_status.assert_not_called()
    app._loop.screen.get_cols_rows.assert_not_called()


def test_terminal_check_falls_back_to_tty_when_window_probe_fails(monkeypatch):
    app = _app()
    app._railmux_pane_id = "%142"
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (39, 11)
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.window_size", lambda _pane: None)

    app._check_terminal_size()

    assert app._last_workspace_size == (39, 11)
    assert app._set_status.call_args.args[1] == "error"


def test_agent_pane_warns_after_divider_makes_its_area_too_small(monkeypatch):
    app = _app()
    slot = app._primary_slot
    slot.pane_id = "%9"
    monkeypatch.setattr(
        "railmux.ui.app.tmux_ctl.pane_size", lambda _pane: (49, 11))

    app._check_agent_slot_size(slot)

    assert slot.last_size == (49, 11)
    assert slot.last_size_class == "critical"
    assert app._set_status.call_args.args[1] == "error"
    assert app._set_status.call_args.kwargs == {"force": True}
