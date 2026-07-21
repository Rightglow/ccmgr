from __future__ import annotations

import inspect
import io
import os
import struct
import subprocess
import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from railmux.fast_display_protocol import (
    DISPLAY_MAGIC,
    HistoryBatch,
    HistorySnapshot,
    InputFrameDecoder,
    InputKind,
    PROTOCOL_VERSION,
    REMOTE_HELLO_PREFIX,
    REMOTE_START,
    ScreenUpdate,
    ScreenUpdateDecoder as ClientScreenUpdateDecoder,
    ServerMessageDecoder,
    TerminalMode,
    UpdateKind,
    decode_history_prefetch,
    decode_history_request,
    encode_history_batch,
    encode_history_prefetch,
    encode_history_request,
    encode_history_snapshot,
    encode_update,
)
from railmux.fast_display_server import parse_args as parse_server_args
from railmux.fast_display_server import render_rows
from railmux.fast_display_server import terminal_modes_for_screen
from railmux import fast_display_client, fast_display_server
from railmux.fast_display_client import (
    HistoryAction,
    LOCAL_ESCAPE,
    LocalHistoryView,
    RemoteHello,
    RemoteStartKind,
    RemoteStartup,
    ScreenModel,
    SgrMouseEvent,
    TerminalInputDecoder,
    TerminalSurface,
    UpdateKind as ClientUpdateKind,
    build_ssh_argv,
    build_ssh_install_argv,
    await_remote_startup,
    coalesce_forwarded_wheel,
    encode_input as encode_client_input,
    encode_keyframe_request as encode_client_keyframe_request,
    encode_resize as encode_client_resize,
    input_may_change_routes,
    parse_args as parse_client_args,
    parse_remote_hello,
    prepare_remote_process,
    remote_install_help,
    split_local_escape,
)


def _keyframe(
    sequence: int = 1,
    width: int = 4,
    height: int = 2,
    terminal_modes: TerminalMode = TerminalMode.NONE,
) -> ScreenUpdate:
    return ScreenUpdate(
        kind=UpdateKind.KEYFRAME,
        sequence=sequence,
        width=width,
        height=height,
        cursor_x=1,
        cursor_y=0,
        cursor_visible=True,
        rows=tuple((index, f"row-{index}".encode()) for index in range(height)),
        terminal_modes=terminal_modes,
    )


def test_compressed_keyframe_crosses_standalone_client_decoder_in_parts():
    packet = encode_update(_keyframe())
    decoder = ClientScreenUpdateDecoder()

    assert decoder.feed(b"login banner\n" + packet[:8]) == []
    assert decoder.feed(packet[8:-1]) == []
    updates = decoder.feed(packet[-1:])

    assert len(updates) == 1
    update = updates[0]
    assert update.kind is ClientUpdateKind.KEYFRAME
    assert update.sequence == 1
    assert (update.width, update.height) == (4, 2)
    assert update.rows == ((0, b"row-0"), (1, b"row-1"))
    assert update.terminal_modes is TerminalMode.NONE


def test_v6_wire_round_trips_allowlisted_terminal_modes_and_rejects_unknown():
    modes = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    update = ClientScreenUpdateDecoder().feed(
        encode_update(_keyframe(terminal_modes=modes))
    )[0]

    assert update.terminal_modes == modes
    with pytest.raises(ValueError, match="unknown terminal mode"):
        encode_update(_keyframe(terminal_modes=TerminalMode(1 << 8)))

    malformed = bytearray(encode_update(_keyframe()))
    modes_offset = len(DISPLAY_MAGIC) + 4 + 1 + 14
    malformed[modes_offset:modes_offset + 2] = struct.pack(">H", 1 << 8)
    assert ClientScreenUpdateDecoder().feed(malformed) == []


def test_v6_decoder_does_not_accept_a_v5_packet_prefix():
    old_packet = b"RMUXD5\x00" + struct.pack(">I", 32) + bytes(32)

    assert ClientScreenUpdateDecoder().feed(old_packet) == []


def test_v6_unified_decoder_round_trips_history_between_screen_updates():
    snapshot = HistorySnapshot(
        request_id=7,
        pane_id="%42",
        x=30,
        y=1,
        width=50,
        height=2,
        lines=(b"old-1", b"old-2", b"visible-1", b"visible-2"),
    )
    packet = b"".join((
        encode_update(_keyframe()),
        encode_history_snapshot(snapshot),
        encode_update(_keyframe(sequence=2)),
    ))
    decoder = ServerMessageDecoder()

    assert decoder.feed(packet[:11]) == []
    messages = decoder.feed(packet[11:])

    assert messages == [_keyframe(), snapshot, _keyframe(sequence=2)]


def test_rejected_history_response_is_bounded_and_screen_decoder_ignores_it():
    rejected = HistorySnapshot(9, None)
    packet = encode_history_snapshot(rejected) + encode_update(_keyframe())

    assert ServerMessageDecoder().feed(packet)[0] == rejected
    assert ClientScreenUpdateDecoder().feed(packet) == [_keyframe()]


def test_history_request_round_trip_validates_pointer_and_line_limit():
    decoder = InputFrameDecoder()
    message = decoder.feed(encode_history_request(12, 80, 24, 1500))[0]

    assert message.kind is InputKind.REQUEST_HISTORY
    assert decode_history_request(message.data) == (12, 80, 24, 1500)
    with pytest.raises(ValueError):
        encode_history_request(1, 0, 24)
    with pytest.raises(ValueError):
        encode_history_request(1, 80, 24, 5000)


def test_v6_history_prefetch_batch_round_trip_is_atomic_and_bounded():
    decoder = InputFrameDecoder()
    request = decoder.feed(encode_history_prefetch(17, 300))[0]
    assert request.kind is InputKind.PREFETCH_HISTORY
    assert decode_history_prefetch(request.data) == (17, 300)

    snapshots = (
        HistorySnapshot(17, "%8", 31, 0, 49, 2, (b"a", b"b", b"c")),
        HistorySnapshot(17, "%9", 31, 3, 49, 2, (b"d", b"e", b"f")),
    )
    batch = HistoryBatch(17, snapshots)

    assert ServerMessageDecoder().feed(encode_history_batch(batch)) == [batch]
    with pytest.raises(ValueError):
        encode_history_prefetch(1, 5000)


def test_client_decoder_recovers_from_false_marker_and_reads_patch():
    false = DISPLAY_MAGIC + struct.pack(">I", 1) + b"x"
    patch = ScreenUpdate(
        UpdateKind.PATCH, 2, 4, 2, 2, 1, False, ((1, b"changed"),)
    )

    updates = ClientScreenUpdateDecoder().feed(false + encode_update(patch))

    assert len(updates) == 1
    assert updates[0].kind is ClientUpdateKind.PATCH
    assert updates[0].rows == ((1, b"changed"),)


