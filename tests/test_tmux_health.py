from __future__ import annotations

import json
import stat

from railmux import tmux_health


def test_failure_watchdog_requires_consecutive_due_failures():
    watchdog = tmux_health.FailureWatchdog.starting(
        0.0, interval=5.0, failure_limit=3
    )

    assert not watchdog.due(4.9)
    assert not watchdog.observe(False, 5.0)
    assert not watchdog.observe(False, 10.0)
    assert not watchdog.observe(True, 15.0)
    assert watchdog.consecutive_failures == 0
    assert not watchdog.observe(False, 20.0)
    assert not watchdog.observe(False, 25.0)
    assert watchdog.observe(False, 30.0)
    assert watchdog.consecutive_failures == 3


def test_incident_round_trip_is_private_and_contains_no_tmux_identity(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("railmux.tmux_health.time.time", lambda: 1_000)

    assert tmux_health.record_incident(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=3,
    )

    incident = tmux_health.read_last_incident()
    assert incident == tmux_health.TmuxIncident(
        1_000, "remote-display", "remote-display-watchdog-timeout", 3
    )
    path = tmp_path / "railmux" / "last-tmux-incident-railmux.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert set(payload) == {
        "schema_version", "recorded_at", "component", "reason",
        "consecutive_failures",
    }


def test_clean_exit_round_trip_is_private_exact_and_one_shot(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("railmux.tmux_health.time.time", lambda: 1_000)

    assert tmux_health.record_clean_exit(server_pid=123, session_id="$4")

    path = tmp_path / "railmux" / "clean-tmux-exit-railmux.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not tmux_health.consume_clean_exit(
        server_pid=123, session_id="$5")
    assert not path.exists()

    assert tmux_health.record_clean_exit(server_pid=123, session_id="$4")
    assert tmux_health.consume_clean_exit(server_pid=123, session_id="$4")
    assert not tmux_health.consume_clean_exit(
        server_pid=123, session_id="$4")


def test_clean_exit_rejects_expired_or_invalid_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    now = [1_000]
    monkeypatch.setattr("railmux.tmux_health.time.time", lambda: now[0])

    assert not tmux_health.record_clean_exit(server_pid=0, session_id="$4")
    assert not tmux_health.record_clean_exit(server_pid=123, session_id="name")
    assert tmux_health.record_clean_exit(server_pid=123, session_id="$4")
    now[0] += 31
    assert not tmux_health.consume_clean_exit(
        server_pid=123, session_id="$4")


def test_soft_exit_is_private_exact_broadcast_and_short_lived(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    now = [1_000]
    monkeypatch.setattr("railmux.tmux_health.time.time", lambda: now[0])

    assert not tmux_health.record_soft_exit(server_pid=0, session_id="$4")
    assert not tmux_health.record_soft_exit(server_pid=123, session_id="name")
    assert tmux_health.record_soft_exit(server_pid=123, session_id="$4")

    path = tmp_path / "railmux" / "soft-tmux-exit-railmux.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not tmux_health.soft_exit_intended(
        server_pid=123, session_id="$5")
    assert tmux_health.soft_exit_intended(server_pid=123, session_id="$4")
    assert tmux_health.soft_exit_intended(server_pid=123, session_id="$4")
    now[0] += 31
    assert not tmux_health.soft_exit_intended(
        server_pid=123, session_id="$4")


def test_incident_reader_rejects_unknown_or_unbounded_content(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    root = tmp_path / "railmux"
    root.mkdir(mode=0o700)
    path = root / "last-tmux-incident-railmux.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "recorded_at": 100,
        "component": "unknown",
        "reason": "arbitrary raw error",
        "consecutive_failures": 999,
    }))
    monkeypatch.setattr("railmux.tmux_health.time.time", lambda: 1_000)

    assert tmux_health.read_last_incident() is None


def test_incident_age_is_coarse():
    assert tmux_health.incident_age(970, now=1_000) == "less than a minute ago"
    assert tmux_health.incident_age(800, now=1_000) == "3 minutes ago"
    assert tmux_health.incident_age(1_000, now=10_000) == "2 hours ago"
