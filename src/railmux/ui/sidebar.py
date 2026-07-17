"""Shared chrome for the three sidebar sections."""
from __future__ import annotations

from collections.abc import Callable, Sequence

import urwid

from railmux.ui._widgets import ScrollableSidebarPane


def _truncate_title(title: str, maxcol: int) -> str:
    """Keep the stable section name visible and trim only the trailing detail."""
    maxcol = max(1, maxcol)
    if urwid.str_util.calc_width(title, 0, len(title)) <= maxcol:
        return title
    if maxcol == 1:
        return title[:1]
    pos, _ = urwid.calc_text_pos(title, 0, len(title), maxcol - 1)
    return title[:pos].rstrip() + "…"


class SidebarSection(urwid.WidgetWrap):
    """An unboxed pane preceded by one focus-aware labelled divider."""

    def __init__(
        self,
        pane: ScrollableSidebarPane,
        title: Callable[[], str],
    ) -> None:
        self.pane = pane
        self._title = title
        self._previous_section_focused = False
        self._header = urwid.Text("")
        self._pile = urwid.Pile([
            ("pack", self._header),
            ("weight", 1, pane),
        ])
        self._pile.focus_position = 1
        super().__init__(self._pile)

    def set_previous_section_focused(self, focused: bool) -> None:
        """Treat this title rule as the previous section's lower boundary."""
        if self._previous_section_focused == focused:
            return
        self._previous_section_focused = focused
        self._invalidate()

    def render(self, size, focus: bool = False):
        maxcol = size[0]
        title = _truncate_title(self._title(), max(1, maxcol - 4))
        used = urwid.str_util.calc_width("─ " + title + " ", 0,
                                         len("─ " + title + " "))
        fill = max(0, maxcol - used)
        prefix = "─ "
        suffix = " " + "─" * fill
        if focus:
            self._header.set_text(("pane_focus", prefix + title + suffix))
        elif self._previous_section_focused:
            # This row closes the selected section while introducing the next:
            # keep the boundary green without colouring the next title as if it
            # also owned keyboard focus.
            self._header.set_text([
                ("pane_focus", prefix),
                ("pane", title),
                ("pane_focus", suffix),
            ])
        else:
            # At rest, title and rule share one neutral tone so the three
            # section boundaries read as one consistent hierarchy.
            self._header.set_text(
                ("pane", prefix + title + suffix))
        return super().render(size, focus)


class StableWeightedPile(urwid.Pile):
    """A weighted Pile whose row rounding never depends on focus position."""

    def get_rows_sizes(self, size, focus: bool = False):
        if len(size) != 2 or any(
            option[0] != urwid.WHSettings.WEIGHT
            for _widget, option in self.contents
        ):
            return super().get_rows_sizes(size, focus)

        maxcol, maxrow = size
        weights = [float(option[1]) for _widget, option in self.contents]
        remaining = max(0, maxrow)
        remaining_weight = sum(weights)
        heights: list[int] = []
        for weight in weights:
            if remaining <= 0 or remaining_weight <= 0:
                rows = 0
            else:
                rows = int(remaining * weight / remaining_weight + 0.5)
            heights.append(rows)
            remaining -= rows
            remaining_weight -= weight
        widths = (maxcol,) * len(heights)
        sizes = tuple((maxcol, rows) for rows in heights)
        return widths, tuple(heights), sizes