def test_input_protocol_decodes_bytes_resize_and_keyframe_request():
    decoder = InputFrameDecoder()
    packet = b"".join((
        encode_client_input(b"one"),
        encode_client_resize(120, 40),
        encode_client_keyframe_request(),
    ))

    assert decoder.feed(packet[:5]) == []
    messages = decoder.feed(packet[5:])

    assert [(message.kind, message.data) for message in messages] == [
        (InputKind.BYTES, b"one"),
        (InputKind.RESIZE, struct.pack(">HH", 120, 40)),
        (InputKind.REQUEST_KEYFRAME, b""),
    ]
    with pytest.raises(ValueError):
        encode_client_input(b"")
    with pytest.raises(ValueError):
        encode_client_resize(39, 40)


def test_screen_model_applies_patch_and_rejects_gap_or_wrong_geometry():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    keyframe = decoder.feed(encode_update(_keyframe()))[0]
    first = model.apply(keyframe, size)
    assert first is not None
    assert first.clear is True
    assert first.rows == (b"row-0", b"row-1")

    patch = ScreenUpdate(
        UpdateKind.PATCH, 2, 4, 2, 3, 1, True, ((1, b"latest"),)
    )
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    assert applied.clear is False
    assert applied.changed_rows == (1,)
    assert applied.rows == (b"row-0", b"latest")

    gap = ScreenUpdate(UpdateKind.PATCH, 4, 4, 2, 0, 0, True, ())
    assert model.apply(decoder.feed(encode_update(gap))[0], size) is None
    assert model.apply(keyframe, os.terminal_size((5, 2))) is None


def test_terminal_surface_paints_only_changed_patch_rows_and_restores_mouse():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    model.apply(decoder.feed(encode_update(_keyframe()))[0], size)
    patch = ScreenUpdate(
        UpdateKind.PATCH, 2, 4, 2, 1, 1, True, ((1, b"changed"),)
    )
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    stream = io.BytesIO()
    surface = TerminalSurface(stream)

    surface.paint(applied)
    surface.close()

    rendered = stream.getvalue()
    assert b"\033[?1002h\033[?1006h" in rendered
    assert b"\033[?1002l\033[?1006l" in rendered
    assert b"\033[2;1H\033[2Kchanged" in rendered
    assert b"\033[1;1H" not in rendered
    assert b"\033[2J" in rendered  # alternate-screen initialization only


def test_terminal_surface_can_leave_mouse_to_the_local_terminal():
    stream = io.BytesIO()
    surface = TerminalSurface(stream, mouse=False)

    surface.start()
    surface.close()

    assert b"?1002" not in stream.getvalue()
    assert b"?1006" not in stream.getvalue()


def test_terminal_surface_paints_only_the_local_history_pane_rectangle():
    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    snapshot = HistorySnapshot(
        1, "%9", x=10, y=2, width=8, height=2, lines=(b"one", b"two")
    )
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=20, height=5))
        )[0],
        os.terminal_size((20, 5)),
    )
    assert screen is not None

    surface.paint_overlays(
        screen, ((snapshot, (b"one", "你好long".encode())),)
    )
    surface.close()

    rendered = stream.getvalue()
    assert b"\033[3;11H\033[8Xone" in rendered
    assert b"\033[4;11H\033[8X" + "你好long".encode() in rendered
    assert b"\033[3;1H" not in rendered


def test_terminal_surface_composites_live_rows_then_multiple_frozen_panes():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=12, height=3))
        )[0],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    left = HistorySnapshot(1, "%8", 2, 0, 3, 2, ())
    right = HistorySnapshot(1, "%9", 7, 1, 3, 2, ())
    stream = io.BytesIO()

    TerminalSurface(stream).paint(
        screen,
        ((left, (b"a0", b"a1")), (right, (b"b0", b"b1"))),
    )

    rendered = stream.getvalue()
    live_row = rendered.index(b"\033[1;1H\033[2Krow-0")
    left_overlay = rendered.index(b"\033[1;3H\033[3Xa0")
    right_overlay = rendered.index(b"\033[2;8H\033[3Xb0")
    cursor = rendered.rindex(b"\033[1;2H")
    assert live_row < left_overlay < cursor
    assert live_row < right_overlay < cursor
    assert rendered.endswith(b"\033[?25h")


def test_terminal_surface_hides_cursor_covered_by_a_frozen_pane():
    screen = ScreenModel().apply(
        ClientScreenUpdateDecoder().feed(
            encode_update(_keyframe(width=12, height=3))
        )[0],
        os.terminal_size((12, 3)),
    )
    assert screen is not None
    covering = HistorySnapshot(1, "%8", 0, 0, 4, 2, ())
    stream = io.BytesIO()

    TerminalSurface(stream).paint(screen, ((covering, (b"a", b"b")),))

    assert stream.getvalue().endswith(b"\033[?25l")


def test_mode_only_patch_reconciles_terminal_modes_once_and_restores_them():
    decoder = ClientScreenUpdateDecoder()
    model = ScreenModel()
    size = os.terminal_size((4, 2))
    first = model.apply(decoder.feed(encode_update(_keyframe()))[0], size)
    assert first is not None
    requested = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    patch = ScreenUpdate(
        UpdateKind.PATCH,
        2,
        4,
        2,
        1,
        0,
        True,
        (),
        requested,
    )
    applied = model.apply(decoder.feed(encode_update(patch))[0], size)
    assert applied is not None
    assert applied.changed_rows == ()

    stream = io.BytesIO()
    surface = TerminalSurface(stream)
    surface.paint(first)
    surface.paint(applied)
    surface.paint(applied)
    surface.close()

    rendered = stream.getvalue()
    assert rendered.count(b"\033[?2004h") == 1
    assert rendered.count(b"\033[?1004h") == 1
    assert rendered.count(b"\033[?2004l") == 1
    assert rendered.count(b"\033[?1004l") == 1
    assert rendered.index(b"\033[?2004l") < rendered.index(b"\033[?1049l")
    assert rendered.index(b"\033[?1004l") < rendered.index(b"\033[?1049l")


def test_ctrl_right_bracket_is_consumed_locally_with_trailing_data():
    forwarded, should_exit = split_local_escape(b"before" + LOCAL_ESCAPE + b"after")

    assert forwarded == b"before"
    assert should_exit is True
    assert split_local_escape(b"ordinary") == (b"ordinary", False)


def test_terminal_input_decoder_preserves_order_and_partial_sgr_mouse():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"key\x1b[") == [b"key"]
    parts = decoder.feed(b"<64;40;12Mtail\x1b[<65;40;12M")

    assert len(parts) == 3
    assert isinstance(parts[0], SgrMouseEvent)
    assert parts[0].wheel_direction == 1
    assert (parts[0].x, parts[0].y) == (40, 12)
    assert parts[1] == b"tail"
    assert isinstance(parts[2], SgrMouseEvent)
    assert parts[2].wheel_direction == -1


