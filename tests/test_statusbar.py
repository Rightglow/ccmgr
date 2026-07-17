"""Status-bar state machine (level classification, TTL expiry, idle tips) plus
the HintBar/ButtonBar footer widgets. The status text itself is rendered into
the outer tmux bar (see test_tmux_status.py), not an in-pane widget."""
import pytest

from railmux.config import Config
from railmux.ui import app as app_mod
from railmux.ui.statusbar import TIPS, ButtonBar, HintBar


@pytest.fixture
def app(tmp_path, monkeypatch):
    ch = tmp_path / ".claude"
    (ch / "projects").mkdir(parents=True)
    # App startup normally re-adopts real detached tmux sessions. Status tests
    # must not inherit the developer machine's external tmux/Codex state or
    # depend on which optional command-line tools the test runner installed.
    monkeypatch.setattr(
        app_mod.App, "_discover_orphans", lambda self, state=None: None)
    monkeypatch.setattr(app_mod.tmux_ctl, "has_tmux", lambda: True)
    real_which = app_mod.shutil.which
    monkeypatch.setattr(
        app_mod.shutil,
        "which",
        lambda command: "/test/bin/claude"
        if command == "claude" else real_which(command),
    )
    return app_mod.App(claude_home=ch, config=Config(), auto_launched=False)


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for railmux.ui.app."""
    now = {"t": 1000.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: now["t"])
    return now


@pytest.fixture
def shown(app, monkeypatch):
    """Record the text handed to the tmux status renderer — railmux's only status
    surface now that the in-pane StatusBar widget is gone. ``shown[-1]`` is what
    the bar currently displays."""
    seen: list[str] = []
    monkeypatch.setattr(
        app, "_render_status_to_tmux",
        lambda text, level="info", refresh=True: seen.append(text))
    return seen


# ── level auto-classification ────────────────────────────────────────────

def test_set_status_classifies_error(app, clock):
    app._set_status("ERROR: tmux not found")
    assert app._status_level == "error"


def test_set_status_classifies_warn(app, clock):
    app._set_status("WARNING: claude not on PATH")
    assert app._status_level == "warn"
    app._set_status("Failed to rename: boom")
    assert app._status_level == "warn"
    app._set_status("failed to re-attach")
    assert app._status_level == "warn"


def test_set_status_classifies_info(app, clock):
    app._set_status("→ new session in proj")
    assert app._status_level == "info"


def test_set_status_explicit_level_overrides_prefix(app, clock):
    app._set_status("ERROR-looking but forced tip", level="tip")
    assert app._status_level == "tip"


# ── TTL expiry → idle tips ───────────────────────────────────────────────

def test_info_message_holds_then_falls_back_to_tip(app, clock, shown):
    app._set_status("→ opened session X")
    # Within TTL: message is held, not clobbered.
    clock["t"] += app._STATUS_TTL["info"] - 0.1
    app._update_status()
    assert app._status_text == "→ opened session X"
    # Past TTL: drops to idle, shows a tip.
    clock["t"] += 1.0
    app._update_status()
    assert app._status_text is None
    assert shown[-1] in TIPS


def test_error_is_sticky(app, clock):
    app._set_status("ERROR: tmux missing")
    clock["t"] += 10_000
    app._update_status()
    assert app._status_text == "ERROR: tmux missing"  # never expires on its own


def test_refresh_does_not_clobber_fresh_message(app, clock):
    """Regression: a refresh tick used to overwrite one-shot messages with the
    focus hint before the user could read them. It must now leave a still-valid
    message untouched."""
    app._set_status("→ opened session X")
    app._refresh()
    assert app._status_text == "→ opened session X"


def test_warn_holds_past_info_ttl(app, clock):
    app._set_status("Failed to do the thing")
    clock["t"] += app._STATUS_TTL["info"] + 0.1  # past info TTL...
    app._update_status()
    assert app._status_text == "Failed to do the thing"  # ...but warn still holds


# ── minimum-hold: severity floor against clobbering ──────────────────────

def test_error_not_clobbered_by_info_within_hold(app, clock):
    app._set_status("ERROR: tmux gone")
    clock["t"] += app._STATUS_MIN_HOLD["error"] - 0.1
    app._set_status("Project: foo (3 sessions)")  # lower severity, too soon
    assert app._status_text == "ERROR: tmux gone"
    assert app._status_level == "error"


def test_error_replaceable_by_info_after_hold(app, clock):
    app._set_status("ERROR: tmux gone")
    clock["t"] += app._STATUS_MIN_HOLD["error"] + 0.1
    app._set_status("Project: foo (3 sessions)")
    assert app._status_text == "Project: foo (3 sessions)"


def test_warn_hold_is_shorter_than_error(app, clock):
    # warn clears sooner than error would: info replaces it past the warn hold
    # but (by construction) still within the error hold window.
    assert app._STATUS_MIN_HOLD["warn"] < app._STATUS_MIN_HOLD["error"]
    app._set_status("Failed to X")
    clock["t"] += app._STATUS_MIN_HOLD["warn"] + 0.1
    app._set_status("routine info")
    assert app._status_text == "routine info"


def test_higher_severity_overrides_immediately(app, clock):
    app._set_status("routine info")
    app._set_status("ERROR: boom")  # same tick, no time passes
    assert app._status_text == "ERROR: boom"
    assert app._status_level == "error"


def test_first_tip_after_expiry_lasts_one_interval(app, clock, shown):
    """Regression: the post-message tip used to linger ~2× the interval because
    the expiry path showed a tip without advancing the rotation clock."""
    app._set_status("info msg")
    clock["t"] += app._STATUS_TTL["info"] + 0.1  # expire → first idle tip
    app._update_status()
    first = shown[-1]
    clock["t"] += app._TIP_INTERVAL - 0.1
    app._update_status()
    assert shown[-1] == first                    # keep it for the full interval
    clock["t"] += 0.2                           # exactly one interval later
    app._update_status()
    assert shown[-1] != first                    # advanced, not stuck at 2×


# ── optional in-pane error row ───────────────────────────────────────────

def _error_row_visible(app) -> bool:
    return any(widget is app._error_bar
               for widget, _options in app._footer.contents)


def test_error_row_uses_no_space_until_needed(app):
    rows_without_error = app._footer.rows((100,))
    assert not _error_row_visible(app)

    app._show_error("Launch failed")
    assert _error_row_visible(app)
    assert app._footer.rows((100,)) == rows_without_error + 1

    app._clear_error()
    assert not _error_row_visible(app)
    assert app._footer.rows((100,)) == rows_without_error


def test_error_row_timeout_removes_optional_footer_row(app):
    app._show_error("Launch failed")
    assert _error_row_visible(app)

    app._on_error_timeout(None, None)

    assert app._error_text.text == ""
    assert not _error_row_visible(app)


# ── idle tip rotation ────────────────────────────────────────────────────

def test_tips_rotate_on_interval(app, clock, shown):
    # First idle update shows a tip immediately.
    app._update_status()
    first = shown[-1]
    assert first in TIPS
    # Before the interval elapses, the tip does not change.
    clock["t"] += app._TIP_INTERVAL - 0.1
    app._update_status()
    assert shown[-1] == first
    # After the interval, it advances to the next tip.
    clock["t"] += 0.2
    app._update_status()
    assert shown[-1] != first
    assert shown[-1] in TIPS


def test_explicit_message_interrupts_tips(app, clock, shown):
    app._update_status()  # show a tip
    app._set_status("→ opened X")
    assert shown[-1] == "→ opened X"
    assert app._status_text == "→ opened X"


# ── HintBar: context-sensitive, two-line wrap, overflow paging ──────────

from railmux.ui import keymap  # noqa: E402


def _hint_rows(bar: HintBar, width: int) -> list[str]:
    canvas = bar.render((width,), False)
    return [t.decode() for t in canvas.text]


# ── idle tip focus guard (regression for CJK IME flicker) ─────────────────

def test_idle_tip_skips_tmux_repaint_while_agent_pane_focused(app, clock, shown):
    """When the right agent pane has focus, idle tip rotation must NOT repaint
    the shared tmux status bar — ``refresh-client -S`` inside the repaint
    path makes the CJK preedit box jump (regression of 18f8c18, re-introduced
    when the status line moved into the outer tmux bar)."""
    app._railmux_has_focus = False
    app._tip_since = 0.0  # tip is due immediately
    shown.clear()
    app._update_status()
    assert shown == [], "must not repaint tmux bar while agent pane focused"


def test_idle_tip_repaints_when_railmux_focused(app, clock, shown):
    """When railmux has focus the idle tip rotation repaints the tmux status
    bar normally — the focus guard only suppresses repaints while the user
    is typing in the agent pane."""
    app._railmux_has_focus = True
    app._tip_since = 0.0  # tip is due immediately
    shown.clear()
    app._update_status()
    assert len(shown) == 1
    assert shown[0] in TIPS


def _cols(s: str) -> int:
    import urwid
    return urwid.calc_width(s, 0, len(s))


def test_hintbar_wide_shows_all_keys_no_ellipsis():
    bar = HintBar()
    bar.set_context(keymap.CTX_SESSIONS)
    rows = _hint_rows(bar, 140)  # wider than the full sessions hint (incl. m Mode)
    joined = "".join(rows)
    assert "…" not in joined
    assert "F9 fullscreen" in joined  # last key is present


def test_hintbar_narrow_still_two_lines_height():
    bar = HintBar()
    bar.set_context(keymap.CTX_SESSIONS)  # widest context
    rows = _hint_rows(bar, 30)
    assert len(rows) == 2                 # fixed two-line height
    assert all(_cols(r) <= 30 for r in rows)


def test_hintbar_narrow_splits_into_multiple_pages():
    """When the text overflows 2 lines it's split into pages (no ellipsis)."""
    bar = HintBar()
    bar.set_context(keymap.CTX_SESSIONS)
    bar._reflow(30)
    assert len(bar._pages) >= 2, "narrow width should need multiple pages"


