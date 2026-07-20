"""Tests for railmux.ui.modals — PathBrowser directory navigation and filter."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import urwid

from railmux.models import (
    AttentionCategory,
    AttentionState,
    Project,
    SessionMeta,
)
from railmux.ui.app import App

from railmux.ui.modals import (
    ContextMenu,
    DeleteConfirmModal,
    HelpModal,
    PathBrowser,
    PathBrowserModal,
    ProjectInfoModal,
    QuitConfirmModal,
    RenameModal,
    RunningInfoModal,
    SessionInfoModal,
    YoloConfirmModal,
    _BrowserRow,
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


def _attention_session(tmp_path: Path) -> SessionMeta:
    project = Project(
        real_path=tmp_path,
        encoded_name="-tmp-project",
        claude_dir=tmp_path / ".claude",
        session_count=1,
        last_activity_ts=1.0,
    )
    return SessionMeta(
        project=project,
        session_id="11111111-1111-1111-1111-111111111111",
        jsonl_path=tmp_path / "rollout.jsonl",
        title="Attention test",
        message_count=2,
        token_total=10,
        last_mtime=1.0,
        attention=AttentionState(
            AttentionCategory.UNKNOWN_ERROR,
            "Provider reported an error.",
            retryable=True,
        ),
    )


def _rendered_text(widget, size=(60, 24)) -> str:
    canvas = widget.render(size, focus=False)
    return "\n".join(line.decode(errors="replace") for line in canvas.text)


def _rendered_attrs(widget, size=(60, 24)) -> set[str | None]:
    canvas = widget.render(size, focus=False)
    return {
        attr
        for row in canvas.content()
        for attr, _charset, _text in row
    }


def test_delete_confirm_height_tracks_content_and_caps_long_names():
    short = DeleteConfirmModal(
        "Delete session", "short", "Permanent removal.",
        on_confirm=lambda: None, on_cancel=lambda: None,
    )
    long = DeleteConfirmModal(
        "Delete session", "very long title " * 80, "Permanent removal.",
        on_confirm=lambda: None, on_cancel=lambda: None,
    )

    assert short.preferred_height(40) < long.preferred_height(40)
    assert short.preferred_height(40) <= 10
    assert long.preferred_height(40) == 16


def test_delete_confirm_action_keys_use_high_contrast_attribute():
    modal = DeleteConfirmModal(
        "Delete session", "short", "Permanent removal.",
        on_confirm=lambda: None, on_cancel=lambda: None,
    )

    assert "modal_key" in _rendered_attrs(
        modal, size=(40, modal.preferred_height(40)))


def test_modal_action_legends_use_high_contrast_attribute(tmp_path):
    project = Project(
        real_path=tmp_path,
        encoded_name="project",
        claude_dir=tmp_path / ".claude",
        session_count=0,
        last_activity_ts=0.0,
    )
    modals = [
        ProjectInfoModal(project, on_close=lambda: None),
        QuitConfirmModal(
            on_confirm=lambda: None,
            on_soft_quit=lambda: None,
            on_cancel=lambda: None,
            running_count=1,
        ),
        RenameModal("old title", lambda _title: None, lambda: None),
        YoloConfirmModal(lambda: None, lambda: None),
        SessionInfoModal(None, None, lambda: None),
        RunningInfoModal(
            "agent", "cx-agent", None, None, False, lambda: None),
        PathBrowser(tmp_path, lambda _path: None, allow_create=True),
    ]

    for modal in modals:
        assert "modal_key" in _rendered_attrs(modal), type(modal).__name__


def test_quit_confirm_wraps_all_choices_in_narrow_sidebar():
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=12,
    )
    height = modal.preferred_height(22)

    text = " ".join(
        _rendered_text(modal, size=(22, height)).replace("│", " ").split())

    assert "quit and kill all sessions" in text
    assert "soft quit (keep sessions alive)" in text
    assert "n / Esc = cancel" in text


def test_help_explains_bottom_left_workspace_target_indicator():
    modal = HelpModal(on_close=lambda: None)

    text = _rendered_text(modal, size=(60, 80))
    normalized = " ".join(text.replace("│", " ").split())

    assert "Workspace indicator (bottom-left)" in text
    assert "▣" in text
    assert "◧ / ◨" in text
    assert "⬒ / ⬓" in text
    assert "filled half is the Target pane" in normalized


def test_rename_ctrl_u_clears_entire_title_without_closing():
    submitted = MagicMock()
    cancelled = MagicMock()
    modal = RenameModal("old title", submitted, cancelled)
    modal._edit.set_edit_pos(3)

    assert modal.keypress((40, 12), "ctrl u") is None
    assert modal._edit.edit_text == ""
    assert modal._edit.edit_pos == 0
    assert modal.keypress((40, 12), "ctrl u") is None
    submitted.assert_not_called()
    cancelled.assert_not_called()


def test_rename_legend_documents_clear_shortcut():
    modal = RenameModal("old title", lambda _title: None, lambda: None)

    text = _rendered_text(modal, size=(40, 12))

    assert "Ctrl+U = clear" in text


def test_rename_height_grows_for_long_wrapped_original_and_keeps_actions():
    short = RenameModal("short", lambda _title: None, lambda: None)
    long = RenameModal("很长的原始会话名称" * 30,
                       lambda _title: None, lambda: None)

    assert short.preferred_height(24) < long.preferred_height(24)
    assert long.preferred_height(24) == 18
    text = _rendered_text(long, size=(24, 10))
    assert "save" in text
    assert "clear" in text
    assert "cancel" in text


def test_app_uses_compact_fixed_height_for_short_delete_confirm():
    app = App.__new__(App)
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (40, 24)
    app._right_pane_open = MagicMock(return_value=False)
    app._show_overlay = MagicMock()
    modal = DeleteConfirmModal(
        "Delete session", "short", "Permanent removal.",
        on_confirm=lambda: None, on_cancel=lambda: None,
    )

    app._show_delete_confirm(modal)

    assert app._show_overlay.call_args.kwargs == {
        "width": 54,
        "height": 10,
        "fixed_height": True,
    }


def test_app_sizes_quit_confirm_for_wrapped_choices():
    app = App.__new__(App)
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (30, 24)
    app._right_pane_open = MagicMock(return_value=True)
    app._show_overlay = MagicMock()
    modal = QuitConfirmModal(
        on_confirm=lambda: None,
        on_soft_quit=lambda: None,
        on_cancel=lambda: None,
        running_count=2,
    )

    app._show_quit_confirm(modal)

    assert app._show_overlay.call_args.kwargs == {
        "width": 50,
        "height": modal.preferred_height(24),
        "fixed_height": True,
    }


def test_app_sizes_rename_for_wrapped_existing_title():
    app = App.__new__(App)
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (30, 24)
    app._right_pane_open = MagicMock(return_value=True)
    app._show_overlay = MagicMock()
    modal = RenameModal("long original title " * 10,
                        lambda _title: None, lambda: None)

    app._show_rename_modal(modal)

    assert app._show_overlay.call_args.kwargs == {
        "width": 50,
        "height": modal.preferred_height(24),
        "fixed_height": True,
    }


def test_overlay_dimensions_stay_inside_a_cramped_sidebar():
    app = App.__new__(App)
    app._frame = urwid.SolidFill(" ")
    app._loop = MagicMock()
    app._loop.screen.get_cols_rows.return_value = (20, 10)
    app._right_pane_open = MagicMock(return_value=True)

    app._show_overlay(
        urwid.SolidFill(" "), width=60, height=80,
        fixed_width=False, fixed_height=False,
    )
    assert app._loop.widget.width == ("relative", 96)
    assert app._loop.widget.height == ("relative", 96)

    app._show_overlay(
        urwid.SolidFill(" "), width=36, height=15,
        fixed_width=True, fixed_height=True,
    )
    assert app._loop.widget.width == 18
    assert app._loop.widget.height == 8


def test_delete_confirm_keeps_actions_visible_for_long_name():
    modal = DeleteConfirmModal(
        "Delete session",
        "long session title " * 12,
        "The session file will be permanently removed from disk.",
        on_confirm=lambda: None,
        on_cancel=lambda: None,
    )

    text = _rendered_text(modal, size=(30, 12))

    assert "↵ = confirm" in text
    assert "Esc = cancel" in text


def test_delete_confirm_keeps_actions_visible_for_long_cjk_name():
    modal = DeleteConfirmModal(
        "Kill running session",
        "很长的会话名称" * 20,
        "The detached tmux session will be killed.",
        on_confirm=lambda: None,
        on_cancel=lambda: None,
    )

    text = _rendered_text(modal, size=(24, 8))

    assert "confirm" in text
    assert "cancel" in text


def test_delete_confirm_scrolls_long_body_but_keeps_footer():
    modal = DeleteConfirmModal(
        "Delete session",
        "word " * 80,
        "PERMANENT CONSEQUENCE",
        on_confirm=lambda: None,
        on_cancel=lambda: None,
    )

    for _ in range(12):
        modal.keypress((30, 10), "page down")
    text = _rendered_text(modal, size=(30, 10))

    assert "PERMANENT CONSEQUENCE" in text
    assert "↵ = confirm" in text


def test_session_info_renders_attention_and_retry(tmp_path: Path):
    modal = SessionInfoModal(
        _attention_session(tmp_path), running_label=None, on_close=lambda: None)

    text = _rendered_text(modal)

    assert "attention: unknown error" in text
    assert "Provider reported an error." in text
    assert "Retrying is likely safe." in text


def test_running_info_renders_attention_in_narrow_width(tmp_path: Path):
    session = _attention_session(tmp_path)
    modal = RunningInfoModal(
        label="project/Attention test",
        tmux_name="cx-attention",
        project=session.project,
        session=session,
        is_placeholder=False,
        on_close=lambda: None,
    )

    text = _rendered_text(modal, size=(28, 24))

    assert "! attention:" in text
    assert "unknown error" in text


def test_info_modal_keeps_close_legend_visible_when_body_overflows(
        tmp_path: Path):
    session = _attention_session(tmp_path)
    session = replace(session, last_user_message="very long input " * 80)
    modal = SessionInfoModal(session, None, lambda: None)

    text = _rendered_text(modal, size=(24, 8))

    assert "↵ / Esc = close" in text


# ── basic rendering ─────────────────────────────────────────────────────

def test_browser_renders_dot_and_dotdot(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    browser = _make_browser(tmp_path)
    labels = _row_labels(browser)
    assert labels[0] == ".  (use this path)"
    assert labels[1] == "..  (parent directory)"
    assert any("sub/" in label for label in labels)


def test_browser_dirs_first_then_files(tmp_path: Path):
    (tmp_path / "zzz_file").write_text("")
    (tmp_path / "aaa_dir").mkdir()
    browser = _make_browser(tmp_path)
    labels = _row_labels(browser)
    # Strip first two rows (. and ..)
    entries = labels[2:]
    dirs = [label for label in entries if "/" in label]
    files = [label for label in entries if "/" not in label]
    assert len(dirs) == 1 and "aaa_dir" in dirs[0]
    assert len(files) == 1 and "zzz_file" in files[0]
    # Directory comes before file
    assert labels.index(next(label for label in labels if "aaa_dir" in label)) < \
           labels.index(next(label for label in labels if "zzz_file" in label))


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
    assert any("alpha" in label for label in labels)
    assert not any("beta" in label for label in labels)
    assert not any("gamma" in label for label in labels)


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
    assert any("MyProject" in label for label in labels)


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
