"""Crash/concurrency safety for shared Railmux tmux bindings."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from railmux import tmux_ctl
from railmux.tmux_binding_manager import SharedTmuxBindingManager


def _snapshot(*panes: str) -> tmux_ctl.ServerSnapshot:
    return tmux_ctl.ServerSnapshot(
        sessions=frozenset({"railmux"}), panes=frozenset(panes))


def _install_mocks(monkeypatch, tmp_path):
    backup = {"F8": "original-f8", "F9": None}
    prefix_backup = {"Tab": None}
    right_click_backup = {
        "MouseDown3Pane": "bind-key -T root MouseDown3Pane display-menu",
    }
    monkeypatch.setattr(
        "railmux.tmux_binding_manager.restart_state.runtime_state_dir",
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
    monkeypatch.setattr(
        tmux_ctl, "prepare_prefix_target_binding", lambda: prefix_backup)
    monkeypatch.setattr(
        tmux_ctl, "prepare_root_right_click_binding",
        lambda: right_click_backup,
    )
    monkeypatch.setattr(
        tmux_ctl, "read_root_function_bindings", lambda: backup)
    monkeypatch.setattr(
        tmux_ctl, "read_prefix_target_binding", lambda: prefix_backup)
    monkeypatch.setattr(
        tmux_ctl, "read_root_right_click_binding",
        lambda: right_click_backup,
    )
    install = MagicMock(return_value=True)
    restore = MagicMock()
    install.prefix = MagicMock(return_value=True)
    install.right_click = MagicMock(return_value=True)
    restore.prefix = MagicMock()
    restore.right_click = MagicMock()
    set_controller = MagicMock(return_value=True)
    unset_controller = MagicMock(return_value=True)
    monkeypatch.setattr(tmux_ctl, "set_root_function_forwarding", install)
    monkeypatch.setattr(tmux_ctl, "restore_root_function_bindings", restore)
    monkeypatch.setattr(
        tmux_ctl, "set_prefix_target_binding", install.prefix)
    monkeypatch.setattr(
        tmux_ctl, "restore_prefix_target_binding", restore.prefix)
    monkeypatch.setattr(
        tmux_ctl, "set_root_right_click_forwarding", install.right_click)
    monkeypatch.setattr(
        tmux_ctl, "restore_root_right_click_binding", restore.right_click)
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: True)
    monkeypatch.setattr(
        tmux_ctl, "prefix_target_binding_owned_by", lambda _token: True)
    monkeypatch.setattr(
        tmux_ctl, "root_right_click_binding_owned_by", lambda _token: True)
    monkeypatch.setattr(tmux_ctl, "set_window_user_option", set_controller)
    monkeypatch.setattr(
        tmux_ctl, "unset_window_user_option_if_value", unset_controller)
    return backup, install, restore, set_controller, unset_controller


def test_multiple_owners_share_install_and_last_owner_restores(
        monkeypatch, tmp_path):
    backup, install, restore, set_controller, unset_controller = (
        _install_mocks(monkeypatch, tmp_path))
    first = SharedTmuxBindingManager("server", "%1")
    second = SharedTmuxBindingManager("server", "%2")

    assert first.open()
    assert second.open()
    assert install.call_count == 1
    assert install.prefix.call_count == 1
    assert install.right_click.call_count == 1
    assert set_controller.call_count == 2
    first.close()
    restore.assert_not_called()
    second.close()

    assert unset_controller.call_count == 2
    restore.assert_called_once()
    restore.prefix.assert_called_once()
    restore.right_click.assert_called_once()
    assert restore.call_args.args[0] == backup


def test_dead_owner_is_pruned_by_successor(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    crashed = SharedTmuxBindingManager("server", "%1")
    assert crashed.open()
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    successor = SharedTmuxBindingManager("server", "%2")

    assert successor.open()
    assert install.call_count == 1
    successor.close()

    restore.assert_called_once()


def test_v1_function_lease_upgrades_in_place_with_new_bindings(
        monkeypatch, tmp_path):
    _backup, install, _restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    first = SharedTmuxBindingManager("server", "%1")
    assert first.open()
    state_path = first._state_path
    assert state_path is not None
    state = json.loads(state_path.read_text())
    state["version"] = 1
    del state["prefix_tab_backup"]
    del state["prefix_tab_managed"]
    del state["right_click_backup"]
    del state["right_click_managed"]
    state_path.write_text(json.dumps(state))
    prefix_calls = install.prefix.call_count

    def install_after_backup_is_durable(_backup, _token):
        persisted = json.loads(state_path.read_text())
        assert persisted["version"] == 3
        assert persisted["phase"] == "installing"
        assert persisted["prefix_tab_backup"] == {"Tab": None}
        assert persisted["right_click_backup"]["MouseDown3Pane"]
        return True

    install.prefix.side_effect = install_after_backup_is_durable

    second = SharedTmuxBindingManager("server", "%2")

    assert second.open()
    upgraded = json.loads(state_path.read_text())
    assert upgraded["version"] == 3
    assert upgraded["prefix_tab_backup"] == {"Tab": None}
    assert upgraded["prefix_tab_managed"] is True
    assert install.prefix.call_count == prefix_calls + 1


def test_prefix_failure_keeps_function_keys_active(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    install.prefix.return_value = False
    manager = SharedTmuxBindingManager("server", "%1")

    assert manager.open()
    assert manager.target_toggle_available is False
    assert install.call_count == 1
    restore.assert_not_called()
    state = json.loads(manager._state_path.read_text())
    assert state["prefix_tab_managed"] is False

    manager.close()
    restore.assert_called_once()


def test_stale_restored_transaction_reinstalls(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    crashed = SharedTmuxBindingManager("server", "%1")
    assert crashed.open()
    monkeypatch.setattr(
        tmux_ctl, "server_snapshot", lambda: _snapshot("%2"))
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: False)
    successor = SharedTmuxBindingManager("server", "%2")

    assert successor.open()

    restore.assert_called_once()
    assert install.call_count == 2


def test_live_owner_and_user_reload_fails_closed(monkeypatch, tmp_path):
    _backup, install, restore, _set, _unset = _install_mocks(
        monkeypatch, tmp_path)
    first = SharedTmuxBindingManager("server", "%1")
    assert first.open()
    monkeypatch.setattr(
        tmux_ctl, "root_function_bindings_owned_by", lambda _token: False)

    assert not SharedTmuxBindingManager("server", "%2").open()
    assert install.call_count == 1
    restore.assert_not_called()


def test_busy_coordination_lock_never_blocks_startup(monkeypatch, tmp_path):
    _install_mocks(monkeypatch, tmp_path)
    holder = SharedTmuxBindingManager("server", "%1")
    contender = SharedTmuxBindingManager("server", "%2")

    with holder._locked():
        assert contender.open() is False


def test_join_save_failure_rolls_back_only_controller_option(
        monkeypatch, tmp_path):
    _backup, install, restore, _set, unset = _install_mocks(
        monkeypatch, tmp_path)
    first = SharedTmuxBindingManager("server", "%1")
    assert first.open()
    second = SharedTmuxBindingManager("server", "%2")
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
    first = SharedTmuxBindingManager("server", "%1")
    second = SharedTmuxBindingManager("server", "%2")
    assert first.open()
    assert second.open()

    second.close()

    assert set_controller.call_args.args == (
        "@shared", tmux_ctl.RAILMUX_CONTROLLER_OPTION, "%1")
