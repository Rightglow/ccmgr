"""Footer hint/button widgets, idle-tip strings, and text-reflow helpers.

Status/tips themselves are rendered into the outer tmux status bar by
``App._render_status_to_tmux``; this module no longer owns a status widget."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from railmux.ui import keymap


# Idle tips are a scarce attention surface: include only high-value behaviour
# that is not already evident in the visible HintBar, Button Bar, or current
# screen. Keep each tip actionable, valid in every context where it can appear,
# and short enough for the status bar. Do not use this pool for redundant key
# reminders, marketing copy, or transient state that the UI already shows.
TIPS: tuple[str, ...] = (
    "Soft Quit (q, then s) closes shared views; agents keep running",
    "Restored filters: / edits; Ctrl-U clears the current filter",
    "Single-click a stopped session to preview it without starting it",
    "Sidebar actions target the last-focused pane in two-pane layouts",
    "Returning to one pane leaves the hidden second agent running",
    "Codex mode lists interactive sessions and hides codex exec threads",
    "railmux doctor reports the last detected tmux incident",
    "In tmux copy-mode, press Esc twice to return to agent input",
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
    # "remaining" (the reflow helpers) always make progress.
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


def reflow_hint_pages(
    text: str,
    maxcol: int,
    lines: int = 2,
    separator: str = " · ",
) -> list[tuple[str, ...]]:
    """Page a hint bar without separating one binding from its description.

    Each separator-delimited hint is an atomic visual group. Groups may share
    a line, but a group that fits on one page always moves whole to the next
    page instead of leaving its key or description behind during auto-flip.
    """
    maxcol = max(1, maxcol)
    lines = max(1, lines)
    items = [item.strip() for item in text.split(separator) if item.strip()]
    pages: list[tuple[str, ...]] = []
    page: list[str] = []
    current_line = ""

    def finish_page() -> None:
        nonlocal page
        if page:
            pages.append(tuple(page + [""] * (lines - len(page))))
            page = []

    for item in items:
        combined = (
            f"{current_line}{separator}{item}" if current_line else item
        )
        if current_line and urwid.calc_width(
                combined, 0, len(combined)) <= maxcol:
            current_line = combined
            continue

        if current_line:
            page.append(current_line)
            current_line = ""

        item_lines: list[str] = []
        remaining = item
        while remaining:
            head, remaining = split_at_width(remaining, maxcol)
            item_lines.append(head)

        if len(item_lines) == 1:
            if len(page) >= lines:
                finish_page()
            current_line = item_lines[0]
            continue

        # Keep a wrapped item together whenever it can fit on an empty page.
        if page and len(item_lines) <= lines \
                and len(page) + len(item_lines) > lines:
            finish_page()
        for item_line in item_lines:
            if len(page) >= lines:
                finish_page()
            page.append(item_line)
        if len(page) >= lines:
            finish_page()

    if current_line:
        if len(page) >= lines:
            finish_page()
        page.append(current_line)
    finish_page()
    return pages


class ButtonBar(urwid.WidgetWrap):
    """Responsive one/two-row utility controls with an internal More toggle."""

    _PRESS_HOLD_SECONDS = 0.15

    def __init__(self, on_help: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_detach: Callable[[], None],
                 on_mode_toggle: Callable[[], None] | None = None,
                 on_layout: Callable[[], None] | None = None,
                 on_options: Callable[[], None] | None = None,
                 on_expanded_change: Callable[[bool], None] | None = None,
                 ) -> None:
        self._on_mode_toggle = on_mode_toggle
        self._on_layout = on_layout
        self._on_options = on_options
        self._on_expanded_change = on_expanded_change
        self._on_help = on_help
        self._on_quit = on_quit
        self._on_detach = on_detach
        callbacks = {
            "help": ("?", self._on_help),
            "quit": ("Q", self._on_quit),
            "detach": ("D", self._on_detach),
        }
        self._primary_specs: list[
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
            self._primary_specs.append(
                (f"{key} {capitalized}", capitalized, tiny, callback))
        self._primary_specs.append(("More", "More", "+", self._toggle_more))
        self._secondary_specs: list[
            tuple[str, str, str, Callable[[], None]]
        ] = []
        if self._on_mode_toggle is not None:
            self._secondary_specs.append(
                ("m Mode", "Mode", "M", self._on_mode_toggle))
        if self._on_layout is not None:
            self._secondary_specs.append(
                ("F8 Layout", "Layout", "L", self._on_layout))
        if self._on_options is not None:
            self._secondary_specs.append(
                ("o Options", "Options", "O", self._on_options))
        self._hit_areas: list[
            tuple[int, int, int, int, Callable[[], None]]
        ] = []
        self._layout_tiers = {0: "full", 1: "full"}
        self._expanded = False
        self._pressed_index: int | None = None
        self._clear_alarm: object | None = None
        self._loop: urwid.MainLoop | None = None
        self._texts = (
            urwid.Text("", align="left", wrap="clip"),
            urwid.Text("", align="left", wrap="clip"),
        )
        self._pile = urwid.Pile([("pack", self._texts[0])])
        super().__init__(urwid.AttrMap(self._pile, "dim"))
        self._rebuild()

    def set_loop(self, loop: urwid.MainLoop) -> None:
        self._loop = loop

    @staticmethod
    def _labels_for_width(
        specs: list[tuple[str, str, str, Callable[[], None]]],
        maxcol: int,
    ) -> tuple[str, list[str]]:
        variants = {
            "full": [spec[0] for spec in specs],
            "compact": [spec[1] for spec in specs],
            "tiny": [spec[2] for spec in specs],
        }
        for tier in ("full", "compact"):
            labels = variants[tier]
            if sum(map(len, labels)) + max(0, len(labels) - 1) <= maxcol:
                return tier, labels
        return "tiny", variants["tiny"]

    def _ensure_width(self, maxcol: int) -> None:
        changed = False
        rows = [(0, self._primary_specs)]
        if self._expanded:
            rows.append((1, self._secondary_specs))
        for row, specs in rows:
            tier, _labels = self._labels_for_width(specs, maxcol)
            if tier != self._layout_tiers[row]:
                self._layout_tiers[row] = tier
                changed = True
        if changed:
            self._rebuild()

    def _row_markup(
        self,
        row: int,
        specs: list[tuple[str, str, str, Callable[[], None]]],
        index_offset: int,
    ) -> list:
        gap = " "
        tier = self._layout_tiers[row]
        label_idx = {"full": 0, "compact": 1, "tiny": 2}[tier]
        markup: list = []
        col: int = 0
        for local_index, spec in enumerate(specs):
            if markup:
                markup.append(gap)
                col += len(gap)
            index = index_offset + local_index
            label = spec[label_idx]
            if row == 0 and local_index == len(specs) - 1:
                label = {
                    "full": "Less" if self._expanded else "More",
                    "compact": "Less" if self._expanded else "More",
                    "tiny": "-" if self._expanded else "+",
                }[tier]
            self._hit_areas.append(
                (row, col, col + len(label), index, spec[3]))
            attr = "btn_pressed" if index == self._pressed_index else "btn"
            markup.append((attr, label))
            col += len(label)
        return markup

    def _rebuild(self) -> None:
        """Build visible row markup and row-aware hit areas from scratch."""
        self._hit_areas.clear()
        self._texts[0].set_text(self._row_markup(
            0, self._primary_specs, 0))
        contents = [(self._texts[0], self._pile.options("pack"))]
        if self._expanded and self._secondary_specs:
            self._texts[1].set_text(self._row_markup(
                1, self._secondary_specs, len(self._primary_specs)))
            contents.append((self._texts[1], self._pile.options("pack")))
        self._pile.contents = contents

    def _toggle_more(self) -> None:
        self._expanded = not self._expanded
        self._rebuild()
        self._invalidate()
        if self._on_expanded_change is not None:
            self._on_expanded_change(self._expanded)

    @property
    def extra_rows(self) -> int:
        return 1 if self._expanded and self._secondary_specs else 0

    def render(self, size, focus: bool = False):
        self._ensure_width(size[0])
        return super().render(size, focus)

    def selectable(self) -> bool:
        return False

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button == 1:
            self._ensure_width(size[0])
            for hit_row, start, end, index, cb in self._hit_areas:
                if hit_row == row and start <= col < end:
                    self._pressed_index = index
                    self._rebuild()
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
        self._rebuild()
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
        new_pages = reflow_hint_pages(self._text, maxcol, lines=2)
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
