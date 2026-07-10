"""History cleanup must not overwrite records appended by Claude."""
import json
from pathlib import Path

from ccmgr.ui.app import App


def _record(session_id: str, prompt: str) -> str:
    return json.dumps({"sessionId": session_id, "display": prompt})


def test_remove_from_history_preserves_other_entries(tmp_path, monkeypatch):
    history = tmp_path / ".claude" / "history.jsonl"
    history.parent.mkdir()
    history.write_text(
        _record("remove", "old") + "\n"
        + _record("keep", "current") + "\n"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    App._remove_from_history("remove")

    records = [json.loads(line) for line in history.read_text().splitlines()]
    assert [record["sessionId"] for record in records] == ["keep"]


def test_remove_from_history_retries_concurrent_append(tmp_path, monkeypatch):
    history = tmp_path / ".claude" / "history.jsonl"
    history.parent.mkdir()
    history.write_text(
        _record("remove", "old") + "\n"
        + _record("keep", "current") + "\n"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_read_text = Path.read_text
    raced = False

    def read_then_append(path, *args, **kwargs):
        nonlocal raced
        text = original_read_text(path, *args, **kwargs)
        if path == history and not raced:
            raced = True
            with history.open("a") as stream:
                stream.write(_record("new", "concurrent") + "\n")
        return text

    monkeypatch.setattr(Path, "read_text", read_then_append)

    App._remove_from_history("remove")

    records = [
        json.loads(line)
        for line in original_read_text(history).splitlines()
    ]
    assert [record["sessionId"] for record in records] == ["keep", "new"]


def test_remove_from_history_gives_up_without_overwriting(tmp_path, monkeypatch):
    history = tmp_path / ".claude" / "history.jsonl"
    history.parent.mkdir()
    history.write_text(_record("remove", "old") + "\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_read_text = Path.read_text

    def always_append(path, *args, **kwargs):
        text = original_read_text(path, *args, **kwargs)
        with history.open("a") as stream:
            stream.write(_record("new", "concurrent") + "\n")
        return text

    monkeypatch.setattr(Path, "read_text", always_append)
    App._remove_from_history("remove")

    assert '"sessionId": "remove"' in original_read_text(history)
