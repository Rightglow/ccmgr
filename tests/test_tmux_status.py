"""Rendering the status line into the outer tmux status bar (ccmgr's only status
surface). Only the pure escaping/style/guard logic of ``_render_status_to_tmux``
is exercised here (tmux itself is stubbed) — the option round-trip, the forced
redraw, and the actual colour rendering are verified manually against a
throwaway tmux session.
"""
from unittest.mock import MagicMock

from ccmgr.ui.app import App


def _status_app(*, enabled=True, session="ccmgr"):
    app = App.__new__(App)
    app._tmux_status_enabled = enabled
    app._tmux_status_session = session
    return app


def _set_option_call(run):
    """The tmux set-option invocation among the captured subprocess calls."""
    for call in run.call_args_list:
        argv = call.args[0]
        if argv[:2] == ["tmux", "set-option"]:
            return argv
    raise AssertionError("no tmux set-option call captured")


def _payload(run):
    """The status-right value pushed by the set-option call."""
    return _set_option_call(run)[5]


def test_escapes_hash_and_percent_inside_style(monkeypatch):
    # tmux runs status strings through #{...} format expansion AND strftime, so
    # both '#' and '%' in the BODY must be doubled. The style prefix is added
    # after escaping, so its '#[' stays a real directive (single '#').
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("50% done #{x} #[bold] /a#b", "info")

    argv = _set_option_call(run)
    assert argv[:5] == ["tmux", "set-option", "-t", "ccmgr", "status-right"]
    assert argv[5] == "#[bg=colour236,fg=colour114]50%% done ##{x} ##[bold] /a##b#[default]"


def test_forces_status_redraw(monkeypatch):
    # tmux only auto-repaints the bar every status-interval seconds; a short
    # status message must trigger an immediate redraw or it never shows.
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)

    _status_app()._render_status_to_tmux("hi", "info")

    argvs = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "refresh-client", "-S"] in argvs
    # set-option must come before the refresh so the redraw shows the new value.
    assert argvs.index(_set_option_call(run)) < argvs.index(["tmux", "refresh-client", "-S"])


def test_level_styles_differ(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr("subprocess.run", run)
    app = _status_app()

    app._render_status_to_tmux("boom", "error")
    assert _payload(run) == "#[bg=colour52,fg=colour231,bold]boom#[default]"

    run.reset_mock()
    app._render_status_to_tmux("careful", "warn")
    assert _payload(run) == "#[bg=colour236,fg=colour214,bold]careful#[default]"

    run.reset_mock()
    app._render_status_to_tmux("hint", "tip")
    assert _payload(run) == "#[bg=colour236,fg=colour245]hint#[default]"


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
