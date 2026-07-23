"""Crash-safe shared ownership of Railmux's server-global tmux bindings."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from railmux import restart_state, tmux_ctl
from railmux.atomic_file import atomic_write_text


_VERSION = 5
_KEYS = ("F8", "F9")
_MAX_STATE_BYTES = 64 * 1024


class SharedTmuxBindingManager:
    """Share Railmux-scoped root/prefix bindings across instances."""

    def __init__(self, server_digest: str, owner_pane_id: str) -> None:
        key = "".join(ch for ch in server_digest if ch.isalnum())[:32]
        self._owner_pane_id = owner_pane_id
        prefix = f"railmux-functions-{os.getuid()}-{key or 'unknown'}"
        self._lock_name = f"{prefix}.lock"
        self._state_name = f"{prefix}.json"
        self._lock_path: Path | None = None
        self._state_path: Path | None = None
        self._registered = False
        self._prefix_tab_managed = False
        self._right_click_managed = False
        self._status_click_managed = False
        self._selection_hook_managed = False
        self._selection_hook_index: int | None = None

    @property
    def target_toggle_available(self) -> bool:
        """Whether this instance owns the prefix-Tab Target toggle."""
        return self._registered and self._prefix_tab_managed

    @property
    def selection_isolation_available(self) -> bool:
        """Whether pane copy-mode changes can drive selection isolation."""
        return self._registered and self._selection_hook_managed

    @property
    def status_navigation_available(self) -> bool:
        """Whether pane ranges in this instance's status bar are clickable."""
        return self._registered and self._status_click_managed

    @contextmanager
    def _locked(self) -> Iterator[None]:
        root = restart_state.runtime_state_dir()
        self._lock_path = root / self._lock_name
        self._state_path = root / self._state_name
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict | None:
        if self._state_path is None:
            return None
        try:
            info = self._state_path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > _MAX_STATE_BYTES):
                return None
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (not isinstance(raw, dict)
                or raw.get("version") not in {1, 2, 3, 4, _VERSION}):
            return None
        token = raw.get("token")
        phase = raw.get("phase")
        owners = raw.get("owners")
        backup = raw.get("backup")
        prefix_backup = raw.get("prefix_tab_backup")
        prefix_managed = raw.get("prefix_tab_managed")
        right_click_backup = raw.get("right_click_backup")
        right_click_managed = raw.get("right_click_managed")
        status_click_backup = raw.get("status_click_backup")
        status_click_managed = raw.get("status_click_managed")
        selection_hook_managed = raw.get("selection_hook_managed")
        selection_hook_index = raw.get("selection_hook_index")
        prefix_valid = (
            isinstance(prefix_backup, dict)
            and set(prefix_backup) == {"Tab"}
            and all(value is None or isinstance(value, str)
                    for value in prefix_backup.values())
        )
        right_click_valid = (
            isinstance(right_click_backup, dict)
            and set(right_click_backup) == {"MouseDown3Pane"}
            and all(value is None or isinstance(value, str)
                    for value in right_click_backup.values())
        )
        status_click_valid = (
            isinstance(status_click_backup, dict)
            and set(status_click_backup) == {"MouseDown1Status"}
            and all(value is None or isinstance(value, str)
                    for value in status_click_backup.values())
        )
        if (not isinstance(token, str) or not token or len(token) > 64
                or phase not in {"installing", "active"}
                or not isinstance(owners, dict) or len(owners) > 1024
                or any(not isinstance(owner, str) or not owner.startswith("%")
                       or not isinstance(window, str) or not window.startswith("@")
                       for owner, window in owners.items())
                or not isinstance(backup, dict) or set(backup) != set(_KEYS)
                or any(value is not None and not isinstance(value, str)
                       for value in backup.values())
                or (raw["version"] >= 2
                    and (not prefix_valid
                         or not isinstance(prefix_managed, bool)))
                or (raw["version"] >= 3
                    and (not right_click_valid
                         or not isinstance(right_click_managed, bool)))
                or (raw["version"] >= 4
                    and (not isinstance(selection_hook_managed, bool)
                         or (selection_hook_managed
                             and (not isinstance(selection_hook_index, int)
                                  or not 9000 <= selection_hook_index < 9100))
                         or (not selection_hook_managed
                             and selection_hook_index is not None)))
                or (raw["version"] >= 5
                    and (not status_click_valid
                         or not isinstance(status_click_managed, bool)))):
            return None
        return raw

    def _save(self, state: dict) -> bool:
        if self._state_path is None:
            return False
        try:
            atomic_write_text(
                self._state_path,
                json.dumps(state, separators=(",", ":"), sort_keys=True),
            )
            os.chmod(self._state_path, 0o600)
            return True
        except OSError:
            return False

    def _remove_state(self) -> None:
        if self._state_path is not None:
            try:
                self._state_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _live_panes() -> frozenset[str] | None:
        snapshot = tmux_ctl.server_snapshot()
        return snapshot.panes if snapshot is not None else None

    @staticmethod
    def _prune_owners(state: dict, live: frozenset[str]) -> dict[str, str]:
        owners: dict[str, str] = state["owners"]
        for pane_id, window_id in owners.items():
            if pane_id not in live:
                tmux_ctl.unset_window_user_option_if_value(
                    window_id,
                    tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                    pane_id,
                )
        return {
            pane_id: window_id
            for pane_id, window_id in sorted(owners.items())
            if pane_id in live
        }

    def _owner_window_id(self) -> str | None:
        identity = tmux_ctl.pane_identity(self._owner_pane_id)
        return identity.window_id if identity is not None else None

    def _set_controller(self) -> bool:
        return tmux_ctl.set_window_user_option(
            self._owner_pane_id,
            tmux_ctl.RAILMUX_CONTROLLER_OPTION,
            self._owner_pane_id,
        )

    def open(self) -> bool:
        if self._registered or not self._owner_pane_id.startswith("%"):
            return self._registered
        try:
            with self._locked():
                live = self._live_panes()
                if live is None or self._owner_pane_id not in live:
                    return False
                tmux_ctl.cleanup_stale_selection_markers(live)
                owner_window_id = self._owner_window_id()
                if owner_window_id is None:
                    return False
                state = self._load()
                if state is not None:
                    upgraded = False
                    if state["version"] == 1:
                        prefix_tab_backup = (
                            tmux_ctl.prepare_prefix_target_binding())
                        # F8/F9 forwarding shipped before prefix Tab joined the
                        # same transaction. Re-enter the crash-safe installing
                        # phase so an existing live/stale v1 lease upgrades in
                        # place instead of leaving its marker permanently
                        # unclaimable after a code update.
                        state["prefix_tab_managed"] = (
                            prefix_tab_backup is not None)
                        state["prefix_tab_backup"] = (
                            prefix_tab_backup or {"Tab": None})
                        upgraded = True
                    if state["version"] < 3:
                        right_click_backup = (
                            tmux_ctl.prepare_root_right_click_binding())
                        # Right-click routing joined the same lease in v3. Its
                        # original must be durable before any root-table write.
                        state["right_click_managed"] = (
                            right_click_backup is not None)
                        state["right_click_backup"] = (
                            right_click_backup
                            or {"MouseDown3Pane": None})
                        upgraded = True
                    if state["version"] < 4:
                        hook_index = tmux_ctl.prepare_selection_mode_hook()
                        state["selection_hook_managed"] = (
                            hook_index is not None)
                        state["selection_hook_index"] = hook_index
                        upgraded = True
                    if state["version"] < 5:
                        status_click_backup = (
                            tmux_ctl.prepare_root_status_click_binding())
                        # Compact pane-ID status ranges joined the shared lease
                        # in v5.
                        # Persist the original before installing the global
                        # MouseDown1Status wrapper.
                        state["status_click_managed"] = (
                            status_click_backup is not None)
                        state["status_click_backup"] = (
                            status_click_backup
                            or {"MouseDown1Status": None})
                        upgraded = True
                    if upgraded:
                        state["version"] = _VERSION
                        state["phase"] = "installing"
                        if not self._save(state):
                            return False
                    token = state["token"]
                    state["owners"] = self._prune_owners(state, live)
                    if state["phase"] == "installing":
                        current = tmux_ctl.read_root_function_bindings()
                        safe = all(
                            tmux_ctl.root_function_binding_is_original_or_owned(
                                key, current.get(key),
                                state["backup"].get(key), token)
                            for key in _KEYS
                        )
                        if state["prefix_tab_managed"]:
                            current_tab = (
                                tmux_ctl.read_prefix_target_binding()["Tab"])
                            safe = (
                                safe
                                and tmux_ctl.prefix_target_binding_is_original_or_owned(
                                    current_tab,
                                    state["prefix_tab_backup"].get("Tab"),
                                    token,
                                )
                            )
                        if state["right_click_managed"]:
                            current_right = (
                                tmux_ctl.read_root_right_click_binding()
                                ["MouseDown3Pane"]
                            )
                            safe = (
                                safe
                                and tmux_ctl.root_right_click_binding_is_original_or_owned(
                                    current_right,
                                    state["right_click_backup"].get(
                                        "MouseDown3Pane"),
                                    token,
                                )
                            )
                        if state["selection_hook_managed"]:
                            safe = (
                                safe
                                and tmux_ctl.selection_mode_hook_is_absent_or_owned(
                                    state["selection_hook_index"], token)
                            )
                        if state["status_click_managed"]:
                            current_status = (
                                tmux_ctl.read_root_status_click_binding()
                                ["MouseDown1Status"]
                            )
                            safe = (
                                safe
                                and tmux_ctl.root_status_click_binding_is_original_or_owned(
                                    current_status,
                                    state["status_click_backup"].get(
                                        "MouseDown1Status"),
                                    token,
                                )
                            )
                        if not safe:
                            if state["owners"]:
                                return False
                            tmux_ctl.restore_root_function_bindings(
                                state["backup"], token=token)
                            if state["prefix_tab_managed"]:
                                tmux_ctl.restore_prefix_target_binding(
                                    state["prefix_tab_backup"], token=token)
                            if state["right_click_managed"]:
                                tmux_ctl.restore_root_right_click_binding(
                                    state["right_click_backup"], token=token)
                            if state["selection_hook_managed"]:
                                tmux_ctl.restore_selection_mode_hook(
                                    state["selection_hook_index"], token)
                            if state["status_click_managed"]:
                                tmux_ctl.restore_root_status_click_binding(
                                    state["status_click_backup"], token=token)
                            self._remove_state()
                            state = None
                        else:
                            if not tmux_ctl.set_root_function_forwarding(
                                    state["backup"], token):
                                return False
                            if (state["prefix_tab_managed"]
                                    and not tmux_ctl.set_prefix_target_binding(
                                        state["prefix_tab_backup"], token)):
                                tmux_ctl.restore_prefix_target_binding(
                                    state["prefix_tab_backup"], token=token)
                                state["prefix_tab_managed"] = False
                                if not self._save(state):
                                    return False
                            if (state["right_click_managed"]
                                    and not tmux_ctl.set_root_right_click_forwarding(
                                        state["right_click_backup"], token)):
                                tmux_ctl.restore_root_right_click_binding(
                                    state["right_click_backup"], token=token)
                                state["right_click_managed"] = False
                                if not self._save(state):
                                    return False
                            if (state["selection_hook_managed"]
                                    and not tmux_ctl.set_selection_mode_hook(
                                        state["selection_hook_index"], token)):
                                tmux_ctl.restore_selection_mode_hook(
                                    state["selection_hook_index"], token)
                                state["selection_hook_managed"] = False
                                state["selection_hook_index"] = None
                                if not self._save(state):
                                    return False
                            if (state["status_click_managed"]
                                    and not tmux_ctl.set_root_status_click_forwarding(
                                        state["status_click_backup"], token)):
                                tmux_ctl.restore_root_status_click_binding(
                                    state["status_click_backup"], token=token)
                                state["status_click_managed"] = False
                                if not self._save(state):
                                    return False
                            state["phase"] = "active"
                    elif not (
                        tmux_ctl.root_function_bindings_owned_by(token)
                        and (
                            not state["prefix_tab_managed"]
                            or tmux_ctl.prefix_target_binding_owned_by(token)
                        )
                        and (
                            not state["right_click_managed"]
                            or tmux_ctl.root_right_click_binding_owned_by(token)
                        )
                        and (
                            not state["selection_hook_managed"]
                            or tmux_ctl.selection_mode_hook_owned_by(
                                state["selection_hook_index"], token)
                        )
                        and (
                            not state["status_click_managed"]
                            or tmux_ctl.root_status_click_binding_owned_by(
                                token)
                        )
                    ):
                        if state["owners"]:
                            return False
                        tmux_ctl.restore_root_function_bindings(
                            state["backup"], token=token)
                        if state["prefix_tab_managed"]:
                            tmux_ctl.restore_prefix_target_binding(
                                state["prefix_tab_backup"], token=token)
                        if state["right_click_managed"]:
                            tmux_ctl.restore_root_right_click_binding(
                                state["right_click_backup"], token=token)
                        if state["selection_hook_managed"]:
                            tmux_ctl.restore_selection_mode_hook(
                                state["selection_hook_index"], token)
                        if state["status_click_managed"]:
                            tmux_ctl.restore_root_status_click_binding(
                                state["status_click_backup"], token=token)
                        self._remove_state()
                        state = None
                    if state is not None:
                        previous_owners = dict(state["owners"])
                        state["owners"][self._owner_pane_id] = owner_window_id
                        if not self._set_controller():
                            return False
                        if not self._save(state):
                            tmux_ctl.unset_window_user_option_if_value(
                                self._owner_pane_id,
                                tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                                self._owner_pane_id,
                            )
                            for pane_id, window_id in previous_owners.items():
                                if window_id == owner_window_id:
                                    tmux_ctl.set_window_user_option(
                                        window_id,
                                        tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                                        pane_id,
                                    )
                                    break
                            return False
                        self._registered = True
                        self._prefix_tab_managed = state["prefix_tab_managed"]
                        self._right_click_managed = (
                            state["right_click_managed"])
                        self._selection_hook_managed = (
                            state["selection_hook_managed"])
                        self._selection_hook_index = (
                            state["selection_hook_index"])
                        self._status_click_managed = (
                            state["status_click_managed"])
                        return True

                backup = tmux_ctl.prepare_root_function_bindings()
                prefix_tab_backup = tmux_ctl.prepare_prefix_target_binding()
                right_click_backup = (
                    tmux_ctl.prepare_root_right_click_binding())
                selection_hook_index = tmux_ctl.prepare_selection_mode_hook()
                status_click_backup = (
                    tmux_ctl.prepare_root_status_click_binding())
                if backup is None:
                    return False
                token = secrets.token_hex(8)
                state = {
                    "version": _VERSION,
                    "phase": "installing",
                    "token": token,
                    "owners": {self._owner_pane_id: owner_window_id},
                    "backup": backup,
                    "prefix_tab_managed": prefix_tab_backup is not None,
                    "prefix_tab_backup": prefix_tab_backup or {"Tab": None},
                    "right_click_managed": right_click_backup is not None,
                    "right_click_backup": (
                        right_click_backup or {"MouseDown3Pane": None}),
                    "selection_hook_managed": selection_hook_index is not None,
                    "selection_hook_index": selection_hook_index,
                    "status_click_managed": status_click_backup is not None,
                    "status_click_backup": (
                        status_click_backup or {"MouseDown1Status": None}),
                }
                if not self._save(state):
                    return False
                if not tmux_ctl.set_root_function_forwarding(backup, token):
                    tmux_ctl.restore_root_function_bindings(
                        backup, token=token)
                    self._remove_state()
                    return False
                if (state["prefix_tab_managed"]
                        and not tmux_ctl.set_prefix_target_binding(
                            state["prefix_tab_backup"], token)):
                    tmux_ctl.restore_prefix_target_binding(
                        state["prefix_tab_backup"], token=token)
                    state["prefix_tab_managed"] = False
                    if not self._save(state):
                        tmux_ctl.restore_root_function_bindings(
                            backup, token=token)
                        self._remove_state()
                        return False
                if (state["right_click_managed"]
                        and not tmux_ctl.set_root_right_click_forwarding(
                            state["right_click_backup"], token)):
                    tmux_ctl.restore_root_right_click_binding(
                        state["right_click_backup"], token=token)
                    state["right_click_managed"] = False
                    if not self._save(state):
                        tmux_ctl.restore_root_function_bindings(
                            backup, token=token)
                        if state["prefix_tab_managed"]:
                            tmux_ctl.restore_prefix_target_binding(
                                state["prefix_tab_backup"], token=token)
                        self._remove_state()
                        return False
                if (state["selection_hook_managed"]
                        and not tmux_ctl.set_selection_mode_hook(
                            state["selection_hook_index"], token)):
                    tmux_ctl.restore_selection_mode_hook(
                        state["selection_hook_index"], token)
                    state["selection_hook_managed"] = False
                    state["selection_hook_index"] = None
                    if not self._save(state):
                        tmux_ctl.restore_root_function_bindings(
                            backup, token=token)
                        if state["prefix_tab_managed"]:
                            tmux_ctl.restore_prefix_target_binding(
                                state["prefix_tab_backup"], token=token)
                        if state["right_click_managed"]:
                            tmux_ctl.restore_root_right_click_binding(
                                state["right_click_backup"], token=token)
                        self._remove_state()
                        return False
                if (state["status_click_managed"]
                        and not tmux_ctl.set_root_status_click_forwarding(
                            state["status_click_backup"], token)):
                    tmux_ctl.restore_root_status_click_binding(
                        state["status_click_backup"], token=token)
                    state["status_click_managed"] = False
                    if not self._save(state):
                        tmux_ctl.restore_root_function_bindings(
                            backup, token=token)
                        if state["prefix_tab_managed"]:
                            tmux_ctl.restore_prefix_target_binding(
                                state["prefix_tab_backup"], token=token)
                        if state["right_click_managed"]:
                            tmux_ctl.restore_root_right_click_binding(
                                state["right_click_backup"], token=token)
                        if state["selection_hook_managed"]:
                            tmux_ctl.restore_selection_mode_hook(
                                state["selection_hook_index"], token)
                        self._remove_state()
                        return False
                state["phase"] = "active"
                if not self._save(state) or not self._set_controller():
                    tmux_ctl.restore_root_function_bindings(
                        backup, token=token)
                    if state["prefix_tab_managed"]:
                        tmux_ctl.restore_prefix_target_binding(
                            state["prefix_tab_backup"], token=token)
                    if state["right_click_managed"]:
                        tmux_ctl.restore_root_right_click_binding(
                            state["right_click_backup"], token=token)
                    if state["selection_hook_managed"]:
                        tmux_ctl.restore_selection_mode_hook(
                            state["selection_hook_index"], token)
                    if state["status_click_managed"]:
                        tmux_ctl.restore_root_status_click_binding(
                            state["status_click_backup"], token=token)
                    self._remove_state()
                    return False
                self._registered = True
                self._prefix_tab_managed = state["prefix_tab_managed"]
                self._right_click_managed = state["right_click_managed"]
                self._selection_hook_managed = state["selection_hook_managed"]
                self._selection_hook_index = state["selection_hook_index"]
                self._status_click_managed = state["status_click_managed"]
                return True
        except OSError:
            return False

    def close(self) -> None:
        if not self._registered:
            return
        option_released = False
        try:
            try:
                with self._locked():
                    state = self._load()
                    if state is None:
                        return
                    live = self._live_panes()
                    if live is None:
                        return
                    owners = self._prune_owners(state, live)
                    owner_window_id = owners.pop(
                        self._owner_pane_id, None)
                    if owner_window_id is not None:
                        tmux_ctl.unset_window_user_option_if_value(
                            owner_window_id,
                            tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                            self._owner_pane_id,
                        )
                        option_released = True
                        for pane_id, window_id in owners.items():
                            if window_id == owner_window_id:
                                tmux_ctl.set_window_user_option(
                                    window_id,
                                    tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                                    pane_id,
                                )
                                break
                    state["owners"] = owners
                    if owners:
                        self._save(state)
                        return
                    tmux_ctl.restore_root_function_bindings(
                        state["backup"], token=state["token"])
                    if state["prefix_tab_managed"]:
                        tmux_ctl.restore_prefix_target_binding(
                            state["prefix_tab_backup"], token=state["token"])
                    if state["right_click_managed"]:
                        tmux_ctl.restore_root_right_click_binding(
                            state["right_click_backup"], token=state["token"])
                    if state["selection_hook_managed"]:
                        tmux_ctl.restore_selection_mode_hook(
                            state["selection_hook_index"], state["token"])
                    if state["status_click_managed"]:
                        tmux_ctl.restore_root_status_click_binding(
                            state["status_click_backup"], token=state["token"])
                    self._remove_state()
            except OSError:
                pass
        finally:
            if not option_released:
                tmux_ctl.unset_window_user_option_if_value(
                    self._owner_pane_id,
                    tmux_ctl.RAILMUX_CONTROLLER_OPTION,
                    self._owner_pane_id,
                )
            self._registered = False
            self._prefix_tab_managed = False
            self._right_click_managed = False
            self._status_click_managed = False
            self._selection_hook_managed = False
            self._selection_hook_index = None
