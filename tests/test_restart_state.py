"""Restart-state schema, identity, isolation, and cleanup tests."""
from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import restart_state, tmux_ctl
from railmux.restart_state import OuterTmuxIdentity


def _identity(
    *,
    server: str = "a",
    pid: int = 100,
    pane: str = "%1",
    session: str = "$1",
    window: str = "@1",
) -> OuterTmuxIdentity:
    return OuterTmuxIdentity(server * 64, pid, pane, session, window)


def _instance_payload(identity: OuterTmuxIdentity, mode: str = "claude") -> dict:
    return {
        "schema_version": restart_state.SCHEMA_VERSION,
        "kind": "instance",
        "owner": identity.to_json(),
        "view": restart_state.build_view(
            {"mode": mode, "project": "-tmp-project"}),
        "recovery": {"right_kind": "empty"},
    }


def test_storage_key_is_pane_scoped_but_survives_window_session_move():
    original = _identity()
    moved = _identity(session="$9", window="@9")
    other_pane = _identity(pane="%2")
    other_server = _identity(server="b")

    assert original.storage_key == moved.storage_key
    assert original.storage_key != other_pane.storage_key
    assert original.storage_key != other_server.storage_key


def test_capture_identity_hashes_socket_and_server_lifetime(
    monkeypatch, tmp_path,
):
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    monkeypatch.setenv("TMUX", f"{socket_path},4321,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        restart_state.tmux_ctl,
        "pane_identity",
        lambda pane: tmux_ctl.PaneIdentity(
            pane, 99, "same-name", "$3", "@5", False, 100, 30),
    )

    identity = restart_state.capture_outer_identity()

    assert identity is not None
    assert identity.server_pid == 4321
    assert identity.pane_id == "%7"
    assert str(socket_path) not in identity.server_digest
    assert len(identity.server_digest) == 64


def test_capture_identity_ignores_mutable_socket_ctime(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux.sock,4321,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        restart_state.tmux_ctl,
        "pane_identity",
        lambda pane: tmux_ctl.PaneIdentity(
            pane, 99, "same-name", "$3", "@5", False, 100, 30),
    )
    monkeypatch.setattr(
        restart_state, "_process_start_token", lambda _pid: "proc-start:77")
    ctime = [100]
    monkeypatch.setattr(
        restart_state.os,
        "stat",
        lambda _path: SimpleNamespace(
            st_dev=1, st_ino=2, st_ctime_ns=ctime[0]),
    )

    first = restart_state.capture_outer_identity()
    ctime[0] = 200
    second = restart_state.capture_outer_identity()

    assert first is not None and second is not None
    assert first.server_digest == second.server_digest


def test_capture_identity_changes_when_server_process_start_changes(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux.sock,4321,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        restart_state.tmux_ctl,
        "pane_identity",
        lambda pane: tmux_ctl.PaneIdentity(
            pane, 99, "same-name", "$3", "@5", False, 100, 30),
    )
    monkeypatch.setattr(
        restart_state.os,
        "stat",
        lambda _path: SimpleNamespace(st_dev=1, st_ino=2),
    )
    process_start = ["proc-start:77"]
    monkeypatch.setattr(
        restart_state, "_process_start_token", lambda _pid: process_start[0])

    first = restart_state.capture_outer_identity()
    process_start[0] = "proc-start:88"
    second = restart_state.capture_outer_identity()

    assert first is not None and second is not None
    assert first.server_digest != second.server_digest


