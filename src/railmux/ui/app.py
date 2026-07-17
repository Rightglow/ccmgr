"""Top-level urwid app: provider sidebar + tmux agent workspace.

Railmux runs in the left pane of a tmux window. The current release exposes one
agent pane; its state already lives in a bounded two-slot workspace so a second
pane can be enabled without duplicating provider/display state. Press `i` for a
session-info popup.
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

from railmux import orphan_marker, tmux_ctl
from railmux.atomic_file import atomic_write_text
from railmux.codex_index import CodexIndex
from railmux.config import Config
from railmux.display_transport import (
    AgentDisplayTransport,
    recover_interrupted_swaps,
)
from railmux.discovery import invalidate_session, list_projects
from railmux.favorites import Favorites
from railmux.settings import Settings
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
from railmux.renames import Renames
from railmux import restart_state
from railmux.session_cache import SessionCache
from railmux.scroll_manager import ScrollManager
from railmux.ui import keymap
from railmux.ui.modals import (
    ContextMenu,
    DeleteConfirmModal,
    HelpModal,
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
from railmux.ui.statusbar import ButtonBar, HintBar, TIPS
from railmux.ui.workspace import (
    AgentSlot,
    AgentWorkspace,
    SlotRestoreState,
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
    ("status_error", "light red,bold", "default", "", f"{_STATUS_RED},bold", "default"),
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
    # ButtonBar — bright bold + underline reads as a clickable control.
    ("btn", "white,bold,underline", ""),
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


def _tmux_status_left(error: bool, mode_label: str | bool) -> str:
    """The tmux status-left segment: the ``railmux`` brand plus a current-mode
    indicator (``· Claude Code`` / ``· Codex``), rendered in the tips colour
    (colour0 = black on green, or white on red)."""
    brand = _TMUX_BRAND_ERROR if error else _TMUX_BRAND_NORMAL
    fg = "colour231" if error else "colour0"
    # Bool support is a compatibility bridge for callers from <=0.1.1. New
    # code passes the registered label so a third mode renders correctly.
    if isinstance(mode_label, bool):
        mode_label = "Codex" if mode_label else "Claude Code"
    return f"{brand}#[fg={fg}]· {mode_label} #[default]"

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

    @property
    def is_placeholder(self) -> bool:
        return self.key.startswith("__new__-")


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
    _MINIMUM_TERMINAL_SIZE = (80, 20)
    _RECOMMENDED_AGENT_PANE_SIZE = (80, 20)
    _MINIMUM_AGENT_PANE_SIZE = (50, 12)

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
        # Outer tmux display state. Only ``primary`` is rendered today; the
        # bounded secondary slot is deliberately present so dual-agent support
        # does not grow a second set of scalar fields throughout App.
        self._workspace = AgentWorkspace()
        self._display_transport_manager: AgentDisplayTransport | None = None
        self._loop: urwid.MainLoop | None = None
        self._pending_restore_state: dict | None = None
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
        self._railmux_pane_id: str | None = None  # set in run()
        self._railmux_has_focus: bool = True
        self._divider_active: bool | None = None
        self._has_less: bool = shutil.which("less") is not None
        self._less_mouse_flag: str = self._detect_less_mouse()
        self._scroll_manager = ScrollManager(enabled=scroll_coalescing)
        self._soft_quit_flag: bool = False
        # Ordered provider registry + stable active key. No two-mode boolean:
        # ``m`` cycles the registry and each key owns independent view state.
        self._mode_registry: ModeRegistry = DEFAULT_MODE_REGISTRY
        self._active_mode_key: str = self._mode_registry.default_key
        self._codex_index = CodexIndex(
            self._codex_home_path(), self._renames)
        self._codex_project_filter: dict[Path, int] = {}  # cwd → Codex session count
        # Mode switches paint from existing snapshots immediately. A daemon
        # worker refreshes both NFS-backed indexes; _refresh consumes the result
        # on the UI thread so no urwid widget is touched from the worker.
        self._mode_refresh_lock = threading.Lock()
        self._mode_refresh_thread: threading.Thread | None = None
        self._mode_refresh_result: (
            tuple[list[Project] | None, CodexIndex | None, str | None] | None
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
        )
        self._sessions_pane = SessionsPane(
            on_select=self._on_session_select,
            on_preview=self._on_session_preview,
            on_context=self._open_session_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
            provider_label=initial_mode.label,
        )
        self._running_pane = RunningSessionsPane(
            on_select=self._on_running_select,
            on_context=self._on_running_context_menu,
            on_double_detected=self._schedule_right_pane_focus_after_double,
            provider_label=initial_mode.label,
        )
        # Warn early if dependencies are missing so the user doesn't
        # discover it by getting a cryptic error in the right pane.
        if not tmux_ctl.has_tmux():
            self._set_status(
                "ERROR: tmux not found on PATH — railmux cannot run without tmux")

        # The outer AttrMaps highlight both LineBox borders and otherwise-
        # unstyled titles in the focused pane. A one-column gutter keeps those
        # right edges visually separate from tmux's center divider.
        self._sidebar = urwid.Pile([
            ("weight", 2, urwid.AttrMap(self._projects_pane, "pane", focus_map="pane_focus")),
            ("weight", 3, urwid.AttrMap(self._sessions_pane, "pane", focus_map="pane_focus")),
            ("weight", 2, urwid.AttrMap(self._running_pane, "pane", focus_map="pane_focus")),
        ])
        self._sidebar_body = urwid.Padding(self._sidebar, right=1)
        self._hint_bar = HintBar()
        # Start on the focused pane's key set (sidebar defaults to Projects) so
        # the bar is correct before the first refresh tick.
        self._hint_bar.set_context(self._help_context())
        self._button_bar = ButtonBar(
            on_help=self._open_help_modal,
            on_quit=self._open_quit_confirm,
            on_detach=self._on_detach,
            on_mode_toggle=self._cycle_mode,
        )
        # Footer: context key hints, optional error bar, then the constant
        # button row. Status/tips are shown in the outer tmux status bar; the
        # error bar is the in-pane surface for agent-launch failures so the user
        # sees what went wrong without having to check the tmux bar.
        self._error_text = urwid.Text("", align="left", wrap="clip")
        self._error_bar = urwid.AttrMap(self._error_text, "status_error")
        self._error_timer: object | None = None
        # Do not add the error widget until there is an error.  An empty
        # urwid.Text still occupies one row, so leaving it in the Pile would
        # permanently steal terminal space despite the bar looking blank.
        self._footer = urwid.Pile([
            ("pack", self._hint_bar),
            ("pack", self._button_bar),
        ])
        self._frame = _FocusAwareFrame(
            body=self._sidebar_body, footer=self._footer)
        # Backstop for direct ``--inside-tmux`` starts. The normal CLI also
        # audits before ``new-session -A`` so a stale outer session cannot
        # prevent a new App process from launching.
        if tmux_ctl.in_tmux():
            recover_interrupted_swaps()
        state = self._load_state()
        # Recover sessions left alive from a previous soft-quit.  Load the
        # state first: a resolved session may intentionally retain its
        # ``cx-new---*`` tmux name, so the tmux name alone is not enough to
        # reconstruct the real session id on platforms without procfs.
        self._running_recovery_ok = self._discover_orphans(state)
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
        self, slot: AgentSlot, session_id: str | None, tmux_name: str | None,
    ) -> None:
        """Update one slot, painting sidebar highlights only for the active slot."""
        slot.active_session_id = session_id
        mode = self._modes().for_tmux_name(tmux_name) if tmux_name else None
        slot.mode_key = mode.key if mode is not None else None
        if slot.key == self._agent_workspace().active_slot_key:
            self._sessions_pane.set_active_session(session_id)
            self._running_pane.set_active(tmux_name)

    def _set_active_target(self, session_id: str | None,
                           tmux_name: str | None) -> None:
        """Compatibility entry point for the currently exposed primary slot."""
        self._set_slot_active_target(
            self._primary_slot, session_id, tmux_name)

    def _set_active_tmux_target(
        self, tmux_name: str, slot: AgentSlot | None = None,
    ) -> None:
        slot = slot or self._primary_slot
        running = self._by_tmux(tmux_name)
        session_id = None
        if running is not None and not running.is_placeholder:
            session_id = running.key
        self._set_slot_active_target(slot, session_id, tmux_name)

    def _set_divider_active(self, active: bool, *, force: bool = False) -> None:
        """Highlight the whole shared divider when an agent pane has focus.

        With exactly two panes tmux intentionally applies active-border colour
        to only half of their shared edge. Setting both border styles to the
        same value is therefore required for one continuous divider.
        """
        if not force and self._divider_active == active:
            return
        self._divider_active = active
        style = f"fg={_GRASS_GREEN}" if active else "fg=colour240"
        tmux_ctl.set_window_border_style(style)

    def _set_railmux_focus(self, active: bool, *, force_border: bool = False) -> None:
        """Synchronize urwid focus maps and the tmux center divider."""
        self._railmux_has_focus = active
        self._frame.set_window_active(active)
        self._set_divider_active(not active, force=force_border)
        if hasattr(self, "_hint_bar"):
            self._hint_bar.set_context(self._help_context())

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
        pane_id = self._primary_slot.pane_id
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
                # Ignore the late left-pane report while a double-click transfer
                # is pending; cancellation restores it for newer sidebar input.
                if not self._double_focus_visual_pending:
                    self._set_railmux_focus(True)
            elif key == "focus out":
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
        self._pending_restore_state = None
        if state is None:
            return
        restored = self._restore_right_pane(state)
        if not restored or not getattr(self, "_running_recovery_ok", True):
            return
        path = self._state_path()
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

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
                and self._primary_slot.agent_tmux_name == claude_tmux_name):
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
            None, [], running_ids=set(self._running),
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
        self._sessions_pane.set_sessions(project, sessions, running_ids=set(self._running),
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
        self._primary_slot.in_history_mode = False
        self._primary_slot.restore_state = None
        if session is None:
            self._launch_new_session()
            return
        self._launch_resume(session, steal_focus=steal_focus)

    def _on_running_select(self, entry: RunningEntry,
                            steal_focus: bool = True,
                            from_double: bool = False) -> None:
        if not from_double:
            self._cancel_pending_double_focus()
        selected = self._by_tmux(entry.tmux_name)
        if (selected is not None and selected.orphan is not None
                and not self._running_action_valid(
                    selected, entry.identity_token)):
            msg = "Open refused: the unresolved tmux identity changed"
            self._set_status(msg, "error")
            self._show_error(msg)
            return
        # Re-attach the agent pane to this already-running session AND
        # sync the Projects/Sessions panes to that session's project, so the
        # sidebar reflects what's actually showing on the right.
        self._primary_slot.in_history_mode = False
        self._primary_slot.restore_state = None
        ok = self._attach_in_right_pane(entry.tmux_name, steal_focus=steal_focus)
        if not ok:
            msg = "Re-attach failed: could not connect to agent pane"
            self._set_status(msg, "error")
            self._show_error(msg)
            return
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
                    running_ids=set(self._running),
                    favorite_ids=self._favorites.get_ids(),
                )
        self._clear_error()
        if not self._show_attention_status(entry.attention):
            self._set_status(f"→ {entry.label}")

    # --- history preview (display pane shows a transcript, not an agent session) ---

    def _on_session_preview(self, session: SessionMeta) -> None:
        """Show session history in the right pane without launching Claude.

        Stopped-session clicks preview immediately. On a double-click the first
        press may briefly preview before the second press opens the session;
        both operations reuse the same right pane.
        """
        # Rows bind their click callback from a periodically-refreshed snapshot.
        # The registry can become live between render and click (or a click can
        # land on an old row instance preserved across a rebuild). Revalidate
        # the tmux identity at action time so a stale "preview" callback never
        # replaces a live agent display with transcript history.
        running = getattr(self, "_running", {}).get(session.session_id)
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
        self._cancel_pending_double_focus()
        if not self._has_less:
            self._set_status("'less' not installed — cannot preview history")
            return
        if not self._primary_slot.in_history_mode:
            self._save_restore_state()
        if self._show_transcript(session.jsonl_path,
                                 session_type=session.session_type):
            self._primary_slot.in_history_mode = True
            self._set_active_target(session.session_id, None)
            if not self._show_attention_status(session.attention):
                self._set_status(
                    f"≡ Previewing {session.display_title} (history)")

    def _save_restore_state(self) -> None:
        """Remember what's in the right pane before taking it over for history."""
        slot = self._primary_slot
        if slot.pane_id and tmux_ctl.pane_alive(slot.pane_id):
            if (slot.agent_tmux_name
                    and tmux_ctl.session_exists(slot.agent_tmux_name)):
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
                         session_type: str = "claude") -> bool:
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
        slot = self._primary_slot
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
        self._install_fullscreen_binding()
        return True

    def _restore_from_history_mode(self) -> None:
        """Restore whatever was in the right pane before we entered history mode."""
        slot = self._primary_slot
        restore = slot.restore_state
        slot.restore_state = None
        if restore is None or restore.kind == "empty":
            return
        if restore.kind in ("agent", "claude") and restore.tmux_name:
            if tmux_ctl.session_exists(restore.tmux_name):
                running = self._by_tmux(restore.tmux_name)
                if (running is not None and running.orphan is not None
                        and not self._running_action_valid(running)):
                    self._set_status(
                        "Restore refused: marked tmux identity changed", "error")
                    return
                self._attach_in_right_pane(restore.tmux_name)
                # Sync the sidebar to the restored session's project so the
                # user doesn't see a stale project after less exits.
                r = self._by_tmux(restore.tmux_name)
                if r is not None and r.project is not None:
                    proj = self._project_in_current_view(r.project)
                    if (self._selected_project is None
                            or self._selected_project.encoded_name != proj.encoded_name):
                        self._set_current_project(proj)
                        # #9: a synthetic Codex project (empty claude_dir) must
                        # never reach the Claude cache — ``_pane_sessions`` routes
                        # Codex/Claude and skips the empty case.
                        sessions = self._pane_sessions(
                            proj, refresh=not self._mode_refresh_pending())
                        self._sessions_pane.set_sessions(
                            proj, sessions,
                            running_ids=set(self._running),
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
        outcome = self._display_transport().attach(slot, agent_tmux_name)
        if not outcome.ok:
            return False
        if slot.pane_id is None:
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
        if outcome.fell_back and outcome.reason:
            self._set_status(
                f"Using nested agent display: {outcome.reason}", "warn")
        if slot.key == self._agent_workspace().active_slot_key:
            self._schedule_scroll_acceleration(agent_tmux_name)
        self._install_fullscreen_binding()
        return True

    def _attach_in_right_pane(self, claude_tmux_name: str, *,
                               steal_focus: bool = True) -> bool:
        """Compatibility entry point targeting the current primary slot."""
        return self._attach_agent_slot(
            self._primary_slot, claude_tmux_name, steal_focus=steal_focus)

    def _install_fullscreen_binding(self) -> None:
        """(Re)bind F9 to fullscreen-toggle the *agent* (right) pane.

        Unlike tmux's built-in ``Ctrl-B z`` — which zooms whichever pane is
        active and can therefore fullscreen the railmux sidebar by mistake — this
        targets the right pane's current id explicitly, so F9 always zooms the
        agent pane regardless of focus. Rebound whenever the right pane is
        (re)created because its id changes. Copy workflow: F9 → Shift-drag to
        select → Cmd/Ctrl+C → F9 to exit.
        """
        pane_id = self._agent_workspace().active.pane_id
        if not pane_id:
            return
        import subprocess as _sp
        _sp.run(
            ["tmux", "bind-key", "-n", "F9", "resize-pane", "-Z", "-t", pane_id],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )

    def _by_tmux(self, tmux_name: str) -> "_Running | None":
        """Find the running session backed by a given tmux session name."""
        for r in self._running.values():
            if r.tmux_name == tmux_name:
                return r
        return None

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
        real_pane = self._display_transport().displayed_real_pane(tmux_name)
        if real_pane is not None:
            if server is not None:
                return real_pane in server.panes
            return tmux_ctl.pane_alive(real_pane)
        if server is not None:
            return tmux_name in server.sessions
        return tmux_ctl.session_exists(tmux_name)

    def _existing_session_ids(self, cwd: Path, project: Project | None,
                              session_type: str) -> frozenset[str]:
        """Session ids already present in *cwd* right now (pre-launch snapshot).

        Codex reads a fresh Codex-index scan of the cwd; Claude reads the
        session cache (skipped for a synthetic project with empty claude_dir,
        or a brand-new project dir). Used by ``_launch`` to fence off (#12)
        pre-existing rollouts from placeholder resolution."""
        if session_type == "codex":
            raw = self._codex_index.sessions_for_cwd(cwd, refresh=True)
        elif project is not None and project.claude_dir != Path():
            raw = self._session_cache.list_sessions(project)
        else:
            raw = []
        return frozenset(s.session_id for s in raw)

    def _launch(self, key: str, cmd: list[str], cwd: Path, label: str,
                project: Project | None, placeholder_path: Path | None = None,
                *, steal_focus: bool = True,
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
        existing = self._running.get(key)
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
            self._show_error(msg)
            return False
        # #12: snapshot the session ids already present in the launch cwd BEFORE
        # starting the child, so placeholder resolution only ever binds a NEWLY
        # appeared id — never a rollout another process wrote to the same cwd.
        pre_launch_ids: frozenset[str] = frozenset()
        if placeholder_path is not None:
            pre_launch_ids = self._existing_session_ids(
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
            self._show_error(msg)
            return False
        self._running[key] = _Running(
            key=key, tmux_name=tmux_name, label=label, project=project,
            placeholder_path=placeholder_path,
            created_at=(launch_marker.created_at if launch_marker is not None
                        else 0.0),
            session_type=session_type,
            pre_launch_ids=pre_launch_ids,
            orphan=launch_marker,
        )
        self._stamp_running(self._running[key])
        if not self._attach_in_right_pane(tmux_name, steal_focus=steal_focus):
            msg = "Launch failed: could not attach to agent pane"
            self._set_status(msg, "error")
            self._show_error(msg)
            return False
        self._clear_error()
        return True

    def _launch_resume(self, session_meta: SessionMeta,
                        *, steal_focus: bool = True) -> None:
        # Revalidate at action time.  A row can be stale, and an older Railmux
        # may have left a live placeholder writer that startup could not adopt.
        # Discover once more before ever running ``codex resume``/``claude
        # --resume`` so a click cannot create a second writer for one session.
        registry = getattr(self, "_running", None)
        running = registry.get(session_meta.session_id) if registry is not None else None
        if (registry is not None
                and (running is None
                     or not self._agent_session_alive(running.tmux_name))):
            self._discover_orphans()
            # Discovery may restore a still-unresolved placeholder under its
            # placeholder key. Promote it synchronously before deciding the
            # real UUID is stopped; waiting for the next poll recreates the
            # exact click-to-duplicate window this guard is meant to close.
            self._resolve_placeholders([session_meta.project])
            running = self._running.get(session_meta.session_id)
        if (running is not None
                and self._agent_session_alive(running.tmux_name)):
            self._on_running_select(
                RunningEntry(
                    tmux_name=running.tmux_name,
                    label=running.label,
                    status=running.status,
                ),
                steal_focus=steal_focus,
            )
            return

        if registry is not None:
            target_path = self._path_key(session_meta.project.real_path)
            ambiguous_live_placeholder = any(
                candidate.is_placeholder
                and candidate.session_type == session_meta.session_type
                and candidate.placeholder_path is not None
                and self._path_key(candidate.placeholder_path) == target_path
                and session_meta.session_id not in candidate.pre_launch_ids
                and self._agent_session_alive(candidate.tmux_name)
                for candidate in self._running.values()
            )
            if ambiguous_live_placeholder:
                msg = (
                    "Resume deferred: a live initializing agent in this "
                    "project could own this session"
                )
                self._set_status(msg, "error")
                self._show_error(msg)
                return

        cwd = session_meta.project.real_path
        env: dict[str, str] | None = None
        if session_meta.session_type == "codex":
            cmd = build_codex_resume_command(
                codex_binary=self._config.codex_binary,
                session_id=session_meta.session_id,
                cwd=cwd,
                yolo=self._settings.codex_yolo,
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
                        env=env, login_shell=session_meta.session_type == "codex",
                        session_type=session_meta.session_type):
            self._set_status(f"→ {session_meta.display_title}")

    def _new_placeholder_key(self) -> str:
        """Return a fresh ``__new__-<proc-token>-N`` placeholder key.

        Keeps the ``__new__-`` prefix (so ``_Running.is_placeholder`` still
        holds) but namespaces the name with this process's random token, so a
        restart's counter reset to 0 can never reproduce a previous process's
        placeholder tmux name and hijack a surviving orphan session (#11)."""
        self._new_session_counter += 1
        return f"__new__-{self._proc_token}-{self._new_session_counter}"

    def _launch_new_session(self) -> None:
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
                yolo=self._settings.codex_yolo,
            )
            env = self._codex_env()
        else:
            cmd = build_new_session_command(
                claude_binary=self._config.claude_binary,
                cwd=proj.real_path,
            )
        if self._launch(placeholder, cmd, proj.real_path, f"{proj.display_name}/(new)",
                        proj, placeholder_path=proj.real_path, env=env,
                        login_shell=mode.login_shell,
                        session_type=mode.session_type):
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
            self._show_error(msg)
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
                yolo=self._settings.codex_yolo,
            )
            env = self._codex_env()
        else:
            cmd = build_new_session_command(
                claude_binary=self._config.claude_binary, cwd=path)
        if self._launch(
                placeholder, cmd, path, f"{path.name}/(new)", project,
                placeholder_path=path, env=env, login_shell=login_shell,
                session_type=session_type):
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
                    sid = r.key if (r and not r.is_placeholder) else None
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
            r = self._running.get(session.session_id)
            if r and tmux_ctl.session_exists(r.tmux_name):
                running_label = f"detached as '{r.tmux_name}'"
        modal = SessionInfoModal(session=session, running_label=running_label, on_close=self._close_modal)
        self._show_overlay(modal, width=60, height=40,
                           click_outside_to_close=True)

    def _open_help_modal(self) -> None:
        # Zoom the left (railmux) pane fullscreen so the help modal has the
        # entire terminal.  Tmux resize-pane -Z toggles — the second call in
        # _close_help_modal restores the original split layout.  This is
        # much cleaner than shrinking the right pane: it doesn't force a
        # reflow in the agent pane, so no history corruption.
        if self._railmux_pane_id:
            import subprocess as _sp
            _sp.run(
                ["tmux", "resize-pane", "-Z", "-t", self._railmux_pane_id],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

        modal = HelpModal(on_close=self._close_help_modal)
        self._show_overlay(modal, width=60, height=80,
                           click_outside_to_close=True,
                           on_click_outside=self._close_help_modal,
                           fixed_width=True, fixed_height=True)

    def _close_help_modal(self) -> None:
        self._close_modal()
        # Un-zoom — restore the previous tmux layout, but only if the railmux
        # pane is still zoomed.  F9 shares the same resize-pane -Z toggle
        # (targeting the right pane), so if the user pressed F9 while help
        # was open the left pane was already unzoomed and calling -Z again
        # would RE-zoom it, trapping the user in fullscreen.
        if self._railmux_pane_id:
            import subprocess as _sp
            result = _sp.run(
                ["tmux", "display-message", "-p", "-t", self._railmux_pane_id,
                 "-F", "#{window_zoomed_flag}"],
                stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True,
            )
            if result.stdout.strip() == "1":
                _sp.run(
                    ["tmux", "resize-pane", "-Z", "-t", self._railmux_pane_id],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )

    def _open_quit_confirm(self) -> None:
        self._save_state()
        modal = QuitConfirmModal(
            on_confirm=self._confirm_quit,
            on_soft_quit=self._soft_quit,
            on_cancel=self._close_modal,
            running_count=len(self._running),
        )
        self._show_overlay(modal, width=50, height=40)

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
        # Split visibly in the same window: if an agent pane exists,
        # put the terminal below it; otherwise split off the current pane.
        pane_id = self._primary_slot.pane_id
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
        _sp.run(["tmux", "detach-client"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

    def _confirm_quit(self) -> None:
        """Hard quit: close modal, tear down everything in ``finally``."""
        self._close_modal()
        raise urwid.ExitMainLoop()

    def _soft_quit(self) -> None:
        """Soft quit: set flag so ``_teardown_tmux`` skips session kill."""
        # Save again at the point of commitment.  The confirmation modal may
        # have been open for a while and placeholder bindings can resolve in
        # the meantime.
        self._save_state()
        self._soft_quit_flag = True
        self._close_modal()
        raise urwid.ExitMainLoop()

    # --- state file (for restart-after-soft-quit) --------------------------

    def _state_path(self) -> Path | None:
        identity = getattr(self, "_restart_identity", None)
        return (
            restart_state.instance_state_path(identity)
            if identity is not None else None
        )

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
        return data

    def _recovery_state_data(self) -> dict:
        data: dict = {}
        slot = self._primary_slot
        if slot.in_history_mode:
            data["right_kind"] = "preview"
            if slot.active_session_id is not None:
                data["right_session"] = slot.active_session_id
        elif slot.agent_tmux_name is not None:
            data["right_kind"] = "agent"
            data["right_tmux"] = slot.agent_tmux_name
        else:
            data["right_kind"] = "empty"

        bindings: list[dict] = []
        for running in self._running.values():
            cwd = (
                running.placeholder_path
                if running.is_placeholder
                else running.project.real_path if running.project is not None
                else None
            )
            if cwd is None:
                continue
            item = {
                "key": running.key,
                "tmux_name": running.tmux_name,
                "session_type": running.session_type,
                "cwd": str(cwd),
            }
            if running.is_placeholder:
                item["created_at"] = running.created_at
                item["pre_launch_ids"] = sorted(running.pre_launch_ids)
                item["pre_launch_complete"] = True
            bindings.append(item)
        if bindings:
            data["running_bindings_version"] = 1
            data["running_bindings"] = bindings
        return data

    def _save_state(self) -> None:
        """Persist portable view state and exact-owner recovery independently."""
        view = restart_state.build_view(self._view_state_data())
        portable = {
            "schema_version": restart_state.SCHEMA_VERSION,
            "kind": "portable",
            "view": view,
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
            "view": view,
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

    def _restore_right_pane(self, state: dict) -> bool:
        """Re-open the right pane to its state at soft-quit time."""
        kind = state.get("right_kind")
        if kind in ("agent", "claude"):  # "claude" written by <=0.1.1
            tmux_name = state.get("right_tmux")
            if tmux_name and tmux_ctl.session_exists(tmux_name):
                running = self._by_tmux(tmux_name)
                if running is None:
                    # If the requested tmux was a historical duplicate, a
                    # validated state binding may lead to the canonical writer
                    # selected during discovery. Never attach an unrepresented
                    # live process directly.
                    for raw in state.get("running_bindings", []):
                        if (isinstance(raw, dict)
                                and raw.get("tmux_name") == tmux_name
                                and isinstance(raw.get("key"), str)):
                            running = self._running.get(raw["key"])
                            if running is not None:
                                tmux_name = running.tmux_name
                            break
                if running is None:
                    msg = (
                        "Restore deferred: previous agent could not be "
                        "validated"
                    )
                    self._set_status(msg, "error")
                    self._show_error(msg)
                    return False
                if (running.orphan is not None
                        and not self._running_action_valid(running)):
                    msg = "Restore deferred: marked tmux identity changed"
                    self._set_status(msg, "error")
                    self._show_error(msg)
                    return False
                ok = self._attach_in_right_pane(tmux_name, steal_focus=False)
                if not ok:
                    msg = (
                        "Restore failed: could not re-attach to previous "
                        "agent session"
                    )
                    self._set_status(msg, "error")
                    self._show_error(msg)
                    return False
            return True
        elif kind == "preview":
            sess_id = state.get("right_session")
            if sess_id and self._selected_project is not None:
                if self._active_mode().project_source == ProjectSource.CODEX:
                    meta = self._codex_index.get(sess_id)
                else:
                    meta = self._session_cache.get(
                        self._selected_project, sess_id)
                if meta is not None and self._show_transcript(
                        meta.jsonl_path, session_type=meta.session_type):
                    self._primary_slot.in_history_mode = True
                    self._set_active_target(meta.session_id, None)
                    return True
                return False
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

    def _valid_running_binding(
        self,
        raw: object,
        live: dict[str, tuple[Path, int]],
        projects: dict[Path, Project],
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
            if (meta is None
                    or self._path_key(meta.project.real_path) != self._path_key(cwd)):
                return None
            try:
                open_ids = tmux_ctl.session_rollout_ids(
                    tmux_name, self._codex_home_path() / "sessions")
            except Exception:
                open_ids = None
            # An empty set is a transient/permission failure and does not
            # disprove the persisted mapping.  A non-empty set naming other
            # rollouts but not this id does disprove it.
            if open_ids and key not in open_ids:
                return None
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

    def _running_binding_data(self, running: _Running) -> dict | None:
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
            # The potentially-large exclusion set lives in the atomic runtime
            # state, not in a tmux command argument. A stamp alone therefore
            # identifies the process but must not authorize heuristic binding.
            data["pre_launch_complete"] = False
        return data

    def _stamp_running(self, running: _Running) -> bool:
        """Best-effort identity stamp for cross-platform orphan recovery."""
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
        if running.orphan is None:
            return identity_token is None
        if (identity_token is not None
                and identity_token != running.orphan.creation_token):
            return False
        return self._exact_running_pane(running) is not None

    def _discover_orphans(self, state: dict | None = None) -> bool:
        """Find registered agent tmux sessions and rebuild ``_running``.

        Called at startup so a soft-quit → restart cycle picks up every
        session that was left alive.

        tmux session names are truncated (``_safe_name``, 16 chars), so
        we must resolve each truncated name back to the full session_id
        by scanning the project's sessions — otherwise the truncated key
        will not match ``SessionMeta.session_id`` elsewhere.
        """
        import subprocess as _sp
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
        projects = {
            self._path_key(p.real_path): p
            for p in list_projects(self._claude_home)
        }
        # A Codex-only cwd has no Claude project directory. Include synthetic
        # projects from one index snapshot so its surviving cx-* tmux session
        # is re-adopted after a soft restart instead of silently disappearing.
        has_codex_session = any(
            (mode := self._modes().for_tmux_name(line.split("\t", 1)[0]))
            is not None and mode.project_source == ProjectSource.CODEX
            for line in out.splitlines()
        )
        if has_codex_session:
            for cwd, count in self._codex_index.all_cwds().items():
                projects.setdefault(
                    self._path_key(cwd), self._synthesise_codex_project(cwd, count))

        live: dict[str, tuple[Path, int]] = {}
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
                    and marker.owner.server_digest
                    == current_owner.server_digest
                    and marker.owner.pane_id != current_owner.pane_id
                    and not owner_snapshot_loaded):
                server = tmux_ctl.server_snapshot()
                live_panes = server.panes if server is not None else None
                owner_snapshot_loaded = True
            if not orphan_marker.owner_available(
                    marker, current_owner, live_panes):
                continue
            if (current_owner is not None
                    and marker.owner.pane_id != current_owner.pane_id):
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
                            pre_launch_complete = True
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
                            enriched["pre_launch_complete"] = True
                        break
            running = self._valid_running_binding(enriched, live, projects)
            if running is None or running.tmux_name != name:
                continue
            if running.key in self._running:
                continue
            self._running[running.key] = running
            claimed_tmux.add(name)
            found += 1

        if state_bindings:
            for raw in state_bindings:
                running = self._valid_running_binding(raw, live, projects)
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
                if mode.project_source != ProjectSource.CODEX:
                    continue
                try:
                    open_ids = tmux_ctl.session_rollout_ids(
                        name, self._codex_home_path() / "sessions")
                except Exception:
                    open_ids = None
                if not open_ids:
                    continue
                matches = [
                    session_id for session_id in open_ids
                    if (meta := self._codex_index.get(session_id, refresh=False))
                    is not None
                    and self._path_key(meta.project.real_path) == self._path_key(cwd)
                ]
                if len(matches) != 1:
                    continue
                full_id = matches[0]
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
        # When the right pane is open the railmux sidebar is only ~30% of the
        # terminal.  Bump relative dimensions so overlays stay readable.
        # Fixed-pixel overlays (context menus) are left alone.
        if not fixed_width and self._right_pane_open():
            width = int(width * 1.6)
        if not fixed_height and self._right_pane_open():
            height = int(height * 1.35)
        width_spec = width if fixed_width else ("relative", width)
        height_spec = height if fixed_height else ("relative", height)
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

    def _close_modal(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._frame
        self._sessions_pane.set_selected_session(None)
        self._running_pane.set_selected(None)

    # --- key handling ---

    def _on_input(self, key: str) -> None:
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
        # Simple action keys are dispatched from the shared keymap (single
        # source of truth shared with the hint bar) so the two can't drift.
        action = keymap.action_for(key, self._help_context())
        if action is not None:
            getattr(self, action)()
            return

    def _maybe_prompt_codex_yolo(self) -> None:
        """First time the user enters Codex mode, ask whether to enable auto-run
        (yolo). Enabling flips the persisted ``codex_yolo`` setting True so
        subsequent Codex launches bypass approvals + sandbox. Either answer marks
        ``codex_yolo_prompted`` True, so the popup is shown only once."""
        # getattr: keep bare ``App.__new__`` unit tests (no loop/settings) safe.
        if getattr(self, "_loop", None) is None:
            return
        settings = getattr(self, "_settings", None)
        if settings is None or settings.codex_yolo_prompted:
            return

        def _enable() -> None:
            saved = self._settings.record_codex_yolo_choice(True)
            self._close_modal()
            if not saved:
                self._set_status(
                    "Could not save Codex auto-run choice; settings unchanged.",
                    "error",
                )
                return
            self._set_status("Codex auto-run enabled (m to exit mode).")

        def _decline() -> None:
            saved = self._settings.record_codex_yolo_choice(False)
            self._close_modal()
            if not saved:
                self._set_status(
                    "Could not save Codex auto-run choice; it will ask again.",
                    "warn",
                )

        from railmux.ui.modals import YoloConfirmModal
        modal = YoloConfirmModal(on_confirm=_enable, on_cancel=_decline)
        self._show_overlay(modal, width=60, height=45)

    def _schedule_mode_data_refresh(self) -> None:
        """Refresh both NFS-backed mode indexes without blocking the UI thread."""
        thread = self._mode_refresh_thread
        if thread is not None and thread.is_alive():
            return
        with self._mode_refresh_lock:
            if self._mode_refresh_result is not None:
                return

        claude_home = self._claude_home
        codex_home = self._codex_home_path()
        renames = self._renames
        lock = self._mode_refresh_lock

        def _worker() -> None:
            try:
                projects = list_projects(claude_home)
                index = CodexIndex(codex_home, renames)
                index.refresh()
                result = (projects, index, None)
            except Exception as exc:
                result = (None, None, str(exc))
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
        thread = getattr(self, "_mode_refresh_thread", None)
        lock = getattr(self, "_mode_refresh_lock", None)
        if lock is None:
            return False
        with lock:
            has_result = self._mode_refresh_result is not None
        return has_result or (thread is not None and thread.is_alive())

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
        projects, index, error = result
        if error is not None or projects is None or index is None:
            self._set_status(
                f"Background mode refresh failed: {error or 'unknown error'}",
                "warn",
            )
            return False
        self._project_snapshot = projects
        self._project_snapshot_at = time.monotonic()
        self._codex_index = index
        self._codex_project_filter = index.all_cwds(refresh=False)
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
            projects = list_projects(self._claude_home)
            self._project_snapshot = projects
            self._project_snapshot_at = now
        if self._active_mode().project_source != ProjectSource.CODEX:
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

    def _teardown_tmux(self) -> None:
        """Clean up on quit.

        Called exactly once, from ``run()``'s ``finally`` block, for both
        hard and soft quit.  On soft quit (``_soft_quit_flag`` is set) the
        detached agent sessions and the outer tmux session are left alive.
        """
        self._teardown_scroll_acceleration()
        # Drop our status-bar overrides BEFORE the soft-quit early return below —
        # on soft quit the outer tmux session survives, so our appearance (bar
        # style, brand, forced `status on`) and status text would otherwise linger
        # in it. ``-u`` reverts each session option to its inherited/default value.
        if self._tmux_status_enabled and self._tmux_status_session:
            try:
                import subprocess as _sp
                revert = [opt for opt, _ in self._TMUX_BAR_OPTIONS]
                revert += list(self._TMUX_BAR_STYLE_OPTIONS)  # status-style, status-left
                revert.append("status-right")  # set dynamically, not in a tuple
                for opt in revert:
                    _sp.run(
                        ["tmux", "set-option", "-u", "-t",
                         self._tmux_status_session, opt],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    )
            except Exception:
                pass
            self._tmux_status_enabled = False
        # Remove the F9 fullscreen binding we installed (it's server-global).
        try:
            import subprocess as _sp
            _sp.run(["tmux", "unbind-key", "-n", "F9"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass
        # The coordinator first swaps every real pane home, then removes only
        # nested clients/placeholders. If a return cannot be proven, preserve
        # the marked keeper state and degrade this exit to soft semantics.
        try:
            display_closed = self._display_transport().close_all()
        except Exception:
            display_closed = False
        if not display_closed:
            self._soft_quit_flag = True
        if self._soft_quit_flag:
            return  # <-- soft quit: leave cc-* and outer tmux session alive
        for r in list(self._running.values()):
            try:
                tmux_ctl.kill_session(r.tmux_name)
            except Exception:
                pass
        self._running.clear()
        if self._auto_launched:
            session_name = tmux_ctl.current_session_name()
            if session_name == "railmux":
                try:
                    tmux_ctl.kill_session("railmux")
                except Exception:
                    pass

    def _enter_filter_mode(self) -> None:
        # Borrow the button row (footer index 1) for a filter Edit — both are a
        # single line, so the sidebar height doesn't jump. Restored on enter/esc.
        initial_text = (
            self._running_pane.filter_text
            if self._sidebar.focus_position == 2 else ""
        )
        edit = urwid.Edit(caption="filter: ", edit_text=initial_text)
        footer_pile = self._frame.contents["footer"][0]
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
                self._frame.focus_position = "body"
                return None
            return key

        original_keypress = edit.keypress
        def new_keypress(size, key):
            handled = restore(key)
            if handled is None:
                return None
            return original_keypress(size, key)
        edit.keypress = new_keypress

    # --- periodic refresh ---

    def _refresh(self) -> None:
        self._scroll_manager.maintain()
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
            self._install_fullscreen_binding()
        dead_display_agents = {
            agent
            for slot in self._agent_workspace().slots
            if (agent := transport.reap_dead_display(slot)) is not None
        }
        if dead_display_agents:
            for key in [
                key for key, running in self._running.items()
                if running.tmux_name in dead_display_agents
            ]:
                del self._running[key]
            self._set_active_target(None, None)
        background_refreshed = self._consume_mode_refresh()
        mode_refresh_pending = self._mode_refresh_pending()
        mode = self._active_mode()
        prefix = mode.tmux_prefix
        slot = self._primary_slot
        needs_liveness = slot.pane_id is not None or any(
            r.tmux_name.startswith(prefix) for r in self._running.values())
        server = tmux_ctl.server_snapshot() if needs_liveness else None
        child_probes: dict[str, bool | None] = {}

        def session_is_alive(name: str) -> bool:
            return self._agent_session_alive(name, server)

        def pane_is_alive(pane_id: str) -> bool:
            if server is not None:
                return pane_id in server.panes
            return tmux_ctl.pane_alive(pane_id)

        # A refresh may need several Codex views. Walk its session tree once,
        # then serve each view from the same mtime-keyed index snapshot.
        # TODO(review #6): this still walks/stats the whole Codex session tree
        # synchronously on the UI thread each tick; move to a single rate-limited
        # background scanner serving an immutable snapshot (needs design).
        refresh_codex = (
            mode.project_source == ProjectSource.CODEX
            or any(r.session_type == "codex" for r in self._running.values())
        )
        if (refresh_codex and not background_refreshed
                and not mode_refresh_pending):
            self._codex_index.refresh()

        # Refresh the Codex project filter so newly-created Codex sessions
        # make their cwd appear as a project in Codex mode.
        if mode.project_source == ProjectSource.CODEX:
            self._codex_project_filter = self._codex_index.all_cwds(refresh=False)
        # Placeholder resolution must discover its JSONL without extra delay.
        force_projects = any(r.is_placeholder for r in self._running.values())
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
        # Prune dead tmux sessions (e.g. a provider exited via /quit).
        for key in list(self._running):
            if self._running[key].tmux_name.startswith(prefix):
                if not session_is_alive(self._running[key].tmux_name):
                    del self._running[key]

        # If the session we were showing in the right pane has exited,
        # kill the right pane so the TUI returns to full-screen.
        if slot.pane_id and slot.agent_tmux_name:
            if not session_is_alive(slot.agent_tmux_name):
                tmux_ctl.kill_pane(slot.pane_id)
                slot.pane_id = None
                slot.agent_tmux_name = None
                slot.mode_key = None
                self._set_active_target(None, None)

        # Detect when the right pane was closed (user pressed q in less, the
        # pane was cleaned up above, or it was killed externally).
        if slot.pane_id and not pane_is_alive(slot.pane_id):
            slot.pane_id = None
            slot.agent_tmux_name = None
            slot.mode_key = None
            if slot.in_history_mode:
                slot.in_history_mode = False
                self._set_active_target(None, None)
                self._restore_from_history_mode()
            else:
                self._set_active_target(None, None)

        # Promote any `__new__-N` placeholders to their real session id — in
        # BOTH Claude and Codex mode. While a session stays a placeholder its
        # real-UUID row (filled from the on-disk scan) looks "not running", so
        # clicking it spawns a duplicate session; and `force_projects` above
        # stays stuck True, defeating the 3s project-scan cache. Codex
        # resolution must run too or neither ever clears.
        self._resolve_placeholders(projects)
        running_ids = set(self._running)
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

    _HELP_CONTEXTS = (keymap.CTX_PROJECTS, keymap.CTX_SESSIONS, keymap.CTX_RUNNING)

    def _help_context(self) -> str:
        """Map the focused sidebar pane (0/1/2) to a keymap context name.

        When the right-hand agent pane has focus (via Ctrl-B →), return the
        agent context so the help bar shows only the two keys that matter:
        Ctrl-B ← (back to sidebar) and F9 (fullscreen)."""
        if not self._railmux_has_focus:
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
            r = self._running.get(meta.session_id)
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
                meta = self._codex_index.get(r.key, refresh=False)
            else:
                meta = self._session_cache.get(r.project, r.key)
            if meta is not None:
                if meta.title:
                    r.label = f"{meta.project.display_name}/{meta.display_title}"
                r.status = self._effective_status(meta, child_probes, server)
                r.last_mtime = meta.last_mtime
                r.attention = meta.attention
                if self._primary_slot.agent_tmux_name == r.tmux_name:
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
        placeholders = [r for r in self._running.values() if r.is_placeholder]
        if not placeholders:
            return
        # Index visible projects by real_path. A Claude placeholder becomes
        # resolvable once its first real session makes the project visible;
        # Codex New Project can use its in-memory synthetic project earlier.
        by_path = {p.real_path: p for p in projects}
        claimed = set(self._running)
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
            if self._primary_slot.agent_tmux_name == r.tmux_name:
                self._set_active_target(candidate.session_id, r.tmux_name)
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
        """Prove a displayed real pane is home before killing its session."""
        if self._display_transport().prepare_kill(tmux_name):
            return True
        self._set_status(
            f"could not safely return {tmux_name} home; nothing was killed",
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
        r = self._running.get(session.session_id)
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
            detail = f"Permanently delete '{title}'?\n\nThis removes the session file from disk\nand kills its background tmux session."
            modal = DeleteConfirmModal(
                title=f"Delete '{title}'?",
                detail=detail,
                on_confirm=lambda: self._do_delete_session(session),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

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
            session_id = r.key if (r and not r.is_placeholder) else None
            project = r.project if r else None
            if session_id:
                detail = (f"Kill '{label}'?\n\n"
                          "The detached tmux session will be killed.\n"
                          "The session file will be deleted from disk.")
            else:
                detail = f"Kill '{label}'?\n\nThe detached tmux session will be killed."
            modal = DeleteConfirmModal(
                title=f"Kill '{label}'?",
                detail=detail,
                on_confirm=lambda: self._do_kill_running(
                    entry.tmux_name, session_id, project,
                    entry.identity_token),
                on_cancel=self._close_modal,
            )
            self._show_overlay(modal, width=54, height=30)

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
            r = self._running.get(session_id)
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
        self._show_overlay(modal, width=50, height=22)

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
        if r.is_placeholder:
            # Placeholder: no SessionMeta yet, but we can still kill the
            # running tmux session or switch to it.
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
            self._show_overlay(menu, width=32, height=12,
                               click_outside_to_close=True,
                               fixed_width=True, fixed_height=True)
            return
        session = self._find_session_meta(r.key, r.project, r.session_type)
        if session is None:
            return
        self._open_session_context_menu(session)

    def _open_session_context_menu(self, session: SessionMeta) -> None:
        # Ensure tmux focus is on our pane so the 200 ms poll doesn't
        # auto-close the menu (can happen if focus was on the right pane).
        if self._railmux_pane_id:
            tmux_ctl.select_pane(self._railmux_pane_id)
        self._sessions_pane.set_selected_session(session.session_id)
        r = self._running.get(session.session_id)
        is_alive = r is not None and not r.is_placeholder
        is_starred = session.session_id in self._favorites.get_ids()
        items: list[tuple[str, Callable[[], None]]] = [
            (" Open      ↵", lambda s=session: self._do_context_open(s)),
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
        self._show_overlay(menu, width=32, height=14,
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
        self._show_overlay(modal, width=50, height=22)

    def _do_context_info(self, session: SessionMeta) -> None:
        r = self._running.get(session.session_id)
        running_label = None
        if r and tmux_ctl.session_exists(r.tmux_name):
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
        r = self._running.get(session.session_id)
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
        self._attach_in_right_pane(tmux_name, steal_focus=True)

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
            if not self._return_agent_before_kill(tmux_name):
                return
            pane = self._exact_running_pane(running)
            if (pane is None
                    or not orphan_marker.same_live_tmux(running.orphan, pane)):
                self._set_status(
                    "Kill refused: the marked pane did not return home", "error")
                return
            if not tmux_ctl.kill_session_identity(pane):
                self._set_status(
                    "Kill failed: exact tmux session is still live", "error")
                return
        elif tmux_ctl.session_exists(tmux_name):
            tmux_ctl.kill_session(tmux_name)
        # Remove any _running entry keyed by this tmux name.
        for key in [k for k, r in self._running.items() if r.tmux_name == tmux_name]:
            del self._running[key]
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
        pane_id = self._agent_workspace().active.pane_id
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
        pane_id = self._agent_workspace().active.pane_id
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
        detail = f"Permanently delete '{title}'?\n\nThis removes the session file from disk\nand kills its background tmux session."
        modal = DeleteConfirmModal(
            title=f"Delete '{title}'?",
            detail=detail,
            on_confirm=lambda s=session: self._do_delete_session(s),
            on_cancel=self._close_modal,
        )
        self._show_overlay(modal, width=54, height=30)

    # --- resize divider ---

    def _resize_divider(self, expand_railmux: bool) -> None:
        """Move the vertical divider: [ shrinks railmux, ] expands it."""
        pane_id = self._primary_slot.pane_id
        if not pane_id or not tmux_ctl.pane_alive(pane_id):
            self._set_status("No agent pane to resize against.")
            return
        direction = "-R" if expand_railmux else "-L"
        tmux_ctl.resize_pane(pane_id, direction, 5)
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
        previous = getattr(self, "_last_size_class", None)
        current = self._terminal_size_class(width, height)
        self._last_workspace_size = (width, height)
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
        size = tmux_ctl.pane_size(slot.pane_id)
        if size is None:
            return
        width, height = size
        min_width, min_height = self._MINIMUM_AGENT_PANE_SIZE
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
        brand = _tmux_status_left(error, self._active_mode().label)
        try:
            import subprocess as _sp
            for opt, val in (("status-style", bar), ("status-left", brand)):
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

    # ── in-pane error bar ──────────────────────────────────────────────────

    _ERROR_BAR_TTL: float = 8.0  # seconds before auto-clear

    def _set_error_bar_visible(self, visible: bool) -> None:
        """Insert/remove the optional footer row without leaving blank space."""
        if not hasattr(self, "_footer") or not hasattr(self, "_error_bar"):
            return
        current = next(
            (i for i, (widget, _options) in enumerate(self._footer.contents)
             if widget is self._error_bar),
            None,
        )
        if visible and current is None:
            self._footer.contents.insert(
                1, (self._error_bar, self._footer.options("pack")))
        elif not visible and current is not None:
            del self._footer.contents[current]

    def _show_error(self, msg: str) -> None:
        """Display an error in the in-pane bottom bar (red, between hints and
        buttons).  Auto-clears after ``_ERROR_BAR_TTL`` seconds or on next
        successful launch.

        Info/warn messages use the outer tmux status bar (``_set_status``) —
        this bar is reserved for hard failures the user must not miss."""
        if not hasattr(self, "_error_text"):
            return
        self._error_bar.set_attr_map({None: "status_error"})
        self._error_text.set_text(msg)
        self._set_error_bar_visible(True)
        self._cancel_error_timer()
        if hasattr(self, "_loop") and self._loop is not None:
            self._error_timer = self._loop.set_alarm_in(
                self._ERROR_BAR_TTL, self._on_error_timeout)

    def _clear_error(self) -> None:
        """Clear the in-pane error bar and cancel its auto-clear timer."""
        if not hasattr(self, "_error_text"):
            return
        self._error_text.set_text("")
        self._set_error_bar_visible(False)
        self._cancel_error_timer()

    def _cancel_error_timer(self) -> None:
        if self._error_timer is not None and hasattr(self, "_loop") and self._loop is not None:
            try:
                self._loop.remove_alarm(self._error_timer)
            except Exception:
                pass
        self._error_timer = None

    def _on_error_timeout(self, _loop, _user_data) -> None:
        self._error_timer = None
        self._error_text.set_text("")
        self._set_error_bar_visible(False)

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
        # When a click-outside overlay is showing OR we're in history mode
        # (less running in the right pane), poll faster so the user sees a
        # quick response when pressing q in less or clicking the right pane.
        fast_poll = (
            self._primary_slot.in_history_mode
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
                # Unbind tmux's built-in right-click context menu so right-click
                # passes through to the application (Claude / less) instead of
                # flashing display-menu.  Left-click (MouseDown1Pane) is left at
                # its default: tmux switches pane focus then forwards the event.
                _sp.run(
                    ["tmux", "unbind-key", "-T", "root", "MouseDown3Pane"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
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
            self._set_railmux_focus(True, force_border=True)
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
