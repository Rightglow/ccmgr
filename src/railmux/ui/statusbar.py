"""Footer hint/button widgets, idle-tip strings, and text-reflow helpers.

Status/tips themselves are rendered into the outer tmux status bar by
``App._render_status_to_tmux``; this module no longer owns a status widget."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from railmux.ui import keymap


# Idle tips cycled through the status bar when there's no active message. These
# intentionally avoid the keys already listed in the always-visible HintBar
# (n/r/s/d, /, i, ?, q, Ctrl-B ←/→) — they surface behaviour that isn't obvious
# from the hint bar, like soft-quit and history preview.
TIPS: tuple[str, ...] = (
    "Quit with q, then s to soft-quit — leaves every agent session running",
    "Click a stopped session to preview its history without launching the agent",
    "Double-click a session to open it and jump focus to the agent pane",
    "[ / ] resize the sidebar divider — shrink or expand",
    "␣ mirrors single-click; F8 cycles agent layouts",
    "F9 toggles fullscreen for the agent pane",
    "t opens a shell in the focused project's directory",
    "Codex mode hides codex exec automation threads — only interactive sessions shown",
)


def split_at_width(text: str, maxcol: int) -> tuple[str, str]:
    """Split *text* into (head, tail) where head fits in *maxcol* display
    columns. Uses urwid's column arithmetic so wide (CJK) glyphs — which
    occupy two columns — wrap correctly; ``textwrap`` counts characters and
    would let a wide line overflow and get clipped instead of wrapping.
    Prefers to break on the last space within the width. Spaces at the wrap
    boundary are dropped so the continuation line has no leading gap; spaces
    elsewhere in the text are preserved verbatim."""
    if not text:
        return "", ""
    maxcol = max(1, maxcol)
    pos, _ = urwid.calc_text_pos(text, 0, len(text), maxcol)
    if pos >= len(text):
        return text, ""
    # When even one glyph won't fit (e.g. CJK character in a 1-column
    # terminal, pos == 0), force one character so callers that loop on
    # "remaining" (reflow_pages) always make progress.
    if pos == 0:
        return text[:1], text[1:]
    # Prefer a word boundary, but only if it doesn't waste most of the
    # line — otherwise (e.g. CJK text whose only early space trails a lone
    # glyph) hard-break at the column limit.
    brk = text.rfind(" ", 0, pos)
    if brk > pos // 2:
        head, tail = text[:brk], text[brk + 1:]
    else:
        head, tail = text[:pos], text[pos:]
    return head, tail.lstrip(" ")


def reflow_pages(text: str, maxcol: int, lines: int = 2) -> list[tuple[str, ...]]:
    """Split *text* into a list of pages, each a tuple of *lines* strings.

    When the text fits in one page the result has a single entry.  Callers use
    this to detect overflow and drive a page-flipping animation."""
    maxcol = max(1, maxcol)
    pages: list[tuple[str, ...]] = []
    remaining = text
    while remaining:
        page: list[str] = []
        for _ in range(lines):
            if not remaining:
                page.append("")
                continue
            head, remaining = split_at_width(remaining, maxcol)
            page.append(head)
        pages.append(tuple(page))
    return pages


class ButtonBar(urwid.WidgetWrap):
    """The utility/action row.  Each button is underlined text — no brackets,
    no background — so the row stays clean while still reading as clickable.

    ``? Help``  ``q Quit``  ``C-b d Detach`` are always present.
    ``m Mode`` cycles the registered agent modes (optional, at end).
    """

    _PRESS_HOLD_SECONDS = 0.15

    def __init__(self, on_help: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_detach: Callable[[], None],
                 on_mode_toggle: Callable[[], None] | None = None) -> None:
        self._on_mode_toggle = on_mode_toggle
        self._on_help = on_help
        self._on_quit = on_quit
        self._on_detach = on_detach
        callbacks = {
            "help": ("?", self._on_help),
            "quit": ("Q", self._on_quit),
            "detach": ("D", self._on_detach),
        }
        self._button_specs: list[
            tuple[str, str, str, Callable[[], None]]
        ] = []
        trail = keymap.hint_text_for(None).split("\n", 1)[1]
        for item in trail.split(" · "):
            key, sep, desc = item.rpartition(" ")
            if not sep:
                continue
            desc_key = desc.lower()
            callback_spec = callbacks.get(desc_key)
            if callback_spec is None:
                continue
            tiny, callback = callback_spec
            capitalized = desc[0].upper() + desc[1:]
            self._button_specs.append(
                (f"{key} {capitalized}", capitalized, tiny, callback))
        if self._on_mode_toggle is not None:
            self._button_specs.append(
                ("m Mode", "Mode", "M", self._on_mode_toggle))
        self._hit_areas: list[
            tuple[int, int, int, Callable[[], None]]
        ] = []
        self._layout_tier = "full"
        self._pressed_index: int | None = None
        self._clear_alarm: object | None = None
        self._loop: urwid.MainLoop | None = None
        self._text = urwid.Text("", align="left", wrap="clip")
        super().__init__(urwid.AttrMap(self._text, "dim"))
        self._rebuild("full")

    def set_loop(self, loop: urwid.MainLoop) -> None:
        self._loop = loop

    def _labels_for_width(self, maxcol: int) -> tuple[str, list[str]]:
        variants = {
            "full": [spec[0] for spec in self._button_specs],
            "compact": [spec[1] for spec in self._button_specs],
            "tiny": [spec[2] for spec in self._button_specs],
        }
        for tier in ("full", "compact"):
            labels = variants[tier]
            if sum(map(len, labels)) + max(0, len(labels) - 1) <= maxcol:
                return tier, labels
        return "tiny", variants["tiny"]

    def _ensure_width(self, maxcol: int) -> None:
        tier, _labels = self._labels_for_width(maxcol)
        if tier != self._layout_tier:
            self._rebuild(tier)

    def _rebuild(self, tier: str) -> None:
        """Build the markup list and hit-area index from scratch."""
        gap = " "
        label_idx = {"full": 0, "compact": 1, "tiny": 2}[tier]
        self._layout_tier = tier
        self._hit_areas.clear()
        markup: list = []
        col: int = 0

        def _add(index: int, label: str, cb: Callable[[], None]) -> None:
            nonlocal col
            if markup:
                markup.append(gap)
                col += len(gap)
            self._hit_areas.append((col, col + len(label), index, cb))
            attr = "btn_pressed" if index == self._pressed_index else "btn"
            markup.append((attr, label))
            col += len(label)

        for index, spec in enumerate(self._button_specs):
            _add(index, spec[label_idx], spec[3])

        self._text.set_text(markup)

    def render(self, size, focus: bool = False):
        self._ensure_width(size[0])
        return super().render(size, focus)

    def selectable(self) -> bool:
        return False

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button == 1 and row == 0:
            self._ensure_width(size[0])
            for start, end, index, cb in self._hit_areas:
                if start <= col < end:
                    self._pressed_index = index
                    self._rebuild(self._layout_tier)
                    self._invalidate()
                    loop = self._loop
                    if loop is not None:
                        try:
                            # Emit the acknowledgement frame before a callback
                            # performs synchronous tmux or filesystem work.
                            loop.draw_screen()
                        except Exception:
                            pass
                    try:
                        cb()
                    finally:
                        if loop is None:
                            self._clear_pressed(None, None)
                        else:
                            if self._clear_alarm is not None:
                                loop.remove_alarm(self._clear_alarm)
                            self._clear_alarm = loop.set_alarm_in(
                                self._PRESS_HOLD_SECONDS,
                                self._clear_pressed,
                            )
                    return True
        return super().mouse_event(size, event, button, col, row, focus)

    def _clear_pressed(self, _loop, _user_data) -> None:
        self._clear_alarm = None
        if self._pressed_index is None:
            return
        self._pressed_index = None
        self._rebuild(self._layout_tier)
        self._invalidate()


class HintBar(urwid.WidgetWrap):
    """Context-sensitive key reference — the action keys valid for the focused
    sidebar pane (Projects / Sessions / Running), from ``keymap.hint_text_for``
    so it can't drift from dispatch.

    Two fixed lines (height never changes, so the sidebar doesn't jitter). The
    hint soft-wraps across both lines by display width.  When it needs more than
    two lines the bar auto-flips through pages every *PAGE_INTERVAL* seconds
    rather than truncating — every key is shown eventually.
    """

    PAGE_INTERVAL: float = 5.0

    def __init__(self) -> None:
        self._context: str | None = None
        self._text = keymap.hint_text_for(self._context).split("\n", 1)[0]
        self._line1 = urwid.Text("", align="left", wrap="clip")
        self._line2 = urwid.Text("", align="left", wrap="clip")
        self._pages: list[tuple[str, str]] = []
        self._page_idx: int = 0
        self._timer_handle: object | None = None
        self._loop: urwid.MainLoop | None = None
        body = urwid.Pile([self._line1, self._line2])
        super().__init__(urwid.AttrMap(body, "dim"))
        self._reflow(80)

    def set_loop(self, loop: urwid.MainLoop) -> None:
        self._loop = loop
        # If pages were computed before the loop was available, start the timer.
        self._start_timer_if_needed()

    def set_context(self, context: str | None) -> None:
        """Switch to the key set for *context* (a ``keymap.CTX_*`` value, or
        None for all keys). No-op when unchanged."""
        if context == self._context:
            return
        self._context = context
        self._text = keymap.hint_text_for(context).split("\n", 1)[0]
        self._page_idx = 0
        self._reflow(80)

    def _reflow(self, maxcol: int) -> None:
        new_pages = reflow_pages(self._text, maxcol, lines=2)
        # Cast each page to exactly 2 strings to keep the type narrow.
        new_pages = [(p[0] if len(p) > 0 else "",
                       p[1] if len(p) > 1 else "") for p in new_pages]
        pages_changed = len(new_pages) != len(self._pages)
        self._pages = new_pages
        # Only reset to page 0 when the number of pages actually changed
        # (context switch, terminal resize).  Otherwise keep the current
        # page so the auto-flip animation isn't stomped on every tick.
        if pages_changed:
            self._show_page(0)
        else:
            # Clamp index in case pages changed slightly but count stayed same.
            if self._page_idx >= len(self._pages):
                self._show_page(0)
            else:
                line1, line2 = self._pages[self._page_idx]
                self._line1.set_text(line1)
                self._line2.set_text(line2)

    def _show_page(self, idx: int) -> None:
        self._page_idx = idx
        line1, line2 = self._pages[idx] if self._pages else ("", "")
        self._line1.set_text(line1)
        self._line2.set_text(line2)
        self._start_timer_if_needed()

    def _start_timer_if_needed(self) -> None:
        self._cancel_timer()
        if len(self._pages) <= 1 or self._loop is None:
            return
        self._timer_handle = self._loop.set_alarm_in(
            self.PAGE_INTERVAL, self._on_page_tick)

    def _cancel_timer(self) -> None:
        if self._timer_handle is not None and self._loop is not None:
            self._loop.remove_alarm(self._timer_handle)
        self._timer_handle = None

    def _on_page_tick(self, _loop, _user_data) -> None:
        self._timer_handle = None
        if len(self._pages) <= 1:
            return
        nxt = (self._page_idx + 1) % len(self._pages)
        self._show_page(nxt)

    def render(self, size, focus: bool = False):
        if size:
            self._reflow(size[0])
        return super().render(size, focus)