def test_terminal_input_decoder_releases_ambiguous_escape_after_short_timeout():
    decoder = TerminalInputDecoder()

    assert decoder.feed(b"\x1b") == []
    assert decoder.flush_pending(delay=0) == [b"\x1b"]
    assert decoder.flush_pending(delay=0) == []


def test_terminal_input_decoder_forwards_invalid_or_nonwheel_mouse_unchanged():
    decoder = TerminalInputDecoder()
    parts = decoder.feed(b"\x1b[<brokenM\x1b[<0;2;3M")

    assert parts[0] == b"\x1b[<brokenM"
    assert isinstance(parts[1], SgrMouseEvent)
    assert parts[1].wheel_direction == 0


def test_local_history_view_scrolls_cached_lines_and_restores_at_bottom():
    view = LocalHistoryView()
    prefetch_frame = view.begin_prefetch(1.0)
    prefetch_message = InputFrameDecoder().feed(prefetch_frame)[0]
    prefetch_id, max_lines = decode_history_prefetch(prefetch_message.data)
    assert max_lines == 300
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        x=30,
        y=2,
        width=40,
        height=3,
        lines=tuple(f"line-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    wheel_up = SgrMouseEvent(b"\x1b[<64;50;4M", 64, 50, 4, True)
    request = view.wheel(wheel_up)
    framed = InputFrameDecoder().feed(request.protocol_frame)[0]
    request_id, x, y, max_lines = decode_history_request(framed.data)
    assert (x, y, max_lines) == (50, 4, 2000)
    assert request.render_history is True
    assert view.overlays()[0][1] == (b"line-4", b"line-5", b"line-6")
    click = SgrMouseEvent(b"\x1b[<0;50;4M", 0, 50, 4, True)
    assert view.pointer_event(click, "%8").forwarded_input == b""
    assert view.active is True
    snapshot = HistorySnapshot(
        request_id,
        "%8",
        x=30,
        y=2,
        width=40,
        height=3,
        lines=tuple(f"line-{index}".encode() for index in range(10)),
    )

    accepted = view.accept(snapshot)

    assert accepted.render_history is True
    assert view.overlays()[0][1] == (b"line-4", b"line-5", b"line-6")
    wheel_down = SgrMouseEvent(b"\x1b[<65;50;4M", 65, 50, 4, True)
    restored = view.wheel(wheel_down)
    assert restored.restore_live is True
    assert view.active is False


def test_local_history_routes_sidebar_immediately_and_owns_agent_wheel():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    view.accept_prefetch(HistoryBatch(prefetch_id, (
        HistorySnapshot(
            prefetch_id, "%8", 30, 0, 40, 2, (b"old", b"one", b"two")
        ),
    )))

    sidebar = b"\x1b[<64;4;4M"
    sidebar_action = view.wheel(SgrMouseEvent(sidebar, 64, 4, 4, True))
    assert sidebar_action.forwarded_input == sidebar

    agent_down = b"\x1b[<65;40;1M"
    agent_action = view.wheel(SgrMouseEvent(agent_down, 65, 40, 1, True))
    assert agent_action.forwarded_input == b""
    assert agent_action.protocol_frame == b""


def test_local_history_cross_agent_click_preserves_prefetched_routes():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    first = HistorySnapshot(
        prefetch_id, "%8", 30, 0, 30, 3,
        tuple(f"a-{index}".encode() for index in range(8)),
    )
    second = HistorySnapshot(
        prefetch_id, "%9", 61, 0, 30, 3,
        tuple(f"b-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (first, second)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    outside_down = SgrMouseEvent(b"down-other", 0, 70, 2, True)
    action = view.pointer_event(outside_down)

    assert action.forwarded_input == b"down-other"
    assert action.restore_live is False
    assert action.refresh_routes is True
    assert view.active is True
    assert tuple(view.viewports) == ("%8",)
    assert view.visible_routes == (first, second)
    assert view.pointer_event(
        SgrMouseEvent(b"release-other", 0, 70, 2, False)
    ).forwarded_input == b"release-other"


def test_local_history_sidebar_click_invalidates_agent_routes():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id, "%8", 30, 0, 30, 3,
        tuple(f"line-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    action = view.pointer_event(SgrMouseEvent(b"sidebar", 0, 5, 2, True))

    assert action.forwarded_input == b"sidebar"
    assert action.restore_live is True
    assert action.refresh_routes is True
    assert view.visible_routes == ()
    assert view.pointer_event(
        SgrMouseEvent(b"sidebar-release", 0, 5, 2, False)
    ).forwarded_input == b"sidebar-release"
    assert view.pointer_event(
        SgrMouseEvent(b"stray-release", 0, 5, 2, False)
    ) == HistoryAction()


def test_local_history_captures_an_in_pane_pointer_gesture_until_release():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id, "%8", 30, 0, 30, 3,
        tuple(f"line-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))

    assert view.pointer_event(
        SgrMouseEvent(b"down", 0, 40, 2, True), "%8"
    ) == HistoryAction()
    assert view.pointer_event(
        SgrMouseEvent(b"wheel", 64, 40, 2, True), "%8"
    ) == HistoryAction()
    assert view.pointer_event(SgrMouseEvent(b"drag", 32, 5, 10, True)) == HistoryAction()
    assert view.pointer_event(SgrMouseEvent(b"up", 0, 5, 10, False)) == HistoryAction()
    assert view.active is True


def test_local_history_forwards_complete_click_to_another_frozen_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = (
        HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(prefetch_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    down = view.pointer_event(
        SgrMouseEvent(b"down-b", 0, 70, 2, True), "%8"
    )
    drag = view.pointer_event(
        SgrMouseEvent(b"drag-b", 32, 71, 2, True), "%8"
    )
    wheel = view.pointer_event(
        SgrMouseEvent(b"wheel-b", 64, 71, 2, True), "%8"
    )
    release = view.pointer_event(
        SgrMouseEvent(b"release-b", 0, 71, 2, False), "%8"
    )

    assert down == HistoryAction(
        forwarded_input=b"down-b", refresh_routes=True
    )
    assert drag.forwarded_input == b"drag-b"
    assert wheel.forwarded_input == b"wheel-b"
    assert release.forwarded_input == b"release-b"
    assert tuple(view.viewports) == ("%8", "%9")


def test_local_history_wheel_over_another_pane_preserves_both_viewports():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    first = HistorySnapshot(
        prefetch_id, "%8", 30, 0, 30, 3,
        tuple(f"a-{index}".encode() for index in range(8)),
    )
    second = HistorySnapshot(
        prefetch_id, "%9", 61, 0, 30, 3,
        tuple(f"b-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (first, second)))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    old_lines = view.overlays()[0][1]

    action = view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    assert old_lines == (b"a-2", b"a-3", b"a-4")
    assert action.restore_live is False
    assert action.render_history is True
    assert tuple(view.viewports) == ("%8", "%9")
    assert tuple(lines for _snapshot, lines in view.overlays()) == (
        (b"a-2", b"a-3", b"a-4"),
        (b"b-2", b"b-3", b"b-4"),
    )


def test_local_history_reaching_bottom_restores_only_that_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = tuple(
        HistorySnapshot(
            prefetch_id,
            pane_id,
            x,
            0,
            30,
            3,
            tuple(f"{pane_id}-{index}".encode() for index in range(8)),
        )
        for pane_id, x in (("%8", 30), ("%9", 61))
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    action = view.wheel(SgrMouseEvent(b"down-b", 65, 70, 2, True))

    assert action.restore_live is True
    assert tuple(view.viewports) == ("%8",)
    assert view.overlays()[0][1] == (b"%8-2", b"%8-3", b"%8-4")


def test_local_history_input_restores_only_its_routed_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshots = (
        HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(prefetch_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))

    assert view.cancel_for_input(70, 1) is True
    assert tuple(view.viewports) == ("%8",)
    assert view.cancel_for_input(40, 1) is True
    assert view.active is False


def test_local_history_input_outside_known_routes_restores_every_pane():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    snapshot = HistorySnapshot(
        prefetch_id, "%8", 30, 0, 30, 3, (b"0",) * 8
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (snapshot,)))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))

    assert view.cancel_for_input(5, 1) is True
    assert view.active is False


def test_deep_history_response_keeps_the_visible_anchor_when_output_advances():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached_lines = tuple(f"line-{index}".encode() for index in range(10))
    cached = HistorySnapshot(prefetch_id, "%8", 30, 0, 30, 3, cached_lines)
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    assert view.overlays()[0][1] == (b"line-4", b"line-5", b"line-6")

    deep = HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"older-0", b"older-1", *cached_lines, b"new-10", b"new-11"),
    )
    action = view.accept(deep)

    assert action.render_history is True
    assert view.overlays()[0][1] == (b"line-4", b"line-5", b"line-6")


def test_deep_history_response_without_anchor_does_not_jump_viewport():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"old-{index}".encode() for index in range(10)),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    before = view.overlays()

    action = view.accept(HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"different-{index}".encode() for index in range(20)),
    ))

    assert action == HistoryAction()
    assert view.overlays() == before


def test_deep_history_response_with_duplicate_anchor_does_not_jump_viewport():
    view = LocalHistoryView()
    prefetch = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    prefetch_id, _limit = decode_history_prefetch(prefetch.data)
    cached = HistorySnapshot(
        prefetch_id,
        "%8",
        30,
        0,
        30,
        3,
        (b"0", b"repeat-a", b"repeat-b", b"repeat-c", b"4", b"5", b"6"),
    )
    view.accept_prefetch(HistoryBatch(prefetch_id, (cached,)))
    request = view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    request_id = decode_history_request(
        InputFrameDecoder().feed(request.protocol_frame)[0].data
    )[0]
    before = view.overlays()
    assert before[0][1] == (b"repeat-a", b"repeat-b", b"repeat-c")

    action = view.accept(HistorySnapshot(
        request_id,
        "%8",
        30,
        0,
        30,
        3,
        (
            b"older",
            b"repeat-a",
            b"repeat-b",
            b"repeat-c",
            b"middle",
            b"repeat-a",
            b"repeat-b",
            b"repeat-c",
            b"newer",
        ),
    ))

    assert action == HistoryAction()
    assert view.overlays() == before


def test_prefetch_geometry_change_restores_only_incompatible_viewport():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    snapshots = (
        HistorySnapshot(first_id, "%8", 30, 0, 30, 3, (b"0",) * 8),
        HistorySnapshot(first_id, "%9", 61, 0, 30, 3, (b"1",) * 8),
    )
    view.accept_prefetch(HistoryBatch(first_id, snapshots))
    view.wheel(SgrMouseEvent(b"up-a", 64, 40, 2, True))
    view.wheel(SgrMouseEvent(b"up-b", 64, 70, 2, True))
    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)

    action = view.accept_prefetch(HistoryBatch(second_id, (
        HistorySnapshot(second_id, "%8", 30, 0, 30, 3, (b"new",) * 8),
        HistorySnapshot(second_id, "%9", 60, 0, 31, 3, (b"new",) * 8),
    )))

    assert action.restore_live is True
    assert tuple(view.viewports) == ("%8",)


def test_periodic_prefetch_never_moves_an_existing_frozen_viewport():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    cached = HistorySnapshot(
        first_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"old-{index}".encode() for index in range(8)),
    )
    view.accept_prefetch(HistoryBatch(first_id, (cached,)))
    view.wheel(SgrMouseEvent(b"up", 64, 40, 2, True))
    before = view.overlays()
    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)
    advanced = HistorySnapshot(
        second_id,
        "%8",
        30,
        0,
        30,
        3,
        tuple(f"new-{index}".encode() for index in range(8)),
    )

    action = view.accept_prefetch(HistoryBatch(second_id, (advanced,)))

    assert action == HistoryAction()
    assert view.overlays() == before
    assert view.content_cache["%8"] == advanced


