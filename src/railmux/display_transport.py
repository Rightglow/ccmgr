"""Provider-neutral transports for rendering an agent in an outer tmux pane.

The default swap transport moves the real agent pane into the display window
transactionally and records enough identity in tmux window options to repair
an interrupted operation. Nested attach remains the compatibility fallback for
unsupported or unverified environments and can be selected explicitly.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
from dataclasses import asdict, dataclass

from railmux import tmux_ctl
from railmux.ui.workspace import (
    AgentSlot,
    AgentWorkspace,
    DisplayTransportKind,
    SwapState,
    WorkspaceLayout,
)


_SCHEMA_VERSION = 1
_MARKERS = {
    AgentWorkspace.PRIMARY: "@railmux_swap_primary",
    AgentWorkspace.SECONDARY: "@railmux_swap_secondary",
}
_KEEPER_MARKER = "@railmux_swap_keeper"
_PLACEHOLDER_COMMAND = "while :; do sleep 3600; done"


def _empty_slot_command(slot: AgentSlot) -> str:
    pane_number = 2 if slot.key == AgentWorkspace.SECONDARY else 1
    return (
        f"{shlex.quote(sys.executable)} -m railmux.pane_surface "
        f"--empty {pane_number}"
    )


@dataclass(frozen=True)
class AttachOutcome:
    ok: bool
    kind: DisplayTransportKind
    reason: str | None = None
    fell_back: bool = False


@dataclass(frozen=True)
class RecoveryReport:
    repaired: int = 0
    skipped_active: int = 0
    unresolved: int = 0
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class KillPreparation:
    """Whether a displayed agent is safe to kill, with a truthful failure."""

    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _marker_for(slot_key: str) -> str:
    return _MARKERS.get(slot_key, _MARKERS[AgentWorkspace.PRIMARY])


def _record(state: SwapState, slot_key: str) -> dict[str, object]:
    return {
        "version": _SCHEMA_VERSION,
        "slot_key": slot_key,
        **asdict(state),
    }


def _encode_record(state: SwapState, slot_key: str) -> str:
    return json.dumps(
        _record(state, slot_key), sort_keys=True, separators=(",", ":"))


def _decode_record(raw: str) -> tuple[str, SwapState] | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
            return None
        slot_key = data["slot_key"]
        if slot_key not in _MARKERS:
            return None
        state_fields = {
            key: value for key, value in data.items()
            if key not in ("version", "slot_key")
        }
        state = SwapState(**state_fields)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    values = (
        state.transaction_id,
        state.agent_tmux_name,
        state.agent_pane_id,
        state.home_window_id,
        state.placeholder_pane_id,
        state.display_window_id,
        state.keeper_session,
        state.keeper_session_id,
        state.outer_session_name,
        state.outer_session_id,
        state.owner_pane_id,
        state.phase,
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    if not isinstance(state.agent_pane_pid, int) or state.agent_pane_pid <= 0:
        return None
    return slot_key, state


def _clear_matching_marker(window_id: str, marker: str, transaction_id: str) -> None:
    raw = tmux_ctl.show_window_user_option(window_id, marker)
    decoded = _decode_record(raw) if raw else None
    if decoded is not None and decoded[1].transaction_id == transaction_id:
        tmux_ctl.set_window_user_option(window_id, marker, None)


def _write_marker_pair(state: SwapState, slot_key: str) -> bool:
    marker = _marker_for(slot_key)
    raw = _encode_record(state, slot_key)
    if not tmux_ctl.set_window_user_option(state.home_window_id, marker, raw):
        return False
    if not tmux_ctl.set_window_user_option(state.display_window_id, marker, raw):
        _clear_matching_marker(
            state.home_window_id, marker, state.transaction_id)
        return False
    return True


def _clear_markers(state: SwapState, slot_key: str) -> None:
    marker = _marker_for(slot_key)
    for window_id in {state.home_window_id, state.display_window_id}:
        _clear_matching_marker(window_id, marker, state.transaction_id)


def _verified_home(state: SwapState) -> bool:
    real = tmux_ctl.pane_identity(state.agent_pane_id)
    placeholder = tmux_ctl.pane_identity(state.placeholder_pane_id)
    return bool(
        real is not None
        and placeholder is not None
        and real.pane_pid == state.agent_pane_pid
        and real.window_id == state.home_window_id
        and placeholder.window_id == state.display_window_id
        and tmux_ctl.session_has_window(
            state.agent_tmux_name, state.home_window_id)
    )


def _verified_displayed(state: SwapState) -> bool:
    real = tmux_ctl.pane_identity(state.agent_pane_id)
    placeholder = tmux_ctl.pane_identity(state.placeholder_pane_id)
    topology = tmux_ctl.session_topology(state.agent_tmux_name)
    return bool(
        real is not None
        and placeholder is not None
        and topology is not None
        and real.pane_pid == state.agent_pane_pid
        and real.window_id == state.display_window_id
        and placeholder.window_id == state.home_window_id
        and topology.attached_clients == 0
        and topology.single_live_pane is not None
        and topology.single_live_pane.pane_id == state.placeholder_pane_id
    )


class NestedDisplayTransport:
    """The compatibility transport: a nested tmux attach client."""

    def attach(
        self, slot: AgentSlot, agent_tmux_name: str,
    ) -> AttachOutcome:
        if (slot.pane_id is not None
                and slot.transport_kind == DisplayTransportKind.NESTED
                and slot.agent_tmux_name == agent_tmux_name
                and tmux_ctl.pane_alive(slot.pane_id)):
            return AttachOutcome(True, DisplayTransportKind.NESTED)

        created = False
        if not slot.pane_id or not tmux_ctl.pane_alive(slot.pane_id):
            pane_id = tmux_ctl.split_window_h(
                _PLACEHOLDER_COMMAND, size_percent=70, detached=True)
            if pane_id is None:
                return AttachOutcome(
                    False, DisplayTransportKind.NESTED,
                    "could not create the display pane",
                )
            slot.pane_id = pane_id
            created = True

        assert slot.pane_id is not None
        tmux_ctl.fit_session_to_pane(agent_tmux_name, slot.pane_id)
        command = (
            f"TMUX= exec tmux attach-session -t "
            f"{shlex.quote(agent_tmux_name)}"
        )
        if not tmux_ctl.respawn_pane(slot.pane_id, command):
            if created:
                tmux_ctl.kill_pane(slot.pane_id)
                slot.pane_id = None
            return AttachOutcome(
                False, DisplayTransportKind.NESTED,
                "could not start the nested tmux client",
            )
        slot.transport_kind = DisplayTransportKind.NESTED
        slot.swap_state = None
        slot.agent_tmux_name = agent_tmux_name
        return AttachOutcome(True, DisplayTransportKind.NESTED)


class AgentDisplayTransport:
    """Select nested/swap transport and own every destructive pane transition."""

    def __init__(
        self,
        workspace: AgentWorkspace,
        preference: str,
        *,
        auto_launched: bool,
        outer_session_name: str | None,
        outer_session_id: str | None,
        owner_pane_id: str | None,
    ) -> None:
        self.workspace = workspace
        self.preference = preference
        self.auto_launched = auto_launched
        self.outer_session_name = outer_session_name
        self.outer_session_id = outer_session_id
        self.owner_pane_id = owner_pane_id
        self.nested = NestedDisplayTransport()
        self._keeper_session: str | None = None
        self._keeper_session_id: str | None = None

    @property
    def swap_capable(self) -> tuple[bool, str | None]:
        if self.preference != "swap":
            return False, None
        if tmux_ctl.tmux_version() < (2, 7):
            return False, "tmux 2.7 or newer is required"
        if (not self.auto_launched
                or self.outer_session_name != "railmux"):
            return False, "swap transport is limited to the managed railmux session"
        if not (self.outer_session_id and self.owner_pane_id):
            return False, "outer tmux identity is unavailable"
        return True, None

    def _keeper_name(self) -> str:
        suffix = re.sub(r"[^A-Za-z0-9]", "", self.outer_session_id or "")
        return f"railmux-keep-{suffix or 'unknown'}"

    def _ensure_keeper(self) -> tuple[str, str] | None:
        if self._keeper_session and self._keeper_session_id:
            topology = tmux_ctl.session_topology(self._keeper_session)
            if (topology is not None
                    and topology.session_id == self._keeper_session_id):
                return self._keeper_session, self._keeper_session_id
            return None
        if not (self.outer_session_name and self.outer_session_id):
            return None
        name = self._keeper_name()
        expected = json.dumps(
            {"version": _SCHEMA_VERSION,
             "outer_session_id": self.outer_session_id},
            sort_keys=True, separators=(",", ":"),
        )
        if tmux_ctl.session_exists(name):
            if tmux_ctl.show_session_user_option(name, _KEEPER_MARKER) != expected:
                return None
        else:
            if not tmux_ctl.create_grouped_session(name, self.outer_session_name):
                return None
            if not tmux_ctl.set_session_user_option(name, _KEEPER_MARKER, expected):
                tmux_ctl.kill_session(name)
                return None
        topology = tmux_ctl.session_topology(name)
        if topology is None:
            return None
        self._keeper_session = name
        self._keeper_session_id = topology.session_id
        return name, topology.session_id

    def _drop_keeper_if_idle(self) -> None:
        if any(slot.swap_state is not None for slot in self.workspace.slots):
            return
        name = self._keeper_session
        session_id = self._keeper_session_id
        self._keeper_session = None
        self._keeper_session_id = None
        if not (name and session_id):
            return
        topology = tmux_ctl.session_topology(name)
        if topology is not None and topology.session_id == session_id:
            tmux_ctl.kill_session(name)

    def create_primary(self) -> bool:
        """Create the productized empty Pane 1 without attaching an agent."""
        primary = self.workspace.primary
        if (primary.pane_id is not None
                and tmux_ctl.pane_alive(primary.pane_id)):
            return True
        primary.clear_display()
        pane_id = tmux_ctl.split_window_h(
            _empty_slot_command(primary), size_percent=70, detached=True)
        if pane_id is None:
            return False
        primary.pane_id = pane_id
        return True

    def create_secondary(self, layout: WorkspaceLayout) -> bool:
        """Create an inert secondary display pane beside the primary slot."""
        primary = self.workspace.primary
        secondary = self.workspace.secondary
        if (secondary.pane_id is not None
                and tmux_ctl.pane_alive(secondary.pane_id)):
            return True
        if (primary.pane_id is None
                or not tmux_ctl.pane_alive(primary.pane_id)):
            return False
        secondary.clear_display()
        if layout is WorkspaceLayout.STACKED:
            pane_id = tmux_ctl.split_window_v(
                _empty_slot_command(secondary),
                target=primary.pane_id,
                size_percent=50,
                detached=True,
            )
        elif layout is WorkspaceLayout.SIDE_BY_SIDE:
            pane_id = tmux_ctl.split_window_h(
                _empty_slot_command(secondary),
                target=primary.pane_id,
                size_percent=50,
                detached=True,
            )
        else:
            return False
        secondary.pane_id = pane_id
        if pane_id is None:
            return False
        self.workspace.layout = layout
        return True

    def _prepare_placeholder(self, slot: AgentSlot) -> str | None:
        old_agent = slot.agent_tmux_name
        if not slot.pane_id or not tmux_ctl.pane_alive(slot.pane_id):
            slot.pane_id = tmux_ctl.split_window_h(
                _PLACEHOLDER_COMMAND, size_percent=70, detached=True)
        elif not tmux_ctl.respawn_pane(slot.pane_id, _PLACEHOLDER_COMMAND):
            return None
        if slot.pane_id is None:
            return None
        if old_agent:
            # Stop Railmux's own nested client first, then let the topology
            # gate distinguish a lingering independent client and select the
            # nested fallback. A nonzero count is not a placeholder failure.
            tmux_ctl.wait_session_detached(old_agent)
        slot.agent_tmux_name = None
        slot.transport_kind = DisplayTransportKind.NESTED
        slot.swap_state = None
        return slot.pane_id

    def _marker_gate(self, agent_tmux_name: str) -> str | None:
        rows = tmux_ctl.list_window_user_options(tuple(_MARKERS.values()))
        if rows is None:
            return "swap recovery metadata could not be audited"
        known = {
            state.transaction_id
            for slot in self.workspace.slots
            if (state := slot.swap_state) is not None
        }
        for _window, *values in rows:
            for raw in values:
                if not raw:
                    continue
                decoded = _decode_record(raw)
                if decoded is None:
                    return "inconsistent swap recovery metadata exists"
                state = decoded[1]
                if (state.agent_tmux_name == agent_tmux_name
                        and state.transaction_id not in known):
                    return "agent pane is owned by another swap transaction"
        return None

    def attach(self, slot: AgentSlot, agent_tmux_name: str) -> AttachOutcome:
        capable, reason = self.swap_capable
        if not capable:
            outcome = self.nested.attach(slot, agent_tmux_name)
            if outcome.ok and self.preference == "swap" and reason:
                return AttachOutcome(
                    True, DisplayTransportKind.NESTED, reason, fell_back=True)
            return outcome

        if (slot.swap_state is not None
                and slot.swap_state.agent_tmux_name == agent_tmux_name
                and _verified_displayed(slot.swap_state)):
            slot.pane_id = slot.swap_state.agent_pane_id
            return AttachOutcome(True, DisplayTransportKind.SWAP)

        if slot.swap_state is not None:
            if not self.return_home(slot, release_keeper=False):
                return AttachOutcome(
                    False, DisplayTransportKind.SWAP,
                    "could not safely return the displayed agent home",
                )

        marker_reason = self._marker_gate(agent_tmux_name)
        if marker_reason is not None:
            return AttachOutcome(
                False, DisplayTransportKind.SWAP, marker_reason)

        placeholder = self._prepare_placeholder(slot)
        if placeholder is None:
            return AttachOutcome(
                False, DisplayTransportKind.SWAP,
                "could not establish a stable display placeholder",
            )

        topology = tmux_ctl.session_topology(agent_tmux_name)
        real = topology.single_live_pane if topology is not None else None
        fallback_reason: str | None = None
        if topology is None:
            fallback_reason = "agent topology could not be inspected"
        elif topology.attached_clients != 0:
            fallback_reason = "agent session has an independent client"
        elif real is None:
            fallback_reason = "agent session is not a single live pane/window"
        elif any(
            other is not slot
            and other.swap_state is not None
            and other.swap_state.agent_pane_id == real.pane_id
            for other in self.workspace.slots
        ):
            fallback_reason = "agent pane is already owned by another slot"

        keeper = self._ensure_keeper() if fallback_reason is None else None
        if fallback_reason is None and keeper is None:
            fallback_reason = "could not establish the display keeper session"

        if fallback_reason is not None or real is None or keeper is None:
            self._drop_keeper_if_idle()
            outcome = self.nested.attach(slot, agent_tmux_name)
            if outcome.ok:
                return AttachOutcome(
                    True, DisplayTransportKind.NESTED,
                    fallback_reason, fell_back=True,
                )
            return outcome

        placeholder_identity = tmux_ctl.pane_identity(placeholder)
        if placeholder_identity is None:
            self._drop_keeper_if_idle()
            return self.nested.attach(slot, agent_tmux_name)
        if not tmux_ctl.session_has_window(
                keeper[0], placeholder_identity.window_id):
            self._drop_keeper_if_idle()
            outcome = self.nested.attach(slot, agent_tmux_name)
            return AttachOutcome(
                outcome.ok, outcome.kind,
                "keeper does not own the display window",
                fell_back=outcome.ok,
            )

        state = SwapState(
            transaction_id=uuid.uuid4().hex,
            agent_tmux_name=agent_tmux_name,
            agent_pane_id=real.pane_id,
            agent_pane_pid=real.pane_pid,
            home_window_id=real.window_id,
            placeholder_pane_id=placeholder,
            display_window_id=placeholder_identity.window_id,
            keeper_session=keeper[0],
            keeper_session_id=keeper[1],
            outer_session_name=self.outer_session_name or "railmux",
            outer_session_id=self.outer_session_id or "",
            owner_pane_id=self.owner_pane_id or "",
            phase="prepared",
        )
        if not _write_marker_pair(state, slot.key):
            self._drop_keeper_if_idle()
            outcome = self.nested.attach(slot, agent_tmux_name)
            return AttachOutcome(
                outcome.ok, outcome.kind,
                "could not persist swap recovery metadata",
                fell_back=outcome.ok,
            )
        if not tmux_ctl.swap_panes(state.agent_pane_id, placeholder):
            _clear_markers(state, slot.key)
            self._drop_keeper_if_idle()
            outcome = self.nested.attach(slot, agent_tmux_name)
            return AttachOutcome(
                outcome.ok, outcome.kind, "tmux swap-pane failed",
                fell_back=outcome.ok,
            )
        if not _verified_displayed(state):
            if _verified_home(state) or (
                    tmux_ctl.swap_panes(state.agent_pane_id, placeholder)
                    and _verified_home(state)):
                _clear_markers(state, slot.key)
                self._drop_keeper_if_idle()
                outcome = self.nested.attach(slot, agent_tmux_name)
                return AttachOutcome(
                    outcome.ok, outcome.kind,
                    "swap verification failed; rolled back",
                    fell_back=outcome.ok,
                )
            slot.swap_state = state
            slot.pane_id = state.agent_pane_id
            slot.transport_kind = DisplayTransportKind.SWAP
            slot.agent_tmux_name = agent_tmux_name
            return AttachOutcome(
                False, DisplayTransportKind.SWAP,
                "swap verification and rollback both failed; recovery metadata retained",
            )

        state.phase = "displayed"
        if not _write_marker_pair(state, slot.key):
            state.phase = "prepared"
            if (tmux_ctl.swap_panes(state.agent_pane_id, placeholder)
                    and _verified_home(state)):
                _clear_markers(state, slot.key)
                self._drop_keeper_if_idle()
                outcome = self.nested.attach(slot, agent_tmux_name)
                return AttachOutcome(
                    outcome.ok, outcome.kind,
                    "swap commit failed; rolled back",
                    fell_back=outcome.ok,
                )
            slot.swap_state = state
            slot.pane_id = state.agent_pane_id
            slot.transport_kind = DisplayTransportKind.SWAP
            slot.agent_tmux_name = agent_tmux_name
            return AttachOutcome(
                False, DisplayTransportKind.SWAP,
                "swap commit and rollback failed; recovery metadata retained",
            )

        slot.swap_state = state
        slot.pane_id = state.agent_pane_id
        slot.transport_kind = DisplayTransportKind.SWAP
        slot.agent_tmux_name = agent_tmux_name
        return AttachOutcome(True, DisplayTransportKind.SWAP)

    def return_home(self, slot: AgentSlot, *, release_keeper: bool = True) -> bool:
        state = slot.swap_state
        if state is None:
            if release_keeper:
                self._drop_keeper_if_idle()
            return True
        if _verified_home(state):
            _clear_markers(state, slot.key)
        else:
            real = tmux_ctl.pane_identity(state.agent_pane_id)
            placeholder = tmux_ctl.pane_identity(state.placeholder_pane_id)
            if not (
                real is not None
                and placeholder is not None
                and real.pane_pid == state.agent_pane_pid
                and real.window_id == state.display_window_id
                and placeholder.window_id == state.home_window_id
            ):
                return False
            returning = SwapState(**{**asdict(state), "phase": "returning"})
            _write_marker_pair(returning, slot.key)
            if not tmux_ctl.swap_panes(
                    state.agent_pane_id, state.placeholder_pane_id):
                return False
            if not _verified_home(state):
                return False
            _clear_markers(state, slot.key)
        slot.pane_id = state.placeholder_pane_id
        slot.agent_tmux_name = None
        slot.swap_state = None
        slot.transport_kind = DisplayTransportKind.NESTED
        if release_keeper:
            self._drop_keeper_if_idle()
        return True

    def prepare_preview(self, slot: AgentSlot) -> bool:
        if not self.return_home(slot):
            return False
        return slot.pane_id is None or tmux_ctl.pane_alive(slot.pane_id)

    def reset_slot(self, slot: AgentSlot) -> bool:
        """Return any agent home and leave one truthful branded empty pane."""
        if not self.return_home(slot):
            return False
        pane_id = slot.pane_id
        if pane_id is None or not tmux_ctl.pane_alive(pane_id):
            return False
        if not tmux_ctl.respawn_pane(pane_id, _empty_slot_command(slot)):
            return False
        slot.clear_content()
        return True

    def prepare_kill(self, agent_tmux_name: str) -> KillPreparation:
        slot = self.workspace.slot_for_agent(agent_tmux_name)
        if slot is None:
            return KillPreparation(True)
        was_swap = slot.swap_state is not None
        if not self.return_home(slot):
            return KillPreparation(
                False,
                f"could not safely return {agent_tmux_name} home; "
                "nothing was killed",
            )
        pane_id = slot.pane_id
        if pane_id is None or not tmux_ctl.pane_alive(pane_id):
            slot.clear_display()
            return KillPreparation(True)
        if not tmux_ctl.respawn_pane(pane_id, _empty_slot_command(slot)):
            if was_swap:
                # return_home already detached the real pane. Keep the model
                # truthful even though tmux could not paint the branded idle
                # surface: this slot now displays only its inert placeholder.
                slot.clear_content()
                error = (
                    f"{agent_tmux_name} returned home, but Pane "
                    f"{2 if slot.key == AgentWorkspace.SECONDARY else 1} "
                    "could not be reset; nothing was killed"
                )
            else:
                error = (
                    f"could not detach {agent_tmux_name} from its nested "
                    "display; nothing was killed"
                )
            return KillPreparation(False, error)
        slot.clear_content()
        return KillPreparation(True)

    def close_slot(self, slot: AgentSlot) -> bool:
        if not self.return_home(slot):
            return False
        if slot.pane_id and tmux_ctl.pane_alive(slot.pane_id):
            if not tmux_ctl.kill_pane(slot.pane_id):
                return False
        slot.clear_display()
        self._drop_keeper_if_idle()
        return True

    def close_all(self) -> bool:
        ok = True
        for slot in self.workspace.slots:
            if slot.swap_state is not None and not self.return_home(slot):
                ok = False
        for slot in self.workspace.slots:
            if slot.swap_state is None and slot.pane_id:
                try:
                    tmux_ctl.kill_pane(slot.pane_id)
                except Exception:
                    ok = False
                slot.clear_display()
        self._drop_keeper_if_idle()
        return ok

    def displayed_real_pane(self, agent_tmux_name: str) -> str | None:
        slot = self.workspace.slot_for_agent(agent_tmux_name)
        if slot is None or slot.swap_state is None:
            return None
        return slot.swap_state.agent_pane_id

    def displayed_real_pid(self, agent_tmux_name: str) -> int | None:
        slot = self.workspace.slot_for_agent(agent_tmux_name)
        if slot is None or slot.swap_state is None:
            return None
        identity = tmux_ctl.pane_identity(slot.swap_state.agent_pane_id)
        return identity.pane_pid if identity is not None else None

    def reap_dead_display(self, slot: AgentSlot) -> str | None:
        state = slot.swap_state
        if state is None or tmux_ctl.pane_identity(state.agent_pane_id) is not None:
            return None
        topology = tmux_ctl.session_topology(state.agent_tmux_name)
        if (topology is not None and topology.single_live_pane is not None
                and topology.single_live_pane.pane_id == state.placeholder_pane_id):
            tmux_ctl.kill_session(state.agent_tmux_name)
        _clear_markers(state, slot.key)
        agent = state.agent_tmux_name
        slot.clear_display()
        self._drop_keeper_if_idle()
        return agent

    def fallback_for_external_client(
        self, slot: AgentSlot,
    ) -> AttachOutcome | None:
        """Move home if a client attached after swap validation raced us."""
        state = slot.swap_state
        if state is None:
            return None
        topology = tmux_ctl.session_topology(state.agent_tmux_name)
        if topology is None or topology.attached_clients == 0:
            return None
        agent = state.agent_tmux_name
        if not self.return_home(slot):
            return AttachOutcome(
                False, DisplayTransportKind.SWAP,
                "an external client attached and the agent could not return home",
            )
        outcome = self.nested.attach(slot, agent)
        if outcome.ok:
            slot.agent_tmux_name = agent
            return AttachOutcome(
                True, DisplayTransportKind.NESTED,
                "an external client attached after swap", fell_back=True,
            )
        return outcome

    def outer_session_lost(self) -> bool:
        if self.preference != "swap" or not self.outer_session_id:
            return False
        ids = tmux_ctl.session_ids()
        return ids is not None and self.outer_session_id not in ids


def recover_interrupted_swaps() -> RecoveryReport:
    """Repair stale marked swaps without adopting any unmarked pane."""
    rows = tmux_ctl.list_window_user_options(tuple(_MARKERS.values()))
    if rows is None:
        return RecoveryReport()
    grouped: dict[str, tuple[str, SwapState, set[str]]] = {}
    malformed = 0
    messages: list[str] = []
    for row in rows:
        window_id, *values = row
        for marker, raw in zip(_MARKERS.values(), values):
            if not raw:
                continue
            decoded = _decode_record(raw)
            if decoded is None:
                malformed += 1
                messages.append(f"invalid swap marker on {window_id}")
                continue
            slot_key, state = decoded
            if marker != _marker_for(slot_key):
                malformed += 1
                messages.append(f"slot marker mismatch on {window_id}")
                continue
            existing = grouped.get(state.transaction_id)
            if existing is not None and existing[1] != state:
                malformed += 1
                messages.append(
                    f"conflicting swap transaction {state.transaction_id}")
                continue
            windows = existing[2] if existing is not None else set()
            windows.add(window_id)
            grouped[state.transaction_id] = (slot_key, state, windows)

    ids = tmux_ctl.session_ids()
    repaired = 0
    skipped = 0
    unresolved = malformed
    keepers_in_use: set[tuple[str, str]] = set()
    stale_outer_sessions: set[tuple[str, str]] = set()
    for slot_key, state, windows in grouped.values():
        owner_alive = tmux_ctl.pane_identity(state.owner_pane_id) is not None
        outer_alive = ids is None or state.outer_session_id in ids
        if owner_alive and outer_alive:
            skipped += 1
            keepers_in_use.add((state.keeper_session, state.keeper_session_id))
            continue

        real = tmux_ctl.pane_identity(state.agent_pane_id)
        placeholder = tmux_ctl.pane_identity(state.placeholder_pane_id)
        fixed = False
        if (real is not None and real.pane_pid == state.agent_pane_pid
                and real.window_id == state.home_window_id):
            fixed = True
        elif (
            real is not None
            and placeholder is not None
            and real.pane_pid == state.agent_pane_pid
            and real.window_id == state.display_window_id
            and placeholder.window_id == state.home_window_id
            and tmux_ctl.swap_panes(
                state.agent_pane_id, state.placeholder_pane_id)
            and _verified_home(state)
        ):
            fixed = True
        elif (
            real is not None
            and real.pane_pid == state.agent_pane_pid
            and real.window_id == state.display_window_id
            and placeholder is None
            and not tmux_ctl.session_exists(state.agent_tmux_name)
        ):
            # The placeholder was the home session's only pane, so its death
            # removed that session. Recreate only the exact marked home name;
            # never replace or adopt an existing user topology.
            created, _reason = tmux_ctl.new_detached_session(
                state.agent_tmux_name, _PLACEHOLDER_COMMAND)
            topology = (
                tmux_ctl.session_topology(state.agent_tmux_name)
                if created else None)
            new_placeholder = (
                topology.single_live_pane if topology is not None else None)
            if (new_placeholder is not None
                    and topology.attached_clients == 0):
                state = SwapState(**{
                    **asdict(state),
                    "placeholder_pane_id": new_placeholder.pane_id,
                    "home_window_id": new_placeholder.window_id,
                    "phase": "returning",
                })
                if (_write_marker_pair(state, slot_key)
                        and tmux_ctl.swap_panes(
                            state.agent_pane_id, state.placeholder_pane_id)
                        and _verified_home(state)):
                    fixed = True
        elif real is None and placeholder is not None:
            topology = tmux_ctl.session_topology(state.agent_tmux_name)
            if (topology is not None and topology.single_live_pane is not None
                    and topology.single_live_pane.pane_id
                    == state.placeholder_pane_id):
                tmux_ctl.kill_session(state.agent_tmux_name)
                fixed = True

        if not fixed:
            unresolved += 1
            keepers_in_use.add((state.keeper_session, state.keeper_session_id))
            messages.append(
                f"could not safely repair swap transaction {state.transaction_id}")
            continue
        for window_id in windows | {
                state.home_window_id, state.display_window_id}:
            _clear_matching_marker(
                window_id, _marker_for(slot_key), state.transaction_id)
        repaired += 1
        if not owner_alive or not outer_alive:
            stale_outer_sessions.add(
                (state.outer_session_name, state.outer_session_id))

    for _slot_key, state, _windows in grouped.values():
        keeper = (state.keeper_session, state.keeper_session_id)
        if keeper in keepers_in_use:
            continue
        topology = tmux_ctl.session_topology(state.keeper_session)
        if topology is not None and topology.session_id == state.keeper_session_id:
            tmux_ctl.kill_session(state.keeper_session)
        keepers_in_use.add(keeper)

    for session_name, session_id in stale_outer_sessions:
        topology = tmux_ctl.session_topology(session_name)
        if topology is not None and topology.session_id == session_id:
            tmux_ctl.kill_session(session_name)

    return RecoveryReport(
        repaired=repaired,
        skipped_active=skipped,
        unresolved=unresolved,
        messages=tuple(messages),
    )