def test_hintbar_no_paging_when_text_fits():
    """Single page when the full hint fits in 2 lines — no timer needed."""
    bar = HintBar()
    bar.set_context(keymap.CTX_SESSIONS)
    bar._reflow(120)
    assert len(bar._pages) == 1


def test_hintbar_context_switch_resets_to_first_page():
    bar = HintBar()
    bar.set_context(keymap.CTX_SESSIONS)
    bar._reflow(30)
    bar._show_page(1)  # flip to page 2
    assert bar._page_idx == 1
    # Context switch resets.
    bar.set_context(keymap.CTX_RUNNING)
    assert bar._page_idx == 0


def test_hintbar_context_switch_changes_keys():
    bar = HintBar()
    bar.set_context(keymap.CTX_RUNNING)
    running = "".join(_hint_rows(bar, 120))
    bar.set_context(keymap.CTX_SESSIONS)
    sessions = "".join(_hint_rows(bar, 120))
    # Running hides rename/star but supports the same filter entry point.
    assert "rename" not in running
    assert "rename" in sessions
    assert "filter" in running


def test_hintbar_always_two_lines_even_when_short():
    bar = HintBar()
    bar.set_context(keymap.CTX_PROJECTS)
    rows = _hint_rows(bar, 200)  # everything fits on line 1
    assert len(rows) == 2        # height stays fixed
    assert rows[1].strip() == ""