class _SidebarRail(urwid.Widget):
    """Full-height outer rail with a focus-coloured section segment."""

    _sizing = frozenset((urwid.Sizing.BOX,))

    def __init__(self, sidebar: urwid.Pile, *, left: bool) -> None:
        super().__init__()
        self.sidebar = sidebar
        self._left = left
        self._selected: int | None = None

    def set_selected(self, selected: int | None) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self._invalidate()

    def render(self, size, focus: bool = False):
        maxcol, maxrow = size
        content_rows = max(0, maxrow - 1)
        rows = self.sidebar.get_item_rows((max(1, maxcol), content_rows), focus)

        start = end = -1
        if self._selected is not None and rows:
            start = sum(rows[:self._selected])
            end = (
                sum(rows[:self._selected + 1])
                if self._selected < len(rows) - 1
                else maxrow - 1
            )
        internal_boundaries = {
            sum(rows[:index]) for index in range(1, len(rows))
        }

        markup: list = []
        for row in range(maxrow):
            if row:
                markup.append("\n")
            attr = "pane_focus" if start <= row <= end else "pane"
            if row == start == end:
                char = "├" if self._left else "┤"
            elif row == start:
                char = "┌" if self._left else "┐"
            elif row == end:
                char = "└" if self._left else "┘"
            elif row == 0:
                char = "┌" if self._left else "┐"
            elif row == maxrow - 1:
                char = "└" if self._left else "┘"
            elif row in internal_boundaries:
                char = "├" if self._left else "┤"
            else:
                char = "│"
            markup.append((attr, char))
        return urwid.Text(markup, wrap="clip").render((maxcol,))


class UnifiedSidebarFrame(urwid.WidgetWrap):
    """Shared horizontal chrome with pointer-local wheel routing."""

    def __init__(
        self,
        sidebar: urwid.Pile,
        panes: Sequence[ScrollableSidebarPane],
    ) -> None:
        if len(panes) != 3:
            raise ValueError("the unified sidebar requires exactly three panes")
        self.sidebar = sidebar
        self.panes = tuple(panes)
        self._bottom = urwid.AttrMap(urwid.Divider("─"), "pane")
        self._layout = urwid.Pile([
            ("weight", 1, sidebar),
            ("pack", self._bottom),
        ])
        self._layout.focus_position = 0
        self._left_rail = _SidebarRail(sidebar, left=True)
        self._right_rail = _SidebarRail(sidebar, left=False)
        self._columns = urwid.Columns([
            ("fixed", 1, self._left_rail),
            self._layout,
            ("fixed", 1, self._right_rail),
        ], dividechars=0)
        self._columns.focus_position = 1
        super().__init__(self._columns)

    def render(self, size, focus: bool = False):
        selected = self.sidebar.focus_position if focus else None
        self._left_rail.set_selected(selected)
        self._right_rail.set_selected(selected)
        sections = [widget for widget, _options in self.sidebar.contents]
        for index, section in enumerate(sections):
            if isinstance(section, SidebarSection):
                section.set_previous_section_focused(
                    selected is not None and selected == index - 1)
        bottom_attr = (
            "pane_focus"
            if selected is not None and selected == len(sections) - 1
            else "pane"
        )
        self._bottom.set_attr_map({None: bottom_attr})
        return super().render(size, focus)

    def mouse_event(self, size, event, button, col, row, focus):
        if event == "mouse press" and button in (4, 5) and len(size) >= 2:
            maxcol, maxrow = size[:2]
            inner_cols = maxcol - 2
            inner_rows = maxrow - 1
            if inner_cols <= 0 or inner_rows <= 0:
                return True

            rows = self.sidebar.get_item_rows((inner_cols, inner_rows), focus)
            if not rows:
                return True
            if row >= maxrow - 1:
                section = 2
            else:
                boundary = 0
                section = len(rows) - 1
                for index, section_rows in enumerate(rows):
                    boundary += section_rows
                    if row < boundary:
                        section = index
                        break

            section_rows = rows[section]
            # Every section spends its first allocated row on its title rule.
            pane_rows = section_rows - 1
            if pane_rows <= 0:
                return True
            pane = self.panes[section]
            pane.mouse_event(
                (inner_cols, pane_rows), event, button,
                min(max(col - 1, 0), inner_cols - 1), 0,
                focus and self.sidebar.focus_position == section,
            )
            return True
        return super().mouse_event(size, event, button, col, row, focus)
