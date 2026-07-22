"""Tests for the first-time Codex auto-run (yolo) prompt in App."""
from __future__ import annotations

from unittest.mock import MagicMock

from railmux.settings import Settings
from railmux.ui.app import App
from railmux.ui.modals import YoloConfirmModal


def _app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "railmux.settings._config_path", lambda: tmp_path / "config.toml")
    app = App.__new__(App)
    app._loop = object()          # non-None → UI is up
    app._settings = Settings()
    app._show_overlay = MagicMock()
    app._close_modal = MagicMock()
    app._set_status = MagicMock()
    app._codex_yolo_runtime = False
    app._codex_yolo_prompt_handled = False
    return app


def test_prompt_shown_first_time(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._maybe_prompt_codex_yolo()
    app._show_overlay.assert_called_once()
    assert isinstance(app._show_overlay.call_args[0][0], YoloConfirmModal)


def test_always_enables_yolo_and_marks_prompted(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._maybe_prompt_codex_yolo()
    app._show_overlay.call_args[0][0]._on_always()
    assert app._settings.codex_yolo_policy == "always"
    # Persisted: a fresh store sees it too.
    assert Settings().codex_yolo_policy == "always"


def test_no_keeps_yolo_off_for_run_and_preserves_ask_policy(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)
    app._maybe_prompt_codex_yolo()
    app._show_overlay.call_args[0][0]._on_no()
    assert app._settings.codex_yolo_policy == "ask"
    assert app._codex_yolo_prompt_handled is True


def test_not_shown_again_once_prompted(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._settings.set_codex_yolo_policy("never")
    app._maybe_prompt_codex_yolo()
    app._show_overlay.assert_not_called()


def test_no_prompt_without_loop(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._loop = None
    app._maybe_prompt_codex_yolo()
    app._show_overlay.assert_not_called()


def test_this_time_enables_only_current_app(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._maybe_prompt_codex_yolo()
    app._show_overlay.call_args[0][0]._on_this_time()

    assert app._codex_yolo_enabled() is True
    assert app._settings.codex_yolo_policy == "ask"
    app._show_overlay.reset_mock()
    app._maybe_prompt_codex_yolo()
    app._show_overlay.assert_not_called()


def test_enter_keeps_yolo_off():
    always = MagicMock()
    this_time = MagicMock()
    no = MagicMock()
    modal = YoloConfirmModal(always, this_time, no)
    assert modal.keypress((80,), "enter") is None
    always.assert_not_called()
    this_time.assert_not_called()
    no.assert_called_once_with()


def test_failed_persistence_does_not_enable_yolo(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("railmux.settings.atomic_write_text", fail_write)
    app._maybe_prompt_codex_yolo()
    modal = app._show_overlay.call_args[0][0]
    modal._on_always()
    assert app._settings.codex_yolo_policy == "ask"
    app._close_modal.assert_called_once_with()
    app._set_status.assert_called_once_with(
        "Could not save Codex auto-run choice; settings unchanged.",
        "error",
    )
