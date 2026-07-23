"""Opt-in smoke coverage against a real, isolated tmux server."""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from railmux import (
    cli,
    fast_display_server,
    orphan_marker,
    restart_state,
    tmux_ctl,
    tmux_health,
    tmux_server,
)
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.fast_display_protocol import (
    PROTOCOL_VERSION,
    REMOTE_ATTACH_ACCEPTED,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    RemoteExit,
    encode_input,
)
from railmux.tmux_binding_manager import SharedTmuxBindingManager
from railmux.selection_isolation import SelectionIsolationManager
from railmux.modes import CODEX_MODE
from railmux.models import Project
from railmux.ui.app import App, _Running
from railmux.ui.workspace import (
    AgentWorkspace,
    DisplayTransportKind,
    WorkspaceLayout,
)


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


def _script_command(command: str) -> list[str]:
    """Run *command* under a PTY with the platform's script(1) syntax."""
    if sys.platform == "darwin":
        return ["script", "-q", "/dev/null", "sh", "-c", command]
    return ["script", "-qefc", command, "/dev/null"]


def test_real_tmux_parameterized_scroll_matches_headless_screen():
    """The advertised xterm client may use CSI S/T; its model must agree."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    pyte = pytest.importorskip("pyte")
    socket_root = Path(tempfile.mkdtemp(prefix="rx-vt-", dir="/tmp"))
    socket_root.chmod(0o700)
    socket_path = str(socket_root / "s")
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 8, 20, 0, 0),
    )
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    client = None
    try:
        program = (
            "import sys,time; time.sleep(.5); "
            "sys.stdout.write('\\x1b[2J\\x1b[H' + "
            "''.join(str(i) * 20 for i in range(1, 9))); "
            "sys.stdout.flush(); time.sleep(.5); "
            "sys.stdout.write('\\x1b[2;7r\\x1b[2S'); "
            "sys.stdout.flush(); time.sleep(5)"
        )
        subprocess.run(
            [
                "tmux", "-S", socket_path, "-f", "/dev/null",
                "new-session", "-d", "-x", "20", "-y", "8",
                "-s", "probe", sys.executable, "-c", program,
            ],
            check=True,
            capture_output=True,
        )
        client = subprocess.Popen(
            ["tmux", "-S", socket_path, "attach-session", "-t", "probe"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.set_blocking(master_fd, False)
        raw = bytearray()
        deadline = time.monotonic() + 4.0
        quiet_since = None
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                raw.extend(chunk)
                quiet_since = None
            elif b"\033[2S" in raw:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= 0.2:
                    break

        # This is real tmux output, not a synthetic sequence: it proves that
        # the TERM advertised by the display helper selects parameterized SU.
        assert b"\033[2S" in raw
        captured = subprocess.check_output(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", "probe"],
            text=True,
        ).splitlines()
        terminal = fast_display_server._extended_pyte(pyte)
        screen = terminal.Screen(20, 8)
        terminal.ByteStream(screen).feed(bytes(raw))

        assert [row.rstrip() for row in screen.display[:len(captured)]] == captured

        stock_screen = pyte.Screen(20, 8)
        try:
            pyte.ByteStream(stock_screen).feed(bytes(raw))
        except TypeError:
            stock_display = None
        else:
            stock_display = [
                row.rstrip() for row in stock_screen.display[:len(captured)]
            ]
        assert stock_display != captured
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        subprocess.run(
            ["tmux", "-S", socket_path, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if client is not None and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=2.0)
        shutil.rmtree(socket_root, ignore_errors=True)


def test_dedicated_label_routes_fast_server_to_private_tmux(monkeypatch):
    """Outside-tmux helpers must never fall back to the default socket."""
    socket_root = Path(tempfile.mkdtemp(prefix="rx-label-", dir="/tmp"))
    socket_root.chmod(0o700)
    label = f"rx-isolation-{os.getpid()}"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    try:
        subprocess.run(
            tmux_server.tmux_argv(
                "-f", "/dev/null", "new-session", "-d", "-s", "probe",
                "sleep 60",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        controller = subprocess.check_output(
            tmux_server.tmux_argv(
                "display-message", "-p", "-t", "probe", "#{pane_id}"
            ),
            text=True,
        ).strip()
        subprocess.run(
            tmux_server.tmux_argv(
                "set-window-option", "-t", "probe",
                "@railmux_controller_pane", controller,
            ),
            check=True,
        )
        agent = subprocess.check_output(
            tmux_server.tmux_argv(
                "split-window", "-d", "-h", "-P", "-F", "#{pane_id}",
                "-t", "probe", "sleep 60",
            ),
            text=True,
        ).strip()
        subprocess.run(
            tmux_server.tmux_argv("select-pane", "-t", agent), check=True
        )

        target = tmux_server.discover_target()
        assert target is not None
        assert Path(target.socket_path).resolve().is_relative_to(
            socket_root.resolve()
        )
        session_id = fast_display_server._validate_railmux("probe")
        assert session_id == "$0"
        assert tmux_server.target_session_id(target, "probe") == session_id
        assert [pane.pane_id for pane in fast_display_server._list_agent_panes(
            session_id
        )] == [agent]
    finally:
        subprocess.run(
            tmux_server.tmux_argv("kill-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(socket_root, ignore_errors=True)


def test_remote_compatibility_hello_precedes_any_tmux_server(monkeypatch):
    """A client prompt must never leave behind or resize a tmux session."""
    socket_root = Path(tempfile.mkdtemp(prefix="rx-handshake-", dir="/tmp"))
    socket_root.chmod(0o700)
    label = f"rx-handshake-{os.getpid()}"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "railmux",
                "remote-server",
                "--protocol",
                str(PROTOCOL_VERSION),
                "--width",
                "80",
                "--height",
                "24",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        assert process.stdout is not None
        assert process.stdout.readline().startswith(REMOTE_HELLO_PREFIX)

        probe = subprocess.run(
            tmux_server.tmux_argv("list-sessions"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        assert probe.returncode != 0
        assert process.poll() is None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        subprocess.run(
            tmux_server.tmux_argv("kill-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(socket_root, ignore_errors=True)


def test_nested_history_prefetch_reads_real_source_pane(monkeypatch):
    """A nested wrapper's zero scrollback must not hide its source history."""
    dedicated_root = Path(tempfile.mkdtemp(prefix="rx-history-d-", dir="/tmp"))
    source_root = Path(tempfile.mkdtemp(prefix="rx-history-s-", dir="/tmp"))
    dedicated_root.chmod(0o700)
    source_root.chmod(0o700)
    label = f"rx-history-{os.getpid()}"
    source_socket = str(source_root / "s")
    monkeypatch.setenv("TMUX_TMPDIR", str(dedicated_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    try:
        subprocess.run(
            tmux_server.tmux_argv(
                "-f", "/dev/null", "new-session", "-d", "-s", "probe",
                "sleep 60",
            ),
            check=True,
        )
        controller = subprocess.check_output(
            tmux_server.tmux_argv(
                "display-message", "-p", "-t", "probe", "#{pane_id}"
            ),
            text=True,
        ).strip()
        subprocess.run(
            tmux_server.tmux_argv(
                "set-window-option", "-t", "probe",
                "@railmux_controller_pane", controller,
            ),
            check=True,
        )
        wrapper = subprocess.check_output(
            tmux_server.tmux_argv(
                "split-window", "-d", "-h", "-P", "-F", "#{pane_id}",
                "-t", "probe", "sleep 60",
            ),
            text=True,
        ).strip()
        subprocess.run(
            tmux_server.tmux_argv("select-pane", "-t", wrapper), check=True)

        subprocess.run(
            [
                "tmux", "-S", source_socket, "-f", "/dev/null",
                "new-session", "-d", "-s", "legacy",
                "i=0; while [ $i -lt 160 ]; do "
                "echo source-history-$i; i=$((i+1)); done; sleep 60",
            ],
            check=True,
        )
        source_identity = subprocess.check_output(
            [
                "tmux", "-S", source_socket, "display-message", "-p",
                "-t", "legacy", "#{socket_path}\t#{pid}\t#{session_id}",
            ],
            text=True,
        ).strip().split("\t")
        source_target = tmux_server.TmuxServerTarget(
            source_identity[0], int(source_identity[1]))
        source_session_id = source_identity[2]
        assert _wait_until(
            lambda: int(subprocess.check_output(
                [
                    "tmux", "-S", source_socket, "display-message", "-p",
                    "-t", "legacy", "#{history_size}",
                ],
                text=True,
            ).strip()) > 100
        )

        marker = tmux_server.encode_history_source(
            source_target, source_session_id, legacy=True)
        assert marker is not None
        subprocess.run(
            tmux_server.tmux_argv(
                "set-option", "-p", "-t", wrapper,
                tmux_server.HISTORY_SOURCE_OPTION, marker,
            ),
            check=True,
        )
        # NestedDisplayTransport stamps the wrapper immediately before respawn.
        # Prove real tmux preserves that pane-local identity across the respawn.
        subprocess.run(
            tmux_server.tmux_argv(
                "respawn-pane", "-k", "-t", wrapper, "sleep 60"
            ),
            check=True,
        )
        assert subprocess.check_output(
            tmux_server.tmux_argv(
                "show-options", "-pv", "-t", wrapper,
                tmux_server.HISTORY_SOURCE_OPTION,
            ),
            text=True,
        ).strip() == marker
        monkeypatch.setattr(
            tmux_server,
            "discover_legacy_target",
            lambda **_kwargs: source_target,
        )

        session_id = tmux_server.target_session_id(
            tmux_server.discover_target(), "probe")
        assert session_id is not None
        panes = fast_display_server._list_agent_panes(session_id)
        assert len(panes) == 1
        assert panes[0].pane_id == wrapper
        assert panes[0].history_pane_id == tmux_server.target_single_pane_id(
            source_target, source_session_id)
        batch = fast_display_server.capture_history_batch(
            __import__("pyte"), session_id, 7, 300)
        assert len(batch.snapshots) == 1
        snapshot = batch.snapshots[0]
        assert snapshot.pane_id == wrapper
        assert len(snapshot.lines) > snapshot.height
        assert any(b"source-history-" in line for line in snapshot.lines)
    finally:
        subprocess.run(
            tmux_server.tmux_argv("kill-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["tmux", "-S", source_socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(dedicated_root, ignore_errors=True)
        shutil.rmtree(source_root, ignore_errors=True)


def test_local_watchdog_escapes_a_frozen_private_tmux(monkeypatch):
    """A stopped private server must not retain the outer launcher forever."""
    socket_root = Path(tempfile.mkdtemp(prefix="rx-watchdog-", dir="/tmp"))
    socket_root.chmod(0o700)
    label = f"rx-watchdog-{os.getpid()}"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(cli, "_LOCAL_WATCHDOG_INTERVAL", 0.1)
    server_pid = 0
    freezer = None

    try:
        subprocess.run(
            tmux_server.tmux_argv(
                "-f", "/dev/null", "new-session", "-d", "-s", "probe",
                "sleep 60",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        target = tmux_server.discover_target()
        assert target is not None
        server_pid = target.server_pid
        freezer = threading.Timer(
            0.2, lambda: os.kill(server_pid, signal.SIGSTOP)
        )
        freezer.start()

        result = cli._run_tmux_client_with_watchdog(
            ["sleep", "30"], os.environ.copy(), expected_target=target
        )

        assert result == 2
        incident = tmux_health.read_last_incident()
        assert incident is not None
        assert incident.reason == "launcher-watchdog-timeout"
        assert incident.consecutive_failures == 3
    finally:
        if freezer is not None:
            freezer.cancel()
            freezer.join(timeout=1.0)
        if server_pid > 0:
            try:
                os.kill(server_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        subprocess.run(
            tmux_server.tmux_argv("kill-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
        shutil.rmtree(socket_root, ignore_errors=True)


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


def test_real_managed_restart_handoff_crosses_recreated_controller_pane(
    isolated_tmux, monkeypatch, tmp_path,
):
    """The CLI's replacement session must still find the saved dual layout."""
    session_name, old_pane, socket_path = isolated_tmux
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        App, "_portable_state_path",
        staticmethod(lambda: tmp_path / "portable.json"),
    )
    source = restart_state.capture_outer_identity()
    assert source is not None and source.pane_id == old_pane
    payload = {
        "schema_version": restart_state.SCHEMA_VERSION,
        "kind": "instance",
        "owner": source.to_json(),
        "view": restart_state.build_view({"mode": "codex"}),
        "recovery": {
            "right_kind": "empty",
            "workspace": {
                "version": 1,
                "layout": "stacked",
                "target": "secondary",
                "focus": "sidebar",
                "slots": {
                    "primary": {"kind": "empty"},
                    "secondary": {"kind": "empty"},
                },
            },
        },
    }
    assert restart_state.write_instance(source, payload)
    assert restart_state.write_managed_handoff(source)

    subprocess.run(
        ["tmux", "-S", socket_path, "new-session", "-d", "-s", "keeper",
         "sleep 60"],
        check=True,
    )
    subprocess.run(
        ["tmux", "-S", socket_path, "kill-session", "-t", session_name],
        check=True,
    )
    subprocess.run(
        ["tmux", "-S", socket_path, "new-session", "-d", "-s", session_name,
         "sleep 60"],
        check=True,
    )
    new_pane = subprocess.check_output(
        ["tmux", "-S", socket_path, "display-message", "-p", "-t",
         session_name, "#{pane_id}"],
        text=True,
    ).strip()
    monkeypatch.setenv("TMUX_PANE", new_pane)
    replacement_identity = restart_state.capture_outer_identity()
    assert replacement_identity is not None
    assert replacement_identity.pane_id != source.pane_id

    app = App.__new__(App)
    app._restart_identity = replacement_identity
    app._auto_launched = True
    app._loaded_restart_source = None
    app._loaded_restart_state_path = None
    state = app._load_state()

    assert state is not None
    assert state["workspace"]["layout"] == "stacked"
    assert state["workspace"]["target"] == "secondary"
    assert app._loaded_restart_source == source


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


def test_real_legacy_session_migration_installs_v2_marker(
        isolated_tmux, tmp_path):
    """A strictly matched pre-marker session is upgraded on the live object."""
    _display_session, _sidebar_pane, _socket_path = isolated_tmux
    cwd = tmp_path / "project"
    cwd.mkdir()
    provider = tmp_path / "codex"
    provider.write_text("#!/bin/sh\nexec sleep 60\n")
    provider.chmod(0o700)
    name = "cx-new---abc123-1"
    command = App._shellify(
        [str(provider), "-C", str(cwd)], cwd, login_shell=True)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, command], check=True)
    raw = subprocess.check_output(
        [
            "tmux", "list-sessions", "-F",
            "#{session_name}\t#{session_created}\t#{session_id}\t#{pane_id}",
        ],
        text=True,
    )
    fields = next(
        line.split("\t") for line in raw.splitlines()
        if line.startswith(name + "\t")
    )
    _name, created, session_id, pane_id = fields
    owner = restart_state.capture_outer_identity()
    assert owner is not None
    app = App.__new__(App)
    app._restart_identity = owner
    app._config = SimpleNamespace(codex_binary=str(provider))
    project = Project(cwd, "project", Path(), 0, 0.0)

    running = app._legacy_unresolved_running(
        name=name,
        cwd=cwd,
        created=int(created),
        session_id=session_id,
        pane_id=pane_id,
        mode=CODEX_MODE,
        project=project,
    )

    assert running is not None and running.orphan is not None
    saved = tmux_ctl.show_session_user_option(
        session_id, orphan_marker.OPTION_NAME)
    assert orphan_marker.decode(saved) == running.orphan
    assert running.orphan.phase == "unresolved"


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


def test_real_shared_window_uses_smallest_of_two_client_sizes(isolated_tmux):
    display_session, _pane, socket_path = isolated_tmux
    subprocess.run(
        ["tmux", "-S", socket_path, "set-option", "-t", display_session,
         "status", "off"],
        check=True,
    )
    assert tmux_ctl.use_smallest_window_size(_pane)
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    clients: list[tuple[subprocess.Popen, int, int]] = []

    def attach(width: int, height: int) -> subprocess.Popen:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", height, width, 0, 0),
        )
        process = subprocess.Popen(
            ["tmux", "-S", socket_path, "attach-session", "-t",
             display_session],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        clients.append((process, master, slave))
        return process

    def window_size() -> tuple[int, int]:
        text = subprocess.check_output(
            ["tmux", "-S", socket_path, "display-message", "-p", "-t",
             display_session, "#{window_width} #{window_height}"],
            text=True,
        ).split()
        return int(text[0]), int(text[1])

    try:
        attach(120, 40)
        assert _wait_until(
            lambda: tmux_ctl.session_attached_count(display_session) == 1)
        assert _wait_until(lambda: window_size() == (120, 40))

        small = attach(80, 24)
        assert _wait_until(
            lambda: tmux_ctl.session_attached_count(display_session) == 2)
        assert _wait_until(lambda: window_size() == (80, 24))

        small.terminate()
        small.wait(timeout=2.0)
        assert _wait_until(
            lambda: tmux_ctl.session_attached_count(display_session) == 1)
        assert _wait_until(lambda: window_size() == (120, 40))
    finally:
        for process, master, slave in clients:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass


def test_real_soft_quit_disconnects_shared_clients_and_agents_survive(
        monkeypatch, tmp_path):
    """Two real clients leave together; a detached agent survives restart."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    # Keep the explicit tmux socket short. macOS pytest paths under
    # /private/var/folders can exceed the Unix-domain socket path limit after
    # tmux appends its uid directory and label.
    socket_root = Path(tempfile.mkdtemp(prefix="rx-soft-", dir="/tmp"))
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    claude_home = home / ".claude"
    for directory in (socket_root, home, runtime, claude_home):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    label = f"rx-soft-quit-{os.getpid()}"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_RUNTIME_DIR": str(runtime),
        "TERM": "xterm-256color",
    })
    clients: list[tuple[subprocess.Popen, int, threading.Thread]] = []
    stop_draining = threading.Event()
    railmux_executable = Path(sys.executable).with_name("railmux")
    assert railmux_executable.is_file()

    def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", label, *args],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=check,
        )

    def attached_count() -> int | None:
        try:
            raw = subprocess.check_output(
                [
                    "tmux", "-L", label, "display-message", "-p",
                    "-t", "railmux", "#{session_attached}",
                ],
                env=env,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return int(raw.strip())
        except (OSError, subprocess.CalledProcessError, ValueError):
            return None

    def session_exists(name: str) -> bool:
        return tmux("has-session", "-t", name, check=False).returncode == 0

    def captured_railmux() -> str:
        try:
            return subprocess.check_output(
                ["tmux", "-L", label, "capture-pane", "-p", "-t", "railmux"],
                env=env,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return ""

    def launch_client() -> tuple[subprocess.Popen, int]:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 40, 140, 0, 0),
        )
        process = subprocess.Popen(
            [
                str(railmux_executable),
                "--claude-home", str(claude_home),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)

        def drain_output() -> None:
            while not stop_draining.is_set():
                readable, _, _ = select.select([master], [], [], 0.1)
                if not readable:
                    continue
                try:
                    if not os.read(master, 65536):
                        return
                except OSError:
                    return

        drain_thread = threading.Thread(target=drain_output, daemon=True)
        drain_thread.start()
        clients.append((process, master, drain_thread))
        return process, master

    try:
        first, first_master = launch_client()
        assert _wait_until(lambda: attached_count() == 1, timeout=8.0)
        assert _wait_until(
            lambda: "q Quit" in captured_railmux(), timeout=8.0)

        agent_session = "integration-agent"
        tmux("new-session", "-d", "-s", agent_session, "sleep", "60")
        assert session_exists(agent_session)

        second, _second_master = launch_client()
        assert _wait_until(lambda: attached_count() == 2, timeout=8.0)

        os.write(first_master, b"q")
        assert _wait_until(
            lambda: "Quit railmux?" in captured_railmux(),
            timeout=3.0,
        )
        os.write(first_master, b"s")

        assert _wait_until(lambda: first.poll() is not None, timeout=8.0)
        assert _wait_until(lambda: second.poll() is not None, timeout=8.0)
        assert not session_exists("railmux")
        assert session_exists(agent_session)

        replacement, _replacement_master = launch_client()
        assert _wait_until(lambda: attached_count() == 1, timeout=8.0)
        assert replacement.poll() is None
        assert session_exists(agent_session)
    finally:
        tmux("kill-server", check=False)
        stop_draining.set()
        for process, master, drain_thread in clients:
            if process.poll() is None:
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)
            drain_thread.join(timeout=1.0)
            try:
                os.close(master)
            except OSError:
                pass
        shutil.rmtree(socket_root, ignore_errors=True)


def test_real_remote_display_soft_quit_keeps_tmux_responsive(
        monkeypatch, tmp_path):
    """The SSH helper observes soft quit without wedging its tmux server."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    socket_root = Path(tempfile.mkdtemp(prefix="rx-remote-soft-", dir="/tmp"))
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    claude_home = home / ".claude"
    for directory in (socket_root, home, runtime, claude_home):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    label = f"rx-remote-soft-{os.getpid()}"
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv(tmux_server.SOCKET_LABEL_ENV, label)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_RUNTIME_DIR": str(runtime),
        "TERM": "xterm-256color",
    })
    process = None
    drain_thread = None

    def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", label, *args],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=check,
        )

    def captured_railmux() -> str:
        try:
            return subprocess.check_output(
                ["tmux", "-L", label, "capture-pane", "-p", "-t", "railmux"],
                env=env,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            return ""

    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "railmux",
                "remote-server",
                "--protocol",
                str(PROTOCOL_VERSION),
                "--width",
                "140",
                "--height",
                "40",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stdout.readline().startswith(REMOTE_HELLO_PREFIX)
        process.stdin.write(REMOTE_START)
        process.stdin.flush()
        assert process.stdout.readline() == REMOTE_ATTACH_ACCEPTED

        def drain_output() -> None:
            assert process is not None and process.stdout is not None
            while process.stdout.read(65536):
                pass

        drain_thread = threading.Thread(target=drain_output, daemon=True)
        drain_thread.start()
        assert _wait_until(
            lambda: "q Quit" in captured_railmux(), timeout=8.0)

        tmux("new-session", "-d", "-s", "integration-agent", "sleep", "60")
        process.stdin.write(encode_input(b"q"))
        process.stdin.flush()
        assert _wait_until(
            lambda: "Quit railmux?" in captured_railmux(), timeout=3.0)
        process.stdin.write(encode_input(b"s"))
        process.stdin.flush()

        returncode = process.wait(timeout=8.0)
        assert process.stderr is not None
        stderr = process.stderr.read().decode(errors="replace")
        assert returncode == int(RemoteExit.SOFT_QUIT), stderr
        assert tmux(
            "has-session", "-t", "integration-agent", check=False
        ).returncode == 0
        assert _wait_until(
            lambda: tmux(
                "display-message", "-p", "#{pid}", check=False
            ).returncode == 0,
            timeout=3.0,
        )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if drain_thread is not None:
            drain_thread.join(timeout=1.0)
        tmux("kill-server", check=False)
        shutil.rmtree(socket_root, ignore_errors=True)


def test_real_tmux_single_sidebar_focus_clears_stale_target_format(isolated_tmux):
    """A restored single pane cannot leave half the divider dim green."""
    display_session, _sidebar_pane, _socket_path = isolated_tmux
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    stale_target = (
        "fg=#{?#{==:#{pane_id}," + primary + "},#3f6f00,colour240}"
    )
    assert tmux_ctl.set_window_border_styles(stale_target, "fg=colour240")

    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.primary.pane_id = primary
    app._divider_active = None
    app._set_divider_active(False)

    for option in ("pane-border-style", "pane-active-border-style"):
        assert subprocess.check_output(
            [
                "tmux", "show-window-options", "-v", "-t", display_session,
                option,
            ],
            text=True,
        ).strip() == "fg=colour240"


def test_real_private_tmux_client_applies_runtime_pty_resize(isolated_tmux):
    session_name, _sidebar_pane, socket_path = isolated_tmux
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 80, 0, 0),
    )
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    process = subprocess.Popen(
        [
            "tmux", "-S", socket_path,
            "attach-session", "-t", session_name,
        ],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        start_new_session=True,
    )
    os.close(slave_fd)
    try:
        def client_size() -> str:
            return subprocess.check_output(
                ["tmux", "-S", socket_path, "list-clients", "-F",
                 "#{client_width}x#{client_height}"],
                text=True,
            ).strip()

        assert _wait_until(lambda: client_size() == "80x24")
        fast_display_server._resize_tmux_client(
            process.pid, master_fd, 70, 18)
        assert _wait_until(lambda: client_size() == "70x18")
        assert subprocess.check_output(
            ["tmux", "-S", socket_path,
             "display-message", "-p", "-t", session_name,
             "#{window_width}x#{window_height}"],
            text=True,
        ).strip() == "70x17"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)


def test_real_tmux_agent_focus_heals_external_gray_border_drift(isolated_tmux):
    display_session, _sidebar_pane, _socket_path = isolated_tmux
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(["tmux", "select-pane", "-t", primary], check=True)

    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = primary
    app._railmux_has_focus = False
    app._divider_active = (
        True, WorkspaceLayout.SIDE_BY_SIDE, None)
    app._last_border_verify_at = 0.0
    app._sync_border_indicators = lambda _arrows: True

    assert tmux_ctl.set_window_border_styles(
        "fg=colour240", "fg=colour240")
    app._retry_pending_divider_style()

    assert tmux_ctl.window_border_styles() == (
        True, ("fg=colour240", "fg=#5faf00"))


def test_real_side_by_side_focus_draws_inward_arrows_and_restores(
    isolated_tmux,
):
    """Three-pane columns identify the middle active pane directionally."""
    if tmux_ctl.tmux_version() < (3, 3):
        pytest.skip("pane-border-indicators was added in tmux 3.3")
    if shutil.which("script") is None or shutil.which("timeout") is None:
        pytest.skip("PTY screen capture requires script and timeout")

    display_session, sidebar_pane, socket_path = isolated_tmux
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    secondary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", primary,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(["tmux", "select-pane", "-t", primary], check=True)

    app = App.__new__(App)
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = primary
    app._workspace.secondary.pane_id = secondary
    app._divider_active = None

    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, None)
    app._set_divider_active(True)
    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, "arrows")

    with tempfile.NamedTemporaryFile(
        prefix="rx-border-", dir="/tmp", delete=False,
    ) as capture_file:
        capture = Path(capture_file.name)
    try:
        command = (
            "stty cols 100 rows 24; TERM=xterm-256color "
            f"tmux -S {shlex.quote(socket_path)} "
            f"attach-session -t {shlex.quote(display_session)}"
        )
        subprocess.run(
            ["timeout", "0.4", "script", "-qfec", command, str(capture)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        screen = capture.read_text(errors="replace")
        assert "→" in screen
        assert "←" in screen
    finally:
        capture.unlink(missing_ok=True)

    subprocess.run(["tmux", "select-pane", "-t", sidebar_pane], check=True)
    app._set_divider_active(False)
    assert subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-border-style",
        ],
        text=True,
    ).strip() == "fg=colour240"
    assert subprocess.check_output(
        [
            "tmux", "show-window-options", "-v", "-t", display_session,
            "pane-active-border-style",
        ],
        text=True,
    ).strip() == "fg=colour240"
    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, "colour")

    app._tmux_status_enabled = True
    app._tmux_status_session = display_session
    app._tmux_error_bar = False
    app._railmux_has_focus = True
    app._active_mode = lambda: SimpleNamespace(label="Codex")
    app._apply_tmux_bar(False)
    status_left = subprocess.check_output(
        [
            "tmux", "show-options", "-v", "-t", display_session,
            "status-left",
        ],
        text=True,
    )
    assert "· Codex · ◧" in status_left
    # A direct mouse-style focus move from P1/sidebar to P2 is observed through
    # tmux while the sidebar remains unfocused; Target and status-left must
    # update without waiting for focus to return through the sidebar.
    app._sessions_pane = SimpleNamespace(set_active_session=lambda _value: None)
    app._running_pane = SimpleNamespace(set_active=lambda _value: None)
    app._railmux_pane_id = sidebar_pane
    app._railmux_has_focus = False
    subprocess.run(["tmux", "select-pane", "-t", secondary], check=True)
    app._sync_target_slot_from_tmux()
    status_left = subprocess.check_output(
        [
            "tmux", "show-options", "-v", "-t", display_session,
            "status-left",
        ],
        text=True,
    )
    assert "· Codex · ◨" in status_left

    assert app._restore_border_indicators()
    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, None)

    # An explicit per-window setting is restored by value, not flattened into
    # inheritance. This covers the defensive branch separately from the
    # ordinary fresh-window path above.
    assert tmux_ctl.set_window_option("pane-border-indicators", "both")
    app._divider_active = None
    app._set_divider_active(True)
    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, "arrows")
    app._set_divider_active(False)
    assert app._restore_border_indicators()
    assert tmux_ctl.local_window_option("pane-border-indicators") == (
        True, "both")


