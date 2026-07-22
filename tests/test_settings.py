"""Mutable Options settings in the shared, style-preserving config.toml."""
from __future__ import annotations

import pytest
import tomlkit

from railmux.settings import LayoutProfile, Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr("railmux.settings._config_path", lambda: path)
    return Settings(), path


def test_defaults_when_no_file(settings):
    store, _path = settings
    assert store.codex_yolo_policy == "ask"
    assert store.layout_profile is None
    assert store.layout_save_policy == "ask"


def test_codex_policy_persists_in_existing_codex_table(settings):
    store, path = settings
    path.write_text(
        "# user comment\n[codex]\n"
        'binary = "/opt/codex" # keep this\n')
    store = Settings()

    assert store.set_codex_yolo_policy("always")
    assert Settings().codex_yolo_policy == "always"
    text = path.read_text()
    assert "# user comment" in text
    assert 'binary = "/opt/codex" # keep this' in text
    assert 'auto_run = "always"' in text


@pytest.mark.parametrize("policy", ["always", "ask", "never"])
def test_codex_yolo_policy_round_trips(settings, policy):
    store, _path = settings

    assert store.set_codex_yolo_policy(policy)
    assert store.codex_yolo_policy == policy
    assert Settings().codex_yolo_policy == policy


def test_malformed_config_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    original = "[codex\nbinary = 'secret'"
    path.write_text(original)
    monkeypatch.setattr("railmux.settings._config_path", lambda: path)
    store = Settings()

    assert store.codex_yolo_policy == "ask"
    assert not store.set_codex_yolo_policy("always")
    assert path.read_text() == original


def test_non_string_policy_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[codex]\nauto_run = true\n")
    monkeypatch.setattr("railmux.settings._config_path", lambda: path)

    store = Settings()

    assert store.codex_yolo_policy == "ask"


def test_codex_choice_writes_one_policy_key(settings):
    store, path = settings

    assert store.set_codex_yolo_policy("always")

    data = tomlkit.parse(path.read_text()).unwrap()
    assert data == {"codex": {"auto_run": "always"}}


def test_write_failure_rolls_back_memory(settings, monkeypatch):
    store, path = settings

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("railmux.settings.atomic_write_text", fail_write)
    assert not store.set_codex_yolo_policy("always")
    assert store.codex_yolo_policy == "ask"
    assert not path.exists()


def test_layout_profile_round_trips_and_one_time_is_consumed(settings):
    store, path = settings
    profile = LayoutProfile("once", "side-by-side", 237, 613)

    assert store.set_layout_save_policy("ask", profile)
    assert store.layout_profile == profile
    assert store.layout_save_policy == "ask"
    assert Settings().layout_profile == profile
    data = tomlkit.parse(path.read_text()).unwrap()
    assert data["ui"]["layout_profile"]["version"] == 1

    assert store.consume_layout_profile(profile)
    assert store.layout_profile is None
    data = tomlkit.parse(path.read_text()).unwrap()
    assert data["ui"] == {"layout_retention": "ask"}


def test_consume_does_not_remove_profile_changed_after_startup(settings):
    store, path = settings
    expected = LayoutProfile("once", "side-by-side", 237, 613)
    replacement = LayoutProfile("once", "stacked", 300, 500)
    assert store.set_layout_save_policy("ask", expected)
    other = Settings()
    assert other.set_layout_save_policy("ask", replacement)

    assert not store.consume_layout_profile(expected)
    assert Settings().layout_profile == replacement


def test_always_layout_profile_sets_always_policy(settings):
    store, _path = settings
    profile = LayoutProfile("always", "stacked", 300, 500)

    assert store.set_layout_save_policy("always", profile)
    assert store.layout_profile == profile
    assert store.layout_save_policy == "always"
    assert not store.consume_layout_profile(profile)


def test_layout_policy_and_profile_update_atomically(settings):
    store, _path = settings
    profile = LayoutProfile("always", "stacked", 300, 500)

    assert store.set_layout_save_policy("always", profile)
    assert store.layout_save_policy == "always"
    assert store.layout_profile == profile
    assert store.set_layout_save_policy("ask")
    assert store.layout_save_policy == "ask"
    assert store.layout_profile is None
    assert store.set_layout_save_policy("never")
    assert store.layout_save_policy == "never"


def test_layout_policy_rejects_incompatible_profile_scope(settings):
    store, _path = settings

    assert not store.set_layout_save_policy(
        "always", LayoutProfile("once", "single", 300))
    assert not store.set_layout_save_policy(
        "never", LayoutProfile("always", "single", 300))
    assert store.layout_save_policy == "ask"


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
    path = tmp_path / "config.toml"
    document = tomlkit.document()
    ui = tomlkit.table()
    ui["layout_profile"] = raw
    document["ui"] = ui
    path.write_text(tomlkit.dumps(document))
    monkeypatch.setattr("railmux.settings._config_path", lambda: path)

    assert Settings().layout_profile is None


def test_layout_write_failure_keeps_previous_profile(settings, monkeypatch):
    store, _path = settings
    previous = LayoutProfile("always", "single", 300)
    assert store.set_layout_save_policy("always", previous)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("railmux.settings.atomic_write_text", fail_write)

    assert not store.set_layout_save_policy(
        "always", LayoutProfile("always", "stacked", 200, 500))
    assert store.layout_profile == previous


def test_write_preserves_valid_manual_edits_made_after_startup(settings):
    _store, path = settings
    path.write_text('[claude]\nbinary = "claude-one"\n')
    store = Settings()
    path.write_text(
        '# changed while running\n[claude]\nbinary = "claude-two"\n')

    assert store.set_codex_yolo_policy("always")

    text = path.read_text()
    assert "# changed while running" in text
    assert 'binary = "claude-two"' in text
    assert 'auto_run = "always"' in text


def test_conflicting_non_table_section_is_preserved_and_update_fails(
    tmp_path, monkeypatch,
):
    path = tmp_path / "config.toml"
    path.write_text('ui = "owned by user"\n')
    monkeypatch.setattr("railmux.settings._config_path", lambda: path)
    store = Settings()

    assert not store.set_layout_save_policy("always")
    assert path.read_text() == 'ui = "owned by user"\n'
