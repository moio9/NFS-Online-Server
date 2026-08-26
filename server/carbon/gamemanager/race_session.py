"""Carbon race-session and countdown wire helpers.

These helpers intentionally contain no sockets or directory state.  They encode
only the non-GameManager OLMSG/session bodies and the capture-proven HostProps
transitions used by the Carbon rebroadcaster.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
from typing import Mapping

from carbon.gamemanager.protocol import (
    GMMessageType,
    LOGICAL_PREFIX,
    OLMessageType,
    PLAIN_TERMINATOR,
    gm_message_tag,
    logical_message,
)
# The 25-byte descriptor layout is capture-verified.  Handle, server clock and
# room tick are allocator/runtime fields and must never fall back to values
# copied from one packet capture.  Keep zero placeholders in the static layout
# and require every caller to provide all three recovered dynamic fields.
_SESSION_DESCRIPTOR_LAYOUT = bytes.fromhex(
    "000000000022012b1800000000000000000000000000000000"
)
_SESSION_DESCRIPTOR_HANDLE_OFFSET = 9
_SESSION_DESCRIPTOR_HANDLE_WIDTH = 4
_SESSION_DESCRIPTOR_CLOCK_OFFSET = 13
_SESSION_DESCRIPTOR_COOKIE_OFFSET = 21
# Attribute order confirmed by the retail rebroadcaster table at 0x004ac5f0:
# version, matchmaking_state, game_type, help_type, game_mode, skill,
# team_play, car_tier, max_online_player, length, track, n2o,
# collision_detection, player_dnf, location and the six race-type votes.
SESSION_ATTRIBUTE_NAMES = (
    "version",
    "matchmaking_state",
    "game_type",
    "help_type",
    "game_mode",
    "skill",
    "team_play",
    "car_tier",
    "max_online_player",
    "length",
    "track",
    "n2o",
    "collision_detection",
    "player_dnf",
    "location",
    "race_type_circuit",
    "race_type_sprint",
    "race_type_canyon_due",
    "race_type_speedtrap",
    "race_type_knockout",
    "race_type_pursuit_tag",
)

SESSION_ATTRIBUTE_DEFAULTS: dict[str, str] = {
    "version": "298_prod_server+22012b18",
    "matchmaking_state": "0",
    "game_type": "1",
    "help_type": "0",
    "game_mode": "",
    "skill": "",
    "team_play": "1",
    "car_tier": "",
    "max_online_player": "",
    "length": "",
    "track": "",
    "n2o": "",
    "collision_detection": "",
    "player_dnf": "",
    "location": "WH-EU",
    "race_type_circuit": "",
    "race_type_sprint": "",
    "race_type_canyon_due": "",
    "race_type_speedtrap": "",
    "race_type_knockout": "",
    "race_type_pursuit_tag": "",
}


def _session_attribute_value(properties: Mapping[str, object], name: str) -> str:
    if name == "version":
        for key in ("B-U-version", "B-version", "version"):
            if key in properties:
                return str(properties[key])
    else:
        key = f"B-U-{name}"
        if key in properties:
            return str(properties[key])
    return SESSION_ATTRIBUTE_DEFAULTS[name]


def session_attributes(properties: Mapping[str, object]) -> bytes:
    """Encode the retail 0x1D15 room-attribute broadcast.

    The original V691 fixture was copied from an unranked room and therefore
    always broadcast attribute 0x02 (``game_type``) as ``"1"``.  Carbon trusts
    this GameManager broadcast after GDAT, so ranked rooms were displayed as
    unranked.  Build the message from the authoritative Theater properties.
    """
    values = {
        name: _session_attribute_value(properties, name)
        for name in SESSION_ATTRIBUTE_NAMES
    }
    version = values["version"].encode("utf-8", errors="replace")
    if len(version) > 0xFFFFFF:
        raise RaceSessionCodecError("session version is too long")

    body = bytearray(logical_message(OLMessageType.GAME_ATTRIBUTES, b"\x15"))
    body.extend(len(version).to_bytes(3, "big"))
    body.extend(version)
    for index, name in enumerate(SESSION_ATTRIBUTE_NAMES[1:], start=1):
        encoded = values[name].encode("utf-8", errors="replace")
        if len(encoded) > 0xFFFF:
            raise RaceSessionCodecError(f"session attribute {name} is too long")
        body.append(index)
        body.extend(len(encoded).to_bytes(2, "big"))
        body.extend(encoded)
    return bytes(body)


def decode_session_attributes(body: bytes) -> dict[str, str]:
    """Decode a retail 0x1D15 body for diagnostics and regression tests."""
    raw = bytes(body)
    if len(raw) < 9 or raw[:6] != logical_message(OLMessageType.GAME_ATTRIBUTES, b"\x15"):
        raise RaceSessionCodecError("not a Carbon 0x1D15 attribute body")
    offset = 6
    version_length = int.from_bytes(raw[offset:offset + 3], "big")
    offset += 3
    if offset + version_length > len(raw):
        raise RaceSessionCodecError("truncated session version")
    result = {
        "version": raw[offset:offset + version_length].decode(
            "utf-8", errors="replace"
        )
    }
    offset += version_length
    while offset < len(raw):
        if offset + 3 > len(raw):
            raise RaceSessionCodecError("truncated session attribute header")
        index = raw[offset]
        length = int.from_bytes(raw[offset + 1:offset + 3], "big")
        offset += 3
        if offset + length > len(raw):
            raise RaceSessionCodecError("truncated session attribute value")
        if index >= len(SESSION_ATTRIBUTE_NAMES):
            raise RaceSessionCodecError(f"invalid session attribute index: {index}")
        result[SESSION_ATTRIBUTE_NAMES[index]] = raw[offset:offset + length].decode(
            "utf-8", errors="replace"
        )
        offset += length
    return result


# Backward-compatible baseline for callers outside the release package.
SESSION_CAPABILITIES = session_attributes({})


class RaceSessionCodecError(ValueError):
    pass


class InviteStatus(IntEnum):
    ENABLED = 0
    HOST_ONLY = 1
    DISABLED = 2


class JoinMode(IntEnum):
    CLOSED = 0
    OPEN = 1
    AUTO = 2
    CUSTOM = 3


@dataclass(frozen=True)
class HostProperties:
    wire_flag0: bool
    join_in_progress: bool
    join_via_presence: bool
    invite_status: InviteStatus
    join_mode: JoinMode
    join_flags: int = 0
    max_hosted_players: int = 8

    def encode(self) -> bytes:
        flags = int(self.join_flags)
        maximum = int(self.max_hosted_players)
        if not 0 <= flags <= 0xFFFF:
            raise RaceSessionCodecError(f"invalid HostProps join flags: {flags}")
        if not 1 <= maximum <= 8:
            raise RaceSessionCodecError(
                f"invalid HostProps max hosted players: {maximum}"
            )
        return (
            gm_message_tag(GMMessageType.HOST_PROPERTIES)
            + bytes(
                (
                    int(bool(self.wire_flag0)),
                    int(bool(self.join_in_progress)),
                    int(bool(self.join_via_presence)),
                    _encode_small_enum(self.invite_status),
                    _encode_small_enum(self.join_mode),
                )
            )
            + flags.to_bytes(2, "big")
            + maximum.to_bytes(2, "big")
        )


def _encode_small_enum(value: IntEnum | int) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 0x7F:
        raise RaceSessionCodecError(f"small enum out of range: {parsed}")
    return 0x80 | parsed


def open_host_properties(max_hosted_players: int, *, wire_flag0: bool = False) -> HostProperties:
    return HostProperties(
        wire_flag0=bool(wire_flag0),
        join_in_progress=True,
        join_via_presence=True,
        invite_status=InviteStatus.ENABLED,
        join_mode=JoinMode.OPEN,
        max_hosted_players=max_hosted_players,
    )


def locked_host_properties(max_hosted_players: int, *, wire_flag0: bool = False) -> HostProperties:
    return HostProperties(
        wire_flag0=bool(wire_flag0),
        join_in_progress=False,
        join_via_presence=False,
        invite_status=InviteStatus.DISABLED,
        join_mode=JoinMode.CLOSED,
        max_hosted_players=max_hosted_players,
    )


def reopen_host_properties(
    max_hosted_players: int, *, wire_flag0: bool = False
) -> tuple[HostProperties, ...]:
    """Captured progressive room reopen after returning from a race."""
    return (
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=False,
            join_via_presence=False,
            invite_status=InviteStatus.DISABLED,
            join_mode=JoinMode.OPEN,
            max_hosted_players=max_hosted_players,
        ),
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=False,
            join_via_presence=True,
            invite_status=InviteStatus.DISABLED,
            join_mode=JoinMode.OPEN,
            max_hosted_players=max_hosted_players,
        ),
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=True,
            join_via_presence=True,
            invite_status=InviteStatus.DISABLED,
            join_mode=JoinMode.OPEN,
            max_hosted_players=max_hosted_players,
        ),
        open_host_properties(
            max_hosted_players,
            wire_flag0=wire_flag0,
        ),
    )


def start_lock_host_properties(
    max_hosted_players: int, *, wire_flag0: bool = False
) -> tuple[HostProperties, ...]:
    """Captured progressive room lock, represented as named properties."""
    return (
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=True,
            join_via_presence=True,
            invite_status=InviteStatus.ENABLED,
            join_mode=JoinMode.CLOSED,
            max_hosted_players=max_hosted_players,
        ),
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=True,
            join_via_presence=False,
            invite_status=InviteStatus.ENABLED,
            join_mode=JoinMode.CLOSED,
            max_hosted_players=max_hosted_players,
        ),
        HostProperties(
            wire_flag0=bool(wire_flag0),
            join_in_progress=False,
            join_via_presence=False,
            invite_status=InviteStatus.ENABLED,
            join_mode=JoinMode.CLOSED,
            max_hosted_players=max_hosted_players,
        ),
        locked_host_properties(max_hosted_players, wire_flag0=wire_flag0),
    )


def logical_type(body: bytes) -> int | None:
    raw = bytes(body)
    if len(raw) < 5 or raw[:4] != LOGICAL_PREFIX:
        return None
    return raw[4]


def contains_logical_type(body: bytes, message_type: int) -> bool:
    marker = LOGICAL_PREFIX + bytes((int(message_type) & 0xFF,))
    return marker in bytes(body)


def descriptor(
    local_handle: int,
    elapsed_seconds: float,
    *,
    room_tick_ms: int,
) -> bytes:
    handle = int(local_handle)
    if not 1 <= handle <= 0xFFFFFFFF:
        raise RaceSessionCodecError(f"invalid descriptor handle: {local_handle}")
    body = bytearray(_SESSION_DESCRIPTOR_LAYOUT)
    body[_SESSION_DESCRIPTOR_HANDLE_OFFSET:_SESSION_DESCRIPTOR_HANDLE_OFFSET + _SESSION_DESCRIPTOR_HANDLE_WIDTH] = handle.to_bytes(_SESSION_DESCRIPTOR_HANDLE_WIDTH, "big")
    struct.pack_into(
        ">f",
        body,
        _SESSION_DESCRIPTOR_CLOCK_OFFSET,
        max(0.0, float(elapsed_seconds)),
    )
    tick = int(room_tick_ms)
    if not 1 <= tick <= 0xFFFFFFFF:
        raise RaceSessionCodecError(f"invalid descriptor room tick: {room_tick_ms}")
    body[_SESSION_DESCRIPTOR_COOKIE_OFFSET:_SESSION_DESCRIPTOR_COOKIE_OFFSET + 4] = (
        tick.to_bytes(4, "big")
    )
    return bytes(body)


def descriptor_bundle(
    local_handle: int,
    elapsed_seconds: float,
    *,
    room_tick_ms: int,
) -> bytes:
    body = descriptor(local_handle, elapsed_seconds, room_tick_ms=room_tick_ms)
    return logical_message(OLMessageType.CLOCK_SYNC_REQUEST, PLAIN_TERMINATOR + body + PLAIN_TERMINATOR)


def session_probe() -> bytes:
    return logical_message(OLMessageType.CLOCK_SYNC_REQUEST)


def session_confirm(token: bytes, elapsed_seconds: float) -> bytes:
    raw_token = bytes(token)
    if len(raw_token) != 4:
        raise RaceSessionCodecError("session token must be four bytes")
    elapsed = max(0.0, float(elapsed_seconds))
    return logical_message(OLMessageType.CLOCK_SYNC_END, raw_token + struct.pack(">f", elapsed))


def anonymous_state(state: int) -> bytes:
    return logical_message(OLMessageType.ACTIVE_GAME_MESSAGE, b"\x00\x00" + (int(state) & 0xFFFFFFFF).to_bytes(4, "big"))


def named_state(name: str, state: int) -> bytes:
    encoded = str(name or "player").encode("utf-8", errors="replace")[:255]
    return (
        logical_message(OLMessageType.ACTIVE_GAME_MESSAGE)
        + len(encoded).to_bytes(2, "big")
        + encoded
        + (int(state) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def start_timer(*, current_seconds: float, duration_seconds: float = 30.5, timer_id: int = 2) -> bytes:
    duration = float(duration_seconds)
    # Official Carbon uses the same 0x1B structure for multiple timers:
    # room wait (600.5 s), race countdown (20.5 s) and post-race (120.5 s).
    # Keep the codec generic; lifecycle validation belongs to the service.
    if not 0.0 < duration <= 900.0:
        raise RaceSessionCodecError(f"invalid timer duration: {duration_seconds}")
    return (
        logical_message(OLMessageType.START_TIMER)
        + (int(timer_id) & 0xFFFFFFFF).to_bytes(4, "big")
        + struct.pack(">f", max(0.0, float(current_seconds)))
        + struct.pack(">f", duration)
    )


def start_race_sync(
    clock_ms: int,
    *,
    start_delay_seconds: float = 2.0,
    ping: float = 0.0,
) -> bytes:
    return (
        logical_message(OLMessageType.START_RACE_SYNC_BEGIN)
        + (int(clock_ms) & 0xFFFFFFFF).to_bytes(4, "big")
        + struct.pack(">ff", float(start_delay_seconds), float(ping))
    )


def latency_info(player_id: int, latency_to_host: float = 25.0) -> bytes:
    return (
        logical_message(OLMessageType.LATENCY_INFO)
        + (int(player_id) & 0xFFFFFFFF).to_bytes(4, "big")
        + struct.pack(">f", float(latency_to_host))
    )
