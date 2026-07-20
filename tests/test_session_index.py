import time

from railmux.discovery import list_projects
from railmux.session_index import list_sessions


def _make_one_project(claude_home, tmp_path, write_session_fixture, sessions: list[tuple[str, list[dict]]]):
    real = tmp_path / "proj"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    for sid, records in sessions:
        write_session_fixture(encoded, sid, records)
    projects = list_projects(claude_home)
    return projects[0]


def test_empty_project_returns_no_sessions(claude_home, write_session_fixture, tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    (claude_home / "projects" / encoded).mkdir()
    projects = list_projects(claude_home)
    assert list_sessions(projects[0]) == []


def test_title_only_metadata_stub_is_hidden(claude_home, tmp_path):
    real = tmp_path / "title_stub"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    project_dir = claude_home / "projects" / encoded
    project_dir.mkdir()
    (project_dir / "01234567-89ab-cdef-0123-456789abcdef.jsonl").write_text(
        '{"type":"ai-title","aiTitle":"Recreated stub"}\n')

    project = list_projects(claude_home)[0]
    assert project.session_count == 0
    assert list_sessions(project) == []


def test_session_basic_metadata(claude_home, write_session_fixture, tmp_path):
    project = _make_one_project(claude_home, tmp_path, write_session_fixture, [
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "ai-title", "aiTitle": "Hello session"},
        ]),
    ])
    sessions = list_sessions(project)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert s.title == "Hello session"
    assert s.message_count == 2  # 1 user + 1 assistant
    assert s.token_total == 15
    assert s.last_mtime > 0


def test_counts_real_messages_and_deduplicates_claude_usage(
    claude_home, write_session_fixture, tmp_path,
):
    project = _make_one_project(claude_home, tmp_path, write_session_fixture, [
        ("abababab-abab-abab-abab-abababababab", [
            {"type": "user", "message": {
                "role": "user", "content": "Run the tests"}},
            {"type": "user", "message": {
                "role": "user", "content": [{
                    "type": "tool_result", "content": "test output"}]}},
            {"type": "assistant", "message": {
                "id": "msg-1", "role": "assistant", "content": [],
                "usage": {"input_tokens": 10,
                          "cache_creation_input_tokens": 20,
                          "cache_read_input_tokens": 30,
                          "output_tokens": 4}}},
            {"type": "assistant", "message": {
                "id": "msg-1", "role": "assistant", "content": [],
                "usage": {"input_tokens": 10,
                          "cache_creation_input_tokens": 20,
                          "cache_read_input_tokens": 30,
                          "output_tokens": 8}}},
            {"type": "assistant", "message": {
                "id": "msg-2", "role": "assistant", "content": [],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2}}},
        ]),
    ])

    session = list_sessions(project)[0]

    # One real user prompt + two unique provider assistant messages. The tool
    # result and duplicate streaming record are not conversation messages.
    assert session.message_count == 3
    # max(msg-1 snapshots) + msg-2, including cache creation/read tokens.
    assert session.token_total == 75


def test_most_recent_ai_title_wins(claude_home, write_session_fixture, tmp_path):
    project = _make_one_project(claude_home, tmp_path, write_session_fixture, [
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
            {"type": "ai-title", "aiTitle": "First"},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "ai-title", "aiTitle": "Second"},
            {"type": "ai-title", "aiTitle": "Third (latest)"},
        ]),
    ])
    sessions = list_sessions(project)
    assert sessions[0].title == "Third (latest)"


def test_sessions_sorted_by_mtime_desc(claude_home, write_session_fixture, tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    write_session_fixture(encoded, "11111111-1111-1111-1111-111111111111", [{"type": "user", "message": {"role": "user", "content": "old"}}])
    time.sleep(0.05)
    write_session_fixture(encoded, "22222222-2222-2222-2222-222222222222", [{"type": "user", "message": {"role": "user", "content": "new"}}])

    project = list_projects(claude_home)[0]
    sessions = list_sessions(project)
    assert [s.session_id for s in sessions] == ["22222222-2222-2222-2222-222222222222", "11111111-1111-1111-1111-111111111111"]


def test_session_with_no_title_uses_fallback(claude_home, write_session_fixture, tmp_path):
    project = _make_one_project(claude_home, tmp_path, write_session_fixture, [
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ]),
    ])
    sessions = list_sessions(project)
    # No ai-title → falls back to first user message.
    assert sessions[0].title == "hi"
    assert sessions[0].display_title == "hi"


def test_malformed_lines_are_skipped(claude_home, write_session_fixture, tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    encoded = str(real).replace("/", "-")
    jpath = (claude_home / "projects" / encoded)
    jpath.mkdir(parents=True, exist_ok=True)
    with (jpath / "dddddddd-dddd-dddd-dddd-dddddddddddd.jsonl").open("w") as f:
        f.write("not json at all\n")
        f.write('{"type": "user", "message": {"role": "user", "content": "hi"}}\n')
        f.write('{"type": "assistant", "message": {"role": "assistant", "content": "ok", "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}}\n')
        f.write("{partial json\n")
    project = list_projects(claude_home)[0]
    sessions = list_sessions(project)
    assert len(sessions) == 1
    assert sessions[0].message_count == 2  # 1 user + 1 assistant


def test_background_session_is_filtered(claude_home, write_session_fixture, tmp_path):
    """Background-job sessions (sessionKind: bg) must not appear in the sidebar."""
    project = _make_one_project(claude_home, tmp_path, write_session_fixture, [
        ("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", [
            {"type": "user", "message": {"role": "user", "content": "normal session"}, "sessionKind": "bg"},
            {"type": "assistant", "message": {"role": "assistant", "content": "ok", "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]),
    ])
    sessions = list_sessions(project)
    assert len(sessions) == 0, "bg session should be filtered out"
