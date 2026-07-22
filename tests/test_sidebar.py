"""Unified sidebar chrome, focus, title, and wheel-routing coverage."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import urwid

from railmux.models import Project, SessionMeta
from railmux.ui.projects_pane import ProjectsPane
from railmux.ui.running_pane import RunningEntry, RunningSessionsPane
from railmux.ui.sessions_pane import SessionsPane
from railmux.ui.sidebar import (
    SidebarSection,
    StableWeightedPile,
    UnifiedSidebarFrame,
)


def _sidebar():
    project = Project(
        Path("/work/a-project-name-that-is-long"),
        "project",
        Path("/metadata/project"),
        1,
        1.0,
    )
    session = SessionMeta(
        project,
        "s" * 36,
        Path("/metadata/project/session.jsonl"),
        "A session",
        1,
        1,
        1.0,
    )
    projects = ProjectsPane(
        [project], lambda *_: None, boxed=False)
    sessions = SessionsPane(
        lambda *_a, **_k: None, lambda *_: None, boxed=False)
    sessions.set_sessions(
        project, [session], running_ids=set(), favorite_ids=set())
    running = RunningSessionsPane(
        lambda *_a, **_k: None, boxed=False)
    running.set_running([RunningEntry("cx-one", "project/agent")])
    pile = StableWeightedPile([
        ("weight", 2, SidebarSection(projects, lambda: "Projects")),
        ("weight", 4, SidebarSection(
            sessions, lambda: sessions.section_title)),
        ("weight", 2, SidebarSection(
            running, lambda: running.section_title)),
    ])
    frame = UnifiedSidebarFrame(pile, (projects, sessions, running))
    return frame, pile, (projects, sessions, running)


def test_footer_expansion_charges_only_the_running_section():
    _frame, pile, _panes = _sidebar()
    collapsed = pile.get_item_rows((60, 24), focus=False)

    pile.set_bottom_row_debt(1)
    expanded = pile.get_item_rows((60, 23), focus=False)

    assert expanded[:2] == collapsed[:2]
    assert expanded[2] == collapsed[2] - 1


def _row_attrs(canvas, row_index: int) -> set[str | None]:
    return {
        attr
        for attr, _charset, text in list(canvas.content())[row_index]
        for _ in text
    }


def _row_text(canvas, row_index: int) -> str:
    return canvas.text[row_index].decode()


def _column_attr(canvas, row_index: int, column: int) -> str | None:
    remaining = column
    for attr, _charset, text in list(canvas.content())[row_index]:
        width = urwid.str_util.calc_width(text, 0, len(text))
        if remaining < width:
            return attr
        remaining -= width
    raise AssertionError(f"column {column} is outside row {row_index}")


def _inner_row_attrs(canvas, row_index: int) -> set[str | None]:
    maxcol = canvas.cols()
    return {
        _column_attr(canvas, row_index, column)
        for column in range(1, maxcol - 1)
    }


def test_narrow_shared_frame_keeps_every_stable_section_name_visible():
    frame, _pile, _panes = _sidebar()

    text = b"\n".join(frame.render((24, 16), focus=True).text).decode()

    assert "Projects" in text
    assert "Sessions" in text
    assert "Running" in text
    assert "Sessions (" in text
    assert "…" in text
    assert all(line[0] in "│┌└├" and line[-1] in "│┐┘┤"
               for line in text.splitlines())
    assert all("│" not in line[1:-1] for line in text.splitlines())


def test_section_headers_expose_distinct_focus_chrome():
    frame, pile, _panes = _sidebar()
    size = (32, 18)

    pile.focus_position = 0
    projects_canvas = frame.render(size, focus=True)
    inner_rows = pile.get_item_rows((30, 17), True)
    assert _inner_row_attrs(projects_canvas, 0) == {"pane_focus"}
    assert _inner_row_attrs(projects_canvas, inner_rows[0]) == {
        "pane", "pane_focus"}
    assert "─ Projects " in _row_text(projects_canvas, 0)
    assert "─ Sessions " in _row_text(projects_canvas, inner_rows[0])
    assert "pane_focus" not in _inner_row_attrs(
        projects_canvas, size[1] - 1)
    assert "pane_focus" not in _inner_row_attrs(projects_canvas, 1)

    pile.focus_position = 1
    inner_rows = pile.get_item_rows((30, 17), True)
    sessions_canvas = frame.render(size, focus=True)
    sessions_header = inner_rows[0]
    running_header = inner_rows[0] + inner_rows[1]
    assert "pane_focus" not in _inner_row_attrs(sessions_canvas, 0)
    assert _inner_row_attrs(
        sessions_canvas, sessions_header) == {"pane_focus"}
    assert _inner_row_attrs(sessions_canvas, running_header) == {
        "pane", "pane_focus"}
    assert "─ Sessions " in _row_text(sessions_canvas, sessions_header)
    assert "─ Running " in _row_text(sessions_canvas, running_header)
    assert "pane_focus" not in _inner_row_attrs(
        sessions_canvas, size[1] - 1)

    pile.focus_position = 2
    inner_rows = pile.get_item_rows((30, 17), True)
    running_canvas = frame.render(size, focus=True)
    running_header = inner_rows[0] + inner_rows[1]
    assert _inner_row_attrs(
        running_canvas, running_header) == {"pane_focus"}
    assert _inner_row_attrs(
        running_canvas, size[1] - 1) == {"pane_focus"}
    assert "─ Running " in _row_text(running_canvas, running_header)
    assert set(_row_text(running_canvas, size[1] - 1)[1:-1]) == {"─"}


def test_both_vertical_rails_follow_the_focused_section_span():
    frame, pile, _panes = _sidebar()
    size = (32, 18)
    rows = pile.get_item_rows((30, 17), True)
    expected_spans = (
        range(0, rows[0] + 1),
        range(rows[0], rows[0] + rows[1] + 1),
        range(rows[0] + rows[1], size[1]),
    )

    for focus_position, span in enumerate(expected_spans):
        pile.focus_position = focus_position
        canvas = frame.render(size, focus=True)
        for column in (0, size[0] - 1):
            assert {
                row for row in range(size[1])
                if _column_attr(canvas, row, column) == "pane_focus"
            } == set(span)
        start, end = span.start, span.stop - 1
        assert _row_text(canvas, start)[0] == "┌"
        assert _row_text(canvas, start)[-1] == "┐"
        assert _row_text(canvas, end)[0] == "└"
        assert _row_text(canvas, end)[-1] == "┘"
        if end - start > 1:
            assert _row_text(canvas, start + 1)[0] == "│"
            assert _row_text(canvas, start + 1)[-1] == "│"

    canvas = frame.render(size, focus=False)
    for column in (0, size[0] - 1):
        assert {
            _column_attr(canvas, row, column) for row in range(size[1])
        } == {"pane"}
    assert _row_text(canvas, 0)[0] == "┌"
    assert _row_text(canvas, 0)[-1] == "┐"
    assert _row_text(canvas, size[1] - 1)[0] == "└"
    assert _row_text(canvas, size[1] - 1)[-1] == "┘"
    for boundary in (rows[0], rows[0] + rows[1]):
        assert _row_text(canvas, boundary)[0] == "├"
        assert _row_text(canvas, boundary)[-1] == "┤"
    assert _row_text(canvas, 1)[0] == _row_text(canvas, 1)[-1] == "│"


def test_agent_focus_uses_the_title_rule_colour_on_the_bottom_rule():
    frame, _pile, _panes = _sidebar()
    size = (32, 18)

    canvas = frame.render(size, focus=False)

    assert _inner_row_attrs(canvas, size[1] - 1) == {"pane"}


def test_unfocused_sidebar_retains_titles_but_subdues_pinned_dividers():
    frame, pile, _panes = _sidebar()
    size = (32, 18)
    rows = pile.get_item_rows((30, 17), False)

    canvas = frame.render(size, focus=False)
    headers = (0, rows[0], rows[0] + rows[1])
    for header in headers:
        assert _inner_row_attrs(canvas, header) == {"pane"}
        assert _row_text(canvas, header)[1:].startswith("─ ")
        assert "━" not in _row_text(canvas, header)

    # Projects and Sessions each pin a New row directly below their title.
    # Their following separators are intentionally secondary chrome.
    assert _inner_row_attrs(canvas, 2) == {"dim"}
    assert _inner_row_attrs(canvas, rows[0] + 2) == {"dim"}
    assert all(
        "pane_focus" not in _row_attrs(canvas, row)
        for row in range(size[1])
    )


def test_weighted_section_heights_do_not_change_with_focus():
    _frame, pile, _panes = _sidebar()
    allocations = []

    for focus_position in range(3):
        pile.focus_position = focus_position
        allocations.append(pile.get_item_rows((30, 17), True))

    assert allocations[0] == allocations[1] == allocations[2]
    assert allocations[0] == [4, 9, 4]


def test_wheel_routes_by_pointer_section_including_shared_chrome():
    frame, pile, panes = _sidebar()
    size = (32, 18)
    rows = pile.get_item_rows((30, 17), True)
    for pane in panes:
        pane._listbox.keypress = MagicMock(return_value=None)

    assert frame.mouse_event(size, "mouse press", 5, 0, 0, True)
    panes[0]._listbox.keypress.assert_called_once()

    sessions_header = rows[0]
    assert frame.mouse_event(
        size, "mouse press", 5, 10, sessions_header, True)
    panes[1]._listbox.keypress.assert_called_once()

    running_header = rows[0] + rows[1]
    assert frame.mouse_event(
        size, "mouse press", 4, 31, running_header, True)
    panes[2]._listbox.keypress.assert_called_once()

    assert frame.mouse_event(size, "mouse press", 5, 10, 17, True)
    assert panes[2]._listbox.keypress.call_count == 2
