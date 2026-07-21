"""Private v6 framing for the coalesced full-window SSH display."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag


DISPLAY_MAGIC = b"RMUXD6\x00"
INPUT_MAGIC = b"RMUXK6\x00"
PROTOCOL_VERSION = 6
LENGTH_BYTES = 4
REMOTE_HELLO_PREFIX = b"RAILMUX-REMOTE/1 "
REMOTE_START = b"RAILMUX-START/1\n"
MAX_WIRE_BYTES = 16 * 1024 * 1024
MAX_SCREEN_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_WIDTH = 1000
MAX_HEIGHT = 500
MAX_HISTORY_LINES = 4096
MAX_HISTORY_PANES = 8
_UPDATE_METADATA = struct.Struct(">BIHHHHBHI")
_HISTORY_METADATA = struct.Struct(">IIHHHHI")
_HISTORY_REQUEST = struct.Struct(">IHHH")
_HISTORY_BATCH_METADATA = struct.Struct(">II")
_HISTORY_PANE_METADATA = struct.Struct(">IHHHH")
_PREFETCH_HISTORY_REQUEST = struct.Struct(">IH")


class UpdateKind(IntEnum):
    KEYFRAME = 1
    PATCH = 2


class OutputKind(IntEnum):
    SCREEN = 1
    HISTORY = 2
    HISTORY_BATCH = 3


class InputKind(IntEnum):
    BYTES = 1
    RESIZE = 2
    REQUEST_KEYFRAME = 3
    REQUEST_HISTORY = 4
    PREFETCH_HISTORY = 5


class TerminalMode(IntFlag):
    """Allowlisted modes the outer terminal must mirror for correct input."""

    NONE = 0
    BRACKETED_PASTE = 1 << 0
    FOCUS_EVENTS = 1 << 1


KNOWN_TERMINAL_MODES = TerminalMode.BRACKETED_PASTE | TerminalMode.FOCUS_EVENTS


class RemoteExit(IntEnum):
    """Stable process exit codes understood by the matching local client."""

    DETACHED = 10
    SOFT_QUIT = 11
    HARD_QUIT = 12


@dataclass(frozen=True)
class ScreenUpdate:
    """One keyframe or row patch in a monotonically sequenced screen stream."""

    kind: UpdateKind
    sequence: int
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    rows: tuple[tuple[int, bytes], ...]
    terminal_modes: TerminalMode = TerminalMode.NONE


@dataclass(frozen=True)
class HistorySnapshot:
    """A bounded, read-only pane history response for a local viewport."""

    request_id: int
    pane_id: str | None
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    lines: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class HistoryBatch:
    """An atomic hot-cache refresh for every visible non-controller pane."""

    request_id: int
    snapshots: tuple[HistorySnapshot, ...] = ()


@dataclass(frozen=True)
class InputMessage:
    kind: InputKind
    data: bytes = b""


def _pack_rows(update: ScreenUpdate) -> bytes:
    if update.kind is UpdateKind.KEYFRAME:
        if len(update.rows) != update.height:
            raise ValueError("a keyframe must contain every row")
        expected = tuple(range(update.height))
        if tuple(index for index, _row in update.rows) != expected:
            raise ValueError("keyframe rows must be complete and ordered")
    elif len(update.rows) > update.height:
        raise ValueError("a patch contains too many rows")

    body = [struct.pack(">H", len(update.rows))]
    seen: set[int] = set()
    total = 2
    for index, row in update.rows:
        if index in seen or not 0 <= index < update.height:
            raise ValueError("invalid or duplicate screen row")
        seen.add(index)
        total += 6 + len(row)
        if total > MAX_SCREEN_BYTES:
            raise ValueError("screen update is too large")
        body.extend((struct.pack(">HI", index, len(row)), row))
    return b"".join(body)


def encode_update(update: ScreenUpdate) -> bytes:
    if not 1 <= update.width <= MAX_WIDTH or not 1 <= update.height <= MAX_HEIGHT:
        raise ValueError("invalid screen geometry")
    if not 0 <= update.sequence <= 0xFFFFFFFF:
        raise ValueError("invalid screen sequence")
    if int(update.terminal_modes) & ~int(KNOWN_TERMINAL_MODES):
        raise ValueError("unknown terminal mode")
    raw_rows = _pack_rows(update)
    compressed = zlib.compress(raw_rows, level=3)
    metadata = _UPDATE_METADATA.pack(
        int(update.kind),
        update.sequence,
        update.width,
        update.height,
        min(update.cursor_x, update.width - 1),
        min(update.cursor_y, update.height - 1),
        int(update.cursor_visible),
        int(update.terminal_modes),
        len(raw_rows),
    )
    payload = bytes((int(OutputKind.SCREEN),)) + metadata + compressed
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError("compressed screen update is too large")
    return DISPLAY_MAGIC + struct.pack(">I", len(payload)) + payload


def _pack_history_lines(lines: tuple[bytes, ...]) -> bytes:
    if len(lines) > MAX_HISTORY_LINES:
        raise ValueError("too many history lines")
    body = [struct.pack(">H", len(lines))]
    total = 2
    for line in lines:
        total += 4 + len(line)
        if total > MAX_SCREEN_BYTES:
            raise ValueError("history snapshot is too large")
        body.extend((struct.pack(">I", len(line)), line))
    return b"".join(body)


def encode_history_snapshot(snapshot: HistorySnapshot) -> bytes:
    if not 0 <= snapshot.request_id <= 0xFFFFFFFF:
        raise ValueError("invalid history request identity")
    if snapshot.pane_id is None:
        if any((snapshot.x, snapshot.y, snapshot.width, snapshot.height, snapshot.lines)):
            raise ValueError("a rejected history response must be empty")
        pane_number = 0
    else:
        if not snapshot.pane_id.startswith("%") or not snapshot.pane_id[1:].isdigit():
            raise ValueError("invalid history pane identity")
        pane_number = int(snapshot.pane_id[1:])
        if not 0 < pane_number <= 0xFFFFFFFF:
            raise ValueError("invalid history pane identity")
        if not 1 <= snapshot.width <= MAX_WIDTH or not 1 <= snapshot.height <= MAX_HEIGHT:
            raise ValueError("invalid history geometry")
        if not 0 <= snapshot.x < MAX_WIDTH or not 0 <= snapshot.y < MAX_HEIGHT:
            raise ValueError("invalid history position")
        if len(snapshot.lines) < snapshot.height:
            raise ValueError("history snapshot is shorter than its viewport")
    raw_lines = _pack_history_lines(snapshot.lines)
    compressed = zlib.compress(raw_lines, level=3)
    metadata = _HISTORY_METADATA.pack(
        snapshot.request_id,
        pane_number,
        snapshot.x,
        snapshot.y,
        snapshot.width,
        snapshot.height,
        len(raw_lines),
    )
    payload = bytes((int(OutputKind.HISTORY),)) + metadata + compressed
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError("compressed history snapshot is too large")
    return DISPLAY_MAGIC + struct.pack(">I", len(payload)) + payload


def encode_history_batch(batch: HistoryBatch) -> bytes:
    if not 0 <= batch.request_id <= 0xFFFFFFFF:
        raise ValueError("invalid history request identity")
    if len(batch.snapshots) > MAX_HISTORY_PANES:
        raise ValueError("too many history panes")
    raw_parts = [struct.pack(">H", len(batch.snapshots))]
    total = 2
    seen: set[str] = set()
    for snapshot in batch.snapshots:
        if snapshot.request_id != batch.request_id or snapshot.pane_id is None:
            raise ValueError("invalid batched history snapshot")
        if snapshot.pane_id in seen:
            raise ValueError("duplicate history pane")
        seen.add(snapshot.pane_id)
        if not snapshot.pane_id.startswith("%") or not snapshot.pane_id[1:].isdigit():
            raise ValueError("invalid history pane identity")
        pane_number = int(snapshot.pane_id[1:])
        if not 0 < pane_number <= 0xFFFFFFFF:
            raise ValueError("invalid history pane identity")
        if not 1 <= snapshot.width <= MAX_WIDTH or not 1 <= snapshot.height <= MAX_HEIGHT:
            raise ValueError("invalid history geometry")
        if not 0 <= snapshot.x < MAX_WIDTH or not 0 <= snapshot.y < MAX_HEIGHT:
            raise ValueError("invalid history position")
        if len(snapshot.lines) < snapshot.height:
            raise ValueError("history snapshot is shorter than its viewport")
        packed_lines = _pack_history_lines(snapshot.lines)
        pane_metadata = _HISTORY_PANE_METADATA.pack(
            pane_number,
            snapshot.x,
            snapshot.y,
            snapshot.width,
            snapshot.height,
        )
        total += len(pane_metadata) + len(packed_lines)
        if total > MAX_SCREEN_BYTES:
            raise ValueError("history batch is too large")
        raw_parts.extend((pane_metadata, packed_lines))
    raw = b"".join(raw_parts)
    compressed = zlib.compress(raw, level=3)
    metadata = _HISTORY_BATCH_METADATA.pack(batch.request_id, len(raw))
    payload = bytes((int(OutputKind.HISTORY_BATCH),)) + metadata + compressed
    if len(payload) > MAX_WIRE_BYTES:
        raise ValueError("compressed history batch is too large")
    return DISPLAY_MAGIC + struct.pack(">I", len(payload)) + payload


def _decompress_rows(data: bytes, expected_size: int) -> bytes:
    if not 0 <= expected_size <= MAX_SCREEN_BYTES:
        raise ValueError("invalid decompressed screen size")
    inflater = zlib.decompressobj()
    raw = inflater.decompress(data, expected_size + 1)
    if (
        len(raw) != expected_size
        or inflater.unconsumed_tail
        or not inflater.eof
        or inflater.unused_data
    ):
        raise ValueError("invalid compressed screen update")
    return raw


def _unpack_rows(raw: bytes, height: int) -> tuple[tuple[int, bytes], ...]:
    if len(raw) < 2:
        raise ValueError("truncated screen rows")
    count = struct.unpack(">H", raw[:2])[0]
    if count > height:
        raise ValueError("too many screen rows")
    offset = 2
    rows: list[tuple[int, bytes]] = []
    seen: set[int] = set()
    for _ in range(count):
        if len(raw) - offset < 6:
            raise ValueError("truncated screen row header")
        index, size = struct.unpack(">HI", raw[offset:offset + 6])
        offset += 6
        if index >= height or index in seen or size > len(raw) - offset:
            raise ValueError("invalid screen row")
        seen.add(index)
        rows.append((index, raw[offset:offset + size]))
        offset += size
    if offset != len(raw):
        raise ValueError("trailing screen row data")
    return tuple(rows)


def _unpack_history_lines(raw: bytes) -> tuple[bytes, ...]:
    lines, offset = _unpack_history_lines_at(raw, 0)
    if offset != len(raw):
        raise ValueError("trailing history line data")
    return lines


def _unpack_history_lines_at(
    raw: bytes, offset: int,
) -> tuple[tuple[bytes, ...], int]:
    if len(raw) - offset < 2:
        raise ValueError("truncated history lines")
    count = struct.unpack(">H", raw[offset:offset + 2])[0]
    if count > MAX_HISTORY_LINES:
        raise ValueError("too many history lines")
    offset += 2
    lines: list[bytes] = []
    for _ in range(count):
        if len(raw) - offset < 4:
            raise ValueError("truncated history line header")
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        offset += 4
        if size > len(raw) - offset:
            raise ValueError("invalid history line")
        lines.append(raw[offset:offset + size])
        offset += size
    return tuple(lines), offset


class ServerMessageDecoder:
    """Decode bounded screen/history messages while tolerating an SSH banner."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(
        self, data: bytes,
    ) -> list[ScreenUpdate | HistorySnapshot | HistoryBatch]:
        self._buffer.extend(data)
        messages: list[ScreenUpdate | HistorySnapshot | HistoryBatch] = []
        header_size = len(DISPLAY_MAGIC) + LENGTH_BYTES
        while True:
            marker = self._buffer.find(DISPLAY_MAGIC)
            if marker < 0:
                keep = min(len(self._buffer), len(DISPLAY_MAGIC) - 1)
                if len(self._buffer) > keep:
                    del self._buffer[:-keep]
                return messages
            if marker:
                del self._buffer[:marker]
            if len(self._buffer) < header_size:
                return messages
            payload_size = struct.unpack(
                ">I", self._buffer[len(DISPLAY_MAGIC):header_size]
            )[0]
            if not 1 <= payload_size <= MAX_WIRE_BYTES:
                del self._buffer[0]
                continue
            packet_size = header_size + payload_size
            if len(self._buffer) < packet_size:
                return messages
            payload = bytes(self._buffer[header_size:packet_size])
            del self._buffer[:packet_size]
            try:
                output_kind = OutputKind(payload[0])
                body = payload[1:]
                if output_kind is OutputKind.HISTORY:
                    if len(body) < _HISTORY_METADATA.size:
                        raise ValueError("truncated history metadata")
                    request_id, pane_number, x, y, width, height, raw_size = (
                        _HISTORY_METADATA.unpack(body[:_HISTORY_METADATA.size])
                    )
                    raw_lines = _decompress_rows(
                        body[_HISTORY_METADATA.size:], raw_size
                    )
                    lines = _unpack_history_lines(raw_lines)
                    if pane_number == 0:
                        if any((x, y, width, height, lines)):
                            raise ValueError("invalid rejected history response")
                        pane_id = None
                    else:
                        if not 1 <= width <= MAX_WIDTH or not 1 <= height <= MAX_HEIGHT:
                            raise ValueError("invalid history geometry")
                        if not 0 <= x < MAX_WIDTH or not 0 <= y < MAX_HEIGHT:
                            raise ValueError("invalid history position")
                        if len(lines) < height:
                            raise ValueError("incomplete history viewport")
                        pane_id = f"%{pane_number}"
                    messages.append(HistorySnapshot(
                        request_id=request_id,
                        pane_id=pane_id,
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        lines=lines,
                    ))
                    continue
                if output_kind is OutputKind.HISTORY_BATCH:
                    if len(body) < _HISTORY_BATCH_METADATA.size:
                        raise ValueError("truncated history batch metadata")
                    request_id, raw_size = _HISTORY_BATCH_METADATA.unpack(
                        body[:_HISTORY_BATCH_METADATA.size]
                    )
                    raw = _decompress_rows(
                        body[_HISTORY_BATCH_METADATA.size:], raw_size
                    )
                    if len(raw) < 2:
                        raise ValueError("truncated history batch")
                    pane_count = struct.unpack(">H", raw[:2])[0]
                    if pane_count > MAX_HISTORY_PANES:
                        raise ValueError("too many history panes")
                    offset = 2
                    snapshots: list[HistorySnapshot] = []
                    seen: set[int] = set()
                    for _ in range(pane_count):
                        if len(raw) - offset < _HISTORY_PANE_METADATA.size:
                            raise ValueError("truncated history pane metadata")
                        pane_number, x, y, width, height = _HISTORY_PANE_METADATA.unpack(
                            raw[offset:offset + _HISTORY_PANE_METADATA.size]
                        )
                        offset += _HISTORY_PANE_METADATA.size
                        if pane_number == 0 or pane_number in seen:
                            raise ValueError("invalid history pane identity")
                        seen.add(pane_number)
                        if not 1 <= width <= MAX_WIDTH or not 1 <= height <= MAX_HEIGHT:
                            raise ValueError("invalid history geometry")
                        if not 0 <= x < MAX_WIDTH or not 0 <= y < MAX_HEIGHT:
                            raise ValueError("invalid history position")
                        lines, offset = _unpack_history_lines_at(raw, offset)
                        if len(lines) < height:
                            raise ValueError("incomplete history viewport")
                        snapshots.append(HistorySnapshot(
                            request_id=request_id,
                            pane_id=f"%{pane_number}",
                            x=x,
                            y=y,
                            width=width,
                            height=height,
                            lines=lines,
                        ))
                    if offset != len(raw):
                        raise ValueError("trailing history batch data")
                    messages.append(HistoryBatch(request_id, tuple(snapshots)))
                    continue
                if len(body) < _UPDATE_METADATA.size:
                    raise ValueError("truncated screen metadata")
                (
                    raw_kind,
                    sequence,
                    width,
                    height,
                    cursor_x,
                    cursor_y,
                    visible,
                    raw_modes,
                    raw_size,
                ) = _UPDATE_METADATA.unpack(body[:_UPDATE_METADATA.size])
                kind = UpdateKind(raw_kind)
                if not 1 <= width <= MAX_WIDTH or not 1 <= height <= MAX_HEIGHT:
                    raise ValueError("invalid screen geometry")
                if raw_modes & ~int(KNOWN_TERMINAL_MODES):
                    raise ValueError("unknown terminal mode")
                terminal_modes = TerminalMode(raw_modes)
                raw_rows = _decompress_rows(body[_UPDATE_METADATA.size:], raw_size)
                rows = _unpack_rows(raw_rows, height)
                if kind is UpdateKind.KEYFRAME and (
                    len(rows) != height
                    or tuple(index for index, _row in rows) != tuple(range(height))
                ):
                    raise ValueError("incomplete keyframe")
            except (ValueError, struct.error, zlib.error):
                # The packet is already length-delimited. Drop it rather than
                # exposing untrusted compressed or malformed row data.
                continue
            messages.append(ScreenUpdate(
                kind=kind,
                sequence=sequence,
                width=width,
                height=height,
                cursor_x=min(cursor_x, width - 1),
                cursor_y=min(cursor_y, height - 1),
                cursor_visible=bool(visible),
                rows=rows,
                terminal_modes=terminal_modes,
            ))


