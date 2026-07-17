"""Regression tests for Codex-aware cleanup / delete, CODEX_HOME single-source,
API-key handling, cross-mode metadata mapping, and the Codex transcript hint.

Covers codex-mode-review.md issues #1 (delete path + wrong cleanup), #7
(CODEX_HOME single source), #8 (no secret in tmux command + env-key parsing),
#9 (cross-mode metadata mixup), #5 (transcript format hint) and #16 (Info modal
Codex lookup).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from railmux.config import Config
from railmux.models import Project, SessionMeta
from railmux.ui import app as app_mod
from railmux.ui.app import App, _Running
from railmux.ui.running_pane import RunningEntry


UUID = "12345678-1234-1234-1234-1234567890ab"


# ── helpers ──────────────────────────────────────────────────────────────

def _project(claude_dir: Path | None = None, encoded: str = "-tmp-p") -> Project:
    return Project(
        real_path=Path("/tmp/p"),
        encoded_name=encoded,
        claude_dir=claude_dir if claude_dir is not None
        else Path("/tmp/p/.claude/projects/-tmp-p"),
        session_count=1,
        last_activity_ts=0.0,
    )


def _codex_meta(session_id: str = UUID, jsonl: Path | None = None) -> SessionMeta:
    return SessionMeta(
        project=_project(),
        session_id=session_id,
        jsonl_path=jsonl or Path("/tmp/rollout.jsonl"),
        title="Codex chat",
        message_count=1,
        token_total=1,
        last_mtime=1.0,
        session_type="codex",
    )


def _cleanup_app(monkeypatch, running):
    """Bare App wired for exercising the cleanup helpers."""
    app = App.__new__(App)
    app._config = Config()
    app._claude_home = Path("/nonexistent-claude-home")
    app._running = running
    app._codex_index = MagicMock()
    app._session_cache = MagicMock()
    app._project_snapshot_at = 123.0
    statuses: list[tuple[str, str]] = []
    app._set_status = lambda text, level="info": statuses.append((text, level))
    refreshed: list[bool] = []
    app._refresh = lambda: refreshed.append(True)
    return app, statuses, refreshed


# ── #1 / #7: codex delete command shape + resolved CODEX_HOME ────────────

def test_codex_delete_runs_force_command_with_resolved_home(monkeypatch, tmp_path):
    app = App.__new__(App)
    app._config = Config(codex_binary="codex", codex_home=str(tmp_path / "cx"))
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert app._codex_delete(UUID) is True
    assert captured["argv"] == ["codex", "delete", "--force", UUID]
    assert captured["env"]["CODEX_HOME"] == str(tmp_path / "cx")


def test_codex_delete_nonzero_exit_returns_false(monkeypatch):
    app = App.__new__(App)
    app._config = Config()
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=1))
    assert app._codex_delete(UUID) is False


def test_codex_delete_oserror_returns_false(monkeypatch):
    app = App.__new__(App)
    app._config = Config()

    def boom(*a, **k):
        raise OSError("no codex")

    monkeypatch.setattr("subprocess.run", boom)
    assert app._codex_delete(UUID) is False


def test_codex_delete_rejects_placeholder():
    app = App.__new__(App)
    app._config = Config()
    assert app._codex_delete("__new__-1") is False
    assert app._codex_delete("") is False


# ── #1: cleanup routes by provider, keeps state on failure ───────────────

def test_cleanup_codex_success_forgets_and_reports_deleted(monkeypatch):
    running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="p/Chat",
                              session_type="codex")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app, "_codex_delete", lambda u: True)
    app._cleanup_codex_session(UUID, "cx-abc", "p/Chat")
    assert UUID not in app._running
    app._codex_index.invalidate.assert_called_once()
    assert refreshed == [True]
    assert statuses[-1] == ("Deleted: p/Chat", "info")


def test_cleanup_codex_failure_keeps_registry_and_index(monkeypatch):
    running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="p/Chat",
                              session_type="codex")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app, "_codex_delete", lambda u: False)
    app._cleanup_codex_session(UUID, "cx-abc", "p/Chat")
    # Delete failed → nothing removed, no false "Deleted", error shown.
    assert UUID in app._running
    app._codex_index.invalidate.assert_not_called()
    assert refreshed == []
    assert statuses[-1][1] == "error"
    assert "failed" in statuses[-1][0]


def test_cleanup_codex_placeholder_is_killed_not_deleted(monkeypatch):
    running = {"__new__-1": _Running(key="__new__-1", tmux_name="cx-new",
                                     label="p/(new)", session_type="codex")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    # A placeholder has no rollout; _codex_delete must not even be called.
    monkeypatch.setattr(app, "_codex_delete",
                        lambda u: pytest.fail("must not delete a placeholder"))
    app._cleanup_codex_session("__new__-1", "cx-new", "p/(new)")
    assert "__new__-1" not in app._running
    assert statuses[-1] == ("Killed: p/(new)", "info")


def test_cleanup_session_codex_kills_tmux_then_never_unlinks(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="x",
                              session_type="codex")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    killed: list[str] = []
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda n: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "kill_session",
                        lambda n: (killed.append(n) or True))
    monkeypatch.setattr(app, "_codex_delete", lambda u: True)
    # A Codex delete must never touch Claude history/session-env.
    monkeypatch.setattr(app, "_remove_from_history",
                        lambda *a, **k: pytest.fail("claude history touched"))
    app._cleanup_session(session_id=UUID, jsonl_path=rollout,
                         tmux_name="cx-abc", label="x", session_type="codex")
    assert killed == ["cx-abc"]
    assert rollout.exists()  # codex path leaves the rollout for `codex delete`
    assert UUID not in app._running


# ── #1: running-pane delete never builds a Claude path for Codex ─────────

def test_do_kill_running_codex_passes_no_claude_path(monkeypatch):
    proj = _project()  # has a real claude_dir
    running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="x",
                              project=proj, session_type="codex")}
    app = App.__new__(App)
    app._running = running
    app._close_modal = lambda: None
    captured: dict = {}
    app._cleanup_session = lambda **kw: captured.update(kw)
    app._do_kill_running("cx-abc", UUID, proj)
    assert captured["session_type"] == "codex"
    assert captured["jsonl_path"] is None  # never claude_dir/<id>.jsonl


def test_do_kill_running_codex_only_project_no_relative_path(monkeypatch):
    synth = _project(claude_dir=Path(), encoded="codex-tmp-p")
    running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="x",
                              project=synth, session_type="codex")}
    app = App.__new__(App)
    app._running = running
    app._close_modal = lambda: None
    captured: dict = {}
    app._cleanup_session = lambda **kw: captured.update(kw)
    app._do_kill_running("cx-abc", UUID, synth)
    # Codex-only synthetic project (claude_dir=Path()) must NOT produce a
    # relative "<uuid>.jsonl" path that could delete an unrelated file.
    assert captured["jsonl_path"] is None
    assert captured["session_type"] == "codex"


def test_do_kill_running_claude_still_builds_jsonl_path():
    proj = _project()
    running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="x",
                              project=proj, session_type="claude")}
    app = App.__new__(App)
    app._running = running
    app._close_modal = lambda: None
    captured: dict = {}
    app._cleanup_session = lambda **kw: captured.update(kw)
    app._do_kill_running("cc-abc", UUID, proj)
    assert captured["session_type"] == "claude"
    assert captured["jsonl_path"] == proj.claude_dir / f"{UUID}.jsonl"


def test_do_delete_session_codex_marks_codex_type():
    app = App.__new__(App)
    app._close_modal = lambda: None
    captured: dict = {}
    app._cleanup_session = lambda **kw: captured.update(kw)
    app._do_delete_session(_codex_meta())
    assert captured["session_type"] == "codex"


def test_cleanup_session_claude_still_unlinks(monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("{}\n")
    running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="x",
                              session_type="claude")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda n: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "kill_session", lambda n: True)
    monkeypatch.setattr(app, "_remove_from_history", lambda *a, **k: None)
    app._cleanup_session(session_id=UUID, jsonl_path=jsonl,
                         tmux_name="cc-abc", label="x", session_type="claude")
    assert not jsonl.exists()
    assert statuses[-1] == ("Deleted: x", "info")


def test_cleanup_aborts_if_tmux_writer_cannot_be_stopped(monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("{}\n")
    running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="x")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _n: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "kill_session", lambda _n: False)
    app._cleanup_session(
        session_id=UUID, jsonl_path=jsonl, tmux_name="cc-abc", label="x")
    assert jsonl.exists()
    assert UUID in app._running
    assert refreshed == []
    assert statuses[-1][1] == "error"


def test_cleanup_aborts_if_displayed_real_pane_cannot_return_home(
        monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("{}\n")
    running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="x")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    transport = MagicMock()
    transport.prepare_kill.return_value = False
    app._display_transport_manager = transport
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _n: True)
    killed = []
    monkeypatch.setattr(
        app_mod.tmux_ctl, "kill_session", lambda name: killed.append(name) or True)

    app._cleanup_session(
        session_id=UUID, jsonl_path=jsonl,
        tmux_name="cc-abc", label="x")

    assert jsonl.exists()
    assert killed == []
    assert refreshed == []
    assert statuses[-1][1] == "error"


def test_cleanup_waits_for_writer_exit_before_deleting(monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("{}\n")
    running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="x")}
    app, statuses, refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _n: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_process_ids",
                        lambda _n: (100, 200))
    monkeypatch.setattr(app_mod.tmux_ctl, "kill_session", lambda _n: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "wait_for_processes_exit",
                        lambda _pids: False)

    app._cleanup_session(
        session_id=UUID, jsonl_path=jsonl, tmux_name="cc-abc", label="x")

    assert jsonl.exists()
    assert UUID in app._running
    assert refreshed == []
    assert statuses[-1][1] == "error"
    assert "shutting down" in statuses[-1][0]


def test_cleanup_removes_recreated_stub_after_history_cleanup(
        monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("conversation\n")
    app, statuses, _refreshed = _cleanup_app(monkeypatch, {})

    def recreate(*_args, **_kwargs):
        jsonl.write_text('{"type":"ai-title","aiTitle":"stub"}\n')
        return True

    monkeypatch.setattr(app, "_remove_from_history", recreate)
    app._cleanup_session(session_id=UUID, jsonl_path=jsonl, label="x")

    assert not jsonl.exists()
    assert statuses[-1] == ("Deleted: x", "info")


def test_cleanup_reports_first_unlink_failure(monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("conversation\n")
    app, statuses, _refreshed = _cleanup_app(monkeypatch, {})
    original_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == jsonl:
            raise OSError("read-only filesystem")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    app._cleanup_session(session_id=UUID, jsonl_path=jsonl, label="x")

    assert jsonl.exists()
    assert statuses[-1][1] == "error"
    assert statuses[-1][0].startswith("failed to delete ")


def test_cleanup_reports_recreated_stub_unlink_failure(monkeypatch, tmp_path):
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("conversation\n")
    app, statuses, _refreshed = _cleanup_app(monkeypatch, {})
    original_unlink = Path.unlink
    target_unlinks = 0

    def fail_second_target_unlink(path, *args, **kwargs):
        nonlocal target_unlinks
        if path == jsonl:
            target_unlinks += 1
            if target_unlinks == 2:
                raise OSError("writer owns file")
        return original_unlink(path, *args, **kwargs)

    def recreate(*_args, **_kwargs):
        jsonl.write_text('{"type":"ai-title","aiTitle":"stub"}\n')
        return True

    monkeypatch.setattr(Path, "unlink", fail_second_target_unlink)
    monkeypatch.setattr(app, "_remove_from_history", recreate)
    app._cleanup_session(session_id=UUID, jsonl_path=jsonl, label="x")

    assert jsonl.exists()
    assert statuses[-1][1] == "error"
    assert "recreated and could not be deleted" in statuses[-1][0]
    assert not any(text.startswith("Deleted:") for text, _level in statuses)


def test_cleanup_uses_configured_claude_home(monkeypatch, tmp_path):
    claude_home = tmp_path / "custom-claude"
    env_dir = claude_home / "session-env" / UUID
    env_dir.mkdir(parents=True)
    history = claude_home / "history.jsonl"
    history.write_text(
        '{"sessionId":"' + UUID + '","display":"remove"}\n'
        '{"sessionId":"keep","display":"keep"}\n')
    jsonl = tmp_path / f"{UUID}.jsonl"
    jsonl.write_text("conversation\n")
    app, statuses, _refreshed = _cleanup_app(monkeypatch, {})
    app._claude_home = claude_home

    app._cleanup_session(session_id=UUID, jsonl_path=jsonl, label="x")

    assert not env_dir.exists()
    assert UUID not in history.read_text()
    assert '"sessionId":"keep"' in history.read_text()
    assert statuses[-1] == ("Deleted: x", "info")


def test_cleanup_claude_placeholder_reports_killed(monkeypatch):
    key = "__new__-test-1"
    running = {key: _Running(key=key, tmux_name="cc-new", label="p/(new)")}
    app, statuses, _refreshed = _cleanup_app(monkeypatch, running)
    monkeypatch.setattr(app_mod.tmux_ctl, "session_exists", lambda _n: False)

    app._cleanup_session(
        session_id=None, tmux_name="cc-new", label="p/(new)")

    assert key not in app._running
    assert statuses[-1] == ("Killed: p/(new)", "info")


# ── #8: railmux never injects the provider API key by ANY channel ────────

def test_codex_env_includes_resolved_codex_home(tmp_path):
    app = App.__new__(App)
    app._config = Config(codex_home=str(tmp_path / "cx"))  # no config.toml
    env = app._codex_env()
    assert env["CODEX_HOME"] == str(tmp_path / "cx")


def test_codex_env_never_includes_provider_api_key(monkeypatch, tmp_path):
    """Even with a provider env_key configured AND that key set in the process
    environment, ``_codex_env`` must return ONLY the non-secret CODEX_HOME. tmux
    retains ``-e`` values in the session environment (queryable via
    ``show-environment``), so injecting the key at all would leak it (#8)."""
    (tmp_path / "config.toml").write_text(
        'model_provider = "deepseek"\n'
        '[model_providers.deepseek]\nenv_key = "DEEPSEEK_API_KEY"\n'
    )
    app = App.__new__(App)
    app._config = Config(codex_home=str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sekret")
    env = app._codex_env()
    assert env == {"CODEX_HOME": str(tmp_path)}
    assert "DEEPSEEK_API_KEY" not in env
    assert "sekret" not in "".join(env.values())


def test_no_key_reading_helpers_remain():
    """The provider-guessing / key-reading code (only ever used to inject the
    secret) is fully removed, not merely bypassed (#8)."""
    assert not hasattr(App, "_read_codex_env_key")
    assert not hasattr(App, "_probe_shell_var")


# ── #8: neither the shell command NOR the tmux -e env carries a secret ────

def test_launch_never_passes_secret_via_any_channel(monkeypatch):
    """A real launch pushes only CODEX_HOME through both the shell command and
    the tmux ``-e`` env; no provider key value can appear in either (#8)."""
    app = App.__new__(App)
    app._running = {}
    app._set_status = lambda *a, **k: None
    captured: dict = {}

    def fake_shellify(cmd, cwd, env=None, login_shell=False):
        captured["shell_env"] = env
        return "SHELLCMD"

    def fake_ensure(name, shell_cmd, env=None):
        captured["tmux_env"] = env
        captured["shell_cmd"] = shell_cmd
        return True, None

    monkeypatch.setattr(app, "_shellify", fake_shellify)
    monkeypatch.setattr(app, "_ensure_detached_agent", fake_ensure)
    monkeypatch.setattr(app, "_attach_in_right_pane", lambda *a, **k: True)
    monkeypatch.setattr(app, "_session_name", lambda key: "cx-abc")

    # _codex_env is the only source of the launch env; it now yields CODEX_HOME
    # only, so the secret is never present to leak through -e.
    app._config = Config(codex_home="/h/.codex")
    env = app._codex_env()
    assert app._launch(UUID, ["codex"], Path("/tmp"), "label", None,
                       env=env, login_shell=True, session_type="codex")
    assert captured["shell_env"] == {"CODEX_HOME": "/h/.codex"}
    assert captured["tmux_env"] == {"CODEX_HOME": "/h/.codex"}
    assert "sekret" not in captured["shell_cmd"]
    assert app._running[UUID].session_type == "codex"


# ── #16: Info modal / context menu resolve Codex via the index ───────────

def test_find_session_meta_codex_uses_index():
    app = App.__new__(App)
    meta = _codex_meta()
    app._codex_index = MagicMock()
    app._codex_index.get.return_value = meta
    result = app._find_session_meta(UUID, project=None, session_type="codex")
    assert result is meta
    app._codex_index.get.assert_called_once_with(UUID, refresh=False)


def test_find_session_meta_claude_needs_project():
    app = App.__new__(App)
    app._codex_index = MagicMock()
    assert app._find_session_meta(UUID, project=None, session_type="claude") is None
    app._codex_index.get.assert_not_called()


# ── #5: transcript preview passes an explicit Codex format hint ──────────

def _transcript_app(monkeypatch):
    app = App.__new__(App)
    app._less_mouse_flag = ""
    app._right_pane_id = "%1"
    app._set_status = lambda *a, **k: None
    app._install_fullscreen_binding = lambda: None
    captured: dict = {}
    transport = MagicMock()
    transport.prepare_preview.side_effect = (
        lambda _slot: captured.__setitem__("prepared", True) or True)
    app._display_transport_manager = transport
    monkeypatch.setattr(app_mod.tmux_ctl, "pane_alive", lambda pid: True)
    monkeypatch.setattr(app_mod.tmux_ctl, "respawn_pane",
                        lambda pid, cmd: captured.__setitem__("cmd", cmd) or True)
    return app, captured


def test_show_transcript_codex_passes_explicit_format(monkeypatch):
    app, captured = _transcript_app(monkeypatch)
    assert app._show_transcript(Path("/tmp/r.jsonl"), session_type="codex")
    assert captured["prepared"] is True
    assert "railmux.transcript --format codex --preview-limit 2000 -" in captured["cmd"]


def test_show_transcript_claude_passes_explicit_format(monkeypatch):
    app, captured = _transcript_app(monkeypatch)
    assert app._show_transcript(Path("/tmp/r.jsonl"), session_type="claude")
    assert "railmux.transcript --format claude" in captured["cmd"]


def test_show_transcript_uses_secure_read_only_pager_and_quotes_python(monkeypatch):
    app, captured = _transcript_app(monkeypatch)
    monkeypatch.setattr(sys, "executable", "/tmp/Python Dir/python")
    assert app._show_transcript(Path("/tmp/a file.jsonl"), session_type="codex")
    cmd = captured["cmd"]
    assert "'/tmp/Python Dir/python' -m railmux.transcript" in cmd
    assert "tail -n 2000 '/tmp/a file.jsonl'" in cmd
    assert "--preview-limit 2000" in cmd
    assert "LESSSECURE=1" in cmd
    assert "LESSHISTFILE=-" in cmd
    assert "LESSOPEN= LESSCLOSE=" in cmd


# ── #9: cross-mode selection re-maps by real_path, never leaks synthetic ─

def test_toggle_to_claude_remaps_synthetic_codex_project(monkeypatch):
    app = App.__new__(App)
    app._codex_mode = True  # toggling makes it Claude mode
    synth = _project(claude_dir=Path(), encoded="codex-tmp-p")
    real = _project(claude_dir=Path("/tmp/p/.claude/x"), encoded="-tmp-p")
    app._selected_project = synth
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._running = {}
    app._tmux_error_bar = False
    app._apply_tmux_bar = lambda *a, **k: None
    app._set_status = lambda *a, **k: None
    monkeypatch.setattr(app, "_visible_projects", lambda **k: [real])
    selected: list = []
    monkeypatch.setattr(app, "_on_project_select", lambda p: selected.append(p))
    app._toggle_codex_mode()
    assert app._codex_mode is False
    # Remapped to the REAL Claude project (same real_path), not the synthetic.
    assert selected == [real]


def test_on_project_select_claude_skips_synthetic_project():
    app = App.__new__(App)
    app._codex_mode = False
    app._running = {}
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._set_status = lambda *a, **k: None
    app._cancel_pending_double_focus = lambda *a, **k: None
    app._pending_project = None
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.side_effect = AssertionError(
        "synthetic Codex project reached the Claude cache")
    synth = _project(claude_dir=Path(), encoded="codex-tmp-p")
    app._on_project_select(synth)
    _proj, sessions = app._sessions_pane.set_sessions.call_args.args[:2]
    assert sessions == []


# ── #4: Codex Sessions pane status matches the Running pane ───────────────

def test_codex_session_status_matches_running_pane(monkeypatch):
    """A Codex session shows the SAME refined status in the Sessions pane as in
    the Running pane. The Sessions pane must apply the same effective-status
    refinement (via _pane_sessions), not hand the raw JSONL status through, or
    the same session shows two different dots (#4)."""
    proj = _project()
    meta = SessionMeta(
        project=proj, session_id=UUID, jsonl_path=Path("/tmp/rollout.jsonl"),
        title="Codex chat", message_count=1, token_total=1, last_mtime=1.0,
        status="blocked", pending_tool=True, session_type="codex",
    )
    app = App.__new__(App)
    app._codex_mode = True
    app._running = {UUID: _Running(key=UUID, tmux_name="cx-abc", label="l",
                                   project=proj, session_type="codex")}
    app._codex_index = MagicMock()
    app._codex_index.sessions_for_cwd.return_value = [meta]
    # Codex keeps its JSONL-derived state; its permanent child processes are
    # not a valid liveness signal. Both panes must still agree.
    monkeypatch.setattr(
        app_mod.tmux_ctl, "session_has_child",
        lambda _name: pytest.fail("Codex used the Claude process heuristic"),
    )

    sessions_status = app._pane_sessions(proj, refresh=False)[0].status
    running_status = app._effective_status(meta)

    assert sessions_status == "blocked"
    assert sessions_status == running_status   # both panes agree


# ── #3 / #9: synthetic Codex project must never reach the Claude cache ────

def test_on_running_select_claude_skips_synthetic_project(monkeypatch):
    """Re-attaching to a running session whose project is a synthetic Codex
    entry (empty claude_dir) must not pass that empty dir to the Claude
    SessionCache — list_sessions would read relative to railmux's cwd (#9)."""
    synth = _project(claude_dir=Path(), encoded="codex-tmp-p")
    app = App.__new__(App)
    app._codex_mode = False
    app._selected_project = None
    app._running = {UUID: _Running(key=UUID, tmux_name="cc-abc", label="l",
                                   project=synth)}
    app._projects_pane = MagicMock()
    app._sessions_pane = MagicMock()
    app._favorites = MagicMock()
    app._favorites.get_ids.return_value = set()
    app._set_status = lambda *a, **k: None
    app._cancel_pending_double_focus = lambda *a, **k: None
    app._attach_in_right_pane = lambda *a, **k: True
    # _project_in_current_view falls back to the (synthetic) project itself.
    monkeypatch.setattr(app, "_visible_projects", lambda **k: [])
    app._session_cache = MagicMock()
    app._session_cache.list_sessions.side_effect = AssertionError(
        "synthetic Codex project reached the Claude cache")

    app._on_running_select(RunningEntry(tmux_name="cc-abc", label="l",
                                        status="idle"))

    _proj, sessions = app._sessions_pane.set_sessions.call_args.args[:2]
    assert sessions == []