def test_history_generations_reject_prefetch_and_deep_responses_after_invalidation():
    view = LocalHistoryView()
    first_request = InputFrameDecoder().feed(view.begin_prefetch(1.0))[0]
    first_id, _limit = decode_history_prefetch(first_request.data)
    view.invalidate_routes()
    stale = HistorySnapshot(first_id, "%8", 30, 0, 30, 2, (b"a", b"b", b"c"))
    view.accept_prefetch(HistoryBatch(first_id, (stale,)))
    assert view.visible_routes == ()

    second_request = InputFrameDecoder().feed(view.begin_prefetch(2.0))[0]
    second_id, _limit = decode_history_prefetch(second_request.data)
    current = HistorySnapshot(
        second_id, "%8", 30, 0, 30, 2, (b"a", b"b", b"c")
    )
    view.accept_prefetch(HistoryBatch(second_id, (current,)))
    deep = view.wheel(SgrMouseEvent(b"up", 64, 40, 1, True))
    deep_id = decode_history_request(
        InputFrameDecoder().feed(deep.protocol_frame)[0].data
    )[0]
    view.invalidate_routes()

    assert view.accept(HistorySnapshot(
        deep_id, "%8", 30, 0, 30, 2, (b"old", b"a", b"b", b"c")
    )) == HistoryAction()
    assert view.active is False


def test_history_content_cache_keeps_only_recent_pane_lifetimes():
    view = LocalHistoryView()
    pane_ids = []
    for index in range(12):
        request = InputFrameDecoder().feed(
            view.begin_prefetch(float(index + 1))
        )[0]
        request_id, _limit = decode_history_prefetch(request.data)
        pane_id = f"%{100 + index}"
        pane_ids.append(pane_id)
        snapshot = HistorySnapshot(
            request_id, pane_id, 30, 0, 30, 2, (b"a", b"b", b"c")
        )
        view.accept_prefetch(HistoryBatch(request_id, (snapshot,)))
        view.invalidate_routes()

    assert tuple(view.content_cache) == tuple(pane_ids[-8:])