class ScreenUpdateDecoder:
    """Compatibility view which ignores v6 history response messages."""

    def __init__(self) -> None:
        self._decoder = ServerMessageDecoder()

    def feed(self, data: bytes) -> list[ScreenUpdate]:
        return [
            message
            for message in self._decoder.feed(data)
            if isinstance(message, ScreenUpdate)
        ]


def _encode_input_message(kind: InputKind, data: bytes) -> bytes:
    payload = bytes((int(kind),)) + data
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("input message is too large")
    return INPUT_MAGIC + struct.pack(">I", len(payload)) + payload


def encode_input(data: bytes) -> bytes:
    if not data or len(data) >= MAX_INPUT_BYTES:
        raise ValueError("input frame must contain 1 to 65534 bytes")
    return _encode_input_message(InputKind.BYTES, data)


def encode_resize(width: int, height: int) -> bytes:
    if not 40 <= width <= MAX_WIDTH or not 12 <= height <= MAX_HEIGHT:
        raise ValueError("invalid terminal geometry")
    return _encode_input_message(InputKind.RESIZE, struct.pack(">HH", width, height))


def encode_keyframe_request() -> bytes:
    return _encode_input_message(InputKind.REQUEST_KEYFRAME, b"")


def encode_history_request(
    request_id: int, x: int, y: int, max_lines: int = 2000,
) -> bytes:
    if not 0 <= request_id <= 0xFFFFFFFF:
        raise ValueError("invalid history request identity")
    if not 1 <= x <= MAX_WIDTH or not 1 <= y <= MAX_HEIGHT:
        raise ValueError("invalid history pointer position")
    if not 1 <= max_lines <= MAX_HISTORY_LINES:
        raise ValueError("invalid history line limit")
    return _encode_input_message(
        InputKind.REQUEST_HISTORY,
        _HISTORY_REQUEST.pack(request_id, x, y, max_lines),
    )


