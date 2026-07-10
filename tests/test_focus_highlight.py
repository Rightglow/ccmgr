from pathlib import Path
from unittest.mock import MagicMock

import pytest
import urwid

from ccmgr.models import Project, SessionMeta
from ccmgr.ui.app import App, _FocusAwareFrame
from ccmgr.ui.sessions_pane import _SessionRow


def _canvas_attrs(canvas) -> list[str | None]:
    return [
        attr
        for row in canvas.content()
        for attr, _charset, text in row
        for _ in text
    ]


def _row_attrs(canvas, row_index: int) -> list[str | None]:
    return [
        attr
        for attr, _charset, text in list(canvas.content())[row_index]
        for _ in text
    ]


def test_inactive_frame_suppresses_focus_but_keeps_selection_and_status():
    project = Project(Path("/tmp/p"), "-tmp-p", Path("/tmp/meta"), 1, 1.0)
    session = SessionMeta(
        project=project,
        session_id="1" * 36,
        jsonl_path=Path("/tmp/session.jsonl"),
        title="Selected",
        message_count=1,
        token_total=1,
        last_mtime=1.0,
        status="busy",
    )
    row = _SessionRow(session, is_selected=True)
    frame = _FocusAwareFrame(urwid.ListBox(urwid.SimpleFocusListWalker([row])))

    assert "focus" in _canvas_attrs(frame.render((30, 4), focus=True))

    frame.set_window_active(False)
    attrs = _canvas_attrs(frame.render((30, 4), focus=True))
    assert "focus" not in attrs
    assert "pane_focus" not in attrs
    assert "selected" in attrs
    assert "status_busy_sel" in attrs


def test_sidebar_gutter_separates_pane_edge_from_tmux_divider():
    pane = urwid.AttrMap(
        urwid.LineBox(urwid.SolidFill(" ")),
        "pane",
        focus_map="pane_focus",
    )
    padded = urwid.Padding(pane, right=1)
    canvas = padded.render((20, 5), focus=True)

    for row_index in range(canvas.rows()):
        attrs = _row_attrs(canvas, row_index)
        assert attrs[-2] == "pane_focus"
        assert attrs[-1] is None


def test_focus_reports_are_consumed_and_update_divider(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._ccmgr_has_focus = True
    app._divider_active = None

    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "ccmgr.ui.app.tmux_ctl.set_window_border_style", set_border)

    assert app._filter_input(["focus out", "x"], []) == ["x"]
    assert app._frame._window_active is False
    assert set_border.call_args.args == ("fg=cyan",)

    assert app._filter_input(["focus in"], []) == []
    assert app._frame._window_active is True
    assert set_border.call_args.args == ("fg=colour240",)