# ── ButtonBar: constant utility row ──────────────────────────────────────

def test_buttonbar_lists_help_quit_detach():
    calls = []
    bar = ButtonBar(
        on_help=lambda: calls.append("help"),
        on_quit=lambda: calls.append("quit"),
        on_detach=lambda: calls.append("detach"),
    )
    canvas = bar.render((60,), False)
    text = "".join(t.decode() for t in canvas.text)
    assert "Help" in text and "Quit" in text and "Detach" in text


def test_buttonbar_button_format():
    """Each button is rendered with key + capitalized desc, underlined."""
    bar = ButtonBar(on_help=lambda: None, on_quit=lambda: None, on_detach=lambda: None)
    canvas = bar.render((60,), False)
    text = "".join(t.decode() for t in canvas.text)
    assert "? Help" in text
    assert "q Quit" in text
    assert "C-b d Detach" in text


def test_buttonbar_mouse_click_hits_button():
    """Mouse click on a button's column range fires the right callback."""
    calls = []
    bar = ButtonBar(
        on_help=lambda: calls.append("help"),
        on_quit=lambda: calls.append("quit"),
        on_detach=lambda: calls.append("detach"),
    )
    # The first hit area (lowest start column) is ? help.
    help_start, help_end = sorted(bar._hit_areas)[0][:2]
    mid = (help_start + help_end) // 2
    bar.mouse_event((60,), "mouse press", 1, mid, 0, False)
    assert calls == ["help"]


def test_buttonbar_mouse_click_miss_does_nothing():
    """Clicking between buttons does not fire any callback."""
    calls = []
    bar = ButtonBar(
        on_help=lambda: calls.append("help"),
        on_quit=lambda: calls.append("quit"),
        on_detach=lambda: calls.append("detach"),
    )
    # Click in the gap between ? help and q quit — gap is 1 column at pos 6.
    bar.mouse_event((60,), "mouse press", 1, 6, 0, False)
    assert calls == []


def test_buttonbar_not_selectable():
    """ButtonBar must not steal keyboard focus from the sidebar."""
    bar = ButtonBar(on_help=lambda: None, on_quit=lambda: None, on_detach=lambda: None)
    assert bar.selectable() is False
