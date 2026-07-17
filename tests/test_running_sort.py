"""The Running pane re-orders by recency, throttled to once per minute.

``App._maybe_resort_running`` reorders ``self._running`` (the pane renders it in
dict order) so recently-active sessions bubble to the top — but only once per
``_RUNNING_SORT_INTERVAL`` so rows don't jump under the cursor mid-click.
"""
from __future__ import annotations

import time

from railmux.ui.app import App, _Running, _RUNNING_SORT_INTERVAL


def _app(running: dict, sort_ts: float) -> App:
    app = App.__new__(App)
    app._running = running
    app._running_sort_ts = sort_ts
    return app


def _r(key: str, mtime: float = 0.0, created: float = 0.0,
       status: str = "idle") -> _Running:
    return _Running(key=key, tmux_name=f"cc-{key}", label=key,
                    last_mtime=mtime, created_at=created, status=status)


def test_resort_orders_by_recency_desc():
    # Inserted a, b, c but activity order is b (newest), c, a (oldest).
    app = _app({
        "a": _r("a", mtime=100.0),
        "b": _r("b", mtime=300.0),
        "c": _r("c", mtime=200.0),
    }, sort_ts=0.0)  # ts=0 → interval elapsed → sorts now

    app._maybe_resort_running()

    assert list(app._running) == ["b", "c", "a"]
    assert app._running_sort_ts > 0.0  # timestamp advanced


def test_resort_throttled_within_interval():
    # Just sorted (ts=now) → a reorder that WOULD change order is skipped.
    app = _app({
        "a": _r("a", mtime=100.0),
        "b": _r("b", mtime=300.0),
    }, sort_ts=time.time())

    app._maybe_resort_running()

    assert list(app._running) == ["a", "b"]  # untouched


def test_resort_fires_after_interval_elapses():
    app = _app({
        "a": _r("a", mtime=100.0),
        "b": _r("b", mtime=300.0),
    }, sort_ts=time.time() - _RUNNING_SORT_INTERVAL - 1)

    app._maybe_resort_running()

    assert list(app._running) == ["b", "a"]


def test_placeholder_sorts_by_created_at():
    # A placeholder has no JSONL yet (last_mtime=0); it should sort by its
    # launch time, not sink to the bottom.
    app = _app({
        "real": _r("real", mtime=50.0),
        "__new__-1": _r("__new__-1", mtime=0.0, created=999.0),
    }, sort_ts=0.0)

    app._maybe_resort_running()

    assert list(app._running) == ["__new__-1", "real"]


def test_blocked_groups_first_then_recency():
    app = _app({
        "new-idle": _r("new-idle", mtime=400.0),
        "old-blocked": _r("old-blocked", mtime=100.0, status="blocked"),
        "new-blocked": _r("new-blocked", mtime=300.0, status="blocked"),
        "old-idle": _r("old-idle", mtime=200.0),
    }, sort_ts=0.0)

    app._maybe_resort_running()

    assert list(app._running) == [
        "new-blocked", "old-blocked", "new-idle", "old-idle"]


def test_newly_blocked_does_not_jump_inside_throttle_window():
    app = _app({
        "idle": _r("idle", mtime=100.0),
        "blocked": _r("blocked", mtime=50.0, status="blocked"),
    }, sort_ts=time.time())

    app._maybe_resort_running()

    assert list(app._running) == ["idle", "blocked"]
