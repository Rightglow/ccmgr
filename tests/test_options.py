"""Options screen integration with app-mutable settings."""

from __future__ import annotations

from unittest.mock import MagicMock

from railmux.settings import LayoutProfile, Settings
from railmux.ui.app import App
from railmux.ui.modals import OptionsModal


def _app(tmp_path, monkeypatch) -> App:
    monkeypatch.setattr(
        "railmux.settings._config_path", lambda: tmp_path / "config.toml")
    app = App.__new__(App)
    app._settings = Settings()
    app._layout_profile = None
    app._codex_yolo_runtime = False
    app._codex_yolo_prompt_handled = False
    app._capture_layout_profile = MagicMock(
        return_value=LayoutProfile("always", "single", 300))
    app._set_status = MagicMock()
    app._open_full_sidebar_modal = MagicMock()
    return app


def test_options_layout_choices_persist_and_update_live_preference(
    tmp_path, monkeypatch,
):
    app = _app(tmp_path, monkeypatch)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    assert isinstance(modal, OptionsModal)

    modal._option_rows["layout"][0].keypress((60,), "enter")
    assert app._settings.layout_save_policy == "always"
    assert app._settings.layout_profile == LayoutProfile(
        "always", "single", 300)
    assert app._layout_profile == app._settings.layout_profile

    modal._option_rows["layout"][2].keypress((60,), "enter")
    assert app._settings.layout_save_policy == "never"
    assert app._settings.layout_profile is None
    assert app._layout_profile is None


def test_options_yolo_policy_changes_only_future_launches(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._codex_yolo_runtime = True

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["yolo"][0].keypress((60,), "enter")

    assert app._settings.codex_yolo_policy == "always"
    assert app._codex_yolo_enabled() is True
    assert app._codex_yolo_runtime is False

    modal._option_rows["yolo"][1].keypress((60,), "enter")
    assert app._settings.codex_yolo_policy == "ask"
    assert app._codex_yolo_enabled() is False
    assert app._codex_yolo_prompt_handled is False


def test_options_failed_write_keeps_modal_and_app_state(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app._settings.set_layout_save_policy = MagicMock(return_value=False)

    app._open_options_modal()
    modal = app._open_full_sidebar_modal.call_args.args[0]
    modal._option_rows["layout"][0].keypress((60,), "enter")

    assert modal._policies["layout"] == "ask"
    assert app._layout_profile is None
    assert app._set_status.call_args.args[1] == "error"