def test_forwarded_wheel_burst_is_bounded_per_read_without_time_threshold():
    seen: set[int] = set()
    up = SgrMouseEvent(b"up", 64, 5, 5, True)
    down = SgrMouseEvent(b"down", 65, 5, 5, True)
    forwarded_up = HistoryAction(forwarded_input=b"up")

    assert coalesce_forwarded_wheel(forwarded_up, up, seen).forwarded_input == b"up"
    assert coalesce_forwarded_wheel(forwarded_up, up, seen).forwarded_input == b""
    assert coalesce_forwarded_wheel(
        HistoryAction(forwarded_input=b"down"), down, seen
    ).forwarded_input == b"down"
    assert coalesce_forwarded_wheel(HistoryAction(render_history=True), up, seen) == (
        HistoryAction(render_history=True)
    )


def test_only_bounded_layout_and_modal_keys_invalidate_live_routes():
    assert input_may_change_routes(b"\x1b[19~", routes_visible=True)
    assert input_may_change_routes(b"\x1b[20~", routes_visible=True)
    assert input_may_change_routes(b"?", routes_visible=True)
    assert input_may_change_routes(b"\x1b", routes_visible=False)
    assert input_may_change_routes(b"\r", routes_visible=False)
    assert not input_may_change_routes(b"ordinary input", routes_visible=True)
    assert not input_may_change_routes(b"\r", routes_visible=True)


def test_full_window_ssh_command_uses_railmux_remote_subcommand_and_protocol():
    argv = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        remote_command="railmux",
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-T", "-J", "jump", "server"]
    assert "then exec railmux remote-server" in argv[-1]
    assert f"--protocol {PROTOCOL_VERSION}" in argv[-1]
    assert "python3 -m railmux remote-server" in argv[-1]
    assert "--session 'rail mux'" in argv[-1]
    assert "--width 120 --height 40 --fps 20.0" in argv[-1]


def test_remote_install_command_uses_user_pip_then_matching_python_module():
    argv = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=("-J", "jump"),
    )

    assert argv[:5] == ["ssh", "-T", "-J", "jump", "server"]
    assert "python3 -m pip --version" in argv[-1]
    assert "python3 -m pip install --user --upgrade" in argv[-1]
    assert "'railmux[ssh]==1.2.3'" in argv[-1]
    assert "pip3 install --user --upgrade" in argv[-1]
    assert "&& exec python3 -m railmux remote-server" in argv[-1]
    assert "sudo" not in argv[-1]


def test_generated_remote_bootstrap_and_install_commands_are_posix_shell_syntax():
    bootstrap = build_ssh_argv(
        "server",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        remote_command="railmux",
        ssh_args=(),
    )[-1]
    installer = build_ssh_install_argv(
        "server",
        version="1.2.3",
        session="rail mux",
        width=120,
        height=40,
        fps=20.0,
        ssh_args=(),
    )[-1]

    for command in (bootstrap, installer):
        result = subprocess.run(
            ["/bin/sh", "-n", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()


def test_remote_install_help_is_exact_and_has_source_fallback():
    help_text = remote_install_help("server", "1.2.3")

    assert "python3 -m pip install --user --upgrade" in help_text
    assert "'railmux[ssh]==1.2.3'" in help_text
    assert "matching wheel or source checkout" in help_text


def test_remote_hello_is_strictly_bounded_and_typed():
    hello = parse_remote_hello(
        REMOTE_HELLO_PREFIX
        + b'{"protocol":6,"ready":true,"tmux":true,"version":"1.2.3"}\n'
    )

    assert hello == RemoteHello("1.2.3", 6, True)
    with pytest.raises(ValueError):
        parse_remote_hello(
            REMOTE_HELLO_PREFIX
            + b'{"protocol":true,"ready":true,"tmux":true,'
            b'"version":"1.2.3"}\n'
        )
    with pytest.raises(ValueError):
        parse_remote_hello(REMOTE_HELLO_PREFIX + b"not-json\n")


def test_remote_startup_wait_reads_hello_before_raw_mode():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        "b'RAILMUX-REMOTE/1 {\"protocol\":6,\"ready\":true,\"tmux\":true,"
        "\"version\":\"1.2.3\"}\\n'); "
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_tolerates_a_non_newline_shell_banner():
    script = (
        "import sys; "
        "sys.stdout.buffer.write("
        "b'banner: RAILMUX-REMOTE/1 {\"protocol\":6,\"ready\":true,"
        "\"tmux\":true,\"version\":\"1.2.3\"}\\n'); "
        "sys.stdout.buffer.flush()"
    )
    process = subprocess.Popen(
        [fast_display_client.sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    startup = await_remote_startup(process, timeout=2.0)
    process.wait(timeout=2.0)

    assert startup == RemoteStartup(
        RemoteStartKind.HELLO,
        RemoteHello("1.2.3", PROTOCOL_VERSION, True),
    )


def test_remote_startup_rejects_an_old_wire_protocol_without_timing_out():
    process = subprocess.Popen(
        [
            fast_display_client.sys.executable,
            "-c",
            "import sys,time;sys.stdout.buffer.write(b'RMUXD5\\0');"
            "sys.stdout.buffer.flush();time.sleep(5)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        started = time.monotonic()
        startup = await_remote_startup(process, timeout=2.0)

        assert startup == RemoteStartup(RemoteStartKind.FAILED)
        assert time.monotonic() - started < 1.0
    finally:
        process.terminate()
        process.wait(timeout=2.0)


class _PreflightProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode or 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_compatible_remote_is_confirmed_before_attach(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(fast_display_client.__version__, PROTOCOL_VERSION, True),
        ),
    )

    selected, initial = prepare_remote_process(
        args, os.terminal_size((120, 40))
    )

    assert selected is process
    assert initial == b""
    assert process.stdin.getvalue() == REMOTE_START


def test_missing_remote_prompts_then_installs_and_starts(monkeypatch):
    missing = _PreflightProcess(127)
    installed = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: missing
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(RemoteStartKind.MISSING, returncode=127),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            installed,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(
                    fast_display_client.__version__, PROTOCOL_VERSION, True
                ),
            ),
        ),
    )

    selected, _initial = prepare_remote_process(
        args, os.terminal_size((120, 40))
    )

    assert selected is installed
    assert installed.stdin.getvalue() == REMOTE_START


def test_missing_remote_decline_returns_copyable_install_help(
    monkeypatch,
):
    process = _PreflightProcess(127)
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(RemoteStartKind.MISSING, returncode=127),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: False)

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "python3 -m pip install --user" in str(exc.value)
    assert fast_display_client.__version__ in str(exc.value)


def test_remote_without_tmux_gives_system_package_guidance(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__, PROTOCOL_VERSION, True, False
            ),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError, match="tmux is not"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_newer_compatible_remote_prompts_for_local_upgrade_but_can_continue(
    monkeypatch, capsys,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    selected, _initial = prepare_remote_process(
        args, os.terminal_size((120, 40))
    )

    assert selected is process
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert process.stdin.getvalue() == REMOTE_START
    assert "continuing with local Railmux" in capsys.readouterr().err


def test_newer_remote_protocol_can_upgrade_and_restart_local_client(monkeypatch):
    process = _PreflightProcess()
    args = parse_client_args(["server", "--fps", "30"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION + 1, True),
        ),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)

    class Restarted(Exception):
        pass

    def restart(version, raw_args):
        assert version == "999.0"
        assert raw_args == ("server", "--fps", "30")
        raise Restarted

    monkeypatch.setattr(
        fast_display_client, "_upgrade_local_and_restart", restart
    )

    with pytest.raises(Restarted):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert process.terminated


def test_newer_protocol_with_non_newer_package_cannot_downgrade_local(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello(
                fast_display_client.__version__, PROTOCOL_VERSION + 1, True
            ),
        ),
    )
    confirm = MagicMock()
    monkeypatch.setattr(fast_display_client, "_confirm", confirm)

    with pytest.raises(
        fast_display_client.ProbeError, match="unsafe automatic local downgrade"
    ):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    confirm.assert_not_called()
    assert process.terminated