def test_real_empty_secondary_pane_shows_guidance(isolated_tmux):
    display_session, sidebar_pane, _socket_path = isolated_tmux
    outer_id = tmux_ctl.current_session_id()
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    workspace = AgentWorkspace()
    workspace.primary.pane_id = primary
    manager = AgentDisplayTransport(
        workspace,
        "nested",
        auto_launched=True,
        outer_session_name=display_session,
        outer_session_id=outer_id,
        owner_pane_id=sidebar_pane,
    )

    assert manager.create_secondary(WorkspaceLayout.STACKED)
    secondary = workspace.secondary.pane_id
    assert secondary is not None

    def content() -> str:
        return subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", secondary],
            text=True,
        )

    assert _wait_until(
        lambda: "RAILMUX  ·  PANE 2" in content()
    ), subprocess.check_output(
        [
            "tmux", "list-panes", "-t", secondary, "-F",
            "#{pane_dead} #{pane_dead_status} #{pane_current_command} "
            "#{pane_start_command}",
        ],
        text=True,
    ) + content()
    assert "Ready for another agent" in content()
    assert "click / ␣  show" in content()


def test_real_local_workspace_restore_rebuilds_layout_target_and_focus(
        isolated_tmux):
    display_session, sidebar_pane, _socket_path = isolated_tmux
    for name in ("cc-soft-primary", "cx-soft-secondary"):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "sleep 60"],
            check=True,
        )
    outer_id = tmux_ctl.current_session_id()
    workspace = AgentWorkspace()
    app = App.__new__(App)
    app._workspace = workspace
    app._display_transport_manager = AgentDisplayTransport(
        workspace,
        "swap",
        auto_launched=True,
        outer_session_name=display_session,
        outer_session_id=outer_id,
        owner_pane_id=sidebar_pane,
    )
    app._running = {
        "primary-session": _Running(
            key="primary-session",
            tmux_name="cc-soft-primary",
            label="primary",
            session_type="claude",
        ),
        "secondary-session": _Running(
            key="secondary-session",
            tmux_name="cx-soft-secondary",
            label="secondary",
            session_type="codex",
        ),
    }
    app._railmux_pane_id = sidebar_pane
    app._railmux_has_focus = True
    app._double_focus_visual_pending = False
    app._sessions_pane = SimpleNamespace(set_active_session=lambda _value: None)
    app._running_pane = SimpleNamespace(set_active=lambda _value: None)
    app._paint_slot_active_target = lambda *_args: None
    app._redraw_focus_state_now = lambda: None
    app._check_agent_slot_size = lambda *_args, **_kwargs: None
    app._schedule_scroll_acceleration = lambda *_args: None
    app._install_tmux_bindings = lambda: None
    statuses = []
    app._set_status = lambda *args, **_kwargs: statuses.append(args)
    app._agent_region_size = lambda: (160, 30)
    app._layout_fits = lambda _region, _layout: True

    def set_focus(active, *, force_border=False):
        app._railmux_has_focus = active

    app._set_railmux_focus = set_focus
    saved = {
        "layout": "stacked",
        "target": "secondary",
        "focus": "secondary",
        "slots": {
            "primary": {
                "kind": "agent", "tmux": "cc-soft-primary",
                "session": "primary-session", "mode": "claude",
            },
            "secondary": {
                "kind": "agent", "tmux": "cx-soft-secondary",
                "session": "secondary-session", "mode": "codex",
            },
        },
    }

    assert app._restore_workspace({}, saved), statuses

    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.target_slot_key == AgentWorkspace.SECONDARY
    assert workspace.primary.agent_tmux_name == "cc-soft-primary"
    assert workspace.secondary.agent_tmux_name == "cx-soft-secondary"
    assert app._railmux_has_focus is False
    assert tmux_ctl.active_pane_id(sidebar_pane) == workspace.secondary.pane_id
    window_width, _ = tmux_ctl.window_size(sidebar_pane) or (0, 0)
    sidebar_width, _ = tmux_ctl.pane_size(sidebar_pane) or (0, 0)
    assert sidebar_width == App._sidebar_width_for_layout(
        WorkspaceLayout.STACKED, window_width)
    assert workspace.primary.transport_kind is DisplayTransportKind.SWAP
    assert workspace.secondary.transport_kind is DisplayTransportKind.SWAP


