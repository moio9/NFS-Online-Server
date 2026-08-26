"""Reliable registration and delivery tracking for coordinator-owned AI cars.

The coordinator operates on explicit room/endpoint snapshots supplied by the
rebroadcaster service.  It owns no endpoint directory and performs no room
enumeration; destination-local serialization is delegated to
``EndpointPublisher``.
"""

from __future__ import annotations

from collections.abc import Sequence
import logging
import time

from carbon.gamemanager.protocol import OLMessageType, logical_message
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.retry import ReliableWindow, RetryPolicy
from carbon.rebroadcaster.state import Address, EndpointWireState
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000
_AI_REGISTRATION_RETRY_POLICY = RetryPolicy(0.75, 3.0, 8, 30.0)

DestinationWire = tuple[Address, EndpointWireState]
ReadyGuest = tuple[Address, int, EndpointWireState]


class AIRegistrationCoordinator:
    """Normalize AI registrations and track reliable delivery per endpoint."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def current_body(logical: bytes) -> bytes | None:
        """Return the leading OLMSG 0x05 without history/footer data."""
        if (
            logical_type(logical) != OLMessageType.PLAYER_CONTROLLED_AI_CAR
            or len(logical) < 17
        ):
            return None
        name_length = int.from_bytes(logical[15:17], "big")
        end = 17 + name_length
        if name_length <= 0 or name_length > 0x20 or end > len(logical):
            return None
        return bytes(logical[:end])

    def relay(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        *,
        gid: str,
        race: GameRaceState,
        destinations: Sequence[DestinationWire],
        logicals: Sequence[bytes],
    ) -> None:
        """Register new coordinator-owned cars and publish the retail window."""
        normalized: list[bytes] = []
        for logical in logicals:
            current = self.current_body(logical)
            if current is None:
                continue
            server_car_id = int.from_bytes(current[5:9], "big")
            if server_car_id <= 0 or server_car_id in race.player_controlled_ai:
                continue
            sanitized = bytearray(current)
            sanitized[11:15] = b"\x00\x00\x00\x00"
            body = bytes(sanitized)
            race.player_controlled_ai[server_car_id] = body
            normalized.append(body)

        if not normalized:
            return

        records = tuple(normalized)
        now = time.monotonic()
        for destination, wire in destinations:
            base_sequence, final_sequence = self.append_window(
                replies,
                destination,
                wire,
                records,
            )
            wire.pending_ai_registration_windows.append(
                ReliableWindow(
                    records=records,
                    base_sequence=base_sequence,
                    final_sequence=final_sequence,
                    retry=_AI_REGISTRATION_RETRY_POLICY.begin(now),
                )
            )
        self.log.info(
            "Carbon GM release V825 player-controlled AI registration relay: "
            "gid=%s src=%s:%d endpoints=%d cars=%s controller=zero "
            "tail=ready history=retail-0-1-1-2 ack_tracking=per-endpoint",
            gid,
            source[0],
            source[1],
            len(destinations),
            ",".join(
                f"{int.from_bytes(body[5:9], 'big'):08x}"
                for body in records
            ),
        )

    def refresh_ready_guests(
        self,
        replies: list[tuple[bytes, Address]],
        *,
        gid: str,
        race: GameRaceState,
        guests: Sequence[ReadyGuest],
    ) -> None:
        """Republish an ACKed AI registry when ready guests can consume it."""
        registrations = tuple(race.player_controlled_ai.values())
        if not registrations:
            return

        now = time.monotonic()
        for destination, player_id, wire in guests:
            if (
                wire.ai_registration_ready_refresh_sent
                or wire.pending_ai_registration_windows
            ):
                continue
            base_sequence, final_sequence = self.append_window(
                replies,
                destination,
                wire,
                registrations,
            )
            wire.pending_ai_registration_windows.append(
                ReliableWindow(
                    records=registrations,
                    base_sequence=base_sequence,
                    final_sequence=final_sequence,
                    retry=_AI_REGISTRATION_RETRY_POLICY.begin(now),
                )
            )
            wire.ai_registration_ready_refresh_sent = True
            self.log.info(
                "Carbon GM release V828 AI registration ready refresh: "
                "gid=%s dst=%s:%d pid=%d cars=%s seq=%07x-%07x "
                "reason=acked-before-roster-ready",
                gid,
                destination[0],
                destination[1],
                player_id,
                ",".join(
                    f"{int.from_bytes(body[5:9], 'big'):08x}"
                    for body in registrations
                ),
                base_sequence,
                final_sequence,
            )

    def append_window(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        wire: EndpointWireState,
        registrations: Sequence[bytes],
        *,
        base_sequence: int | None = None,
    ) -> tuple[int, int]:
        registrations = tuple(bytes(item) for item in registrations)
        if not registrations:
            raise ValueError("AI registration window requires at least one record")
        advances_reliable_sequence = base_sequence is None
        base = (
            int(wire.next_server_sequence)
            if base_sequence is None
            else int(base_sequence)
        ) & _SEQUENCE_MASK
        acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
        packets: list[TunnelPacket] = []

        for index, current in enumerate(registrations):
            records = (
                (current,)
                if index == 0
                else (current, registrations[index - 1])
            )
            sequence = (
                ((len(records) - 1) << 28)
                | ((base + index) & _SEQUENCE_MASK)
            )
            packets.append(
                TunnelPacket(
                    1,
                    encode_active(
                        sequence,
                        acknowledgement,
                        self.publisher.commudp_aggregate_payload(records),
                    ),
                )
            )

        ready_history = tuple(reversed(registrations[-2:]))
        ready_records = (
            logical_message(OLMessageType.READY),
            *ready_history,
        )
        ready_sequence = (
            ((len(ready_records) - 1) << 28)
            | ((base + len(registrations)) & _SEQUENCE_MASK)
        )
        packets.append(
            TunnelPacket(
                1,
                encode_active(
                    ready_sequence,
                    acknowledgement,
                    self.publisher.commudp_aggregate_payload(ready_records),
                ),
            )
        )
        final_sequence = (base + len(registrations)) & _SEQUENCE_MASK
        if advances_reliable_sequence:
            wire.next_server_sequence = (final_sequence + 1) & _SEQUENCE_MASK
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, tuple(packets)),
            destination,
        )
        return base, final_sequence

    def update_delivery(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        wire: EndpointWireState,
        race: GameRaceState | None,
        *,
        gid: str,
        player_id: int,
        force_retry: bool = False,
        reason: str = "ack-gap",
        now: float | None = None,
    ) -> bool:
        """Confirm or gently retransmit reliable AI registration windows."""
        pending = wire.pending_ai_registration_windows
        if not pending:
            return False

        acknowledgement = int(wire.last_client_acknowledgement) & _SEQUENCE_MASK
        unconfirmed: list[ReliableWindow] = []
        for window in pending:
            if self._sequence_acked(acknowledgement, window.final_sequence):
                self.log.info(
                    "Carbon GM release V827 AI registration confirmed: "
                    "gid=%s dst=%s:%d pid=%d seq=%07x-%07x ack=%07x "
                    "retries=%d",
                    gid,
                    destination[0],
                    destination[1],
                    player_id,
                    window.base_sequence,
                    window.final_sequence,
                    acknowledgement,
                    window.retry.retries_sent,
                )
                continue
            unconfirmed.append(window)
        wire.pending_ai_registration_windows = unconfirmed
        if not unconfirmed:
            return False

        if race is not None and race.phase >= RacePhase.FINISHED:
            wire.pending_ai_registration_windows.clear()
            return False

        current = time.monotonic() if now is None else float(now)
        active: list[ReliableWindow] = []
        delivery_failed = False
        for window in unconfirmed:
            exhaustion_reason = window.retry.exhaustion_reason(current)
            if exhaustion_reason is None:
                active.append(window)
                continue
            delivery_failed = True
            self.log.warning(
                "Carbon GM release V827 AI registration retry exhausted: "
                "gid=%s dst=%s:%d pid=%d seq=%07x-%07x ack=%07x "
                "attempts=%d reason=%s elapsed=%.3f",
                gid,
                destination[0],
                destination[1],
                player_id,
                window.base_sequence,
                window.final_sequence,
                acknowledgement,
                window.retry.retries_sent,
                exhaustion_reason,
                max(0.0, current - window.retry.opened_at),
            )
        wire.pending_ai_registration_windows = active
        if not active:
            return delivery_failed

        retry_windows = active if force_retry else active[:1]
        for window in retry_windows:
            if not force_retry and not window.retry.due(current):
                continue
            self.append_window(
                replies,
                destination,
                wire,
                window.records,
                base_sequence=window.base_sequence,
            )
            retry_delay = window.retry.record_retry(current)
            self.log.info(
                "Carbon GM release V827 AI registration retry: "
                "gid=%s dst=%s:%d pid=%d seq=%07x-%07x ack=%07x "
                "attempt=%d reason=%s next_retry=%.3f",
                gid,
                destination[0],
                destination[1],
                player_id,
                window.base_sequence,
                window.final_sequence,
                acknowledgement,
                window.retry.retries_sent,
                reason,
                retry_delay,
            )
        return delivery_failed

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF
