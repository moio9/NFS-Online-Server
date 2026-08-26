"""Capture-shaped ProtoTunnel/CommUDP connection handshake."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from carbon.transport.commudp import CommUDPControl, CommUDPType, parse_channel_one
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


class HandshakeStage(str, Enum):
    NEW = "new"
    CONNECT_SENT = "connect_sent"
    WAIT_CONFIRMATION = "wait_confirmation"
    ESTABLISHED = "established"


class HandshakeRole(str, Enum):
    UNKNOWN = "unknown"
    HOST = "host"
    JOINER = "joiner"


def hash_sar_decimal(value: int | str) -> int:
    """Return Carbon's signed-SAR hash of a decimal identity string.

    ProtoTunnel channel 7 is six bytes:

        00 + hash_sar_decimal(EGEG.HUID)[4] + stage[1]

    Earlier release builds incorrectly split the middle four bytes into a
    synthetic client-id/state pair.  That happened to reproduce captures where
    HUID was 5 (hash 0x35) or 51 (hash 0x691), but failed as soon as EGEG used
    the real nfsdevserver HUID 799270239 (hash 0x2768fb60).
    """

    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        raise ValueError("ProtoTunnel HUID must be a decimal integer")
    hashed = 0
    for character in text:
        signed = hashed if hashed < 0x80000000 else hashed - 0x100000000
        hashed = (
            ord(character)
            ^ (signed >> 27)
            ^ ((hashed << 5) & 0xFFFFFFFF)
        ) & 0xFFFFFFFF
    return hashed


# The create/invite capture advertises EGEG.HUID=51, whose channel-7 hash is
# 0x00000691.  Keep that as the compatibility default for client-hosted paths;
# dedicated paths override it from the actual game host HUID.
DEFAULT_TUNNEL_ID = hash_sar_decimal(51)


@dataclass
class EndpointHandshake:
    server_tunnel_id: int = DEFAULT_TUNNEL_ID
    dedicated: bool = False
    stage: HandshakeStage = HandshakeStage.NEW
    connection_id: int = 0
    role: HandshakeRole = HandshakeRole.UNKNOWN

    def accept(self, datagram: TunnelDatagram) -> TunnelDatagram | None:
        channel7 = next((packet.payload for packet in datagram.packets if packet.channel == 7), b"")
        controls = []
        for packet in datagram.packets:
            parsed = parse_channel_one(packet)
            if isinstance(parsed, CommUDPControl):
                controls.append(parsed)
        connect = next((item for item in controls if item.kind is CommUDPType.CONNECT), None)
        acknowledgement = next((item for item in controls if item.kind is CommUDPType.CONNECT_ACK), None)
        if connect is None and acknowledgement is None:
            return None
        if connect is not None:
            self.connection_id = connect.connection_id
        elif acknowledgement is not None:
            self.connection_id = acknowledgement.connection_id
        if acknowledgement is not None:
            if self.role is HandshakeRole.JOINER and connect is not None:
                self.stage = HandshakeStage.WAIT_CONFIRMATION
                return connect_reply(
                    self.connection_id,
                    server_tunnel_id=self.server_tunnel_id,
                )
            self.stage = HandshakeStage.ESTABLISHED
            if self.role is HandshakeRole.JOINER:
                return joiner_confirmation_reply(self.connection_id)
            # Host capture confirms our combined Type2+Type1. The
            # rebroadcaster proceeds to session bootstrap instead of echoing
            # another connection response.
            return None
        if self.stage is HandshakeStage.NEW:
            self.stage = HandshakeStage.CONNECT_SENT
            # Invite join uses a two-leg negotiation. Preserve the capture-
            # verified 0x37 low hash byte / stage-0 discriminator used by the
            # second endpoint. Dedicated rooms answer directly with the
            # combined Type2+Type1 response.
            if (
                not self.dedicated
                and len(channel7) == 6
                and channel7[4] == 0x37
                and channel7[5] == 0
            ):
                self.role = HandshakeRole.JOINER
                return connect_offer(
                    self.connection_id,
                    server_tunnel_id=self.server_tunnel_id,
                )
            self.role = HandshakeRole.HOST
        return connect_reply(
            self.connection_id,
            server_tunnel_id=self.server_tunnel_id,
        )


def channel7_identity(server_tunnel_id: int, *, stage: int = 0x01) -> bytes:
    if not 0 <= int(server_tunnel_id) <= 0xFFFFFFFF:
        raise ValueError("server tunnel id out of range")
    return b"\x00" + int(server_tunnel_id).to_bytes(4, "big") + bytes((int(stage) & 0xFF,))


def control_payload(kind: CommUDPType, connection_id: int) -> bytes:
    return int(kind).to_bytes(4, "big") + (int(connection_id) & 0xFFFFFFFF).to_bytes(4, "big")


def connect_reply(
    connection_id: int,
    *,
    server_tunnel_id: int = DEFAULT_TUNNEL_ID,
    offset_words: int = 5,
) -> TunnelDatagram:
    return TunnelDatagram(
        int(offset_words),
        (
            TunnelPacket(7, channel7_identity(server_tunnel_id, stage=1)),
            TunnelPacket(1, control_payload(CommUDPType.CONNECT_ACK, connection_id)),
            TunnelPacket(1, control_payload(CommUDPType.CONNECT, connection_id)),
        ),
    )


def connect_offer(
    connection_id: int,
    *,
    server_tunnel_id: int = DEFAULT_TUNNEL_ID,
) -> TunnelDatagram:
    return TunnelDatagram(
        0,
        (
            TunnelPacket(7, channel7_identity(server_tunnel_id, stage=0)),
            TunnelPacket(1, control_payload(CommUDPType.CONNECT, connection_id)),
        ),
    )


def joiner_confirmation_reply(connection_id: int) -> TunnelDatagram:
    return TunnelDatagram(
        12,
        (
            TunnelPacket(1, control_payload(CommUDPType.CONNECT_ACK, connection_id)),
            TunnelPacket(1, b"\x00\x00\x01\x00\x00\x00\x00\xff"),
        ),
    )