def test_real_kill_preparation_keeps_swap_secondary_as_empty_surface(
        isolated_tmux):
    """Intentional kill returns the real pane home without collapsing Pane 2."""
    display_session, sidebar_pane, _socket_path = isolated_tmux
    outer_id = tmux_ctl.current_session_id()
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", display_session,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "kill-display-agent", "sleep 60"],
        check=True,
    )
    workspace = AgentWorkspace()
    workspace.primary.pane_id = primary
    manager = AgentDisplayTransport(
        workspace,
        "swap",
        auto_launched=True,
        outer_session_name=display_session,
        outer_session_id=outer_id,
        owner_pane_id=sidebar_pane,
    )
    assert manager.create_secondary(WorkspaceLayout.STACKED)
    outcome = manager.attach(workspace.secondary, "kill-display-agent")
    assert outcome.ok and outcome.kind is DisplayTransportKind.SWAP

    assert manager.prepare_kill("kill-display-agent")
    empty_pane = workspace.secondary.pane_id
    assert empty_pane is not None
    assert workspace.layout is WorkspaceLayout.STACKED
    assert workspace.secondary.agent_tmux_name is None
    assert tmux_ctl.kill_session("kill-display-agent")

    def content() -> str:
        return subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", empty_pane],
            text=True,
        )

    assert _wait_until(lambda: "Ready for another agent" in content())
    assert tmux_ctl.pane_alive(empty_pane)


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


