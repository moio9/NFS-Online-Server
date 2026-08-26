"""Role-local publication of Carbon's capture-backed Ready seed."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging
import struct
import zlib

from carbon.gamemanager.protocol import (
    OLMessageType,
    ObservedTimerId,
    PLAIN_TERMINATOR,
)
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.state import Address, EndpointWireState
from carbon.theater.directory import CarbonGame, CarbonTicketResolution
from carbon.transport.commudp import CommUDPActive, game_manager_body
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF

CurrentBody = Callable[[bytes], bytes | None]
StateValue = Callable[[bytes], int | None]
IsHost = Callable[[CarbonTicketResolution], bool]


class ReadySeedCoordinator:
    """Recognize and serialize Ready seed packets for host and helper roles."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        *,
        current_timer_body: CurrentBody,
        current_active_game_body: CurrentBody,
        state_value: StateValue,
        is_host: IsHost,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self._wires = wires
        self._bindings = bindings
        self._current_timer_body = current_timer_body
        self._current_active_game_body = current_active_game_body
        self._state_value = state_value
        self._is_host = is_host
        self.log = logger or logging.getLogger(__name__)

    def parts(
        self,
        game: CarbonGame,
        active_packets: list[CommUDPActive],
    ) -> tuple[bytes, bytes, bytes] | None:
        """Recognize the capture-backed five-packet Challenge Ready seed."""
        timer: bytes | None = None
        attributes: bytes | None = None
        state1: bytes | None = None
        has_matchmaking_off = False
        has_disable_joins = False
        for active in active_packets:
            logical = game_manager_body(active.payload)
            kind = logical_type(logical)
            if kind == OLMessageType.START_TIMER:
                current = self._current_timer_body(logical)
                if current is not None:
                    timer_id, _clock, duration = struct.unpack(
                        ">Iff",
                        current[5:17],
                    )
                    if (
                        timer_id == int(ObservedTimerId.RACE_COUNTDOWN)
                        and 0.0 < duration <= 300.0
                    ):
                        timer = current
            elif kind == OLMessageType.GAME_ATTRIBUTES:
                attributes = bytes(logical)
            elif kind == OLMessageType.ACTIVE_GAME_MESSAGE:
                current = self._current_active_game_body(logical)
                if current is not None and self._state_value(current) == 1:
                    state1 = current
            elif kind == OLMessageType.MATCHMAKING_OFF_REQUEST:
                has_matchmaking_off = True
            elif kind == OLMessageType.DISABLE_JOINS_REQUEST:
                has_disable_joins = True

        if (
            timer is None
            or attributes is None
            or state1 is None
            or not has_matchmaking_off
            or not has_disable_joins
        ):
            return None
        source_flags = [
            (int(active.sequence) >> 28) & 0xF
            for active in active_packets
        ]
        if len(active_packets) != 5 or source_flags != [2, 0, 0, 1, 2]:
            self.log.info(
                "Carbon GM ReadyEpoch seed candidate rejected: gid=%s "
                "reason=packet-shape packets=%d flags=%s",
                game.gid,
                len(active_packets),
                ",".join(str(flag) for flag in source_flags),
            )
            return None
        return timer, attributes, state1

    def _prepare_destination(
        self,
        destination: Address,
        generation: int,
    ) -> tuple[EndpointWireState, int, int]:
        wire = self._wires[destination]
        wire.ready_epoch_generation = generation
        wire.ready_requested = False
        wire.active_game_ready = False
        wire.match_timer_retry = None
        wire.ready_seed_final_sequence = 0
        acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        return wire, acknowledgement, base

    def append_host(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        game: CarbonGame,
        generation: int,
        timer: bytes,
        attributes: bytes,
        state1: bytes,
    ) -> None:
        wire, acknowledgement, base = self._prepare_destination(
            destination,
            generation,
        )
        control_80 = bytes.fromhex("018c000000828000000002")
        control_81 = bytes.fromhex("018c000000828100000002")
        timer_window = (
            timer
            + b"\x04"
            + control_80
            + b"\x04\x0c"
            + control_81
            + b"\x04"
        )
        prelude_packets = (
            TunnelPacket(
                1,
                encode_active(base, acknowledgement, control_81 + b"\x04"),
            ),
            TunnelPacket(
                1,
                encode_active(
                    0x10000000 | ((base + 1) & _SEQUENCE_MASK),
                    acknowledgement,
                    control_80 + b"\x04" + control_81 + b"\x04\x04",
                ),
            ),
        )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, prelude_packets),
            destination,
            confirmation="ready-seed-host-prelude",
        )
        seed_packets = (
            TunnelPacket(
                1,
                encode_active(
                    0x20000000 | ((base + 2) & _SEQUENCE_MASK),
                    acknowledgement,
                    timer_window + b"\x04",
                ),
            ),
            TunnelPacket(
                1,
                encode_active(
                    (base + 3) & _SEQUENCE_MASK,
                    acknowledgement,
                    attributes + b"\x04",
                ),
            ),
            TunnelPacket(
                1,
                encode_active(
                    (base + 4) & _SEQUENCE_MASK,
                    acknowledgement,
                    state1 + b"\x04",
                ),
            ),
        )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, seed_packets),
            destination,
            confirmation="ready-seed-host",
        )
        wire.next_server_sequence = (base + 5) & _SEQUENCE_MASK
        wire.match_timer_sequence = (base + 2) & _SEQUENCE_MASK
        wire.ready_seed_final_sequence = (base + 4) & _SEQUENCE_MASK
        self.log.info(
            "Carbon GM ReadyEpoch seed: gid=%s gen=%d dst=%s:%d "
            "role=host prelude_flags=0,1 seed_flags=2,0,0 timer_hash=%08x",
            game.gid,
            generation,
            destination[0],
            destination[1],
            zlib.crc32(timer),
        )

    def append_guest(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        endpoints: tuple[Address, ...],
        game: CarbonGame,
        generation: int,
        timer: bytes,
        attributes: bytes,
        state1: bytes,
    ) -> tuple[str, bool]:
        wire, acknowledgement, base = self._prepare_destination(
            destination,
            generation,
        )
        control_80 = bytes.fromhex("018c000000828000000002")
        control_81 = bytes.fromhex("018c000000828100000002")
        host_endpoint = next(
            endpoint
            for endpoint in endpoints
            if self._is_host(self._bindings[endpoint])
        )
        host_latency = bytes(self._wires[host_endpoint].latest_latency_info)
        guest_latency = bytes(wire.latest_latency_info)
        has_latency_pair = (
            len(host_latency) == 13
            and logical_type(host_latency) == OLMessageType.LATENCY_INFO
            and len(guest_latency) == 13
            and logical_type(guest_latency) == OLMessageType.LATENCY_INFO
        )
        if has_latency_pair:
            latency_packets = (
                TunnelPacket(
                    1,
                    encode_active(
                        base,
                        acknowledgement,
                        host_latency + PLAIN_TERMINATOR,
                    ),
                ),
                TunnelPacket(
                    1,
                    encode_active(
                        0x10000000 | ((base + 1) & _SEQUENCE_MASK),
                        acknowledgement,
                        self.publisher.commudp_aggregate_payload(
                            (guest_latency, host_latency)
                        ),
                    ),
                ),
            )
            self.publisher.append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, latency_packets),
                destination,
                confirmation="ready-seed-guest-latency",
            )
            prelude_base = (base + 2) & _SEQUENCE_MASK
            prelude_packets = (
                TunnelPacket(
                    1,
                    encode_active(
                        0x20000000 | prelude_base,
                        acknowledgement,
                        self.publisher.commudp_aggregate_payload(
                            (control_81, guest_latency, host_latency)
                        ),
                    ),
                ),
                TunnelPacket(
                    1,
                    encode_active(
                        0x20000000
                        | ((prelude_base + 1) & _SEQUENCE_MASK),
                        acknowledgement,
                        self.publisher.commudp_aggregate_payload(
                            (control_80, control_81, guest_latency)
                        ),
                    ),
                ),
            )
            self.publisher.append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, prelude_packets),
                destination,
                confirmation="ready-seed-guest-prelude",
            )
            seed_base = (prelude_base + 2) & _SEQUENCE_MASK
            prelude_flags = "0,1-latency+2,2-controls-destination-local-v810"
            wire.ready_seed_used_latency_history = True
        else:
            seed_base = base
            prelude_flags = "omitted-incomplete-latency-pair"
            wire.ready_seed_used_latency_history = False

        seed_packets = (
            TunnelPacket(
                1,
                encode_active(
                    0x20000000 | seed_base,
                    acknowledgement,
                    self.publisher.commudp_aggregate_payload(
                        (timer, control_80, control_81)
                    ),
                ),
            ),
            TunnelPacket(
                1,
                encode_active(
                    (seed_base + 1) & _SEQUENCE_MASK,
                    acknowledgement,
                    attributes + b"\x04",
                ),
            ),
            TunnelPacket(
                1,
                encode_active(
                    (seed_base + 2) & _SEQUENCE_MASK,
                    acknowledgement,
                    state1 + b"\x04",
                ),
            ),
        )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, seed_packets),
            destination,
            confirmation="ready-seed-guest",
        )
        wire.next_server_sequence = (seed_base + 3) & _SEQUENCE_MASK
        wire.match_timer_sequence = seed_base
        wire.ready_seed_final_sequence = (seed_base + 2) & _SEQUENCE_MASK
        self.log.info(
            "Carbon GM ReadyEpoch seed: gid=%s gen=%d dst=%s:%d "
            "role=guest prelude_flags=%s seed_flags=2,0,0 "
            "latency_pair=%d timer_hash=%08x",
            game.gid,
            generation,
            destination[0],
            destination[1],
            prelude_flags,
            int(has_latency_pair),
            zlib.crc32(timer),
        )
        return prelude_flags, has_latency_pair
