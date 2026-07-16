"""Opt-in smoke coverage against a real, isolated tmux server."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from railmux import tmux_ctl
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.ui.workspace import AgentWorkspace, DisplayTransportKind


pytestmark = pytest.mark.skipif(
    os.environ.get("RAILMUX_RUN_TMUX_INTEGRATION") != "1",
    reason="set RAILMUX_RUN_TMUX_INTEGRATION=1 to run real tmux smoke tests",
)


@pytest.fixture
def isolated_tmux(monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    # Unix-domain socket paths are commonly capped at 104–108 bytes. macOS's
    # default TMPDIR lives under /private/var/folders and can exceed that limit
    # after tmux appends its own components. Keep the explicit socket pathname
    # short while retaining a private directory for isolation.
    socket_root = Path(tempfile.mkdtemp(prefix="rx-", dir="/tmp"))
    socket_root.chmod(0o700)
    socket_path = str(socket_root / "s")
    session_name = "railmux"
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    try:
        subprocess.run(
            [
                "tmux", "-S", socket_path, "-f", "/dev/null",
                "new-session", "-d", "-s", session_name, "sleep 60",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        server_pid = subprocess.check_output(
            [
                "tmux", "-S", socket_path, "display-message", "-p",
                "-t", session_name, "#{pid}",
            ],
            text=True,
        ).strip()
        pane_id = subprocess.check_output(
            [
                "tmux", "-S", socket_path, "display-message", "-p",
                "-t", session_name, "#{pane_id}",
            ],
            text=True,
        ).strip()

        # Bare tmux commands in tmux_ctl now resolve only to this private socket.
        monkeypatch.setenv("TMUX", f"{socket_path},{server_pid},0")
        monkeypatch.setenv("TMUX_PANE", pane_id)
        tmux_ctl.tmux_version.cache_clear()
        yield session_name, pane_id, socket_path
    finally:
        subprocess.run(
            ["tmux", "-S", socket_path, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        tmux_ctl.tmux_version.cache_clear()
        shutil.rmtree(socket_root, ignore_errors=True)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_real_tmux_session_split_attach_persistence_and_styles(isolated_tmux):
    display_session, primary_pane, socket_path = isolated_tmux
    agent_session = "railmux-smoke-agent"

    assert tmux_ctl.session_exists(display_session)
    assert tmux_ctl.pane_size(primary_pane) is not None
    assert tmux_ctl.window_size(primary_pane) is not None

    original_border = subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip()
    assert tmux_ctl.set_window_border_styles("fg=#5faf00", "fg=#5faf00")
    assert subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip() == "fg=#5faf00"

    created, reason = tmux_ctl.new_detached_session(
        agent_session, "sleep 60")
    assert created, reason
    assert tmux_ctl.session_exists(agent_session)

    attach_command = (
        f"TMUX= exec tmux -S {shlex.quote(socket_path)} "
        f"attach-session -t {agent_session}"
    )
    display_pane = tmux_ctl.split_window_h(
        attach_command,
        target=primary_pane,
        size_percent=60,
        detached=True,
    )
    assert display_pane is not None
    assert tmux_ctl.pane_alive(display_pane)
    assert _wait_until(
        lambda: tmux_ctl.session_attached_count(agent_session) == 1)

    # Removing only the display pane must detach, never kill, the background
    # agent session that owns the actual process.
    assert tmux_ctl.kill_pane(display_pane)
    assert _wait_until(
        lambda: tmux_ctl.session_attached_count(agent_session) == 0)
    assert tmux_ctl.session_exists(agent_session)

    assert tmux_ctl.set_window_border_styles(original_border, original_border)
    restored_border = subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip()
    # tmux versions disagree on how an unset inherited style is rendered:
    # 3.4 keeps it empty, while 2.7 may canonicalize it to ``default``.
    assert restored_border == original_border or (
        not original_border and restored_border == "default"
    )
    assert tmux_ctl.kill_session(agent_session)


def test_real_tmux_swap_recovery_direct_kill_and_fallback(isolated_tmux):
    display_session, owner_pane, socket_path = isolated_tmux
    outer_id = tmux_ctl.current_session_id()
    assert outer_id is not None
    display_window = tmux_ctl.pane_identity(owner_pane).window_id
    workspace = AgentWorkspace()
    manager = AgentDisplayTransport(
        workspace,
        "swap",
        auto_launched=True,
        outer_session_name=display_session,
        outer_session_id=outer_id,
        owner_pane_id=owner_pane,
    )

    for name in ("agent-a", "agent-b"):
        created, reason = tmux_ctl.new_detached_session(name, "sleep 60")
        assert created, reason
    pane_a = tmux_ctl.session_pane_id("agent-a")
    pane_b = tmux_ctl.session_pane_id("agent-b")
    assert pane_a and pane_b
    pid_a = tmux_ctl.pane_identity(pane_a).pane_pid
    pid_b = tmux_ctl.pane_identity(pane_b).pane_pid

    first = manager.attach(workspace.primary, "agent-a")
    assert first.ok and first.kind == DisplayTransportKind.SWAP
    assert tmux_ctl.pane_identity(pane_a).pane_pid == pid_a
    # A linked window may format under the keeper's session ID; window ID is
    # the unambiguous physical display location.
    assert tmux_ctl.pane_identity(pane_a).window_id == display_window

    assert manager.attach(workspace.primary, "agent-b").ok
    assert tmux_ctl.pane_identity(pane_a).pane_pid == pid_a
    assert tmux_ctl.pane_identity(pane_b).pane_pid == pid_b
    assert manager.attach(workspace.primary, "agent-a").ok
    assert tmux_ctl.pane_identity(pane_a).pane_pid == pid_a
    markers_before_kill = tmux_ctl.list_window_user_options(
        ("@railmux_swap_primary", "@railmux_swap_secondary"))
    assert markers_before_kill is not None
    assert any(row[1] for row in markers_before_kill)

    # The group keeper must retain the real pane if the outer Railmux session
    # is directly killed. Recovery keys off the missing immutable outer ID,
    # returns A home, and removes the keeper/display window.
    assert tmux_ctl.kill_session(display_session)
    assert _wait_until(lambda: outer_id not in (tmux_ctl.session_ids() or ()))
    assert tmux_ctl.pane_identity(pane_a).pane_pid == pid_a
    report = recover_interrupted_swaps()
    assert report.repaired == 1
    assert report.unresolved == 0
    assert tmux_ctl.session_pane_id("agent-a") == pane_a
    assert tmux_ctl.pane_identity(pane_a).pane_pid == pid_a
    assert tmux_ctl.session_exists("agent-b")

    # Recreate an outer session on the same private server and prove an
    # independently attached agent takes the nested fallback.
    subprocess.run(
        [
            "tmux", "-S", socket_path, "new-session", "-d",
            "-s", display_session, "sleep 60",
        ],
        check=True,
    )
    new_owner = subprocess.check_output(
        [
            "tmux", "-S", socket_path, "display-message", "-p",
            "-t", display_session, "#{pane_id}",
        ],
        text=True,
    ).strip()
    new_outer_id = subprocess.check_output(
        [
            "tmux", "-S", socket_path, "display-message", "-p",
            "-t", display_session, "#{session_id}",
        ],
        text=True,
    ).strip()
    os.environ["TMUX_PANE"] = new_owner
    client_host = "client-host"
    subprocess.run(
        [
            "tmux", "-S", socket_path, "new-session", "-d", "-s",
            client_host,
            f"env TMUX= tmux -S {shlex.quote(socket_path)} "
            "attach-session -t agent-b",
        ],
        check=True,
    )
    assert _wait_until(
        lambda: tmux_ctl.session_attached_count("agent-b") == 1)
    fallback_workspace = AgentWorkspace()
    fallback = AgentDisplayTransport(
        fallback_workspace,
        "swap",
        auto_launched=True,
        outer_session_name=display_session,
        outer_session_id=new_outer_id,
        owner_pane_id=new_owner,
    ).attach(fallback_workspace.primary, "agent-b")
    assert fallback.ok and fallback.fell_back
    assert fallback.kind == DisplayTransportKind.NESTED
    assert "independent client" in (fallback.reason or "")
