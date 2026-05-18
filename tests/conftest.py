import json
from pathlib import Path

import pytest


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """A fake ~/.claude/ tree for tests."""
    home = tmp_path / ".claude"
    (home / "projects").mkdir(parents=True)
    return home


def write_session(claude_home: Path, encoded_project: str, session_id: str, records: list[dict]) -> Path:
    """Helper: create a session JSONL file under <claude_home>/projects/<encoded>/."""
    proj_dir = claude_home / "projects" / encoded_project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


@pytest.fixture
def write_session_fixture(claude_home):
    def _writer(encoded_project: str, session_id: str, records: list[dict]) -> Path:
        return write_session(claude_home, encoded_project, session_id, records)
    return _writer
