from io import StringIO
from types import SimpleNamespace

from railmux.diagnostics import _version, run_doctor


def test_version_preserves_tmux_letter_suffix(monkeypatch):
    monkeypatch.setattr(
        "railmux.diagnostics.shutil.which", lambda binary: f"/bin/{binary}")
    monkeypatch.setattr(
        "railmux.diagnostics.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="tmux 3.2a", stderr="", returncode=0),
    )

    assert _version("tmux", "-V") == "3.2a"


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
    output = StringIO()

    assert run_doctor(
        claude_home=home / ".claude", stdout=output, environ={}) == 0

    report = output.getvalue()
    assert "tmux: not found" in report
    assert "Claude Code: not found" in report
    assert "Codex: not found" in report
    assert "valid=no (invalid TOML)" in report
    assert "file=absent" not in report
