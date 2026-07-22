"""Tests for railmux.settings — the app-mutable JSON settings store."""
from __future__ import annotations

import json

import pytest

from railmux.settings import LayoutProfile, Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("railmux.settings._settings_path", lambda: path)
    return Settings(), path


def test_defaults_when_no_file(settings):
    store, _ = settings
    assert store.codex_yolo is False
    assert store.codex_yolo_prompted is False
    assert store.layout_profile is None


def test_set_codex_yolo_persists(settings):
    store, path = settings
    store.set_codex_yolo(True)
    assert store.codex_yolo is True
    assert json.loads(path.read_text())["codex_yolo"] is True
    assert Settings().codex_yolo is True  # a fresh store reads it back


def test_mark_prompted_persists(settings):
    store, _ = settings
    store.mark_codex_yolo_prompted()
    assert store.codex_yolo_prompted is True
    assert Settings().codex_yolo_prompted is True


def test_load_tolerates_malformed_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    monkeypatch.setattr("railmux.settings._settings_path", lambda: path)
    store = Settings()  # must not raise
    assert store.codex_yolo is False


def test_non_boolean_json_values_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"codex_yolo": "false", "codex_yolo_prompted": 1}))
    monkeypatch.setattr("railmux.settings._settings_path", lambda: path)
    store = Settings()
    assert store.codex_yolo is False
    assert store.codex_yolo_prompted is False


def test_record_choice_persists_both_flags_atomically(settings):
    store, path = settings
    assert store.record_codex_yolo_choice(True) is True
    assert json.loads(path.read_text()) == {
        "codex_yolo": True,
        "codex_yolo_prompted": True,
    }


def test_write_failure_rolls_back_memory(settings, monkeypatch):
    store, path = settings

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("railmux.settings.atomic_write_text", fail_write)
    assert store.record_codex_yolo_choice(True) is False
    assert store.codex_yolo is False
    assert store.codex_yolo_prompted is False
    assert not path.exists()


def test_layout_profile_round_trips_and_one_time_is_consumed(settings):
    store, path = settings
    profile = LayoutProfile("once", "side-by-side", 237, 613)

    assert store.save_layout_profile(profile) is True
    assert store.layout_profile == profile
    assert Settings().layout_profile == profile
    assert json.loads(path.read_text())["layout_profile"]["version"] == 1
    assert store.consume_layout_profile(profile) is True
    assert store.layout_profile is None


def test_always_layout_profile_cannot_be_consumed(settings):
    store, _path = settings
    profile = LayoutProfile("always", "stacked", 300, 500)

    assert store.save_layout_profile(profile) is True
    assert store.consume_layout_profile(profile) is False
    assert store.layout_profile == profile


@pytest.mark.parametrize(
    "raw",
    [
        {"version": 2, "scope": "always", "layout": "single",
         "sidebar_permille": 300},
        {"version": 1, "scope": "forever", "layout": "single",
         "sidebar_permille": 300},
        {"version": 1, "scope": "always", "layout": "grid",
         "sidebar_permille": 300},
        {"version": 1, "scope": "always", "layout": "single",
         "sidebar_permille": True},
        {"version": 1, "scope": "always", "layout": "single",
         "sidebar_permille": 300, "primary_permille": 500},
        {"version": 1, "scope": "always", "layout": "single",
         "sidebar_permille": 300, "unknown": "field"},
    ],
)
def test_invalid_layout_profiles_fail_closed(tmp_path, monkeypatch, raw):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"layout_profile": raw}))
    monkeypatch.setattr("railmux.settings._settings_path", lambda: path)

    assert Settings().layout_profile is None


def test_layout_write_failure_keeps_previous_profile(settings, monkeypatch):
    store, _path = settings
    previous = LayoutProfile("always", "single", 300)
    assert store.save_layout_profile(previous)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("railmux.settings.atomic_write_text", fail_write)

    assert not store.save_layout_profile(
        LayoutProfile("always", "stacked", 200, 500))
    assert store.layout_profile == previous
