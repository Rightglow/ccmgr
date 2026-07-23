"""Top-level urwid app: provider sidebar + tmux agent workspace.

Railmux runs in the left pane of a tmux window beside a bounded workspace that
can display one or two agents without duplicating provider state. Press `i` for
a session-info popup.
"""
from __future__ import annotations

import math
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import urwid

from railmux import (
    legacy_sessions,
    orphan_marker,
    tmux_ctl,
    tmux_health,
    tmux_server,
)
from railmux.atomic_file import atomic_write_text
from railmux.background_index import BackgroundCodexIndex
from railmux.config import Config
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.discovery import invalidate_session, list_projects
from railmux.favorites import Favorites
from railmux.help_workspace import (
    is_help_workspace,
    materialize_help_workspace,
)
from railmux.settings import LayoutProfile, Settings
from railmux.launcher import (
    build_codex_new_command,
    build_codex_resume_command,
    build_new_session_command,
    build_resume_command,
)
from railmux.modes import (
    CODEX_MODE,
    DEFAULT_MODE_REGISTRY,
    AgentMode,
    ModeRegistry,
    ProjectSource,
)
from railmux.models import AttentionState, Project, SessionMeta
from railmux.tmux_binding_manager import SharedTmuxBindingManager
from railmux.mouse_manager import RootWheelForwardingManager
from railmux.renames import Renames
from railmux import restart_state
from railmux.session_cache import SessionCache
from railmux.scroll_manager import ScrollManager
from railmux.selection_isolation import SelectionIsolationManager
from railmux.ui import keymap
from railmux.ui.modals import (
    ContextMenu,
    DeleteConfirmModal,
    ExitProgressModal,
    HelpModal,
    LayoutSaveModal,
    OptionsModal,
    PathBrowserModal,
    ProjectInfoModal,
    QuitConfirmModal,
    RenameModal,
    RunningInfoModal,
    SessionInfoModal,
)
from railmux.ui.projects_pane import ProjectsPane
from railmux.ui.running_pane import RunningEntry, RunningSessionsPane
from railmux.ui.sessions_pane import SessionsPane
from railmux.ui.sidebar import (
    SidebarSection,
    StableWeightedPile,
    UnifiedSidebarFrame,
)
from railmux.ui.statusbar import ButtonBar, HintBar, TIPS
from railmux.ui.workspace import (
    AgentSlot,
    AgentWorkspace,
    SlotRestoreState,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
    next_workspace_layout,
    presentation_for_geometry,
    projected_agent_size,
)


_GRASS_GREEN = "#5faf00"
_DEEP_GRASS_GREEN = "#005200"
_SLATE = "#3a3a3a"
_BODY = "#d0d0d0"
_STATUS_YELLOW = "#ffd700"
_STATUS_RED = "#ff5f5f"
_TERMINAL_COLORS = 2**24


PALETTE = [
    # Status-bar message levels. Idle tips are dim; info neutral green;
    # warn/error escalate so failures stand out from routine feedback.
    ("status_info", "light green", "default", "", _GRASS_GREEN, "default"),
    ("status_warn", "yellow,bold", "default", "", f"{_STATUS_YELLOW},bold", "default"),
    ("status_tip", "dark gray", "default"),
    # Explicit body colour prevents the outer pane-focus AttrMap from leaking
    # into ordinary rows. Grass green is reserved for navigation focus:
    # bright on pane chrome and deep behind the current cursor row.
    ("body", "light gray", "default", "", _BODY, "default"),
    ("focus", "white,bold", "dark green", "bold", "#ffffff,bold", _DEEP_GRASS_GREEN),
    # Persistent right-pane target. Slate stays visible after focus moves but
    # does not compete with the grass-green input focus.
    ("selected", "white,bold", "dark gray", "bold", "#ffffff,bold", _SLATE),
    ("title", "white,bold", ""),
    ("dim", "dark gray", ""),
    # Session-row metadata remains visibly secondary even when its title is
    # focused or selected. Deliberately omit bold from all three variants.
    ("session_meta", "dark gray", "", "", "#808080", "default"),
    ("session_meta_focus", "light gray", "dark green", "", "#b8b8b8", _DEEP_GRASS_GREEN),
    ("session_meta_sel", "light gray", "dark gray", "", "#b0b0b0", _SLATE),
    ("modal_key", "yellow,bold", "", "bold", f"{_STATUS_YELLOW},bold", "default"),
    # ButtonBar — bright bold + underline reads as a clickable control.
    ("btn", "white,bold,underline", ""),
    ("btn_pressed", "white,bold", "dark green", "bold", "#ffffff,bold", _DEEP_GRASS_GREEN),
    # A live tmux session is structural state, not lifecycle status. Its grass-
    # green title is independent from the idle/busy/blocked status-dot colour.
    ("live", "light green,bold", "", "bold", f"{_GRASS_GREEN},bold", "default"),
    ("current_path", "yellow,bold", ""),
    # Status dots — the ● glyph carries its own palette attribute so it keeps
    # its colour on any row background. Each status has three background
    # variants so it blends into normal / focused (deep grass) / selected (slate)
    # rows. The foreground itself never changes with row focus, so red/yellow/
    # green remain stable status signals. (The star is plain text — no colour —
    # so it just inherits whatever the row's highlight is.)
    ("status_idle", "light green,bold", "", "bold", f"{_GRASS_GREEN},bold", "default"),
    ("status_idle_focus", "light green,bold", "dark green",
     "bold", f"{_GRASS_GREEN},bold", _DEEP_GRASS_GREEN),
    ("status_idle_sel", "light green,bold", "dark gray",
     "bold", f"{_GRASS_GREEN},bold", _SLATE),
    ("status_busy", "yellow,bold", "", "bold", f"{_STATUS_YELLOW},bold", "default"),
    ("status_busy_focus", "yellow,bold", "dark green",
     "bold", f"{_STATUS_YELLOW},bold", _DEEP_GRASS_GREEN),
    ("status_busy_sel", "yellow,bold", "dark gray",
     "bold", f"{_STATUS_YELLOW},bold", _SLATE),
    ("status_blocked", "light red,bold", "", "bold", f"{_STATUS_RED},bold", "default"),
    ("status_blocked_focus", "light red,bold", "dark green",
     "bold", f"{_STATUS_RED},bold", _DEEP_GRASS_GREEN),
    ("status_blocked_sel", "light red,bold", "dark gray",
     "bold", f"{_STATUS_RED},bold", _SLATE),
    # Attention is a separate conversational outcome, not another activity
    # state. Magenta ``!`` badges therefore never compete with green/yellow/red
    # status dots or with the grass-green liveness/focus signal.
    ("attention", "light magenta,bold", ""),
    ("attention_focus", "light magenta,bold", "dark green",
     "bold", "#ff5fff,bold", _DEEP_GRASS_GREEN),
    ("attention_sel", "light magenta,bold", "dark gray",
     "bold", "#ff5fff,bold", _SLATE),
    ("legacy", "yellow,bold", "", "bold", f"{_STATUS_YELLOW},bold", "default"),
    ("legacy_focus", "yellow,bold", "dark green",
     "bold", f"{_STATUS_YELLOW},bold", _DEEP_GRASS_GREEN),
    ("legacy_sel", "yellow,bold", "dark gray",
     "bold", f"{_STATUS_YELLOW},bold", _SLATE),
    # Pane border. Dim by default; grass green when focused. Keep
    # row selection and status colours separate so green retains a clear focus
    # role rather than also meaning "selected" or "idle".
    ("pane", "dark gray", ""),
    ("pane_focus", "light green,bold", "", "bold", f"{_GRASS_GREEN},bold", ""),
]


# Colours for the outer tmux status bar (railmux's only status surface). The bar is
# green in normal use; on an error the WHOLE bar flips to dark red so the alert is
# unmissable and the line reads as one block (not just a red pill on green). The
# brand (status-left) follows the bar so its fg stays legible in both modes.
_TMUX_BAR_STYLE_NORMAL = f"bg={_GRASS_GREEN},fg=colour0"  # grass, black default fg
_TMUX_BAR_STYLE_ERROR = "bg=colour52,fg=colour231"  # dark-red bar, white fg
_TMUX_BRAND_NORMAL = "#[fg=colour0] Railmux #[default]"
_TMUX_BRAND_ERROR = "#[fg=colour231] Railmux #[default]"


def _tmux_status_left(
    error: bool,
    mode_label: str | bool,
    layout_indicator: str | None = None,
) -> str:
    """The tmux status-left segment: the ``railmux`` brand plus a current-mode
    indicator (``· Claude Code`` / ``· Codex``) and a compact workspace-layout
    glyph. All are rendered in the tips colour (colour0 = black on green, or
    white on red)."""
    brand = _TMUX_BRAND_ERROR if error else _TMUX_BRAND_NORMAL
    fg = "colour231" if error else "colour0"
    # Bool support is a compatibility bridge for callers from <=0.1.1. New
    # code passes the registered label so a third mode renders correctly.
    if isinstance(mode_label, bool):
        mode_label = "Codex" if mode_label else "Claude Code"
    layout = f" · {layout_indicator}" if layout_indicator else ""
    return f"{brand}#[fg={fg}]· {mode_label}{layout} #[default]"


def _compact_tmux_status_left(
    error: bool,
    mode_label: str,
    page: WorkspacePage,
    panes: tuple[str | None, str | None, str | None],
    width: int,
    range_wrapper: Callable[[str, str], str] | None = None,
) -> tuple[str, int]:
    """Build the responsive compact navigation and its visible cell length.

    The first three controls keep stable short labels on phone-width terminals
    and expand only when room is available. tmux format directives are excluded
    from the returned length so ``status-left-length`` can be set precisely.
    """
    if width < 52:
        labels = ("R", "1", "2")
        mode = "Cx" if mode_label == "Codex" else (
            "CC" if mode_label == "Claude Code" else mode_label[:2])
    elif width < 80:
        labels = ("Railmux", "A1", "A2")
        mode = "Codex" if mode_label == "Codex" else (
            "CC" if mode_label == "Claude Code" else mode_label[:8])
    else:
        labels = ("Railmux", "Agent 1", "Agent 2")
        mode = mode_label

    pages = (
        WorkspacePage.SIDEBAR,
        WorkspacePage.PRIMARY,
        WorkspacePage.SECONDARY,
    )
    fg_inactive = "colour0"
    fg_active = "colour231"
    rendered: list[str] = []
    visible = 0
    for candidate, pane_id, label in zip(pages, panes, labels):
        # Keep all three positions stable even before F8 creates Pane 2. A
        # missing pane has no tmux range, so it is visible but not clickable.
        content = (
            f"#[fg={fg_active if candidate is page else fg_inactive}]"
            f"[{label}]"
        )
        if range_wrapper is not None and pane_id is not None:
            content = range_wrapper(pane_id, content)
        rendered.append(content)
        visible += len(label) + 2
    suffix = f"#[fg={fg_inactive if not error else 'colour231'}] {mode} "
    visible += len(mode) + 2
    return "".join(rendered) + suffix + "#[default]", visible

# Per-level foreground for the status text (status-right). No pill backgrounds:
# info/warn/tip sit directly on the green bar (info white, warn bold gold, tip
# black/muted); error is white-bold because the whole bar is already dark red.
_TMUX_LEVEL_STYLE = {
    "info": "#[fg=colour231]",
    "warn": "#[fg=colour220,bold]",
    "error": "#[fg=colour231,bold]",
    "tip": "#[fg=colour0]",
}

# How often the Running pane is re-ordered by recency.  Re-sorting on every poll
# would make rows jump under the cursor mid-click, so it's throttled to this.
_RUNNING_SORT_INTERVAL = 60.0

# Cross-platform identity stamp stored on each detached agent tmux session.
# Unlike the short-lived runtime state file, a session option lives exactly as
# long as the tmux session and survives Railmux restarts without renaming it.
_SESSION_BINDING_OPTION = "@railmux_binding_v1"
_HELP_SESSION_OPTION = "@railmux_help_v1"
_HELP_POLICY_VERSION = "read-only-auto-v2"


@dataclass
class _Running:
    """One agent session opened by this Railmux instance.

    Replaces the four parallel dicts that previously tracked running sessions
    (tmux name, label, project, placeholder) and had to be kept in sync by hand.
    Keyed in ``App._running`` by ``key``: the real session_id, or a
    ``__new__-N`` placeholder until the session's JSONL appears on disk.
    """
    key: str
    tmux_name: str
    label: str
    project: Project | None = None
    placeholder_path: Path | None = None  # cwd to resolve against, while a placeholder
    created_at: float = 0.0                # launch time, for placeholder resolution
    # Session ids that already existed in the launch cwd BEFORE this placeholder
    # launched. A placeholder must only ever bind a session id NOT in this set,
    # so a rollout another process wrote to the same cwd can't be mis-bound (#12).
    pre_launch_ids: frozenset[str] = frozenset()
    # False for stamp-only unresolved recovery: without the complete launch
    # snapshot and without procfs, an exactly-one heuristic could bind a
    # pre-existing same-cwd rollout. Such entries wait for authoritative
    # correlation or a later state-enriched restart.
    allow_heuristic_resolution: bool = True
    status: str = "idle"                   # "idle" | "busy" | "blocked"
    last_mtime: float = 0.0                # session JSONL mtime, for recency sort
    session_type: str = "claude"           # "claude" | "codex" — which CLI owns it
    attention: AttentionState | None = None
    orphan: orphan_marker.Marker | None = None
    # Sessions created before Railmux moved to its dedicated tmux server stay
    # usable in-place.  These fields pin their old server/session identity and
    # make every action route explicitly instead of relying on ``$TMUX``.
    legacy_server: tmux_server.TmuxServerTarget | None = None
    legacy_session_id: str | None = None
    provider_session_id: str | None = None

    @property
    def is_placeholder(self) -> bool:
        return self.key.startswith("__new__-")

    @property
    def is_legacy(self) -> bool:
        return self.legacy_server is not None

    @property
    def logical_session_id(self) -> str | None:
        if self.provider_session_id is not None:
            return self.provider_session_id
        return None if self.is_placeholder else self.key


class _CloseOnClickOverlay(urwid.Overlay):
    """An ``urwid.Overlay`` that calls *on_click_outside* when the user
    left-clicks anywhere outside the overlay's area.

    The overlay calculates its own screen-space rectangle so it can tell
    "inside, but the child didn't handle the event" from "truly outside".
    Without this, a left-click on non-interactive content inside the modal
    (e.g. a ``Text`` row) would propagate ``False`` back up, and the
    overlay would misinterpret it as an outside click and dismiss the
    modal.
    """

    def __init__(self, top_w: urwid.Widget, bottom_w: urwid.Widget,
                 align, width, valign, height,
                 on_click_outside: Callable[[], None]) -> None:
        self._on_click_outside = on_click_outside
        super().__init__(top_w, bottom_w, align, width, valign, height)

    # -- screen-space rectangle -------------------------------------------

    def _overlay_rect(self, size) -> tuple[int, int, int, int]:
        """Return ``(left, top, width, height)`` of the overlay in screen
        coordinates, matching urwid's own layout calculation."""
        maxcol, maxrow = size
        # Resolve width (int or ("relative", percent)).
        if isinstance(self.width, tuple) and self.width[0] == "relative":
            ow = int(maxcol * self.width[1] / 100)
        else:
            ow = self.width
        # Resolve height.
        if isinstance(self.height, tuple) and self.height[0] == "relative":
            oh = int(maxrow * self.height[1] / 100)
        else:
            oh = self.height
        # Horizontal alignment.
        align = self.align
        if align == "center":
            left = (maxcol - ow) // 2
        elif align == "right":
            left = maxcol - ow
        else:
            left = 0
        # Vertical alignment.
        valign = self.valign
        if valign == "middle":
            top = (maxrow - oh) // 2
        elif valign == "bottom":
            top = maxrow - oh
        else:
            top = 0
        return left, top, ow, oh

    # -- mouse ------------------------------------------------------------

    def mouse_event(self, size, event, button, col, row, focus):
        left, top, ow, oh = self._overlay_rect(size)
        within = (left <= col < left + ow and top <= row < top + oh)

        if within:
            # Inside the overlay: let the top widget handle scroll events
            # etc., but always return True — a click inside the modal must
            # never trigger on_click_outside, even when the child widget
            # doesn't consume the event (e.g. a plain Text row).
            super().mouse_event(size, event, button, col, row, focus)
            return True

        # Outside the overlay: delegate to the bottom widget (the frame).
        handled = super().mouse_event(size, event, button, col, row, focus)
        if not handled and event == "mouse press" and button == 1:
            self._on_click_outside()
            return True
        return handled


class _FocusAwareFrame(urwid.Frame):
    """Frame that can suppress all descendant focus maps when tmux focus leaves."""

    def __init__(self, *args, **kwargs) -> None:
        self._window_active = True
        super().__init__(*args, **kwargs)

    def set_window_active(self, active: bool) -> None:
        if self._window_active == active:
            return
        self._window_active = active
        self._invalidate()

    def render(self, size, focus: bool = False):
        return super().render(size, focus=focus and self._window_active)


@dataclass
class _ModeViewState:
    """UI state owned by one agent mode.

    The dictionary that stores these objects is keyed by a stable mode name, so
    adding another provider does not require another pair of fields or teach
    existing modes about one another. More per-mode view state can be added here
    as switching behaviour grows.
    """

    selected_project_path: Path | None = None
    running_filter: str = ""


