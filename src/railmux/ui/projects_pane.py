"""Top sidebar pane: projects for the currently browsed agent provider."""
from __future__ import annotations

from collections.abc import Callable

from railmux.fuzzy import fuzzy_match

import urwid

from railmux.models import Project
from railmux.ui._widgets import ClickableRow, remember_focus, restore_focus


class _ProjectRow(ClickableRow):
    def __init__(self, project: Project, selected: bool = False,
                 on_click: "Callable[[], None] | None" = None,
                 on_double_click: "Callable[[], None] | None" = None) -> None:
        self.project = project
        label = f"{project.display_name} [{project.session_count}]"
        attr = "selected" if selected else None
        super().__init__(urwid.AttrMap(urwid.Text(label), attr, focus_map="focus"),
                         on_click, on_double_click,
                         click_key=project.encoded_name,
                         immediate_click=True)


class _NewProjectRow(ClickableRow):
    def __init__(self, on_click: "Callable[[], None] | None" = None) -> None:
        super().__init__(
            urwid.AttrMap(urwid.Text("+ New project", align="left"), "dim", focus_map="focus"),
            on_click,
        )


class ProjectsPane(urwid.WidgetWrap):
    """Pinned + New project header + scrollable list of projects below it.

    Layout:
        Pile([
            ("pack", new_project_row),
            ("pack", Divider),
            ("weight", 1, ListBox(projects)),
        ])

    Focus moves within the ListBox naturally; pressing up at the top of
    the ListBox bubbles to the Divider (non-selectable, auto-skipped) and
    then to the new_project_row. Pressing down at the bottom is consumed
    so focus does not escape into a sibling sidebar pane.
    """

    def __init__(self, projects: list[Project],
                 on_select: Callable[[Project | None], None],
                 on_double_click: Callable[[Project | None], None] | None = None,
                 provider_label: str = "Agent") -> None:
        self._all_projects = projects
        self._on_select = on_select
        self._on_double_click = on_double_click
        self._provider_label = provider_label
        self._filter = ""
        self._selected_encoded_name: str | None = None

        self._new_row = _NewProjectRow(on_click=lambda: self._on_select(None))
        self._walker = urwid.SimpleFocusListWalker(self._build_rows(projects))
        self._listbox = urwid.ListBox(self._walker)
        self._pile = urwid.Pile([
            ("pack", self._new_row),
            ("pack", urwid.Divider("─")),
            ("weight", 1, self._listbox),
        ])
        # Start with focus on the ListBox so j/k works immediately.
        if self._walker:
            self._pile.focus_position = 2
        # Give body cells an explicit attribute so App's outer pane-focus map
        # colours only the LineBox chrome (border/title), never ordinary rows.
        self._body = urwid.AttrMap(self._pile, "body")
        super().__init__(urwid.LineBox(self._body, title="Projects"))

    def _build_rows(self, projects: list[Project]) -> list:
        needle = self._filter.lower()
        sel = self._selected_encoded_name
        rows = [
            _ProjectRow(p, selected=(p.encoded_name == sel),
                        on_click=lambda p=p: self._on_select(p),
                        on_double_click=(lambda p=p: self._on_double_click(p))
                                        if self._on_double_click else None)
            for p in projects
            if fuzzy_match(needle, str(p.real_path))
        ]
        if not rows:
            text = (
                "  (no matches)"
                if self._filter
                else (
                    f"No {self._provider_label} projects yet\n"
                    "Choose + New project above to start"
                )
            )
            rows = [urwid.Text(text, align="center")]
        return rows

    def set_provider_label(self, label: str) -> None:
        """Update provider-specific empty text, including empty-to-empty switches."""
        if self._provider_label == label:
            return
        self._provider_label = label
        self._refresh_rows()

    def set_projects(self, projects: list[Project]) -> None:
        if self._all_projects == projects:
            return
        self._all_projects = projects
        self._refresh_rows()

    def set_selected(self, encoded_name: str | None) -> None:
        if self._selected_encoded_name == encoded_name:
            return
        self._selected_encoded_name = encoded_name
        self._refresh_rows()

    def set_filter(self, needle: str) -> None:
        if self._filter == needle:
            return
        self._filter = needle
        self._refresh_rows()

    @property
    def filter_text(self) -> str:
        return self._filter

    def _refresh_rows(self) -> None:
        # Remember the currently-focused row's identity (project encoded_name).
        prior_focus = self._remember_focus()

        new_rows = self._build_rows(self._all_projects)
        self._walker[:] = new_rows

        self._restore_focus(prior_focus)

    @staticmethod
    def _row_key(row: "_ProjectRow") -> str:
        return row.project.encoded_name

    def _remember_focus(self) -> str | None:
        return remember_focus(self._walker, _ProjectRow, self._row_key)

    def _restore_focus(self, encoded_name: str | None) -> None:
        restore_focus(self._walker, _ProjectRow, encoded_name, self._row_key)

    def focused_project(self) -> Project | None:
        if self._pile.focus_position == 0:
            return None
        if not self._walker:
            return None
        focus_w, _ = self._walker.get_focus()
        if isinstance(focus_w, _ProjectRow):
            return focus_w.project
        return None

    def is_focus_new_project(self) -> bool:
        return self._pile.focus_position == 0

    def keypress(self, size, key):
        if key == "enter":
            if self.is_focus_new_project():
                self._on_select(None)
                return None
            proj = self.focused_project()
            if proj is not None:
                # Enter = double-click equivalent (steals focus to sessions).
                if self._on_double_click:
                    self._on_double_click(proj)
                else:
                    self._on_select(proj)
                return None
        # Consume up/down at pane boundaries — Tab/Shift-Tab is the only way
        # to switch panes, preventing accidental overscroll into sibling panes.
        if key == "up" and self._pile.focus_position == 0:
            return None
        if key == "down" and self._pile.focus_position == 2 and self._walker:
            cur = self._walker.focus
            last = None
            for i, w in enumerate(self._walker):
                if isinstance(w, _ProjectRow):
                    last = i
            if last is not None and cur == last:
                return None
        return super().keypress(size, key)