def encode_history_prefetch(request_id: int, max_lines: int = 300) -> bytes:
    if not 0 <= request_id <= 0xFFFFFFFF:
        raise ValueError("invalid history request identity")
    if not 1 <= max_lines <= MAX_HISTORY_LINES:
        raise ValueError("invalid history line limit")
    return _encode_input_message(
        InputKind.PREFETCH_HISTORY,
        _PREFETCH_HISTORY_REQUEST.pack(request_id, max_lines),
    )


def decode_history_request(data: bytes) -> tuple[int, int, int, int]:
    if len(data) != _HISTORY_REQUEST.size:
        raise ValueError("invalid history request")
    request_id, x, y, max_lines = _HISTORY_REQUEST.unpack(data)
    if not 1 <= x <= MAX_WIDTH or not 1 <= y <= MAX_HEIGHT:
        raise ValueError("invalid history pointer position")
    if not 1 <= max_lines <= MAX_HISTORY_LINES:
        raise ValueError("invalid history line limit")
    return request_id, x, y, max_lines


def decode_history_prefetch(data: bytes) -> tuple[int, int]:
    if len(data) != _PREFETCH_HISTORY_REQUEST.size:
        raise ValueError("invalid history prefetch request")
    request_id, max_lines = _PREFETCH_HISTORY_REQUEST.unpack(data)
    if not 1 <= max_lines <= MAX_HISTORY_LINES:
        raise ValueError("invalid history line limit")
    return request_id, max_lines


class InputFrameDecoder:
    """Decode bounded input messages from the SSH stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[InputMessage]:
        self._buffer.extend(data)
        messages: list[InputMessage] = []
        header_size = len(INPUT_MAGIC) + LENGTH_BYTES
        while True:
            marker = self._buffer.find(INPUT_MAGIC)
            if marker < 0:
                keep = min(len(self._buffer), len(INPUT_MAGIC) - 1)
                if len(self._buffer) > keep:
                    del self._buffer[:-keep]
                return messages
            if marker:
                del self._buffer[:marker]
            if len(self._buffer) < header_size:
                return messages
            payload_size = struct.unpack(
                ">I", self._buffer[len(INPUT_MAGIC):header_size]
            )[0]
            if not 1 <= payload_size <= MAX_INPUT_BYTES:
                del self._buffer[0]
                continue
            packet_size = header_size + payload_size
            if len(self._buffer) < packet_size:
                return messages
            payload = bytes(self._buffer[header_size:packet_size])
            del self._buffer[:packet_size]
            try:
                kind = InputKind(payload[0])
            except ValueError:
                continue
            message_data = payload[1:]
            if (
                (kind is InputKind.BYTES and not message_data)
                or (kind is InputKind.RESIZE and len(message_data) != 4)
                or (kind is InputKind.REQUEST_KEYFRAME and message_data)
                or (
                    kind is InputKind.REQUEST_HISTORY
                    and len(message_data) != _HISTORY_REQUEST.size
                )
                or (
                    kind is InputKind.PREFETCH_HISTORY
                    and len(message_data) != _PREFETCH_HISTORY_REQUEST.size
                )
            ):
                continue
            messages.append(InputMessage(kind, message_data))
