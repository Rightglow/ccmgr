"""Rendering the status line into the outer tmux status bar (railmux's only status
surface). Only the pure escaping/style/guard logic of ``_render_status_to_tmux``
is exercised here (tmux itself is stubbed) — the option round-trip, the forced
redraw, and the actual colour rendering are verified manually against a
throwaway tmux session.
"""
from unittest.mock import MagicMock

from railmux.ui.app import (
    App,
    _TMUX_BAR_STYLE_NORMAL,
    _TMUX_BAR_STYLE_ERROR,
    _TMUX_BRAND_NORMAL,
    _TMUX_BRAND_ERROR,
    _TMUX_LEVEL_STYLE,
    _compact_tmux_status_left,
    _tmux_status_left,
)
from railmux.ui.workspace import (
    AgentWorkspace,
    WorkspaceLayout,
    WorkspacePage,
    WorkspacePresentation,
)


def _status_app(*, enabled=True, session="railmux", codex_mode=False):
    app = App.__new__(App)
    app._tmux_status_enabled = enabled
    app._tmux_status_session = session
    app._codex_mode = codex_mode
    return app


def _style_calls(run, option):
    """Every value pushed to ``option`` via tmux set-option, in call order."""
    return [
        c.args[0][5]
        for c in run.call_args_list
        if c.args[0][:2] == ["tmux", "set-option"] and c.args[0][4] == option
    ]


def _status_right_call(run):
    """The full argv of the status-right set-option (skips the bar-style swap)."""
    for call in run.call_args_list:
        argv = call.args[0]
        if argv[:2] == ["tmux", "set-option"] and argv[4] == "status-right":
            return argv
    raise AssertionError("no status-right set-option captured")


def _payload(run):
    """The status-right value pushed by the set-option call."""
    return _status_right_call(run)[5]


def test_escapes_hash_and_percent_inside_style(monkeypatch):
    # tmux runs status strings through #{...} format expansion AND strftime, so
    # both '#' and '%' in the BODY must be doubled. The style prefix is added
    # after escaping, so its '#[' stays a real directive (single '#').
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("50% done #{x} #[bold] /a#b", "info")

    argv = _status_right_call(run)
    assert argv[:5] == ["tmux", "set-option", "-t", "railmux", "status-right"]
    assert argv[5] == "#[fg=colour231]50%% done ##{x} ##[bold] /a##b#[default]"


def test_forces_status_redraw(monkeypatch):
    # tmux only auto-repaints the bar every status-interval seconds; a short
    # status message must trigger an immediate redraw or it never shows.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("hi", "info")

    argvs = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "refresh-client", "-S"] in argvs
    # set-option must come before the refresh so the redraw shows the new value.
    assert argvs.index(_status_right_call(run)) < argvs.index(["tmux", "refresh-client", "-S"])


