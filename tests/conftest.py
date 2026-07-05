import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_path_cache(tmp_path, monkeypatch):
    """Redirect discovery's persistent path cache into the test's tmp dir so
    tests never read or write the real ~/.config/ccmgr/path-cache.json."""
    import ccmgr.discovery as discovery
    cache_file = tmp_path / "path-cache.json"
    monkeypatch.setattr(discovery, "_path_cache_file", lambda: cache_file)


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """A fake ~/.claude/ tree for tests."""
    home = tmp_path / ".claude"
    (home / "projects").mkdir(parents=True)
    return home


def write_session(claude_home: Path, encoded_project: str, session_id: str, records: list[dict]) -> Path:
    """Helper: create a session JSONL file under <claude_home>/projects/<encoded>/.

    If *records* has no assistant entry, a minimal assistant record is appended
    automatically.  Real sessions always have at least one assistant reply;
    sessions without one are orphans that ccmgr filters out.
    """
    proj_dir = claude_home / "projects" / encoded_project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
        # Ensure at least one assistant record exists so the session passes
        # the orphan filter in _scan_session.
        has_assistant = any(r.get("type") == "assistant" for r in records)
        if not has_assistant:
            f.write(json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "Hello!",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }) + "\n")
    return path


@pytest.fixture
def write_session_fixture(claude_home):
    def _writer(encoded_project: str, session_id: str, records: list[dict]) -> Path:
        return write_session(claude_home, encoded_project, session_id, records)
    return _writer
