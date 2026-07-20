"""Crash/concurrency safety for server-global F8/F9 forwarding."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.function_key_manager import RootFunctionKeyManager


def _snapshot(*panes: str) -> tmux_ctl.ServerSnapshot:
    return tmux_ctl.ServerSnapshot(
        sessions=frozenset({"railmux"}), panes=frozenset(panes))


def _install_mocks(monkeypatch, tmp_path):
    backup = {"F8": "original-f8", "F9": None}
    monkeypatch.setattr(
        "railmux.function_key_manager.restart_state.runtime_state_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot", lambda: _snapshot("%1", "%2"))
    monkeypatch.setattr(
        tmux_ctl,
        "pane_identity",
        lambda pane: SimpleNamespace(window_id=f"@{pane.removeprefix('%')}")
        if pane in {"%1", "%2"} else None,
    )
    monkeypatch.setattr(
        tmux_ctl, "prepare_root_function_bindings", lambda: backup)
    install = MagicMock(return_value=True)
    restore = MagicMock()
    set_controller = MagicMock(return_value=True)
    unset_controller = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_root_function_forwarding", install)
    monkeypatch.setattr(tmux_ctl, "restore_root_function_bindings", restore)
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: True)
    monkeypatch.setattr(tmux_ctl, "set_window_user_option", set_controller)
    monkeypatch.setattr(
        tmux_ctl, "unset_window_user_option_if_value", unset_controller)
    return backup, install, restore, set_controller, unset_controller


def test_multiple_owners_share_install_and_last_owner_restores(
        monkeypatch, tmp_path):
    backup, install, restore, set_controller, unset_controller = (
        _install_mocks(monkeypatch, tmp_path))
    first = RootFunctionKeyManager("server", "%1")
    second = RootFunctionKeyManager("server", "%2")

    assert first.open()
    assert second.open()
    assert install.call_count == 1
    assert set_controller.call_count == 2
    first.close()
    restore.assert_not_called()
    second.close()

    assert unset_controller.call_count == 2
    restore.assert_called_once()
    assert restore.call_args.args[0] == backup


def test_dead_owner_is_pruned_by_successor(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    crashed = RootFunctionKeyManager("server", "%1")
    assert crashed.open()
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    successor = RootFunctionKeyManager("server", "%2")

    assert successor.open()
    assert install.call_count == 1
    successor.close()

    restore.assert_called_once()


def test_stale_restored_transaction_reinstalls(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    crashed = RootFunctionKeyManager("server", "%1")
    assert crashed.open()
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: False)
    successor = RootFunctionKeyManager("server", "%2")

    assert successor.open()

    restore.assert_called_once()
    assert install.call_count == 2


def test_live_owner_and_user_reload_fails_closed(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    first = RootFunctionKeyManager("server", "%1")
    assert first.open()
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: False)

    assert not RootFunctionKeyManager("server", "%2").open()
    assert install.call_count == 1
    restore.assert_not_called()


def test_busy_coordination_lock_never_blocks_startup(monkeypatch, tmp_path):
    _install_mocks(monkeypatch, tmp_path)
    holder = RootFunctionKeyManager("server", "%1")
    contender = RootFunctionKeyManager("server", "%2")

    with holder._locked():
        assert contender.open() is False


def test_join_save_failure_rolls_back_only_controller_option(
        monkeypatch, tmp_path):
    _backup, install, restore, _set, unset = _install_mocks(
        monkeypatch, tmp_path)
    first = RootFunctionKeyManager("server", "%1")
    assert first.open()
    second = RootFunctionKeyManager("server", "%2")
    monkeypatch.setattr(second, "_save", lambda _state: False)

    assert not second.open()

    assert install.call_count == 1
    restore.assert_not_called()
    unset.assert_called_once_with(
        "%2", tmux_ctl.RAILMUX_CONTROLLER_OPTION, "%2")


def test_same_window_owner_close_hands_controller_back(
        monkeypatch, tmp_path):
    _backup, _install, _restore, set_controller, _unset = _install_mocks(
        monkeypatch, tmp_path)
    monkeypatch.setattr(
        tmux_ctl,
        "pane_identity",
        lambda pane: SimpleNamespace(window_id="@shared"),
    )
    first = RootFunctionKeyManager("server", "%1")
    second = RootFunctionKeyManager("server", "%2")
    assert first.open()
    assert second.open()

    second.close()

    assert set_controller.call_args.args == (
        "@shared", tmux_ctl.RAILMUX_CONTROLLER_OPTION, "%1")
