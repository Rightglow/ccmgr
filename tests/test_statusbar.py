"""Status-bar state machine (level classification, TTL expiry, idle tips) and
StatusBar two-line reflow/truncation."""
import pytest

from ccmgr.config import Config
from ccmgr.ui import app as app_mod
from ccmgr.ui.statusbar import TIPS, StatusBar, _LEVEL_ATTR


@pytest.fixture
def app(tmp_path):
    ch = tmp_path / ".claude"
    (ch / "projects").mkdir(parents=True)
    return app_mod.App(claude_home=ch, config=Config(), auto_launched=False)


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for ccmgr.ui.app."""
    now = {"t": 1000.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: now["t"])
    return now


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

def test_info_message_holds_then_falls_back_to_tip(app, clock):
    app._set_status("→ opened session X")
    # Within TTL: message is held, not clobbered.
    clock["t"] += app._STATUS_TTL["info"] - 0.1
    app._update_status()
    assert app._status_text == "→ opened session X"
    # Past TTL: drops to idle, shows a tip.
    clock["t"] += 1.0
    app._update_status()
    assert app._status_text is None
    assert app._status._text in TIPS


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


# ── idle tip rotation ────────────────────────────────────────────────────

def test_tips_rotate_on_interval(app, clock):
    # First idle update shows a tip immediately.
    app._update_status()
    first = app._status._text
    assert first in TIPS
    # Before the interval elapses, the tip does not change.
    clock["t"] += app._TIP_INTERVAL - 0.1
    app._update_status()
    assert app._status._text == first
    # After the interval, it advances to the next tip.
    clock["t"] += 0.2
    app._update_status()
    assert app._status._text != first
    assert app._status._text in TIPS


def test_explicit_message_interrupts_tips(app, clock):
    app._update_status()  # show a tip
    app._set_status("→ opened X")
    assert app._status._text == "→ opened X"
    assert app._status_text == "→ opened X"


# ── StatusBar reflow / two-line truncation ───────────────────────────────

def _lines(bar: StatusBar) -> tuple[str, str]:
    return bar._line1.get_text()[0], bar._line2.get_text()[0]


def _cols(s: str) -> int:
    import urwid
    return urwid.calc_width(s, 0, len(s))


def test_short_message_stays_on_one_line():
    bar = StatusBar()
    bar.set_message("hello", "info")
    bar._reflow(40)
    l1, l2 = _lines(bar)
    assert l1 == "hello"
    assert l2 == ""


def test_long_message_spills_to_second_line():
    bar = StatusBar()
    bar.set_message("one two three four five six seven eight", "info")
    bar._reflow(20)
    l1, l2 = _lines(bar)
    assert l1 and l2  # both lines used
    assert _cols(l1) <= 20 and _cols(l2) <= 20


def test_long_message_breaks_on_word_boundary():
    bar = StatusBar()
    bar.set_message("one two three four five six seven eight", "info")
    bar._reflow(20)
    l1, l2 = _lines(bar)
    # Words aren't split across the break: line 1 ends on a whole word and
    # line 2 starts on one.
    assert not l1.endswith(" ") and not l2.startswith(" ")
    assert l1.split()[-1] in {"one", "two", "three", "four", "five"}


def test_wide_cjk_message_wraps_by_display_width():
    # Regression: textwrap counts characters, so a CJK line that fits by
    # char-count but overflows by display width used to clip on line 1 instead
    # of wrapping. Each Chinese glyph is two columns.
    bar = StatusBar()
    bar.set_message("→ 会话标题很长很长很长的中文名字测试换行 (1 session)", "info")
    bar._reflow(30)
    l1, l2 = _lines(bar)
    assert _cols(l1) <= 30      # line 1 fits the width...
    assert _cols(l1) >= 20      # ...and isn't wasted on a lone glyph
    assert l2                   # the rest wrapped to line 2


def test_overlong_message_truncated_with_ellipsis():
    bar = StatusBar()
    bar.set_message(" ".join(["word"] * 40), "info")
    bar._reflow(12)
    _, l2 = _lines(bar)
    assert l2.endswith("…")
    assert _cols(l2) <= 12


def test_level_sets_palette_attr():
    bar = StatusBar()
    bar.set_message("boom", "error")
    assert bar._attr.attr_map[None] == _LEVEL_ATTR["error"]
    bar.set_message("note", "info")
    assert bar._attr.attr_map[None] == _LEVEL_ATTR["info"]


def test_tip_shares_info_style():
    # Tips must render with the same attribute as info (same font/colour), not
    # a distinct dim style.
    bar = StatusBar()
    bar.set_message("a tip", "tip")
    assert bar._attr.attr_map[None] == _LEVEL_ATTR["info"]


def test_unknown_level_falls_back_to_info():
    bar = StatusBar()
    bar.set_message("x", "bogus")
    assert bar._level == "info"