def test_real_tmux_binding_manager_round_trip_and_user_reload(
        isolated_tmux, monkeypatch, tmp_path):
    """Global wrappers execute and restore their exact per-key authority."""
    _display_session, owner_pane, _socket_path = isolated_tmux
    monkeypatch.setattr(
        "railmux.tmux_binding_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    subprocess.run(
        [
            "tmux", "bind-key", "-T", "root", "F8",
            "display-message", "original-f8",
        ],
        check=True,
    )
    subprocess.run(
        ["tmux", "unbind-key", "-T", "root", "F9"],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    original = tmux_ctl.read_root_function_bindings()
    original_prefix_tab = tmux_ctl.read_prefix_target_binding()
    original_right_click = tmux_ctl.read_root_right_click_binding()
    original_status_click = tmux_ctl.read_root_status_click_binding()
    assert original["F8"] is not None and original["F9"] is None

    manager = SharedTmuxBindingManager("integration-server", owner_pane)
    assert manager.open()
    current = tmux_ctl.read_root_function_bindings()
    current_prefix_tab = tmux_ctl.read_prefix_target_binding()["Tab"]
    current_right_click = (
        tmux_ctl.read_root_right_click_binding()["MouseDown3Pane"])
    current_status_click = (
        tmux_ctl.read_root_status_click_binding()["MouseDown1Status"])
    assert all(
        binding is not None
        and "railmux-function-forward-v1-" in binding
        and tmux_ctl.RAILMUX_CONTROLLER_OPTION in binding
        for binding in current.values()
    )
    assert current_prefix_tab is not None
    assert "railmux-target-toggle-v1-" in current_prefix_tab
    assert tmux_ctl.RAILMUX_CONTROLLER_OPTION in current_prefix_tab
    assert tmux_ctl.RAILMUX_TARGET_OPTION in current_prefix_tab
    assert current_right_click is not None
    assert "railmux-right-click-forward-v1-" in current_right_click
    assert "select-pane -t =" in current_right_click
    assert "send-keys -M" in current_right_click
    if tmux_ctl.tmux_version() >= (3, 4):
        assert manager.status_navigation_available
        assert current_status_click is not None
        assert "railmux-status-pane-v1-" in current_status_click
        assert "mouse_status_range" in current_status_click
        assert "select-pane -Z -t" in current_status_click
    else:
        assert not manager.status_navigation_available
        assert current_status_click == original_status_click["MouseDown1Status"]
    assert tmux_ctl.show_window_user_option(
        owner_pane, tmux_ctl.RAILMUX_CONTROLLER_OPTION) == owner_pane
    subprocess.run(["tmux", "set-option", "-g", "mouse", "on"], check=True)

    # Installation alone does not exercise if-shell's second parsing pass.
    # Send a real F8 through an attached client's root key table and prove it
    # reaches the controller pane without the historical ``-t`` parse error.
    if shutil.which("script") is None:
        pytest.skip("script(1) is required to emulate an attached client")
    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", owner_pane,
         "printf '\\033[?1000h\\033[?1006h'; exec bash --noprofile --norc"],
        check=True,
    )
    other_pane = subprocess.check_output(
        [
            "tmux", "split-window", "-h", "-t", owner_pane,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(
        ["tmux", "select-pane", "-t", other_pane], check=True)
    client_process = subprocess.Popen(
        _script_command(
            f"env TERM=xterm-256color tmux -S {shlex.quote(_socket_path)} "
            f"attach-session -t {shlex.quote(_display_session)}"
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    client_name = ""
    try:
        assert _wait_until(
            lambda: bool(subprocess.check_output(
                ["tmux", "list-clients", "-F", "#{client_name}"],
                text=True,
            ).strip())
        )
        client_name = subprocess.check_output(
            ["tmux", "list-clients", "-F", "#{client_name}"],
            text=True,
        ).strip()
        assert _wait_until(
            lambda: "bash-" in subprocess.check_output(
                ["tmux", "capture-pane", "-p", "-t", owner_pane],
                text=True,
            )
        )
        # A right-click over an unfocused mouse-aware Railmux pane must select
        # that pointer pane before forwarding the event. This executes the
        # wrapper's second parsing pass; inspecting list-keys alone would not
        # catch a routing command that installs but cannot run.
        assert client_process.stdin is not None
        client_process.stdin.write(b"\x1b[<2;2;2M")
        client_process.stdin.flush()
        assert _wait_until(
            lambda: tmux_ctl.active_pane_id(owner_pane) == owner_pane)
        subprocess.run(
            ["tmux", "respawn-pane", "-k", "-t", owner_pane,
             "bash --noprofile --norc"],
            check=True,
        )
        subprocess.run(
            ["tmux", "select-pane", "-t", other_pane], check=True)
        subprocess.run(
            ["tmux", "send-keys", "-K", "-c", client_name, "F8"],
            check=True,
        )
        assert _wait_until(
            lambda: "$ ~" in subprocess.check_output(
                ["tmux", "capture-pane", "-p", "-t", owner_pane],
                text=True,
            )
        )
        assert tmux_ctl.set_window_user_option(
            owner_pane, tmux_ctl.RAILMUX_TARGET_OPTION, other_pane)
        subprocess.run(
            ["tmux", "resize-pane", "-Z", "-t", other_pane], check=True)
        subprocess.run(
            ["tmux", "send-keys", "-K", "-c", client_name, "C-b"],
            check=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-K", "-c", client_name, "Tab"],
            check=True,
        )
        assert _wait_until(
            lambda: tmux_ctl.active_pane_id(owner_pane) == owner_pane)
        assert subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", owner_pane,
             "#{window_zoomed_flag}"],
            text=True,
        ).strip() == "1"
        subprocess.run(
            ["tmux", "send-keys", "-K", "-c", client_name, "C-b"],
            check=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-K", "-c", client_name, "Tab"],
            check=True,
        )
        assert _wait_until(
            lambda: tmux_ctl.active_pane_id(owner_pane) == other_pane)
        assert subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", other_pane,
             "#{window_zoomed_flag}"],
            text=True,
        ).strip() == "1"
        subprocess.run(
            ["tmux", "detach-client", "-t", client_name], check=True)
        output = client_process.communicate(timeout=2)[0]
    finally:
        if client_process.poll() is None:
            client_process.kill()
            client_process.wait()
    assert b"-t expects an argument" not in output

    # A newer user binding wins independently while F8 still round-trips.
    subprocess.run(
        [
            "tmux", "bind-key", "-T", "root", "F9",
            "display-message", "new-user-f9",
        ],
        check=True,
    )
    manager.close()
    restored = tmux_ctl.read_root_function_bindings()
    assert restored["F8"] == original["F8"]
    assert restored["F9"] is not None and "new-user-f9" in restored["F9"]
    assert tmux_ctl.read_prefix_target_binding() == original_prefix_tab
    assert tmux_ctl.read_root_right_click_binding() == original_right_click
    assert tmux_ctl.read_root_status_click_binding() == original_status_click
    assert tmux_ctl.show_window_user_option(
        owner_pane, tmux_ctl.RAILMUX_CONTROLLER_OPTION) is None


