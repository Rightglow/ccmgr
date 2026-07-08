import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from ccmgr.scroll_manager import ScrollManager


DEFAULT_BACKUP = {
    ("copy-mode", "WheelUpPane"): "up",
    ("copy-mode", "WheelDownPane"): "down",
    ("copy-mode-vi", "WheelUpPane"): "vi-up",
    ("copy-mode-vi", "WheelDownPane"): "vi-down",
}


@contextmanager
def mocked_tmux():
    with patch("ccmgr.scroll_manager.tmux_ctl.session_pane_id",
               return_value="%10") as session_pane_id, \
         patch("ccmgr.scroll_manager.tmux_ctl.pane_alive",
               return_value=True) as pane_alive, \
         patch("ccmgr.scroll_manager.tmux_ctl.start_scroll_agent",
               return_value="%20") as start, \
         patch("ccmgr.scroll_manager.tmux_ctl.wait_window_user_option",
               return_value=True) as wait_ready, \
         patch("ccmgr.scroll_manager.tmux_ctl.prepare_scroll_bindings",
               return_value=DEFAULT_BACKUP) as prepare, \
         patch("ccmgr.scroll_manager.tmux_ctl.rebind_scroll_agent",
               return_value=True) as rebind, \
         patch("ccmgr.scroll_manager.tmux_ctl.set_scroll_agent_target",
               return_value=True) as set_target, \
         patch("ccmgr.scroll_manager.tmux_ctl.set_window_user_option",
               return_value=True) as set_option, \
         patch("ccmgr.scroll_manager.tmux_ctl.session_exists",
               return_value=False), \
         patch("ccmgr.scroll_manager.tmux_ctl.kill_session") as kill, \
         patch("ccmgr.scroll_manager.tmux_ctl.restore_scroll_bindings") as restore:
        owned = patch(
            "ccmgr.scroll_manager.tmux_ctl.scroll_bindings_owned_by",
            return_value=True,
        )
        with owned as bindings_owned:
            yield SimpleNamespace(
                session_pane_id=session_pane_id,
                pane_alive=pane_alive,
                start=start,
                wait_ready=wait_ready,
                prepare=prepare,
                rebind=rebind,
                set_target=set_target,
                set_option=set_option,
                kill=kill,
                restore=restore,
                bindings_owned=bindings_owned,
            )


def test_second_manager_cannot_overwrite_live_owner(tmp_path):
    with patch("ccmgr.scroll_manager.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch.dict(os.environ, {"TMUX": "/tmp/test-socket,1,0"}), \
         mocked_tmux():
        first = ScrollManager(True)
        second = ScrollManager(True)
        assert first.configure("cc-a")
        assert not second.configure("cc-b")
        first.close()


def test_new_manager_adopts_state_after_owner_crash(tmp_path):
    with patch("ccmgr.scroll_manager.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch.dict(os.environ, {"TMUX": "/tmp/test-socket,1,0"}), \
         mocked_tmux() as tmux:
        first = ScrollManager(True)
        assert first.configure("cc-a")
        assert tmux.start.call_count == 1
        assert tmux.prepare.call_count == 1

        # Simulate SIGKILL: flock is released, while the detached agent and
        # JSON recovery record remain untouched.
        assert first._lock_fd is not None
        os.close(first._lock_fd)
        first._lock_fd = None

        second = ScrollManager(True)
        assert second.configure("cc-b")
        assert tmux.start.call_count == 1
        assert tmux.prepare.call_count == 1
        assert tmux.rebind.call_count == 2
        assert second._bindings_backup == DEFAULT_BACKUP
        second.close()


def test_failed_target_message_does_not_mark_window(tmp_path):
    with patch("ccmgr.scroll_manager.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch.dict(os.environ, {"TMUX": "/tmp/test-socket,1,0"}), \
         mocked_tmux() as tmux:
        tmux.set_target.return_value = False
        manager = ScrollManager(True)
        assert not manager.configure("cc-a")
        tmux.set_option.assert_not_called()
        manager.close()


def test_dead_agent_is_recreated_by_maintenance(tmp_path):
    with patch("ccmgr.scroll_manager.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch.dict(os.environ, {"TMUX": "/tmp/test-socket,1,0"}), \
         mocked_tmux() as tmux:
        manager = ScrollManager(True)
        assert manager.configure("cc-a")
        tmux.pane_alive.return_value = False
        manager.maintain()
        assert tmux.start.call_count == 2
        assert tmux.rebind.call_count == 2
        manager.close()


def test_state_write_failure_removes_new_window_marker(tmp_path):
    with patch("ccmgr.scroll_manager.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch.dict(os.environ, {"TMUX": "/tmp/test-socket,1,0"}), \
         mocked_tmux() as tmux:
        manager = ScrollManager(True)
        with patch.object(manager, "_save_state", side_effect=[True, False]):
            assert not manager.configure("cc-a")
        tmux.set_option.assert_not_called()
        manager.close()
