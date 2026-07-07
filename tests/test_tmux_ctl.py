"""Tests for ccmgr.tmux_ctl — CLI wrappers (mocked, no real tmux needed)."""

import subprocess
from unittest.mock import patch, MagicMock

from ccmgr.tmux_ctl import enable_clipboard_passthrough


def _mock_check_output(stdout: str):
    return patch("subprocess.check_output", return_value=stdout.encode())


def _mock_check_call():
    return patch("subprocess.check_call")


def test_clipboard_adds_override_when_missing():
    with _mock_check_output("") as out, _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should have appended the Ms override
        call.assert_called_once()
        args = call.call_args[0][0]
        assert args[0] == "tmux"
        assert "set-option" in args
        assert "-ga" in args
        assert "Ms=" in str(args)


def test_clipboard_idempotent_when_already_set():
    with _mock_check_output("stuff,Ms=something,more") as out, \
         _mock_check_call() as call:
        enable_clipboard_passthrough()
        # Should NOT have called check_call again
        call.assert_not_called()


def test_clipboard_handles_tmux_not_found():
    with _mock_check_output("") as out, \
         patch("subprocess.check_call", side_effect=FileNotFoundError):
        # Should not raise
        enable_clipboard_passthrough()


def test_clipboard_handles_check_output_error():
    with patch("subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "tmux")), \
         _mock_check_call() as call:
        enable_clipboard_passthrough()
        call.assert_called_once()