def test_real_tmux_status_pane_range_selects_and_keeps_zoom(
        isolated_tmux, monkeypatch, tmp_path):
    """A compact status control targets its declared pane, not the pane below."""
    if sys.platform == "darwin":
        pytest.skip(
            "a bare macOS PTY cannot answer tmux 3.7 terminal capability "
            "queries; the real click path is covered on Linux"
        )
    if tmux_ctl.tmux_version() < (3, 4):
        pytest.skip("pane-ID user status ranges need tmux 3.4")
    display_session, owner_pane, socket_path = isolated_tmux
    monkeypatch.setattr(
        "railmux.tmux_binding_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    manager = SharedTmuxBindingManager("status-range-server", owner_pane)
    assert manager.open()
    assert manager.status_navigation_available
    subprocess.run(["tmux", "set-option", "-g", "mouse", "on"], check=True)
    other_pane = subprocess.check_output(
        [
            "tmux", "split-window", "-h", "-t", owner_pane,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    subprocess.run(
        [
            "tmux", "set-option", "-t", display_session, "status-left",
            tmux_ctl.status_pane_range(owner_pane, "[R]"),
        ],
        check=True,
    )
    subprocess.run(
        ["tmux", "set-option", "-t", display_session,
         "status-left-length", "3"],
        check=True,
    )
    subprocess.run(
        ["tmux", "resize-pane", "-Z", "-t", other_pane], check=True)
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, 80, 0, 0),
    )
    client_env = os.environ.copy()
    client_env.pop("TMUX", None)
    client_env.pop("TMUX_PANE", None)
    client_env["TERM"] = "xterm-256color"
    client = subprocess.Popen(
        [
            "tmux", "-S", socket_path,
            "attach-session", "-t", display_session,
        ],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=client_env,
        start_new_session=True,
    )
    os.close(slave_fd)
    slave_fd = -1
    try:
        assert _wait_until(
            lambda: bool(subprocess.check_output(
                ["tmux", "list-clients", "-F", "#{client_name}"],
                text=True,
            ).strip())
        )
        client_name = subprocess.check_output(
            ["tmux", "list-clients", "-F", "#{client_name}"],
            text=True,
        ).strip()
        subprocess.run(
            ["tmux", "refresh-client", "-S", "-t", client_name],
            check=True,
        )
        os.set_blocking(master_fd, False)
        painted = bytearray()
        paint_deadline = time.monotonic() + 3.0
        while time.monotonic() < paint_deadline and b"[R]" not in painted:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if not readable:
                continue
            try:
                painted.extend(os.read(master_fd, 65536))
            except BlockingIOError:
                pass
        assert b"[R]" in painted
        client_height = int(subprocess.check_output(
            ["tmux", "display-message", "-p", "-c", client_name,
             "#{client_height}"],
            text=True,
        ).strip())
        # Follow the mouse protocol that tmux negotiated with this terminal.
        # macOS and Linux terminfo databases can make different choices for the
        # same TERM. Waiting for the first paint above also ensures the mode
        # enable sequence has arrived before the synthetic click.
        if b"\x1b[?1006h" in painted:
            click = f"\x1b[<0;2;{client_height}M".encode()
        else:
            # tmux's legacy parser keeps coordinates zero-based after removing
            # the 32-byte protocol offset (unlike SGR, which it decrements).
            assert client_height <= 224
            click = b"\x1b[M" + bytes((32, 32 + 1, 32 + client_height - 1))
        os.write(master_fd, click)
        assert _wait_until(
            lambda: tmux_ctl.active_pane_id(owner_pane) == owner_pane)
        assert subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", owner_pane,
             "#{window_zoomed_flag}"],
            text=True,
        ).strip() == "1"
    finally:
        if client.poll() is None:
            client.kill()
            client.wait()
        os.close(master_fd)
        if slave_fd >= 0:
            os.close(slave_fd)
        manager.close()


