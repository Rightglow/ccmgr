"""State model for the tmux panes that display up to two agents.

Primary retains the established single-pane behavior. The split
exposes secondary without duplicating pane, focus, and transport state in
``App``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkspaceLayout(str, Enum):
    SINGLE = "single"
    STACKED = "stacked"
    SIDE_BY_SIDE = "side-by-side"


class WorkspacePresentation(str, Enum):
    """How the existing pane topology is presented to one terminal.

    Presentation is intentionally independent from :class:`WorkspaceLayout`.
    A compact client still owns the same one/two agent panes; it merely zooms
    one page at a time instead of forcing those panes into unusably small
    rectangles.
    """

    WIDE = "wide"
    COMPACT = "compact"


class WorkspacePage(str, Enum):
    """One full-window page available in compact presentation."""

    SIDEBAR = "sidebar"
    PRIMARY = "primary"
    SECONDARY = "secondary"


COMPACT_ENTER_WIDTH = 80
COMPACT_ENTER_HEIGHT = 24
COMPACT_EXIT_WIDTH = 84
COMPACT_EXIT_HEIGHT = 26
SINGLE_SIDEBAR_PERCENT = 30
DUAL_SIDEBAR_PERCENT = 20
DUAL_SIDEBAR_MIN_WIDTH = 30
MINIMUM_AGENT_PANE_SIZE = (50, 12)


def presentation_for_geometry(
    current: WorkspacePresentation,
    width: int,
    height: int,
) -> WorkspacePresentation:
    """Apply hysteresis to responsive presentation selection.

    Either cramped dimension enters compact mode. Both dimensions must clear
    the larger exit threshold before a resize returns to wide mode. Thus a
    short 105x20 phone is compact while a 100x60 portrait monitor remains wide.
    """
    if current is WorkspacePresentation.WIDE:
        if width < COMPACT_ENTER_WIDTH or height < COMPACT_ENTER_HEIGHT:
            return WorkspacePresentation.COMPACT
        return current
    if width >= COMPACT_EXIT_WIDTH and height >= COMPACT_EXIT_HEIGHT:
        return WorkspacePresentation.WIDE
    return current


def next_workspace_layout(layout: WorkspaceLayout) -> WorkspaceLayout:
    """Cycle single -> columns -> rows -> single."""
    return {
        WorkspaceLayout.SINGLE: WorkspaceLayout.SIDE_BY_SIDE,
        WorkspaceLayout.SIDE_BY_SIDE: WorkspaceLayout.STACKED,
        WorkspaceLayout.STACKED: WorkspaceLayout.SINGLE,
    }[layout]


def projected_agent_size(
    region: tuple[int, int], layout: WorkspaceLayout,
) -> tuple[int, int]:
    """Size of each equal agent pane after splitting *region* once."""
    width, height = region
    if layout is WorkspaceLayout.SIDE_BY_SIDE:
        return max(0, (width - 1) // 2), height
    if layout is WorkspaceLayout.STACKED:
        return width, max(0, (height - 1) // 2)
    return width, height


def sidebar_width_for_layout(
    layout: WorkspaceLayout,
    window_width: int,
    sidebar_permille: int | None = None,
) -> int:
    """Return the bounded responsive width of the sidebar divider."""
    if sidebar_permille is None:
        percent = (
            SINGLE_SIDEBAR_PERCENT
            if layout is WorkspaceLayout.SINGLE
            else DUAL_SIDEBAR_PERCENT
        )
        width = round(window_width * percent / 100)
    else:
        width = round(window_width * sidebar_permille / 1000)
    if layout is not WorkspaceLayout.SINGLE:
        width = max(DUAL_SIDEBAR_MIN_WIDTH, width)
    return min(max(1, width), max(1, window_width - 2))


def wide_layout_fits_geometry(
    width: int,
    height: int,
    layout: WorkspaceLayout,
    *,
    sidebar_permille: int | None = None,
    exit_margin: bool = False,
) -> bool:
    """Whether an unzoomed logical layout can keep every agent pane usable."""
    if layout is WorkspaceLayout.SINGLE:
        return True
    sidebar_width = sidebar_width_for_layout(
        layout,
        width,
        sidebar_permille,
    )
    agent_region = (max(0, width - sidebar_width - 1), height)
    pane_width, pane_height = projected_agent_size(agent_region, layout)
    min_width, min_height = MINIMUM_AGENT_PANE_SIZE
    if exit_margin:
        min_width += 2
        min_height += 1
    return pane_width >= min_width and pane_height >= min_height


def terminal_size_class(
    width: int,
    height: int,
    *,
    minimum: tuple[int, int],
    recommended: tuple[int, int],
) -> str:
    """Classify one outer terminal without coupling policy to UI effects."""
    min_width, min_height = minimum
    rec_width, rec_height = recommended
    if width < min_width or height < min_height:
        return "critical"
    if width < rec_width or height < rec_height:
        return "reduced"
    return "comfortable"


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
    project_key: str | None = None
    last_size: tuple[int, int] | None = None
    last_size_class: str | None = None
    transport_kind: DisplayTransportKind = DisplayTransportKind.NESTED
    swap_state: SwapState | None = None

    @property
    def is_open(self) -> bool:
        return self.pane_id is not None

    def clear_display(self) -> None:
        self.pane_id = None
        self.clear_content()

    def clear_content(self) -> None:
        """Forget displayed content while retaining the owned outer pane."""
        self.agent_tmux_name = None
        self.active_session_id = None
        self.in_history_mode = False
        self.restore_state = None
        self.mode_key = None
        self.project_key = None
        self.last_size = None
        self.last_size_class = None
        self.transport_kind = DisplayTransportKind.NESTED
        self.swap_state = None


class AgentWorkspace:
    """At-most-two-slot workspace with one explicit Target pane.

    ``target`` is the canonical product concept: sidebar actions operate on
    this slot. While an agent owns keyboard focus it is also the focused pane;
    while the sidebar owns focus it remains only the remembered Target pane.
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"

    def __init__(self) -> None:
        self.layout = WorkspaceLayout.SINGLE
        self.presentation = WorkspacePresentation.WIDE
        self.compact_page = WorkspacePage.SIDEBAR
        self.target_slot_key = self.PRIMARY
        # Closing the outer secondary pane must not kill its detached agent.
        # Keep its exact instance-local tmux name so F8 can reopen the same
        # target while cycling back from single to a dual layout.
        self.collapsed_secondary_agent: str | None = None
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
    def target(self) -> AgentSlot:
        return self._slots[self.target_slot_key]

    @property
    def active_slot_key(self) -> str:
        """Compatibility view of :attr:`target_slot_key` for released callers."""
        return self.target_slot_key

    @active_slot_key.setter
    def active_slot_key(self, slot_key: str) -> None:
        self.target_slot_key = slot_key

    @property
    def active(self) -> AgentSlot:
        """Compatibility view of :attr:`target` for released callers."""
        return self.target

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

    def set_target(self, slot_key: str) -> AgentSlot:
        """Make one slot the Target pane for subsequent sidebar actions."""
        if slot_key not in self._slots:
            raise KeyError(slot_key)
        self.target_slot_key = slot_key
        return self._slots[slot_key]

    def activate(self, slot_key: str) -> AgentSlot:
        """Compatibility wrapper for the previously released workspace API."""
        return self.set_target(slot_key)

    def collapse_to_primary(self) -> str | None:
        """Reset secondary state and return its outer pane ID for caller cleanup."""
        secondary = self.secondary
        pane_id = secondary.pane_id
        secondary.clear_display()
        self.layout = WorkspaceLayout.SINGLE
        self.target_slot_key = self.PRIMARY
        return pane_id