def test_double_click_prepaints_focus_before_tmux_settles(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._ccmgr_has_focus = True
    app._divider_active = None
    app._right_pane_id = "%2"
    app._double_focus_alarm = None
    app._double_focus_visual_pending = False
    loop = MagicMock()
    alarm = object()
    loop.set_alarm_in.return_value = alarm
    app._loop = loop
    set_border = MagicMock(return_value=True)
    select_pane = MagicMock(return_value=True)
    monkeypatch.setattr(
        "ccmgr.ui.app.tmux_ctl.set_window_border_style", set_border)
    monkeypatch.setattr("ccmgr.ui.app.tmux_ctl.select_pane", select_pane)

    app._schedule_right_pane_focus_after_double()

    delay, callback = loop.set_alarm_in.call_args.args
    assert delay == App._DOUBLE_CLICK_FOCUS_DELAY
    assert app._double_focus_alarm is alarm
    assert app._double_focus_visual_pending is True
    assert app._ccmgr_has_focus is False
    assert app._frame._window_active is False
    set_border.assert_called_once_with("fg=cyan")
    loop.draw_screen.assert_called_once_with()
    select_pane.assert_not_called()

    callback(loop, None)

    select_pane.assert_called_once_with("%2")
    assert app._double_focus_alarm is None
    assert app._double_focus_visual_pending is False
    assert app._ccmgr_has_focus is False
    assert loop.draw_screen.call_count == 2


def test_failed_delayed_focus_restores_sidebar(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._ccmgr_has_focus = True
    app._divider_active = None
    app._right_pane_id = "%2"
    app._double_focus_alarm = None
    app._double_focus_visual_pending = False
    app._loop = MagicMock()
    app._loop.set_alarm_in.return_value = object()
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "ccmgr.ui.app.tmux_ctl.set_window_border_style", set_border)
    monkeypatch.setattr(
        "ccmgr.ui.app.tmux_ctl.select_pane", MagicMock(return_value=False))

    app._schedule_right_pane_focus_after_double()
    callback = app._loop.set_alarm_in.call_args.args[1]
    callback(app._loop, None)

    assert app._double_focus_visual_pending is False
    assert app._ccmgr_has_focus is True
    assert app._frame._window_active is True
    assert set_border.call_args_list[-1].args == ("fg=colour240",)


def test_new_session_intent_cancels_pending_double_focus(monkeypatch):
    app = App.__new__(App)
    app._frame = _FocusAwareFrame(urwid.SolidFill(" "))
    app._frame.set_window_active(False)
    app._ccmgr_has_focus = False
    app._divider_active = True
    app._loop = MagicMock()
    alarm = object()
    app._double_focus_alarm = alarm
    app._double_focus_visual_pending = True
    app._in_history_mode = True
    app._restore_state = object()
    app._launch_resume = MagicMock()
    session = MagicMock()
    set_border = MagicMock(return_value=True)
    monkeypatch.setattr(
        "ccmgr.ui.app.tmux_ctl.set_window_border_style", set_border)

    app._on_session_select(session, steal_focus=False)

    app._loop.remove_alarm.assert_called_once_with(alarm)
    assert app._double_focus_alarm is None
    assert app._double_focus_visual_pending is False
    assert app._ccmgr_has_focus is True
    assert app._frame._window_active is True
    set_border.assert_called_once_with("fg=colour240")
    app._loop.draw_screen.assert_called_once_with()
    app._launch_resume.assert_called_once_with(session, steal_focus=False)



def test_pending_restore_consumes_state_after_success(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: state_path))
    app = App.__new__(App)
    app._pending_restore_state = {"right_kind": "empty"}
    app._restore_right_pane = MagicMock()

    app._restore_pending_right_pane(None, None)

    app._restore_right_pane.assert_called_once_with({"right_kind": "empty"})
    assert app._pending_restore_state is None
    assert not state_path.exists()


def test_pending_restore_keeps_state_when_restore_raises(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(
        App, "_state_path", staticmethod(lambda: state_path))
    app = App.__new__(App)
    app._pending_restore_state = {"right_kind": "claude"}
    app._restore_right_pane = MagicMock(side_effect=RuntimeError("failed"))

    with pytest.raises(RuntimeError, match="failed"):
        app._restore_pending_right_pane(None, None)

    assert state_path.exists()
    assert app._pending_restore_state is None


def test_scroll_configuration_is_coalesced_and_deferred():
    app = App.__new__(App)
    app._loop = MagicMock()
    app._pending_scroll_session = None
    app._scroll_alarm_pending = False
    app._right_pane_claude = "cc-b"
    app._configure_scroll_acceleration = MagicMock()

    app._schedule_scroll_acceleration("cc-a")
    app._schedule_scroll_acceleration("cc-b")

    app._loop.set_alarm_in.assert_called_once_with(
        0.05, app._apply_pending_scroll_acceleration)
    app._configure_scroll_acceleration.assert_not_called()

    app._apply_pending_scroll_acceleration(None, None)

    app._configure_scroll_acceleration.assert_called_once_with("cc-b")
    assert app._pending_scroll_session is None
    assert app._scroll_alarm_pending is False


def test_pending_project_load_is_deferred_and_stale_safe():
    project = Project(
        Path("/tmp/p"),
        "-tmp-p",
        Path("/tmp/meta"),
        1,
        1.0,
    )
    app = App.__new__(App)
    app._pending_project = project
    app._selected_project = project
    app._on_project_select = MagicMock()

    app._load_pending_project(None, None)

    app._on_project_select.assert_called_once_with(project)
    assert app._pending_project is None

    app._pending_project = project
    app._selected_project = None

    app._load_pending_project(None, None)
    app._on_project_select.assert_called_once_with(project)
    assert app._pending_project is None