def test_real_selection_isolation_freezes_only_sibling_agent(
        isolated_tmux, monkeypatch, tmp_path):
    """Copy-mode selection stills its sibling without pausing the sidebar."""
    if tmux_ctl.tmux_version() < (3, 0):
        pytest.skip("pane-local options and configurable hooks need tmux 3.0")
    display_session, sidebar, _socket_path = isolated_tmux
    subprocess.run(
        ["tmux", "set-environment", "-g", "PYTHONPATH",
         str(Path(__file__).parents[1] / "src")],
        check=True,
    )
    monkeypatch.setattr(
        "railmux.tmux_binding_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    primary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", sidebar,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    secondary = subprocess.check_output(
        [
            "tmux", "split-window", "-d", "-h", "-t", primary,
            "-P", "-F", "#{pane_id}", "sleep 60",
        ],
        text=True,
    ).strip()
    workspace = AgentWorkspace()
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    workspace.primary.pane_id = primary
    workspace.secondary.pane_id = secondary
    bindings = SharedTmuxBindingManager("selection-server", sidebar)
    isolation = SelectionIsolationManager(sidebar)

    assert bindings.open()
    assert bindings.selection_isolation_available
    isolation.sync(workspace, enabled=True)
    subprocess.run(["tmux", "copy-mode", "-t", primary], check=True)
    key = f"{sidebar}:primary"

    assert _wait_until(
        lambda: (
            (state := tmux_ctl.selection_pane_state(secondary)) is not None
            and state.in_mode
            and state.frozen_by == key
        )
    )
    sidebar_state = tmux_ctl.selection_pane_state(sidebar)
    assert sidebar_state is not None and not sidebar_state.in_mode

    subprocess.run(
        ["tmux", "send-keys", "-X", "-t", primary, "cancel"], check=True)
    assert _wait_until(
        lambda: (
            (state := tmux_ctl.selection_pane_state(secondary)) is not None
            and not state.in_mode
            and state.frozen_by is None
        )
    )

    isolation.close()
    bindings.close()


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