def test_capture_identity_requires_tmux_and_live_exact_pane(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert restart_state.capture_outer_identity() is None

    monkeypatch.setenv("TMUX", "/tmp/socket,123,0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(restart_state.tmux_ctl, "pane_identity", lambda _pane: None)
    assert restart_state.capture_outer_identity() is None


def test_instance_write_is_atomic_and_private(tmp_path):
    identity = _identity()
    path = tmp_path / "private" / "instances" / "state.json"

    assert restart_state.write_instance(
        identity, _instance_payload(identity), path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert restart_state.decode_instance(
        restart_state.read_json_object(path), identity
    )["right_kind"] == "empty"


def test_managed_handoff_accepts_only_dead_owner_on_same_server(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(restart_state, "runtime_base", lambda: tmp_path)
    source = _identity(pane="%1", session="$1", window="@1")
    replacement = _identity(pane="%2", session="$2", window="@2")

    assert restart_state.write_managed_handoff(source)
    monkeypatch.setattr(
        restart_state.tmux_ctl,
        "pane_identity",
        lambda _pane: MagicMock(dead=False),
    )
    assert restart_state.read_managed_handoff(replacement) is None

    monkeypatch.setattr(
        restart_state.tmux_ctl, "pane_identity", lambda _pane: None)
    assert restart_state.read_managed_handoff(replacement) == source
    assert not restart_state.clear_managed_handoff(
        replacement, _identity(pane="%9"))
    assert restart_state.clear_managed_handoff(replacement, source)
    assert restart_state.read_managed_handoff(replacement) is None


def test_tmp_fallback_hardens_every_railmux_owned_directory(
    monkeypatch, tmp_path,
):
    fallback = tmp_path / "fallback"
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(restart_state, "runtime_base", lambda: fallback)
    identity = _identity()

    assert restart_state.write_instance(identity, _instance_payload(identity))

    for path in (
        fallback,
        fallback / "railmux",
        fallback / "railmux" / "instances",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_instance_write_rejects_insecure_runtime_directory(tmp_path):
    identity = _identity()
    parent = tmp_path / "insecure"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "state.json"

    assert not restart_state.write_instance(
        identity, _instance_payload(identity), path)
    assert not path.exists()


def test_write_failures_are_bounded_for_read_only_or_full_storage(
    monkeypatch, tmp_path,
):
    identity = _identity()
    monkeypatch.setattr(
        restart_state,
        "atomic_write_text",
        MagicMock(side_effect=OSError("read-only or full")),
    )

    assert not restart_state.write_instance(
        identity, _instance_payload(identity), tmp_path / "local" / "state.json")
    assert not restart_state.write_portable(
        {"schema_version": 1}, tmp_path / "portable.json")


def test_decode_schemas_fail_closed_independently():
    identity = _identity()
    portable = {
        "schema_version": 1,
        "kind": "portable",
        "view": restart_state.build_view(
            {"mode": "codex", "project_filter": "rail"}),
    }
    assert restart_state.decode_portable(portable) == {
        "mode": "codex", "project_filter": "rail"}

    for version in (0, 2):
        candidate = dict(portable, schema_version=version)
        assert restart_state.decode_portable(candidate) is None
    assert restart_state.decode_portable({"schema_version": 1}) is None

    local = _instance_payload(identity)
    assert restart_state.decode_instance(local, identity) is not None
    assert restart_state.decode_instance(local, _identity(pane="%8")) is None
    assert restart_state.decode_instance(dict(local, schema_version=2), identity) is None


def test_portable_view_round_trips_stable_display_without_tmux_authority():
    payload = {
        "schema_version": restart_state.SCHEMA_VERSION,
        "kind": "portable",
        "view": restart_state.build_view({
            "mode": "codex",
            "project": "-tmp-sidebar",
            "right_kind": "agent",
            "right_mode": "claude",
            "right_session": "session-uuid",
            "right_project": "-tmp-agent-project",
            "right_tmux": "cc-must-not-be-serialized",
        }),
    }

    assert restart_state.decode_portable(payload) == {
        "mode": "codex",
        "project": "-tmp-sidebar",
        "right_kind": "agent",
        "right_mode": "claude",
        "right_session": "session-uuid",
        "right_project": "-tmp-agent-project",
    }
    assert "right_tmux" not in json.dumps(payload)


def test_legacy_migration_extracts_no_process_authority():
    view = restart_state.legacy_portable_view({
        "codex_mode": True,
        "project": "-tmp-project",
        "session": "session-id",
        "right_kind": "agent",
        "right_tmux": "cx-private",
        "running_bindings": [{"tmux_name": "cx-private"}],
    })

    assert view == {
        "mode": "codex",
        "project": "-tmp-project",
        "session": "session-id",
    }
    assert "right_tmux" not in view
    assert "running_bindings" not in view


def test_concurrent_instance_saves_do_not_overwrite_each_other(tmp_path):
    first = _identity(pane="%1")
    second = _identity(pane="%2")
    first_path = tmp_path / "instances" / "first.json"
    second_path = tmp_path / "instances" / "second.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: restart_state.write_instance(*args),
            [
                (first, _instance_payload(first, "claude"), first_path),
                (second, _instance_payload(second, "codex"), second_path),
            ],
        ))

    assert results == [True, True]
    assert restart_state.decode_instance(
        restart_state.read_json_object(first_path), first)["mode"] == "claude"
    assert restart_state.decode_instance(
        restart_state.read_json_object(second_path), second)["mode"] == "codex"


def test_exact_owner_workspace_round_trips_but_portable_cannot_carry_it():
    identity = _identity()
    workspace = {
        "version": 1,
        "layout": "stacked",
        "target": "secondary",
        "focus": "secondary",
        "collapsed_secondary": {
            "tmux": "cx-collapsed",
            "session": "collapsed-session",
            "mode": "codex",
        },
        "slots": {
            "primary": {
                "kind": "agent",
                "tmux": "cc-primary",
                "session": "primary-session",
                "mode": "claude",
            },
            "secondary": {
                "kind": "preview",
                "session": "secondary-session",
                "mode": "codex",
                "project": "-tmp-secondary",
                "restore": {"kind": "agent", "tmux": "cx-secondary"},
            },
        },
    }
    payload = _instance_payload(identity)
    payload["recovery"]["workspace"] = workspace

    decoded = restart_state.decode_instance(payload, identity)

    assert decoded is not None and decoded["workspace"] == workspace
    portable = restart_state.build_view({
        "mode": "claude",
        "workspace": workspace,
    })
    assert "workspace" not in json.dumps(portable)


def test_instance_rejects_inconsistent_or_unbounded_workspace():
    identity = _identity()
    base = {
        "version": 1,
        "layout": "side-by-side",
        "target": "secondary",
        "focus": "primary",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "empty"},
        },
    }
    payload = _instance_payload(identity)
    payload["recovery"]["workspace"] = base
    assert restart_state.decode_instance(payload, identity) is None

    payload["recovery"]["workspace"] = {
        **base,
        "focus": "secondary",
        "slots": {
            **base["slots"],
            "secondary": {"kind": "agent", "tmux": "x" * 257},
        },
    }
    assert restart_state.decode_instance(payload, identity) is None

    payload["recovery"]["workspace"] = {
        **base,
        "layout": "single",
        "target": "primary",
        "focus": "sidebar",
        "slots": {
            "primary": {"kind": "empty"},
            "secondary": {"kind": "agent", "tmux": "cx-secondary"},
        },
    }
    assert restart_state.decode_instance(payload, identity) is None


def test_concurrent_portable_writes_remain_valid_json(tmp_path):
    path = tmp_path / "view.json"
    payloads = [
        {"schema_version": 1, "kind": "portable",
         "view": restart_state.build_view({"mode": mode})}
        for mode in ("claude", "codex")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(
            lambda payload: restart_state.write_portable(payload, path),
            payloads,
        ))

    stored = json.loads(path.read_text())
    assert stored in payloads


def test_portable_second_node_never_contains_local_identity(tmp_path):
    path = tmp_path / "view.json"
    payload = {
        "schema_version": 1,
        "kind": "portable",
        "view": restart_state.build_view(
            {"mode": "codex", "project": "-cx-project"}),
    }
    assert restart_state.write_portable(payload, path)

    decoded = restart_state.decode_portable(restart_state.read_json_object(path))
    assert decoded == {"mode": "codex", "project": "-cx-project"}
    assert "owner" not in path.read_text()
    assert "right_tmux" not in path.read_text()


def test_cleanup_removes_only_old_proven_dead_owners(monkeypatch, tmp_path):
    root = tmp_path / "instances"
    root.mkdir(mode=0o700)
    current = _identity(server="a", pid=os.getpid(), pane="%1")
    current_path = root / f"instance-{current.storage_key}.json"
    dead_same = _identity(server="a", pid=os.getpid(), pane="%2")
    dead_other = _identity(server="b", pid=99999999, pane="%3")
    live_other = _identity(server="c", pid=os.getpid(), pane="%4")
    paths = []
    for identity in (current, dead_same, dead_other, live_other):
        path = root / f"instance-{identity.storage_key}.json"
        path.write_text(json.dumps(_instance_payload(identity)))
        os.utime(path, (1, 1))
        paths.append(path)
    monkeypatch.setattr(restart_state, "instances_dir", lambda: root)
    monkeypatch.setattr(
        restart_state,
        "instance_state_path",
        lambda identity: root / f"instance-{identity.storage_key}.json",
    )
    monkeypatch.setattr(
        restart_state.tmux_ctl,
        "pane_identity",
        lambda pane: None if pane == "%2" else MagicMock(),
    )

    removed = restart_state.cleanup_stale_instances(current, now=10**9)

    assert removed == 2
    assert current_path.exists()
    assert not paths[1].exists()
    assert not paths[2].exists()
    assert paths[3].exists()


def test_cleanup_preserves_corrupt_and_newer_live_unknown_state(
    monkeypatch, tmp_path,
):
    root = tmp_path / "instances"
    root.mkdir(mode=0o700)
    corrupt = root / "instance-corrupt.json"
    corrupt.write_text("not json")
    newer = root / "instance-newer.json"
    newer.write_text(json.dumps({"schema_version": 2, "kind": "instance"}))
    for path in (corrupt, newer):
        os.utime(path, (1, 1))
    current = _identity()
    monkeypatch.setattr(restart_state, "instances_dir", lambda: root)
    monkeypatch.setattr(
        restart_state, "instance_state_path", lambda _identity: root / "current.json")

    assert restart_state.cleanup_stale_instances(current, now=10**9) == 0
    assert corrupt.exists() and newer.exists()
