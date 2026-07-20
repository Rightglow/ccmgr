"""Crash-safe shared ownership of tmux root-table F8/F9 bindings."""
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


_VERSION = 1
_KEYS = ("F8", "F9")
_MAX_STATE_BYTES = 64 * 1024


class RootFunctionKeyManager:
    """Share one marker-owned F8/F9 wrapper across live Railmux panes."""

    def __init__(self, server_digest: str, owner_pane_id: str) -> None:
        key = "".join(ch for ch in server_digest if ch.isalnum())[:32]
        self._owner_pane_id = owner_pane_id
        prefix = f"railmux-functions-{os.getuid()}-{key or 'unknown'}"
        self._lock_name = f"{prefix}.lock"
        self._state_name = f"{prefix}.json"
        self._lock_path: Path | None = None
        self._state_path: Path | None = None
        self._registered = False

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
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return None
        token = raw.get("token")
        phase = raw.get("phase")
        owners = raw.get("owners")
        backup = raw.get("backup")
        if (not isinstance(token, str) or not token or len(token) > 64
                or phase not in {"installing", "active"}
                or not isinstance(owners, dict) or len(owners) > 1024
                or any(not isinstance(owner, str) or not owner.startswith("%")
                       or not isinstance(window, str) or not window.startswith("@")
                       for owner, window in owners.items())
                or not isinstance(backup, dict) or set(backup) != set(_KEYS)
                or any(value is not None and not isinstance(value, str)
                       for value in backup.values())):
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
                owner_window_id = self._owner_window_id()
                if owner_window_id is None:
                    return False
                state = self._load()
                if state is not None:
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
                        if not safe:
                            if state["owners"]:
                                return False
                            tmux_ctl.restore_root_function_bindings(
                                state["backup"], token=token)
                            self._remove_state()
                            state = None
                        else:
                            if not tmux_ctl.set_root_function_forwarding(
                                    state["backup"], token):
                                return False
                            state["phase"] = "active"
                    elif not tmux_ctl.root_function_bindings_owned_by(token):
                        if state["owners"]:
                            return False
                        tmux_ctl.restore_root_function_bindings(
                            state["backup"], token=token)
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
                        return True

                backup = tmux_ctl.prepare_root_function_bindings()
                if backup is None:
                    return False
                token = secrets.token_hex(8)
                state = {
                    "version": _VERSION,
                    "phase": "installing",
                    "token": token,
                    "owners": {self._owner_pane_id: owner_window_id},
                    "backup": backup,
                }
                if not self._save(state):
                    return False
                if not tmux_ctl.set_root_function_forwarding(backup, token):
                    tmux_ctl.restore_root_function_bindings(
                        backup, token=token)
                    self._remove_state()
                    return False
                state["phase"] = "active"
                if not self._save(state) or not self._set_controller():
                    tmux_ctl.restore_root_function_bindings(
                        backup, token=token)
                    self._remove_state()
                    return False
                self._registered = True
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
