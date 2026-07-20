"""Terminal-native startup and idle pane surfaces."""
from __future__ import annotations

import re
from unittest.mock import MagicMock

from railmux.pane_surface import render_empty_surface, render_startup_surface
from railmux import pane_surface


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain_lines(surface: str) -> list[str]:
    plain = _ANSI.sub("", surface.removeprefix("\x1b[2J\x1b[H"))
    return plain.splitlines()


def test_empty_surface_is_centered_and_explains_the_interaction_pair():
    surface = render_empty_surface(2, 80, 20)
    lines = _plain_lines(surface)

    assert surface.startswith("\x1b[2J\x1b[H")
    assert any("RAILMUX  ·  PANE 2" in line for line in lines)
    assert any("Ready for another agent" in line for line in lines)
    assert any("click / ␣  show" in line for line in lines)
    assert any("↵  open & focus" in line for line in lines)
    assert len(lines) <= 20
    assert min(
        len(line) - len(line.lstrip())
        for line in lines if line.strip()
    ) > 10


def test_startup_surface_uses_the_same_product_language():
    surface = render_startup_surface(60, 12)

    assert "RAILMUX" in surface
    assert "Restoring your workspace" in surface
    assert "Reconnecting sessions and panes…" in surface


def test_empty_surface_exits_instead_of_spinning_on_stdin_eof(monkeypatch):
    stdin = MagicMock()
    stdin.fileno.return_value = 7
    stdout = MagicMock()
    stdout.fileno.return_value = 8
    monkeypatch.setattr(pane_surface.sys, "stdin", stdin)
    monkeypatch.setattr(pane_surface.sys, "stdout", stdout)
    monkeypatch.setattr("termios.tcgetattr", lambda _fd: [0, 0, 0, 0])
    monkeypatch.setattr("termios.tcsetattr", lambda *_args: None)
    monkeypatch.setattr(pane_surface.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(pane_surface.select, "select", lambda *_args: ([7], [], []))
    read = MagicMock(return_value=b"")
    monkeypatch.setattr(pane_surface.os, "read", read)
    monkeypatch.setattr(pane_surface, "_terminal_size", lambda: (80, 24))

    pane_surface._run_empty_surface(2)

    read.assert_called_once_with(7, 4096)
