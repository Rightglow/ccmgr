from unittest.mock import patch

from ccmgr.scroll_agent import ScrollAccumulator, ScrollInput, apply_scroll


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
    with patch("ccmgr.scroll_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        assert apply_scroll("%12", 18)
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "tmux", "send-keys", "-X", "-N", "18", "-t", "%12", "scroll-up",
    ]


def test_apply_scroll_down_uses_absolute_count():
    with patch("ccmgr.scroll_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        assert apply_scroll("%4", -6)
    assert run.call_args.args[0][-2:] == ["%4", "scroll-down"]
    assert run.call_args.args[0][4] == "6"


def test_apply_scroll_zero_does_not_spawn_tmux():
    with patch("ccmgr.scroll_agent.subprocess.run") as run:
        assert apply_scroll("%1", 0)
    run.assert_not_called()


def test_target_change_discards_pending_delta():
    state = ScrollInput("%1")
    state.feed(b"UUU", now=10.0, frame_seconds=0.5)
    assert state.accumulator.pending == 6

    state.feed(b"T%2\n", now=10.1, frame_seconds=0.5)

    assert state.target_pane == "%2"
    assert state.accumulator.pending == 0
    assert state.next_flush is None


def test_wheel_after_target_change_applies_to_new_target():
    state = ScrollInput("%1")
    state.feed(b"UT%2\nDD", now=10.0, frame_seconds=0.5)
    assert state.target_pane == "%2"
    assert state.drain() == -4
