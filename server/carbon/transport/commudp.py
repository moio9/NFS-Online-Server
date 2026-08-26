"""CommUDP control/active and embedded GameManager parsing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from carbon.gamemanager.protocol import GMMessageType, gm_message_tag
from carbon.transport.prototunnel import TunnelPacket


class CommUDPType(IntEnum):
    CONNECT = 1
    CONNECT_ACK = 2
    DISCONNECT = 3
    PING = 4
    RELIABLE = 5
    RELIABLE_ACK = 6


@dataclass(frozen=True)
class CommUDPControl:
    kind: CommUDPType
    connection_id: int


@dataclass(frozen=True)
class GameManagerMessage:
    message_type: int
    body: bytes


@dataclass(frozen=True)
class CommUDPActive:
    sequence: int
    acknowledgement: int
    payload: bytes
    game_manager: GameManagerMessage | None


def parse_session_ticket(message: GameManagerMessage | None) -> str | None:
    """Return the decimal ticket carried by Carbon's capture-proven 0x0180."""
    if message is None or message.message_type != 0:
        return None
    body = message.body
    if len(body) < 9 or body[:7] != gm_message_tag(GMMessageType.SESSION_TICKET) + bytes.fromhex("8004800000"):
        return None
    size = body[7]
    if size == 0 or len(body) != 8 + size:
        return None
    try:
        ticket = body[8:].decode("ascii")
    except UnicodeDecodeError:
        return None
    return ticket if ticket.isdecimal() else None


def game_manager_body(active_payload: bytes) -> bytes:
    if len(active_payload) < 10:
        return b""
    ngl = active_payload[8:]
    final = ngl[-1]
    body = ngl[:-1]
    if final & 0x40:
        if len(body) < 12:
            return b""
        body = body[:-12]
    return body


def parse_game_manager(active_payload: bytes) -> GameManagerMessage | None:
    body = game_manager_body(active_payload)
    if len(body) < 2 or body[0] != 0x01 or body[1] < 0x80:
        return None
    return GameManagerMessage(body[1] - 0x80, body)


def parse_channel_one(packet: TunnelPacket) -> CommUDPControl | CommUDPActive | None:
    if packet.channel != 1:
        return None
    payload = packet.payload
    if len(payload) == 8:
        raw_type = int.from_bytes(payload[:4], "big")
        if raw_type in CommUDPType._value2member_map_:
            return CommUDPControl(CommUDPType(raw_type), int.from_bytes(payload[4:], "big"))
    if len(payload) < 8:
        return None
    return CommUDPActive(
        int.from_bytes(payload[:4], "big"),
        int.from_bytes(payload[4:8], "big"),
        payload,
        parse_game_manager(payload),
    )