def test_level_styles_differ(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("boom", "error")
    assert _payload(run) == "#[fg=colour231,bold]boom#[default]"

    run.reset_mock()
    app._render_status_to_tmux("careful", "warn")
    assert _payload(run) == "#[fg=colour220,bold]careful#[default]"

    run.reset_mock()
    app._render_status_to_tmux("hint", "tip")
    assert _payload(run) == "#[fg=colour0]hint#[default]"


def test_unknown_level_is_unstyled(monkeypatch):
    # A level with no mapping falls back to raw (escaped) text, no #[...] wrap.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("plain #x", "bogus")

    assert _payload(run) == "plain ##x"


def test_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_noop_without_session(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(session=None)._render_status_to_tmux("anything", "error")

    run.assert_not_called()


def test_swallows_tmux_errors(monkeypatch):
    # A tmux failure must never propagate into the UI thread.
    def boom(*_a, **_k):
        raise OSError("tmux gone")
    monkeypatch.setattr("subprocess.run", boom)

    # Should not raise.
    _status_app()._render_status_to_tmux("hello", "info")


# ── whole-bar error flip (green normal, dark red on error) ───────────────

def test_error_flips_whole_bar_then_reverts(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()  # starts in normal (green) mode

    app._render_status_to_tmux("ERROR: boom", "error")
    assert app._tmux_error_bar is True
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_ERROR
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(True, False)

    run.reset_mock()
    app._render_status_to_tmux("→ back to normal", "info")
    assert app._tmux_error_bar is False
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_NORMAL
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(False, False)


def test_non_error_levels_leave_the_bar_green(monkeypatch):
    # info/warn/tip must not touch status-style/status-left — the bar stays green
    # and only the status-right text colour changes.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    for level in ("info", "warn", "tip"):
        run.reset_mock()
        app._render_status_to_tmux("msg", level)
        assert _style_calls(run, "status-style") == []
        assert _style_calls(run, "status-left") == []
        assert app._tmux_error_bar is False


def test_error_bar_not_repainted_while_staying_in_error(monkeypatch):
    # A second error render (or a held error re-render) must not re-push the bar
    # style — the swap only fires on the normal↔error transition.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("ERROR: one", "error")
    run.reset_mock()
    app._render_status_to_tmux("ERROR: two", "error")
    assert _style_calls(run, "status-style") == []  # no re-paint
    assert _payload(run) == "#[fg=colour231,bold]ERROR: two#[default]"


def test_apply_bar_sets_normal_and_error_styles(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._apply_tmux_bar(error=False)
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_NORMAL
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(False, False)

    run.reset_mock()
    app._apply_tmux_bar(error=True)
    assert _style_calls(run, "status-style")[-1] == _TMUX_BAR_STYLE_ERROR
    assert _style_calls(run, "status-left")[-1] == _tmux_status_left(True, False)


def test_status_left_shows_current_mode(monkeypatch):
    # The brand carries a "· Claude Code" / "· Codex" indicator that reflects
    # the current mode; toggling mode repaints status-left.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(codex_mode=False)._apply_tmux_bar(error=False)
    claude_left = _style_calls(run, "status-left")[-1]
    assert "Claude Code" in claude_left and "Codex" not in claude_left

    run.reset_mock()
    _status_app(codex_mode=True)._apply_tmux_bar(error=False)
    codex_left = _style_calls(run, "status-left")[-1]
    assert codex_left.endswith("· Codex #[default]")
    assert claude_left != codex_left


def test_status_left_pure_function():
    # Brand prefix stays; the mode segment is in the same weight as tips (no bold).
    normal = _tmux_status_left(False, False)
    assert normal.startswith(_TMUX_BRAND_NORMAL)
    assert normal.endswith("· Claude Code #[default]")
    assert "bold" not in normal
    error = _tmux_status_left(True, True)
    assert error.startswith(_TMUX_BRAND_ERROR)
    assert error.endswith("· Codex #[default]")

    third = _tmux_status_left(False, "Review Agent")
    assert third.endswith("· Review Agent #[default]")
    assert "bold" not in error

    with_layout = _tmux_status_left(False, "Codex", "◨")
    assert with_layout.endswith("· Codex · ◨ #[default]")


def test_compact_status_left_has_stable_phone_navigation_and_mode_abbreviation():
    value, visible = _compact_tmux_status_left(
        False,
        "Claude Code",
        WorkspacePage.PRIMARY,
        ("%1", "%2", "%3"),
        40,
    )

    assert "[R]" in value and "[1]" in value and "[2]" in value
    assert " CC " in value
    assert "#[fg=colour231][1]" in value
    assert "#[fg=colour0][R]" in value
    assert visible == len("[R][1][2] CC ")


def test_compact_status_left_expands_without_shortening_tip_pool():
    value, visible = _compact_tmux_status_left(
        False,
        "Codex",
        WorkspacePage.SIDEBAR,
        ("%1", "%2", "%3"),
        105,
    )

    assert "[Railmux]" in value
    assert "[Agent 1]" in value
    assert "[Agent 2]" in value
    assert " Codex " in value
    assert visible == len("[Railmux][Agent 1][Agent 2] Codex ")


def test_apply_bar_uses_dynamic_compact_left_length(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.presentation = WorkspacePresentation.COMPACT
    app._workspace.compact_page = WorkspacePage.SIDEBAR
    app._workspace.primary.pane_id = "%2"
    app._railmux_pane_id = "%1"
    app._last_workspace_size = (40, 20)
    app._tmux_binding_manager = None

    app._apply_tmux_bar(error=False)

    lengths = _style_calls(run, "status-left-length")
    assert lengths[-1] == str(len("[R][1][2] CC "))
    right_lengths = _style_calls(run, "status-right-length")
    assert right_lengths[-1] == str(40 - len("[R][1][2] CC "))

    run.reset_mock()
    app._workspace.presentation = WorkspacePresentation.WIDE
    app._apply_tmux_bar(error=False)
    assert _style_calls(run, "status-right-length")[-1] == str(
        app._TMUX_STATUS_RIGHT_LENGTH)


def test_status_left_keeps_layout_and_target_visible_across_focus(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()
    app._workspace = AgentWorkspace()
    app._workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    app._workspace.primary.pane_id = "%2"
    app._workspace.secondary.pane_id = "%3"
    app._workspace.set_target(AgentWorkspace.SECONDARY)
    app._railmux_has_focus = True

    app._apply_tmux_bar(error=False)
    assert "· Claude Code · ◨" in _style_calls(run, "status-left")[-1]

    run.reset_mock()
    app._railmux_has_focus = False
    app._apply_tmux_bar(error=False)
    assert "· Claude Code · ◨" in _style_calls(run, "status-left")[-1]


def test_layout_indicator_maps_orientation_and_target():
    app = _status_app()
    app._workspace = AgentWorkspace()
    workspace = app._workspace

    assert app._status_layout_indicator() is None
    workspace.primary.pane_id = "%2"
    assert app._status_layout_indicator() == "▣"

    workspace.secondary.pane_id = "%3"
    workspace.layout = WorkspaceLayout.SIDE_BY_SIDE
    assert app._status_layout_indicator() == "◧"
    workspace.set_target(AgentWorkspace.SECONDARY)
    assert app._status_layout_indicator() == "◨"

    workspace.layout = WorkspaceLayout.STACKED
    assert app._status_layout_indicator() == "⬓"
    workspace.set_target(AgentWorkspace.PRIMARY)
    assert app._status_layout_indicator() == "⬒"


def test_apply_bar_noop_when_disabled(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app(enabled=False)._apply_tmux_bar(error=True)

    run.assert_not_called()


def test_level_styles_are_pill_free():
    # The design decision: info/warn/tip sit directly on the green bar and error
    # sits on the whole-bar red — so NO per-level style carries its own bg.
    for level, style in _TMUX_LEVEL_STYLE.items():
        assert "bg=" not in style, f"{level} should not set its own background"
    # warn is the only non-error level bolded (alert without a pill); error too.
    assert "bold" in _TMUX_LEVEL_STYLE["warn"]
    assert "bold" in _TMUX_LEVEL_STYLE["error"]
    # The bar/brand styles are what carry the background.
    assert "bg=" in _TMUX_BAR_STYLE_NORMAL and "bg=" in _TMUX_BAR_STYLE_ERROR


def test_normal_bar_uses_grass_green_accent():
    assert _TMUX_BAR_STYLE_NORMAL.startswith("bg=#5faf00,")