def test_local_upgrade_uses_current_python_user_site_and_restarts(monkeypatch):
    monkeypatch.setattr(fast_display_client.sys, "prefix", "/usr")
    monkeypatch.setattr(fast_display_client.sys, "base_prefix", "/usr")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(fast_display_client.subprocess, "run", run)

    class Restarted(Exception):
        pass

    observed = {}

    def execv(executable, argv):
        observed["executable"] = executable
        observed["argv"] = argv
        raise Restarted

    monkeypatch.setattr(fast_display_client.os, "execv", execv)

    with pytest.raises(Restarted):
        fast_display_client._upgrade_local_and_restart(
            "1.2.3", ("server", "--fps", "30")
        )

    install = run.call_args.args[0]
    assert install == [
        fast_display_client.sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--upgrade",
        "railmux==1.2.3",
    ]
    assert observed["argv"] == [
        fast_display_client.sys.executable,
        "-m",
        "railmux",
        "ssh",
        "server",
        "--fps",
        "30",
    ]


def test_older_remote_protocol_prompts_for_matching_remote_upgrade(monkeypatch):
    old = _PreflightProcess()
    upgraded = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: old
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("0.1.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or True,
    )
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            upgraded,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello(
                    fast_display_client.__version__, PROTOCOL_VERSION, True
                ),
            ),
        ),
    )

    selected, _initial = prepare_remote_process(
        args, os.terminal_size((120, 40))
    )

    assert selected is upgraded
    assert "uses older SSH protocol" in questions[0]
    assert old.terminated
    assert upgraded.stdin.getvalue() == REMOTE_START


def test_higher_remote_version_is_offered_to_local_before_protocol_direction(
    monkeypatch,
):
    process = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION - 1, True),
        ),
    )
    questions = []
    monkeypatch.setattr(
        fast_display_client,
        "_confirm",
        lambda question: questions.append(question) or False,
    )

    with pytest.raises(fast_display_client.ProbeError, match="newer remote"):
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "Remote Railmux 999.0 is newer than local" in questions[0]
    assert "Upgrade local Railmux to 999.0?" in questions[0]
    assert process.terminated


def test_declining_local_upgrade_does_not_downgrade_remote_dependency_repair(
    monkeypatch,
):
    process = _PreflightProcess()
    repaired = _PreflightProcess()
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: process
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(
            RemoteStartKind.HELLO,
            RemoteHello("999.0", PROTOCOL_VERSION, False),
        ),
    )
    answers = iter((False, True))
    monkeypatch.setattr(
        fast_display_client, "_confirm", lambda _question: next(answers)
    )
    installed_versions = []

    def install(_args, _size, version):
        installed_versions.append(version)
        return (
            repaired,
            RemoteStartup(
                RemoteStartKind.HELLO,
                RemoteHello("999.0", PROTOCOL_VERSION, True),
            ),
        )

    monkeypatch.setattr(
        fast_display_client, "_install_remote_and_start", install
    )

    selected, _initial = prepare_remote_process(
        args, os.terminal_size((120, 40))
    )

    assert selected is repaired
    assert installed_versions == ["999.0"]
    assert process.terminated
    assert repaired.stdin.getvalue() == REMOTE_START


def test_failed_remote_auto_install_returns_manual_recovery(monkeypatch):
    missing = _PreflightProcess(127)
    failed = _PreflightProcess(1)
    args = parse_client_args(["server"])
    monkeypatch.setattr(
        fast_display_client, "_spawn_remote", lambda _argv: missing
    )
    monkeypatch.setattr(
        fast_display_client,
        "await_remote_startup",
        lambda _process: RemoteStartup(RemoteStartKind.MISSING, returncode=127),
    )
    monkeypatch.setattr(fast_display_client, "_confirm", lambda _question: True)
    monkeypatch.setattr(
        fast_display_client,
        "_install_remote_and_start",
        lambda _args, _size, _version: (
            failed,
            RemoteStartup(RemoteStartKind.FAILED, returncode=1),
        ),
    )

    with pytest.raises(fast_display_client.ProbeError) as exc:
        prepare_remote_process(args, os.terminal_size((120, 40)))

    assert "automatic remote installation" in str(exc.value)
    assert "matching wheel or source checkout" in str(exc.value)


def test_remote_server_has_no_bare_tmux_server_argv():
    source = inspect.getsource(fast_display_server)

    assert '["tmux",' not in source
    assert "['tmux'," not in source