class App:
    _HELP_SESSION_PREFIX = "railmux-help-v1-"
    # tmux may apply DoubleClick1Pane after the application's double callback.
    # Wait past that multi-click window before selecting the right pane.
    _DOUBLE_CLICK_FOCUS_DELAY = 0.35
    _double_focus_alarm: object | None = None
    _double_focus_visual_pending: bool = False
    # Set while dropping a bracketed-paste burst; see _filter_input.  Class-level
    # default so partially-built instances (App.__new__ in tests) are safe.
    _in_paste: bool = False
    # Whether the in-progress paste is being delivered to a text field (rename /
    # filter / path browser) rather than dropped.  Decided once at "begin paste".
    _paste_passthrough: bool = False
    # Fallback for terminals WITHOUT bracketed paste: this many single-character
    # keys arriving in one input read is treated as a paste in command mode.
    # Bracketed paste is the precise primary guard; this is a coarse net.
    _PASTE_BURST_MIN = 2
    # Global project counts/order are less latency-sensitive than the selected
    # session list and are expensive on NFS homes.
    _PROJECT_SCAN_INTERVAL = 3.0
    _project_snapshot: list[Project] | None = None
    _project_snapshot_at: float = 0.0
    # Session to re-focus in the sidebar after a restart, applied once the
    # Sessions pane's rows are (re)built. Class default so bare ``App.__new__``
    # instances in tests are safe before ``__init__`` sets it.
    _pending_focus_session: str | None = None
    # Status-bar state defaults at class scope so methods invoked on a bare
    # ``App.__new__(App)`` (e.g. in unit tests) don't hit AttributeError before
    # ``__init__`` runs. ``__init__`` reassigns these per instance.
    _status_text: str | None = None
    _status_level: str = "info"
    _status_since: float = 0.0
    _attention_notice_key: tuple[str, int] | None = None
    _tip_index: int = 0
    _tip_since: float = 0.0
    # railmux's status line is rendered into the OUTER tmux status bar (full
    # terminal width) — there is no in-pane status widget. Off until run() wires
    # it up; session-scoped so it never touches the user's global tmux config.
    _tmux_status_enabled: bool = False
    _tmux_status_session: str | None = None
    _TMUX_STATUS_RIGHT_LENGTH = 200
    # Whether the bar is currently in error mode (whole bar dark red). Tracked so
    # the style swap only fires on the normal↔error transition, not every render.
    _tmux_error_bar: bool = False
    # tmux >= 3.3 can draw arrows into shared pane borders.  These fields keep
    # the temporary side-by-side focus treatment reversible, including when
    # the user's original window option was inherited rather than explicit.
    _border_indicators_original_known: bool = False
    _border_indicators_original: str | None = None
    _border_indicators_arrows: bool = False
    # Static options railmux sets on the OUTER tmux status bar. The bar
    # background (status-style) and brand (status-left) are set dynamically per
    # error state (see _apply_tmux_bar / _TMUX_BAR_STYLE_OPTIONS). All are
    # session-scoped and reverted with `set-option -u` on teardown, so the user's
    # global tmux config is untouched. Rationale:
    #   - window-status-*: blanked. railmux's outer session has a single fixed window,
    #     so its `0:tmux*` list entry is pure noise.
    #   - status: forced on (the bar is now the only status surface).
    #   - status-right-length: raised from the ~40 default so messages aren't cut.
    #   - status-left-length: raised from the 10 default so "railmux · Claude Code"
    #     (the brand + mode indicator) isn't truncated.
    _TMUX_BAR_OPTIONS = (
        ("status", "on"),
        ("window-status-format", ""),
        ("window-status-current-format", ""),
        ("status-right-length", str(_TMUX_STATUS_RIGHT_LENGTH)),
        ("status-left-length", "40"),
    )
    # Bar options set dynamically (normal green / error red); reverted on teardown.
    _TMUX_BAR_STYLE_OPTIONS = ("status-style", "status-left")
    # Below the recommended size Railmux remains usable but warns once per
    # size-class transition. Below the hard floor the status bar turns red;
    # actions stay available so a remote user is never trapped by a resize.
    _RECOMMENDED_TERMINAL_SIZE = (120, 30)
    # Compact presentation remains functional down to the fast SSH client's
    # protocol floor. Everything below the wide recommendation is still
    # reported as reduced, but a normal phone portrait is not a red error.
    _MINIMUM_TERMINAL_SIZE = (40, 12)
    _RECOMMENDED_AGENT_PANE_SIZE = (80, 20)
    _MINIMUM_AGENT_PANE_SIZE = (50, 12)
    _SINGLE_SIDEBAR_PERCENT = 30
    _DUAL_SIDEBAR_PERCENT = 20
    _DUAL_SIDEBAR_MIN_WIDTH = 30

    # -- compatibility shims -------------------------------------------------
    # Tests and third-party extensions built against pre-workspace releases may
    # still touch the old scalar attributes. Keep them as properties backed by
    # the primary slot; application code below uses the workspace model.

    def _agent_workspace(self) -> AgentWorkspace:
        workspace = getattr(self, "_workspace", None)
        if workspace is None:
            workspace = AgentWorkspace()
            self._workspace = workspace
        return workspace

    def _display_transport(self) -> AgentDisplayTransport:
        manager = getattr(self, "_display_transport_manager", None)
        if manager is None:
            config = getattr(self, "_config", Config())
            wants_swap = config.agent_transport == "swap"
            manager = AgentDisplayTransport(
                self._agent_workspace(),
                config.agent_transport,
                auto_launched=getattr(self, "_auto_launched", False),
                outer_session_name=(
                    tmux_ctl.current_session_name() if wants_swap else None),
                outer_session_id=(
                    tmux_ctl.current_session_id() if wants_swap else None),
                owner_pane_id=(
                    getattr(self, "_railmux_pane_id", None)
                    if wants_swap else None),
            )
            self._display_transport_manager = manager
        return manager

    @property
    def _primary_slot(self) -> AgentSlot:
        return self._agent_workspace().primary

    @property
    def _right_pane_id(self) -> str | None:
        return self._primary_slot.pane_id

    @_right_pane_id.setter
    def _right_pane_id(self, value: str | None) -> None:
        self._primary_slot.pane_id = value

    @property
    def _right_pane_claude(self) -> str | None:
        return self._primary_slot.agent_tmux_name

    @_right_pane_claude.setter
    def _right_pane_claude(self, value: str | None) -> None:
        self._primary_slot.agent_tmux_name = value

    @property
    def _active_session_id(self) -> str | None:
        return self._primary_slot.active_session_id

    @_active_session_id.setter
    def _active_session_id(self, value: str | None) -> None:
        self._primary_slot.active_session_id = value

    @property
    def _in_history_mode(self) -> bool:
        return self._primary_slot.in_history_mode

    @_in_history_mode.setter
    def _in_history_mode(self, value: bool) -> None:
        self._primary_slot.in_history_mode = value

    @property
    def _restore_state(self) -> SlotRestoreState | None:
        return self._primary_slot.restore_state

    @_restore_state.setter
    def _restore_state(self, value: SlotRestoreState | None) -> None:
        self._primary_slot.restore_state = value

    def _modes(self) -> ModeRegistry:
        registry = getattr(self, "_mode_registry", None)
        if registry is None:
            registry = DEFAULT_MODE_REGISTRY
            self._mode_registry = registry
        return registry

    def _active_mode(self) -> AgentMode:
        registry = self._modes()
        key = getattr(self, "_active_mode_key", registry.default_key)
        mode = registry.resolve(key)
        self._active_mode_key = mode.key
        return mode

    @property
    def _codex_mode(self) -> bool:
        """Deprecated bool view retained for compatibility with old callers."""
        return self._active_mode().project_source == ProjectSource.CODEX

    @_codex_mode.setter
    def _codex_mode(self, enabled: bool) -> None:
        self._active_mode_key = (
            CODEX_MODE.key if enabled else self._modes().default_key)

    def __init__(self, claude_home: Path, config: Config,
                 auto_launched: bool = False,
                 scroll_coalescing: bool = True) -> None:
        # Capture before any pane may be split or moved. The server-lifetime
        # digest plus immutable pane id namespaces local recovery state across
        # windows, sessions, and private tmux servers.
        self._restart_identity = restart_state.capture_outer_identity()
        self._claude_home = claude_home
        self._config = config
        self._auto_launched = auto_launched
        # Status-bar state machine. An explicit message (info/warn/error) holds
        # the bar for a level-dependent TTL, then it falls back to cycling idle
        # tips. This is what stops one-shot messages ("→ opened X") from being
        # clobbered by the poll tick before the user can read them. The text is
        # rendered only in the outer tmux status bar (see _render_status_to_tmux);
        # the old in-pane StatusBar widget was removed to reclaim sidebar rows.
        self._status_text: str | None = None
        self._status_level: str = "info"
        self._status_since: float = 0.0
        self._attention_notice_key: tuple[str, int] | None = None
        self._tip_index: int = 0
        self._tip_since: float = 0.0
        # Outer tmux status-bar rendering; run() enables it once tmux is up.
        self._tmux_status_enabled: bool = False
        self._tmux_status_session: str | None = None
        self._tmux_error_bar: bool = False  # whole-bar red while an error shows
        self._selected_project: Project | None = None
        # Project selection belongs to a provider view, not to the whole app.
        # Keep the live field above aligned with the currently visible mode,
        # while this extensible table remembers state for every mode. Paths
        # (rather than Project objects) are re-resolved against the fresh
        # visible list so deleted projects and stale session counts stay safe.
        self._mode_view_states: dict[str, _ModeViewState] = {}
        self._renames = Renames()
        self._session_cache = SessionCache(self._renames)
        self._favorites = Favorites()
        self._settings = Settings()
        self._layout_profile = self._settings.layout_profile
        self._active_sidebar_permille = (
            self._layout_profile.sidebar_permille
            if self._layout_profile is not None else None
        )
        self._active_primary_permille = (
            self._layout_profile.primary_permille
            if self._layout_profile is not None else None
        )
        self._layout_profile_applied = False
        self._layout_profile_fallback = False
        self._layout_geometry_user_owned = False
        self._codex_yolo_runtime = False
        self._codex_yolo_prompt_handled = False
        # Every agent session this Railmux instance has opened, keyed by
        # session_id (or a "__new__-N" placeholder until the JSONL appears).
        self._running: dict[str, _Running] = {}
        # Wall-clock of the last Running-pane re-sort; throttles reordering to
        # once per _RUNNING_SORT_INTERVAL so rows don't jump under the cursor.
        self._running_sort_ts: float = 0.0
        self._new_session_counter: int = 0
        # Per-process random token woven into ``__new__-N`` placeholder names so
        # a restart never reuses a placeholder tmux name from a previous process
        # (the counter alone resets to 0 on restart). Without this, a fresh
        # launch's placeholder could collide with a surviving orphan tmux session
        # of the same name and attach to an unrelated process (#11). 3 bytes → 6
        # hex chars, which survive ``_safe_name``'s 16-char truncation.
        import secrets
        self._proc_token: str = secrets.token_hex(3)
        # Outer tmux display state. The bounded primary/secondary workspace
        # prevents dual-agent support from growing a second set of scalar
        # display fields throughout App.
        self._workspace = AgentWorkspace()
        self._display_transport_manager: AgentDisplayTransport | None = None
        self._loop: urwid.MainLoop | None = None
        identity = self._restart_identity
        self._root_wheel_manager = (
            RootWheelForwardingManager(
                identity.server_digest, identity.pane_id)
            if identity is not None else None
        )
        self._tmux_binding_manager = (
            SharedTmuxBindingManager(
                identity.server_digest, identity.pane_id)
            if identity is not None else None
        )
        self._selection_isolation_manager = (
            SelectionIsolationManager(identity.pane_id)
            if identity is not None else None
        )
        self._projected_target_pane_id: str | None = None
        self._target_toggle_warning_shown = False
        self._teardown_core_done: bool = False
        self._outer_teardown_done: bool = False
        self._exit_in_progress: bool = False
        self._pending_restore_state: dict | None = None
        self._loaded_restart_source: restart_state.OuterTmuxIdentity | None = None
        self._loaded_restart_state_path: Path | None = None
        self._pending_project: Project | None = None
        self._pending_scroll_session: str | None = None
        self._scroll_alarm_pending: bool = False
        self._double_focus_alarm: object | None = None
        self._double_focus_visual_pending: bool = False
        # True while consuming a bracketed-paste burst (between the terminal's
        # "begin paste"/"end paste" markers).  The sidebar panes are command
        # mode — a single stray key like `k`/`d` triggers a destructive action —
        # so pasted text is dropped wholesale rather than replayed as keystrokes.
        # Spans may straddle multiple _filter_input reads, so this is instance
        # state, not a per-call local.
        self._in_paste: bool = False
        self._paste_passthrough: bool = False
        self._last_workspace_size: tuple[int, int] | None = None
        self._last_size_class: str | None = None
        self._last_geometry_poll_at: float = 0.0
        self._railmux_pane_id: str | None = None  # set in run()
        self._railmux_has_focus: bool = True
        self._divider_active: (
            tuple[bool, WorkspaceLayout, str | None] | None
        ) = None
        self._last_border_verify_at: float = 0.0
        self._border_indicators_original_known = False
        self._border_indicators_original = None
        self._border_indicators_arrows = False
        self._has_less: bool = shutil.which("less") is not None
        self._less_mouse_flag: str = self._detect_less_mouse()
        self._scroll_manager = ScrollManager(enabled=scroll_coalescing)
        self._soft_quit_flag: bool = False
        # Ordered provider registry + stable active key. No two-mode boolean:
        # ``m`` cycles the registry and each key owns independent view state.
        self._mode_registry: ModeRegistry = DEFAULT_MODE_REGISTRY
        self._active_mode_key: str = self._mode_registry.default_key
        self._codex_index = BackgroundCodexIndex(
            self._codex_home_path(), self._renames)
        # Start the first immutable generation while the widget tree is built.
        # Startup recovery pins whichever generation is available; it never
        # observes generation 0 for early candidates and generation 1 for later
        # candidates in the same pass.
        self._codex_index.refresh(force=True)
        self._codex_recovery_pending: bool = False
        self._codex_recovery_state: dict | None = None
        self._codex_recovery_generation: int = 0
        self._codex_recovery_candidates_seen: bool = False
        self._codex_provisional_recovery_keys: set[str] = set()
        self._last_orphan_probe_ok: bool = True
        self._codex_project_filter: dict[Path, int] = {}  # cwd → Codex session count
        # Mode switches paint from existing snapshots immediately. A daemon
        # worker refreshes both NFS-backed indexes; _refresh consumes the result
        # on the UI thread so no urwid widget is touched from the worker.
        self._mode_refresh_lock = threading.Lock()
        self._mode_refresh_thread: threading.Thread | None = None
        self._mode_refresh_result: (
            tuple[list[Project] | None, str | None] | None
        ) = None

        projects = list_projects(claude_home)
        self._project_snapshot = projects
        self._project_snapshot_at = time.monotonic()
        initial_mode = self._active_mode()
        self._projects_pane = ProjectsPane(
            projects,
            on_select=self._on_project_select,
            on_double_click=self._on_project_double_click,
            provider_label=initial_mode.label,
            boxed=False,
        )
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            on_preview=self._on_session_row_preview,
            on_context=self._open_session_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
            provider_label=initial_mode.label,
            boxed=False,
        )
        self._running_pane = RunningSessionsPane(
            on_select=self._on_running_select,
            on_context=self._on_running_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
            provider_label=initial_mode.label,
            boxed=False,
        )
        # Warn early if dependencies are missing so the user doesn't
        # discover it by getting a cryptic error in the right pane.
        if not tmux_ctl.has_tmux():
            self._set_status(
                "ERROR: tmux not found on PATH — railmux cannot run without tmux")

        # Three horizontal title rules replace the stacked boxes inside one
        # shared pair of vertical rails. The focused section owns a closed green
        # outline. Sessions receives half the available height because each
        # session consumes two display rows; stable weight rounding prevents a
        # one-row jump when keyboard focus moves between sections.
        self._sidebar = StableWeightedPile([
            ("weight", 2, SidebarSection(
                self._projects_pane,
                lambda: self._projects_pane.section_title,
            )),
            ("weight", 4, SidebarSection(
                self._sessions_pane,
                lambda: self._sessions_pane.section_title,
            )),
            ("weight", 2, SidebarSection(
                self._running_pane,
                lambda: self._running_pane.section_title,
            )),
        ])
        sidebar_frame = UnifiedSidebarFrame(
            self._sidebar,
            (self._projects_pane, self._sessions_pane, self._running_pane),
        )
        self._sidebar_body = urwid.Padding(sidebar_frame, right=1)
        self._hint_bar = HintBar()
        # Start on the focused pane's key set (sidebar defaults to Projects) so
        # the bar is correct before the first refresh tick.
        self._hint_bar.set_context(self._help_context())
        self._button_bar = ButtonBar(
            on_help=self._open_help_modal,
            on_quit=self._open_quit_confirm,
            on_detach=self._on_detach,
            on_mode_toggle=self._cycle_mode,
            on_layout=self._rotate_split,
            on_options=self._open_options_modal,
            on_expanded_change=self._on_button_bar_expanded,
        )
        # Footer contains only stable controls. Status, warnings, and errors all
        # use the full-width outer tmux bar, Railmux's single status surface.
        self._footer = urwid.Pile([
            ("pack", self._hint_bar),
            ("pack", self._button_bar),
        ])
        self._frame = _FocusAwareFrame(
            body=self._sidebar_body, footer=self._footer)
        # Backstop for direct ``--inside-tmux`` starts. The normal CLI also
        # audits the explicitly targeted dedicated server before
        # ``new-session -A`` so a stale outer session cannot prevent a new App
        # process from launching.
        if tmux_ctl.in_tmux():
            recover_interrupted_swaps()
        state = self._load_state()
        # Recover sessions left alive from a previous soft-quit.  Load the
        # state first: a resolved session may intentionally retain its
        # ``cx-new---*`` tmux name, so the tmux name alone is not enough to
        # reconstruct the real session id on platforms without procfs.
        recovery_ok, recovery_generation = self._discover_orphans_consistent(state)
        self._discover_legacy_running(force=True)
        if recovery_generation == 0 and self._codex_recovery_candidates_seen:
            self._codex_recovery_pending = True
            self._codex_recovery_state = state
            self._codex_recovery_generation = 0
            # Retain exact instance state until one coherent Codex generation
            # either completes recovery or proves a candidate invalid.
            self._running_recovery_ok = False
        else:
            self._running_recovery_ok = recovery_ok
        # Restore the view from a previous soft-quit, or auto-select the
        # most recent project as usual.
        # Restore the mode BEFORE choosing a project so selection resolves
        # against the correct provider source. ``codex_mode`` remains a read-only
        # migration path for state files written by Railmux <= 0.1.1.
        restored_mode = self._mode_key_from_state(state)
        if restored_mode != self._modes().default_key:
            self._enter_mode_on_restore(restored_mode)
        self._warn_missing_mode_binary(self._active_mode())
        if state:
            self._projects_pane.set_filter(state.get("project_filter", ""))
            self._sessions_pane.set_filter(state.get("session_filter", ""))
            running_filter = state.get("running_filter", "")
            if isinstance(running_filter, str):
                self._current_mode_view_state().running_filter = running_filter
                self._running_pane.set_filter(
                    running_filter, capture_focus=False)
        # Apply the mode-specific project view immediately. In Claude mode this
        # also enforces the configured empty-project policy on the first frame.
        visible = self._visible_projects()
        self._projects_pane.set_projects(visible)
        # Session to re-focus in the sidebar once its rows are loaded (below,
        # from _load_pending_project). Backward-compatible default.
        self._pending_focus_session = state.get("session") if state else None
        initial_project: Project | None = None
        if state:
            proj_name = state.get("project")
            if proj_name:
                initial_project = next(
                    (p for p in visible if p.encoded_name == proj_name), None)
        if initial_project is None and visible:
            initial_project = visible[0]
        if initial_project is not None:
            self._set_current_project(initial_project)
            self._pending_project = initial_project
        # Paint discovered orphans without parsing their JSONLs. Full labels
        # and statuses are refined after MainLoop renders the first frame.
        self._render_running_pane()
        # Re-open the right pane after MainLoop paints the sidebar's first frame.
        self._pending_restore_state = state

    def _set_slot_active_target(
        self,
        slot: AgentSlot,
        session_id: str | None,
        tmux_name: str | None,
        *,
        mode_key: str | None = None,
        project_key: str | None = None,
    ) -> None:
        """Update one slot, painting sidebar highlights only for the Target."""
        slot.active_session_id = session_id
        if mode_key is not None:
            try:
                slot.mode_key = self._modes().get(mode_key).key
            except KeyError:
                slot.mode_key = None
        elif tmux_name:
            mode = self._modes().for_tmux_name(tmux_name)
            slot.mode_key = mode.key if mode is not None else None
        elif session_id is None:
            slot.mode_key = None

        if project_key is not None:
            slot.project_key = project_key
        elif tmux_name:
            running = self._by_tmux(tmux_name)
            slot.project_key = (
                running.project.encoded_name
                if running is not None and running.project is not None
                else None
            )
        elif session_id is None:
            slot.project_key = None
        self._paint_slot_active_target(slot, session_id, tmux_name)

    def _paint_slot_active_target(
        self,
        slot: AgentSlot,
        session_id: str | None,
        tmux_name: str | None,
    ) -> None:
        """Paint one slot's target without committing its workspace state.

        Attach operations are synchronous, while tmux can expose a pane swap
        partway through the transaction. Keeping this display-only operation
        separate lets a click acknowledge its intended target immediately and
        still leaves the prior :class:`AgentSlot` state available for rollback.
        """
        if slot.key == self._agent_workspace().target_slot_key:
            self._sessions_pane.set_active_session(session_id)
            self._running_pane.set_active(tmux_name)

    def _session_id_for_tmux_target(self, tmux_name: str) -> str | None:
        running = self._by_tmux(tmux_name)
        if running is None or running.is_placeholder:
            return None
        return getattr(running, "logical_session_id", running.key)

    def _paint_slot_active_tmux_target(
        self, slot: AgentSlot, tmux_name: str,
    ) -> None:
        """Optimistically paint a tmux target without changing *slot*."""
        self._paint_slot_active_target(
            slot,
            self._session_id_for_tmux_target(tmux_name),
            tmux_name,
        )

    def _set_active_target(self, session_id: str | None,
                           tmux_name: str | None, *,
                           mode_key: str | None = None,
                           project_key: str | None = None) -> None:
        """Compatibility entry point for the currently exposed primary slot."""
        self._set_slot_active_target(
            self._primary_slot,
            session_id,
            tmux_name,
            mode_key=mode_key,
            project_key=project_key,
        )

    def _set_active_tmux_target(
        self, tmux_name: str, slot: AgentSlot | None = None,
    ) -> None:
        slot = slot or self._primary_slot
        self._set_slot_active_target(
            slot,
            self._session_id_for_tmux_target(tmux_name),
            tmux_name,
        )

    def _sync_border_indicators(self, arrows: bool) -> bool:
        """Show exact focus direction on an ambiguous shared vertical rail.

        tmux cannot colour only the left edge of an active pane.  In a
        three-pane side-by-side window, inward arrows identify which side of
        both green borders owns focus.  The option arrived in tmux 3.3; older
        versions retain the existing colour-only treatment.
        """
        if tmux_ctl.tmux_version() < (3, 3):
            return True
        current = getattr(self, "_border_indicators_arrows", False)
        if current == arrows:
            return True
        known = getattr(
            self, "_border_indicators_original_known", False)
        if arrows and not known:
            ok, original = tmux_ctl.local_window_option(
                "pane-border-indicators")
            if not ok:
                return False
            self._border_indicators_original = original
            self._border_indicators_original_known = True
        if arrows:
            applied = tmux_ctl.set_window_option(
                "pane-border-indicators", "arrows")
        else:
            # Keep arrows out of sidebar/single/stacked states even if the
            # user's inherited option happens to request them.  The exact
            # original local/inherited setting is restored on teardown.
            applied = tmux_ctl.set_window_option(
                "pane-border-indicators", "colour",
            )
        if applied:
            self._border_indicators_arrows = arrows
        return applied

    def _restore_border_indicators(self) -> bool:
        """Best-effort restoration for soft quit and interrupted layouts."""
        if not getattr(self, "_border_indicators_original_known", False):
            return True
        restored = tmux_ctl.set_window_option(
            "pane-border-indicators",
            getattr(self, "_border_indicators_original", None),
        )
        if restored:
            self._border_indicators_arrows = False
            self._border_indicators_original_known = False
            self._border_indicators_original = None
        return restored

    def _set_divider_active(self, active: bool, *, force: bool = False) -> None:
        """Highlight the outer border owned by the focused workspace pane.

        Agent focus uses tmux's bright active-border colour. Sidebar focus
        keeps every tmux border gray: green always means actual keyboard focus,
        while a dual workspace names its remembered Target pane in the status
        brand.
        """
        workspace = self._agent_workspace()
        layout = workspace.layout
        target_pane = None if active else workspace.target.pane_id
        state = (active, layout, target_pane)
        if not force and self._divider_active == state:
            return
        gray = "fg=colour240"
        green = f"fg={_GRASS_GREEN}"
        if not active:
            # Green means real keyboard focus. The old single-pane Target
            # treatment used a per-pane dim-green format here; tmux owns shared
            # border cells in segments, so after restart that format could
            # colour only half of the center divider. A single workspace has no
            # ambiguous target, and a dual workspace names P1/P2 in the status
            # brand, so every sidebar-focused border can stay honestly gray.
            applied = tmux_ctl.set_window_border_styles(gray, gray)
        elif layout is WorkspaceLayout.SINGLE:
            applied = tmux_ctl.set_window_border_styles(green, green)
        else:
            applied = tmux_ctl.set_window_border_styles(
                gray, green if active else gray)
        arrows = active and layout is WorkspaceLayout.SIDE_BY_SIDE
        indicators_applied = self._sync_border_indicators(arrows)
        # A failed tmux update must remain retryable. Caching an unapplied dual
        # style is what can leave half of the single-pane divider green after
        # Pane 2 is closed.
        self._divider_active = state if applied and indicators_applied else None

    def _retry_pending_divider_style(self) -> None:
        """Heal failed writes and externally drifted tmux border options."""
        if not hasattr(self, "_divider_active"):
            return
        workspace = self._agent_workspace()
        active = not getattr(self, "_railmux_has_focus", True)
        desired_state = (
            active,
            workspace.layout,
            None if active else workspace.target.pane_id,
        )
        state = getattr(self, "_divider_active", None)
        if state != desired_state:
            # The focus reconciliation immediately before this method treats
            # tmux's active pane as authoritative. Repair the style cache too:
            # a reconnect can change pane focus and window options in separate
            # frames, leaving either one stale.
            self._set_divider_active(active, force=True)
            # A successful write just established the desired styles; a failed
            # one leaves ``None`` and will retry on the next refresh.
            return

        # tmux options can be restored or overwritten independently of the
        # active-pane/Target state (for example while clients detach and
        # reattach). The in-memory cache alone cannot detect that drift. Check
        # the effective pair at a low cadence and repaint only on mismatch.
        now = time.monotonic()
        if now - getattr(self, "_last_border_verify_at", 0.0) < 2.0:
            return
        self._last_border_verify_at = now
        active, layout, _target_pane = state
        gray = "fg=colour240"
        green = f"fg={_GRASS_GREEN}"
        if not active:
            expected = (gray, gray)
        elif layout is WorkspaceLayout.SINGLE:
            expected = (green, green)
        else:
            expected = (gray, green)
        ok, actual = tmux_ctl.window_border_styles()
        if ok and actual != expected:
            self._set_divider_active(active, force=True)

    def _set_railmux_focus(self, active: bool, *, force_border: bool = False) -> None:
        """Synchronize urwid focus maps and the tmux center divider."""
        self._railmux_has_focus = active
        self._frame.set_window_active(active)
        self._set_divider_active(not active, force=force_border)
        if hasattr(self, "_hint_bar"):
            self._hint_bar.set_context(self._help_context())
        # The status brand carries a compact layout/Target-pane cue. It remains
        # visible on either side of the focus transition because layout and
        # Target pane are state, not focus decoration.
        self._apply_tmux_bar(self._tmux_error_bar)

    def _sync_target_slot_from_tmux(self, *, previous: bool = False) -> AgentSlot:
        """Resolve actual tmux pane focus to the workspace Target.

        ``previous`` is used when the sidebar has just received focus: tmux's
        active pane is already Railmux, while ``pane_last`` still identifies
        the agent the user came from.
        """
        workspace = self._agent_workspace()
        target = getattr(self, "_railmux_pane_id", None)
        if target is None:
            target = workspace.primary.pane_id or workspace.secondary.pane_id
        if target is None:
            return workspace.target
        pane_id = (
            tmux_ctl.last_pane_id(target)
            if previous else tmux_ctl.active_pane_id(target)
        )
        slot = workspace.slot_for_pane(pane_id) if pane_id else None
        if slot is None:
            return workspace.target
        if slot.key != workspace.target_slot_key:
            self._set_workspace_target(slot.key)
            self._paint_slot_active_target(
                slot, slot.active_session_id, slot.agent_tmux_name)
            # While focus stays somewhere in the agent region, tmux does not
            # send another focus event to the already-unfocused sidebar when a
            # mouse click moves directly between P1 and P2. The refresh loop
            # still resolves the new active pane through this method; repaint
            # the compact layout glyph at the same state transition rather than
            # waiting for focus to return to Railmux. This runs only when the
            # Target pane actually changes, so ordinary polling never jitters
            # the status line or an agent's input preedit.
            self._apply_tmux_bar(self._tmux_error_bar)
            if (hasattr(self, "_hint_bar")
                    and not self._railmux_has_focus):
                self._hint_bar.set_context(self._help_context())
            if not self._railmux_has_focus and not previous:
                pane_number = (
                    2 if slot.key == AgentWorkspace.SECONDARY else 1)
                self._set_status(f"Agent Pane {pane_number} focused")
        return slot

    def _reconcile_focus_from_tmux(self) -> bool:
        """Converge delayed terminal focus reports on tmux's active pane.

        Some terminal hosts can deliver ``focus in`` after a programmatic
        select-pane has already moved input to an agent.  Treating that report
        as final leaves Railmux's model on the sidebar and every agent border
        gray.  The tmux window is the routing authority, so focus events and
        the periodic refresh both use this bounded reconciliation.
        """
        owner = getattr(self, "_railmux_pane_id", None)
        if owner is None:
            return False
        self._sync_compact_page_from_tmux()
        active_pane = tmux_ctl.active_pane_id(owner)
        if active_pane is None:
            return False
        if active_pane == owner:
            # Double-click paints agent focus just before its delayed
            # select-pane. Do not undo that intentional transitional frame.
            if getattr(self, "_double_focus_visual_pending", False):
                return True
            if not getattr(self, "_railmux_has_focus", True):
                self._sync_target_slot_from_tmux(previous=True)
                self._set_railmux_focus(True, force_border=True)
            return True

        slot = self._agent_workspace().slot_for_pane(active_pane)
        if slot is None:
            return False
        self._sync_target_slot_from_tmux()
        if getattr(self, "_railmux_has_focus", True):
            self._set_railmux_focus(False, force_border=True)
        return True

    def _schedule_right_pane_focus_after_double(self) -> None:
        """Show the right-focus state now, then move tmux focus once settled."""
        self._cancel_pending_double_focus(restore_visual=False)
        self._double_focus_visual_pending = True
        self._set_railmux_focus(False)
        self._redraw_focus_state_now()
        if self._loop is None:
            self._apply_right_pane_focus_after_double(None, None)
            return
        self._double_focus_alarm = self._loop.set_alarm_in(
            self._DOUBLE_CLICK_FOCUS_DELAY,
            self._apply_right_pane_focus_after_double,
        )

    def _apply_right_pane_focus_after_double(self, _loop, _user_data) -> None:
        self._double_focus_alarm = None
        pane_id = self._agent_workspace().target.pane_id
        if pane_id is None or not tmux_ctl.select_pane(pane_id):
            self._double_focus_visual_pending = False
            self._set_railmux_focus(True)
            self._redraw_focus_state_now()
            return
        self._double_focus_visual_pending = False
        self._set_railmux_focus(False)
        self._redraw_focus_state_now()

    def _cancel_pending_double_focus(self, *, restore_visual: bool = True) -> None:
        alarm = self._double_focus_alarm
        if alarm is not None and self._loop is not None:
            self._loop.remove_alarm(alarm)
        self._double_focus_alarm = None
        visual_pending = self._double_focus_visual_pending
        self._double_focus_visual_pending = False
        if visual_pending and restore_visual:
            self._set_railmux_focus(True)
            self._redraw_focus_state_now()

    def _redraw_focus_state_now(self) -> None:
        """Flush a focus-only transition instead of waiting for the next tick."""
        if self._loop is not None:
            self._loop.draw_screen()

    def _paste_target_is_text_input(self) -> bool:
        """True when the focused widget is a text field that should receive
        pasted text (rename, path browser, or the filter Edit).

        Everything else — the sidebar panes and the confirm/menu modals — is
        command mode, where pasted characters are dispatched as destructive
        keybindings and must be dropped.  Note this deliberately excludes the
        delete/quit confirmations: pasting ``y`` into them is the exact hazard.
        """
        loop = self._loop
        if loop is not None and loop.widget is not self._frame:
            # A modal overlay is up; only the text-entry modals accept paste.
            top = getattr(loop.widget, "top_w", None)
            return isinstance(top, (RenameModal, PathBrowserModal))
        # No modal — the filter Edit lives in the footer and only gets focus
        # while filtering.
        try:
            return self._frame.focus_position == "footer"
        except Exception:
            return False

    def _looks_like_paste_burst(self, keys: list) -> bool:
        """Heuristic paste detector for terminals lacking bracketed paste.

        A human presses one command key per read; a paste dumps many character
        bytes at once.  Count only single-character keys so multi-key names
        (``up``, ``enter``, ``tab``) and mouse tuples from a single action don't
        trip it.
        """
        singles = sum(1 for k in keys if isinstance(k, str) and len(k) == 1)
        return singles >= self._PASTE_BURST_MIN

    def _filter_input(self, keys: list, _raw: list[int]) -> list:
        """Consume terminal focus reports and drop pasted input before normal
        key dispatch.

        Pasting into the sidebar is dangerous: each pasted character is dispatched
        as a command key, so a clipboard containing ``k`` (kill), ``d``+``y``
        (delete-confirm) or ``q``+Enter (quit-all) can destroy sessions.  Two
        layers guard against it:

        * **Bracketed paste** (primary, precise): with the mode enabled the
          terminal frames the paste in ``begin paste``/``end paste`` markers, so
          we can drop the whole span exactly.  The span can straddle multiple
          reads, hence ``self._in_paste`` persists across calls.
        * **Burst heuristic** (fallback): terminals without bracketed paste send
          the paste as raw characters; a dense burst in one read is dropped too.

        Text fields (rename / filter / path browser) are exempt — there a paste
        is wanted, so the markers are stripped and the content passed through.
        """
        # Fallback layer: no bracketed markers, not mid-span, but a burst arrived
        # in a command-mode context.  Drop it (focus reports don't ride along
        # with pasted text, so returning [] loses nothing).
        if (not self._in_paste
                and "begin paste" not in keys
                and self._looks_like_paste_burst(keys)
                and not self._paste_target_is_text_input()):
            self._set_status(
                "Paste ignored in sidebar — switch to the agent pane "
                "(Ctrl-B →) to paste.",
                "warn",
            )
            return []

        filtered = []
        for key in keys:
            if self._in_paste:
                # Safety reset: if a modal overlay appeared while we were
                # mid-paste (e.g. focus-out closed the paste span), don't
                # stay stuck swallowing keys forever.
                if self._loop is not None and self._loop.widget is not self._frame:
                    self._in_paste = False
                # Inside a bracketed-paste span.  The closing marker ends the
                # span and is never forwarded.
                if key == "end paste":
                    self._in_paste = False
                elif self._paste_passthrough:
                    # Only forward printable single characters.  Control keys
                    # (enter, esc, tab, …) must be dropped — a newline in the
                    # pasted text would otherwise exit the text field and
                    # dispatch the remaining characters as sidebar commands
                    # (potentially destructive ones like d, k, q).
                    if isinstance(key, str) and len(key) == 1 and key.isprintable():
                        filtered.append(key)
                continue
            if key == "begin paste":
                self._in_paste = True
                self._paste_passthrough = self._paste_target_is_text_input()
                if not self._paste_passthrough:
                    self._set_status(
                        "Paste ignored in sidebar — switch to the agent pane "
                        "(Ctrl-B →) to paste.",
                        "warn",
                    )
            elif key == "end paste":
                # Stray close with no matching open — nothing to do.
                pass
            elif key == "focus in":
                # Cursor/terminal focus reports can arrive after a tmux pane
                # transition. Reconcile against the active pane instead of
                # allowing a stale report to gray every agent border.
                if not self._reconcile_focus_from_tmux():
                    if not self._double_focus_visual_pending:
                        self._sync_target_slot_from_tmux(previous=True)
                        self._set_railmux_focus(True)
            elif key == "focus out":
                if not self._reconcile_focus_from_tmux():
                    self._sync_target_slot_from_tmux()
                    self._set_railmux_focus(False)
                # Belt-and-suspenders: if the paste-end marker was lost the
                # sidebar would swallow every key forever.  Focus leaving the
                # pane is a natural reset point — the paste is over.
                self._in_paste = False
            elif key == "window resize":
                # Urwid's TTY is only the sidebar pane after the agent split.
                # Re-read the containing tmux window on the real resize event;
                # do not launch a tmux query from every periodic refresh tick.
                self._check_terminal_size()
                filtered.append(key)
            else:
                filtered.append(key)
        return filtered

    def _load_pending_project(self, _loop, _user_data) -> None:
        """Load initial session metadata after the sidebar's first frame."""
        project = self._pending_project
        self._pending_project = None
        if (project is not None
                and self._selected_project is not None
                and project.encoded_name == self._selected_project.encoded_name):
            self._on_project_select(project)
            # Restore the sidebar cursor to the previously-focused session now
            # that its row exists. No-op if the session is gone.
            focus_session = self._pending_focus_session
            self._pending_focus_session = None
            if focus_session is not None:
                self._sessions_pane._restore_focus(focus_session)

    def _restore_pending_right_pane(self, _loop, _user_data) -> None:
        """Restore persisted state, retaining its file if restoration raises."""
        state = self._pending_restore_state
        right_mode = state.get("right_mode") if state is not None else None
        workspace_slots: tuple[dict, ...] = ()
        if state is not None:
            workspace = state.get("workspace")
            raw_slots = workspace.get("slots") if isinstance(workspace, dict) else None
            if isinstance(raw_slots, dict):
                workspace_slots = tuple(
                    item for item in raw_slots.values()
                    if isinstance(item, dict)
                )

        def workspace_codex_metadata_pending() -> bool:
            for item in workspace_slots:
                if item.get("mode") != CODEX_MODE.key:
                    continue
                if item.get("kind") == "preview":
                    return True
                if item.get("kind") != "agent":
                    continue
                tmux_name = item.get("tmux")
                session_id = item.get("session")
                represented = (
                    isinstance(tmux_name, str)
                    and any(running.tmux_name == tmux_name
                            for running in self._running.values())
                ) or (
                    isinstance(session_id, str)
                    and session_id in self._running
                )
                if not represented:
                    return True
            return False

        codex_snapshot_ready = True
        index = getattr(self, "_codex_index", None)
        if isinstance(index, BackgroundCodexIndex):
            codex_snapshot_ready = index.current_snapshot().generation > 0
        if getattr(self, "_codex_recovery_pending", False) and state is not None:
            if workspace_codex_metadata_pending():
                return
            # Exact live targets adopted from stamps are safe to display even
            # while the first filesystem generation is still pending. A
            # metadata-dependent target waits and is retried after settlement.
            if state.get("right_kind") == "agent":
                target = state.get("right_tmux")
                session_id = state.get("right_session")
                represented = (
                    isinstance(target, str)
                    and any(item.tmux_name == target
                            for item in self._running.values())
                ) or (
                    isinstance(session_id, str)
                    and session_id in self._running
                )
                if not represented:
                    return
            elif state.get("right_kind") == "preview":
                if (right_mode == CODEX_MODE.key
                        or (right_mode is None
                            and self._active_mode().key == CODEX_MODE.key)):
                    return
        if state is not None and not codex_snapshot_ready:
            if workspace_codex_metadata_pending():
                return
            kind = state.get("right_kind")
            uses_codex_metadata = (
                kind == "preview"
                and (right_mode == CODEX_MODE.key
                     or (right_mode is None
                         and self._active_mode().key == CODEX_MODE.key))
            ) or (
                kind == "agent"
                and right_mode == CODEX_MODE.key
                and isinstance(state.get("right_session"), str)
                and state["right_session"] not in self._running
            )
            if uses_codex_metadata:
                return
        self._pending_restore_state = None
        if state is None:
            return
        restored = self._restore_right_pane(state)
        if not restored or not getattr(self, "_running_recovery_ok", True):
            return
        path = getattr(self, "_loaded_restart_state_path", None)
        source = getattr(self, "_loaded_restart_source", None)
        identity = getattr(self, "_restart_identity", None)
        try:
            if path is not None:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        if identity is not None and source is not None:
            restart_state.clear_managed_handoff(identity, source)
        self._loaded_restart_state_path = None
        self._loaded_restart_source = None

    def _schedule_scroll_acceleration(self, claude_tmux_name: str) -> None:
        """Configure scrolling after the pane switch has had a chance to draw."""
        if self._loop is None:
            self._configure_scroll_acceleration(claude_tmux_name)
            return
        self._pending_scroll_session = claude_tmux_name
        if self._scroll_alarm_pending:
            return
        self._scroll_alarm_pending = True
        self._loop.set_alarm_in(0.05, self._apply_pending_scroll_acceleration)

    def _apply_pending_scroll_acceleration(self, _loop, _user_data) -> None:
        self._scroll_alarm_pending = False
        claude_tmux_name = self._pending_scroll_session
        self._pending_scroll_session = None
        if (claude_tmux_name is not None
                and self._agent_workspace().target.agent_tmux_name
                == claude_tmux_name):
            self._configure_scroll_acceleration(claude_tmux_name)

    # --- project / session selection callbacks ---

    @staticmethod
    def _selection_path(path: Path) -> Path:
        """Return a comparison-safe project path without requiring it to exist."""
        try:
            return path.resolve()
        except OSError:
            return path

    def _remember_project_selection(self, project: Project) -> None:
        self._current_mode_view_state().selected_project_path = (
            self._selection_path(project.real_path))

    def _remembered_project_path(self) -> Path | None:
        return self._current_mode_view_state().selected_project_path

    def _current_mode_key(self) -> str:
        """Stable key for state owned by the currently visible agent mode."""
        return self._active_mode().key

    def _current_mode_view_state(self) -> _ModeViewState:
        # Lazily initialise for App.__new__ unit fixtures and for future modes.
        states = getattr(self, "_mode_view_states", None)
        if states is None:
            states = {}
            self._mode_view_states = states
        return states.setdefault(self._current_mode_key(), _ModeViewState())

    def _set_current_project(self, project: Project | None,
                             *, remember: bool = True) -> None:
        """Synchronise the live project and its Projects-pane highlight."""
        self._selected_project = project
        self._projects_pane.set_selected(
            project.encoded_name if project is not None else None)
        if project is not None and remember:
            self._remember_project_selection(project)

    def _visible_project_for_path(self, projects: list[Project],
                                  path: Path | None) -> Project | None:
        if path is None:
            return None
        target = self._selection_path(path)
        return next(
            (project for project in projects
             if self._selection_path(project.real_path) == target),
            None,
        )

    def _preferred_project(self, projects: list[Project],
                           fallback: Project | None = None) -> Project | None:
        """Choose the current mode's remembered project, safely and visibly."""
        if not projects:
            return None
        remembered = self._visible_project_for_path(
            projects, self._remembered_project_path())
        if remembered is not None:
            return remembered
        if fallback is not None:
            mapped = self._visible_project_for_path(projects, fallback.real_path)
            if mapped is not None:
                return mapped
        return projects[0]

    def _clear_current_project(self) -> None:
        """Clear only the visible mode; keep its remembered path for later."""
        self._set_current_project(None, remember=False)
        self._sessions_pane.set_sessions(
            None, [], running_ids=self._running_session_ids(),
            favorite_ids=self._favorites.get_ids())

    def _on_project_select(self, project: Project | None) -> None:
        """Single-click / initial auto-select: show sessions, keep focus here."""
        self._cancel_pending_double_focus()
        self._pending_project = None
        if project is None:
            self._open_new_project_modal()
            return
        self._set_current_project(project)
        sessions = self._pane_sessions(
            project, refresh=not self._mode_refresh_pending())
        self._sessions_pane.set_sessions(project, sessions, running_ids=self._running_session_ids(),
                favorite_ids=self._favorites.get_ids())
        self._set_status(f"Project: {project.real_path}  ({len(sessions)} sessions)")

    def _on_project_double_click(self, project: Project | None) -> None:
        """Double-click / Enter on a project: show sessions AND move focus to them."""
        if project is None:
            self._open_new_project_modal()
            return
        self._on_project_select(project)
        if self._loop is not None:
            self._sidebar.focus_position = 1
            self._hint_bar.set_context(self._help_context())

    def _on_session_select(self, session: SessionMeta | None,
                            steal_focus: bool = True,
                            from_double: bool = False) -> None:
        if not from_double:
            self._cancel_pending_double_focus()
        # Opening a real session (or creating a new one) — clear any
        # history-preview state so the launch takes over the right pane.
        slot = self._agent_workspace().target
        slot.in_history_mode = False
        slot.restore_state = None
        if session is None:
            if slot is self._primary_slot:
                self._launch_new_session()
            else:
                self._launch_new_session(slot=slot)
            return
        if slot is self._primary_slot:
            self._launch_resume(
                session,
                steal_focus=steal_focus,
                from_double=from_double,
            )
        else:
            self._launch_resume(
                session,
                steal_focus=steal_focus,
                from_double=from_double,
                slot=slot,
            )

    def _on_running_select(self, entry: RunningEntry,
                            steal_focus: bool = True,
                            from_double: bool = False,
                            slot: AgentSlot | None = None) -> bool:
        if slot is None:
            slot = (self._agent_workspace().slot_for_agent(entry.tmux_name)
                    or self._agent_workspace().target)
        self._set_workspace_target(slot.key)
        if not from_double:
            self._cancel_pending_double_focus()
        selected = self._by_tmux(entry.tmux_name)
        if (selected is not None and selected.orphan is not None
                and not self._running_action_valid(
                    selected, entry.identity_token)):
            msg = "Open refused: the unresolved tmux identity changed"
            self._set_status(msg, "error")
            return False
        # Re-attach the agent pane to this already-running session AND
        # sync the Projects/Sessions panes to that session's project, so the
        # sidebar reflects what's actually showing on the right.
        slot.in_history_mode = False
        slot.restore_state = None
        if slot is self._primary_slot:
            # Preserve the long-standing primary compatibility entry point;
            # integrations may wrap it while secondary goes through the new
            # explicit-slot path.
            ok = self._attach_in_right_pane(
                entry.tmux_name, steal_focus=steal_focus)
        else:
            ok = self._attach_agent_slot(
                slot, entry.tmux_name, steal_focus=steal_focus)
        if not ok:
            msg = "Re-attach failed: could not connect to agent pane"
            self._set_status(msg, "error")
            return False
        r = self._by_tmux(entry.tmux_name)
        project = r.project if r else None
        if project is not None:
            # Resolve to the project instance shown in the current mode's
            # Projects pane. A running session's project may carry an
            # encoded_name from a different source (the Codex index) than the
            # sidebar row (Claude discovery / a synthesised Codex entry), so
            # selecting by its own encoded_name would miss — and thus clear —
            # the visible Projects highlight.
            project = self._project_in_current_view(project)
            if (self._selected_project is None
                    or self._selected_project.encoded_name != project.encoded_name):
                self._set_current_project(project)
                # Pull sessions from the correct source for the mode. In Codex
                # mode the Claude cache is empty, so using it would clear the
                # Sessions highlight (the running session isn't in that list);
                # a synthetic Codex project (empty claude_dir) must also never
                # reach the Claude cache (#9). ``_pane_sessions`` handles both.
                sessions = self._pane_sessions(
                    project, refresh=not self._mode_refresh_pending())
                self._sessions_pane.set_sessions(
                    project,
                    sessions,
                    running_ids=self._running_session_ids(),
                    favorite_ids=self._favorites.get_ids(),
                )
        if not self._show_attention_status(entry.attention):
            self._set_status(f"→ {entry.label}")
        return True

    # --- history preview (display pane shows a transcript, not an agent session) ---

    def _on_session_row_preview(self, session: SessionMeta) -> None:
        """Apply click semantics after rechecking whether the row is live."""
        running = self._by_session_id(session.session_id)
        if (running is not None
                and self._agent_session_alive(running.tmux_name)):
            self._on_running_select(
                RunningEntry(
                    tmux_name=running.tmux_name,
                    label=running.label,
                    status=running.status,
                ),
                steal_focus=False,
            )
            return
        self._on_session_preview(session)

    def _on_session_preview(self, session: SessionMeta) -> None:
        """Show session history in the right pane without launching Claude.

        Stopped-row clicks, Space, and the context action preview history. Live
        rows are filtered by _on_session_row_preview before reaching this path.
        """
        slot = self._agent_workspace().target
        self._cancel_pending_double_focus()
        if not self._has_less:
            self._set_status("'less' not installed — cannot preview history")
            return
        if not slot.in_history_mode:
            if slot is self._primary_slot:
                self._save_restore_state()
            else:
                self._save_restore_state(slot)
        shown = (
            self._show_transcript(
                session.jsonl_path, session_type=session.session_type)
            if slot is self._primary_slot
            else self._show_transcript(
                session.jsonl_path,
                session_type=session.session_type,
                slot=slot,
            )
        )
        if shown:
            slot.in_history_mode = True
            target_kwargs = {
                "mode_key": self._current_mode_key(),
                "project_key": session.project.encoded_name,
            }
            if slot is self._primary_slot:
                self._set_active_target(
                    session.session_id, None, **target_kwargs)
            else:
                self._set_slot_active_target(
                    slot, session.session_id, None, **target_kwargs)
            if not self._show_attention_status(session.attention):
                self._set_status(
                    f"≡ Previewing {session.display_title} (history)")

    def _save_restore_state(self, slot: AgentSlot | None = None) -> None:
        """Remember what's in the right pane before taking it over for history."""
        slot = slot or self._agent_workspace().target
        if slot.pane_id and tmux_ctl.pane_alive(slot.pane_id):
            if (slot.agent_tmux_name
                    and self._agent_session_alive(slot.agent_tmux_name)):
                slot.restore_state = SlotRestoreState(
                    "agent", tmux_name=slot.agent_tmux_name)
                return
        slot.restore_state = SlotRestoreState("empty")

    @staticmethod
    def _detect_less_mouse() -> str:
        """Return ``"--mouse --wheel-lines=3"`` if less supports it, else ``""``."""
        import subprocess as _sp
        try:
            out = _sp.check_output(
                ["less", "--version"], stderr=_sp.STDOUT, text=True, timeout=3)
            # "less 668 (GNU regular expressions)" → major version
            ver = int(out.strip().split()[1].split(".")[0])
            if ver >= 590:
                return "--mouse --wheel-lines=3"
        except Exception:
            pass
        return ""

    def _show_transcript(self, jsonl_path: Path,
                         session_type: str = "claude",
                         slot: AgentSlot | None = None) -> bool:
        """Create or respawn the right pane with a ``less`` transcript viewer.

        Mouse-wheel scrolling works after focusing the right pane (double-click
        or Ctrl-B →) when less ≥ 590 is installed.

        Returns True on success.
        """
        import shlex
        import sys as _sys
        mouse = self._less_mouse_flag
        # Tail the last 2000 lines so large sessions appear instantly. Tailing
        # drops the leading ``session_meta`` record, so a long Codex rollout
        # would otherwise be auto-detected as Claude and render blank. Pass the
        # format explicitly from SessionMeta.session_type so the parser doesn't
        # have to guess from the first tailed record (#5).
        fmt = "codex" if session_type == "codex" else "claude"
        path = shlex.quote(str(jsonl_path))
        python = shlex.quote(_sys.executable)
        # LESSSECURE turns the pager into a viewer: no shell, pipe, editor,
        # alternate-file, tag, or logfile commands. Do not persist searches,
        # and suppress any user-configured input pre/postprocessors.
        less_env = ("LESSSECURE=1 LESSHISTFILE=- "
                    "LESSOPEN= LESSCLOSE=")
        cmd = (f"tail -n 2000 {path} | "
               f"{python} -m railmux.transcript --format {fmt} "
               f"--preview-limit 2000 - | "
               f"{less_env} less -R +G {mouse}").rstrip()
        slot = slot or self._agent_workspace().target
        # In swap mode ``slot.pane_id`` is the real provider pane. Return it
        # home before the destructive ``respawn-pane -k`` below; only the
        # display placeholder may be replaced by the transcript viewer.
        if not self._display_transport().prepare_preview(slot):
            self._set_status(
                "failed to return the agent home before transcript preview",
                "error",
            )
            return False
        if slot.pane_id and tmux_ctl.pane_alive(slot.pane_id):
            if not tmux_ctl.respawn_pane(slot.pane_id, cmd):
                self._set_status("failed to respawn right pane for transcript")
                return False
        else:
            new_id = tmux_ctl.split_window_h(cmd, size_percent=70, detached=True)
            if not new_id:
                self._set_status("failed to create right pane for transcript")
                return False
            slot.pane_id = new_id
            self._set_railmux_focus(self._railmux_has_focus, force_border=True)
        # The display pane is now showing a transcript, not an agent session.
        slot.agent_tmux_name = None
        slot.mode_key = self._current_mode_key()
        self._set_divider_active(
            not getattr(self, "_railmux_has_focus", True), force=True)
        self._install_tmux_bindings()
        return True

    def _sync_sidebar_to_agent_project(self, tmux_name: str | None) -> None:
        """Restore sidebar project/session context for one displayed agent."""
        if tmux_name is None:
            return
        running = self._by_tmux(tmux_name)
        if running is None or running.project is None:
            return
        project = self._project_in_current_view(running.project)
        if (self._selected_project is not None
                and self._selected_project.encoded_name == project.encoded_name):
            return
        self._set_current_project(project)
        # A synthetic Codex project (empty claude_dir) must never reach the
        # Claude cache; _pane_sessions owns the provider-specific routing.
        sessions = self._pane_sessions(
            project, refresh=not self._mode_refresh_pending())
        self._sessions_pane.set_sessions(
            project,
            sessions,
            running_ids=self._running_session_ids(),
            favorite_ids=self._favorites.get_ids(),
        )

    # --- tmux integration (detached session per agent + display-pane attach) ---

    @staticmethod
    def _safe_name(s: str, n: int = 12) -> str:
        out = "".join(c if c.isalnum() else "-" for c in s)
        return (out.strip("-") or "x")[:n]

    @staticmethod
    def _name_width(key: str) -> int:
        # Real session ids are UUIDs truncated to 16 (reversed by
        # _resolve_truncated_id). Placeholders must NOT be truncated: at 16
        # chars ``__new__-<tok>-1`` and ``__new__-<tok>-10`` collapse to the
        # same tmux name, so counter 10 could hijack counter 1's session (#11).
        return len(key) if key.startswith("__new__-") else 16

    def _session_name(self, key: str) -> str:
        """Stable tmux session name using the active mode's registered prefix."""
        return (
            f"{self._active_mode().tmux_prefix}"
            f"{self._safe_name(key, self._name_width(key))}"
        )

    def _ensure_detached_agent(self, name: str, shell_cmd: str,
                               env: dict[str, str] | None = None
                               ) -> tuple[bool, str | None]:
        """Create a detached agent tmux session unless it already exists.

        Returns ``(True, None)`` on success, ``(False, reason)`` on failure.

        *env* only ever carries the NON-secret ``CODEX_HOME`` — railmux passes
        no provider API key (it relies on the login shell). It's handed to tmux
        via ``-e`` (which does persist in the session env, so it must stay
        secret-free)."""
        if tmux_ctl.session_exists(name):
            return True, None
        return tmux_ctl.new_detached_session(name, shell_cmd, env=env)

    def _ensure_detached_claude(self, name: str, shell_cmd: str,
                                env: dict[str, str] | None = None
                                ) -> tuple[bool, str | None]:
        """Compatibility alias retained for pre-registry integrations."""
        return self._ensure_detached_agent(name, shell_cmd, env)

    def _configure_scroll_acceleration(self, claude_tmux_name: str) -> None:
        """Configure coalescing for the pane currently showing the provider."""
        target = self._display_transport().displayed_real_pane(claude_tmux_name)
        if target is None:
            self._scroll_manager.configure(claude_tmux_name)
        else:
            self._scroll_manager.configure(
                claude_tmux_name, target_pane=target)

    def _teardown_scroll_acceleration(self) -> None:
        self._scroll_manager.close()

    def _attach_agent_slot(self, slot: AgentSlot, agent_tmux_name: str, *,
                           steal_focus: bool = True) -> bool:
        """Make *slot* display an agent through the selected safe transport."""
        if not self._agent_workspace().can_display(slot, agent_tmux_name):
            self._set_status(
                "That agent session is already displayed in another pane.",
                "warn",
            )
            return False
        selection = getattr(self, "_selection_isolation_manager", None)
        if selection is not None:
            selection.release_all()
        previous_session_id = slot.active_session_id
        previous_tmux_name = slot.agent_tmux_name
        # A swap becomes visible to tmux in the middle of this synchronous
        # transaction. Paint the intended active target before entering tmux
        # so the old row does not remain as a second, grey selection while the
        # new row already has the green list cursor. This deliberately does not
        # mutate ``slot``: its confirmed state remains available if attach
        # fails. Double-click needs this flush too because its earlier draw only
        # painted the right-pane focus transition.
        self._paint_slot_active_tmux_target(slot, agent_tmux_name)
        self._redraw_focus_state_now()
        running = self._by_tmux(agent_tmux_name)
        if running is not None and getattr(running, "is_legacy", False):
            outcome = self._display_transport().attach(
                slot,
                agent_tmux_name,
                server_target=running.legacy_server,
                session_target=running.legacy_session_id,
            )
        else:
            outcome = self._display_transport().attach(slot, agent_tmux_name)
        if not outcome.ok:
            self._reconcile_failed_attach_target(
                slot,
                previous_session_id=previous_session_id,
                previous_tmux_name=previous_tmux_name,
            )
            return False
        if slot.pane_id is None:
            self._reconcile_failed_attach_target(
                slot,
                previous_session_id=previous_session_id,
                previous_tmux_name=previous_tmux_name,
            )
            return False
        self._check_agent_slot_size(slot)
        if steal_focus:
            tmux_ctl.select_pane(slot.pane_id)
        slot.agent_tmux_name = agent_tmux_name
        mode = self._modes().for_tmux_name(agent_tmux_name)
        slot.mode_key = mode.key if mode is not None else None
        self._set_active_tmux_target(agent_tmux_name, slot)
        self._set_railmux_focus(
            not steal_focus and not self._double_focus_visual_pending,
            force_border=True,
        )
        if running is not None and getattr(running, "is_legacy", False):
            self._teardown_scroll_acceleration()
            self._set_status(
                "Legacy session opened safely; restart it when convenient",
                "warn",
            )
        elif outcome.fell_back and outcome.reason:
            self._set_status(
                f"Using nested agent display: {outcome.reason}", "warn")
        if (slot.key == self._agent_workspace().target_slot_key
                and not (running is not None
                         and getattr(running, "is_legacy", False))):
            self._schedule_scroll_acceleration(agent_tmux_name)
        if (slot is self._primary_slot
                and not getattr(self, "_restoring_workspace", False)):
            self._apply_layout_profile(allow_create=True)
        self._install_tmux_bindings()
        if (steal_focus
                and self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            self._select_workspace_page(
                WorkspacePage.SECONDARY
                if slot is self._agent_workspace().secondary
                else WorkspacePage.PRIMARY
            )
        return True

    def _reconcile_failed_attach_target(
        self,
        slot: AgentSlot,
        *,
        previous_session_id: str | None,
        previous_tmux_name: str | None,
    ) -> None:
        """Restore optimistic highlights after a failed attach.

        Most failures leave the old display intact, so its visual state is
        restored without touching the confirmed slot model. A swap can also
        fail after it safely returned the old pane home, or after it retained
        recovery metadata for the new pane. In those cases the transport has
        updated ``slot.agent_tmux_name`` to describe what remains on screen;
        commit that truthful final target instead of claiming the old one is
        still displayed.
        """
        displayed_tmux_name = slot.agent_tmux_name
        if displayed_tmux_name == previous_tmux_name:
            self._paint_slot_active_target(
                slot, previous_session_id, previous_tmux_name)
        elif displayed_tmux_name is not None:
            self._set_active_tmux_target(displayed_tmux_name, slot)
        else:
            self._set_slot_active_target(slot, None, None)
        self._redraw_focus_state_now()

    def _attach_in_right_pane(self, claude_tmux_name: str, *,
                               steal_focus: bool = True) -> bool:
        """Compatibility entry point targeting the current primary slot."""
        return self._attach_agent_slot(
            self._primary_slot, claude_tmux_name, steal_focus=steal_focus)

    def _install_tmux_bindings(self) -> None:
        """Ensure global forwarding and the current Target projection exist."""
        manager = getattr(self, "_tmux_binding_manager", None)
        opened = manager is not None and manager.open()
        selection = getattr(self, "_selection_isolation_manager", None)
        if selection is not None:
            selection.sync(
                self._agent_workspace(),
                enabled=bool(
                    opened and manager.selection_isolation_available),
            )
        if opened:
            if manager.target_toggle_available:
                self._sync_target_pane_option(force=True)
            elif not getattr(self, "_target_toggle_warning_shown", False):
                self._target_toggle_warning_shown = True
                self._set_status(
                    "Ctrl-B Tab unavailable; existing tmux binding preserved.",
                    "warn",
                )

    def _set_workspace_target(self, slot_key: str) -> AgentSlot:
        """Apply one Target transition and refresh its tmux projection."""
        slot = self._agent_workspace().set_target(slot_key)
        self._sync_target_pane_option()
        return slot

    def _sync_target_pane_option(self, *, force: bool = False) -> bool:
        """Project the authoritative outer Target pane for prefix-Tab."""
        owner = getattr(self, "_railmux_pane_id", None)
        if owner is None:
            return False
        pane_id = self._agent_workspace().target.pane_id
        desired = pane_id if pane_id and pane_id.startswith("%") else ""
        current = getattr(self, "_projected_target_pane_id", None)
        if not force and current == desired:
            return True
        applied = tmux_ctl.set_window_user_option(
            owner, tmux_ctl.RAILMUX_TARGET_OPTION, desired)
        if applied:
            self._projected_target_pane_id = desired
        return applied

    def _clear_target_pane_option(self) -> None:
        """Release only the exact Target projection this process installed."""
        owner = getattr(self, "_railmux_pane_id", None)
        expected = getattr(self, "_projected_target_pane_id", None)
        if owner is None or expected is None:
            return
        if tmux_ctl.unset_window_user_option_if_value(
                owner, tmux_ctl.RAILMUX_TARGET_OPTION, expected):
            self._projected_target_pane_id = None

    def _toggle_agent_fullscreen(self) -> None:
        """Zoom the focused agent, or the last active agent from the sidebar."""
        if (self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            self._set_status(
                "Compact view already shows one full-window page.", "tip")
            return
        slot = self._sync_target_slot_from_tmux()
        pane_id = slot.pane_id
        if pane_id is None or not tmux_ctl.pane_alive(pane_id):
            self._set_status("No agent pane to fullscreen.", "tip")
            return
        if not self._zoom_pane(pane_id, toggle_if_current=True):
            self._set_status("Could not toggle agent fullscreen.", "error")

    def _pane_for_workspace_page(
        self, page: WorkspacePage,
    ) -> str | None:
        workspace = self._agent_workspace()
        if page is WorkspacePage.SIDEBAR:
            return getattr(self, "_railmux_pane_id", None)
        if page is WorkspacePage.PRIMARY:
            return workspace.primary.pane_id
        return workspace.secondary.pane_id

    def _window_is_zoomed(self) -> bool:
        """Best-effort zoom query scoped to Railmux's current window."""
        pane_id = (
            getattr(self, "_railmux_pane_id", None)
            or self._agent_workspace().primary.pane_id
        )
        if pane_id is None:
            return False
        try:
            import subprocess as _sp
            result = _sp.run(
                ["tmux", "display-message", "-p", "-t", pane_id,
                 "-F", "#{window_zoomed_flag}"],
                stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True,
            )
            return result.returncode == 0 and result.stdout.strip() == "1"
        except Exception:
            return False

    def _zoom_pane(
        self, pane_id: str, *, toggle_if_current: bool = False,
    ) -> bool:
        """Select one pane and establish deterministic zoom ownership."""
        owner = getattr(self, "_railmux_pane_id", None) or pane_id
        active = tmux_ctl.active_pane_id(owner)
        zoomed = self._window_is_zoomed()
        if zoomed and active == pane_id:
            return (
                tmux_ctl.toggle_pane_zoom(pane_id)
                if toggle_if_current else True
            )
        if zoomed:
            if active is None or not tmux_ctl.toggle_pane_zoom(active):
                return False
        if active != pane_id and not tmux_ctl.select_pane(pane_id):
            return False
        return tmux_ctl.toggle_pane_zoom(pane_id)

    def _select_workspace_page(
        self,
        page: WorkspacePage,
        *,
        announce: bool = False,
    ) -> bool:
        """Select and zoom one compact page without toggling blindly.

        This is the single authority used by geometry transitions and modal
        restoration. Target remains a separate workspace concept: returning to
        Railmux does not forget it, while choosing A1/A2 makes that agent the
        natural Target just as focusing it in a wide layout does.
        """
        workspace = self._agent_workspace()
        pane_id = self._pane_for_workspace_page(page)
        if pane_id is None or not tmux_ctl.pane_alive(pane_id):
            if announce:
                self._set_status("That compact page is not available.", "tip")
            return False

        if not self._zoom_pane(pane_id):
            return False

        workspace.compact_page = page
        if page is WorkspacePage.PRIMARY:
            self._set_workspace_target(AgentWorkspace.PRIMARY)
        elif page is WorkspacePage.SECONDARY:
            self._set_workspace_target(AgentWorkspace.SECONDARY)
        self._set_railmux_focus(
            page is WorkspacePage.SIDEBAR, force_border=True)
        self._apply_tmux_bar(self._tmux_error_bar)
        return True

    def _sync_compact_page_from_tmux(self) -> None:
        """Follow status-bar clicks or external pane selection in compact mode."""
        workspace = self._agent_workspace()
        if workspace.presentation is not WorkspacePresentation.COMPACT:
            return
        owner = getattr(self, "_railmux_pane_id", None)
        if owner is None:
            return
        active = tmux_ctl.active_pane_id(owner)
        if active == owner:
            page = WorkspacePage.SIDEBAR
        elif active == workspace.primary.pane_id:
            page = WorkspacePage.PRIMARY
        elif active == workspace.secondary.pane_id:
            page = WorkspacePage.SECONDARY
        else:
            return
        if page is workspace.compact_page:
            return
        workspace.compact_page = page
        if page is WorkspacePage.PRIMARY:
            self._set_workspace_target(AgentWorkspace.PRIMARY)
        elif page is WorkspacePage.SECONDARY:
            self._set_workspace_target(AgentWorkspace.SECONDARY)
        self._set_railmux_focus(
            page is WorkspacePage.SIDEBAR, force_border=True)
        self._apply_tmux_bar(self._tmux_error_bar)

    def _set_workspace_presentation(
        self, presentation: WorkspacePresentation,
    ) -> bool:
        """Transition presentation while preserving pane topology and Target."""
        workspace = self._agent_workspace()
        if workspace.presentation is presentation:
            return True
        if presentation is WorkspacePresentation.COMPACT:
            owner = getattr(self, "_railmux_pane_id", None)
            active = tmux_ctl.active_pane_id(owner) if owner else None
            was_zoomed = self._window_is_zoomed()
            self._pre_compact_wide_zoom_pane = active if was_zoomed else None
            self._pre_compact_layout_profile = (
                None
                if was_zoomed
                else self._capture_layout_profile("always")
            )
            slot = workspace.slot_for_pane(active) if active else None
            if active == owner:
                page = WorkspacePage.SIDEBAR
            elif slot is workspace.secondary:
                page = WorkspacePage.SECONDARY
            elif slot is workspace.primary:
                page = WorkspacePage.PRIMARY
            elif not getattr(self, "_railmux_has_focus", True):
                page = (
                    WorkspacePage.SECONDARY
                    if workspace.target is workspace.secondary
                    else WorkspacePage.PRIMARY
                )
            else:
                page = WorkspacePage.SIDEBAR
            workspace.presentation = presentation
            if not self._select_workspace_page(page):
                # The sidebar exists for every live UI and is the safest
                # fallback when an agent pane disappears during the resize.
                if page is not WorkspacePage.SIDEBAR:
                    self._select_workspace_page(WorkspacePage.SIDEBAR)
            return True

        # Leaving compact mode must remove only Railmux's current page zoom.
        if self._window_is_zoomed():
            owner = (
                getattr(self, "_railmux_pane_id", None)
                or workspace.primary.pane_id
            )
            active = tmux_ctl.active_pane_id(owner) if owner else None
            if active is not None and not tmux_ctl.toggle_pane_zoom(active):
                return False
        workspace.presentation = presentation
        compact_profile = getattr(
            self, "_pre_compact_layout_profile", None)
        self._pre_compact_layout_profile = None
        if not self._restore_transient_layout_profile(compact_profile):
            self._resize_sidebar_for_layout(workspace.layout)
        self._apply_layout_profile(allow_create=True)
        self._reconcile_focus_from_tmux()
        restore_zoom = getattr(
            self, "_pre_compact_wide_zoom_pane", None)
        self._pre_compact_wide_zoom_pane = None
        if (restore_zoom is not None
                and tmux_ctl.pane_alive(restore_zoom)):
            active = tmux_ctl.active_pane_id(restore_zoom)
            if active != restore_zoom:
                tmux_ctl.select_pane(restore_zoom)
            tmux_ctl.toggle_pane_zoom(restore_zoom)
        self._apply_tmux_bar(self._tmux_error_bar)
        return True

    def _agent_region_size(self) -> tuple[int, int] | None:
        """Size of the agent area before its optional 50/50 inner split."""
        workspace = self._agent_workspace()
        primary_id = workspace.primary.pane_id
        if primary_id is None:
            return None
        primary_size = tmux_ctl.pane_size(primary_id)
        if primary_size is None:
            return None
        if workspace.layout is WorkspaceLayout.SINGLE:
            return primary_size
        secondary_id = workspace.secondary.pane_id
        secondary_size = (
            tmux_ctl.pane_size(secondary_id) if secondary_id else None)
        if secondary_size is None:
            return None
        pw, ph = primary_size
        sw, sh = secondary_size
        if workspace.layout is WorkspaceLayout.SIDE_BY_SIDE:
            return pw + sw + 1, max(ph, sh)
        return max(pw, sw), ph + sh + 1

    @classmethod
    def _sidebar_width_for_layout(
        cls,
        layout: WorkspaceLayout,
        window_width: int,
        sidebar_permille: int | None = None,
    ) -> int:
        """Responsive sidebar width for one workspace layout."""
        if sidebar_permille is None:
            percent = (
                cls._SINGLE_SIDEBAR_PERCENT
                if layout is WorkspaceLayout.SINGLE
                else cls._DUAL_SIDEBAR_PERCENT
            )
            width = round(window_width * percent / 100)
        else:
            width = round(window_width * sidebar_permille / 1000)
        if layout is not WorkspaceLayout.SINGLE:
            width = max(cls._DUAL_SIDEBAR_MIN_WIDTH, width)
        # The layout-fit gate rejects dual panes before a tiny window can reach
        # this clamp; the final bound keeps every best-effort tmux request valid.
        return min(max(1, width), max(1, window_width - 2))

    def _resize_sidebar_for_layout(self, layout: WorkspaceLayout) -> bool:
        """Apply the layout's sidebar ratio without making layout depend on it."""
        if (self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            # A zoomed pane reports full-window geometry. Resizing from that
            # measurement would corrupt the hidden wide-layout proportions.
            return True
        sidebar_id = getattr(self, "_railmux_pane_id", None)
        if sidebar_id is None:
            return False
        window = tmux_ctl.window_size(sidebar_id)
        current = tmux_ctl.pane_size(sidebar_id)
        if window is None or current is None:
            return False
        desired = self._sidebar_width_for_layout(
            layout,
            window[0],
            getattr(self, "_active_sidebar_permille", None),
        )
        if current[0] == desired:
            return True
        return tmux_ctl.resize_pane_width(sidebar_id, desired)

    def _capture_layout_profile(self, scope: str) -> LayoutProfile | None:
        """Capture current pane proportions without persisting tmux identity."""
        if scope not in {"always", "once"}:
            return None
        workspace = self._agent_workspace()
        if workspace.presentation is WorkspacePresentation.COMPACT:
            # Compact zoom leaves hidden panes reporting their old narrow
            # geometry. Never let that transient presentation overwrite the
            # user's saved wide proportions.
            return None
        sidebar_id = getattr(self, "_railmux_pane_id", None)
        primary_id = workspace.primary.pane_id
        if sidebar_id is None or primary_id is None:
            return None
        window = tmux_ctl.window_size(sidebar_id)
        sidebar = tmux_ctl.pane_size(sidebar_id)
        primary = tmux_ctl.pane_size(primary_id)
        if window is None or sidebar is None or primary is None:
            return None
        sidebar_permille = min(
            800, max(50, round(sidebar[0] * 1000 / window[0])))
        primary_permille: int | None = None
        if workspace.layout is not WorkspaceLayout.SINGLE:
            region = self._agent_region_size()
            if region is None:
                return None
            if workspace.layout is WorkspaceLayout.SIDE_BY_SIDE:
                usable, primary_extent = region[0] - 1, primary[0]
            else:
                usable, primary_extent = region[1] - 1, primary[1]
            if usable <= 0:
                return None
            primary_permille = min(
                900, max(100, round(primary_extent * 1000 / usable)))
        return LayoutProfile(
            scope=scope,
            layout=workspace.layout.value,
            sidebar_permille=sidebar_permille,
            primary_permille=primary_permille,
        )

    def _resize_primary_for_ratio(
        self,
        region: tuple[int, int],
        ratio: int | None,
    ) -> bool:
        """Restore the primary share of one already-existing dual layout."""
        workspace = self._agent_workspace()
        primary = workspace.primary.pane_id
        if (workspace.layout is WorkspaceLayout.SINGLE
                or primary is None or ratio is None):
            return True
        if workspace.layout is WorkspaceLayout.SIDE_BY_SIDE:
            usable = region[0] - 1
            minimum = self._MINIMUM_AGENT_PANE_SIZE[0]
            if usable < minimum * 2:
                return False
            desired = min(usable - minimum, max(
                minimum, round(usable * ratio / 1000)))
            return tmux_ctl.resize_pane_width(primary, desired)
        usable = region[1] - 1
        minimum = self._MINIMUM_AGENT_PANE_SIZE[1]
        if usable < minimum * 2:
            return False
        desired = min(usable - minimum, max(
            minimum, round(usable * ratio / 1000)))
        return tmux_ctl.resize_pane_height(primary, desired)

    def _restore_transient_layout_profile(
        self, profile: LayoutProfile | None,
    ) -> bool:
        """Replay the pre-compact geometry without persisting new settings."""
        workspace = self._agent_workspace()
        if profile is None or profile.layout != workspace.layout.value:
            return False
        old_sidebar = getattr(self, "_active_sidebar_permille", None)
        old_primary = getattr(self, "_active_primary_permille", None)
        self._active_sidebar_permille = profile.sidebar_permille
        self._active_primary_permille = profile.primary_permille
        restored = self._resize_sidebar_for_layout(workspace.layout)
        region = self._agent_region_size() if restored else None
        if (region is not None
                and self._layout_fits(region, workspace.layout)
                and self._resize_primary_for_ratio(
                    region, profile.primary_permille)):
            return True
        self._active_sidebar_permille = old_sidebar
        self._active_primary_permille = old_primary
        return False

    def _layout_profile_failed_to_fit(self) -> bool:
        """Retain the preference while returning this run to safe defaults."""
        self._layout_profile_fallback = True
        self._layout_profile_applied = False
        self._active_sidebar_permille = None
        self._active_primary_permille = None
        self._resize_sidebar_for_layout(self._agent_workspace().layout)
        self._set_status(
            "Saved layout does not fit this terminal; using safe defaults.",
            "warn",
        )
        return False

    def _apply_layout_profile(self, *, allow_create: bool) -> bool:
        """Apply a saved profile once, after its pane topology is available."""
        if (self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            # Keep the preference unconsumed until a genuinely wide geometry
            # exists; applying it behind a zoom would sample misleading sizes.
            return False
        if getattr(self, "_layout_profile_applied", False):
            return True
        if (getattr(self, "_layout_profile_fallback", False)
                or getattr(self, "_layout_geometry_user_owned", False)):
            # A failed attempt or newer explicit F8/divider choice owns the
            # rest of this run. Keep the profile for a future launch without
            # repeatedly recreating a split or warning on every Pane 1 open.
            return False
        profile = getattr(self, "_layout_profile", None)
        if profile is None:
            return False
        workspace = self._agent_workspace()
        if workspace.primary.pane_id is None:
            return False
        try:
            requested = WorkspaceLayout(profile.layout)
        except ValueError:
            return False

        created_secondary = False
        if workspace.layout is not requested:
            if (workspace.layout is WorkspaceLayout.SINGLE
                    and requested is not WorkspaceLayout.SINGLE
                    and allow_create):
                self._active_sidebar_permille = profile.sidebar_permille
                self._resize_sidebar_for_layout(requested)
                region = self._agent_region_size()
                if region is None or not self._layout_fits(region, requested):
                    return self._layout_profile_failed_to_fit()
                if not self._display_transport().create_secondary(requested):
                    return self._layout_profile_failed_to_fit()
                created_secondary = True
            else:
                return self._layout_profile_failed_to_fit()

        self._active_sidebar_permille = profile.sidebar_permille
        self._active_primary_permille = profile.primary_permille
        if not self._resize_sidebar_for_layout(workspace.layout):
            if created_secondary:
                self._display_transport().close_slot(workspace.secondary)
                workspace.layout = WorkspaceLayout.SINGLE
            return self._layout_profile_failed_to_fit()

        region = self._agent_region_size()
        if (workspace.layout is not WorkspaceLayout.SINGLE
                and (region is None
                     or not self._layout_fits(region, workspace.layout))):
            if created_secondary:
                self._display_transport().close_slot(workspace.secondary)
                workspace.layout = WorkspaceLayout.SINGLE
            return self._layout_profile_failed_to_fit()

        ratio = profile.primary_permille
        if (workspace.layout is not WorkspaceLayout.SINGLE
                and region is not None and ratio is not None):
            if not self._resize_primary_for_ratio(region, ratio):
                if created_secondary:
                    self._display_transport().close_slot(workspace.secondary)
                    workspace.layout = WorkspaceLayout.SINGLE
                return self._layout_profile_failed_to_fit()

        self._layout_profile_applied = True
        self._layout_profile_fallback = False
        if profile.scope == "once":
            if not self._settings.consume_layout_profile(profile):
                self._set_status(
                    "Layout restored, but its one-time setting could not be "
                    "cleared.",
                    "warn",
                )
            else:
                self._layout_profile = None
        return True

    def _layout_fits(
        self, region: tuple[int, int], layout: WorkspaceLayout,
    ) -> bool:
        width, height = projected_agent_size(region, layout)
        min_width, min_height = self._MINIMUM_AGENT_PANE_SIZE
        return width >= min_width and height >= min_height

    def _next_available_layout(
        self,
        current: WorkspaceLayout,
        region: tuple[int, int],
    ) -> WorkspaceLayout | None:
        """Return the next usable layout without stopping at an invalid split."""
        candidate = next_workspace_layout(current)
        while candidate is not current:
            if candidate is WorkspaceLayout.SINGLE:
                return candidate
            if (self._agent_workspace().presentation
                    is WorkspacePresentation.COMPACT):
                # Only one pane is visible and zoomed in compact presentation,
                # so the underlying equal-split rectangle is not the usable
                # agent viewport. Transport creation still performs its own
                # identity and lifecycle safety checks.
                return candidate
            if self._layout_fits(region, candidate):
                return candidate
            candidate = next_workspace_layout(candidate)
        return None

    def _focused_pane_menu_target(
        self,
    ) -> tuple[str, SessionMeta | RunningEntry, str] | None:
        position = self._sidebar.focus_position
        if position == 1:
            session = self._currently_focused_session_meta()
            if session is not None:
                return "session", session, session.display_title
        elif position == 2 and self._running_pane._walker:
            focus_w, _ = self._running_pane._walker.get_focus()
            from railmux.ui.running_pane import _RunningRow
            if isinstance(focus_w, _RunningRow):
                return "running", focus_w.entry, focus_w.entry.label
        return None

    def _preview_focused_target(self) -> None:
        """Mirror a single-click for the focused Sessions/Running target."""
        target = self._focused_pane_menu_target()
        if target is None:
            self._set_status(
                "Select a Sessions or Running row to preview or switch it.",
                "tip")
            return
        kind, value, _label = target
        if kind == "session" and isinstance(value, SessionMeta):
            self._on_session_row_preview(value)
        elif kind == "running" and isinstance(value, RunningEntry):
            self._on_running_select(value, steal_focus=False)

    def _close_secondary_split(self, *, announce: bool = True) -> bool:
        workspace = self._agent_workspace()
        secondary = workspace.secondary
        if not secondary.is_open:
            if announce:
                self._set_status("Pane 2 is not open.", "tip")
            return False
        selection = getattr(self, "_selection_isolation_manager", None)
        if selection is not None:
            selection.release_all()
        remembered_agent = secondary.agent_tmux_name
        sidebar_focused = self._railmux_has_focus
        if not self._display_transport().close_slot(secondary):
            self._set_status(
                "Could not safely return Pane 2's agent home.", "error")
            return False
        if remembered_agent is not None:
            workspace.collapsed_secondary_agent = remembered_agent
        workspace.layout = WorkspaceLayout.SINGLE
        self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
        self._set_workspace_target(AgentWorkspace.PRIMARY)
        if not sidebar_focused and workspace.primary.pane_id:
            tmux_ctl.select_pane(workspace.primary.pane_id)
        self._paint_slot_active_target(
            workspace.primary,
            workspace.primary.active_session_id,
            workspace.primary.agent_tmux_name,
        )
        self._install_tmux_bindings()
        self._set_railmux_focus(sidebar_focused, force_border=True)
        if announce:
            if remembered_agent is None:
                self._set_status("Single pane; empty Pane 2 closed.")
            else:
                self._set_status(
                    "Single pane; Pane 2's agent continues in Running.")
        return True

    def _rebuild_secondary(
        self, layout: WorkspaceLayout, agent_tmux_name: str | None,
    ) -> bool:
        workspace = self._agent_workspace()
        if not self._display_transport().create_secondary(layout):
            return False
        if agent_tmux_name is None:
            return True
        return self._attach_agent_slot(
            workspace.secondary, agent_tmux_name, steal_focus=False)

    def _rotate_split(self) -> None:
        """Cycle layout, committing new geometry authority only on success."""
        old_sidebar = getattr(self, "_active_sidebar_permille", None)
        old_primary = getattr(self, "_active_primary_permille", None)
        # A new orientation starts from responsive defaults. If it cannot be
        # built, restore the previous profile so a failed F8 cannot overwrite a
        # good Always preference during exit.
        self._active_sidebar_permille = None
        self._active_primary_permille = None
        committed = self._rotate_split_attempt()
        if (self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            self._restore_compact_page()
        if committed:
            if (self._agent_workspace().presentation
                    is WorkspacePresentation.COMPACT):
                # F8 intentionally changed topology while the old wide
                # geometry was hidden; do not replay that stale orientation
                # if the user later cycles back to the same layout.
                self._pre_compact_layout_profile = None
            self._layout_geometry_user_owned = True
            self._layout_profile_fallback = False
            return
        self._active_sidebar_permille = old_sidebar
        self._active_primary_permille = old_primary
        self._resize_sidebar_for_layout(self._agent_workspace().layout)

    def _restore_compact_page(self) -> bool:
        """Re-zoom the best surviving page after a topology operation."""
        workspace = self._agent_workspace()
        if workspace.presentation is not WorkspacePresentation.COMPACT:
            return True
        page = workspace.compact_page
        if self._pane_for_workspace_page(page) is None:
            if workspace.primary.pane_id is not None:
                page = WorkspacePage.PRIMARY
            else:
                page = WorkspacePage.SIDEBAR
        return self._select_workspace_page(page)

    def _rotate_split_attempt(self) -> bool:
        """Perform one F8 transition and report whether it committed."""
        workspace = self._agent_workspace()
        secondary = workspace.secondary
        old_layout = workspace.layout
        new_layout = next_workspace_layout(old_layout)

        selection = getattr(self, "_selection_isolation_manager", None)
        if selection is not None:
            selection.release_all()

        if new_layout is WorkspaceLayout.SINGLE:
            return self._close_secondary_split()

        if old_layout is WorkspaceLayout.SINGLE:
            agent_tmux_name = workspace.collapsed_secondary_agent
            if (agent_tmux_name is None
                    or not self._agent_session_alive(agent_tmux_name)):
                workspace.collapsed_secondary_agent = None
                agent_tmux_name = None
            if workspace.primary.agent_tmux_name == agent_tmux_name:
                workspace.collapsed_secondary_agent = None
                agent_tmux_name = None
            self._resize_sidebar_for_layout(new_layout)
            region = self._agent_region_size()
            if region is None:
                self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
                self._set_status(
                    "Cannot open Pane 2: available size is unknown.", "warn")
                return False
            available_layout = self._next_available_layout(old_layout, region)
            if available_layout is None:
                self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
                self._set_status(
                    "Cannot open Pane 2: neither split layout meets the "
                    "minimum pane size of 50×12.",
                    "warn",
                )
                return False
            new_layout = available_layout
            sidebar_focused = self._railmux_has_focus
            if not self._rebuild_secondary(new_layout, agent_tmux_name):
                self._display_transport().close_slot(secondary)
                workspace.layout = WorkspaceLayout.SINGLE
                self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
                self._set_workspace_target(AgentWorkspace.PRIMARY)
                self._set_railmux_focus(sidebar_focused, force_border=True)
                self._set_status(
                    "Could not create Pane 2; any remembered agent remains "
                    "in Running.",
                    "error",
                )
                return False
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            if not sidebar_focused and workspace.primary.pane_id:
                tmux_ctl.select_pane(workspace.primary.pane_id)
            self._install_tmux_bindings()
            self._set_railmux_focus(sidebar_focused, force_border=True)
            self._set_status(f"Layout → {new_layout.value}")
            return True

        if not secondary.is_open:
            workspace.layout = WorkspaceLayout.SINGLE
            self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
            self._set_railmux_focus(
                self._railmux_has_focus, force_border=True)
            self._set_status(
                "Pane 2 disappeared; returned to single-pane layout.", "warn")
            return False

        region = self._agent_region_size()
        if region is None:
            self._set_status(
                "Cannot rotate: available pane size is unknown.", "warn")
            return False
        available_layout = self._next_available_layout(old_layout, region)
        if available_layout is WorkspaceLayout.SINGLE:
            return self._close_secondary_split()
        new_layout = available_layout

        primary_id = workspace.primary.pane_id
        old_secondary_id = secondary.pane_id
        if primary_id is None or old_secondary_id is None:
            return False
        active_before = tmux_ctl.active_pane_id(primary_id)
        sidebar_focused = self._railmux_has_focus
        target_slot_before = workspace.target_slot_key
        agent_tmux_name = secondary.agent_tmux_name

        if not self._display_transport().close_slot(secondary):
            self._set_status("Rotate stopped: Pane 2 could not return home.", "error")
            return False
        workspace.layout = WorkspaceLayout.SINGLE
        if not self._rebuild_secondary(new_layout, agent_tmux_name):
            self._display_transport().close_slot(secondary)
            workspace.layout = WorkspaceLayout.SINGLE
            if not self._rebuild_secondary(old_layout, agent_tmux_name):
                self._display_transport().close_slot(secondary)
                workspace.layout = WorkspaceLayout.SINGLE
                self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
                self._set_workspace_target(AgentWorkspace.PRIMARY)
                self._set_status(
                    "Rotate failed; Pane 2's agent continues in Running.", "error")
            else:
                self._set_status("Rotate failed; restored the previous layout.", "error")
            self._set_railmux_focus(sidebar_focused, force_border=True)
            return False

        if active_before == old_secondary_id and secondary.pane_id:
            self._set_workspace_target(AgentWorkspace.SECONDARY)
            tmux_ctl.select_pane(secondary.pane_id)
        elif active_before == primary_id:
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            tmux_ctl.select_pane(primary_id)
        else:
            self._set_workspace_target(target_slot_before)
        self._install_tmux_bindings()
        self._resize_sidebar_for_layout(new_layout)
        self._set_railmux_focus(sidebar_focused, force_border=True)
        self._set_status(f"Layout → {new_layout.value}")
        return True

    def _slot_reopen_target(
        self, slot: AgentSlot, session_is_alive: Callable[[str], bool],
    ) -> str | None:
        """Agent target that should survive loss of one outer display pane."""
        if slot.in_history_mode:
            restore = slot.restore_state
            if (restore is not None and restore.kind in ("agent", "claude")
                    and restore.tmux_name
                    and session_is_alive(restore.tmux_name)
                    and self._validated_preview_restore_agent(
                        restore.tmux_name, already_alive=True) is not None):
                return restore.tmux_name
            return None
        agent = slot.agent_tmux_name
        return agent if agent and session_is_alive(agent) else None

    def _reap_dead_display_slots(
        self, transport: AgentDisplayTransport,
    ) -> set[str]:
        """Reap dead swap displays and clear the active sidebar selection."""
        dead_agents: set[str] = set()
        for slot in self._agent_workspace().slots:
            agent = transport.reap_dead_display(slot)
            if agent is None:
                continue
            dead_agents.add(agent)
            # The transport clears the slot before reconciliation. Paint the
            # shared sidebar state here because a single layout then has no
            # missing pane left for _reconcile_display_slots to discover.
            self._paint_slot_active_target(slot, None, None)
        return dead_agents

    def _reconcile_display_slots(
        self,
        session_is_alive: Callable[[str], bool],
        pane_is_alive: Callable[[str], bool],
    ) -> None:
        """Make the two-slot model match outer panes after exits or user kills.

        An explicitly selected dual layout remains dual when one agent exits:
        the lost slot is rebuilt as Railmux's branded empty pane. A missing
        primary requires returning and reattaching the surviving secondary so
        slot-specific swap ownership and left/right or top/bottom order remain
        truthful. Only a pane-creation failure degrades the layout to single.
        """
        workspace = self._agent_workspace()
        transport = self._display_transport()
        old_layout = workspace.layout
        old_target_key = workspace.target_slot_key
        targets: dict[str, str | None] = {}
        lost: set[str] = set()

        for slot in workspace.slots:
            pane_id = slot.pane_id
            if pane_id is None:
                if old_layout is not WorkspaceLayout.SINGLE:
                    lost.add(slot.key)
                continue
            agent_dead = (
                slot.agent_tmux_name is not None
                and not session_is_alive(slot.agent_tmux_name)
            )
            pane_dead = not pane_is_alive(pane_id)
            if not (agent_dead or pane_dead):
                continue
            if slot.swap_state is not None:
                self._set_status(
                    "A swap-owned agent pane needs marker recovery; "
                    "automatic layout cleanup was deferred.",
                    "error",
                )
                continue
            targets[slot.key] = (
                None if agent_dead
                else self._slot_reopen_target(slot, session_is_alive)
            )
            # A nested display pane contains only Railmux's attach client and is
            # safe to remove after the owned agent is proven dead. Swap panes
            # require marker-based recovery and are handled by reap_dead_display.
            if agent_dead and slot.swap_state is None:
                tmux_ctl.kill_pane(pane_id)
            lost.add(slot.key)

        if not lost:
            return

        history_was_lost = any(
            slot.key in lost and slot.in_history_mode
            for slot in workspace.slots
        )

        primary = workspace.primary
        secondary = workspace.secondary
        sidebar_focused = self._railmux_has_focus

        # Preserve the established single-pane lifecycle. Closing an ordinary
        # display pane means "return to the full-width sidebar", not "rebuild
        # Pane 1". A transcript preview is the sole exception: if its saved
        # agent is still alive, restore it silently just as the legacy path did.
        if (old_layout is WorkspaceLayout.SINGLE
                and AgentWorkspace.PRIMARY in lost):
            restore_target = (
                targets.get(AgentWorkspace.PRIMARY)
                if primary.in_history_mode else None
            )
            primary.clear_display()
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            if restore_target is not None:
                if not self._attach_agent_slot(
                        primary, restore_target, steal_focus=False):
                    restore_target = None
            self._paint_slot_active_target(
                primary,
                primary.active_session_id,
                primary.agent_tmux_name,
            )
            if history_was_lost and restore_target is not None:
                self._sync_sidebar_to_agent_project(restore_target)
            if not sidebar_focused and primary.pane_id is not None:
                tmux_ctl.select_pane(primary.pane_id)
            self._install_tmux_bindings()
            self._set_railmux_focus(
                sidebar_focused or primary.pane_id is None,
                force_border=True,
            )
            return

        if AgentWorkspace.PRIMARY in lost:
            primary_target = targets.get(AgentWorkspace.PRIMARY)
            secondary_target = (
                self._slot_reopen_target(secondary, session_is_alive)
                if secondary.pane_id is not None else None
            )
            if secondary.pane_id is not None:
                if not transport.close_slot(secondary):
                    self._set_status(
                        "Pane 1 disappeared, but Pane 2 could not be returned "
                        "home safely.",
                        "error",
                    )
                    return
            primary.clear_display()
            secondary.clear_display()
            workspace.layout = WorkspaceLayout.SINGLE
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            primary_ready = transport.create_primary()
            if primary_ready and primary_target is not None:
                # Failure leaves the newly-created branded pane in place; the
                # still-live agent remains represented in Running.
                self._attach_agent_slot(
                    primary, primary_target, steal_focus=False)
            secondary_ready = bool(
                primary_ready and transport.create_secondary(old_layout))
            if secondary_ready and secondary_target is not None:
                self._attach_agent_slot(
                    secondary, secondary_target, steal_focus=False)

            if secondary_ready:
                workspace.collapsed_secondary_agent = None
                self._set_workspace_target(old_target_key)
            else:
                transport.close_slot(secondary)
                workspace.layout = WorkspaceLayout.SINGLE
                self._set_workspace_target(AgentWorkspace.PRIMARY)
                workspace.collapsed_secondary_agent = secondary_target
            self._resize_sidebar_for_layout(workspace.layout)
            self._paint_slot_active_target(
                workspace.target,
                workspace.target.active_session_id,
                workspace.target.agent_tmux_name,
            )
            if history_was_lost:
                self._sync_sidebar_to_agent_project(
                    workspace.target.agent_tmux_name)
            if not sidebar_focused and workspace.target.pane_id:
                tmux_ctl.select_pane(workspace.target.pane_id)
            self._install_tmux_bindings()
            self._set_railmux_focus(sidebar_focused, force_border=True)
            self._set_status(
                (
                    "Pane 1 exited; kept the layout with an empty pane."
                    if primary.agent_tmux_name is None else
                    "Pane 1 was rebuilt and its agent was restored."
                ) if secondary_ready else
                "Pane 1 exited; could not rebuild the dual layout.",
                "warn" if secondary_ready else "error",
            )
            return

        # Only Pane 2 was lost. Preserve the explicit dual layout even when its
        # agent exited: the empty surface is an intentional launch target.
        secondary_target = targets.get(AgentWorkspace.SECONDARY)
        secondary.clear_display()
        secondary_ready = transport.create_secondary(old_layout)
        if secondary_ready:
            if secondary_target is not None:
                self._attach_agent_slot(
                    secondary, secondary_target, steal_focus=False)
            workspace.collapsed_secondary_agent = None
            self._set_workspace_target(old_target_key)
        else:
            transport.close_slot(secondary)
            workspace.layout = WorkspaceLayout.SINGLE
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            if (workspace.collapsed_secondary_agent is not None
                    and not session_is_alive(
                        workspace.collapsed_secondary_agent)):
                workspace.collapsed_secondary_agent = None
        self._resize_sidebar_for_layout(workspace.layout)
        self._paint_slot_active_target(
            workspace.target,
            workspace.target.active_session_id,
            workspace.target.agent_tmux_name,
        )
        if history_was_lost:
            self._sync_sidebar_to_agent_project(
                workspace.target.agent_tmux_name)
        if not sidebar_focused and workspace.target.pane_id:
            tmux_ctl.select_pane(workspace.target.pane_id)
        self._install_tmux_bindings()
        self._set_railmux_focus(sidebar_focused, force_border=True)
        self._set_status(
            (
                "Pane 2 exited; kept the layout with an empty pane."
                if secondary.agent_tmux_name is None else
                "Pane 2 was rebuilt and its agent was restored."
            ) if secondary_ready else
            "Pane 2 exited; could not rebuild the dual layout.",
            "warn" if secondary_ready else "error",
        )

    def _by_tmux(self, tmux_name: str) -> "_Running | None":
        """Find the running session backed by a given tmux session name."""
        for r in getattr(self, "_running", {}).values():
            if r.tmux_name == tmux_name:
                return r
        return None

    def _by_session_id(self, session_id: str) -> "_Running | None":
        """Prefer the dedicated instance, while still finding legacy-only ids."""
        matches = [
            item for item in self._running.values()
            if item.logical_session_id == session_id
        ]
        return next((item for item in matches if not item.is_legacy), None) or (
            matches[0] if matches else None
        )

    def _running_session_ids(self) -> set[str]:
        return {
            session_id
            for item in self._running.values()
            if (session_id := item.logical_session_id) is not None
        }

    def _agent_session_alive(
        self,
        tmux_name: str,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> bool:
        """Whether an owned agent still has a live process-bearing pane.

        In swap mode the home session contains only the placeholder, so its
        existence is not proof that the agent survived. In nested mode the
        agent remains in its own session and the session identity is enough.
        """
        running = self._by_tmux(tmux_name)
        if running is not None and running.is_legacy:
            return bool(
                running.legacy_server is not None
                and running.legacy_session_id is not None
                and tmux_server.target_has_session(
                    running.legacy_server,
                    running.legacy_session_id,
                    timeout=0.2,
                )
            )
        real_pane = self._display_transport().displayed_real_pane(tmux_name)
        if real_pane is not None:
            if server is not None:
                return real_pane in server.panes
            return tmux_ctl.pane_alive(real_pane)
        if server is not None:
            return tmux_name in server.sessions
        return tmux_ctl.session_exists(tmux_name)

    def _recover_unrepresented_displayed_agents(
        self, server: tmux_ctl.ServerSnapshot | None,
    ) -> int:
        """Re-adopt exact swap-owned agents missing only from ``_running``.

        A displayed real pane lives in the outer window, while its named home
        session contains an inert placeholder whose cwd is unrelated. Normal
        startup discovery therefore cannot validate a dropped registry entry
        against the home pane's cwd until the swap returns. The live swap
        identity plus the session's Railmux binding supplies the missing exact
        authority without inferring from a name or launching/resuming anything.
        """
        candidates = []
        for slot in self._agent_workspace().slots:
            name = slot.agent_tmux_name
            state = slot.swap_state
            if (not name or self._is_help_session_name(name)
                    or self._by_tmux(name) is not None
                    or state is None or state.agent_tmux_name != name
                    or self._modes().for_tmux_name(name) is None):
                continue
            if server is not None and state.agent_pane_id not in server.panes:
                continue
            identity = tmux_ctl.pane_identity(state.agent_pane_id)
            if (identity is None or identity.dead
                    or identity.pane_id != state.agent_pane_id
                    or identity.pane_pid != state.agent_pane_pid
                    or identity.window_id != state.display_window_id
                    or identity.session_name != state.outer_session_name
                    or identity.session_id != state.outer_session_id):
                continue
            candidates.append((name, state))
        if not candidates:
            return 0

        snapshot = getattr(self, "_project_snapshot", None) or []
        projects = {
            self._path_key(project.real_path): project
            for project in snapshot
            if not is_help_workspace(project.real_path)
        }
        try:
            codex_cwds = self._codex_index.all_cwds(refresh=False)
        except Exception:
            codex_cwds = {}
        for cwd, count in codex_cwds.items():
            if not is_help_workspace(cwd):
                projects.setdefault(
                    self._path_key(cwd),
                    self._synthesise_codex_project(cwd, count),
                )

        import json
        recovered = 0
        for name, _state in candidates:
            raw_binding = tmux_ctl.show_session_user_option(
                name, _SESSION_BINDING_OPTION)
            try:
                binding = json.loads(raw_binding) if raw_binding else None
            except (TypeError, ValueError):
                binding = None
            cwd_raw = binding.get("cwd") if isinstance(binding, dict) else None
            if not isinstance(cwd_raw, str):
                continue
            cwd = Path(cwd_raw)
            running = self._valid_running_binding(
                binding,
                {name: (cwd, 0)},
                projects,
                allow_missing_codex_metadata=True,
                # The provider pane is proven by the swap identity above; the
                # home placeholder cannot expose its rollout file descriptor.
                probe_live_writer=False,
            )
            if (running is None or running.tmux_name != name
                    or running.key in self._running):
                continue
            self._running[running.key] = running
            recovered += 1
        return recovered

    def _existing_session_ids(self, cwd: Path, project: Project | None,
                              session_type: str) -> tuple[frozenset[str], bool]:
        """Session ids already present in *cwd* right now (pre-launch snapshot).

        Codex reads a fresh Codex-index scan of the cwd; Claude reads the
        session cache (skipped for a synthetic project with empty claude_dir,
        or a brand-new project dir). Used by ``_launch`` to fence off (#12)
        pre-existing rollouts from placeholder resolution."""
        if session_type == "codex":
            index = self._codex_index
            if isinstance(index, BackgroundCodexIndex):
                complete = index.refresh_and_wait(0.5)
                raw = index.sessions_for_cwd(cwd, refresh=False)
            else:  # compatibility for lightweight integrations/test doubles
                raw = index.sessions_for_cwd(cwd, refresh=True)
                complete = True
        elif project is not None and project.claude_dir != Path():
            raw = self._session_cache.list_sessions(project)
            complete = True
        else:
            raw = []
            complete = True
        return frozenset(s.session_id for s in raw), complete

    def _launch(self, key: str, cmd: list[str], cwd: Path, label: str,
                project: Project | None, placeholder_path: Path | None = None,
                *, steal_focus: bool = True,
                slot: AgentSlot | None = None,
                env: dict[str, str] | None = None,
                login_shell: bool = False,
                session_type: str = "claude") -> bool:
        """Create (or reuse) the detached agent tmux session for `key`,
        register it, and attach it in the right pane. Returns success.

        Shared by resume / new-session / new-project so the tracking bookkeeping
        lives in exactly one place.

        *env* only ever carries the NON-secret ``CODEX_HOME``: it is delivered
        via tmux ``-e`` and *also* embedded in the shell command as a portable
        fallback for tmux too old to support ``-e``. Provider API keys are never
        injected by railmux at all — the login+interactive shell ($SHELL -li)
        that runs Codex sources the user's profile and loads the key itself.
        """
        slot = slot or self._primary_slot
        existing = self._running.get(key)
        if existing is not None and existing.is_legacy:
            self._set_status(
                "Launch refused: the legacy session could not be revalidated; "
                "no duplicate was started",
                "error",
            )
            return False
        tmux_name = existing.tmux_name if existing else self._session_name(key)
        # Never adopt an untracked pre-existing tmux session merely because its
        # deterministic name collides.  Resume discovery must validate and
        # register it first; otherwise stamping/reusing it here could hijack an
        # unrelated or duplicate writer in the click-to-launch race window.
        if existing is None and tmux_ctl.session_exists(tmux_name):
            msg = (
                f"Launch refused: untracked live tmux session '{tmux_name}'"
            )
            self._set_status(msg, "error")
            return False
        # #12: snapshot the session ids already present in the launch cwd BEFORE
        # starting the child, so placeholder resolution only ever binds a NEWLY
        # appeared id — never a rollout another process wrote to the same cwd.
        pre_launch_ids: frozenset[str] = frozenset()
        pre_launch_complete = True
        if placeholder_path is not None:
            pre_launch_ids, pre_launch_complete = self._existing_session_ids(
                placeholder_path, project, session_type)
        # Only the non-secret CODEX_HOME may appear in the command string.
        shell_env = ({k: v for k, v in env.items() if k == "CODEX_HOME"}
                     if env else None)
        shell_cmd = self._shellify(cmd, cwd=cwd, env=shell_env,
                                   login_shell=login_shell)
        launch_marker: orphan_marker.Marker | None = None
        if placeholder_path is not None:
            owner = getattr(self, "_restart_identity", None)
            mode = self._modes().for_tmux_name(tmux_name)
            if owner is None or mode is None:
                ok, err = False, "exact outer tmux identity is unavailable"
            else:
                created_at = time.time()
                holder, err = tmux_ctl.create_detached_holder(tmux_name, env=env)
                ok = holder is not None
                if holder is not None:
                    launch_marker = orphan_marker.Marker(
                        mode_key=mode.key,
                        placeholder_key=key,
                        tmux_name=tmux_name,
                        tmux_session_id=holder.session_id,
                        tmux_pane_id=holder.pane_id,
                        owner=owner,
                        cwd=self._path_key(placeholder_path),
                        created_at=created_at,
                        creation_token=uuid.uuid4().hex,
                        phase="launching",
                    )
                    if not self._write_orphan_marker(launch_marker):
                        tmux_ctl.kill_session_identity(holder)
                        ok, err = False, "could not persist launch recovery marker"
                    else:
                        ok, err = tmux_ctl.start_detached_holder(holder, shell_cmd)
                        if ok:
                            unresolved = launch_marker.with_phase("unresolved")
                            # If this transition fails, the durable launching
                            # marker remains sufficient for conservative
                            # recovery after a crash. Never erase it or guess.
                            if self._write_orphan_marker(unresolved):
                                launch_marker = unresolved
        else:
            ok, err = self._ensure_detached_agent(tmux_name, shell_cmd, env=env)
        if not ok:
            msg = f"Launch failed: {err or 'could not create agent session'}"
            self._set_status(msg, "error")
            return False
        self._running[key] = _Running(
            key=key, tmux_name=tmux_name, label=label, project=project,
            placeholder_path=placeholder_path,
            created_at=(launch_marker.created_at if launch_marker is not None
                        else 0.0),
            session_type=session_type,
            pre_launch_ids=pre_launch_ids,
            orphan=launch_marker,
            allow_heuristic_resolution=pre_launch_complete,
        )
        self._stamp_running(self._running[key])
        attached = (
            self._attach_in_right_pane(tmux_name, steal_focus=steal_focus)
            if slot is self._primary_slot
            else self._attach_agent_slot(
                slot, tmux_name, steal_focus=steal_focus)
        )
        if not attached:
            msg = "Launch failed: could not attach to agent pane"
            self._set_status(msg, "error")
            return False
        return True

    def _launch_resume(self, session_meta: SessionMeta,
                        *, steal_focus: bool = True,
                        from_double: bool = False,
                        slot: AgentSlot | None = None) -> bool:
        explicit_slot = slot
        slot = slot or self._primary_slot
        # Revalidate at action time.  A row can be stale, and an older Railmux
        # may have left a live placeholder writer that startup could not adopt.
        # Discover once more before ever running ``codex resume``/``claude
        # --resume`` so a click cannot create a second writer for one session.
        registry = getattr(self, "_running", None)
        running = self._by_session_id(session_meta.session_id) if registry is not None else None
        if (registry is not None
                and (running is None
                     or not self._agent_session_alive(running.tmux_name))):
            self._discover_orphans_consistent()
            self._discover_legacy_running(force=True)
            # Discovery may restore a still-unresolved placeholder under its
            # placeholder key. Promote it synchronously before deciding the
            # real UUID is stopped; waiting for the next poll recreates the
            # exact click-to-duplicate window this guard is meant to close.
            self._resolve_placeholders([session_meta.project])
            running = self._by_session_id(session_meta.session_id)
        if (running is not None
                and self._agent_session_alive(running.tmux_name)):
            entry = RunningEntry(
                tmux_name=running.tmux_name,
                label=running.label,
                status=running.status,
            )
            if explicit_slot is None:
                return self._on_running_select(
                    entry,
                    steal_focus=steal_focus,
                    from_double=from_double,
                )
            return self._on_running_select(
                entry,
                steal_focus=steal_focus,
                from_double=from_double,
                slot=slot,
            )

        if running is not None and running.is_legacy:
            self._set_status(
                "Resume refused: the legacy tmux identity is unavailable; "
                "no duplicate was started",
                "error",
            )
            return False

        if registry is not None:
            target_path = self._path_key(session_meta.project.real_path)
            ambiguous_live_placeholder = any(
                candidate.is_placeholder
                and candidate.session_type == session_meta.session_type
                and candidate.placeholder_path is not None
                and self._path_key(candidate.placeholder_path) == target_path
                and session_meta.session_id not in candidate.pre_launch_ids
                and (candidate.is_legacy
                     or self._agent_session_alive(candidate.tmux_name))
                for candidate in self._running.values()
            )
            if ambiguous_live_placeholder:
                msg = (
                    "Resume deferred: a live initializing agent in this "
                    "project could own this session"
                )
                self._set_status(msg, "error")
                return False

        cwd = session_meta.project.real_path
        env: dict[str, str] | None = None
        if session_meta.session_type == "codex":
            cmd = build_codex_resume_command(
                codex_binary=self._config.codex_binary,
                session_id=session_meta.session_id,
                cwd=cwd,
                yolo=self._codex_yolo_enabled(),
            )
            env = self._codex_env()
        else:
            cmd = build_resume_command(
                claude_binary=self._config.claude_binary,
                session_id=session_meta.session_id,
                cwd=cwd,
            )
        label = f"{session_meta.project.display_name}/{session_meta.display_title}"
        if self._launch(session_meta.session_id, cmd, cwd,
                        label, session_meta.project, steal_focus=steal_focus,
                        slot=slot,
                        env=env, login_shell=session_meta.session_type == "codex",
                        session_type=session_meta.session_type):
            self._set_status(f"→ {session_meta.display_title}")
            return True
        return False

    def _new_placeholder_key(self) -> str:
        """Return a fresh ``__new__-<proc-token>-N`` placeholder key.

        Keeps the ``__new__-`` prefix (so ``_Running.is_placeholder`` still
        holds) but namespaces the name with this process's random token, so a
        restart's counter reset to 0 can never reproduce a previous process's
        placeholder tmux name and hijack a surviving orphan session (#11)."""
        self._new_session_counter += 1
        return f"__new__-{self._proc_token}-{self._new_session_counter}"

    def _launch_new_session(self, slot: AgentSlot | None = None) -> None:
        slot = slot or self._agent_workspace().target
        if self._selected_project is None:
            self._set_status("Pick a project first.")
            return
        proj = self._selected_project
        placeholder = self._new_placeholder_key()
        mode = self._active_mode()
        env: dict[str, str] | None = None
        if mode.project_source == ProjectSource.CODEX:
            cmd = build_codex_new_command(
                codex_binary=self._config.codex_binary,
                cwd=proj.real_path,
                yolo=self._codex_yolo_enabled(),
            )
            env = self._codex_env()
        else:
            cmd = build_new_session_command(
                claude_binary=self._config.claude_binary,
                cwd=proj.real_path,
            )
        slot_kwargs = (
            {} if slot is self._primary_slot else {"slot": slot})
        if self._launch(
                placeholder, cmd, proj.real_path,
                f"{proj.display_name}/(new)", proj,
                placeholder_path=proj.real_path, env=env,
                login_shell=mode.login_shell,
                session_type=mode.session_type,
                **slot_kwargs):
            self._set_status(f"→ new session in {proj.display_name}")

    def _on_new_project_submit(self, path: Path) -> None:
        self._close_modal()
        path = path.expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            path = path.resolve()
        except OSError as e:
            msg = str(e)
            self._set_status(msg, "error")
            return
        placeholder = self._new_placeholder_key()
        project: Project | None = None
        env: dict[str, str] | None = None
        mode = self._active_mode()
        session_type = mode.session_type
        login_shell = mode.login_shell
        if mode.project_source == ProjectSource.CODEX:
            project = self._synthesise_codex_project(path)
            cmd = build_codex_new_command(
                codex_binary=self._config.codex_binary,
                cwd=path,
                yolo=self._codex_yolo_enabled(),
            )
            env = self._codex_env()
        else:
            cmd = build_new_session_command(
                claude_binary=self._config.claude_binary, cwd=path)
        target_slot = self._agent_workspace().target
        slot_kwargs = (
            {} if target_slot is self._primary_slot
            else {"slot": target_slot})
        if self._launch(
                placeholder, cmd, path, f"{path.name}/(new)", project,
                placeholder_path=path, env=env,
                login_shell=login_shell,
                session_type=session_type,
                **slot_kwargs):
            self._set_status(f"→ new project: {path}")

    @staticmethod
    def _shellify(argv: list[str], cwd: Path,
                   env: dict[str, str] | None = None,
                   login_shell: bool = False) -> str:
        import shlex
        quoted = " ".join(shlex.quote(a) for a in argv)
        # SECURITY: *env* here must only ever carry NON-secret values (e.g.
        # CODEX_HOME). railmux never injects a provider API key by any channel
        # (not this command string, not tmux ``-e``): a real Codex API key is
        # loaded by the login+interactive shell below (``$SHELL -li``), which
        # sources the user's profile: ``-l`` reads ~/.bash_profile and ``-i``
        # forces ~/.bashrc, covering common setups.
        if login_shell:
            exports = ""
            if env:
                for k, v in env.items():
                    exports += f"export {shlex.quote(k)}={shlex.quote(v)} && "
            return (f"cd {shlex.quote(str(cwd))} && "
                    f"exec $SHELL -li -c {shlex.quote(exports + 'exec ' + quoted)}")
        exports = ""
        if env:
            for k, v in env.items():
                exports += f"export {shlex.quote(k)}={shlex.quote(v)} && "
        return f"{exports}cd {shlex.quote(str(cwd))} && exec {quoted}"

    # --- modals ---

    def _right_pane_open(self) -> bool:
        """True when the tmux right pane exists (railmux sidebar is ~30% width)."""
        pane_id = self._primary_slot.pane_id
        return pane_id is not None and tmux_ctl.pane_alive(pane_id)

    def _open_new_project_modal(self) -> None:
        modal = PathBrowserModal(
            start_path=Path.home(),
            on_submit=self._on_new_project_submit,
            on_cancel=self._close_modal,
            allow_create=True,
        )
        self._show_overlay(modal, width=54, height=60)

    def _open_info_modal(self) -> None:
        """Show info for whichever pane has focus: Project, Sessions, or Running."""
        if self._sidebar.focus_position == 0:
            proj = self._projects_pane.focused_project()
            modal = ProjectInfoModal(project=proj, on_close=self._close_modal)
            self._show_overlay(modal, width=60, height=40)
            return
        if self._sidebar.focus_position == 2:
            # Running pane — show info from the focused running entry.
            from railmux.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if running_walker:
                focus_w, _ = running_walker.get_focus()
                if isinstance(focus_w, _RunningRow):
                    entry = focus_w.entry
                    r = self._by_tmux(entry.tmux_name)
                    project = r.project if r else None
                    label = r.label if r else entry.tmux_name
                    is_placeholder = r.is_placeholder if r else False
                    # Session metadata only exists once the placeholder resolved.
                    sid = r.logical_session_id if r else None
                    stype = r.session_type if r else "claude"
                    session = (self._find_session_meta(sid, project, stype)
                               if sid else None)
                    modal = RunningInfoModal(
                        label=label,
                        tmux_name=entry.tmux_name,
                        project=project,
                        session=session,
                        is_placeholder=is_placeholder,
                        on_close=self._close_modal,
                    )
                    self._show_overlay(modal, width=60, height=35)
            return
        session = self._currently_focused_session_meta()
        if session is not None:
            self._sessions_pane.set_selected_session(session.session_id)
        running_label = None
        if session is not None:
            r = self._by_session_id(session.session_id)
            if r and self._agent_session_alive(r.tmux_name):
                running_label = f"detached as '{r.tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label, on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40,
                           click_outside_to_close=True)

    def _open_help_modal(self) -> None:
        modal = HelpModal(
            on_close=self._close_help_modal,
            provider_label=self._active_mode().label,
            on_ask=self._ask_railmux,
        )
        self._open_full_sidebar_modal(modal, self._close_help_modal)

    @classmethod
    def _help_session_name(cls, mode: AgentMode) -> str:
        """Stable private name for one provider's reusable help session."""
        return f"{cls._HELP_SESSION_PREFIX}{cls._safe_name(mode.key, 16)}"

    @staticmethod
    def _help_session_identity(mode: AgentMode) -> str:
        """Versioned identity so a live helper adopts new safety policy."""
        return f"{mode.key}:{_HELP_POLICY_VERSION}"

    @classmethod
    def _is_help_session_name(cls, tmux_name: str | None) -> bool:
        return bool(
            tmux_name and tmux_name.startswith(cls._HELP_SESSION_PREFIX))

    def _verified_help_session_names(self) -> set[str]:
        """Return only private helpers whose persisted identity we own."""
        candidates = set(getattr(self, "_help_session_names_used", set()))
        server = tmux_ctl.server_snapshot()
        if server is not None:
            candidates.update(
                name for name in server.sessions
                if self._is_help_session_name(name)
            )
        allowed = {
            identity
            for mode in self._modes().modes
            for identity in (mode.key, self._help_session_identity(mode))
        }
        return {
            name for name in candidates
            if (self._is_help_session_name(name)
                and tmux_ctl.show_session_user_option(
                    name, _HELP_SESSION_OPTION) in allowed)
        }

    def _help_command(
        self, mode: AgentMode, workspace: Path,
    ) -> tuple[list[str], dict[str, str] | None, bool]:
        """Build a provider command with conservative support-only defaults."""
        binary = self._configured_mode_binary(mode)
        if mode.project_source == ProjectSource.CODEX:
            command = build_codex_new_command(binary, workspace, yolo=False)
            # Keep the dedicated help workspace out of Codex's normal history,
            # and never inherit Railmux's optional YOLO choice.
            command[1:1] = [
                "-c", 'history.persistence="none"',
                "--sandbox", "read-only",
                # The sandbox remains the enforcement boundary. Never asking
                # means ordinary reads auto-run while writes/network fail
                # directly instead of interrupting the support conversation.
                "--ask-for-approval", "never",
            ]
            return command, self._codex_env(), True
        command = build_new_session_command(binary, workspace)
        command.extend([
            "--safe-mode",
            # Claude has no equivalent OS read-only sandbox. Expose only its
            # built-in read/search tools, then bypass prompts within that
            # closed capability set. Bash and every mutation tool stay absent.
            "--permission-mode", "bypassPermissions",
            "--tools", "Read,Glob,Grep",
            "--append-system-prompt-file",
            # Safe mode disables automatic CLAUDE.md discovery, so load the
            # support/read-only contract explicitly. It directs Claude to the
            # separately refreshed RAILMUX_HELP.md reference.
            str(workspace / "CLAUDE.md"),
        ])
        return command, None, mode.login_shell

    def _ask_railmux(self) -> None:
        """Open or reuse a safe, untracked help agent in the Target pane."""
        mode = self._active_mode()
        tmux_name = self._help_session_name(mode)
        expected_identity = self._help_session_identity(mode)
        already_live = tmux_ctl.session_exists(tmux_name)
        existing_identity = (
            tmux_ctl.show_session_user_option(
                tmux_name, _HELP_SESSION_OPTION)
            if already_live else None
        )
        legacy_policy = already_live and existing_identity == mode.key
        if (already_live and existing_identity not in {
                mode.key, expected_identity}):
            self._set_status(
                "Ask Railmux refused: existing help session identity is unverified",
                "error",
            )
            return
        if ((not already_live or legacy_policy)
                and self._warn_missing_mode_binary(mode)):
            return
        try:
            workspace = materialize_help_workspace()
            command, env, login_shell = self._help_command(mode, workspace)
            shell_cmd = self._shellify(
                command, workspace, env=env, login_shell=login_shell)
        except Exception:
            self._set_status(
                "Ask Railmux unavailable: could not prepare its help workspace",
                "error",
            )
            return

        # Validation and file preparation happen while static Help is still
        # visible. Once this closes, never re-toggle zoom as error recovery.
        self._close_help_modal()
        if legacy_policy:
            # v1 helpers used prompt-heavy permissions. Return an attached
            # pane through the transport transaction before replacing this
            # exact, disposable private session with the current policy.
            if not self._return_agent_before_kill(tmux_name):
                return
            tmux_ctl.kill_session(tmux_name)
            if tmux_ctl.session_exists(tmux_name):
                self._set_status(
                    "Ask Railmux could not upgrade its read-only policy",
                    "error",
                )
                return
            already_live = False
        display_slot = self._agent_workspace().slot_for_agent(tmux_name)
        if display_slot is not None:
            self._set_workspace_target(display_slot.key)
            if (self._agent_workspace().presentation
                    is WorkspacePresentation.COMPACT):
                self._select_workspace_page(
                    WorkspacePage.SECONDARY
                    if display_slot is self._agent_workspace().secondary
                    else WorkspacePage.PRIMARY
                )
            elif display_slot.pane_id is not None:
                tmux_ctl.select_pane(display_slot.pane_id)
                self._set_railmux_focus(False, force_border=True)
            self._set_status(
                f"Ask Railmux with {mode.label}; your previous agent is still running"
            )
            return

        if not already_live:
            ok, error = tmux_ctl.new_detached_session(
                tmux_name, shell_cmd, env=env)
            if not ok:
                self._set_status(
                    f"Ask Railmux failed: {error or 'could not start help session'}",
                    "error",
                )
                return
            marked = (
                tmux_ctl.set_session_user_option(
                    tmux_name, _HELP_SESSION_OPTION, expected_identity)
                and tmux_ctl.show_session_user_option(
                    tmux_name, _HELP_SESSION_OPTION) == expected_identity
            )
            if not marked:
                tmux_ctl.kill_session(tmux_name)
                self._set_status(
                    "Ask Railmux failed: could not verify help session identity",
                    "error",
                )
                return
        used = getattr(self, "_help_session_names_used", None)
        if used is None:
            used = set()
            self._help_session_names_used = used
        used.add(tmux_name)
        if not self._attach_agent_slot(
            self._agent_workspace().target, tmux_name, steal_focus=True,
        ):
            self._set_status(
                "Ask Railmux failed: could not attach the help session",
                "error",
            )
            return
        workspace = self._agent_workspace()
        if workspace.presentation is WorkspacePresentation.COMPACT:
            page = (
                WorkspacePage.SECONDARY
                if workspace.target is workspace.secondary
                else WorkspacePage.PRIMARY
            )
            self._select_workspace_page(page)
        self._set_status(
            f"Ask Railmux with {mode.label}; your previous agent is still running"
        )

    def _open_full_sidebar_modal(
        self,
        modal: urwid.Widget,
        on_close: Callable[[], None],
    ) -> None:
        """Zoom the sidebar and present one terminal-sized settings surface."""
        workspace = self._agent_workspace()
        if workspace.presentation is WorkspacePresentation.COMPACT:
            self._full_sidebar_return_page = workspace.compact_page
            self._full_sidebar_owned_zoom = False
            self._full_sidebar_return_zoom_pane = None
            self._select_workspace_page(WorkspacePage.SIDEBAR)
        else:
            self._full_sidebar_return_page = None
            owner = getattr(self, "_railmux_pane_id", None)
            active = tmux_ctl.active_pane_id(owner) if owner else None
            was_zoomed = self._window_is_zoomed()
            self._full_sidebar_return_zoom_pane = (
                active if was_zoomed and active != owner else None)
            self._full_sidebar_owned_zoom = not (
                was_zoomed and active == owner)
        # In wide presentation, zooming the sidebar instead of shrinking the
        # agent prevents transcript reflow and history corruption.
        if (workspace.presentation is WorkspacePresentation.WIDE
                and self._railmux_pane_id
                and getattr(self, "_full_sidebar_owned_zoom", False)):
            self._zoom_pane(self._railmux_pane_id)
        self._show_overlay(modal, width=60, height=80,
                           click_outside_to_close=True,
                           on_click_outside=on_close,
                           fixed_width=True, fixed_height=True)

    def _close_help_modal(self) -> None:
        self._close_full_sidebar_modal()

    def _close_full_sidebar_modal(self) -> None:
        self._close_modal()
        workspace = self._agent_workspace()
        return_page = getattr(
            self, "_full_sidebar_return_page", None)
        self._full_sidebar_return_page = None
        if workspace.presentation is WorkspacePresentation.COMPACT:
            self._full_sidebar_owned_zoom = False
            self._full_sidebar_return_zoom_pane = None
            if isinstance(return_page, WorkspacePage):
                self._select_workspace_page(return_page)
            return
        # Restore a pre-existing F9 zoom only when the sidebar zoom we installed
        # is still the current owner. If F9 changed state while the modal was
        # open, that newer explicit action wins.
        owned = getattr(self, "_full_sidebar_owned_zoom", False)
        return_zoom = getattr(
            self, "_full_sidebar_return_zoom_pane", None)
        self._full_sidebar_owned_zoom = False
        self._full_sidebar_return_zoom_pane = None
        owner = getattr(self, "_railmux_pane_id", None)
        active = tmux_ctl.active_pane_id(owner) if owner else None
        if (owned and owner is not None and active == owner
                and self._window_is_zoomed()
                and tmux_ctl.toggle_pane_zoom(owner)
                and return_zoom is not None
                and tmux_ctl.pane_alive(return_zoom)):
            tmux_ctl.select_pane(return_zoom)
            tmux_ctl.toggle_pane_zoom(return_zoom)

    def _open_options_modal(self) -> None:
        wide_profile_at_open = (
            self._capture_layout_profile("always")
            if self._agent_workspace().presentation
            is WorkspacePresentation.WIDE
            else None
        )

        def set_layout(policy: str) -> bool:
            profile = None
            if policy == "always":
                # Options describes future retention; merely choosing Always
                # in compact mode must not overwrite a good saved ratio with
                # hidden-pane geometry. A visible wide layout remains an
                # intentional snapshot, matching the established Options UX.
                if (self._agent_workspace().presentation
                        is WorkspacePresentation.WIDE):
                    # Cache before _open_full_sidebar_modal zooms Railmux.
                    # Sampling inside the callback would store ~100% sidebar.
                    profile = wide_profile_at_open
                else:
                    existing = getattr(self, "_layout_profile", None)
                    if existing is not None:
                        profile = replace(existing, scope="always")
            if not self._settings.set_layout_save_policy(policy, profile):
                self._set_status(
                    "Could not save layout option; setting unchanged.",
                    "error",
                )
                return False
            self._layout_profile = profile
            self._set_status(
                f"Layout retention: {self._policy_label(policy)}.")
            return True

        def set_yolo(policy: str) -> bool:
            if not self._settings.set_codex_yolo_policy(policy):
                self._set_status(
                    "Could not save Codex auto-run option; setting unchanged.",
                    "error",
                )
                return False
            self._codex_yolo_runtime = False
            self._codex_yolo_prompt_handled = policy != "ask"
            self._set_status(
                f"Codex auto-run: {self._policy_label(policy)}.")
            return True

        def set_update(policy: str) -> bool:
            if not self._settings.set_update_policy(policy):
                self._set_status(
                    "Could not save Railmux update option; setting unchanged.",
                    "error",
                )
                return False
            self._set_status(
                f"Railmux updates: {self._policy_label(policy)}.")
            return True

        modal = OptionsModal(
            layout_policy=self._settings.layout_save_policy,
            yolo_policy=self._settings.codex_yolo_policy,
            update_policy=self._settings.update_policy,
            on_layout_policy=set_layout,
            on_yolo_policy=set_yolo,
            on_update_policy=set_update,
            on_close=self._close_options_modal,
        )
        self._open_full_sidebar_modal(modal, self._close_options_modal)

    def _on_button_bar_expanded(self, expanded: bool) -> None:
        """Take More's second row from Running, not every weighted section."""
        self._sidebar.set_bottom_row_debt(1 if expanded else 0)

    def _close_options_modal(self) -> None:
        self._close_full_sidebar_modal()

    @staticmethod
    def _policy_label(policy: str) -> str:
        return {
            "always": "Always",
            "ask": "Ask every time",
            "never": "No",
        }[policy]

    def _open_quit_confirm(self) -> None:
        self._save_state()
        session = tmux_ctl.current_session_name()
        attached = (
            tmux_ctl.session_attached_count(session) if session else None)
        modal = QuitConfirmModal(
            on_confirm=self._confirm_quit,
            on_soft_quit=self._soft_quit,
            on_cancel=self._close_modal,
            running_count=len(self._running),
            attached_clients=attached or 1,
        )
        self._show_quit_confirm(modal)

    # --- project shortcut: terminal ---

    def _active_project(self) -> Project | None:
        """Project to act on for the terminal shortcut.

        Prefer the focused project in the Projects pane; fall back to the
        currently-selected (loaded-into-Sessions) project.
        """
        if self._sidebar.focus_position == 0:
            focused = self._projects_pane.focused_project()
            if focused is not None:
                return focused
        return self._selected_project

    def _open_terminal_for_active_project(self) -> None:
        import os
        import shlex
        import subprocess as _sp
        proj = self._active_project()
        if proj is None:
            self._set_status("no project focused/selected")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(proj.real_path))} && exec {shlex.quote(shell)}"
        # Split below the explicit active agent. If no agent pane exists, tmux
        # falls back to the current pane as before.
        pane_id = self._sync_target_slot_from_tmux().pane_id
        target = pane_id if (pane_id and tmux_ctl.pane_alive(pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        # Auto-close the pane when the shell exits (default, but be explicit).
        _sp.run(
            ["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        tmux_ctl.select_pane(new_pane)
        self._set_railmux_focus(False)
        self._set_status(f"terminal: {proj.display_name}  (Ctrl-B then arrow = move panes)")

    def _on_detach(self) -> None:
        """Detach from Railmux while keeping every agent session alive."""
        import subprocess as _sp
        session = tmux_ctl.current_session_name()
        attached = (
            tmux_ctl.session_attached_count(session) if session else None)
        if attached != 1:
            message = (
                "Multiple terminals are attached; use Ctrl-B d to detach "
                "only this terminal."
                if attached is not None and attached > 1
                else "Could not verify a single attached terminal; use "
                "Ctrl-B d to detach this terminal safely."
            )
            self._set_status(
                message,
                "warn",
            )
            return
        _sp.run(["tmux", "detach-client"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

    def _confirm_quit(self) -> None:
        """Request a hard quit, optionally recording pane proportions."""
        self._request_exit(soft=False)

    def _soft_quit(self) -> None:
        """Request a soft quit, optionally recording pane proportions."""
        self._request_exit(soft=True)

    def _request_exit(self, *, soft: bool) -> None:
        """Resolve layout persistence before committing exit semantics."""
        current = self._capture_layout_profile("always")
        if current is None:
            self._commit_exit(soft=soft)
            return
        if not getattr(self, "_layout_geometry_user_owned", False):
            # Do not prompt merely because a default/restored layout exists.
            # An unconsumed one-shot profile (for example after a small-screen
            # fallback) also remains available for a future launch.
            self._commit_exit(soft=soft)
            return
        policy = self._settings.layout_save_policy
        if policy == "never":
            self._commit_exit(soft=soft)
            return
        if policy == "always":
            can_refresh = (
                not getattr(self, "_layout_profile_fallback", False)
                or getattr(self, "_layout_geometry_user_owned", False)
            )
            if can_refresh:
                if not self._settings.set_layout_save_policy(
                    "always", current,
                ):
                    self._set_status(
                        "Could not update the saved layout; exiting with the "
                        "previous preference.",
                        "error",
                    )
                else:
                    self._layout_profile = current
            self._commit_exit(soft=soft)
            return

        def save(scope: str) -> None:
            profile = LayoutProfile(
                scope=scope,
                layout=current.layout,
                sidebar_permille=current.sidebar_permille,
                primary_permille=current.primary_permille,
            )
            policy = "always" if scope == "always" else "ask"
            if not self._settings.set_layout_save_policy(policy, profile):
                self._set_status(
                    "Could not save the layout preference; exiting without it.",
                    "error",
                )
            else:
                self._layout_profile = profile
            self._commit_exit(soft=soft)

        def no() -> None:
            self._commit_exit(soft=soft)

        def never() -> None:
            if not self._settings.set_layout_save_policy("never"):
                self._set_status(
                    "Could not save the Never layout preference; "
                    "skipping this time only.",
                    "error",
                )
            else:
                self._layout_profile = None
            self._commit_exit(soft=soft)

        def back() -> None:
            self._close_modal()
            self._open_quit_confirm()

        modal = LayoutSaveModal(
            on_always=lambda: save("always"),
            on_this_time=lambda: save("once"),
            on_no=no,
            on_never=never,
            on_back=back,
        )
        self._show_preferred_height_modal(modal, width=56)

    def _commit_exit(self, *, soft: bool) -> None:
        """Commit the already-confirmed exit at the final modal boundary."""
        if not soft:
            self._begin_exit(soft=False)
            return
        # Save again at the point of commitment.  The confirmation modal may
        # have been open for a while and placeholder bindings can resolve in
        # the meantime.
        self._save_state(portable_right=True)
        self._publish_managed_restart_handoff()
        identity = getattr(self, "_restart_identity", None)
        if identity is not None:
            tmux_health.record_soft_exit(
                server_pid=identity.server_pid,
                session_id=identity.session_id,
            )

        self._begin_exit(soft=True)

    def _begin_exit(self, *, soft: bool) -> None:
        """Paint progress, perform core teardown, then leave Urwid.

        Urwid restores the alternate screen before ``MainLoop.run`` returns.
        Core cleanup must therefore happen inside the loop if the user is to
        see an intentional exit state instead of a blank sidebar.  ``run``'s
        ``finally`` remains the unconditional retry and outer-session cleanup.
        """
        if getattr(self, "_exit_in_progress", False):
            return
        self._exit_in_progress = True
        if soft:
            self._soft_quit_flag = True
        self._close_modal()
        self._show_overlay(
            ExitProgressModal(len(self._running), soft=soft),
            width=44,
            height=7,
            fixed_width=True,
            fixed_height=True,
        )
        if self._loop is not None:
            try:
                self._loop.draw_screen()
            except Exception:
                pass
        try:
            self._teardown_tmux(defer_outer=True)
        except Exception:
            # ``run``'s finally retries any unfinished idempotent phase.
            pass
        # Closing the display pane may resize this pane. A SIGWINCH is not
        # guaranteed to be processed while teardown blocks, so this redraw is
        # deliberately best-effort; correctness relies on the draw above.
        if self._loop is not None:
            try:
                self._loop.screen_size = None
                self._loop.draw_screen()
            except Exception:
                pass
        raise urwid.ExitMainLoop()

    # --- state file (for restart-after-soft-quit) --------------------------

    def _state_path(self) -> Path | None:
        identity = getattr(self, "_restart_identity", None)
        return (
            restart_state.instance_state_path(identity)
            if identity is not None else None
        )

    def _managed_restart_context(self) -> bool:
        """Whether this pane is the CLI-owned, uniquely named Railmux UI."""
        if not getattr(self, "_auto_launched", False):
            return False
        identity = getattr(self, "_restart_identity", None)
        if identity is None:
            return False
        topology = tmux_ctl.session_topology(identity.session_id)
        return topology is not None and topology.session_name == "railmux"

    def _publish_managed_restart_handoff(self) -> bool:
        """Make a successfully written local snapshot discoverable next run."""
        identity = getattr(self, "_restart_identity", None)
        path = self._state_path()
        if (identity is None or path is None
                or not self._managed_restart_context()):
            return False
        saved = restart_state.decode_instance(
            restart_state.read_json_object(path), identity)
        if saved is None:
            return False
        return restart_state.write_managed_handoff(identity)

    @staticmethod
    def _portable_state_path() -> Path:
        return restart_state.portable_state_path()

    def _view_state_data(self) -> dict:
        data = {"mode": self._current_mode_key()}
        if self._selected_project is not None:
            data["project"] = self._selected_project.encoded_name
        session = self._currently_focused_session_meta()
        if session is not None:
            data["session"] = session.session_id
        projects_pane = getattr(self, "_projects_pane", None)
        sessions_pane = getattr(self, "_sessions_pane", None)
        if projects_pane is not None and projects_pane.filter_text:
            data["project_filter"] = projects_pane.filter_text
        if sessions_pane is not None and sessions_pane.filter_text:
            data["session_filter"] = sessions_pane.filter_text
        running_pane = getattr(self, "_running_pane", None)
        if running_pane is not None and running_pane.filter_text:
            data["running_filter"] = running_pane.filter_text
        return data

    def _portable_right_state_data(self) -> dict:
        """Return a stable display wish with no node-local tmux authority."""
        slot = self._agent_workspace().target
        session_id = slot.active_session_id
        mode_key = slot.mode_key
        if not session_id or session_id.startswith("__new__-") or not mode_key:
            return {}
        try:
            self._modes().get(mode_key)
        except KeyError:
            return {}
        if slot.in_history_mode:
            kind = "preview"
        elif slot.agent_tmux_name is not None:
            running = self._by_tmux(slot.agent_tmux_name)
            if (running is None or running.is_placeholder
                    or running.logical_session_id != session_id):
                return {}
            kind = "agent"
        else:
            return {}
        data = {
            "right_kind": kind,
            "right_mode": mode_key,
            "right_session": session_id,
        }
        if slot.project_key:
            data["right_project"] = slot.project_key
        return data

    def _slot_recovery_state_data(self, slot: AgentSlot) -> dict:
        """Exact-owner content wish for one display slot."""
        # Ask Railmux is a reusable auxiliary session, not a provider-session
        # recovery authority. After a soft restart leave this slot empty; the
        # user can reconnect from Help without polluting Running or bindings.
        if self._is_help_session_name(slot.agent_tmux_name):
            return {"kind": "empty"}
        if slot.in_history_mode:
            data: dict = {"kind": "preview"}
        elif slot.agent_tmux_name is not None:
            data = {"kind": "agent", "tmux": slot.agent_tmux_name}
        else:
            data = {"kind": "empty"}
        if slot.active_session_id is not None:
            data["session"] = slot.active_session_id
        mode_key = slot.mode_key
        if mode_key is None and slot.in_history_mode:
            mode_key = self._current_mode_key()
        if mode_key is not None:
            data["mode"] = mode_key
        project_key = slot.project_key
        selected = getattr(self, "_selected_project", None)
        if project_key is None and slot.in_history_mode and selected is not None:
            project_key = selected.encoded_name
        if project_key is not None:
            data["project"] = project_key
        restore = slot.restore_state
        if slot.in_history_mode and restore is not None:
            saved_restore = {"kind": restore.kind}
            if restore.tmux_name is not None:
                saved_restore["tmux"] = restore.tmux_name
            data["restore"] = saved_restore
        return data

    def _workspace_recovery_state_data(self) -> dict:
        """Full local workspace wish; never written to portable state."""
        workspace = self._agent_workspace()
        focus = (
            "sidebar"
            if getattr(self, "_railmux_has_focus", True)
            else workspace.target_slot_key
        )
        data = {
            "version": 1,
            "layout": workspace.layout.value,
            "target": workspace.target_slot_key,
            "focus": focus,
            "slots": {
                AgentWorkspace.PRIMARY: self._slot_recovery_state_data(
                    workspace.primary),
                AgentWorkspace.SECONDARY: self._slot_recovery_state_data(
                    workspace.secondary),
            },
        }
        if workspace.collapsed_secondary_agent is not None:
            running = self._by_tmux(workspace.collapsed_secondary_agent)
            mode = (
                self._modes().for_tmux_name(running.tmux_name)
                if running is not None else None
            )
            if running is not None and mode is not None:
                data["collapsed_secondary"] = {
                    "tmux": running.tmux_name,
                    "session": running.logical_session_id,
                    "mode": mode.key,
                }
        return data

    def _recovery_state_data(self) -> dict:
        data: dict = {}
        slot = self._agent_workspace().target
        if slot.in_history_mode:
            data["right_kind"] = "preview"
            if slot.active_session_id is not None:
                data["right_session"] = slot.active_session_id
        elif self._is_help_session_name(slot.agent_tmux_name):
            data["right_kind"] = "empty"
        elif slot.agent_tmux_name is not None:
            data["right_kind"] = "agent"
            data["right_tmux"] = slot.agent_tmux_name
        else:
            data["right_kind"] = "empty"
        data.update(self._portable_right_state_data())
        data["workspace"] = self._workspace_recovery_state_data()

        bindings: list[dict] = []
        for running in self._running.values():
            item = self._running_binding_data(
                running, include_launch_context=True)
            if item is None:
                continue
            bindings.append(item)
        if bindings:
            data["running_bindings_version"] = 1
            data["running_bindings"] = bindings
        return data

    def _save_state(self, *, portable_right: bool = False) -> None:
        """Persist portable view state and exact-owner recovery independently."""
        if not getattr(self, "_railmux_has_focus", True):
            # A direct P1/P2 mouse move may precede the next periodic focus
            # sync. Snapshot the real tmux owner before serializing Target and
            # Focused pane; failure conservatively retains the last known slot.
            self._sync_target_slot_from_tmux()
        view_data = self._view_state_data()
        portable_view_data = dict(view_data)
        if portable_right:
            portable_view_data.update(self._portable_right_state_data())
        portable_view = restart_state.build_view(portable_view_data)
        local_view = restart_state.build_view(view_data)
        portable = {
            "schema_version": restart_state.SCHEMA_VERSION,
            "kind": "portable",
            "view": portable_view,
        }
        if getattr(self, "_portable_state_writable", True):
            restart_state.write_portable(portable, self._portable_state_path())

        identity = getattr(self, "_restart_identity", None)
        path = self._state_path()
        if identity is None or path is None:
            return
        local = {
            "schema_version": restart_state.SCHEMA_VERSION,
            "kind": "instance",
            "owner": identity.to_json(),
            # Local view takes precedence over shared portable defaults, so two
            # simultaneous windows each restore their own display and focus.
            "view": local_view,
            "recovery": self._recovery_state_data(),
        }
        if getattr(self, "_local_state_writable", True):
            restart_state.write_instance(identity, local, path)
        restart_state.cleanup_stale_instances(identity)

    def _load_state(self) -> dict | None:
        """Merge portable defaults with this exact tmux pane's local state."""
        portable_path = self._portable_state_path()
        portable_raw = restart_state.read_json_object(portable_path)
        portable = restart_state.decode_portable(portable_raw)
        self._portable_state_writable = not (
            isinstance(portable_raw, dict)
            and isinstance(portable_raw.get("schema_version"), int)
            and not isinstance(portable_raw.get("schema_version"), bool)
            and portable_raw["schema_version"] > restart_state.SCHEMA_VERSION
        )
        if portable is None and not portable_path.exists():
            # The legacy fixed file has no owner identity. Migrate only stable
            # view data; never import right-pane or running-binding authority.
            portable = restart_state.legacy_portable_view(
                restart_state.read_json_object(restart_state.legacy_state_path()))
            if portable is not None:
                restart_state.write_portable({
                    "schema_version": restart_state.SCHEMA_VERSION,
                    "kind": "portable",
                    "view": restart_state.build_view(portable),
                }, portable_path)

        local = None
        identity = getattr(self, "_restart_identity", None)
        path = self._state_path()
        if identity is not None and path is not None:
            local_raw = restart_state.read_json_object(path)
            local = restart_state.decode_instance(local_raw, identity)
            self._local_state_writable = not (
                isinstance(local_raw, dict)
                and isinstance(local_raw.get("schema_version"), int)
                and not isinstance(local_raw.get("schema_version"), bool)
                and local_raw["schema_version"] > restart_state.SCHEMA_VERSION
            )
            if local is not None:
                self._loaded_restart_source = identity
                self._loaded_restart_state_path = path
            elif self._managed_restart_context():
                source = restart_state.read_managed_handoff(identity)
                if source is not None:
                    source_path = restart_state.instance_state_path(source)
                    source_raw = restart_state.read_json_object(source_path)
                    local = restart_state.decode_instance(source_raw, source)
                    if local is not None:
                        self._loaded_restart_source = source
                        self._loaded_restart_state_path = source_path
            restart_state.cleanup_stale_instances(identity)
        if portable is None and local is None:
            return None
        # Local carries a complete view snapshot. Prefer it wholesale: a
        # missing optional key must mean "unset for this instance", not inherit
        # the last portable value written by another Railmux window.
        return local if local is not None else portable

    def _mode_key_from_state(self, state: dict | None) -> str:
        """Resolve new registry state, then the <=0.1.1 Codex boolean."""
        registry = self._modes()
        if not state:
            return registry.default_key
        stored = state.get("mode")
        if isinstance(stored, str):
            return registry.resolve(stored).key
        if state.get("codex_mode"):
            return registry.resolve(CODEX_MODE.key).key
        return registry.default_key

    def _right_mode_from_state(self, state: dict) -> AgentMode | None:
        stored = state.get("right_mode")
        if stored is None:
            return self._active_mode()
        if not isinstance(stored, str):
            return None
        try:
            return self._modes().get(stored)
        except KeyError:
            return None

    def _right_project_from_state(self, state: dict) -> Project | None:
        project_key = state.get("right_project")
        candidates = list(getattr(self, "_project_snapshot", None) or [])
        selected = self._selected_project
        if selected is not None and all(
                item.encoded_name != selected.encoded_name for item in candidates):
            candidates.append(selected)
        if isinstance(project_key, str):
            return next(
                (item for item in candidates
                 if item.encoded_name == project_key),
                None,
            )
        return selected

    def _restore_preview_target(
        self, state: dict, slot: AgentSlot | None = None,
    ) -> bool:
        session_id = state.get("right_session")
        mode = self._right_mode_from_state(state)
        if not isinstance(session_id, str) or mode is None:
            return False
        if mode.project_source == ProjectSource.CODEX:
            meta = self._codex_index.get(session_id)
        else:
            project = self._right_project_from_state(state)
            if project is None:
                return False
            meta = self._session_cache.get(project, session_id)
        slot = slot or self._agent_workspace().primary
        if meta is None or meta.session_type != mode.session_type:
            return False
        shown = (
            self._show_transcript(
                meta.jsonl_path,
                session_type=meta.session_type,
            )
            if slot is self._agent_workspace().primary
            else self._show_transcript(
                meta.jsonl_path,
                session_type=meta.session_type,
                slot=slot,
            )
        )
        if not shown:
            return False
        slot.in_history_mode = True
        if slot is self._agent_workspace().primary:
            self._set_active_target(
                meta.session_id,
                None,
                mode_key=mode.key,
                project_key=meta.project.encoded_name,
            )
        else:
            self._set_slot_active_target(
                slot,
                meta.session_id,
                None,
                mode_key=mode.key,
                project_key=meta.project.encoded_name,
            )
        return True

    @staticmethod
    def _workspace_slot_as_right_state(saved: dict) -> dict:
        """Translate one validated workspace slot into existing restore keys."""
        state = {"right_kind": saved["kind"]}
        for source, target in (
            ("tmux", "right_tmux"),
            ("session", "right_session"),
            ("mode", "right_mode"),
            ("project", "right_project"),
        ):
            value = saved.get(source)
            if isinstance(value, str):
                state[target] = value
        return state

    def _validated_saved_agent(
        self, state: dict,
    ) -> tuple[str, _Running] | None:
        """Resolve one persisted agent wish through current exact discovery."""
        tmux_name = state.get("right_tmux")
        session_id = state.get("right_session")
        running = (
            self._by_session_id(session_id)
            if isinstance(session_id, str) else None
        )
        if running is not None:
            tmux_name = running.tmux_name
        if not (isinstance(tmux_name, str)
                and (self._agent_session_alive(tmux_name)
                     if running is not None
                     else tmux_ctl.session_exists(tmux_name))):
            return None
        running = running or self._by_tmux(tmux_name)
        if running is None:
            # A validated binding can redirect a historical duplicate to the
            # canonical writer selected during startup discovery.
            for raw in state.get("running_bindings", []):
                if (isinstance(raw, dict)
                        and raw.get("tmux_name") == tmux_name
                        and isinstance(raw.get("key"), str)):
                    running = self._running.get(raw["key"])
                    if running is not None:
                        tmux_name = running.tmux_name
                    break
        if running is None:
            self._set_status(
                "Restore deferred: previous agent could not be validated",
                "error",
            )
            return None
        actual_mode = self._modes().for_tmux_name(running.tmux_name)
        expected_mode = (
            self._right_mode_from_state(state)
            if "right_mode" in state else actual_mode
        )
        if (expected_mode is None or actual_mode is None
                or expected_mode.key != actual_mode.key):
            self._set_status(
                "Restore deferred: previous provider identity changed", "error")
            return None
        if (running.orphan is not None
                and not self._running_action_valid(running)):
            self._set_status(
                "Restore deferred: marked tmux identity changed", "error")
            return None
        return tmux_name, running

    def _restore_agent_target(self, state: dict, slot: AgentSlot) -> bool:
        """Attach one saved agent only after current discovery validates it."""
        validated = self._validated_saved_agent(state)
        if validated is None:
            return False
        tmux_name, _running = validated
        ok = (
            self._attach_in_right_pane(tmux_name, steal_focus=False)
            if slot is self._agent_workspace().primary
            else self._attach_agent_slot(slot, tmux_name, steal_focus=False)
        )
        if not ok:
            self._set_status(
                "Restore failed: could not re-attach to previous agent session",
                "error",
            )
        return ok

    def _restore_workspace_slot(
        self,
        slot: AgentSlot,
        saved: dict,
        running_bindings: list | None = None,
    ) -> bool:
        state = self._workspace_slot_as_right_state(saved)
        state["running_bindings"] = running_bindings or []
        kind = saved["kind"]
        if kind == "agent":
            return self._restore_agent_target(state, slot)
        if kind == "preview":
            restored = self._restore_preview_target(state, slot)
            if restored:
                raw_restore = saved.get("restore")
                if isinstance(raw_restore, dict):
                    restore_kind = raw_restore["kind"]
                    restore_tmux = raw_restore.get("tmux")
                    if restore_kind == "agent":
                        restore_tmux = self._validated_preview_restore_agent(
                            restore_tmux)
                        restore_kind = "agent" if restore_tmux else "empty"
                    slot.restore_state = SlotRestoreState(
                        restore_kind, restore_tmux)
            return restored
        # The caller creates the owning pane before restoring empty content.
        slot.clear_content()
        return True

    def _validated_preview_restore_agent(
        self, tmux_name: object, *, already_alive: bool = False,
    ) -> str | None:
        """Return a represented exact agent name safe for preview rollback."""
        if not isinstance(tmux_name, str):
            return None
        running = self._by_tmux(tmux_name)
        if running is None:
            return None
        if not (already_alive or self._agent_session_alive(tmux_name)):
            return None
        if running.orphan is not None and not self._running_action_valid(running):
            return None
        return tmux_name

    def _restore_workspace(self, state: dict, saved: dict) -> bool:
        """Rebuild an exact-owner two-slot workspace, degrading truthfully."""
        workspace = self._agent_workspace()
        transport = self._display_transport()
        slots = saved["slots"]

        def live_represented(raw: object) -> str | None:
            if not isinstance(raw, dict):
                return None
            candidate = self._workspace_slot_as_right_state({
                **raw, "kind": "agent",
            })
            candidate["running_bindings"] = state.get("running_bindings", [])
            validated = self._validated_saved_agent(candidate)
            return validated[0] if validated is not None else None

        primary_saved = self._workspace_slot_as_right_state(
            slots[AgentWorkspace.PRIMARY])
        primary_saved["running_bindings"] = state.get("running_bindings", [])

        primary_kind = slots[AgentWorkspace.PRIMARY]["kind"]
        if primary_kind == "agent":
            primary_ok = self._restore_agent_target(
                primary_saved, workspace.primary)
        elif primary_kind == "preview":
            primary_ok = self._restore_workspace_slot(
                workspace.primary, slots[AgentWorkspace.PRIMARY])
        else:
            primary_ok = transport.create_primary()
            if primary_ok:
                workspace.primary.clear_content()
        content_ok = primary_ok
        if not primary_ok:
            primary_ok = (
                transport.reset_slot(workspace.primary)
                if (workspace.primary.pane_id is not None
                    and tmux_ctl.pane_alive(workspace.primary.pane_id))
                else transport.create_primary()
            )
        if not primary_ok or workspace.primary.pane_id is None:
            workspace.layout = WorkspaceLayout.SINGLE
            self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            self._set_railmux_focus(True, force_border=True)
            return False

        requested = WorkspaceLayout(saved["layout"])
        self._resize_sidebar_for_layout(requested)
        secondary_ok = True
        if requested is not WorkspaceLayout.SINGLE:
            region = self._agent_region_size()
            if (region is not None
                    and not self._layout_fits(region, requested)):
                secondary_ok = False
            elif not transport.create_secondary(requested):
                secondary_ok = False
            elif not self._restore_workspace_slot(
                    workspace.secondary,
                    slots[AgentWorkspace.SECONDARY],
                    state.get("running_bindings")):
                # Keep the user's chosen layout visible and truthful; the
                # failed content remains represented in Running.
                if not transport.reset_slot(workspace.secondary):
                    transport.close_slot(workspace.secondary)
                    workspace.layout = WorkspaceLayout.SINGLE
                secondary_ok = False
        else:
            workspace.layout = WorkspaceLayout.SINGLE

        if requested is not WorkspaceLayout.SINGLE and not workspace.secondary.is_open:
            workspace.layout = WorkspaceLayout.SINGLE
        if workspace.layout is WorkspaceLayout.SINGLE:
            self._resize_sidebar_for_layout(WorkspaceLayout.SINGLE)
            self._set_workspace_target(AgentWorkspace.PRIMARY)
            collapsed = live_represented(saved.get("collapsed_secondary"))
            if collapsed is None and requested is not WorkspaceLayout.SINGLE:
                secondary_saved = slots[AgentWorkspace.SECONDARY]
                if secondary_saved["kind"] == "agent":
                    collapsed = live_represented(secondary_saved)
            if collapsed is not None:
                workspace.collapsed_secondary_agent = collapsed
        else:
            self._resize_sidebar_for_layout(workspace.layout)
            self._set_workspace_target(saved["target"])

        target = workspace.target
        self._paint_slot_active_target(
            target, target.active_session_id, target.agent_tmux_name)
        focus_key = saved["focus"]
        focus_slot = (
            workspace.secondary
            if focus_key == AgentWorkspace.SECONDARY
            else workspace.primary
        )
        if (focus_key != "sidebar" and focus_slot.pane_id is not None
                and tmux_ctl.select_pane(focus_slot.pane_id)):
            self._set_workspace_target(focus_slot.key)
            self._set_railmux_focus(False, force_border=True)
        else:
            railmux_pane = getattr(self, "_railmux_pane_id", None)
            if railmux_pane is not None:
                tmux_ctl.select_pane(railmux_pane)
            self._set_railmux_focus(True, force_border=True)
        self._apply_layout_profile(allow_create=True)
        self._install_tmux_bindings()
        return content_ok and secondary_ok

    def _restore_right_pane(self, state: dict) -> bool:
        """Re-open the right pane to its state at soft-quit time."""
        workspace = state.get("workspace")
        if isinstance(workspace, dict):
            self._restoring_workspace = True
            try:
                return self._restore_workspace(state, workspace)
            finally:
                self._restoring_workspace = False
        kind = state.get("right_kind")
        if kind in ("agent", "claude"):  # "claude" written by <=0.1.1
            session_id = state.get("right_session")
            exact_tmux = state.get("right_tmux")
            exact_live = (
                isinstance(exact_tmux, str)
                and tmux_ctl.session_exists(exact_tmux)
            )
            if self._restore_agent_target(state, self._primary_slot):
                return True
            if exact_live:
                return False
            # A portable restart wish never authorizes process adoption. If its
            # stable session is not live on this tmux server, reopen the same
            # transcript read-only instead of resuming or launching anything.
            if isinstance(session_id, str):
                return self._restore_preview_target(state)
            return True
        elif kind == "preview":
            return self._restore_preview_target(state)
        return True

    @staticmethod
    def _path_key(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path

    @staticmethod
    def _placeholder_tmux_key(name: str, mode: AgentMode) -> str | None:
        """Reverse a generated placeholder tmux name to its registry key.

        ``_safe_name('__new__-abc-1')`` is ``'new---abc-1'`` because the two
        underscores become dashes and leading dashes are stripped.  The old
        recovery check looked for the impossible literal ``'__new__-'`` in a
        tmux name, so it neither identified nor recovered real placeholders.
        """
        if not name.startswith(mode.tmux_prefix):
            return None
        remainder = name[len(mode.tmux_prefix):]
        if not remainder.startswith("new---"):
            return None
        key = "__new__-" + remainder[len("new---"):]
        return key if App._safe_name(key, len(key)) == remainder else None

    @staticmethod
    def _legacy_placeholder_name(name: str, mode: AgentMode) -> bool:
        """Whether *name* has one of Railmux's historical new-session shapes."""
        import re
        if not name.startswith(mode.tmux_prefix):
            return False
        remainder = name[len(mode.tmux_prefix):]
        return re.fullmatch(
            r"new---(?:[0-9]+|[0-9a-f]{6}-[1-9][0-9]*)",
            remainder,
        ) is not None

    def _is_legacy_new_session_command(
        self,
        raw: str,
        mode: AgentMode,
        cwd: Path,
    ) -> bool:
        """Validate the strict launch grammar used before identity markers.

        tmux quotes ``pane_start_command`` as one shell argument.  Parsing both
        layers lets us compare tokens without executing or substring-matching
        shell text.  Only a provider's configured binary and the exact live cwd
        are accepted; resume commands and additional shell operations fail.
        """
        import shlex
        try:
            outer = shlex.split(raw)
            command = shlex.split(outer[0]) if len(outer) == 1 else outer
        except ValueError:
            return False
        expected_binary = str(getattr(
            self._config, mode.binary_config_attr, ""))
        cwd_key = self._path_key(cwd)
        if not expected_binary or len(command) < 5:
            return False
        try:
            command_cwd = self._path_key(Path(command[1]))
        except (TypeError, ValueError):
            return False
        if (command[:1] != ["cd"] or not Path(command[1]).is_absolute()
                or command_cwd != cwd_key
                or command[2:4] != ["&&", "exec"]):
            return False
        if mode.project_source == ProjectSource.CLAUDE:
            return command[4:] == [expected_binary]
        if command[4:7] != ["$SHELL", "-li", "-c"] or len(command) != 8:
            return False
        try:
            inner = shlex.split(command[7])
        except ValueError:
            return False
        if len(inner) >= 3 and inner[0] == "export" and inner[2] == "&&":
            key, separator, value = inner[1].partition("=")
            if (key != "CODEX_HOME" or not separator or not value
                    or not Path(value).is_absolute()):
                return False
            inner = inner[3:]
        expected = ["exec", expected_binary, "-C", str(cwd)]
        return inner in (
            expected,
            [*expected, "--dangerously-bypass-approvals-and-sandbox"],
        )

    def _migrate_legacy_v2_marker(
        self,
        *,
        name: str,
        cwd: Path,
        created: int,
        session_id: str,
        pane_id: str,
        mode: AgentMode,
        resolved_session_id: str | None = None,
    ) -> orphan_marker.Marker | None:
        """Install a v2 marker on one strictly validated legacy live session.

        This is the direct-upgrade boundary for live new-session processes
        created by Railmux 0.1.3 and earlier. It can be retired only when those
        releases are no longer supported as an in-place upgrade source.
        """
        placeholder_key = self._placeholder_tmux_key(name, mode)
        if (placeholder_key is None
                or not self._legacy_placeholder_name(name, mode)):
            return None
        raw = tmux_ctl.detached_single_pane_start_command(
            name, session_id=session_id, pane_id=pane_id)
        if raw is None or not self._is_legacy_new_session_command(
                raw, mode, cwd):
            return None
        owner = getattr(self, "_restart_identity", None)
        if owner is None or created <= 0:
            return None
        marker = orphan_marker.Marker(
            mode_key=mode.key,
            placeholder_key=placeholder_key,
            tmux_name=name,
            tmux_session_id=session_id,
            tmux_pane_id=pane_id,
            owner=owner,
            cwd=self._path_key(cwd),
            created_at=float(created),
            creation_token=uuid.uuid4().hex,
            phase=("resolved" if resolved_session_id is not None
                   else "unresolved"),
            session_id=resolved_session_id,
        )
        # Marker-first migration mirrors new-session launch. If persistence or
        # readback fails, leave the legacy process untouched and unclaimed.
        if not self._write_orphan_marker(marker):
            return None
        return marker

    def _legacy_unresolved_running(
        self,
        *,
        name: str,
        cwd: Path,
        created: int,
        session_id: str,
        pane_id: str,
        mode: AgentMode,
        project: Project,
    ) -> _Running | None:
        """Conservatively adopt a live agent created before recovery markers."""
        marker = self._migrate_legacy_v2_marker(
            name=name,
            cwd=cwd,
            created=created,
            session_id=session_id,
            pane_id=pane_id,
            mode=mode,
        )
        if marker is None:
            return None
        return _Running(
            key=marker.placeholder_key,
            tmux_name=name,
            label=f"{project.display_name}/(recovering)",
            project=project,
            placeholder_path=cwd,
            created_at=float(created),
            # A pre-marker process has no pre-launch snapshot. Keep it visible
            # and operable, but never guess which same-cwd transcript it owns.
            allow_heuristic_resolution=False,
            status="idle",
            session_type=mode.session_type,
            orphan=marker,
        )

    def _valid_running_binding(
        self,
        raw: object,
        live: dict[str, tuple[Path, int]],
        projects: dict[Path, Project],
        *,
        allow_missing_codex_metadata: bool = False,
        probe_live_writer: bool = True,
    ) -> _Running | None:
        """Validate one state-file binding against current tmux and metadata.

        The runtime file is a cache, not authority: every string is bounded,
        the tmux session/provider/cwd must still agree, and real ids must still
        resolve to metadata in that cwd.  On Linux a positive rollout-fd probe
        also vetoes a stale mapping that points at a different live writer.
        """
        if not isinstance(raw, dict):
            return None
        key = raw.get("key")
        tmux_name = raw.get("tmux_name")
        session_type = raw.get("session_type")
        cwd_raw = raw.get("cwd")
        if not all(isinstance(v, str) for v in
                   (key, tmux_name, session_type, cwd_raw)):
            return None
        if (not key or len(key) > 256 or not tmux_name or len(tmux_name) > 256
                or session_type not in {"claude", "codex"}
                or not cwd_raw or len(cwd_raw) > 4096):
            return None
        cwd = Path(cwd_raw)
        if not cwd.is_absolute() or tmux_name not in live:
            return None
        live_cwd, _created = live[tmux_name]
        if self._path_key(cwd) != self._path_key(live_cwd):
            return None
        mode = self._modes().for_tmux_name(tmux_name)
        if mode is None or mode.session_type != session_type:
            return None
        project = projects.get(self._path_key(cwd))
        if (project is None and session_type == "codex"
                and allow_missing_codex_metadata):
            project = self._synthesise_codex_project(cwd, 0)
            projects[self._path_key(cwd)] = project
        if project is None:
            return None

        if key.startswith("__new__-"):
            if self._placeholder_tmux_key(tmux_name, mode) != key:
                return None
            created_at = raw.get("created_at", 0.0)
            if (not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(float(created_at))
                    or float(created_at) < 0):
                return None
            pre_raw = raw.get("pre_launch_ids", [])
            if (not isinstance(pre_raw, list) or len(pre_raw) > 100000
                    or any(not isinstance(item, str) or len(item) > 256
                           for item in pre_raw)):
                return None
            pre_launch_complete = raw.get(
                "pre_launch_complete", "pre_launch_ids" in raw)
            if not isinstance(pre_launch_complete, bool):
                return None
            return _Running(
                key=key,
                tmux_name=tmux_name,
                label=f"{project.display_name}/(new)",
                project=project,
                placeholder_path=cwd,
                created_at=float(created_at),
                pre_launch_ids=frozenset(pre_raw),
                allow_heuristic_resolution=pre_launch_complete,
                session_type=session_type,
            )

        if session_type == "codex":
            meta = self._codex_index.get(key, refresh=False)
            if (meta is not None
                    and self._path_key(meta.project.real_path)
                    != self._path_key(cwd)):
                return None
            if probe_live_writer:
                try:
                    open_ids = tmux_ctl.session_rollout_ids(
                        tmux_name, self._codex_home_path() / "sessions")
                except Exception:
                    open_ids = None
            else:
                open_ids = None
            # An empty set is a transient/permission failure and does not
            # disprove the persisted mapping.  A non-empty set naming other
            # rollouts but not this id does disprove it.
            if open_ids and key not in open_ids:
                return None
            if meta is None:
                if not allow_missing_codex_metadata:
                    return None
                renames = getattr(self, "_renames", None)
                title = (renames.get(key) if renames is not None else None)
                title = title or key[:8]
                return _Running(
                    key=key,
                    tmux_name=tmux_name,
                    label=f"{project.display_name}/{title}",
                    project=project,
                    status="busy",
                    session_type=session_type,
                )
        else:
            meta = self._session_cache.get(project, key)
            if meta is None:
                return None
        return _Running(
            key=key,
            tmux_name=tmux_name,
            label=f"{project.display_name}/{meta.display_title}",
            project=project,
            status=meta.status,
            last_mtime=meta.last_mtime,
            session_type=session_type,
        )

    def _running_binding_data(
        self,
        running: _Running,
        *,
        include_launch_context: bool = False,
    ) -> dict | None:
        if running.is_legacy:
            return None
        cwd = (
            running.placeholder_path
            if running.is_placeholder
            else running.project.real_path if running.project is not None
            else None
        )
        if cwd is None:
            return None
        data = {
            "key": running.key,
            "tmux_name": running.tmux_name,
            "session_type": running.session_type,
            "cwd": str(cwd),
        }
        if running.is_placeholder:
            data["created_at"] = running.created_at
            if include_launch_context:
                data["pre_launch_ids"] = sorted(running.pre_launch_ids)
                data["pre_launch_complete"] = (
                    running.allow_heuristic_resolution)
            else:
                # The potentially-large exclusion set lives in the atomic
                # runtime state, not in a tmux command argument. A stamp alone
                # therefore identifies the process but must not authorize
                # heuristic binding.
                data["pre_launch_complete"] = False
        return data

    def _stamp_running(self, running: _Running) -> bool:
        """Best-effort identity stamp for cross-platform orphan recovery."""
        if running.is_legacy:
            return False
        import json
        data = self._running_binding_data(running)
        if data is None:
            return False
        return tmux_ctl.set_session_user_option(
            running.tmux_name,
            _SESSION_BINDING_OPTION,
            json.dumps(data, separators=(",", ":"), sort_keys=True),
        )

    @staticmethod
    def _write_orphan_marker(marker: orphan_marker.Marker) -> bool:
        """Write and read back the bounded marker on its immutable session."""
        try:
            raw = orphan_marker.encode(marker)
        except ValueError:
            return False
        if not tmux_ctl.set_session_user_option(
                marker.tmux_session_id, orphan_marker.OPTION_NAME, raw):
            return False
        saved = tmux_ctl.show_session_user_option(
            marker.tmux_session_id, orphan_marker.OPTION_NAME)
        return orphan_marker.decode(saved) == marker

    def _exact_running_pane(
        self, running: _Running,
    ) -> tmux_ctl.PaneIdentity | None:
        marker = running.orphan
        if marker is None:
            return None
        pane = tmux_ctl.pane_identity(marker.tmux_pane_id)
        topology = tmux_ctl.session_topology(marker.tmux_session_id)
        home_matches = bool(
            topology is not None
            and topology.session_id == marker.tmux_session_id
            and topology.session_name == marker.tmux_name
        )
        at_home = orphan_marker.same_live_tmux(marker, pane)
        displayed = bool(
            home_matches and pane is not None and not pane.dead and not at_home
            and self._display_transport().displayed_real_pane(
                marker.tmux_name) == marker.tmux_pane_id
        )
        if (not home_matches or pane is None or pane.dead
                or not (at_home or displayed)):
            return None
        saved = orphan_marker.decode(tmux_ctl.show_session_user_option(
            marker.tmux_session_id, orphan_marker.OPTION_NAME))
        if saved != marker:
            return None
        return pane

    def _running_action_valid(
        self, running: _Running | None, identity_token: str | None = None,
    ) -> bool:
        if running is None:
            return False
        if running.is_legacy:
            if (identity_token is not None and running.orphan is not None
                    and identity_token != running.orphan.creation_token):
                return False
            return bool(
                running.legacy_server is not None
                and running.legacy_session_id is not None
                and tmux_server.target_has_session(
                    running.legacy_server,
                    running.legacy_session_id,
                    timeout=0.5,
                )
            )
        if running.orphan is None:
            return identity_token is None
        if (identity_token is not None
                and identity_token != running.orphan.creation_token):
            return False
        return self._exact_running_pane(running) is not None

    def _discover_orphans_consistent(
        self, state: dict | None = None,
    ) -> tuple[bool, int]:
        """Run discovery against one immutable Codex generation."""
        index = getattr(self, "_codex_index", None)
        if not isinstance(index, BackgroundCodexIndex):
            return self._discover_orphans(state), 0
        generation = index.begin_read()
        before = set(self._running)
        try:
            complete = self._discover_orphans(
                state,
                allow_missing_codex_metadata=generation == 0,
            )
            if generation == 0:
                self._codex_provisional_recovery_keys.update(
                    key for key in set(self._running) - before
                    if self._running[key].session_type == "codex"
                )
            return complete, generation
        finally:
            index.end_read()

    def _discover_legacy_running(self, *, force: bool = False) -> int:
        """Deprecated bridge: merge sessions from tmux's old default server.

        Discovery is deliberately read-only: no marker migration, ownership
        claim, rename, resize, or process signal is sent to the old server.
        New Railmux sessions always remain on the dedicated server. Keep this
        method isolated so the bridge can be deleted with ``legacy_sessions``
        after the documented upgrade window.
        """
        now = time.monotonic()
        previous = getattr(self, "_legacy_discovery_at", 0.0)
        if not force and now - previous < 5.0:
            return 0
        self._legacy_discovery_at = now
        target, records, complete = legacy_sessions.discover(timeout=0.25)
        if not complete:
            return 0

        project_snapshot = getattr(self, "_project_snapshot", None)
        if project_snapshot is None:
            project_snapshot = list_projects(self._claude_home)
        projects = {self._path_key(p.real_path): p for p in project_snapshot}
        try:
            codex_cwds = self._codex_index.all_cwds(refresh=False)
        except Exception:
            codex_cwds = {}
        for cwd, count in codex_cwds.items():
            projects.setdefault(
                self._path_key(cwd), self._synthesise_codex_project(cwd, count))

        # Rebuild only the legacy slice. A transient inventory failure returned
        # above without erasing last-known-good entries; normal exact liveness
        # polling removes sessions whose immutable identity has truly gone.
        for key in [key for key, item in self._running.items() if item.is_legacy]:
            del self._running[key]
        if target is None:
            return 0
        found = 0
        for record in records:
            mode = self._modes().for_tmux_name(record.name)
            if mode is None:
                continue
            project = projects.get(self._path_key(record.cwd))
            if project is None and mode.project_source == ProjectSource.CODEX:
                project = self._synthesise_codex_project(record.cwd, 0)
                projects[self._path_key(record.cwd)] = project
            if project is None:
                continue

            running: _Running | None = None
            marker = orphan_marker.decode(record.orphan_marker)
            if record.orphan_marker and marker is None:
                # Presence of a corrupt/newer authority marker is a fence, not
                # permission to fall back to a weaker v1/name heuristic.
                continue
            if (marker is not None
                    and marker.tmux_name == record.name
                    and marker.tmux_session_id == record.session_id
                    and marker.mode_key == mode.key):
                if marker.phase == "resolved" and marker.session_id:
                    running = self._valid_running_binding(
                        {
                            "key": marker.session_id,
                            "tmux_name": record.name,
                            "session_type": mode.session_type,
                            "cwd": str(record.cwd),
                        },
                        {record.name: (record.cwd, record.created_at)},
                        projects,
                        allow_missing_codex_metadata=True,
                        probe_live_writer=False,
                    )
                if running is None:
                    running = _Running(
                        key=f"__new__-legacy-{record.session_id[1:]}",
                        tmux_name=record.name,
                        label=f"{project.display_name}/(legacy unresolved)",
                        project=project,
                        placeholder_path=record.cwd,
                        created_at=float(record.created_at),
                        allow_heuristic_resolution=False,
                        status="blocked",
                        session_type=mode.session_type,
                    )
            elif marker is not None:
                # A valid marker for a different immutable object is equally
                # authoritative: never adopt or act on it as this candidate.
                continue
            elif record.binding:
                try:
                    import json
                    binding = json.loads(record.binding)
                except (ValueError, TypeError):
                    binding = None
                running = self._valid_running_binding(
                    binding,
                    {record.name: (record.cwd, record.created_at)},
                    projects,
                    allow_missing_codex_metadata=True,
                    probe_live_writer=False,
                )
            else:
                if not record.historical_shape:
                    continue
                placeholder = self._placeholder_tmux_key(record.name, mode)
                if placeholder is None:
                    truncated = record.name[len(mode.tmux_prefix):]
                    full_id = (
                        self._resolve_truncated_codex_id(truncated, record.cwd)
                        if mode.project_source == ProjectSource.CODEX
                        else self._resolve_truncated_id(truncated, project)
                    )
                    if full_id is not None:
                        running = self._valid_running_binding(
                            {
                                "key": full_id,
                                "tmux_name": record.name,
                                "session_type": mode.session_type,
                                "cwd": str(record.cwd),
                            },
                            {record.name: (record.cwd, record.created_at)},
                            projects,
                            allow_missing_codex_metadata=True,
                            probe_live_writer=False,
                        )
                else:
                    running = _Running(
                        key=f"__new__-legacy-{record.session_id[1:]}",
                        tmux_name=record.name,
                        label=f"{project.display_name}/(legacy unresolved)",
                        project=project,
                        placeholder_path=record.cwd,
                        created_at=float(record.created_at),
                        allow_heuristic_resolution=False,
                        status="blocked",
                        session_type=mode.session_type,
                    )
            if running is None:
                continue
            original_key = running.key
            running.provider_session_id = running.logical_session_id
            if running.key in self._running:
                running.key = (
                    f"__legacy__-{target.server_pid}-{record.session_id[1:]}"
                )
            # The slot/UI handle must remain unique even when an upgraded
            # instance and its legacy predecessor have the same tmux name.
            running.tmux_name = (
                f"{record.name}::legacy:{target.server_pid}:"
                f"{record.session_id[1:]}"
            )
            if running.provider_session_id is None and not original_key.startswith(
                    "__new__-"):
                running.provider_session_id = original_key
            running.legacy_server = target
            running.legacy_session_id = record.session_id
            self._running[running.key] = running
            found += 1
        return found

    def _discover_orphans(
        self,
        state: dict | None = None,
        *,
        allow_missing_codex_metadata: bool = False,
    ) -> bool:
        """Find registered agent tmux sessions and rebuild ``_running``.

        Called at startup so a soft-quit → restart cycle picks up every
        session that was left alive.

        tmux session names are truncated (``_safe_name``, 16 chars), so
        we must resolve each truncated name back to the full session_id
        by scanning the project's sessions — otherwise the truncated key
        will not match ``SessionMeta.session_id`` elsewhere.
        """
        import subprocess as _sp
        self._codex_recovery_candidates_seen = False
        self._last_orphan_probe_ok = False
        try:
            out = _sp.check_output(
                ["tmux", "list-sessions", "-F",
                 "#{session_name}\t#{pane_current_path}\t#{session_created}"
                 "\t#{session_id}\t#{pane_id}"
                 f"\t#{{{orphan_marker.OPTION_NAME}}}"
                 f"\t#{{{_SESSION_BINDING_OPTION}}}"],
                stderr=_sp.DEVNULL, text=True,
            )
        except (OSError, _sp.CalledProcessError):
            return False
        self._last_orphan_probe_ok = True
        project_snapshot = getattr(self, "_project_snapshot", None)
        if project_snapshot is None:
            project_snapshot = list_projects(self._claude_home)
        projects = {
            self._path_key(p.real_path): p
            for p in project_snapshot
        }
        # A Codex-only cwd has no Claude project directory. Include synthetic
        # projects from one index snapshot so its surviving cx-* tmux session
        # is re-adopted after a soft restart instead of silently disappearing.
        has_codex_session = any(
            (mode := self._modes().for_tmux_name(line.split("\t", 1)[0]))
            is not None and mode.project_source == ProjectSource.CODEX
            for line in out.splitlines()
        )
        self._codex_recovery_candidates_seen = has_codex_session
        if has_codex_session:
            refresh_index = not isinstance(
                self._codex_index, BackgroundCodexIndex)
            for cwd, count in self._codex_index.all_cwds(
                    refresh=refresh_index).items():
                projects.setdefault(
                    self._path_key(cwd), self._synthesise_codex_project(cwd, count))

        live: dict[str, tuple[Path, int]] = {}
        live_objects: dict[str, tuple[str, str]] = {}
        stamps: dict[str, object] = {}
        orphan_stamps: dict[str, orphan_marker.Marker] = {}
        marker_governed: set[str] = set()
        for line in out.splitlines():
            parts = line.split("\t", 6)
            if len(parts) not in (2, 3, 4, 7) or not parts[0] or not parts[1]:
                continue
            try:
                created = int(parts[2]) if len(parts) >= 3 and parts[2] else 0
            except ValueError:
                created = 0
            live[parts[0]] = (Path(parts[1]), created)
            if len(parts) == 7:
                if parts[3] and parts[4]:
                    live_objects[parts[0]] = (parts[3], parts[4])
                if parts[5]:
                    # Presence alone fences every legacy adoption path. A
                    # corrupt/newer v2 marker is unresolved authority, never a
                    # reason to fall back to ownerless name/cwd inference.
                    marker_governed.add(parts[0])
                marker = orphan_marker.decode(parts[5])
                if marker is not None:
                    orphan_stamps[parts[0]] = marker
                legacy_raw = parts[6]
            else:
                legacy_raw = parts[3] if len(parts) == 4 else ""
            if legacy_raw:
                try:
                    import json
                    stamps[parts[0]] = json.loads(legacy_raw)
                except (json.JSONDecodeError, ValueError):
                    pass

        found = 0
        # Discovery is also called as a resume-time race guard, not only on an
        # empty startup registry. Never re-adopt a tmux already represented by
        # a placeholder key under a second real-id key.
        claimed_tmux: set[str] = {
            running.tmux_name for running in self._running.values()
        }
        state_bindings = (
            state.get("running_bindings", [])
            if (isinstance(state, dict)
                and state.get("running_bindings_version") == 1
                and isinstance(state.get("running_bindings"), list))
            else []
        )
        # Version-2 markers are authoritative for new launches. They carry the
        # exact tmux objects, launch owner, and transaction phase; a name/cwd
        # match alone can never re-adopt one.
        current_owner = getattr(self, "_restart_identity", None)
        live_panes: frozenset[str] | None = None
        owner_snapshot_loaded = False
        for name, marker in sorted(
                orphan_stamps.items(),
                key=lambda item: (item[1].created_at, item[0])):
            if name in claimed_tmux or marker.tmux_name != name:
                continue
            mode = self._modes().for_tmux_name(name)
            if (mode is None or marker.mode_key != mode.key
                    or self._placeholder_tmux_key(name, mode)
                    != marker.placeholder_key):
                continue
            pane = tmux_ctl.pane_identity(marker.tmux_pane_id)
            if not orphan_marker.same_live_tmux(marker, pane):
                continue
            if (current_owner is not None
                    and (marker.owner.server_digest
                         == current_owner.server_digest
                         or marker.owner.server_pid
                         == current_owner.server_pid)
                    and marker.owner.pane_id != current_owner.pane_id
                    and not owner_snapshot_loaded):
                server = tmux_ctl.server_snapshot()
                live_panes = server.panes if server is not None else None
                owner_snapshot_loaded = True
            if not orphan_marker.owner_available(
                    marker, current_owner, live_panes,
                    # Reaching this point already proved that the v2 marker is
                    # stored on its exact live tmux session and pane.  Permit a
                    # one-time claim from the <=0.1.1 ctime-based digest when
                    # the tmux server PID is unchanged; the claim immediately
                    # persists the new stable digest.
                    allow_legacy_server_digest=True):
                continue
            if (current_owner is not None
                    and (marker.owner.pane_id != current_owner.pane_id
                         or marker.owner.server_digest
                         != current_owner.server_digest)):
                claimed = orphan_marker.claim_owner(
                    marker,
                    current_owner,
                    tmux_ctl.show_session_user_option,
                    tmux_ctl.set_session_user_option,
                )
                if claimed is None:
                    continue
                marker = claimed

            cwd_key = self._path_key(marker.cwd)
            project = projects.get(cwd_key)
            if project is None:
                if mode.project_source == ProjectSource.CODEX:
                    project = self._synthesise_codex_project(marker.cwd, 0)
                else:
                    from railmux.path_codec import encode as encode_project_path
                    encoded = encode_project_path(marker.cwd)
                    project = Project(
                        real_path=marker.cwd,
                        encoded_name=encoded,
                        claude_dir=self._claude_home / "projects" / encoded,
                        session_count=0,
                        last_activity_ts=0.0,
                    )
                projects[cwd_key] = project
            running: _Running | None = None
            if marker.phase == "resolved" and marker.session_id is not None:
                running = self._valid_running_binding(
                    {
                        "key": marker.session_id,
                        "tmux_name": name,
                        "session_type": mode.session_type,
                        "cwd": str(marker.cwd),
                    },
                    {name: (marker.cwd, int(marker.created_at))},
                    projects,
                    allow_missing_codex_metadata=(
                        allow_missing_codex_metadata),
                )
            if running is None:
                pre_launch_ids: frozenset[str] = frozenset()
                pre_launch_complete = False
                for saved in state_bindings:
                    if (isinstance(saved, dict)
                            and saved.get("tmux_name") == name
                            and saved.get("key") == marker.placeholder_key
                            and saved.get("cwd") == str(marker.cwd)
                            and isinstance(saved.get("pre_launch_ids"), list)):
                        values = saved["pre_launch_ids"]
                        if (len(values) <= 100000
                                and all(isinstance(value, str)
                                        and len(value) <= 256
                                        for value in values)):
                            pre_launch_ids = frozenset(values)
                            complete = saved.get(
                                "pre_launch_complete", True)
                            pre_launch_complete = (
                                complete if isinstance(complete, bool)
                                else False)
                        break
                running = _Running(
                    key=marker.placeholder_key,
                    tmux_name=name,
                    label=(f"{project.display_name}/"
                           f"({'launch interrupted' if marker.phase == 'launching' else 'unresolved'})"),
                    project=project,
                    placeholder_path=marker.cwd,
                    created_at=marker.created_at,
                    pre_launch_ids=pre_launch_ids,
                    allow_heuristic_resolution=pre_launch_complete,
                    status="blocked",
                    session_type=mode.session_type,
                )
            running.orphan = marker
            if running.key in self._running:
                continue
            self._running[running.key] = running
            claimed_tmux.add(name)
            found += 1

        # A tmux-local stamp is the primary source: it has the same lifetime as
        # the live agent session and works on platforms without procfs.
        for name, raw in sorted(
                stamps.items(), key=lambda item: (live[item[0]][1], item[0])):
            if name in marker_governed:
                continue
            enriched = raw
            # An unresolved stamp identifies the process, while the runtime
            # state carries its potentially-large pre-launch exclusion set.
            # Merge the latter so macOS heuristic resolution stays fenced.
            if isinstance(raw, dict) and str(raw.get("key", "")).startswith(
                    "__new__-"):
                for saved in state_bindings:
                    if (isinstance(saved, dict)
                            and saved.get("tmux_name") == name
                            and saved.get("key") == raw.get("key")):
                        enriched = dict(raw)
                        if "pre_launch_ids" in saved:
                            enriched["pre_launch_ids"] = saved["pre_launch_ids"]
                            # Older state omitted this field and always carried
                            # a complete launch snapshot. Legacy migration
                            # writes False explicitly because it must remain
                            # visible without ever enabling UUID heuristics.
                            enriched["pre_launch_complete"] = saved.get(
                                "pre_launch_complete", True)
                        break
            running = self._valid_running_binding(
                enriched,
                live,
                projects,
                allow_missing_codex_metadata=allow_missing_codex_metadata,
            )
            if running is None or running.tmux_name != name:
                continue
            if running.key in self._running:
                continue
            identities = live_objects.get(name)
            if identities is not None:
                mode = self._modes().for_tmux_name(name)
                if mode is not None:
                    marker = self._migrate_legacy_v2_marker(
                        name=name,
                        cwd=live[name][0],
                        created=live[name][1],
                        session_id=identities[0],
                        pane_id=identities[1],
                        mode=mode,
                        resolved_session_id=(
                            None if running.is_placeholder else running.key),
                    )
                    if marker is not None:
                        running.orphan = marker
            self._running[running.key] = running
            claimed_tmux.add(name)
            found += 1

        if state_bindings:
            for raw in state_bindings:
                running = self._valid_running_binding(
                    raw,
                    live,
                    projects,
                    allow_missing_codex_metadata=allow_missing_codex_metadata,
                )
                if (running is None or running.tmux_name in claimed_tmux
                        or running.tmux_name in marker_governed):
                    continue
                # A valid persisted binding is authoritative.  Duplicate state
                # entries cannot replace an already-restored real id.
                if running.key in self._running:
                    continue
                self._running[running.key] = running
                claimed_tmux.add(running.tmux_name)
                self._stamp_running(running)
                found += 1

        # Oldest writer wins among unpersisted duplicates.  This is important
        # for recovery from the historical bug: clicking a falsely non-running
        # row created a newer stable-name writer for the same rollout while the
        # original placeholder writer was still working.
        for name, (cwd, _created) in sorted(
                live.items(), key=lambda item: (item[1][1], item[0])):
            if name in claimed_tmux or name in marker_governed:
                continue
            mode = self._modes().for_tmux_name(name)
            if mode is None:
                continue
            truncated = name[len(mode.tmux_prefix):]
            project = projects.get(self._path_key(cwd))
            if project is None:
                continue

            placeholder_key = self._placeholder_tmux_key(name, mode)
            if placeholder_key is not None:
                # State-free recovery is exact on Linux: the live Codex process
                # tells us which rollout(s) it has open.  Restrict matches to
                # indexed metadata in this cwd; ambiguity is never guessed.
                full_id = None
                if mode.project_source == ProjectSource.CODEX:
                    try:
                        open_ids = tmux_ctl.session_rollout_ids(
                            name, self._codex_home_path() / "sessions")
                    except Exception:
                        open_ids = None
                    if open_ids:
                        matches = [
                            session_id for session_id in open_ids
                            if (meta := self._codex_index.get(
                                session_id, refresh=False)) is not None
                            and self._path_key(meta.project.real_path)
                            == self._path_key(cwd)
                        ]
                        if len(matches) == 1:
                            full_id = matches[0]
                if full_id is None:
                    identities = live_objects.get(name)
                    legacy = (
                        self._legacy_unresolved_running(
                            name=name,
                            cwd=cwd,
                            created=_created,
                            session_id=identities[0],
                            pane_id=identities[1],
                            mode=mode,
                            project=project,
                        )
                        if identities is not None else None
                    )
                    if legacy is None or legacy.key in self._running:
                        continue
                    self._running[legacy.key] = legacy
                    claimed_tmux.add(name)
                    self._stamp_running(legacy)
                    found += 1
                    continue
            else:
                # Resolve the truncated key back to the full session_id.
                if mode.project_source == ProjectSource.CODEX:
                    full_id = self._resolve_truncated_codex_id(truncated, cwd)
                else:
                    full_id = self._resolve_truncated_id(truncated, project)
            if full_id is None:
                continue
            if (mode.project_source == ProjectSource.CODEX
                    and placeholder_key is None):
                try:
                    writer_ids = tmux_ctl.session_rollout_ids(
                        name, self._codex_home_path() / "sessions")
                except Exception:
                    writer_ids = None
                if writer_ids and full_id not in writer_ids:
                    continue
            if full_id in self._running:
                continue
            self._running[full_id] = _Running(
                key=full_id,
                tmux_name=name,
                label=f"{project.display_name}/{full_id[:8]}",
                project=project,
                session_type=mode.session_type,
            )
            claimed_tmux.add(name)
            self._stamp_running(self._running[full_id])
            found += 1
        if found:
            self._set_status(
                f"Found {found} running session(s)")
        expected_live = {
            raw.get("tmux_name")
            for raw in state_bindings
            if (isinstance(raw, dict)
                and isinstance(raw.get("tmux_name"), str)
                and raw.get("tmux_name") in live
                and self._modes().for_tmux_name(raw["tmux_name"]) is not None)
        }
        expected_live.update(
            name for name in stamps if name not in marker_governed)
        # Retain the state file when a live persisted agent was left unclaimed;
        # a transient index/project read failure must not destroy the only
        # no-procfs recovery record.
        return expected_live <= claimed_tmux

    @staticmethod
    def _resolve_truncated_id(truncated: str, project: Project) -> str | None:
        """Find the full session_id whose ``_safe_name`` matches *truncated*."""
        import os as _os
        try:
            with _os.scandir(project.claude_dir) as scan:
                for entry in scan:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    full_id = entry.name[:-6]  # strip ".jsonl"
                    if App._safe_name(full_id, 16) == truncated:
                        return full_id
        except OSError:
            pass
        return None

    def _resolve_truncated_codex_id(self, truncated: str, cwd: Path) -> str | None:
        """Find a Codex session_id whose ``_safe_name`` matches *truncated*,
        restricted to sessions whose cwd matches."""
        for meta in self._codex_index.sessions_for_cwd(cwd):
            if self._safe_name(meta.session_id, 16) == truncated:
                return meta.session_id
        return None

    def _show_overlay(self, modal: urwid.Widget, width: int, height: int,
                       *, click_outside_to_close: bool = False,
                       fixed_width: bool = False,
                       fixed_height: bool = False,
                       on_click_outside: Callable[[], None] | None = None) -> None:
        if self._loop is None:
            return
        columns, rows = 80, 24
        try:
            columns, rows = self._loop.screen.get_cols_rows()
        except Exception:
            pass
        # When the right pane is open the railmux sidebar is only ~30% of the
        # terminal.  Bump relative dimensions so overlays stay readable.
        # Fixed-pixel overlays (context menus) are left alone.
        if not fixed_width and self._right_pane_open():
            width = int(width * 1.6)
        if not fixed_height and self._right_pane_open():
            height = int(height * 1.35)
        # Never let a proportional overlay grow beyond its pane after the
        # narrow-sidebar multiplier. Fixed menus likewise shrink inside the
        # available pane instead of being clipped on both sides.
        width_spec = (
            min(width, max(1, columns - 2))
            if fixed_width else ("relative", min(96, max(1, width)))
        )
        height_spec = (
            min(height, max(1, rows - 2))
            if fixed_height else ("relative", min(96, max(1, height)))
        )
        overlay_cls = _CloseOnClickOverlay if click_outside_to_close else urwid.Overlay
        kw = {}
        if click_outside_to_close:
            kw["on_click_outside"] = on_click_outside or self._close_modal
        overlay = overlay_cls(
            modal, self._frame,
            align="center", width=width_spec,
            valign="middle", height=height_spec,
            **kw,
        )
        self._loop.widget = overlay

    def _show_preferred_height_modal(
        self, modal: urwid.Widget, *, width: int,
    ) -> None:
        """Size a wrapping/scrolling modal from its rendered content."""
        columns, rows = 80, 24
        if self._loop is not None:
            try:
                columns, rows = self._loop.screen.get_cols_rows()
            except Exception:
                pass
        effective_width = min(
            96,
            int(width * 1.6) if self._right_pane_open() else width,
        )
        overlay_columns = max(8, columns * effective_width // 100)
        height = min(
            modal.preferred_height(overlay_columns),
            max(1, rows - 2),
        )
        self._show_overlay(
            modal,
            width=width,
            height=height,
            fixed_height=True,
        )

    def _show_delete_confirm(self, modal: DeleteConfirmModal) -> None:
        """Show a compact confirmation that grows only for wrapped content."""
        self._show_preferred_height_modal(modal, width=54)

    def _show_quit_confirm(self, modal: QuitConfirmModal) -> None:
        """Show the quit choices at a height derived from their wrapped text."""
        self._show_preferred_height_modal(modal, width=50)

    def _show_rename_modal(self, modal: RenameModal) -> None:
        """Keep wrapped existing titles and the rename actions reachable."""
        self._show_preferred_height_modal(modal, width=50)

    def _close_modal(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._frame
        self._sessions_pane.set_selected_session(None)
        self._running_pane.set_selected(None)

    # --- key handling ---

    def _on_input(self, key: str) -> None:
        # F9 is routed here by a global tmux binding and must remain available
        # while a modal is open (notably Help's fullscreen copy workflow).
        if key == "f9":
            self._toggle_agent_fullscreen()
            return
        # When a modal overlay is showing, don't dispatch sidebar action keys.
        # Modals handle their own keys (Esc, Enter, y/n) in their keypress
        # methods.  Unhandled keys like q would otherwise open a second modal
        # (quit confirm) without cleaning up the first — leaving help's tmux
        # zoom stuck, for example.
        if self._loop is not None and self._loop.widget is not self._frame:
            return
        if key == "esc":
            # Esc navigates "up" the pane hierarchy:
            #   Running → Sessions → Projects
            if self._sidebar.focus_position == 2:
                self._sidebar.focus_position = 1
                self._hint_bar.set_context(self._help_context())
                return
            if self._sidebar.focus_position == 1:
                self._sidebar.focus_position = 0
                self._hint_bar.set_context(self._help_context())
                return
            return
        if key == "ctrl c":
            self._open_quit_confirm()
            return
        if key in ("tab", "shift tab"):
            self._rotate_focus(reverse=(key == "shift tab"))
            return
        if key == "/":
            self._enter_filter_mode()
            return
        if key in ("[", "]"):
            self._resize_divider(key == "]")
            return
        if key == "f8":
            self._rotate_split()
            return
        # Simple action keys are dispatched from the shared keymap (single
        # source of truth shared with the hint bar) so the two can't drift.
        action = keymap.action_for(key, self._help_context())
        if action is not None:
            getattr(self, action)()
            return

    def _maybe_prompt_codex_yolo(self) -> None:
        """Ask once per chosen lifetime whether Codex may bypass safeguards."""
        # getattr: keep bare ``App.__new__`` unit tests (no loop/settings) safe.
        if getattr(self, "_loop", None) is None:
            return
        settings = getattr(self, "_settings", None)
        if (settings is None or settings.codex_yolo_policy != "ask"
                or getattr(self, "_codex_yolo_prompt_handled", False)):
            return

        def _always() -> None:
            saved = self._settings.set_codex_yolo_policy("always")
            self._close_modal()
            if not saved:
                self._set_status(
                    "Could not save Codex auto-run choice; settings unchanged.",
                    "error",
                )
                return
            self._codex_yolo_prompt_handled = True
            self._set_status("Codex auto-run always enabled (m to exit mode).")

        def _this_time() -> None:
            self._codex_yolo_runtime = True
            self._codex_yolo_prompt_handled = True
            self._close_modal()
            self._set_status("Codex auto-run enabled for this Railmux run.")

        def _no() -> None:
            self._codex_yolo_runtime = False
            self._codex_yolo_prompt_handled = True
            self._close_modal()
            self._set_status(
                "Codex auto-run remains off for this Railmux run.")

        def _never() -> None:
            saved = self._settings.set_codex_yolo_policy("never")
            self._codex_yolo_runtime = False
            self._codex_yolo_prompt_handled = True
            self._close_modal()
            if not saved:
                self._set_status(
                    "Could not save the Never Codex auto-run choice; "
                    "keeping it off for this Railmux run.",
                    "error",
                )
                return
            self._set_status("Codex auto-run disabled permanently.")

        from railmux.ui.modals import YoloConfirmModal
        modal = YoloConfirmModal(
            on_always=_always,
            on_this_time=_this_time,
            on_no=_no,
            on_never=_never,
        )
        self._show_overlay(modal, width=60, height=45)

    def _codex_yolo_enabled(self) -> bool:
        settings = getattr(self, "_settings", None)
        persisted = bool(
            settings is not None
            and settings.codex_yolo_policy == "always"
        )
        return bool(
            persisted or getattr(self, "_codex_yolo_runtime", False)
        )

    def _schedule_mode_data_refresh(self) -> None:
        """Refresh Claude project discovery without blocking the UI thread."""
        thread = self._mode_refresh_thread
        if thread is not None and thread.is_alive():
            return
        with self._mode_refresh_lock:
            if self._mode_refresh_result is not None:
                return

        claude_home = self._claude_home
        lock = self._mode_refresh_lock

        def _worker() -> None:
            try:
                projects = list_projects(claude_home)
                result = (projects, None)
            except Exception as exc:
                result = (None, str(exc))
            with lock:
                self._mode_refresh_result = result

        thread = threading.Thread(
            target=_worker,
            name="railmux-mode-refresh",
            daemon=True,
        )
        self._mode_refresh_thread = thread
        thread.start()

    def _mode_refresh_pending(self) -> bool:
        project_pending = self._project_refresh_pending()
        index = getattr(self, "_codex_index", None)
        background = isinstance(index, BackgroundCodexIndex)
        codex_pending = index.is_pending if background else False
        codex_cold = (not index.has_snapshot and not index.is_unavailable
                      if background else False)
        return project_pending or codex_pending or codex_cold

    def _project_refresh_pending(self) -> bool:
        thread = getattr(self, "_mode_refresh_thread", None)
        lock = getattr(self, "_mode_refresh_lock", None)
        if lock is None:
            return False
        with lock:
            has_result = self._mode_refresh_result is not None
        return has_result or (thread is not None and thread.is_alive())

    def _request_codex_refresh(self, *, force: bool = False) -> None:
        """Request a nonblocking Codex generation (test-double compatible)."""
        index = self._codex_index
        if isinstance(index, BackgroundCodexIndex):
            index.refresh(force=force)
        else:
            index.refresh()

    def _consume_mode_refresh(self) -> bool:
        """Install a completed worker result on the UI thread."""
        lock = getattr(self, "_mode_refresh_lock", None)
        if lock is None:
            return False
        with lock:
            result = self._mode_refresh_result
            self._mode_refresh_result = None
        if result is None:
            return False
        legacy_index = None
        if len(result) == 3:  # <=0.1.1 test/integration payload
            projects, legacy_index, error = result
        else:
            projects, error = result
        if error is not None or projects is None:
            self._set_status(
                f"Background mode refresh failed: {error or 'unknown error'}",
                "warn",
            )
            return False
        self._project_snapshot = projects
        self._project_snapshot_at = time.monotonic()
        if (legacy_index is not None
                and not isinstance(self._codex_index, BackgroundCodexIndex)):
            self._codex_index = legacy_index
        self._codex_project_filter = self._codex_index.all_cwds(refresh=False)
        return True

    def _cycle_mode(self) -> None:
        """Move to the next registered agent mode."""
        target = self._modes().next_key(self._current_mode_key())
        self._switch_mode(target)

    def _set_mode_pane_context(self, mode: AgentMode) -> None:
        """Update provider-aware pane copy without coupling panes to mode keys."""
        projects_pane = getattr(self, "_projects_pane", None)
        sessions_pane = getattr(self, "_sessions_pane", None)
        running_pane = getattr(self, "_running_pane", None)
        if projects_pane is not None:
            projects_pane.set_provider_label(mode.label)
        if sessions_pane is not None:
            sessions_pane.set_provider_label(mode.label)
        if running_pane is not None:
            running_pane.set_provider_label(mode.label)
            running_pane.set_filter(
                self._current_mode_view_state().running_filter,
                capture_focus=False,
            )

    def _configured_mode_binary(self, mode: AgentMode) -> str:
        config = getattr(self, "_config", Config())
        return getattr(config, mode.binary_config_attr)

    def _warn_missing_mode_binary(self, mode: AgentMode) -> bool:
        """Warn without echoing a configured command or user-specific path."""
        binary = self._configured_mode_binary(mode)
        if shutil.which(binary) is not None:
            return False
        self._set_status(
            f"{mode.label} executable not found; install it or configure its binary.",
            "warn",
        )
        return True

    def _switch_mode(self, mode_key: str) -> None:
        """Switch to *mode_key* without encoding a two-provider toggle.

        Paint immediately from stale-safe snapshots, then let a daemon worker
        refresh NFS-backed indexes for the next UI refresh tick.
        """
        target = self._modes().resolve(mode_key)
        if target.key == self._current_mode_key():
            return
        outgoing_project = self._selected_project
        if outgoing_project is not None:
            self._remember_project_selection(outgoing_project)
        self._active_mode_key = target.key
        self._set_mode_pane_context(target)
        # Running holds sessions from every mode in one registry; repaint the
        # provider-scoped view immediately instead of showing the outgoing
        # mode until the next periodic tick.
        if getattr(self, "_running_pane", None) is not None:
            self._render_running_pane()
        # Repaint the tmux brand while retaining the current error/normal colour.
        self._apply_tmux_bar(self._tmux_error_bar)
        next_label = self._modes().get(
            self._modes().next_key(target.key)).label
        suffix = f"  (m for {next_label})"
        if target.prompt_for_auto_run:
            self._maybe_prompt_codex_yolo()
        if target.project_source == ProjectSource.CODEX:
            self._request_codex_refresh(force=True)
            self._schedule_mode_data_refresh()
            self._projects_pane.set_projects(self._visible_projects(allow_stale=True))
            if not self._codex_project_filter:
                # The current mode visibly has no project, so it must not keep
                # the outgoing Claude project as a hidden target for ``n``.
                # Its path remains in the Claude-specific memory for return.
                self._clear_current_project()
                if self._mode_refresh_pending():
                    self._set_status(
                        f"{target.label} mode — loading sessions…{suffix}")
                elif (isinstance(self._codex_index, BackgroundCodexIndex)
                      and self._codex_index.is_unavailable):
                    self._set_status(
                        f"{target.label} mode — session index unavailable{suffix}",
                        "warn",
                    )
                else:
                    self._set_status(
                        f"{target.label} mode — no sessions found{suffix}")
                self._warn_missing_mode_binary(target)
                return
            self._set_status(f"{target.label} mode{suffix}")
            visible = self._visible_projects(allow_stale=True)
            selected = self._preferred_project(visible, outgoing_project)
            if selected is not None:
                self._on_project_select(selected)
            else:
                self._clear_current_project()
        else:
            visible = self._visible_projects(allow_stale=True)
            self._projects_pane.set_projects(visible)
            self._set_status(f"{target.label} mode{suffix}")
            # Resolve only against Claude-visible projects. A stale/deleted
            # Project object must never fall through to the Claude cache.
            selected = self._preferred_project(visible, outgoing_project)
            if selected is not None:
                self._on_project_select(selected)
            else:
                self._clear_current_project()
        self._warn_missing_mode_binary(target)

    def _toggle_codex_mode(self) -> None:
        """Compatibility alias for integrations written before mode cycling."""
        self._cycle_mode()

    def _enter_mode_on_restore(self, mode_key: str) -> None:
        """Activate a registered mode during startup state restoration.

        Kept separate from normal switching because the loop and tmux bar are
        not available yet and project selection is handled by the restore path.
        """
        mode = self._modes().resolve(mode_key)
        self._active_mode_key = mode.key
        self._set_mode_pane_context(mode)
        if mode.project_source == ProjectSource.CODEX:
            self._codex_project_filter = self._codex_index.all_cwds()
        self._projects_pane.set_projects(self._visible_projects())

    def _enter_codex_mode_on_restore(self) -> None:
        """Compatibility alias for pre-registry tests and extensions."""
        self._enter_mode_on_restore(CODEX_MODE.key)

    def _codex_home_path(self) -> Path:
        """The single resolved ``CODEX_HOME`` for this instance.

        One source of truth (config) shared by CodexIndex, new/resume launch,
        ``codex delete`` and config/env-key reading, so a non-default
        ``[codex] home`` can't make list/new/resume/delete diverge (#7)."""
        return self._config.resolved_codex_home()

    def _codex_env(self) -> dict[str, str]:
        """Environment variables to hand a launched Codex process.

        Only the NON-secret ``CODEX_HOME`` (the resolved home) is returned, so
        the child reads the same config/state/sessions railmux lists from. The
        provider API key is deliberately NOT injected: passing it via tmux
        ``-e`` would leak it (tmux retains ``-e`` values in the session
        environment, queryable via ``tmux show-environment``), and embedding it
        in the command string would expose it in argv/metadata. Instead, the
        launched Codex runs under ``$SHELL -li`` (login+interactive), which
        sources the user's profile and loads their key the normal way."""
        return {"CODEX_HOME": str(self._codex_home_path())}

    def _visible_projects(self, *, force: bool = False,
                          allow_stale: bool = False) -> list[Project]:
        """Projects for the current mode.

        Claude mode: projects with resumable sessions (plus empty projects when
        configured). Codex mode: only projects whose ``real_path`` has at least
        one Codex session.
        """
        now = time.monotonic()
        projects = self._project_snapshot
        if (projects is None or force
                or (not allow_stale
                    and now - self._project_snapshot_at >= self._PROJECT_SCAN_INTERVAL)):
            # The initial snapshot is built before Urwid starts. Thereafter a
            # normal UI tick only schedules the single coalesced discovery
            # worker and keeps painting the last-good project generation.
            if not self._project_refresh_pending():
                self._schedule_mode_data_refresh()
            projects = projects or []
        if self._active_mode().project_source != ProjectSource.CODEX:
            projects = [
                project for project in projects
                if not is_help_workspace(project.real_path)
            ]
            if getattr(getattr(self, "_config", None),
                       "show_empty_projects", False):
                return projects
            return [p for p in projects if p.session_count > 0]
        # Build a resolve-safe lookup: real_path → project.
        by_resolved: dict[Path, Project] = {}
        for p in projects:
            try:
                by_resolved[p.real_path.resolve()] = p
            except OSError:
                by_resolved[p.real_path] = p
        visible: list[Project] = []
        seen_encoded: set[str] = set()
        for cwd, codex_count in self._codex_project_filter.items():
            if is_help_workspace(cwd):
                continue
            try:
                key = cwd.resolve()
            except OSError:
                key = cwd
            existing = by_resolved.get(key)
            if existing is not None:
                if existing.encoded_name not in seen_encoded:
                    seen_encoded.add(existing.encoded_name)
                    # In Codex mode show the Codex session count, not the
                    # Claude count from discovery.
                    visible.append(replace(existing, session_count=codex_count))
            else:
                # Codex-only directory — synthesise a project entry so the
                # user can browse and launch sessions here.
                synth = self._synthesise_codex_project(cwd, codex_count)
                if synth.encoded_name not in seen_encoded:
                    seen_encoded.add(synth.encoded_name)
                    visible.append(synth)
        # Sort by recency: Claude projects by last_activity_ts, synthetic
        # ones by their most recent Codex session.
        def _sort_key(p: Project) -> float:
            ts = p.last_activity_ts
            if ts == 0.0:
                sessions = self._codex_index.sessions_for_cwd(p.real_path, refresh=False)
                if sessions:
                    ts = sessions[0].last_mtime
            return -ts
        visible.sort(key=lambda p: _sort_key(p))
        return visible

    def _project_in_current_view(self, project: Project) -> Project:
        """Return *project* as represented in the current mode's Projects list.

        Matched by resolved ``real_path``; falls back to *project* itself when
        it isn't in the visible set. Used so a running session's project (which
        may carry a foreign encoded_name) maps onto the actual sidebar row,
        keeping the Projects/Sessions highlight aligned instead of cleared."""
        try:
            target = project.real_path.resolve()
        except OSError:
            target = project.real_path
        for p in self._visible_projects(allow_stale=self._mode_refresh_pending()):
            try:
                key = p.real_path.resolve()
            except OSError:
                key = p.real_path
            if key == target:
                return p
        return project

    def _invalidate_project_snapshot(self) -> None:
        self._project_snapshot_at = 0.0

    @staticmethod
    def _synthesise_codex_project(cwd: Path, session_count: int = 0) -> Project:
        """Create a synthetic Project for a Codex-only directory."""
        from railmux.codex_index import _safe_encoded_name
        try:
            resolved = cwd.resolve()
        except OSError:
            resolved = cwd
        return Project(
            real_path=resolved,
            encoded_name=_safe_encoded_name(resolved),
            claude_dir=Path(),  # no Claude sessions directory
            session_count=session_count,
            last_activity_ts=0.0,
        )

    def _rotate_focus(self, reverse: bool = False) -> None:
        """Tab / Shift-Tab cycle through the three railmux sidebar panes.

        Jumping in/out of the agent pane uses tmux's native nav (Ctrl-B ←/→)
        so Tab keeps its native meaning inside each provider (autocomplete).
        """
        n = len(self._sidebar.contents)
        if n <= 1:
            return
        cur = self._sidebar.focus_position
        self._sidebar.focus_position = (cur - 1) % n if reverse else (cur + 1) % n
        self._hint_bar.set_context(self._help_context())

    def _teardown_tmux(self, *, defer_outer: bool = False) -> None:
        """Clean up on quit.

        The visible exit path performs core cleanup before leaving Urwid;
        ``run()``'s ``finally`` retries an interrupted phase and performs the
        outer-session kill. On soft quit the detached agent sessions and the
        outer tmux session are left alive. Both phases are idempotent.
        """
        if not getattr(self, "_teardown_core_done", False):
            index = getattr(self, "_codex_index", None)
            if isinstance(index, BackgroundCodexIndex):
                index.close(timeout_s=0.2)
            self._teardown_scroll_acceleration()
            selection = getattr(self, "_selection_isolation_manager", None)
            if selection is not None:
                selection.close()
            wheel = getattr(self, "_root_wheel_manager", None)
            if wheel is not None:
                wheel.close()
            self._clear_target_pane_option()
            bindings = getattr(self, "_tmux_binding_manager", None)
            if bindings is not None:
                bindings.close()
            # Drop our status-bar overrides BEFORE the soft-quit branch below —
            # on soft quit the outer tmux session may survive, so Railmux's
            # appearance and text must not linger in it.
            if self._tmux_status_enabled and self._tmux_status_session:
                try:
                    import subprocess as _sp
                    revert = [opt for opt, _ in self._TMUX_BAR_OPTIONS]
                    revert += list(self._TMUX_BAR_STYLE_OPTIONS)
                    revert.append("status-right")
                    for opt in revert:
                        _sp.run(
                            ["tmux", "set-option", "-u", "-t",
                             self._tmux_status_session, opt],
                            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        )
                except Exception:
                    pass
                self._tmux_status_enabled = False
            # Return real swap panes before deleting display placeholders. A
            # failed proof degrades hard quit to soft semantics.
            try:
                display_closed = self._display_transport().close_all()
            except Exception:
                display_closed = False
            if not display_closed:
                self._soft_quit_flag = True
            if not self._soft_quit_flag:
                for r in list(self._running.values()):
                    if r.is_legacy:
                        continue
                    try:
                        tmux_ctl.kill_session(r.tmux_name)
                    except Exception:
                        pass
                self._running.clear()
                # Help sessions intentionally stay outside _running. Include
                # helpers preserved by an earlier soft restart, but kill only
                # names whose persisted private identity still validates.
                for name in self._verified_help_session_names():
                    try:
                        tmux_ctl.kill_session(name)
                    except Exception:
                        pass
            self._teardown_core_done = True

        # Side-by-side focus temporarily enables tmux border arrows.  Keep
        # this outside the one-shot core guard: visible exit first calls
        # teardown with ``defer_outer=True``, and run()'s finally pass can then
        # retry a failed restoration before preserving or killing the outer
        # session. Success clears ``original_known`` and makes this a no-op.
        self._restore_border_indicators()

        if defer_outer or getattr(self, "_outer_teardown_done", False):
            return
        self._outer_teardown_done = True
        if not self._soft_quit_flag and self._auto_launched:
            session_name = tmux_ctl.current_session_name()
            if session_name == "railmux":
                identity = getattr(self, "_restart_identity", None)
                current_id = tmux_ctl.current_session_id()
                intent = bool(
                    identity is not None
                    and current_id == identity.session_id
                    and tmux_health.record_clean_exit(
                        server_pid=identity.server_pid,
                        session_id=identity.session_id,
                    )
                )
                try:
                    killed = tmux_ctl.kill_session("railmux")
                except Exception:
                    killed = False
                if intent and not killed:
                    tmux_health.clear_clean_exit()

    def _enter_filter_mode(self) -> None:
        # Borrow the button row (footer index 1) for a filter Edit — both are a
        # single line, so the sidebar height doesn't jump. Restored on enter/esc.
        focused_pane = (
            self._projects_pane,
            self._sessions_pane,
            self._running_pane,
        )[self._sidebar.focus_position]
        initial_text = focused_pane.filter_text
        edit = urwid.Edit(caption="filter: ", edit_text=initial_text)
        footer_pile = self._frame.contents["footer"][0]
        # The one-line filter temporarily replaces the possibly expanded
        # Button Bar, so there is no second footer row to charge to Running.
        self._sidebar.set_bottom_row_debt(0)
        footer_pile.contents[1] = (edit, footer_pile.options("pack"))
        footer_pile.focus_position = 1
        self._frame.focus_position = "footer"

        def on_change(widget, new_text):
            current_idx = self._sidebar.focus_position
            if current_idx == 0:
                self._projects_pane.set_filter(new_text)
            elif current_idx == 1:
                self._sessions_pane.set_filter(new_text)
            elif current_idx == 2:
                self._running_pane.set_filter(new_text)
                self._current_mode_view_state().running_filter = new_text

        urwid.connect_signal(edit, "change", on_change)

        def restore(key):
            if key in ("enter", "esc"):
                footer_pile.contents[1] = (self._button_bar, footer_pile.options("pack"))
                self._sidebar.set_bottom_row_debt(
                    self._button_bar.extra_rows)
                self._frame.focus_position = "body"
                return None
            return key

        original_keypress = edit.keypress
        def new_keypress(size, key):
            if key == "ctrl u":
                # Make clearing consistent across urwid versions and all three
                # filter fields. set_edit_text emits the change signal, so the
                # pane and the subsequently persisted restart state clear too.
                edit.set_edit_text("")
                edit.set_edit_pos(0)
                return None
            handled = restore(key)
            if handled is None:
                return None
            return original_keypress(size, key)
        edit.keypress = new_keypress

    # --- periodic refresh ---

    def _refresh(self) -> None:
        """Refresh every visible view against one coherent Codex generation."""
        index = getattr(self, "_codex_index", None)
        if isinstance(index, BackgroundCodexIndex):
            index.begin_read()
        try:
            self._refresh_impl()
        finally:
            if isinstance(index, BackgroundCodexIndex):
                index.end_read()

    def _refresh_impl(self) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_geometry_poll_at", 0.0) >= 0.5:
            self._last_geometry_poll_at = now
            # Remote terminals and mobile soft keyboards do not reliably
            # deliver a resize event to a hidden/zoomed controller pane.
            self._check_terminal_size()
        self._retry_pending_codex_recovery()
        self._scroll_manager.maintain()
        selection = getattr(self, "_selection_isolation_manager", None)
        if selection is not None:
            selection.maintain()
        self._reconcile_focus_from_tmux()
        self._retry_pending_divider_style()
        transport = self._display_transport()
        if transport.outer_session_lost():
            # A grouped keeper may still own the window after an external
            # ``kill-session railmux``. Return every real pane, use soft-exit
            # semantics, then let teardown remove only safe placeholders.
            self._soft_quit_flag = True
            transport.close_all()
            raise urwid.ExitMainLoop()
        rebound_for_client = False
        for slot in self._agent_workspace().slots:
            outcome = transport.fallback_for_external_client(slot)
            if outcome is None:
                continue
            if outcome.ok:
                rebound_for_client = True
                self._set_status(
                    "Using nested agent display: an external client attached",
                    "warn",
                )
            else:
                self._set_status(
                    outcome.reason or "could not handle an external tmux client",
                    "error",
                )
        if rebound_for_client:
            self._install_tmux_bindings()
        dead_display_agents = self._reap_dead_display_slots(transport)
        if dead_display_agents:
            for key in [
                key for key, running in self._running.items()
                if running.tmux_name in dead_display_agents
            ]:
                del self._running[key]
        self._consume_mode_refresh()
        mode_refresh_pending = self._mode_refresh_pending()
        mode = self._active_mode()
        needs_liveness = (
            any(slot.pane_id is not None
                for slot in self._agent_workspace().slots)
            or bool(self._running)
        )
        server = tmux_ctl.server_snapshot() if needs_liveness else None
        child_probes: dict[str, bool | None] = {}

        recovered_displayed = self._recover_unrepresented_displayed_agents(
            server)
        if recovered_displayed:
            self._set_status(
                "Recovered a displayed agent in Running."
                if recovered_displayed == 1 else
                f"Recovered {recovered_displayed} displayed agents in Running.",
                "warn",
            )

        def session_is_alive(name: str) -> bool:
            return self._agent_session_alive(name, server)

        def pane_is_alive(pane_id: str) -> bool:
            if server is not None:
                return pane_id in server.panes
            return tmux_ctl.pane_alive(pane_id)

        # A refresh may need several Codex views. Ask the single rate-limited
        # worker for a new immutable generation; every query below returns
        # immediately from the last known-good snapshot.
        refresh_codex = (
            mode.project_source == ProjectSource.CODEX
            or any(r.session_type == "codex" for r in self._running.values())
        )
        force_projects = any(r.is_placeholder for r in self._running.values())
        if refresh_codex:
            self._request_codex_refresh(force=force_projects)
        warning = (self._codex_index.take_warning()
                   if isinstance(self._codex_index, BackgroundCodexIndex)
                   else None)
        if warning:
            self._set_status(warning, "warn")

        # Refresh the Codex project filter so newly-created Codex sessions
        # make their cwd appear as a project in Codex mode.
        if mode.project_source == ProjectSource.CODEX:
            self._codex_project_filter = self._codex_index.all_cwds(refresh=False)
        # Placeholder resolution must discover its JSONL without extra delay.
        projects = self._visible_projects(
            force=force_projects and not mode_refresh_pending,
            allow_stale=mode_refresh_pending)
        self._projects_pane.set_projects(projects)
        if projects:
            # Re-resolve the live object after every project snapshot update.
            # If the mode was previously empty, this also restores its remembered
            # selection (or safely falls back to the first visible project).
            refreshed_selection = self._preferred_project(
                projects, self._selected_project)
            self._set_current_project(refreshed_selection)
        self._discover_legacy_running()
        # Prune dead tmux sessions (e.g. a provider exited via /quit).
        for key in list(self._running):
            if (not self._running[key].is_legacy
                    and not session_is_alive(self._running[key].tmux_name)):
                del self._running[key]

        self._reconcile_display_slots(session_is_alive, pane_is_alive)

        # Promote any `__new__-N` placeholders to their real session id — in
        # BOTH Claude and Codex mode. While a session stays a placeholder its
        # real-UUID row (filled from the on-disk scan) looks "not running", so
        # clicking it spawns a duplicate session; and `force_projects` above
        # stays stuck True, defeating the 3s project-scan cache. Codex
        # resolution must run too or neither ever clears.
        self._resolve_placeholders(projects)
        running_ids = self._running_session_ids()
        if self._selected_project is not None:
            matched = next((p for p in projects if p.encoded_name == self._selected_project.encoded_name), None)
            if matched is not None:
                self._set_current_project(matched)
                # #4: refine Codex sessions too (not just Claude) so the Sessions
                # pane agrees with the Running pane on each session's status dot.
                sessions = self._pane_sessions(
                    matched, refresh=False,
                    child_probes=child_probes, server=server)
                self._sessions_pane.set_sessions(matched, sessions, running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())
            else:
                self._set_current_project(None, remember=False)
                self._sessions_pane.set_sessions(None, [], running_ids=running_ids,
                                                  favorite_ids=self._favorites.get_ids())

        self._update_running_pane(child_probes, server)
        # Advance the status-bar state machine (TTL expiry + idle tip rotation)
        self._update_status()
        # Keep the hint bar showing only the keys valid for the focused pane.
        self._hint_bar.set_context(self._help_context())

    def _retry_pending_codex_recovery(self) -> None:
        """Settle generation-0 startup recovery on a coherent publication.

        ``_refresh`` has already pinned the background index generation around
        this call. Exact stamp/marker candidates remain visible even if the
        source is unavailable; only metadata-dependent legacy inference waits.
        """
        if not getattr(self, "_codex_recovery_pending", False):
            return
        index = self._codex_index
        snapshot = index.current_snapshot()
        generation = snapshot.generation
        if generation <= self._codex_recovery_generation:
            return

        # Generation-0 entries were intentionally visible before metadata was
        # ready. Re-adopt them from scratch so the first complete generation
        # can reject a wrong cwd and replace fallback labels/projects.
        provisional = {
            key: self._running[key]
            for key in self._codex_provisional_recovery_keys
            if key in self._running
        }
        for key in provisional:
            self._running.pop(key, None)
        self._codex_provisional_recovery_keys.clear()
        ok = self._discover_orphans(
            self._codex_recovery_state,
            allow_missing_codex_metadata=False,
        )
        report = snapshot.report
        if not getattr(self, "_last_orphan_probe_ok", True):
            self._running.update(
                (key, running) for key, running in provisional.items()
                if key not in self._running)
            self._codex_provisional_recovery_keys.update(provisional)
            return
        self._codex_recovery_generation = generation
        if not ok and report is not None and report.transient_errors:
            self._running.update(
                (key, running) for key, running in provisional.items()
                if key not in self._running)
            self._codex_provisional_recovery_keys.update(provisional)
            return
        # A clean filesystem generation can still predate an actively-written
        # rollout becoming indexable (especially across NFS). An exact tmux
        # stamp/state binding remains stronger evidence of live ownership than
        # a temporary metadata absence. Keep only those still-missing entries
        # visible and retry on the next generation; metadata that exists under
        # a different cwd is an explicit veto and is not restored here.
        missing_metadata = {
            key: running for key, running in provisional.items()
            if (key not in self._running
                and not key.startswith("__new__-")
                and index.get(key, refresh=False) is None)
        }
        if missing_metadata:
            self._running.update(missing_metadata)
            self._codex_provisional_recovery_keys.update(missing_metadata)
            return
        if (ok or report is None or report.transient_errors == 0):
            self._settle_codex_recovery(ok)

    def _settle_codex_recovery(self, ok: bool) -> None:
        self._codex_recovery_pending = False
        self._codex_recovery_state = None
        self._codex_provisional_recovery_keys.clear()
        self._running_recovery_ok = ok
        if self._loop is not None and self._pending_restore_state is not None:
            self._loop.set_alarm_in(0, self._restore_pending_right_pane)

    _HELP_CONTEXTS = (keymap.CTX_PROJECTS, keymap.CTX_SESSIONS, keymap.CTX_RUNNING)

    def _help_context(self) -> str:
        """Map the focused sidebar pane (0/1/2) to a keymap context name.

        Agent focus uses a compact context whose spatial arrows match the
        current layout and focused slot."""
        if not self._railmux_has_focus:
            workspace = self._agent_workspace()
            if workspace.secondary.is_open:
                primary = (
                    workspace.target_slot_key == AgentWorkspace.PRIMARY)
                if workspace.layout is WorkspaceLayout.SIDE_BY_SIDE:
                    if primary:
                        return keymap.CTX_AGENT_P1_SIDE_BY_SIDE
                    return keymap.CTX_AGENT_P2_SIDE_BY_SIDE
                if workspace.layout is WorkspaceLayout.STACKED:
                    if primary:
                        return keymap.CTX_AGENT_P1_STACKED
                    return keymap.CTX_AGENT_P2_STACKED
            return keymap.CTX_AGENT
        pos = self._sidebar.focus_position
        if 0 <= pos < len(self._HELP_CONTEXTS):
            return self._HELP_CONTEXTS[pos]
        return keymap.CTX_PROJECTS

    def _effective_status(
        self,
        meta: SessionMeta,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> str:
        """Displayed status, refined by the live process when we own the session.

        For a Claude session railmux has opened, a pending ``tool_use`` with a
        live child process means a tool is actively running (busy); no child
        means Claude is waiting for approval (blocked).  Codex is deliberately
        excluded: its pane has permanent wrapper/native/MCP children, so the
        same probe would report busy even while Codex is waiting for approval.
        Codex therefore keeps its JSONL age heuristic. Probe failures fall back
        to ``meta.status``. Used by both panes so the same session never shows
        two different dots.
        """
        if meta.pending_tool and meta.session_type != "codex":
            r = self._by_session_id(meta.session_id)
            if r is not None and not r.is_placeholder:
                if child_probes is not None and r.tmux_name in child_probes:
                    has_child = child_probes[r.tmux_name]
                else:
                    pane_pid = self._display_transport().displayed_real_pid(
                        r.tmux_name)
                    if pane_pid is None and server is not None:
                        pane_pid = server.pane_pid_for(r.tmux_name)
                    if pane_pid is not None:
                        has_child = tmux_ctl.process_has_child(pane_pid)
                    else:
                        has_child = tmux_ctl.session_has_child(r.tmux_name)
                    if child_probes is not None:
                        child_probes[r.tmux_name] = has_child
                if has_child is not None:
                    return "busy" if has_child else "blocked"
        return meta.status

    def _refine_status(
        self,
        meta: SessionMeta,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> SessionMeta:
        """Return `meta` with its status refined, copying only when it changes."""
        status = self._effective_status(meta, child_probes, server)
        return meta if status == meta.status else replace(meta, status=status)

    def _pane_sessions(
        self,
        project: Project,
        *,
        refresh: bool,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> list[SessionMeta]:
        """Sessions for the Sessions pane, from the right source and status-refined.

        - Codex mode → the Codex index (the Claude cache is empty for these).
        - #9: a synthetic Codex project (empty ``claude_dir``) must NEVER reach
          the Claude ``SessionCache`` — ``list_sessions`` would resolve
          ``claude_dir/<id>.jsonl`` relative to railmux's own cwd. Return [].
        - Otherwise the Claude ``SessionCache``.

        Every result runs through ``_refine_status`` (#4) so a session shows the
        SAME status dot here as in the Running pane, in both Claude and Codex
        modes (Running refines via ``_effective_status``; without this the raw
        ``SessionMeta.status`` could disagree)."""
        if self._active_mode().project_source == ProjectSource.CODEX:
            raw = self._codex_index.sessions_for_cwd(project.real_path, refresh=refresh)
        elif project.claude_dir == Path():
            raw = []
        else:
            raw = self._session_cache.list_sessions(project)
        return [self._refine_status(s, child_probes, server) for s in raw]

    def _update_running_pane(
        self,
        child_probes: dict[str, bool | None] | None = None,
        server: tmux_ctl.ServerSnapshot | None = None,
    ) -> None:
        """Sync labels/status and repopulate the Running pane."""
        for r in self._running.values():
            if r.is_placeholder or r.project is None:
                continue
            registered_mode = self._modes().for_tmux_name(r.tmux_name)
            session_type = (
                registered_mode.session_type if registered_mode else r.session_type)
            if session_type == "codex":
                meta = self._codex_index.get(
                    r.logical_session_id or r.key, refresh=False)
            else:
                meta = self._session_cache.get(
                    r.project, r.logical_session_id or r.key)
            if meta is not None:
                if meta.title:
                    r.label = f"{meta.project.display_name}/{meta.display_title}"
                r.status = self._effective_status(meta, child_probes, server)
                r.last_mtime = meta.last_mtime
                r.attention = meta.attention
                if self._agent_workspace().target.agent_tmux_name == r.tmux_name:
                    if meta.attention is None:
                        self._attention_notice_key = None
                    else:
                        notice_key = (
                            meta.session_id, meta.attention.event_order)
                        if self._attention_notice_key != notice_key:
                            self._attention_notice_key = notice_key
                            self._show_attention_status(meta.attention)
        self._maybe_resort_running()
        self._render_running_pane()

    def _maybe_resort_running(self) -> None:
        """Re-order the Running registry by recency, at most once per minute.

        The pane renders ``self._running`` in dict order, so reordering the
        dict reorders the pane.  Sorting on every poll would make rows jump
        under the cursor while the user clicks; throttling to
        ``_RUNNING_SORT_INTERVAL`` bubbles recently-active sessions to the top
        without churn.  Placeholders (no JSONL yet) sort by their launch time.
        Focus is restored by tmux name in ``set_running``, so the highlighted
        row follows its session across the reorder."""
        now = time.time()
        if now - self._running_sort_ts < _RUNNING_SORT_INTERVAL:
            return
        self._running_sort_ts = now
        self._running = dict(sorted(
            self._running.items(),
            key=lambda kv: (
                kv[1].status == "blocked",
                kv[1].last_mtime or kv[1].created_at,
            ),
            reverse=True,
        ))

    def _render_running_pane(self) -> None:
        """Render registry values without doing metadata or process I/O.

        In Claude mode only ``cc-*`` sessions are shown; in Codex mode only
        ``cx-*`` sessions are shown.  The other type's sessions still run,
        but they don't belong in the current view.
        """
        mode = self._active_mode()
        prefix = mode.tmux_prefix
        entries = [
            RunningEntry(
                tmux_name=r.tmux_name,
                label=r.label,
                project_label=(
                    r.project.display_name
                    if r.project is not None
                    else r.placeholder_path.name
                    if r.placeholder_path is not None
                    else ""
                ),
                provider_label=mode.label,
                status=r.status,
                attention=r.attention,
                identity_token=(r.orphan.creation_token
                                if r.orphan is not None else None),
                legacy=r.is_legacy,
            )
            for r in self._running.values()
            if r.tmux_name.startswith(prefix)
        ]
        self._running_pane.set_running(entries)

    def _resolve_placeholders(self, projects: list[Project]) -> None:
        """Re-key any `__new__-N` placeholder to its real session_id.

        For each live placeholder, look at its project's sessions and pick the
        newest one created after the placeholder timestamp whose session_id is
        neither already claimed by another running session NOR present in the
        cwd's pre-launch snapshot (#12) — the latter fences off a rollout that
        another codex/railmux process wrote to the same cwd, so we never bind a
        placeholder to a conversation we didn't launch.

        Works in both modes: Claude placeholders resolve against the Claude
        session cache, Codex placeholders against the Codex index (already
        walked once this refresh, so served snapshot-only).
        """
        placeholders = [
            r for r in self._running.values()
            if r.is_placeholder and not r.is_legacy
        ]
        if not placeholders:
            return
        # Index visible projects by real_path. A Claude placeholder becomes
        # resolvable once its first real session makes the project visible;
        # Codex New Project can use its in-memory synthetic project earlier.
        by_path = {p.real_path: p for p in projects}
        claimed = self._running_session_ids() | set(self._running)
        for r in placeholders:
            # Codex New Project owns an in-memory synthetic project before its
            # first rollout makes that cwd visible in the Codex index. Claude
            # placeholders continue to wait for discovery to supply the real
            # encoded project directory.
            project = by_path.get(r.placeholder_path) or r.project
            if project is None:
                continue
            registered_mode = self._modes().for_tmux_name(r.tmux_name)
            session_type = (
                registered_mode.session_type if registered_mode else r.session_type)
            if session_type == "codex":
                # Codex index was already refreshed once this tick (see
                # _refresh); serve from that snapshot rather than re-walking
                # the tree, and don't use the Claude-only session cache.
                sessions = self._codex_index.sessions_for_cwd(
                    project.real_path, refresh=False)
            else:
                sessions = self._session_cache.list_sessions(project)
            # Candidates: sessions that appeared in this cwd since our launch,
            # not already claimed and not pre-existing before launch.
            candidates = [
                s for s in sessions
                if s.session_id not in claimed
                and s.session_id not in r.pre_launch_ids  # another process's (#12)
                and s.last_mtime + 1.0 >= r.created_at
            ]
            # #12: prefer EXACT child→rollout correlation over the heuristic. The
            # codex process in this placeholder's pane holds its own rollout open;
            # its filename UUID is the exact session_id. This defeats the
            # staggered race where an UNRELATED codex wrote a rollout to the same
            # cwd first (which the "exactly one new rollout" heuristic mis-binds).
            candidate = None
            if (r.orphan is not None
                    and r.orphan.phase == "resolved"
                    and r.orphan.session_id is not None):
                exact = [s for s in candidates
                         if s.session_id == r.orphan.session_id]
                if len(exact) != 1:
                    continue
                candidate = exact[0]
            elif r.session_type == "codex":
                open_ids = self._correlate_codex_rollout(r)
                if open_ids is not None:
                    # procfs available → correlation is AUTHORITATIVE, and we
                    # must NOT fall back to the heuristic (that's what reopens
                    # the staggered race). Bind only the candidate whose id
                    # codex actually holds open; an empty set / no-match means
                    # codex hasn't opened its own rollout fd yet → WAIT for the
                    # next tick, never bind an unrelated rollout that appeared
                    # first (#12).
                    matches = [s for s in candidates
                               if s.session_id in open_ids]
                    if len(matches) == 1:
                        candidate = matches[0]  # exact
                    else:
                        continue  # not yet correlatable → wait, don't guess
                # else: open_ids is None → no procfs (macOS) → heuristic below.
            if candidate is None:
                # Heuristic fallback, used only where exact correlation is
                # impossible (no procfs, e.g. macOS) or for Claude placeholders.
                # Bind ONLY when exactly one new rollout appeared; if several
                # did, a concurrent codex/railmux is writing the same cwd and we
                # can't tell which is ours — leave the placeholder rather than
                # risk binding (and later resuming/deleting) the wrong one (#12).
                if not r.allow_heuristic_resolution:
                    continue
                if len(candidates) != 1:
                    continue
                candidate = candidates[0]
            # Marker-first commit makes a crash between persistence and the
            # in-memory re-key idempotent: startup adopts a resolved marker
            # directly under its validated UUID.
            if r.orphan is not None:
                resolved_marker = r.orphan.resolved(candidate.session_id)
                if not self._write_orphan_marker(resolved_marker):
                    continue
                r.orphan = resolved_marker
            # Re-key the entry from the placeholder to the real session_id.
            del self._running[r.key]
            r.key = candidate.session_id
            r.label = f"{candidate.project.display_name}/{candidate.display_title}"
            r.project = candidate.project
            r.placeholder_path = None
            r.created_at = 0.0
            self._running[candidate.session_id] = r
            self._stamp_running(r)
            claimed.add(candidate.session_id)
            displayed_slot = self._agent_workspace().slot_for_agent(r.tmux_name)
            if displayed_slot is not None:
                self._set_slot_active_target(
                    displayed_slot, candidate.session_id, r.tmux_name)
                if displayed_slot is self._agent_workspace().target:
                    self._set_current_project(candidate.project)

    def _correlate_codex_rollout(self, r: "_Running") -> set[str] | None:
        """Exact child→rollout correlation for a Codex placeholder (#12).

        Return the set of rollout UUIDs that the codex process in this
        placeholder's tmux pane (or its descendants) currently holds open under
        the codex sessions dir — the filename UUID is the placeholder's exact
        session_id. Returns ``None`` ONLY when correlation is impossible on this
        platform (no procfs, e.g. macOS) → caller may use the heuristic. On
        procfs it returns a set (EMPTY while codex/pane isn't ready yet) → caller
        must WAIT, not fall back. Best-effort: any failure degrades to ``None``
        (heuristic) rather than raising into the UI."""
        try:
            sessions_dir = self._codex_home_path() / "sessions"
            return tmux_ctl.session_rollout_ids(r.tmux_name, sessions_dir)
        except Exception:
            # On a procfs platform an inspection failure is ambiguity, not
            # evidence that procfs is unavailable. Falling back here could
            # adopt an external same-cwd writer.
            return set() if tmux_ctl.proc_fs_available() else None

    def _currently_focused_session_meta(self) -> SessionMeta | None:
        if not self._sessions_pane._walker:
            return None
        focus_w, _ = self._sessions_pane._walker.get_focus()
        from railmux.ui.sessions_pane import _SessionRow
        if isinstance(focus_w, _SessionRow):
            return focus_w.session
        return None

    def _find_session_meta(self, session_id: str, project: Project | None = None,
                           session_type: str = "claude") -> SessionMeta | None:
        """Look up session metadata by ID.

        Codex rows resolve via CodexIndex — a Codex synthetic project has an
        empty ``claude_dir``, so the Claude ``claude_dir/<id>.jsonl`` lookup
        would build a relative path and miss (Info modal shows no metadata).
        Claude rows keep the on-disk scan scoped to the project (#16)."""
        if session_type == "codex":
            return self._codex_index.get(session_id, refresh=False)
        if project is None:
            return None
        from railmux.session_index import _scan_session
        jsonl_path = project.claude_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return None
        return _scan_session(project, jsonl_path)

    # --- kill / delete session ---

    def _return_agent_before_kill(self, tmux_name: str) -> bool:
        """Detach a displayed agent into a stable empty slot before killing."""
        slot = self._agent_workspace().slot_for_agent(tmux_name)
        outcome = self._display_transport().prepare_kill(tmux_name)
        if outcome:
            if slot is not None:
                self._paint_slot_active_target(slot, None, None)
                self._set_railmux_focus(
                    self._railmux_has_focus, force_border=True)
            return True
        if slot is not None and slot.agent_tmux_name != tmux_name:
            # Swap return may have succeeded before painting the idle surface
            # failed. Reflect that partial but safe transition immediately.
            self._paint_slot_active_target(slot, None, None)
            self._set_railmux_focus(
                self._railmux_has_focus, force_border=True)
        self._set_status(
            getattr(outcome, "error", None)
            or f"could not safely prepare {tmux_name}; nothing was killed",
            "error",
        )
        return False

    def _on_kill_session(self) -> None:
        """Kill the running Claude process without deleting the JSONL file.

        Works from both Sessions pane (pos 1) and Running pane (pos 2).
        """
        pos = self._sidebar.focus_position
        if pos == 2:
            # Running pane — kill the focused running entry.
            from railmux.ui.running_pane import _RunningRow
            if not self._running_pane._walker:
                self._set_status("No running session selected.")
                return
            focus_w, _ = self._running_pane._walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._set_status("No running session selected.")
                return
            r = self._by_tmux(focus_w.entry.tmux_name)
            if r is None:
                self._set_status("Session not found in registry.")
                return
            self._kill_tmux_session(
                r.tmux_name, r.label, focus_w.entry.identity_token)
            return

        # Sessions pane (pos 1 or default).
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected.")
            return
        r = self._by_session_id(session.session_id)
        if r is None:
            self._set_status(f"'{session.display_title}' is not running.")
            return
        self._kill_tmux_session(
            r.tmux_name, session.display_title,
            r.orphan.creation_token if r.orphan is not None else None)

    def _on_delete_session(self) -> None:
        """Delete the focused session from the current pane (with confirmation)."""
        pos = self._sidebar.focus_position

        if pos == 1:
            # Sessions pane — delete the focused session (JSONL + tmux).
            session = self._currently_focused_session_meta()
            if session is None:
                self._set_status("No session selected to delete.")
                return
            title = session.display_title
            modal = DeleteConfirmModal(
                action="Delete session",
                session_name=title,
                detail=(
                    "The session file will be permanently removed from disk.\n"
                    "Its background tmux session will also be killed."
                ),
                on_confirm=lambda: self._do_delete_session(session),
                on_cancel=self._close_modal,
            )
            self._show_delete_confirm(modal)

        elif pos == 2:
            # Running pane — kill the detached tmux session.
            from railmux.ui.running_pane import _RunningRow
            running_walker = self._running_pane._walker
            if not running_walker:
                self._set_status("No running session selected.")
                return
            focus_w, _ = running_walker.get_focus()
            if not isinstance(focus_w, _RunningRow):
                self._set_status("No running session selected.")
                return
            entry = focus_w.entry
            r = self._by_tmux(entry.tmux_name)
            label = r.label if r else entry.tmux_name
            # Real session_id (and project) only exist once resolved — needed
            # to also delete the JSONL.
            session_id = (
                r.logical_session_id
                if r is not None and not r.is_legacy else None
            )
            project = r.project if r else None
            if session_id:
                detail = (
                    "The detached tmux session will be killed.\n"
                    "The session file will also be permanently deleted from disk."
                )
            else:
                detail = "The detached tmux session will be killed."
            modal = DeleteConfirmModal(
                action="Kill running session",
                session_name=label,
                detail=detail,
                on_confirm=lambda: self._do_kill_running(
                    entry.tmux_name, session_id, project,
                    entry.identity_token),
                on_cancel=self._close_modal,
            )
            self._show_delete_confirm(modal)

        else:
            self._set_status("Use d on a session row or running-entry row to delete.")

    def _do_delete_session(self, session: SessionMeta) -> None:
        """Delete a session completely, provider-aware: kill tmux, then either
        remove the Claude JSONL + session-env or ``codex delete`` the rollout,
        and refresh the UI so pane rows stay aligned."""
        self._close_modal()
        self._cleanup_session(
            session_id=session.session_id,
            jsonl_path=session.jsonl_path,
            label=session.display_title,
            session_type=session.session_type,
        )

    def _do_kill_running(self, tmux_name: str, session_id: str | None,
                         project: Project | None,
                         identity_token: str | None = None) -> None:
        """Kill a detached tmux session; delete its backing store if known.

        The provider comes from the registry entry (``cx-*`` vs ``cc-*``), never
        assumed to be Claude: a Codex session's rollout lives under CODEX_HOME,
        not ``project.claude_dir`` (which is empty for a synthetic Codex
        project), so building a ``claude_dir/<id>.jsonl`` path for it would be a
        relative path that deletes nothing real — or, worse, an unrelated
        same-named file in railmux's cwd (#1)."""
        self._close_modal()
        r = self._by_tmux(tmux_name)
        if r is not None and r.is_legacy:
            self._kill_tmux_session(tmux_name, r.label, identity_token)
            return
        session_type = r.session_type if r else "claude"
        jsonl_path: Path | None = None
        if (session_type != "codex" and session_id
                and not session_id.startswith("__new__-") and project):
            jsonl_path = project.claude_dir / f"{session_id}.jsonl"
        self._cleanup_session(
            session_id=session_id, jsonl_path=jsonl_path,
            tmux_name=tmux_name, label=tmux_name, session_type=session_type,
            identity_token=identity_token,
        )

    def _forget_running(self, session_id: str | None,
                        tmux_name: str | None) -> None:
        """Drop a session from the running registry by id and/or tmux name."""
        if session_id is not None:
            self._running.pop(session_id, None)
        if tmux_name is not None:
            for key in [k for k, r in self._running.items()
                        if r.tmux_name == tmux_name]:
                del self._running[key]

    def _cleanup_session(self, session_id: str | None = None,
                         jsonl_path: Path | None = None,
                         tmux_name: str | None = None,
                         label: str = "",
                         session_type: str = "claude",
                         identity_token: str | None = None) -> None:
        """Provider-aware session cleanup: kill tmux → remove backing store →
        refresh UI.

        Claude sessions unlink the JSONL and clean the Claude session-env +
        history index. Codex sessions are deleted through ``codex delete``
        against the resolved CODEX_HOME and never touch any Claude path (#1)."""
        # 1. Kill the detached tmux session first (avoid race conditions).
        if tmux_name is None and session_id is not None:
            r = self._by_session_id(session_id)
            tmux_name = r.tmux_name if r else None
        writer_pids: tuple[int, ...] = ()
        owned = self._by_tmux(tmux_name) if tmux_name is not None else None
        exact_pane = None
        if owned is not None and owned.orphan is not None:
            if not self._running_action_valid(owned, identity_token):
                self._set_status(
                    "Kill refused: the marked tmux identity changed", "error")
                return
            exact_pane = self._exact_running_pane(owned)
        if owned is not None and owned.is_legacy:
            self._set_status(
                "Deleting legacy history is disabled; use Kill, then restart "
                "the session from Railmux",
                "warn",
            )
            return
        if tmux_name and (exact_pane is not None
                          or (owned is None or owned.orphan is None)
                          and tmux_ctl.session_exists(tmux_name)):
            if not self._return_agent_before_kill(tmux_name):
                return
            writer_pids = tmux_ctl.session_process_ids(tmux_name)
            if exact_pane is not None:
                # Returning a swap-displayed pane changes its current session;
                # refresh and require the recorded home identity before kill.
                exact_pane = self._exact_running_pane(owned)
                killed = bool(
                    exact_pane is not None
                    and orphan_marker.same_live_tmux(owned.orphan, exact_pane)
                    and tmux_ctl.kill_session_identity(exact_pane)
                )
            else:
                killed = tmux_ctl.kill_session(tmux_name)
            # Never remove a rollout while its writer may still be alive. A
            # concurrent exit is fine; a session that still exists after the
            # failed kill is a hard stop for both Claude and Codex deletion.
            still_alive = (
                self._exact_running_pane(owned) is not None
                if owned is not None and owned.orphan is not None
                else tmux_ctl.session_exists(tmux_name)
            )
            if not killed and still_alive:
                self._set_status(
                    f"failed to stop {tmux_name}; nothing was deleted", "error")
                return
            if writer_pids and not tmux_ctl.wait_for_processes_exit(writer_pids):
                self._set_status(
                    f"{tmux_name} is still shutting down; nothing was deleted",
                    "error",
                )
                return

        if session_type == "codex":
            self._cleanup_codex_session(session_id, tmux_name, label)
            return

        # 2. Remove from our running-session registry.
        self._forget_running(session_id, tmux_name)

        # 3. Delete the JSONL file (conversation history). Do not report a
        # successful deletion when the filesystem rejected the unlink.
        if jsonl_path is not None:
            try:
                jsonl_path.unlink(missing_ok=True)
                invalidate_session(jsonl_path)
            except OSError as exc:
                self._session_cache.invalidate()
                self._invalidate_project_snapshot()
                self._refresh()
                self._set_status(f"failed to delete {jsonl_path}: {exc}", "error")
                return

        # 4. Remove Claude's session-env directory (session metadata).
        if session_id is not None and not session_id.startswith("__new__-"):
            claude_home = getattr(
                self, "_claude_home", Path.home() / ".claude")
            env_dir = claude_home / "session-env" / session_id
            if env_dir.is_dir():
                shutil.rmtree(env_dir, ignore_errors=True)

        # 5. Remove from Claude's history index so it doesn't recreate a
        #    metadata stub (Claude rebuilds missing JSONLs from this index).
        history_ok = True
        if session_id is not None and not session_id.startswith("__new__-"):
            claude_home = getattr(
                self, "_claude_home", Path.home() / ".claude")
            history_ok = self._remove_from_history(
                session_id, claude_home=claude_home) is not False

        # A writer outside the captured tmux process tree could race the first
        # unlink. Verify once after history cleanup; a surviving file means the
        # requested delete did not complete and must not be reported as such.
        if jsonl_path is not None and jsonl_path.exists():
            try:
                # Disappearance between exists() and unlink() is already the
                # desired end state, not a deletion failure.
                jsonl_path.unlink(missing_ok=True)
                invalidate_session(jsonl_path)
            except OSError as exc:
                self._session_cache.invalidate()
                self._invalidate_project_snapshot()
                self._refresh()
                self._set_status(
                    f"session was recreated and could not be deleted: {exc}",
                    "error",
                )
                return

        # 6. Invalidate caches and refresh so the UI reflects the deletion
        #    immediately — no stale rows that point to deleted sessions.
        self._session_cache.invalidate()
        self._invalidate_project_snapshot()
        self._refresh()

        deleted = (session_id is not None
                   and not session_id.startswith("__new__-")
                   and jsonl_path is not None)
        if not history_ok:
            self._set_status(
                f"Deleted: {label} (history index cleanup failed)", "warn")
        else:
            self._set_status(f"{'Deleted' if deleted else 'Killed'}: {label}")

    def _cleanup_codex_session(self, session_id: str | None,
                               tmux_name: str | None, label: str) -> None:
        """Codex arm of ``_cleanup_session`` (tmux already killed).

        A real rollout is removed via ``codex delete --force``; on failure the
        registry and index are left untouched and an error is shown, so the row
        stays put and we never falsely report 'Deleted'. A placeholder (no
        resolved UUID yet) has no rollout on disk, so killing the tmux session
        is the whole operation."""
        is_real = bool(session_id) and not session_id.startswith("__new__-")
        if is_real and not self._codex_delete(session_id):
            self._set_status(f"codex delete failed: {label}", "error")
            return
        self._forget_running(session_id, tmux_name)
        if isinstance(self._codex_index, BackgroundCodexIndex):
            self._codex_index.invalidate(
                tombstone=session_id if is_real else None)
        else:
            self._codex_index.invalidate()
        self._invalidate_project_snapshot()
        self._refresh()
        self._set_status(f"{'Deleted' if is_real else 'Killed'}: {label}")

    def _codex_delete(self, uuid: str) -> bool:
        """Run ``codex delete --force <UUID>`` against the resolved CODEX_HOME.

        Returns True only on a clean zero exit. Any failure (missing binary,
        non-zero exit, timeout) returns False so the caller keeps registry/index
        state and reports the error rather than a false success (#1)."""
        import os as _os
        import subprocess as _sp
        if not uuid or uuid.startswith("__new__-"):
            return False
        env = dict(_os.environ)
        env["CODEX_HOME"] = str(self._codex_home_path())
        try:
            result = _sp.run(
                [self._config.codex_binary, "delete", "--force", uuid],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, env=env, timeout=30,
            )
        except (OSError, _sp.TimeoutExpired):
            return False
        return result.returncode == 0

    @staticmethod
    def _remove_from_history(
        session_id: str, _attempts: int = 3,
        claude_home: Path | None = None,
    ) -> bool:
        """Strip every line referencing *session_id* from ~/.claude/history.jsonl.

        Claude Code uses this file as a session index — when a JSONL is deleted
        but the history entry remains, Claude rebuilds an empty metadata stub on
        the next launch.  Removing the entry prevents that.
        """
        history_path = ((claude_home or (Path.home() / ".claude"))
                        / "history.jsonl")
        if not history_path.is_file():
            return True
        try:
            source_stat = history_path.stat()
            lines = history_path.read_text().splitlines()
        except OSError:
            return False
        kept = []
        changed = False
        import json as _json
        for line in lines:
            line_s = line.strip()
            if not line_s:
                continue
            try:
                rec = _json.loads(line_s)
            except (ValueError, _json.JSONDecodeError):
                kept.append(line)
                continue
            if rec.get("sessionId") == session_id:
                changed = True
                continue
            kept.append(line)
        if changed:
            try:
                current_stat = history_path.stat()
                source_signature = (
                    source_stat.st_ino, source_stat.st_mtime_ns, source_stat.st_size)
                current_signature = (
                    current_stat.st_ino, current_stat.st_mtime_ns, current_stat.st_size)
                if current_signature != source_signature:
                    if _attempts > 1:
                        return App._remove_from_history(
                            session_id, _attempts - 1, claude_home)
                    return False
                atomic_write_text(
                    history_path, "\n".join(kept) + ("\n" if kept else ""))
            except OSError:
                return False
        return True

    # --- rename session ---

    def _on_rename_session(self) -> None:
        """Open the rename modal for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected to rename.")
            return
        modal = RenameModal(
            current_title=session.display_title,
            on_submit=lambda new_title, s=session: self._do_rename(s, new_title),
            on_cancel=self._close_modal,
        )
        self._show_rename_modal(modal)

    def _do_rename(self, session: SessionMeta, new_title: str) -> None:
        """Persist a rename for the session.

        The title is stored in railmux's own sidecar (``self._renames``) — the
        source of truth, immune to Claude Code rewriting its ai-title record
        every turn.  We *also* append an ai-title record to the JSONL so
        ``claude --resume``'s own picker reflects the rename until Claude
        re-titles.  An empty title clears the rename (reverts to the auto
        title)."""
        self._close_modal()
        new_title = new_title.strip()
        if not new_title:
            self._renames.clear(session.session_id)
            self._session_cache.invalidate()
            self._codex_index.invalidate()
            self._invalidate_project_snapshot()
            self._refresh()
            self._set_status("Rename cleared.")
            return
        self._renames.set(session.session_id, new_title)
        # Echo the rename into the session JSONL so `claude --resume`'s own
        # picker reflects it too (best-effort).  Claude-only — Codex rollout
        # files use a different schema, and appending Claude records would
        # change the mtime and pollute the file.
        synced = True
        if session.session_type == "claude":
            import json
            record = json.dumps({"type": "ai-title", "aiTitle": new_title})
            try:
                with session.jsonl_path.open("a") as f:
                    f.write(record + "\n")
            except OSError:
                synced = False
        # Invalidate the caches and refresh so both Sessions and Running
        # panes pick up the new title immediately.
        self._session_cache.invalidate()
        self._codex_index.invalidate()
        self._invalidate_project_snapshot()
        self._refresh()
        self._set_status(f"Renamed to: {new_title}" if synced
                         else f"Renamed to: {new_title} (transcript sync failed)")

    # --- toggle favorite ---

    def _on_toggle_star(self) -> None:
        """Toggle star status for the focused session."""
        session = self._currently_focused_session_meta()
        if session is None:
            self._set_status("No session selected.")
            return
        now_star = self._favorites.toggle(session.session_id)
        label = "★" if now_star else "unstarred"
        self._set_status(f"{label} {session.display_title}")

    # --- context menu (right-click) ---

    def _on_running_context_menu(self, entry: RunningEntry) -> None:
        r = self._by_tmux(entry.tmux_name)
        if r is None:
            return
        if (r.orphan is not None
                and not self._running_action_valid(r, entry.identity_token)):
            self._set_status(
                "Action refused: the unresolved tmux identity changed", "error")
            return
        self._running_pane.set_selected(entry.tmux_name)
        # Ensure tmux focus is on our pane so the 200 ms poll doesn't
        # auto-close the menu (can happen if focus was on the right pane).
        if self._railmux_pane_id:
            tmux_ctl.select_pane(self._railmux_pane_id)
        if r.is_placeholder or r.is_legacy:
            # Legacy rows retain their exact server identity here; routing
            # through the provider-session menu could select a same-id session
            # on the dedicated server instead.
            tmux = r.tmux_name
            label = r.label
            token = r.orphan.creation_token if r.orphan is not None else None
            items: list[tuple[str, Callable[[], None]]] = [
                (" Open      ↵", lambda: self._open_running_identity(
                    tmux, token)),
                (" Kill       k", lambda: self._kill_tmux_session(
                    tmux, label, token)),
            ]
            path = (r.project.real_path if r.project is not None
                    else r.placeholder_path)
            if path is not None:
                items.append(
                    (" Term       t", lambda: self._open_terminal_for_path(path)))
            menu = ContextMenu(items, on_close=self._close_modal)
            self._show_overlay(menu, width=36, height=13,
                               click_outside_to_close=True,
                               fixed_width=True, fixed_height=True)
            return
        session_id = r.logical_session_id
        session = (
            self._find_session_meta(session_id, r.project, r.session_type)
            if session_id is not None else None
        )
        if session is None:
            return
        self._open_session_context_menu(session)

    def _open_session_context_menu(self, session: SessionMeta) -> None:
        # Ensure tmux focus is on our pane so the 200 ms poll doesn't
        # auto-close the menu (can happen if focus was on the right pane).
        if self._railmux_pane_id:
            tmux_ctl.select_pane(self._railmux_pane_id)
        self._sessions_pane.set_selected_session(session.session_id)
        r = self._by_session_id(session.session_id)
        is_alive = r is not None and not r.is_placeholder
        is_starred = session.session_id in self._favorites.get_ids()
        items: list[tuple[str, Callable[[], None]]] = [
            (" Open      ↵", lambda s=session: self._do_context_open(s)),
            (" Preview    ␣", lambda s=session:
             self._on_session_row_preview(s)),
            (" Info       i", lambda s=session: self._do_context_info(s)),
            (" Rename     r", lambda s=session: self._do_context_rename(s)),
            (" Unstar    s" if is_starred else " Star      s",
             lambda s=session: self._do_context_star(s)),
            (" Kill       k", lambda s=session: self._do_context_kill(s)
             if is_alive else None),
            (" Term       t", lambda s=session: self._do_context_term(s)),
            (" Delete     d", lambda s=session: self._do_context_delete(s)),
        ]
        # Filter out None callbacks (e.g. Kill for non-running sessions).
        items = [(label, cb) for label, cb in items if cb is not None]
        menu = ContextMenu(items, on_close=self._close_modal)
        self._show_overlay(menu, width=36, height=15,
                           click_outside_to_close=True,
                           fixed_width=True, fixed_height=True)

    def _do_context_open(self, session: SessionMeta) -> None:
        self._on_session_select(session, steal_focus=True)

    def _do_context_rename(self, session: SessionMeta) -> None:
        modal = RenameModal(
            current_title=session.display_title,
            on_submit=lambda new_title, s=session: self._do_rename(s, new_title),
            on_cancel=self._close_modal,
        )
        self._show_rename_modal(modal)

    def _do_context_info(self, session: SessionMeta) -> None:
        r = self._by_session_id(session.session_id)
        running_label = None
        if r and self._agent_session_alive(r.tmux_name):
            running_label = f"detached as '{r.tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label,
                                 on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40,
                           click_outside_to_close=True)

    def _do_context_star(self, session: SessionMeta) -> None:
        now_star = self._favorites.toggle(session.session_id)
        self._session_cache.invalidate()
        self._refresh()
        label = "★" if now_star else "unstarred"
        self._set_status(f"{label} {session.display_title}")

    def _do_context_kill(self, session: SessionMeta) -> None:
        r = self._by_session_id(session.session_id)
        if r is not None:
            self._kill_tmux_session(
                r.tmux_name, session.display_title,
                r.orphan.creation_token if r.orphan is not None else None)

    def _open_running_identity(
        self, tmux_name: str, identity_token: str | None,
    ) -> None:
        running = self._by_tmux(tmux_name)
        if not self._running_action_valid(running, identity_token):
            self._set_status(
                "Open refused: the unresolved tmux identity changed", "error")
            return
        slot = self._agent_workspace().target
        if slot is self._primary_slot:
            self._attach_in_right_pane(tmux_name, steal_focus=True)
        else:
            self._attach_agent_slot(slot, tmux_name, steal_focus=True)

    def _kill_tmux_session(
        self, tmux_name: str, label: str,
        identity_token: str | None = None,
    ) -> None:
        """Kill a running tmux session by name (no SessionMeta needed)."""
        running = self._by_tmux(tmux_name)
        if running is not None and running.orphan is not None:
            pane = (self._exact_running_pane(running)
                    if self._running_action_valid(running, identity_token)
                    else None)
            if pane is None:
                self._set_status(
                    "Kill refused: the unresolved tmux identity changed",
                    "error",
                )
                return

        # A resolved session can be displayed just as an orphan can. Always
        # detach the agent first so killing it cannot strand a swap marker or
        # leave a nested client owning the outer pane.
        if not self._return_agent_before_kill(tmux_name):
            return

        killed = True
        if running is not None and running.is_legacy:
            killed = bool(
                running.legacy_server is not None
                and running.legacy_session_id is not None
                and tmux_server.kill_target_session(
                    running.legacy_server, running.legacy_session_id)
            )
        elif running is not None and running.orphan is not None:
            pane = self._exact_running_pane(running)
            if (pane is None
                    or not orphan_marker.same_live_tmux(running.orphan, pane)):
                self._set_status(
                    "Kill refused: the marked pane did not return home", "error")
                return
            killed = tmux_ctl.kill_session_identity(pane)
        elif tmux_ctl.session_exists(tmux_name):
            killed = tmux_ctl.kill_session(tmux_name)

        if running is not None and running.is_legacy:
            still_alive = self._agent_session_alive(running.tmux_name)
        else:
            still_alive = (
                self._exact_running_pane(running) is not None
                if running is not None and running.orphan is not None
                else tmux_ctl.session_exists(tmux_name)
            )
        if running is not None and running.is_legacy and not killed:
            self._set_status(
                "Kill failed: the legacy tmux identity could not be confirmed; "
                "the session remains in Running",
                "error",
            )
            return
        if not killed and still_alive:
            self._set_status(
                "Kill failed: exact tmux session is still live", "error")
            return
        # Remove any _running entry keyed by this tmux name.
        for key in [k for k, r in self._running.items() if r.tmux_name == tmux_name]:
            del self._running[key]
        self._refresh()
        self._set_status(f"Killed: {label}  (file kept)")

    def _open_terminal_for_project(self, project: Project) -> None:
        """Open a terminal in the given project directory."""
        self._open_terminal_for_path(project.real_path)

    def _open_terminal_for_path(self, path: Path) -> None:
        """Open a terminal in *path*, including unresolved new projects."""
        import os
        import shlex
        import subprocess as _sp
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(path))} && exec {shlex.quote(shell)}"
        pane_id = self._sync_target_slot_from_tmux().pane_id
        target = pane_id if (pane_id and tmux_ctl.pane_alive(pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        _sp.run(["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        tmux_ctl.select_pane(new_pane)
        self._set_railmux_focus(False)
        self._set_status(f"terminal: {path.name or path}")

    def _do_context_term(self, session: SessionMeta) -> None:
        import os
        import shlex
        import subprocess as _sp
        shell = os.environ.get("SHELL", "/bin/bash")
        cmd = f"cd {shlex.quote(str(session.project.real_path))} && exec {shlex.quote(shell)}"
        pane_id = self._sync_target_slot_from_tmux().pane_id
        target = pane_id if (pane_id and tmux_ctl.pane_alive(pane_id)) else None
        new_pane = tmux_ctl.split_window_v(cmd, target=target)
        if not new_pane:
            self._set_status("failed to split for terminal")
            return
        _sp.run(["tmux", "set-option", "-p", "-t", new_pane, "remain-on-exit", "off"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        tmux_ctl.select_pane(new_pane)
        self._set_railmux_focus(False)
        self._set_status(f"terminal: {session.project.display_name}")

    def _do_context_delete(self, session: SessionMeta) -> None:
        title = session.display_title
        modal = DeleteConfirmModal(
            action="Delete session",
            session_name=title,
            detail=(
                "The session file will be permanently removed from disk.\n"
                "Its background tmux session will also be killed."
            ),
            on_confirm=lambda s=session: self._do_delete_session(s),
            on_cancel=self._close_modal,
        )
        self._show_delete_confirm(modal)

    # --- resize divider ---

    def _resize_divider(self, expand_railmux: bool) -> None:
        """Move the vertical divider: [ shrinks railmux, ] expands it."""
        if (self._agent_workspace().presentation
                is WorkspacePresentation.COMPACT):
            self._set_status(
                "Divider resizing is available again in wide view.", "tip")
            return
        pane_id = self._primary_slot.pane_id
        if not pane_id or not tmux_ctl.pane_alive(pane_id):
            self._set_status("No agent pane to resize against.")
            return
        direction = "-R" if expand_railmux else "-L"
        if tmux_ctl.resize_pane(pane_id, direction, 5):
            sidebar_id = getattr(self, "_railmux_pane_id", None)
            window = (
                tmux_ctl.window_size(sidebar_id) if sidebar_id else None)
            sidebar = (
                tmux_ctl.pane_size(sidebar_id) if sidebar_id else None)
            if window is not None and sidebar is not None and window[0] > 0:
                self._active_sidebar_permille = min(
                    800,
                    max(50, round(sidebar[0] * 1000 / window[0])),
                )
                self._layout_geometry_user_owned = True
                self._layout_profile_fallback = False
        self._check_agent_slot_size(self._primary_slot)

    # --- responsive-size guard ------------------------------------------

    @classmethod
    def _terminal_size_class(cls, width: int, height: int) -> str:
        min_width, min_height = cls._MINIMUM_TERMINAL_SIZE
        rec_width, rec_height = cls._RECOMMENDED_TERMINAL_SIZE
        if width < min_width or height < min_height:
            return "critical"
        if width < rec_width or height < rec_height:
            return "reduced"
        return "comfortable"

    def _workspace_size(self) -> tuple[int, int] | None:
        """Return the full Railmux workspace size, not the sidebar TTY size."""
        pane_id = getattr(self, "_railmux_pane_id", None)
        if pane_id:
            size = tmux_ctl.window_size(pane_id)
            if size is not None:
                return size
        try:
            return self._loop.screen.get_cols_rows() if self._loop else None
        except Exception:
            return None

    def _wide_layout_fits_geometry(
        self,
        width: int,
        height: int,
        *,
        exit_margin: bool = False,
    ) -> bool:
        """Whether the saved dual layout remains usable without page zoom.

        This estimates the unzoomed agent region from the full window instead
        of sampling pane sizes: while compact, the selected pane reports the
        full zoomed geometry. A small exit margin prevents resize jitter from
        toggling presentation at the exact minimum.
        """
        workspace = self._agent_workspace()
        if workspace.layout is WorkspaceLayout.SINGLE:
            return True
        sidebar_width = self._sidebar_width_for_layout(
            workspace.layout,
            width,
            getattr(self, "_active_sidebar_permille", None),
        )
        agent_region = (max(0, width - sidebar_width - 1), height)
        pane_width, pane_height = projected_agent_size(
            agent_region, workspace.layout)
        min_width, min_height = self._MINIMUM_AGENT_PANE_SIZE
        if exit_margin:
            min_width += 2
            min_height += 1
        return pane_width >= min_width and pane_height >= min_height

    def _responsive_presentation(
        self, width: int, height: int,
    ) -> tuple[WorkspacePresentation, bool]:
        """Choose presentation and report a dual-layout space constraint."""
        workspace = self._agent_workspace()
        geometry_choice = presentation_for_geometry(
            workspace.presentation, width, height)
        if geometry_choice is WorkspacePresentation.COMPACT:
            return geometry_choice, False
        layout_fits = self._wide_layout_fits_geometry(
            width,
            height,
            exit_margin=(
                workspace.presentation is WorkspacePresentation.COMPACT),
        )
        if layout_fits:
            return WorkspacePresentation.WIDE, False
        return WorkspacePresentation.COMPACT, True

    def _check_terminal_size(
        self, size: tuple[int, int] | None = None,
    ) -> None:
        """Warn on outer-workspace size transitions without blocking input."""
        if size is None:
            size = self._workspace_size()
        if not size:
            return
        width, height = size
        if width <= 0 or height <= 0:
            return
        size_changed = (
            getattr(self, "_last_workspace_size", None) != (width, height))
        # Publish geometry before a presentation transition repaints the bar;
        # its responsive status tier must use this resize, not the prior width.
        self._last_workspace_size = (width, height)
        workspace = self._agent_workspace()
        presentation, layout_constrained = self._responsive_presentation(
            width, height)
        presentation_changed = presentation is not workspace.presentation
        if presentation_changed:
            self._set_workspace_presentation(presentation)
        elif (size_changed and workspace.presentation
              is WorkspacePresentation.COMPACT):
            self._apply_tmux_bar(self._tmux_error_bar)
        if (workspace.presentation is WorkspacePresentation.COMPACT
                and not self._window_is_zoomed()):
            # Retry a transient failed zoom and heal manual/unexpected unzoom;
            # compact presentation is defined by exactly one visible page.
            self._restore_compact_page()
        previous = getattr(self, "_last_size_class", None)
        current = self._terminal_size_class(width, height)
        if current != previous:
            self._last_size_class = current
            rec_width, rec_height = self._RECOMMENDED_TERMINAL_SIZE
            if current == "critical":
                self._set_status(
                    f"Workspace {width}×{height} is too small for a friendly layout; "
                    f"use at least {rec_width}×{rec_height} when possible.",
                    "error",
                    force=True,
                )
            elif current == "reduced":
                if workspace.presentation is WorkspacePresentation.COMPACT:
                    self._set_status(
                        "Compact view: use the status page buttons or "
                        "Ctrl-B Tab to move between Railmux and the Target "
                        "agent.",
                        "info",
                        force=True,
                    )
                else:
                    self._set_status(
                        f"Workspace {width}×{height} is cramped; "
                        f"{rec_width}×{rec_height} or larger is recommended.",
                        "warn",
                        force=True,
                    )
            elif previous in ("critical", "reduced"):
                self._set_status(
                    f"Workspace size restored: {width}×{height}.", "info",
                    force=True)
        if (presentation_changed and layout_constrained
                and current == "comfortable"):
            self._set_status(
                "Compact view: the dual-pane layout needs more space; "
                "resize wider/taller to restore both panes.",
                "info",
                force=True,
            )
        if size_changed:
            repeat_agent_warning = (
                current == "comfortable"
                and previous in ("critical", "reduced"))
            for slot in self._agent_workspace().slots:
                if slot.pane_id:
                    self._check_agent_slot_size(
                        slot, repeat_warning=repeat_agent_warning)

    def _check_agent_slot_size(
        self, slot: AgentSlot, *, repeat_warning: bool = False,
    ) -> None:
        """Warn when an individual agent display area is too small."""
        if not slot.pane_id:
            return
        workspace = self._agent_workspace()
        if workspace.presentation is WorkspacePresentation.COMPACT:
            visible_slot = {
                WorkspacePage.PRIMARY: workspace.primary,
                WorkspacePage.SECONDARY: workspace.secondary,
            }.get(workspace.compact_page)
            if slot is not visible_slot:
                # Hidden panes retain their narrow unzoomed rectangle, which
                # is not the viewport the user receives when selecting them.
                return
        size = tmux_ctl.pane_size(slot.pane_id)
        if size is None:
            return
        width, height = size
        min_width, min_height = (
            (40, 12)
            if workspace.presentation is WorkspacePresentation.COMPACT
            else self._MINIMUM_AGENT_PANE_SIZE
        )
        rec_width, rec_height = self._RECOMMENDED_AGENT_PANE_SIZE
        if width < min_width or height < min_height:
            current = "critical"
        elif width < rec_width or height < rec_height:
            current = "reduced"
        else:
            current = "comfortable"
        previous = slot.last_size_class
        slot.last_size = size
        if current == previous and not repeat_warning:
            return
        slot.last_size_class = current
        if current == "critical":
            self._set_status(
                f"Agent pane {width}×{height} is too small; "
                f"aim for at least {rec_width}×{rec_height}.",
                "error", force=True,
            )
        elif current == "reduced":
            self._set_status(
                f"Agent pane {width}×{height} may render poorly; "
                f"{rec_width}×{rec_height} or larger is recommended.",
                "warn", force=True,
            )
        elif previous in ("critical", "reduced"):
            self._set_status(
                f"Agent pane size restored: {width}×{height}.",
                "info", force=True,
            )

    # --- status bar ---

    @staticmethod
    def _attention_status_text(attention: AttentionState) -> str:
        raw_category = getattr(attention.category, "value", attention.category)
        category = str(raw_category).replace("_", " ")
        if attention.retryable is True:
            retry = "Retrying is likely safe."
        elif attention.retryable is False:
            retry = "Retry is unlikely to help."
        else:
            retry = "Retry suitability is unknown."
        return f"! {category}: {attention.summary} {retry}"

    def _show_attention_status(
        self, attention: AttentionState | None,
    ) -> bool:
        """Explain an active/selected outcome without changing its actions."""
        if not isinstance(attention, AttentionState):
            return False
        self._set_status(
            self._attention_status_text(attention), "warn", force=True)
        return True

    # How long an explicit message holds the bar before it falls back to idle
    # tips. Errors are sticky (cleared only by the next message or action);
    # warnings linger; routine info is brief. Tips rotate on their own cadence.
    _STATUS_TTL = {"error": None, "warn": 12.0, "info": 6.0}
    _TIP_INTERVAL = 20.0

    # Minimum time a message is protected from being overwritten by a *lower*
    # severity message, so a genuine error/warning isn't clobbered by a routine
    # "Project: …" the very next tick. A message of equal-or-higher severity
    # always wins immediately. Info has no floor (routine, freely replaceable).
    _STATUS_MIN_HOLD = {"error": 4.0, "warn": 2.0}
    _LEVEL_PRIORITY = {"tip": 0, "info": 1, "warn": 2, "error": 3}

    def _render_status_to_tmux(self, text: str, level: str = "info",
                               refresh: bool = True) -> None:
        """Render the current status line into the outer tmux status bar.

        This is railmux's only status surface — there is no in-pane status widget.
        The tmux bar is full terminal width, so far more fits on one line than
        the old ~30%-wide sidebar bar could show. Best-effort — a tmux hiccup
        must never raise into the UI.

        When *refresh* is False the ``set-option status-right`` is sent but
        ``refresh-client -S`` is skipped.  Callers use this to clear a stale
        status message while the user is typing in the right agent pane, where
        ``-S`` would briefly jitter the CJK preedit box.

        tmux runs status strings through BOTH its own ``#{...}``/``#[...]``/
        ``#(...)`` format expansion AND strftime, so a literal ``#`` must be
        doubled to ``##`` and a literal ``%`` to ``%%`` or paths/percentages get
        mangled (verified: ``#{x}`` expands to empty, ``%%`` collapses to ``%``).
        The per-level style prefix is added AFTER escaping so its ``#[`` is kept
        as a real style directive rather than doubled into literal text.

        ``refresh-client -S`` forces an immediate status-line redraw: tmux only
        auto-repaints the bar every ``status-interval`` seconds (default 15), so
        without it a short-lived status message (info TTL 6s) would usually be
        overwritten by the next idle tip before it ever became visible — only
        long-lived tips would show.
        """
        if not self._tmux_status_enabled or not self._tmux_status_session:
            return
        # Flip the WHOLE bar (bg + brand) to red on error, back to green otherwise,
        # but only on the transition so a held/idle re-render isn't churn.
        want_error = level == "error"
        if want_error != self._tmux_error_bar:
            self._apply_tmux_bar(want_error)
            self._tmux_error_bar = want_error
        safe = text.replace("#", "##").replace("%", "%%")
        style = _TMUX_LEVEL_STYLE.get(level, "")
        payload = f"{style}{safe}#[default]" if style else safe
        try:
            import subprocess as _sp
            _sp.run(
                ["tmux", "set-option", "-t", self._tmux_status_session,
                 "status-right", payload],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            if refresh:
                _sp.run(
                    ["tmux", "refresh-client", "-S"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
        except Exception:
            pass

    def _apply_tmux_bar(self, error: bool) -> None:
        """Set the whole-bar background (status-style) + brand (status-left) for
        the normal (green) or error (dark red) mode. Called from run() for the
        initial paint, from _render_status_to_tmux on the normal↔error
        transition, and from mode cycling so the mode indicator repaints.
        Best-effort — a tmux hiccup must not raise into the UI."""
        if not self._tmux_status_enabled or not self._tmux_status_session:
            return
        bar = _TMUX_BAR_STYLE_ERROR if error else _TMUX_BAR_STYLE_NORMAL
        workspace = getattr(self, "_workspace", None)
        left_length = 40
        right_length = self._TMUX_STATUS_RIGHT_LENGTH
        if (workspace is not None
                and workspace.presentation
                is WorkspacePresentation.COMPACT):
            width = (getattr(self, "_last_workspace_size", None) or (80, 24))[0]
            manager = getattr(self, "_tmux_binding_manager", None)
            range_helper = getattr(tmux_ctl, "status_pane_range", None)
            wrap = None
            if (getattr(manager, "status_navigation_available", False)
                    is True and callable(range_helper)):
                def wrap(pane_id: str, content: str) -> str:
                    try:
                        return range_helper(pane_id, content)
                    except (TypeError, ValueError):
                        return content
            brand, visible = _compact_tmux_status_left(
                error,
                self._active_mode().label,
                workspace.compact_page,
                (
                    getattr(self, "_railmux_pane_id", None),
                    workspace.primary.pane_id,
                    workspace.secondary.pane_id,
                ),
                width,
                wrap,
            )
            left_length = max(1, visible)
            # Let tmux truncate the original status/tip text naturally inside
            # the cells left after compact navigation. The source tip pool is
            # unchanged; widening the terminal reveals more of the same text.
            right_length = max(1, width - left_length)
        else:
            brand = _tmux_status_left(
                error,
                self._active_mode().label,
                self._status_layout_indicator(),
            )
        try:
            import subprocess as _sp
            for opt, val in (
                ("status-style", bar),
                ("status-left", brand),
                ("status-left-length", str(left_length)),
                ("status-right-length", str(right_length)),
            ):
                _sp.run(
                    ["tmux", "set-option", "-t", self._tmux_status_session,
                     opt, val],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
            # Force an immediate repaint (default status-interval is 15s) so a
            # mode toggle or error flip shows at once, not on the next tick.
            _sp.run(["tmux", "refresh-client", "-S"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass

    def _status_layout_indicator(self) -> str | None:
        """Compact visible layout with its filled half naming the Target pane."""
        workspace = getattr(self, "_workspace", None)
        if workspace is None or workspace.primary.pane_id is None:
            return None
        secondary = workspace.target_slot_key == AgentWorkspace.SECONDARY
        if workspace.layout is WorkspaceLayout.SIDE_BY_SIDE:
            return "◨" if secondary else "◧"
        if workspace.layout is WorkspaceLayout.STACKED:
            return "⬓" if secondary else "⬒"
        return "▣"

    def _set_status(self, msg: str, level: str | None = None, *,
                    force: bool = False) -> None:
        """Show an explicit status message.

        ``level`` is auto-classified from the message prefix when omitted, so
        the ~40 existing call sites keep working unchanged: ``ERROR…`` → error,
        ``WARNING…``/``Failed…``/``failed…`` → warn, everything else → info.
        """
        if level is None:
            if msg.startswith("ERROR"):
                level = "error"
            elif msg.startswith(("WARNING", "Failed", "failed")):
                level = "warn"
            else:
                level = "info"
        # Don't let a still-fresh higher-severity message be overwritten by a
        # lower-severity one within its minimum-hold window.
        if not force and self._status_text is not None:
            hold = self._STATUS_MIN_HOLD.get(self._status_level)
            if (hold is not None
                    and time.monotonic() - self._status_since < hold
                    and self._LEVEL_PRIORITY.get(level, 1)
                    < self._LEVEL_PRIORITY.get(self._status_level, 1)):
                return
        self._status_text = msg
        self._status_level = level
        self._status_since = time.monotonic()
        self._render_status_to_tmux(msg, level)

    def _update_status(self) -> None:
        """Advance the status-bar state machine once per tick.

        Holds an explicit message for its TTL, then falls back to cycling idle
        tips. Called from ``_refresh`` instead of the old unconditional
        ``set_message`` that clobbered one-shot messages every poll.
        """
        now = time.monotonic()
        if self._status_text is not None:
            ttl = self._STATUS_TTL.get(self._status_level, 6.0)
            if ttl is None or now - self._status_since < ttl:
                return
            # Expired → clear immediately so a tip replaces the stale message
            # on the bar.  Use refresh=False when railmux doesn't have focus
            # (the user is typing in the right agent pane) so refresh-client -S
            # doesn't jitter the CJK preedit box — set-option alone is enough
            # to clear the old text; tmux will paint the new value on its next
            # status-interval cycle or another focus-driven redraw.
            self._status_text = None
            if TIPS:
                refresh = self._railmux_has_focus
                self._render_status_to_tmux(
                    TIPS[self._tip_index], "tip", refresh=refresh)
                self._tip_index = (self._tip_index + 1) % len(TIPS)
                # The tip was rendered above; start its full cadence now so the
                # next poll does not immediately replace it with a second tip.
                self._tip_since = now
            else:
                self._tip_since = 0.0
            return
        # Idle: rotate tips on their own cadence.
        if not TIPS:
            return
        if self._tip_since == 0.0 or now - self._tip_since >= self._TIP_INTERVAL:
            # Only repaint the shared tmux status bar when railmux has focus.
            # When the user is typing in the right agent pane, refresh-client -S
            # inside _render_status_to_tmux makes the CJK preedit box jump.
            # The counter still advances so tips don't stall during long typing
            # sessions — the next tip appears as soon as focus returns.
            if self._railmux_has_focus:
                self._render_status_to_tmux(TIPS[self._tip_index], "tip")
            self._tip_index = (self._tip_index + 1) % len(TIPS)
            self._tip_since = now

    # --- periodic refresh ---

    def _on_tick(self, loop, _user_data) -> None:
        self._refresh()
        if self._pending_restore_state is not None:
            # A portable Codex preview may have been waiting for the first
            # immutable history generation even when no live recovery
            # candidate existed. Retry after refresh; the restore callback's
            # readiness checks keep generation zero non-blocking.
            loop.set_alarm_in(0, self._restore_pending_right_pane)
        # When a click-outside overlay is showing OR we're in history mode
        # (less running in the right pane), poll faster so the user sees a
        # quick response when pressing q in less or clicking the right pane.
        fast_poll = (
            any(slot.in_history_mode for slot in self._agent_workspace().slots)
            or (self._railmux_pane_id is not None
                and self._loop is not None
                and isinstance(self._loop.widget, _CloseOnClickOverlay))
        )
        if fast_poll:
            if (self._railmux_pane_id is not None
                    and self._loop is not None
                    and isinstance(self._loop.widget, _CloseOnClickOverlay)):
                if tmux_ctl.current_pane_id() != self._railmux_pane_id:
                    self._close_modal()
            interval_s = 0.2
        else:
            interval_s = self._config.poll_interval_ms / 1000.0
        loop.set_alarm_in(interval_s, self._on_tick)

    # --- lifecycle ---

    def run(self) -> None:
        # - mouse on: tmux switches pane focus on clicks so keyboard input
        #   tracks the active pane and the border colour updates accordingly.
        # - set-clipboard on: text selection in either pane is copied to the
        #   system clipboard.
        import subprocess as _sp
        # Wrap the whole setup (not just loop.run) so `finally` reverts our tmux
        # status-bar overrides even if Screen()/MainLoop() construction raises
        # after we've mutated the outer session — otherwise the user's bar would
        # keep railmux's `status on`, style, brand and blanked window-list.
        try:
            if tmux_ctl.in_tmux():
                sess = tmux_ctl.current_session_name() or "railmux"
                _sp.run(
                    ["tmux", "set-option", "-t", sess, "set-clipboard", "on"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
                # Force OSC 52 passthrough so a left-drag selection in either pane
                # copies to the *local* system clipboard (works over SSH / nested
                # tmux on OSC-52-capable terminals). Pairs with set-clipboard on.
                tmux_ctl.enable_clipboard_passthrough()
                _sp.run(
                    ["tmux", "set-option", "-t", sess, "focus-events", "on"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
                _sp.run(
                    ["tmux", "set-option", "-t", sess, "mouse", "on"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
                wheel = getattr(self, "_root_wheel_manager", None)
                if wheel is not None and not wheel.open():
                    self._set_status(
                        "Mouse-wheel forwarding unavailable; tmux may have "
                        "custom root wheel bindings.",
                        "warn",
                    )
                # Shared binding ownership below scopes right-click forwarding
                # to Railmux windows and preserves the user's original command
                # everywhere else. Left-click keeps tmux's stock
                # select-pane-and-forward behavior.
                # The outer tmux status bar is now railmux's only status surface (the
                # in-pane StatusBar was removed). Apply the static options (window
                # list blanked, bar forced on, length cap) here; the bar background
                # + brand are set by _apply_tmux_bar (green now, dark red on error).
                # All session-scoped + reverted on teardown, so the user's global
                # tmux config is untouched.
                for opt, val in self._TMUX_BAR_OPTIONS:
                    _sp.run(
                        ["tmux", "set-option", "-t", sess, opt, val],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    )
                self._tmux_status_session = sess
                self._tmux_status_enabled = True
                self._apply_tmux_bar(error=False)  # initial green bar

            self._railmux_pane_id = tmux_ctl.current_pane_id()
            if (self._railmux_pane_id is not None
                    and tmux_ctl.current_session_name() == "railmux"
                    and not tmux_ctl.use_smallest_window_size(
                        self._railmux_pane_id)):
                self._set_status(
                    "Could not enable stable multi-terminal sizing.", "warn")
            self._set_railmux_focus(True, force_border=True)
            self._install_tmux_bindings()
            # bracketed_paste_mode: the terminal frames pastes in begin/end markers
            # so _filter_input can drop them — sidebar keys are destructive commands,
            # not text input.
            screen = urwid.raw_display.Screen(
                focus_reporting=True, bracketed_paste_mode=True
            )
            self._loop = urwid.MainLoop(
                self._frame,
                palette=PALETTE,
                screen=screen,
                input_filter=self._filter_input,
                unhandled_input=self._on_input,
            )
            from railmux.ui._widgets import ClickableRow
            ClickableRow._main_loop = self._loop
            self._hint_bar.set_loop(self._loop)
            self._button_bar.set_loop(self._loop)
            if self._active_mode().prompt_for_auto_run:
                self._maybe_prompt_codex_yolo()
            try:
                # Urwid stores both exact RGB and an automatically downsampled
                # fallback; tmux/terminal capabilities decide which is emitted.
                self._loop.screen.set_terminal_properties(colors=_TERMINAL_COLORS)
            except Exception:
                pass
            self._check_terminal_size()
            # Intercept Ctrl-C as a regular keypress so we can show a confirm-quit
            # popup instead of slamming out via SIGINT. Ctrl-\ (quit) is left
            # active as an emergency hard-exit.
            try:
                self._loop.screen.tty_signal_keys(intr="undefined")
            except Exception:
                pass
            # Right pane is created lazily on first session launch — startup is
            # railmux-only, no empty pane.
            self._loop.set_alarm_in(self._config.poll_interval_ms / 1000.0, self._on_tick)
            if self._pending_project is not None:
                self._loop.set_alarm_in(
                    0.05, self._load_pending_project)
            if self._pending_restore_state is not None:
                self._loop.set_alarm_in(
                    0.1, self._restore_pending_right_pane)
            self._loop.run()
        except KeyboardInterrupt:
            # Ctrl-C / SIGINT — fall through to teardown.
            pass
        finally:
            # Always clean up tmux, regardless of how (or how early) we exited.
            self._teardown_tmux()
