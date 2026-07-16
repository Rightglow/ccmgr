"""Transactional de-nested display transport (tmux is modeled in memory)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from railmux import tmux_ctl
from railmux import display_transport as transport_mod
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.ui.workspace import AgentWorkspace, DisplayTransportKind


class FakeTmux:
    def __init__(self, monkeypatch):
        self.sessions = {
            "railmux": {"id": "$1", "windows": {"@1"}, "attached": 1},
            "agent-a": {"id": "$2", "windows": {"@2"}, "attached": 0},
            "agent-b": {"id": "$3", "windows": {"@3"}, "attached": 0},
        }
        self.panes = {
            "%0": tmux_ctl.PaneIdentity(
                "%0", 100, "railmux", "$1", "@1", False, 40, 30),
            "%2": tmux_ctl.PaneIdentity(
                "%2", 202, "agent-a", "$2", "@2", False, 80, 24),
            "%3": tmux_ctl.PaneIdentity(
                "%3", 303, "agent-b", "$3", "@3", False, 80, 24),
        }
        self.window_options: dict[tuple[str, str], str] = {}
        self.session_options: dict[tuple[str, str], str] = {}
        self.next_pane = 10
        self.next_session = 10
        self.swap_calls: list[tuple[str, str]] = []
        self.killed_sessions: list[str] = []
        self.fail_marker_window: str | None = None
        self.fail_swap_at: int | None = None
        self.respawned: list[tuple[str, str]] = []
        self._patch(monkeypatch)

    def _patch(self, monkeypatch):
        names = {
            "tmux_version": lambda: (3, 4),
            "pane_alive": lambda pane: pane in self.panes,
            "pane_identity": lambda pane: self.panes.get(pane),
            "session_exists": lambda name: name in self.sessions,
            "session_topology": self.session_topology,
            "session_has_window": lambda name, window: (
                name in self.sessions and window in self.sessions[name]["windows"]),
            "split_window_h": self.split_window_h,
            "respawn_pane": self.respawn_pane,
            "fit_session_to_pane": lambda *_args: True,
            "wait_session_detached": lambda name, timeout=1.0: (
                self.sessions[name]["attached"] == 0),
            "create_grouped_session": self.create_grouped_session,
            "set_session_user_option": self.set_session_user_option,
            "show_session_user_option": self.show_session_user_option,
            "set_window_user_option": self.set_window_user_option,
            "show_window_user_option": self.show_window_user_option,
            "swap_panes": self.swap_panes,
            "kill_session": self.kill_session,
            "kill_pane": self.kill_pane,
            "new_detached_session": self.new_detached_session,
            "select_pane": lambda _pane: True,
            "session_ids": lambda: frozenset(
                str(session["id"]) for session in self.sessions.values()),
            "list_window_user_options": self.list_window_user_options,
        }
        for name, value in names.items():
            monkeypatch.setattr(transport_mod.tmux_ctl, name, value)

    def _session_for_window(self, window: str) -> tuple[str, str]:
        for name, session in self.sessions.items():
            if window in session["windows"] and name != "railmux-keep-1":
                return name, str(session["id"])
        name, session = next(
            (name, session) for name, session in self.sessions.items()
            if window in session["windows"])
        return name, str(session["id"])

    def _relocate(self, pane_id: str, window: str) -> None:
        name, session_id = self._session_for_window(window)
        self.panes[pane_id] = replace(
            self.panes[pane_id], window_id=window,
            session_name=name, session_id=session_id)

    def session_topology(self, name: str):
        session = self.sessions.get(name)
        if session is None:
            return None
        windows = tuple(sorted(session["windows"]))
        panes = tuple(
            pane for pane in self.panes.values()
            if pane.window_id in session["windows"]
        )
        return tmux_ctl.SessionTopology(
            name, str(session["id"]), int(session["attached"]), windows, panes)

    def split_window_h(self, _cmd, **_kwargs):
        pane_id = f"%{self.next_pane}"
        self.next_pane += 1
        self.panes[pane_id] = tmux_ctl.PaneIdentity(
            pane_id, 1000 + self.next_pane, "railmux", "$1", "@1",
            False, 80, 30)
        return pane_id

    def respawn_pane(self, pane, command):
        if pane not in self.panes:
            return False
        self.respawned.append((pane, command))
        return True

    def create_grouped_session(self, name, target):
        if name in self.sessions or target not in self.sessions:
            return False
        self.sessions[name] = {
            "id": f"${self.next_session}",
            "windows": set(self.sessions[target]["windows"]),
            "attached": 0,
        }
        self.next_session += 1
        return True

    def set_session_user_option(self, session, name, value):
        if session not in self.sessions:
            return False
        key = (session, name)
        if value is None:
            self.session_options.pop(key, None)
        else:
            self.session_options[key] = value
        return True

    def show_session_user_option(self, session, name):
        return self.session_options.get((session, name))

    def set_window_user_option(self, window, name, value):
        if window == self.fail_marker_window:
            return False
        key = (window, name)
        if value is None:
            self.window_options.pop(key, None)
        else:
            self.window_options[key] = value
        return True

    def show_window_user_option(self, window, name):
        return self.window_options.get((window, name))

    def swap_panes(self, source, target):
        self.swap_calls.append((source, target))
        if self.fail_swap_at == len(self.swap_calls):
            return False
        if source not in self.panes or target not in self.panes:
            return False
        source_window = self.panes[source].window_id
        target_window = self.panes[target].window_id
        self._relocate(source, target_window)
        self._relocate(target, source_window)
        return True

    def kill_session(self, name):
        session = self.sessions.pop(name, None)
        if session is None:
            return False
        self.killed_sessions.append(name)
        still_owned = set().union(*(
            value["windows"] for value in self.sessions.values()))
        for pane_id in [
            pane_id for pane_id, pane in self.panes.items()
            if pane.window_id in session["windows"]
            and pane.window_id not in still_owned
        ]:
            del self.panes[pane_id]
        return True

    def kill_pane(self, pane):
        identity = self.panes.pop(pane, None)
        if identity is None:
            return False
        for name in [
            name for name, session in self.sessions.items()
            if identity.window_id in session["windows"]
            and not any(
                other.window_id == identity.window_id
                for other in self.panes.values())
        ]:
            del self.sessions[name]
        return True

    def new_detached_session(self, name, _command, env=None):
        if name in self.sessions:
            return False, "already exists"
        window = f"@{self.next_session}"
        pane = f"%{self.next_pane}"
        session_id = f"${self.next_session}"
        self.next_session += 1
        self.next_pane += 1
        self.sessions[name] = {
            "id": session_id, "windows": {window}, "attached": 0}
        self.panes[pane] = tmux_ctl.PaneIdentity(
            pane, 1000 + self.next_pane, name, session_id, window,
            False, 80, 24)
        return True, None

    def list_window_user_options(self, names):
        windows = sorted({
            window for session in self.sessions.values()
            for window in session["windows"]
        })
        return [tuple(
            [window]
            + [self.window_options.get((window, name), "") for name in names]
        ) for window in windows]


@pytest.fixture
def rig(monkeypatch):
    fake = FakeTmux(monkeypatch)
    workspace = AgentWorkspace()
    manager = AgentDisplayTransport(
        workspace, "swap", auto_launched=True,
        outer_session_name="railmux", outer_session_id="$1",
        owner_pane_id="%0")
    return fake, workspace, manager


def test_successful_swap_out_and_home(rig):
    fake, workspace, manager = rig
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.kind == DisplayTransportKind.SWAP
    assert workspace.primary.pane_id == "%2"
    assert fake.panes["%2"].window_id == "@1"
    placeholder = workspace.primary.swap_state.placeholder_pane_id
    assert fake.panes[placeholder].window_id == "@2"
    assert fake.panes["%2"].pane_pid == 202
    assert fake.window_options

    assert manager.return_home(workspace.primary)
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.pane_id == placeholder
    assert workspace.primary.swap_state is None
    assert not fake.window_options
    assert "railmux-keep-1" in fake.killed_sessions


def test_repeated_a_b_a_switch_keeps_process_identity(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.attach(workspace.primary, "agent-b").ok
    assert manager.attach(workspace.primary, "agent-a").ok

    assert fake.panes["%2"].pane_pid == 202
    assert fake.panes["%3"].pane_pid == 303
    assert fake.panes["%2"].window_id == "@1"
    assert fake.panes["%3"].window_id == "@3"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda fake: fake.sessions["agent-a"].update(attached=1),
         "independent client"),
        (lambda fake: fake.sessions["agent-a"]["windows"].add("@9"),
         "single live pane/window"),
    ],
)
def test_unsupported_target_falls_back_nested(rig, mutate, reason):
    fake, workspace, manager = rig
    mutate(fake)
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED
    assert reason in (outcome.reason or "")
    assert workspace.primary.swap_state is None


def test_old_tmux_falls_back_nested(rig, monkeypatch):
    _fake, workspace, manager = rig
    monkeypatch.setattr(transport_mod.tmux_ctl, "tmux_version", lambda: (2, 6))
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED


def test_marker_failure_never_moves_real_pane(rig):
    fake, workspace, manager = rig
    fake.fail_marker_window = "@1"
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert fake.panes["%2"].window_id == "@2"
    assert not fake.window_options


def test_inconsistent_existing_marker_fails_without_touching_agent(rig):
    fake, workspace, manager = rig
    fake.window_options[("@1", "@railmux_swap_primary")] = "not-json"
    outcome = manager.attach(workspace.primary, "agent-a")
    assert not outcome.ok
    assert "inconsistent" in (outcome.reason or "")
    assert fake.panes["%2"].window_id == "@2"
    assert fake.respawned == []


def test_swap_failure_clears_markers_and_falls_back(rig):
    fake, workspace, manager = rig
    fake.fail_swap_at = 1
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert fake.panes["%2"].window_id == "@2"
    assert not fake.window_options


def test_failed_verify_rolls_back_before_nested_fallback(rig, monkeypatch):
    fake, workspace, manager = rig
    real_verify = transport_mod._verified_displayed
    calls = 0

    def fail_first(state):
        nonlocal calls
        calls += 1
        return False if calls == 1 else real_verify(state)

    monkeypatch.setattr(transport_mod, "_verified_displayed", fail_first)
    outcome = manager.attach(workspace.primary, "agent-a")
    assert outcome.ok and outcome.fell_back
    assert len(fake.swap_calls) == 2
    assert fake.panes["%2"].window_id == "@2"


def test_failed_rollback_retains_recovery_state(rig, monkeypatch):
    fake, workspace, manager = rig
    monkeypatch.setattr(transport_mod, "_verified_displayed", lambda _state: False)
    fake.fail_swap_at = 2
    outcome = manager.attach(workspace.primary, "agent-a")

    assert not outcome.ok
    assert workspace.primary.swap_state is not None
    assert workspace.primary.pane_id == "%2"
    assert fake.window_options
    assert "railmux-keep-1" in fake.sessions


def test_failed_commit_rolls_back_before_nested_fallback(rig, monkeypatch):
    fake, workspace, manager = rig
    original = transport_mod._write_marker_pair
    writes = 0

    def fail_commit(state, slot_key):
        nonlocal writes
        writes += 1
        return False if writes == 2 else original(state, slot_key)

    monkeypatch.setattr(transport_mod, "_write_marker_pair", fail_commit)
    outcome = manager.attach(workspace.primary, "agent-a")

    assert outcome.ok and outcome.fell_back
    assert "commit failed" in (outcome.reason or "")
    assert fake.panes["%2"].window_id == "@2"
    assert len(fake.swap_calls) == 2


def test_return_failure_keeps_real_marked_and_never_kills_it(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    state = workspace.primary.swap_state
    assert state is not None
    fake.fail_swap_at = len(fake.swap_calls) + 1

    assert not manager.close_slot(workspace.primary)
    assert workspace.primary.swap_state is state
    assert "%2" in fake.panes
    assert fake.window_options


def test_two_distinct_slots_share_keeper_without_duplicate_agent(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert manager.attach(workspace.secondary, "agent-b").ok

    assert workspace.primary.swap_state.agent_pane_id == "%2"
    assert workspace.secondary.swap_state.agent_pane_id == "%3"
    assert workspace.primary.swap_state.keeper_session == (
        workspace.secondary.swap_state.keeper_session)
    assert fake.panes["%2"].window_id == "@1"
    assert fake.panes["%3"].window_id == "@1"


def test_preview_returns_real_home_and_keeps_display_placeholder(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    placeholder = workspace.primary.swap_state.placeholder_pane_id

    assert manager.prepare_preview(workspace.primary)
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.pane_id == placeholder
    assert placeholder in fake.panes


def test_late_external_client_returns_home_and_converts_to_nested(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    fake.sessions["agent-a"]["attached"] = 1

    outcome = manager.fallback_for_external_client(workspace.primary)

    assert outcome is not None and outcome.ok and outcome.fell_back
    assert outcome.kind == DisplayTransportKind.NESTED
    assert fake.panes["%2"].window_id == "@2"
    assert workspace.primary.agent_tmux_name == "agent-a"
    assert workspace.primary.swap_state is None


def test_two_slots_cannot_claim_same_real_pane(rig):
    _fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    assert not workspace.can_display(workspace.secondary, "agent-a")


def test_stale_owner_recovery_swaps_home_and_cleans_keeper(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    del fake.panes["%0"]  # model SIGKILL closing the Railmux owner pane

    report = recover_interrupted_swaps()

    assert report.repaired == 1
    assert report.unresolved == 0
    assert fake.panes["%2"].window_id == "@2"
    assert "railmux-keep-1" in fake.killed_sessions
    assert "railmux" in fake.killed_sessions


def test_active_owner_is_not_recovered(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    report = recover_interrupted_swaps()
    assert report.skipped_active == 1
    assert fake.panes["%2"].window_id == "@1"


def test_recovery_recreates_only_missing_marked_placeholder(rig):
    fake, workspace, manager = rig
    assert manager.attach(workspace.primary, "agent-a").ok
    state = workspace.primary.swap_state
    assert state is not None
    del fake.panes["%0"]
    assert fake.kill_pane(state.placeholder_pane_id)
    assert "agent-a" not in fake.sessions

    report = recover_interrupted_swaps()

    assert report.repaired == 1
    topology = fake.session_topology("agent-a")
    assert topology is not None
    assert topology.single_live_pane.pane_id == "%2"
    assert topology.single_live_pane.pane_pid == 202