def test_remote_server_hello_reports_version_protocol_and_dependency(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(fast_display_server.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(
        fast_display_server.sys, "stdout", MagicMock(buffer=output)
    )

    fast_display_server._emit_remote_hello(True)

    hello = parse_remote_hello(output.getvalue())
    assert hello == RemoteHello(
        fast_display_client.__version__, PROTOCOL_VERSION, True
    )


def test_remote_server_waits_for_exact_start_confirmation(monkeypatch):
    remote_input = MagicMock(buffer=io.BytesIO(REMOTE_START))
    monkeypatch.setattr(fast_display_server.sys, "stdin", remote_input)
    monkeypatch.setattr(
        fast_display_server.select,
        "select",
        lambda *_args: ([remote_input.buffer], [], []),
    )

    assert fast_display_server._await_client_start() is True

    remote_input.buffer = io.BytesIO(b"wrong\n")
    assert fast_display_server._await_client_start() is False


def test_remote_server_missing_dependency_never_touches_tmux(monkeypatch):
    monkeypatch.setattr(
        fast_display_server, "_fast_dependency_ready", lambda: False
    )
    emit = MagicMock()
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", emit)
    socket_label = MagicMock()
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", socket_label
    )

    result = fast_display_server.main([
        "--protocol", str(PROTOCOL_VERSION),
        "--width", "80",
        "--height", "24",
    ])

    assert result == 2
    emit.assert_called_once_with(False)
    socket_label.assert_not_called()


def test_remote_server_attaches_only_after_start_confirmation(monkeypatch):
    monkeypatch.setattr(
        fast_display_server, "_fast_dependency_ready", lambda: True
    )
    monkeypatch.setattr(fast_display_server, "_emit_remote_hello", MagicMock())
    monkeypatch.setattr(
        fast_display_server, "_await_client_start", lambda: True
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server, "socket_label", lambda: "railmux"
    )
    serve = MagicMock(return_value=17)
    monkeypatch.setattr(fast_display_server, "serve", serve)

    result = fast_display_server.main([
        "--protocol", str(PROTOCOL_VERSION),
        "--session", "custom",
        "--width", "80",
        "--height", "24",
        "--fps", "30",
    ])

    assert result == 17
    serve.assert_called_once_with("custom", 80, 24, 30.0)


def test_server_starts_default_railmux_with_current_python(monkeypatch):
    identities = iter((None, "$7"))
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: next(identities)
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda session_id: "%9"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert fast_display_server._ensure_railmux_session("railmux") == "$7"
    assert calls[0][0][:7] == [
        "tmux", "-L", "railmux", "new-session", "-d", "-s", "railmux",
    ]
    assert "-m railmux --inside-tmux" in calls[0][0][-1]


def test_display_lock_is_scoped_by_socket_and_session(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fast_display_server.restart_state, "runtime_state_dir", lambda: tmp_path
    )
    sockets = iter(("/tmp/server-a/railmux", "/tmp/server-b/railmux"))
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: next(sockets)
    )

    first = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(first)
    second = fast_display_server._acquire_display_lock("$0")
    fast_display_server._release_display_lock(second)

    locks = sorted(path.name for path in tmp_path.glob("fast-display-*.lock"))
    assert len(locks) == 2
    assert all(name.endswith("-0.lock") for name in locks)


def test_server_does_not_auto_start_a_custom_missing_session(monkeypatch):
    monkeypatch.setattr(fast_display_server, "_try_session_id", lambda _session: None)

    with pytest.raises(fast_display_server.DisplayServerError, match="default"):
        fast_display_server._ensure_railmux_session("custom")


@pytest.mark.parametrize(
    ("resolved", "controller", "expected"),
    [
        (None, None, fast_display_server.RemoteExit.HARD_QUIT),
        ("$4", None, fast_display_server.RemoteExit.SOFT_QUIT),
        ("$4", "%8", fast_display_server.RemoteExit.DETACHED),
    ],
)
def test_server_classifies_remote_lifecycle(
    monkeypatch, resolved, controller, expected
):
    monkeypatch.setattr(
        fast_display_server, "_try_session_id", lambda _session: resolved
    )
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: controller
    )

    assert fast_display_server._classify_remote_exit("$4") is expected


def test_observed_hard_quit_requires_matching_clean_exit(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget(
        "/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server, "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    consume = MagicMock(return_value=True)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "consume_clean_exit", consume)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "record_incident", record)

    assert fast_display_server._classify_observed_exit(
        "$4", target) is fast_display_server.RemoteExit.HARD_QUIT
    consume.assert_called_once_with(server_pid=123, session_id="$4")
    record.assert_not_called()


def test_observed_unexpected_tmux_loss_records_incident(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget(
        "/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server, "_classify_remote_exit",
        lambda _session: fast_display_server.RemoteExit.HARD_QUIT,
    )
    monkeypatch.setattr(
        fast_display_server.tmux_health, "consume_clean_exit",
        lambda **_kwargs: False,
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "record_incident", record)

    with pytest.raises(
        fast_display_server.DisplayServerError,
        match="disappeared unexpectedly",
    ):
        fast_display_server._classify_observed_exit("$4", target)

    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-server-exit",
        consecutive_failures=1,
    )


@pytest.mark.parametrize(
    "exit_kind",
    [fast_display_server.RemoteExit.SOFT_QUIT,
     fast_display_server.RemoteExit.DETACHED],
)
def test_observed_surviving_session_does_not_consume_clean_exit(
    monkeypatch, exit_kind,
):
    target = fast_display_server.tmux_server.TmuxServerTarget(
        "/tmp/tmux/railmux", 123)
    monkeypatch.setattr(
        fast_display_server, "_classify_remote_exit",
        lambda _session: exit_kind,
    )
    consume = MagicMock()
    monkeypatch.setattr(
        fast_display_server.tmux_health, "consume_clean_exit", consume)

    assert fast_display_server._classify_observed_exit("$4", target) is exit_kind
    consume.assert_not_called()


def test_remote_watchdog_records_only_after_consecutive_failures(monkeypatch):
    watchdog = fast_display_server.tmux_health.FailureWatchdog.starting(
        0.0, interval=5.0, failure_limit=3
    )
    monkeypatch.setattr(
        fast_display_server, "_tmux_output", lambda *_args: ""
    )
    record = MagicMock(return_value=True)
    monkeypatch.setattr(
        fast_display_server.tmux_health, "record_incident", record
    )

    assert not fast_display_server._remote_watchdog_tripped(
        watchdog, "$4", 123, 5.0
    )
    assert not fast_display_server._remote_watchdog_tripped(
        watchdog, "$4", 123, 10.0
    )
    assert fast_display_server._remote_watchdog_tripped(
        watchdog, "$4", 123, 15.0
    )
    record.assert_called_once_with(
        component="remote-display",
        reason="remote-display-watchdog-timeout",
        consecutive_failures=3,
    )


def test_server_resolves_only_noncontroller_pane_under_pointer(monkeypatch):
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1"
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "$4\t0\t1\t%1\t0\t0\t30\t20\t\n"
            "$4\t0\t0\t%8\t31\t0\t49\t20\t\n"
        ),
    )

    assert fast_display_server._pane_at_pointer("$4", 5, 5) is None
    pane = fast_display_server._pane_at_pointer("$4", 40, 5)
    assert pane == fast_display_server._PaneGeometry("%8", 31, 0, 49, 20)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            "$4\t1\t1\t%1\t0\t0\t80\t24\t\n"
            "$4\t1\t0\t%8\t31\t0\t49\t20\t\n",
            (),
        ),
        (
            "$4\t1\t0\t%1\t0\t0\t30\t20\t\n"
            "$4\t1\t1\t%8\t0\t0\t80\t24\t\n",
            (fast_display_server._PaneGeometry("%8", 0, 0, 80, 24),),
        ),
        (
            "$4\t1\t1\t%1\t0\t0\t80\t24\t\n"
            "$4\t0\t0\t%8\t31\t0\t49\t20\t\n",
            (),
        ),
    ],
)
def test_server_exposes_only_coherent_visible_panes_when_zoomed(
    monkeypatch, rows, expected
):
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1"
    )
    monkeypatch.setattr(
        subprocess, "check_output", lambda *args, **kwargs: rows
    )

    assert fast_display_server._list_agent_panes("$4") == expected


