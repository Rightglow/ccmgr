import json
from io import StringIO
from types import SimpleNamespace

from railmux.diagnostics import (
    TmuxServerDiagnostic,
    _tool_diagnostic,
    run_doctor,
)
from railmux.tmux_health import TmuxIncident


def test_version_preserves_tmux_letter_suffix(monkeypatch):
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="tmux 3.2a", stderr="", returncode=0),
    )

    diagnostic = _tool_diagnostic("tmux", "-V")
    assert diagnostic.status == "available"
    assert diagnostic.version == "3.2a"


def test_doctor_report_is_useful_and_redacts_user_values(
    monkeypatch, tmp_path,
):
    home = tmp_path / "private-user"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    secret_root = tmp_path / "company-secret-project"
    secret_binary = secret_root / "sk-secret-token" / "claude"
    config_dir.joinpath("config.toml").write_text(
        "[claude]\n"
        f"binary = '{secret_binary}'\n"
        "[codex]\n"
        "binary = 'private-codex-wrapper'\n"
        f"home = '{secret_root}'\n"
    )
    claude_home = secret_root / "claude-data"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: str(binary))
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "tool 9.8.7 /home/private-user "
                "12345678-1234-1234-1234-123456789abc sk-secret-token"
            ),
            stderr="private-host company-secret-project",
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("healthy", context="outside"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident",
        lambda: TmuxIncident(100, "remote-display",
                             "remote-display-watchdog-timeout", 3),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.incident_age",
        lambda _recorded: "2 minutes ago",
    )
    output = StringIO()

    assert run_doctor(
        claude_home=claude_home,
        stdout=output,
        environ={
            "TMUX": "/private/socket,123,0",
            "SSH_CONNECTION": "private-client private-host",
            "TERM": "tmux-256color-private",
            "COLORTERM": "truecolor-private",
        },
    ) == 0

    report = output.getvalue()
    assert "Railmux diagnostics" in report
    assert "Claude Code: 9.8.7" in report
    assert "Inside tmux: yes" in report
    assert "Dedicated Railmux tmux: healthy" in report
    assert "Tmux watchdog: enabled" in report
    assert "SSH display watchdog timeout; 3 consecutive failures; " \
        "2 minutes ago" in report
    assert "SSH transport: yes" in report
    assert "256-colour=yes" in report
    assert "true-colour=no" in report
    assert "Config: ~/.config/railmux/config.toml; valid=yes" in report
    assert "Preferred agent display: swap" in report
    assert "Claude data: <custom>" in report
    assert "Privacy:" in report
    assert "review before sharing" in report
    for secret in (
        str(home), "private-user", "private-host", "private-client",
        "company-secret-project", "sk-secret-token",
        "12345678-1234-1234-1234-123456789abc",
        "/private/socket", "private-codex-wrapper",
    ):
        assert secret not in report


def test_doctor_reports_missing_tools_and_invalid_config(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.toml").write_text("[broken")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("railmux.diagnostics.shutil.which", lambda _binary: None)
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("unavailable"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident", lambda: None
    )
    output = StringIO()

    assert run_doctor(
        claude_home=home / ".claude", stdout=output, environ={}) == 0

    report = output.getvalue()
    assert "tmux: not found" in report
    assert "Dedicated Railmux tmux: unavailable" in report
    assert "Last tmux incident: none recorded" in report
    assert "Claude Code: not found" in report
    assert "Codex: not found" in report
    assert "valid=no (invalid TOML)" in report
    assert "file=absent" not in report


def test_doctor_json_uses_versioned_redacted_snapshot(monkeypatch, tmp_path):
    home = tmp_path / "private-user"
    config_dir = home / ".config" / "railmux"
    config_dir.mkdir(parents=True)
    secret_root = tmp_path / "company-secret-project"
    config_dir.joinpath("config.toml").write_text(
        "[claude]\n"
        f"binary = '{secret_root}/sk-secret-token/claude'\n"
        "[codex]\n"
        "binary = 'private-codex-wrapper'\n"
        f"home = '{secret_root}'\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: str(binary)
    )
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "tool 9.8.7 private-host "
                "12345678-1234-1234-1234-123456789abc sk-secret-token"
            ),
            stderr=str(secret_root),
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._dedicated_tmux_diagnostic",
        lambda: TmuxServerDiagnostic("healthy", context="outside"),
    )
    monkeypatch.setattr(
        "railmux.diagnostics._legacy_tmux_diagnostic",
        lambda: TmuxServerDiagnostic(
            "healthy", candidate_count=2, restart_recommended=True
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.read_last_incident",
        lambda: TmuxIncident(
            100, "remote-display", "remote-display-watchdog-timeout", 3
        ),
    )
    monkeypatch.setattr(
        "railmux.diagnostics.tmux_health.incident_age",
        lambda _recorded: "2 minutes ago",
    )
    output = StringIO()

    assert run_doctor(
        claude_home=secret_root / "claude-data",
        stdout=output,
        environ={
            "TMUX": "/private/socket,123,0",
            "SSH_CONNECTION": "private-client private-host",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        },
        json_output=True,
    ) == 0

    payload = json.loads(output.getvalue())
    assert payload["schema_version"] == 1
    assert payload["dedicated_tmux"] == {
        "candidate_count": None,
        "context": "outside",
        "restart_recommended": False,
        "status": "healthy",
    }
    assert payload["legacy_tmux"]["candidate_count"] == 2
    assert payload["last_tmux_incident"]["category"] == (
        "remote-display-watchdog-timeout"
    )
    assert payload["tools"]["claude_code"] == {
        "status": "available",
        "version": "9.8.7",
    }
    assert payload["data_directories"]["claude"]["path"] == "<custom>"
    encoded = output.getvalue()
    for secret in (
        str(home),
        str(secret_root),
        "private-user",
        "private-host",
        "private-client",
        "sk-secret-token",
        "12345678-1234-1234-1234-123456789abc",
        "/private/socket",
        "private-codex-wrapper",
    ):
        assert secret not in encoded
