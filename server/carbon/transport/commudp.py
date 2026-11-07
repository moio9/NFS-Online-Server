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


def split_active_records(active: CommUDPActive) -> tuple[bytes, ...] | None:
    """Split bounded CommUDP history into the current and appended records."""
    if len(active.payload) < 8:
        return None
    remaining = active.payload[8:]
    history = (int(active.sequence) >> 28) & 15
    previous = []
    for _ in range(history):
        if not remaining:
            return None
        size = remaining[-1]
        if size == 0 or size + 1 > len(remaining):
            return None
        previous.append(remaining[-size - 1:-1])
        remaining = remaining[:-size - 1]
    if not remaining:
        return () if history == 0 else None
    return (remaining, *previous)


def _record_body(record: bytes) -> bytes | None:
    if not record:
        return None
    if record[-1] & 0x40:
        return record[:-13] if len(record) >= 13 else None
    return record[:-1]


def is_empty_timing_ack(active: CommUDPActive) -> bool:
    records = split_active_records(active)
    return bool(records) and all(
        len(record) == 13 and bool(record[-1] & 0x40)
        for record in records
    )


def is_empty_transport_ack(active: CommUDPActive) -> bool:
    """Recognize an eight-byte CommUDP ACK without an NGL timing record."""
    return len(active.payload) == 8 and (int(active.sequence) >> 28) == 0


def current_timing_footer(active: CommUDPActive) -> bytes | None:
    """Read timing from the current record, excluding appended history."""
    records = split_active_records(active)
    if not records:
        return None
    current = records[0]
    if len(current) < 13 or not current[-1] & 0x40:
        return None
    return bytes(current[-13:-1])


def parse_session_ticket_active(active: CommUDPActive) -> str | None:
    """Accept a ticket plus only empty timing records in CommUDP history."""
    records = split_active_records(active)
    if records is None:
        return None
    tickets = []
    for record in records:
        body = _record_body(record)
        if body is None:
            return None
        if not body:
            if len(record) != 13 or not record[-1] & 0x40:
                return None
            continue
        ticket = parse_session_ticket(GameManagerMessage(0, body))
        if ticket is None:
            return None
        tickets.append(ticket)
    return tickets[0] if tickets and len(set(tickets)) == 1 else None


def game_manager_body(active_payload: bytes) -> bytes:
    if len(active_payload) < 10:
        return b""
    # A history made entirely of timing acknowledgements has no application
    # message. Without this check its timing bytes look like unknown OLMSGs.
    sequence = int.from_bytes(active_payload[:4], "big")
    if sequence >> 28:
        active = CommUDPActive(sequence, 0, active_payload, None)
        if is_empty_timing_ack(active):
            return b""
    ngl = active_payload[8:]
    # An Xbox client can aggregate its initial session-ticket record with an
    # empty timing record. The latter is twelve timing bytes, a 0x40 footer,
    # and the appended-record length 0x0d. Unwrap only this validated ticket
    # shape; other aggregates require their own parsing rules.
    if len(ngl) >= 8 and ngl[:7] == b"\x01\x80\x80\x04\x80\x00\x00":
        ticket_end = 8 + ngl[7]
        if (
            ngl[7] > 0
            and len(ngl) == ticket_end + 15
            and ngl[ticket_end] == 0x04
            and ngl[-2:] == b"\x40\x0d"
        ):
            ngl = ngl[:ticket_end + 1]
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
