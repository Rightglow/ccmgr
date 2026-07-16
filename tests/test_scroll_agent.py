import termios
from unittest.mock import call, patch

from railmux.scroll_agent import (
    FRAME_SECONDS,
    ScrollAccumulator,
    ScrollInput,
    apply_scroll,
    run,
)


def test_accumulator_coalesces_and_preserves_distance():
    accumulator = ScrollAccumulator()
    accumulator.feed(b"UUUUU")
    assert accumulator.drain() == 10
    assert accumulator.drain() == 0


def test_accumulator_cancels_opposite_directions():
    accumulator = ScrollAccumulator()
    accumulator.feed(b"UUUDD")
    assert accumulator.drain() == 2


def test_accumulator_preserves_tmux_wheel_distance():
    accumulator = ScrollAccumulator(lines_per_event=5)
    accumulator.feed(b"UUU")
    assert accumulator.drain() == 15


def test_apply_scroll_submits_one_aggregated_tmux_command():
    with patch("railmux.scroll_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        assert apply_scroll("%12", 18)
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "tmux", "send-keys", "-X", "-N", "18", "-t", "%12", "scroll-up",
    ]


def test_apply_scroll_down_uses_absolute_count():
    with patch("railmux.scroll_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        assert apply_scroll("%4", -6)
    assert run.call_args.args[0][-2:] == ["%4", "scroll-down"]
    assert run.call_args.args[0][4] == "6"


def test_apply_scroll_zero_does_not_spawn_tmux():
    with patch("railmux.scroll_agent.subprocess.run") as run:
        assert apply_scroll("%1", 0)
    run.assert_not_called()


def test_default_frame_budget_is_ten_fps():
    assert FRAME_SECONDS == 0.1


def test_first_wheel_input_flushes_immediately():
    state = ScrollInput("%1")

    assert state.feed(b"UUU", now=10.0, frame_seconds=0.5) == 6
    assert state.accumulator.pending == 0
    assert state.next_flush is None


def test_events_inside_frame_are_coalesced_until_deadline():
    state = ScrollInput("%1")
    assert state.feed(b"U", now=10.0, frame_seconds=0.5) == 2

    assert state.feed(b"UUD", now=10.1, frame_seconds=0.5) == 0
    assert state.accumulator.pending == 2
    assert state.next_flush == 10.5

    assert state.flush(10.5) == 2
    assert state.next_flush is None


def test_event_after_idle_frame_flushes_immediately():
    state = ScrollInput("%1")
    assert state.feed(b"U", now=10.0, frame_seconds=0.5) == 2

    assert state.feed(b"D", now=10.6, frame_seconds=0.5) == -2
    assert state.next_flush is None


def test_opposite_events_cancel_without_leaving_timer():
    state = ScrollInput("%1")
    assert state.feed(b"U", now=10.0, frame_seconds=0.5) == 2
    assert state.feed(b"UD", now=10.1, frame_seconds=0.5) == 0

    assert state.accumulator.pending == 0
    assert state.next_flush is None


def test_target_change_discards_pending_delta():
    state = ScrollInput("%1")
    assert state.feed(b"U", now=10.0, frame_seconds=0.5) == 2
    assert state.feed(b"UUU", now=10.1, frame_seconds=0.5) == 0
    assert state.accumulator.pending == 6

    assert state.feed(b"T%2\n", now=10.2, frame_seconds=0.5) == 0

    assert state.target_pane == "%2"
    assert state.accumulator.pending == 0
    assert state.next_flush is None


def test_wheel_after_target_change_applies_to_new_target():
    state = ScrollInput("%1")
    delta = state.feed(b"UT%2\nDD", now=10.0, frame_seconds=0.5)

    assert state.target_pane == "%2"
    assert delta == -4
    assert state.drain() == 0


def test_run_applies_leading_update_then_deadline_flush():
    clock = {"now": 10.0}
    select_calls = 0

    def fake_select(readable, _writable, _errors, timeout):
        nonlocal select_calls
        fd = readable[0]
        select_calls += 1
        if select_calls == 1:
            assert timeout is None
            return [fd], [], []
        if select_calls == 2:
            assert timeout is None
            clock["now"] = 10.1
            return [fd], [], []
        if select_calls == 3:
            assert abs(timeout - 0.4) < 1e-9
            clock["now"] = 10.5
            return [], [], []
        assert select_calls == 4
        assert timeout is None
        return [fd], [], []

    old_attrs = object()
    with patch("railmux.scroll_agent.sys.stdin") as stdin, \
         patch("railmux.scroll_agent.termios.tcgetattr",
               return_value=old_attrs), \
         patch("railmux.scroll_agent.termios.tcsetattr") as restore, \
         patch("railmux.scroll_agent.tty.setcbreak"), \
         patch("railmux.scroll_agent.select.select",
               side_effect=fake_select), \
         patch("railmux.scroll_agent.os.read",
               side_effect=[b"U", b"UU", b""]), \
         patch("railmux.scroll_agent.time.monotonic",
               side_effect=lambda: clock["now"]), \
         patch("railmux.scroll_agent.apply_scroll") as apply:
        stdin.fileno.return_value = 99
        run("%1", frame_seconds=0.5)

    assert apply.call_args_list == [call("%1", 2), call("%1", 4)]
    restore.assert_called_once_with(99, termios.TCSADRAIN, old_attrs)


def test_run_flushes_within_frame_residual_on_eof():
    clock = {"now": 20.0}
    select_calls = 0

    def fake_select(readable, _writable, _errors, timeout):
        nonlocal select_calls
        fd = readable[0]
        select_calls += 1
        if select_calls == 1:
            assert timeout is None
        elif select_calls == 2:
            assert timeout is None
            clock["now"] = 20.1
        else:
            assert select_calls == 3
            assert abs(timeout - 0.4) < 1e-9
            clock["now"] = 20.2
        return [fd], [], []

    with patch("railmux.scroll_agent.sys.stdin") as stdin, \
         patch("railmux.scroll_agent.termios.tcgetattr", return_value=[]), \
         patch("railmux.scroll_agent.termios.tcsetattr"), \
         patch("railmux.scroll_agent.tty.setcbreak"), \
         patch("railmux.scroll_agent.select.select",
               side_effect=fake_select), \
         patch("railmux.scroll_agent.os.read",
               side_effect=[b"U", b"UU", b""]), \
         patch("railmux.scroll_agent.time.monotonic",
               side_effect=lambda: clock["now"]), \
         patch("railmux.scroll_agent.apply_scroll") as apply:
        stdin.fileno.return_value = 99
        run("%1", frame_seconds=0.5)

    assert apply.call_args_list == [call("%1", 2), call("%1", 4)]
