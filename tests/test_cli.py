from unittest.mock import patch

from ccmgr.cli import is_ssh_session, main


def test_no_scroll_coalescing_flag_reaches_app(tmp_path):
    with patch("ccmgr.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--no-scroll-coalescing",
        ])

    assert result == 0
    app_cls.assert_called_once()
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False
    app_cls.return_value.run.assert_called_once()


def test_scroll_coalescing_is_enabled_automatically_over_ssh(tmp_path):
    with patch.dict("os.environ", {"SSH_CONNECTION": "client 1 server 2"}), \
         patch("ccmgr.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_scroll_coalescing_is_disabled_automatically_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("ccmgr.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is False


def test_force_enable_scroll_coalescing_locally(tmp_path):
    clean_env = {
        "SSH_CONNECTION": "",
        "SSH_CLIENT": "",
        "SSH_TTY": "",
    }
    with patch.dict("os.environ", clean_env), patch("ccmgr.ui.app.App") as app_cls:
        result = main([
            "--inside-tmux",
            "--claude-home", str(tmp_path),
            "--scroll-coalescing",
        ])

    assert result == 0
    assert app_cls.call_args.kwargs["scroll_coalescing"] is True


def test_is_ssh_session_recognizes_common_markers():
    assert is_ssh_session({"SSH_CONNECTION": "client 1 server 2"})
    assert is_ssh_session({"SSH_CLIENT": "client 1 2"})
    assert is_ssh_session({"SSH_TTY": "/dev/pts/1"})
    assert not is_ssh_session({})
