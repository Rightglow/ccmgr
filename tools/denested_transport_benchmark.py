#!/usr/bin/env python3
"""Local synthetic evidence for Railmux's de-nested display transport.

This tool never uses the ambient ``TMUX`` server.  Every tmux command carries
an explicit ``-S`` path for a short-lived private server under ``/tmp``.  The
output-pipeline metric stops when the private tmux server exposes a marker via
``capture-pane``; it is not a measurement of paint in the user's terminal.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence


SCHEMA_VERSION = 1
PATHS = ("direct", "nested", "swap")


class BenchmarkError(RuntimeError):
    """A bounded, user-facing benchmark failure."""


@dataclass(frozen=True)
class ScheduleResult:
    policy: str
    event_count: int
    frame_count: int
    first_update_delay_ms: float
    model_tail_delay_ms: float
    frame_times_ms: tuple[float, ...]


def _percentile(samples: Sequence[float], quantile: float) -> float:
    if not samples:
        raise ValueError("samples must not be empty")
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def summarize(samples: Sequence[float]) -> dict[str, float]:
    """Return stable, dependency-free summary statistics in milliseconds."""
    if not samples:
        raise ValueError("samples must not be empty")
    return {
        "min": min(samples),
        "median": statistics.median(samples),
        "p95": _percentile(samples, 0.95),
        "max": max(samples),
    }


def _fixed_interval(interval_ms: float) -> Callable[[Sequence[float]], float]:
    return lambda _recent: interval_ms


def _adaptive_interval(recent: Sequence[float]) -> float:
    """A diagnostic-only policy, not a proposed Railmux implementation."""
    if not recent:
        return 50.0
    newest = recent[-1]
    active = sum(1 for value in recent if newest - value <= 50.0)
    return 33.0 if active >= 3 else 50.0


def simulate_schedule(
    event_times_ms: Sequence[float],
    policy: str,
) -> ScheduleResult:
    """Model leading-edge coalescing for a deterministic wheel-event trace.

    This models scheduler decisions only.  A returned frame is not evidence
    that tmux, SSH, or a terminal painted at that timestamp.
    """
    events = tuple(float(value) for value in event_times_ms)
    if not events or any(right < left for left, right in zip(events, events[1:])):
        raise ValueError("event times must be non-empty and monotonic")
    if policy == "disabled":
        frames = events
    else:
        interval: Callable[[Sequence[float]], float]
        if policy == "fixed-100ms":
            interval = _fixed_interval(100.0)
        elif policy == "fixed-50ms":
            interval = _fixed_interval(50.0)
        elif policy == "fixed-33ms":
            interval = _fixed_interval(33.0)
        elif policy == "adaptive-prototype":
            interval = _adaptive_interval
        else:
            raise ValueError(f"unknown scheduling policy: {policy}")

        frame_list: list[float] = []
        recent: list[float] = []
        pending = False
        deadline: float | None = None
        for event in events:
            if pending and deadline is not None and deadline <= event:
                frame_list.append(deadline)
                pending = False
                deadline = None
            recent.append(event)
            recent = [value for value in recent if event - value <= 100.0]
            if not frame_list:
                frame_list.append(event)
                continue
            pending = True
            candidate = frame_list[-1] + interval(recent)
            deadline = candidate if deadline is None else min(deadline, candidate)
        if pending and deadline is not None:
            frame_list.append(deadline)
        frames = tuple(frame_list)

    return ScheduleResult(
        policy=policy,
        event_count=len(events),
        frame_count=len(frames),
        first_update_delay_ms=frames[0] - events[0],
        model_tail_delay_ms=max(0.0, frames[-1] - events[-1]),
        frame_times_ms=tuple(round(value, 3) for value in frames),
    )


class PrivateTmux:
    """One explicitly addressed tmux server; ambient sessions are unreachable."""

    def __init__(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows
        self.root = Path(tempfile.mkdtemp(prefix="rxd-", dir="/tmp"))
        self.root.chmod(0o700)
        self.socket = self.root / "s"
        self.server_pid: int | None = None

    def _argv(self, *args: str, with_config: bool = False) -> list[str]:
        argv = ["tmux", "-S", str(self.socket)]
        if with_config:
            argv += ["-f", "/dev/null"]
        return [*argv, *args]

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: float = 10.0,
        with_config: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # Explicit -S is the safety boundary.  Removing ambient values also
        # prevents tmux's nesting guard from confusing control commands.
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        result = subprocess.run(
            self._argv(*args, with_config=with_config),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise BenchmarkError(
                f"private tmux command failed ({args[0]}): {detail[:200]}"
            )
        return result

    def output(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def start(self) -> None:
        self.run(
            "new-session", "-d", "-x", str(self.columns), "-y", str(self.rows),
            "-s", "bench-control", "sleep 600", with_config=True,
        )
        self.server_pid = int(self.output(
            "display-message", "-p", "-t", "bench-control", "#{pid}"))
        reported = self.output(
            "display-message", "-p", "-t", "bench-control", "#{socket_path}")
        if Path(reported) != self.socket:
            raise BenchmarkError("private tmux socket identity did not validate")
        self.run("set-option", "-g", "history-limit", "100000")
        self.run("set-option", "-g", "status", "off")

    def close(self) -> None:
        try:
            # Cleanup is also explicitly socket-scoped; never call bare tmux.
            self.run("kill-server", check=False, timeout=3.0)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    def new_shell_session(self, name: str) -> str:
        self.run(
            "new-session", "-d", "-x", str(self.columns), "-y", str(self.rows),
            "-s", name, "stty -echo; exec /bin/sh",
        )
        return self.output(
            "display-message", "-p", "-t", name, "#{pane_id}")

    def new_command_session(self, name: str, command: str) -> str:
        self.run(
            "new-session", "-d", "-x", str(self.columns), "-y", str(self.rows),
            "-s", name, command,
        )
        return self.output(
            "display-message", "-p", "-t", name, "#{pane_id}")

    def kill_sessions(self, names: Sequence[str]) -> None:
        for name in names:
            self.run("kill-session", "-t", name, check=False)

    def wait_until(self, predicate: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.002)
        raise BenchmarkError("private tmux observation timed out")

    def nested_command(self, agent_session: str) -> str:
        return (
            f"TMUX= exec {shlex.quote(shutil.which('tmux') or 'tmux')} "
            f"-S {shlex.quote(str(self.socket))} attach-session "
            f"-t {shlex.quote(agent_session)}"
        )

    def attached_count(self, session: str) -> int:
        raw = self.output(
            "display-message", "-p", "-t", session, "#{session_attached}")
        return int(raw)

    def geometry(self, pane: str) -> str:
        return self.output(
            "display-message", "-p", "-t", pane,
            "#{pane_width}x#{pane_height}:#{window_width}x#{window_height}",
        )

    def window_id(self, pane: str) -> str:
        return self.output(
            "display-message", "-p", "-t", pane, "#{window_id}")

    def pane_pid(self, pane: str) -> int:
        return int(self.output(
            "display-message", "-p", "-t", pane, "#{pane_pid}"))

    def capture(self, pane: str) -> str:
        return self.run("capture-pane", "-p", "-t", pane).stdout

    def cpu_ticks(self) -> int | None:
        if self.server_pid is None or platform.system() != "Linux":
            return None
        try:
            fields = Path(f"/proc/{self.server_pid}/stat").read_text(
                encoding="utf-8").split()
            return int(fields[13]) + int(fields[14])
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def process_tree_ticks(root_pid: int) -> int | None:
        """Best-effort Linux CPU ticks for a synthetic pane process tree."""
        if platform.system() != "Linux":
            return None
        pending = [root_pid]
        seen: set[int] = set()
        total = 0
        try:
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                stat = Path(f"/proc/{pid}/stat").read_text(
                    encoding="utf-8").split()
                total += int(stat[13]) + int(stat[14])
                children = Path(
                    f"/proc/{pid}/task/{pid}/children").read_text(
                        encoding="utf-8").split()
                pending.extend(int(child) for child in children)
        except (OSError, IndexError, ValueError):
            return None
        return total


def _producer_command(lines: int, width: int, marker_path: Path) -> str:
    payload = "x" * max(1, width - 8)
    code = (
        "import sys,time;"
        f"marker=open({str(marker_path)!r},encoding='utf-8').read();"
        f"[sys.stdout.write(f'{{i:06d}} {payload}\\n') for i in range({lines})];"
        "sys.stdout.write(marker+'\\n');sys.stdout.flush();time.sleep(0.2)"
    )
    return shlex.join((sys.executable, "-c", code))


def _send_command(server: PrivateTmux, pane: str, command: str) -> None:
    server.run("send-keys", "-t", pane, "-l", command)
    server.run("send-keys", "-t", pane, "Enter")


def _path_setup(
    server: PrivateTmux, path: str, run_number: int,
) -> tuple[str, str, tuple[str, ...], int]:
    prefix = f"b{run_number}-{path}"
    if path == "direct":
        producer = server.new_shell_session(prefix)
        return producer, producer, (prefix,), 1

    agent_name = f"{prefix}-agent"
    producer = server.new_shell_session(agent_name)
    display_name = f"{prefix}-display"
    if path == "nested":
        observer = server.new_command_session(
            display_name, server.nested_command(agent_name))
        server.wait_until(lambda: server.attached_count(agent_name) == 1, 3.0)
        return producer, observer, (display_name, agent_name), 2
    if path == "swap":
        placeholder = server.new_shell_session(display_name)
        server.run("swap-pane", "-s", producer, "-t", placeholder)
        return producer, producer, (display_name, agent_name), 2
    raise ValueError(path)


def run_output_trials(
    server: PrivateTmux,
    *,
    runs: int,
    lines: int,
    width: int,
    timeout: float,
) -> dict[str, object]:
    raw: dict[str, list[dict[str, object]]] = {path: [] for path in PATHS}
    for run_number in range(runs):
        # Rotate the order so thermal/cache drift is not assigned to one path.
        order = PATHS[run_number % len(PATHS):] + PATHS[:run_number % len(PATHS)]
        for path in order:
            producer, observer, sessions, path_panes = _path_setup(
                server, path, run_number)
            marker = f"RAILMUX_BENCH_{run_number}_{path}_{time.monotonic_ns()}"
            # Keep the sought value out of the interactive command text.  Even
            # if a shell re-enabled terminal echo, capture-pane could not match
            # until the producer reads and emits this out-of-band value.
            marker_path = server.root / f"marker-{run_number}-{path}"
            marker_path.write_text(marker, encoding="utf-8")
            producer_pid = server.pane_pid(producer)
            producer_ticks_before = server.process_tree_ticks(producer_pid)
            nested_client_pid = server.pane_pid(observer) if path == "nested" else None
            nested_ticks_before = (
                server.process_tree_ticks(nested_client_pid)
                if nested_client_pid is not None else None
            )
            before_ticks = server.cpu_ticks()
            started = time.perf_counter_ns()
            _send_command(
                server, producer, _producer_command(lines, width, marker_path))
            server.wait_until(lambda: marker in server.capture(observer), timeout)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            after_ticks = server.cpu_ticks()
            producer_ticks_after = server.process_tree_ticks(producer_pid)
            nested_ticks_after = (
                server.process_tree_ticks(nested_client_pid)
                if nested_client_pid is not None else None
            )
            raw[path].append({
                "marker_observation_ms": round(elapsed_ms, 3),
                "tmux_server_cpu_ticks": (
                    None if before_ticks is None or after_ticks is None
                    else after_ticks - before_ticks
                ),
                "display_geometry": server.geometry(observer),
                "path_pane_count": path_panes,
                "synthetic_producer_tree_cpu_ticks": (
                    None
                    if producer_ticks_before is None or producer_ticks_after is None
                    else producer_ticks_after - producer_ticks_before
                ),
                "nested_client_cpu_ticks": (
                    None
                    if nested_ticks_before is None or nested_ticks_after is None
                    else nested_ticks_after - nested_ticks_before
                ),
            })
            server.kill_sessions(sessions)

    summary: dict[str, object] = {}
    for path, samples in raw.items():
        latencies = [float(sample["marker_observation_ms"]) for sample in samples]
        ticks = [
            int(sample["tmux_server_cpu_ticks"])
            for sample in samples
            if sample["tmux_server_cpu_ticks"] is not None
        ]
        producer_ticks = [
            int(sample["synthetic_producer_tree_cpu_ticks"])
            for sample in samples
            if sample["synthetic_producer_tree_cpu_ticks"] is not None
        ]
        nested_ticks = [
            int(sample["nested_client_cpu_ticks"])
            for sample in samples
            if sample["nested_client_cpu_ticks"] is not None
        ]
        summary[path] = {
            "marker_observation_ms": {
                key: round(value, 3) for key, value in summarize(latencies).items()
            },
            "tmux_server_cpu_ticks_total": sum(ticks) if ticks else None,
            "synthetic_producer_tree_cpu_ticks_total": (
                sum(producer_ticks) if producer_ticks else None
            ),
            "nested_client_cpu_ticks_total": (
                sum(nested_ticks) if nested_ticks else None
            ),
            "cpu_interpretation": (
                "aggregate only; do not compare when totals approach clock-tick resolution"
            ),
        }
    return {"raw_samples": raw, "summary": summary}


def run_switch_trials(
    server: PrivateTmux, *, iterations: int,
) -> dict[str, object]:
    samples: dict[str, list[float]] = {"nested": [], "swap": []}

    # Nested: respawn one display pane with the other nested client and wait
    # until tmux reports the target session attached.
    server.new_shell_session("switch-nested-a")
    server.new_shell_session("switch-nested-b")
    nested_display = server.new_command_session(
        "switch-nested-display", server.nested_command("switch-nested-a"))
    server.wait_until(lambda: server.attached_count("switch-nested-a") == 1, 3.0)
    for index in range(iterations):
        target = "switch-nested-b" if index % 2 == 0 else "switch-nested-a"
        started = time.perf_counter_ns()
        server.run(
            "respawn-pane", "-k", "-t", nested_display,
            server.nested_command(target),
        )
        server.wait_until(lambda target=target: server.attached_count(target) == 1, 3.0)
        samples["nested"].append(
            (time.perf_counter_ns() - started) / 1_000_000)
    server.kill_sessions((
        "switch-nested-display", "switch-nested-a", "switch-nested-b"))

    # Swap: return the current real pane home, then move the other real pane
    # into the same display window.  This matches the two server operations in
    # an A->B swap switch, but does not exercise Railmux's Python transaction.
    swap_a = server.new_shell_session("switch-swap-a")
    swap_b = server.new_shell_session("switch-swap-b")
    placeholder = server.new_shell_session("switch-swap-display")
    display_window = server.window_id(placeholder)
    server.run("swap-pane", "-s", swap_a, "-t", placeholder)
    current = swap_a
    for index in range(iterations):
        target = swap_b if index % 2 == 0 else swap_a
        started = time.perf_counter_ns()
        server.run("swap-pane", "-s", current, "-t", placeholder)
        server.run("swap-pane", "-s", target, "-t", placeholder)
        if server.window_id(target) != display_window:
            raise BenchmarkError("swap switch geometry/identity did not validate")
        samples["swap"].append(
            (time.perf_counter_ns() - started) / 1_000_000)
        current = target
    server.run("swap-pane", "-s", current, "-t", placeholder)
    server.kill_sessions((
        "switch-swap-display", "switch-swap-a", "switch-swap-b"))

    return {
        "raw_samples_ms": {
            key: [round(value, 3) for value in values]
            for key, values in samples.items()
        },
        "summary_ms": {
            key: {name: round(value, 3) for name, value in summarize(values).items()}
            for key, values in samples.items()
        },
    }


def scheduling_report() -> dict[str, object]:
    # 31 events over 240 ms resembles a short wheel burst while remaining
    # deterministic and independent of host scheduling.
    events = tuple(float(value) for value in range(0, 241, 8))
    policies = (
        "disabled", "fixed-100ms", "fixed-50ms", "fixed-33ms",
        "adaptive-prototype",
    )
    return {
        "scope": "simulated scheduler decisions; not tmux/SSH/terminal frames",
        "event_times_ms": events,
        "policies": {
            policy: asdict(simulate_schedule(events, policy))
            for policy in policies
        },
    }


def build_report(args: argparse.Namespace) -> dict[str, object]:
    tmux = shutil.which("tmux")
    if tmux is None:
        raise BenchmarkError("tmux is not installed")
    version = subprocess.run(
        [tmux, "-V"], capture_output=True, text=True, check=False,
    ).stdout.strip()
    server = PrivateTmux(args.columns, args.rows)
    try:
        server.start()
        output = run_output_trials(
            server, runs=args.runs, lines=args.lines, width=args.line_width,
            timeout=args.timeout,
        )
        switching = run_switch_trials(server, iterations=args.switch_iterations)
    finally:
        server.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_scope": {
            "output_pipeline": (
                "producer command dispatch to private tmux capture-pane marker observation; "
                "not client terminal paint"
            ),
            "switching": (
                "server-side command plus identity-observation latency; not perceived switch"
            ),
            "excluded": (
                "real provider and Railmux process CPU/behavior, terminal paint, "
                "network/SSH transport, clipboard, mouse, alternate-screen UX, and macOS"
            ),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "tmux": version,
            "ssh_environment_detected": any(
                os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
            ),
            "clock_ticks_per_second": (
                os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else None
            ),
        },
        "dataset": {
            "runs_per_path": args.runs,
            "lines_per_burst": args.lines,
            "line_width": args.line_width,
            "geometry": f"{args.columns}x{args.rows}",
            "switch_iterations_per_path": args.switch_iterations,
        },
        "output_pipeline": output,
        "switching": switching,
        "scheduling_model": scheduling_report(),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--lines", type=int, default=2500)
    parser.add_argument("--line-width", type=int, default=96)
    parser.add_argument("--columns", type=int, default=112)
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--switch-iterations", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    for name in ("runs", "lines", "line_width", "columns", "rows",
                 "switch_iterations"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
    except (BenchmarkError, OSError, subprocess.SubprocessError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
