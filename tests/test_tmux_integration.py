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

from railmux import orphan_marker, restart_state, tmux_ctl
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


def test_real_swap_preserves_sidebar_focus(isolated_tmux):
    """A transport swap must not undo the mouse-selected sidebar pane."""
    display_session, sidebar_pane, _socket_path = isolated_tmux
    display_pane = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    agent_pane = subprocess.check_output(
        [
            "tmux", "new-session", "-d", "-s", "focus-agent",
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(
        ["tmux", "select-pane", "-t", sidebar_pane], check=True)

    assert tmux_ctl.swap_panes(agent_pane, display_pane)
    active_pane = subprocess.check_output(
        [
            "tmux", "display-message", "-p", "-t", display_session,
            "#{pane_id}",
        ],
        text=True,
    ).strip()
    assert active_pane == sidebar_pane


def test_real_restart_identity_isolates_windows_sessions_and_servers(
    isolated_tmux, monkeypatch, tmp_path,
):
    """Exact state paths cannot collide across the supported tmux topologies."""
    session_name, first_pane, first_socket = isolated_tmux
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first = restart_state.capture_outer_identity()
    assert first is not None and first.pane_id == first_pane
    topology_by_id = tmux_ctl.session_topology(first.session_id)
    assert topology_by_id is not None
    assert topology_by_id.session_name == session_name

    subprocess.run(
        ["tmux", "-S", first_socket, "new-window", "-d", "-t", session_name,
         "-n", "second", "sleep 60"],
        check=True,
    )
    second_pane = subprocess.check_output(
        ["tmux", "-S", first_socket, "display-message", "-p", "-t",
         f"{session_name}:second", "#{pane_id}"],
        text=True,
    ).strip()
    monkeypatch.setenv("TMUX_PANE", second_pane)
    second_window = restart_state.capture_outer_identity()
    assert second_window is not None
    assert second_window.server_digest == first.server_digest
    assert second_window.session_id == first.session_id
    assert second_window.window_id != first.window_id
    assert second_window.storage_key != first.storage_key

    subprocess.run(
        ["tmux", "-S", first_socket, "new-session", "-d", "-s", "other",
         "sleep 60"],
        check=True,
    )
    other_pane = subprocess.check_output(
        ["tmux", "-S", first_socket, "display-message", "-p", "-t", "other",
         "#{pane_id}"],
        text=True,
    ).strip()
    monkeypatch.setenv("TMUX_PANE", other_pane)
    other_session = restart_state.capture_outer_identity()
    assert other_session is not None
    assert other_session.server_digest == first.server_digest
    assert other_session.session_id != first.session_id
    assert other_session.storage_key not in {
        first.storage_key, second_window.storage_key}

    second_root = Path(tempfile.mkdtemp(prefix="rx2-", dir="/tmp"))
    second_root.chmod(0o700)
    second_socket = str(second_root / "s")
    try:
        subprocess.run(
            ["tmux", "-S", second_socket, "-f", "/dev/null", "new-session",
             "-d", "-s", session_name, "sleep 60"],
            check=True,
        )
        second_pid = subprocess.check_output(
            ["tmux", "-S", second_socket, "display-message", "-p", "-t",
             session_name, "#{pid}"], text=True).strip()
        private_pane = subprocess.check_output(
            ["tmux", "-S", second_socket, "display-message", "-p", "-t",
             session_name, "#{pane_id}"], text=True).strip()
        monkeypatch.setenv("TMUX", f"{second_socket},{second_pid},0")
        monkeypatch.setenv("TMUX_PANE", private_pane)
        private = restart_state.capture_outer_identity()
        assert private is not None
        assert private.server_digest != first.server_digest
        assert private.storage_key != first.storage_key

        identities = (first, second_window, other_session, private)
        paths = [restart_state.instance_state_path(item) for item in identities]
        assert len(set(paths)) == len(paths)
        for identity, path in zip(identities, paths):
            payload = {
                "schema_version": 1,
                "kind": "instance",
                "owner": identity.to_json(),
                "view": restart_state.build_view({"mode": "claude"}),
                "recovery": {"right_kind": "empty"},
            }
            assert restart_state.write_instance(identity, payload, path)
            assert restart_state.decode_instance(
                restart_state.read_json_object(path), identity) is not None
    finally:
        subprocess.run(
            ["tmux", "-S", second_socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(second_root, ignore_errors=True)


def test_real_marked_holder_persists_identity_before_provider_runs(
    isolated_tmux, tmp_path,
):
    """The provider's first instruction observes an already-durable marker."""
    owner = restart_state.capture_outer_identity()
    assert owner is not None
    holder, reason = tmux_ctl.create_detached_holder("railmux-marked-agent")
    assert holder is not None, reason
    remain = subprocess.check_output(
        ["tmux", "show-window-options", "-v", "-t", holder.pane_id,
         "remain-on-exit"], text=True).strip()
    assert remain in {"", "off"}  # inherited default or explicit rendering
    marker = orphan_marker.Marker(
        mode_key="codex",
        placeholder_key="__new__-real-1",
        tmux_name=holder.session_name,
        tmux_session_id=holder.session_id,
        tmux_pane_id=holder.pane_id,
        owner=owner,
        cwd=tmp_path.resolve(),
        created_at=time.time(),
        creation_token="c" * 32,
        phase="launching",
    )
    raw = orphan_marker.encode(marker)
    assert tmux_ctl.set_session_user_option(
        holder.session_id, orphan_marker.OPTION_NAME, raw)
    assert tmux_ctl.show_session_user_option(
        holder.session_id, orphan_marker.OPTION_NAME) == raw
    listed = subprocess.check_output(
        ["tmux", "list-sessions", "-F",
         f"#{{session_name}}\t#{{{orphan_marker.OPTION_NAME}}}"],
        text=True,
    ).splitlines()
    assert f"{holder.session_name}\t{raw}" in listed

    observed = tmp_path / "provider-observed-marker"
    # Query by the same immutable session ID used by runtime marker reads.
    # Older tmux releases do not consistently resolve a pane target for a
    # session-scoped user option after respawn-pane.
    script = (
        f'tmux show-options -v -t {shlex.quote(holder.session_id)} '
        f'{shlex.quote(orphan_marker.OPTION_NAME)} > '
        f'{shlex.quote(str(observed))}; sleep 60'
    )
    ok, reason = tmux_ctl.start_detached_holder(
        holder, f"sh -c {shlex.quote(script)}")
    assert ok, reason
    assert _wait_until(
        lambda: observed.exists() and observed.read_text().strip() == raw)
    assert observed.read_text().strip() == raw
    current = tmux_ctl.pane_identity(holder.pane_id)
    assert current is not None
    assert tmux_ctl.kill_session_identity(current)


def test_real_exact_kill_refuses_reused_session_name(isolated_tmux):
    holder, reason = tmux_ctl.create_detached_holder("railmux-reused-name")
    assert holder is not None, reason
    assert tmux_ctl.kill_session(holder.session_name)
    created, reason = tmux_ctl.new_detached_session(
        holder.session_name, "sleep 60")
    assert created, reason
    assert not tmux_ctl.kill_session_identity(holder)
    assert tmux_ctl.session_exists(holder.session_name)
    assert tmux_ctl.kill_session(holder.session_name)


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


def test_real_copy_mode_restore_preserves_one_new_user_binding(isolated_tmux):
    """Closing Railmux restores owned wrappers without undoing a tmux reload."""
    _display_session, helper_pane, _socket_path = isolated_tmux
    backup = tmux_ctl.prepare_scroll_bindings()
    assert backup is not None
    assert tmux_ctl.rebind_scroll_agent(helper_pane, backup)

    subprocess.run(
        [
            "tmux", "bind-key", "-T", "copy-mode", "WheelDownPane",
            "send-keys", "-X", "cancel",
        ],
        check=True,
    )
    tmux_ctl.restore_owned_scroll_bindings(helper_pane, backup)
    current = tmux_ctl.read_scroll_bindings()

    custom = current[("copy-mode", "WheelDownPane")]
    assert custom is not None and "send-keys -X cancel" in custom
    for binding_key, original in backup.items():
        if binding_key == ("copy-mode", "WheelDownPane"):
            continue
        assert current[binding_key] == original


def test_real_root_wheel_install_restore_and_user_reload(isolated_tmux):
    """Root wheel wrappers round-trip exactly and never undo a newer key."""
    backup = tmux_ctl.prepare_root_wheel_bindings()
    assert backup is not None

    first_token = "integration-first"
    assert tmux_ctl.set_root_wheel_forwarding(backup, first_token)
    installed = tmux_ctl.read_root_wheel_bindings()
    assert all(
        binding is not None
        and f"railmux-wheel-forward-v1-{first_token}" in binding
        and "send-keys -M" in binding
        for binding in installed.values()
    )
    tmux_ctl.restore_root_wheel_bindings(backup, token=first_token)
    assert tmux_ctl.read_root_wheel_bindings() == backup

    second_token = "integration-second"
    assert tmux_ctl.set_root_wheel_forwarding(backup, second_token)
    subprocess.run(
        [
            "tmux", "bind-key", "-T", "root", "WheelDownPane",
            "display-message", "user-custom-wheel",
        ],
        check=True,
    )
    tmux_ctl.restore_root_wheel_bindings(backup, token=second_token)
    current = tmux_ctl.read_root_wheel_bindings()
    assert current["WheelUpPane"] == backup["WheelUpPane"]
    custom = current["WheelDownPane"]
    assert custom is not None and "user-custom-wheel" in custom


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