def test_server_maps_nested_history_to_exact_real_pane(monkeypatch):
    target = fast_display_server.tmux_server.TmuxServerTarget(
        "/tmp/default", 44)
    monkeypatch.setattr(
        fast_display_server, "_live_controller", lambda _session: "%1")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            '$4\t0\t1\t%1\t0\t0\t30\t20\t\n'
            '$4\t0\t0\t%8\t31\t0\t49\t20\t{"source":1}\n'
        ),
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "resolve_history_source",
        lambda marker, **_kwargs: (target, "$7") if marker else None,
    )
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_single_pane_id",
        lambda candidate, session, **_kwargs: (
            "%2" if (candidate, session) == (target, "$7") else None
        ),
    )

    assert fast_display_server._list_agent_panes("$4") == (
        fast_display_server._PaneGeometry(
            "%8", 31, 0, 49, 20, target, "%2"),
    )


def test_server_history_capture_preserves_sgr_but_filters_controls(monkeypatch):
    pane = fast_display_server._PaneGeometry("%8", 31, 0, 49, 2)
    monkeypatch.setattr(
        fast_display_server, "_pane_at_pointer", lambda *args: pane
    )
    calls = []

    def fake_check_output(argv, **kwargs):
        calls.append((argv, kwargs))
        return b"old\n\x1b[31mred\x1b[0m\n\x1b]52;c;evil\x07visible\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    snapshot = fast_display_server.capture_history_snapshot(
        "$4", 7, 40, 5, 2000
    )

    assert snapshot.pane_id == "%8"
    assert b"old" in snapshot.lines[0]
    assert b"red" in snapshot.lines[1]
    assert b";31;" in snapshot.lines[1]
    assert b"]52" not in snapshot.lines[2]
    assert b"visible" in snapshot.lines[2]
    assert calls[0][0] == [
        "tmux", "-L", "railmux", "capture-pane", "-p", "-e", "-t", "%8",
        "-S", "-2000",
    ]
    assert not any(
        destructive in calls[0][0]
        for destructive in ("kill-pane", "kill-session", "resize-pane", "send-keys")
    )


def test_server_captures_nested_history_from_real_pane_without_resizing(
    monkeypatch,
):
    target = fast_display_server.tmux_server.TmuxServerTarget(
        "/tmp/default", 44)
    pane = fast_display_server._PaneGeometry(
        "%8", 31, 0, 49, 2, target, "%2")
    monkeypatch.setattr(
        fast_display_server, "_pane_at_pointer", lambda *_args: pane)
    monkeypatch.setattr(
        fast_display_server.tmux_server,
        "target_is_live",
        lambda candidate, **_kwargs: candidate == target,
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_kwargs: calls.append(argv) or b"old\nnew\n",
    )

    snapshot = fast_display_server.capture_history_snapshot(
        "$4", 7, 40, 5, 300)

    assert snapshot.pane_id == "%8"
    assert calls == [[
        "tmux", "-S", "/tmp/default", "capture-pane", "-p", "-e",
        "-t", "%2", "-S", "-300",
    ]]
    assert not any(
        item in calls[0]
        for item in ("resize-pane", "swap-pane", "send-keys", "kill-pane")
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--protocol", "6", "--width", "39", "--height", "24"],
        ["--protocol", "6", "--width", "80", "--height", "11"],
        ["--protocol", "6", "--width", "80", "--height", "24", "--fps", "61"],
        ["--protocol", "5", "--width", "80", "--height", "24"],
    ],
)
def test_server_rejects_unbounded_geometry_and_frame_rates(argv):
    with pytest.raises(SystemExit):
        parse_server_args(argv)


@dataclass(frozen=True)
class _Char:
    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


class _FakeScreen:
    lines = 1
    columns = 4
    buffer = {
        0: {
            0: _Char("A", fg="red", bold=True),
            1: _Char("你"),
            2: _Char(""),
            3: _Char("\x1b\x9b"),
        }
    }
    mode = {2004 << 5, 1004 << 5}


def test_server_renderer_preserves_wide_cells_and_filters_terminal_controls():
    rows = render_rows(_FakeScreen())

    assert len(rows) == 1
    rendered = rows[0]
    assert b"A" in rendered
    assert "你".encode() in rendered
    assert rendered.count("你".encode()) == 1
    assert b"\x1b\x1b" not in rendered
    assert "\x9b".encode() not in rendered
    assert "�".encode() in rendered
    assert rendered.endswith(b"\033[0m")


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (b"\033[2S", ["11111", "44444", "     ", "     ", "55555"]),
        (b"\033[2T", ["11111", "     ", "     ", "22222", "55555"]),
    ],
)
def test_server_terminal_model_applies_parameterized_scroll_inside_margins(
    operation,
    expected,
):
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    screen = terminal.Screen(5, 5)
    stream = terminal.ByteStream(screen)
    for row, value in enumerate(b"12345", 1):
        stream.feed(f"\033[{row};1H".encode() + bytes((value,)) * 5)

    # Restrict scrolling to rows 2-4 and keep the cursor outside that region.
    # SU/SD operate on DECSTBM regardless of cursor position and must not move
    # the cursor; pyte 0.8.2 silently ignored both sequences.
    stream.feed(b"\033[2;4r\033[5;3H")
    screen.dirty.clear()
    stream.feed(operation)

    assert screen.display == expected
    assert (screen.cursor.x, screen.cursor.y) == (2, 4)
    assert screen.dirty == {1, 2, 3}


def test_server_terminal_model_repeats_character_with_current_style():
    pyte = pytest.importorskip("pyte")
    terminal = fast_display_server._extended_pyte(pyte)
    with pytest.warns(DeprecationWarning):
        screen = terminal.DiffScreen(8, 1)
    stream = terminal.ByteStream(screen)

    stream.feed(b"\033[31m#\033[4b")

    assert screen.display == ["#####   "]
    assert [screen.buffer[0][column].fg for column in range(5)] == ["red"] * 5


def test_server_history_renderer_uses_extended_terminal_sequences():
    pyte = pytest.importorskip("pyte")
    rendered = fast_display_server._render_history_line(
        fast_display_server._extended_pyte(pyte),
        b"\033[31m#\033[4b\033[0m",
        8,
    )

    assert rendered.count(b"#") == 5
    assert b";31;" in rendered


def test_server_projects_only_allowlisted_private_terminal_modes():
    assert terminal_modes_for_screen(_FakeScreen()) == (
        TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS
    )

    class OtherModes:
        mode = {1000 << 5, 1006 << 5, 9999 << 5}

    assert terminal_modes_for_screen(OtherModes()) is TerminalMode.NONE
