from __future__ import annotations

import pytest

from tools.denested_transport_benchmark import (
    PrivateTmux,
    _producer_command,
    _percentile,
    parse_args,
    scheduling_report,
    simulate_schedule,
    summarize,
)


def test_summary_uses_nearest_rank_p95():
    samples = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert summarize(samples) == {
        "min": 1.0,
        "median": 3.0,
        "p95": 5.0,
        "max": 5.0,
    }
    assert _percentile([1.0, 2.0], 0.95) == 2.0


def test_schedule_has_immediate_leading_update_and_bounded_tail():
    events = [0.0, 8.0, 16.0, 24.0, 32.0, 40.0]
    fixed = simulate_schedule(events, "fixed-100ms")
    disabled = simulate_schedule(events, "disabled")

    assert fixed.first_update_delay_ms == 0.0
    assert fixed.frame_count == 2
    assert fixed.model_tail_delay_ms == 60.0
    assert disabled.frame_count == len(events)
    assert disabled.model_tail_delay_ms == 0.0


def test_faster_models_trade_more_frames_for_tighter_tail_bound():
    events = list(range(0, 241, 8))
    slow = simulate_schedule(events, "fixed-100ms")
    medium = simulate_schedule(events, "fixed-50ms")
    fast = simulate_schedule(events, "fixed-33ms")

    assert slow.frame_count < medium.frame_count < fast.frame_count
    # The exact tail depends on burst/deadline phase; each policy still keeps
    # it within its declared fixed interval.
    assert slow.model_tail_delay_ms <= 100.0
    assert medium.model_tail_delay_ms <= 50.0
    assert fast.model_tail_delay_ms <= 33.0


def test_adaptive_is_explicitly_diagnostic_only_in_report():
    report = scheduling_report()
    assert "simulated" in report["scope"]
    assert "adaptive-prototype" in report["policies"]
    assert report["policies"]["adaptive-prototype"]["first_update_delay_ms"] == 0.0


def test_schedule_rejects_unknown_or_non_monotonic_input():
    with pytest.raises(ValueError, match="monotonic"):
        simulate_schedule([1.0, 0.0], "fixed-50ms")
    with pytest.raises(ValueError, match="unknown"):
        simulate_schedule([0.0], "magic")


def test_cli_rejects_non_positive_workloads():
    with pytest.raises(SystemExit):
        parse_args(["--runs", "0"])


def test_private_tmux_always_builds_explicit_socket_commands():
    server = PrivateTmux(112, 40)
    try:
        argv = server._argv("list-sessions")
        assert argv[:3] == ["tmux", "-S", str(server.socket)]
        assert server.socket.parent == server.root
        assert str(server.root).startswith("/tmp/rxd-")
    finally:
        server.close()


def test_producer_command_does_not_contain_sought_marker(tmp_path):
    marker = "RAILMUX_BENCH_SECRET_MARKER"
    marker_path = tmp_path / "marker"
    marker_path.write_text(marker, encoding="utf-8")

    command = _producer_command(10, 80, marker_path)

    assert str(marker_path) in command
    assert marker not in command
