"""Capture-verified Carbon GameManager player wire codec.

This module has no server or socket state. It models the PlayerData body used
by reliable roster (0x0183) and join (0x0185) messages.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import struct

from carbon.fesl.primitives import fesl_string, sint8, sint64
from carbon.gamemanager.protocol import (
    GMMessageType,
    PLAIN_TERMINATOR,
    gm_message_tag,
)


class PlayerCodecError(ValueError):
    pass


_OPTIONAL_NETWORK_BLOCK = (
    b"\x01"
    + sint8(0x14) + b"\x00" * 20
    + sint8(0x08) + b"\x00" * 8
    + sint8(0x08) + b"\x00" * 8
)


@dataclass(frozen=True)
class PlayerWireData:
    player_id: int
    name: str
    profile_id: int
    state: int
    internal_ip: str
    internal_port: int
    external_ip: str
    external_port: int
    connection_value: int = 0
    trailing_flag: bool = False


def _ipv4(value: str) -> bytes:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError as exc:
        raise PlayerCodecError(f"invalid IPv4 address: {value!r}") from exc
    if address.version != 4:
        raise PlayerCodecError(f"IPv6 is not supported: {value!r}")
    return address.packed


def encode_endpoint_pair(player: PlayerWireData) -> bytes:
    for label, port in (("internal", player.internal_port), ("external", player.external_port)):
        if not 0 <= int(port) <= 0xFFFF:
            raise PlayerCodecError(f"{label} port out of range: {port}")
    return (
        _ipv4(player.internal_ip)
        + struct.pack(">H", int(player.internal_port))
        + _ipv4(player.external_ip)
        + struct.pack(">H", int(player.external_port))
    )


def encode_player_data(player: PlayerWireData) -> bytes:
    player_id = int(player.player_id)
    if not 0 < player_id <= 0xFFFF:
        raise PlayerCodecError(f"invalid player id: {player_id}")
    state = int(player.state)
    if not 0 <= state <= 0x7F:
        raise PlayerCodecError(f"invalid player state: {state}")
    return (
        struct.pack(">H", player_id)
        + fesl_string(player.name)
        + sint64(int(player.profile_id))
        + sint8(state)
        + int(player.connection_value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
        + _OPTIONAL_NETWORK_BLOCK
        + sint8(0)
        + encode_endpoint_pair(player)
        + (b"\x01" if player.trailing_flag else b"\x00")
    )


def encode_roster(player: PlayerWireData) -> bytes:
    return gm_message_tag(GMMessageType.PLAYER_ROSTER) + encode_player_data(player) + PLAIN_TERMINATOR


def encode_join(player: PlayerWireData) -> bytes:
    player_id = int(player.player_id)
    return gm_message_tag(GMMessageType.PLAYER_JOINED) + struct.pack(">H", player_id) + encode_player_data(player) + PLAIN_TERMINATOR


def encode_leave(player_id: int, reason: int = 0) -> bytes:
    """Encode Carbon's ``PlayerLeft``/leave-announcement logical body."""
    parsed_player_id = int(player_id)
    if not 0 < parsed_player_id <= 0xFFFF:
        raise PlayerCodecError(f"invalid player id: {parsed_player_id}")
    parsed_reason = int(reason)
    if not -128 <= parsed_reason <= 127:
        raise PlayerCodecError(f"leave reason out of range: {parsed_reason}")
    return gm_message_tag(GMMessageType.PLAYER_LEFT) + struct.pack(">H", parsed_player_id) + sint8(parsed_reason)


def _read_shifted(data: bytes, position: int, width: int) -> tuple[int, int]:
    end = position + width
    if end > len(data):
        raise PlayerCodecError(f"truncated shifted integer ({width} bytes)")
    shift = 1 << (width * 8 - 1)
    return (int.from_bytes(data[position:end], "big") - shift) & ((1 << (width * 8)) - 1), end


def decode_player_data(data: bytes, position: int = 0) -> tuple[PlayerWireData, int]:
    if position + 2 > len(data):
        raise PlayerCodecError("truncated player id")
    player_id = int.from_bytes(data[position:position + 2], "big")
    position += 2
    name_length, position = _read_shifted(data, position, 4)
    if position + name_length > len(data):
        raise PlayerCodecError("truncated player name")
    name = data[position:position + name_length].decode("ascii", errors="replace")
    position += name_length
    profile_id, position = _read_shifted(data, position, 8)
    state, position = _read_shifted(data, position, 1)
    if position + 8 > len(data):
        raise PlayerCodecError("truncated connection value")
    connection_value = int.from_bytes(data[position:position + 8], "big")
    position += 8
    if position >= len(data):
        raise PlayerCodecError("truncated optional-network flag")
    optional = data[position]
    position += 1
    if optional:
        for expected_length in (20, 8, 8):
            block_length, position = _read_shifted(data, position, 1)
            if block_length != expected_length:
                raise PlayerCodecError(
                    f"unexpected optional block length {block_length}, expected {expected_length}"
                )
            if position + block_length > len(data):
                raise PlayerCodecError("truncated optional network block")
            position += block_length
    address_type, position = _read_shifted(data, position, 1)
    if address_type != 0:
        raise PlayerCodecError(f"unsupported address type: {address_type}")
    if position + 13 > len(data):
        raise PlayerCodecError("truncated endpoint pair or trailing flag")
    internal_ip = str(ipaddress.ip_address(data[position:position + 4]))
    position += 4
    internal_port = int.from_bytes(data[position:position + 2], "big")
    position += 2
    external_ip = str(ipaddress.ip_address(data[position:position + 4]))
    position += 4
    external_port = int.from_bytes(data[position:position + 2], "big")
    position += 2
    trailing_flag = bool(data[position])
    position += 1
    return PlayerWireData(
        player_id=player_id,
        name=name,
        profile_id=profile_id,
        state=state,
        internal_ip=internal_ip,
        internal_port=internal_port,
        external_ip=external_ip,
        external_port=external_port,
        connection_value=connection_value,
        trailing_flag=trailing_flag,
    ), position
