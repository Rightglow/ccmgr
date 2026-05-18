"""Top sidebar pane: list of Claude projects."""
from __future__ import annotations

from collections.abc import Callable

import urwid

from ccmgr.models import Project


class _ProjectRow(urwid.WidgetWrap):
    def __init__(self, project: Project, selected: bool = False) -> None:
        label = f"{project.display_name} [{project.session_count}]"
        self.project = project
        self._text = urwid.Text(label)
        attr = "selected" if selected else None
        super().__init__(urwid.AttrMap(self._text, attr, focus_map="focus"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


class _NewProjectRow(urwid.WidgetWrap):
    def __init__(self) -> None:
        self._text = urwid.Text("+ New project", align="left")
        super().__init__(urwid.AttrMap(self._text, "dim", focus_map="focus"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key):
        return key


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

    def __init__(self, projects: list[Project], on_select: Callable[[Project | None], None]) -> None:
        self._all_projects = projects
        self._on_select = on_select
        self._filter = ""
        self._selected_encoded_name: str | None = None

        self._new_row = _NewProjectRow()
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
        super().__init__(urwid.LineBox(self._pile, title="Projects"))

    def _build_rows(self, projects: list[Project]) -> list:
        needle = self._filter.lower()
        sel = self._selected_encoded_name
        rows = [
            _ProjectRow(p, selected=(p.encoded_name == sel))
            for p in projects
            if needle in str(p.real_path).lower()
        ]
        if not rows:
            rows = [urwid.Text("  (no matches)" if self._filter else "  (no projects)", align="left")]
        return rows

    def set_projects(self, projects: list[Project]) -> None:
        self._all_projects = projects
        self._refresh_rows()

    def set_selected(self, encoded_name: str | None) -> None:
        if self._selected_encoded_name == encoded_name:
            return
        self._selected_encoded_name = encoded_name
        self._refresh_rows()

    def set_filter(self, needle: str) -> None:
        self._filter = needle
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        # Remember the currently-focused row's identity (project encoded_name).
        prior_focus = self._remember_focus()

        new_rows = self._build_rows(self._all_projects)
        self._walker[:] = new_rows

        self._restore_focus(prior_focus)

    def _remember_focus(self) -> str | None:
        """Return the encoded_name of the focused project, or None."""
        if not self._walker:
            return None
        focus_w, _ = self._walker.get_focus()
        if isinstance(focus_w, _ProjectRow):
            return focus_w.project.encoded_name
        return None

    def _restore_focus(self, encoded_name: str | None) -> None:
        if not self._walker:
            return
        if encoded_name is not None:
            for i, w in enumerate(self._walker):
                if isinstance(w, _ProjectRow) and w.project.encoded_name == encoded_name:
                    self._walker.set_focus(i)
                    return
        # Fallback: first selectable row.
        for i, w in enumerate(self._walker):
            if isinstance(w, _ProjectRow):
                self._walker.set_focus(i)
                return
        self._walker.set_focus(0)

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
                self._on_select(proj)
                return None
        # Consume up/down at the boundary so focus doesn't escape into the
        # sibling Sessions pane.
        if key == "up" and self._pile.focus_position == 0:
            return None
        if key == "down":
            # If focus is in the ListBox and we're at the last selectable row, consume.
            if self._pile.focus_position == 2 and self._walker:
                cur = self._walker.focus
                last_selectable_idx = None
                for i, w in enumerate(self._walker):
                    if isinstance(w, _ProjectRow):
                        last_selectable_idx = i
                if last_selectable_idx is not None and cur == last_selectable_idx:
                    return None
        return super().keypress(size, key)
