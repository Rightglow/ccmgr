"""Tests for railmux.ui.modals — PathBrowser directory navigation and filter."""

import tempfile
from pathlib import Path

import pytest
import urwid

from railmux.ui.modals import (
    ContextMenu, PathBrowser, PathBrowserModal, _BrowserRow,
)


def _make_browser(path: Path, on_select=None) -> PathBrowser:
    return PathBrowser(path, on_select or (lambda p: None))


def _row_count(browser: PathBrowser) -> int:
    return len([w for w in browser._walker if isinstance(w, _BrowserRow)])


def _row_labels(browser: PathBrowser) -> list[str]:
    return [
        w._wrapped_widget.base_widget.text
        for w in browser._walker
        if isinstance(w, _BrowserRow)
    ]


# ── basic rendering ─────────────────────────────────────────────────────

def test_browser_renders_dot_and_dotdot(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    browser = _make_browser(tmp_path)
    labels = _row_labels(browser)
    assert labels[0] == ".  (use this path)"
    assert labels[1] == "..  (parent directory)"
    assert any("sub/" in l for l in labels)


def test_browser_dirs_first_then_files(tmp_path: Path):
    (tmp_path / "zzz_file").write_text("")
    (tmp_path / "aaa_dir").mkdir()
    browser = _make_browser(tmp_path)
    labels = _row_labels(browser)
    # Strip first two rows (. and ..)
    entries = labels[2:]
    dirs = [l for l in entries if "/" in l]
    files = [l for l in entries if "/" not in l]
    assert len(dirs) == 1 and "aaa_dir" in dirs[0]
    assert len(files) == 1 and "zzz_file" in files[0]
    # Directory comes before file
    assert labels.index([l for l in labels if "aaa_dir" in l][0]) < \
           labels.index([l for l in labels if "zzz_file" in l][0])


# ── filter ──────────────────────────────────────────────────────────────

def test_filter_filters_entries(tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "gamma").mkdir()
    browser = _make_browser(tmp_path)

    browser._filter_edit.set_edit_text("al")
    browser._filter = "al"
    browser._render_list()
    labels = _row_labels(browser)
    assert any("alpha" in l for l in labels)
    assert not any("beta" in l for l in labels)
    assert not any("gamma" in l for l in labels)


def test_filter_no_matches(tmp_path: Path):
    browser = _make_browser(tmp_path)
    browser._filter_edit.set_edit_text("zzznonexistent")
    browser._filter = "zzznonexistent"
    browser._render_list()
    # Should show . , .. , and "(no matches)"
    texts = [str(w) for w in browser._walker]
    assert any("no matches" in t.lower() for t in texts)


def test_browser_can_submit_new_relative_directory(tmp_path: Path):
    selected = []
    browser = PathBrowser(
        tmp_path, selected.append, allow_create=True)
    browser._filter_edit.set_edit_text("new/nested")

    labels = _row_labels(browser)
    assert any("create" in label and "new/nested" in label for label in labels)
    browser.keypress((80, 20), "enter")

    assert selected == [(tmp_path / "new" / "nested").resolve()]
    assert not selected[0].exists()  # App creates it only after confirmation.


def test_browser_can_submit_new_absolute_directory(tmp_path: Path):
    selected = []
    target = tmp_path / "absolute" / "child"
    browser = PathBrowser(Path.home(), selected.append, allow_create=True)
    browser._filter_edit.set_edit_text(str(target))
    browser.keypress((120, 20), "enter")
    assert selected == [target.resolve()]


def test_existing_file_filter_does_not_submit_parent(tmp_path: Path):
    selected = []
    (tmp_path / "project.txt").write_text("not a directory")
    browser = PathBrowser(tmp_path, selected.append, allow_create=True)
    browser._filter_edit.set_edit_text("project.txt")

    browser.keypress((80, 20), "enter")

    assert selected == []
    assert browser._path == tmp_path


def test_filter_case_insensitive(tmp_path: Path):
    (tmp_path / "MyProject").mkdir()
    browser = _make_browser(tmp_path)
    browser._filter_edit.set_edit_text("myproj")
    browser._filter = "myproj"
    browser._render_list()
    labels = _row_labels(browser)
    assert any("MyProject" in l for l in labels)


def test_filter_cleared_on_directory_change(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    browser = _make_browser(tmp_path)
    browser._filter_edit.set_edit_text("foo")
    browser._filter = "foo"

    # Navigate into subdir — filter should be cleared
    browser._path = tmp_path / "sub"
    browser._refresh()
    assert browser._filter == ""
    assert browser._filter_edit.edit_text == ""


# ── Enter key ───────────────────────────────────────────────────────────

def test_enter_on_dot_confirms(tmp_path: Path):
    selected = []
    browser = _make_browser(tmp_path, on_select=lambda p: selected.append(p))
    browser._walker.set_focus(0)
    browser.keypress((20, 20), "enter")
    assert selected == [tmp_path]


def test_enter_on_dotdot_goes_to_parent(tmp_path: Path):
    child = tmp_path / "child"
    child.mkdir()
    browser = _make_browser(child)
    browser._walker.set_focus(1)  # ..
    browser.keypress((20, 20), "enter")
    assert browser._path == tmp_path


def test_enter_on_subdir_descends(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "file.txt").write_text("")
    browser = _make_browser(tmp_path)
    # Find the subdirectory row
    for i, w in enumerate(browser._walker):
        if isinstance(w, _BrowserRow) and "sub/" in w._wrapped_widget.base_widget.text:
            browser._walker.set_focus(i)
            break
    browser.keypress((20, 20), "enter")
    assert browser._path == sub


def test_enter_on_file_does_nothing(tmp_path: Path):
    f = tmp_path / "readme.txt"
    f.write_text("hello")
    browser = _make_browser(tmp_path)
    # Find the file row
    for i, w in enumerate(browser._walker):
        if isinstance(w, _BrowserRow) and "readme.txt" in w._wrapped_widget.base_widget.text:
            browser._walker.set_focus(i)
            break
    path_before = browser._path
    browser.keypress((20, 20), "enter")
    assert browser._path == path_before  # unchanged


# ── backspace ───────────────────────────────────────────────────────────

def test_backspace_routes_to_filter(tmp_path: Path):
    browser = _make_browser(tmp_path)
    browser.keypress((20, 20), "backspace")
    # Focus moved to the filter Edit (position 1 in the header pile)
    assert browser._header_pile.focus_position == 1


def test_printable_char_routes_to_filter_and_updates(tmp_path: Path):
    browser = _make_browser(tmp_path)
    browser.keypress((20, 20), "a")
    assert browser._header_pile.focus_position == 1  # filter focused
    assert "a" in browser._filter_edit.edit_text


# ── Esc ─────────────────────────────────────────────────────────────────

def test_esc_clears_filter_in_browser(tmp_path: Path):
    browser = _make_browser(tmp_path)
    browser._filter_edit.set_edit_text("xyz")
    browser._filter = "xyz"
    result = browser.keypress((20, 20), "esc")
    assert result is None  # consumed
    assert browser._filter == ""


def test_esc_with_empty_filter_falls_through(tmp_path: Path):
    browser = _make_browser(tmp_path)
    result = browser.keypress((20, 20), "esc")
    # Falls through — not consumed by browser
    assert result is not None


def test_modal_esc_cancels(tmp_path: Path):
    cancelled = []
    modal = PathBrowserModal(tmp_path, on_submit=lambda p: None,
                             on_cancel=lambda: cancelled.append(1))
    modal.keypress((20, 20), "esc")
    assert cancelled == [1]


def test_modal_esc_clears_filter_first(tmp_path: Path):
    """Esc with active filter clears it; second Esc cancels."""
    cancelled = []
    modal = PathBrowserModal(tmp_path, on_submit=lambda p: None,
                             on_cancel=lambda: cancelled.append(1))
    browser = modal._browser
    browser._filter_edit.set_edit_text("test")
    browser._filter = "test"

    # First Esc clears filter
    result = modal.keypress((20, 20), "esc")
    assert result is None
    assert browser._filter == ""
    assert cancelled == []  # not yet cancelled

    # Second Esc cancels
    result = modal.keypress((20, 20), "esc")
    assert result is None
    assert cancelled == [1]


# ── ContextMenu ─────────────────────────────────────────────────────────

def test_context_menu_click_fires_callback_then_closes():
    calls = []
    closed = []
    menu = ContextMenu(
        [(" Action 1", lambda: calls.append("a1")),
         (" Action 2", lambda: calls.append("a2"))],
        on_close=lambda: closed.append(1),
    )
    # Click the first item
    menu._walker.set_focus(0)
    menu.keypress((20, 10), "enter")
    assert calls == ["a1"]
    assert closed == [1]


def test_context_menu_esc_closes_without_action():
    closed = []
    menu = ContextMenu(
        [(" Action 1", lambda: None)],
        on_close=lambda: closed.append(1),
    )
    menu.keypress((20, 10), "esc")
    assert closed == [1]


def test_context_menu_selectable():
    menu = ContextMenu([(" Item", lambda: None)], on_close=lambda: None)
    assert menu.selectable()


def test_context_menu_mouse_click():
    calls = []
    closed = []
    menu = ContextMenu(
        [(" Action", lambda: calls.append("x"))],
        on_close=lambda: closed.append(1),
    )
    menu._walker.set_focus(0)
    # Simulate left-click on the first row
    row = menu._walker[0]
    row._on_click()
    assert calls == ["x"]
    assert closed == [1]
