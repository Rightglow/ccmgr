"""State model for the tmux panes that display agents.

Only the primary slot is rendered today.  Keeping primary and secondary slots
inside one bounded workspace lets the dual-agent feature be added without
duplicating every pane, preview, focus, and restore field in ``App``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkspaceLayout(str, Enum):
    SINGLE = "single"
    STACKED = "stacked"
    SIDE_BY_SIDE = "side-by-side"


class DisplayTransportKind(str, Enum):
    NESTED = "nested"
    SWAP = "swap"


@dataclass
class SwapState:
    """Durable identities for one displayed real agent pane."""

    transaction_id: str
    agent_tmux_name: str
    agent_pane_id: str
    agent_pane_pid: int
    home_window_id: str
    placeholder_pane_id: str
    display_window_id: str
    keeper_session: str
    keeper_session_id: str
    outer_session_name: str
    outer_session_id: str
    owner_pane_id: str
    phase: str = "displayed"


@dataclass
class SlotRestoreState:
    """Content to restore after a read-only transcript preview exits."""

    kind: str  # "empty" | "agent"
    tmux_name: str | None = None


@dataclass
class AgentSlot:
    """All mutable display state owned by one outer tmux agent pane."""

    key: str
    pane_id: str | None = None
    agent_tmux_name: str | None = None
    active_session_id: str | None = None
    in_history_mode: bool = False
    restore_state: SlotRestoreState | None = None
    mode_key: str | None = None
    last_size: tuple[int, int] | None = None
    last_size_class: str | None = None
    transport_kind: DisplayTransportKind = DisplayTransportKind.NESTED
    swap_state: SwapState | None = None

    @property
    def is_open(self) -> bool:
        return self.pane_id is not None

    def clear_display(self) -> None:
        self.pane_id = None
        self.agent_tmux_name = None
        self.active_session_id = None
        self.in_history_mode = False
        self.restore_state = None
        self.mode_key = None
        self.last_size = None
        self.last_size_class = None
        self.transport_kind = DisplayTransportKind.NESTED
        self.swap_state = None


class AgentWorkspace:
    """At-most-two-slot workspace; current releases render primary only."""

    PRIMARY = "primary"
    SECONDARY = "secondary"

    def __init__(self) -> None:
        self.layout = WorkspaceLayout.SINGLE
        self.active_slot_key = self.PRIMARY
        self._slots = {
            self.PRIMARY: AgentSlot(self.PRIMARY),
            self.SECONDARY: AgentSlot(self.SECONDARY),
        }

    @property
    def primary(self) -> AgentSlot:
        return self._slots[self.PRIMARY]

    @property
    def secondary(self) -> AgentSlot:
        return self._slots[self.SECONDARY]

    @property
    def active(self) -> AgentSlot:
        return self._slots[self.active_slot_key]

    @property
    def slots(self) -> tuple[AgentSlot, AgentSlot]:
        return self.primary, self.secondary

    def slot_for_pane(self, pane_id: str) -> AgentSlot | None:
        return next((slot for slot in self.slots if slot.pane_id == pane_id), None)

    def slot_for_agent(self, tmux_name: str) -> AgentSlot | None:
        return next(
            (slot for slot in self.slots if slot.agent_tmux_name == tmux_name),
            None,
        )

    def can_display(self, slot: AgentSlot, tmux_name: str) -> bool:
        existing = self.slot_for_agent(tmux_name)
        return existing is None or existing is slot

    def activate(self, slot_key: str) -> AgentSlot:
        if slot_key not in self._slots:
            raise KeyError(slot_key)
        self.active_slot_key = slot_key
        return self._slots[slot_key]

    def collapse_to_primary(self) -> str | None:
        """Reset secondary state and return its outer pane ID for caller cleanup."""
        secondary = self.secondary
        pane_id = secondary.pane_id
        secondary.clear_display()
        self.layout = WorkspaceLayout.SINGLE
        self.active_slot_key = self.PRIMARY
        return pane_id
