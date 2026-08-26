"""Capture/decomp-backed NetGameLink world-state timing and serialization."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.state import Address, EndpointWireState
from carbon.theater.directory import CarbonTicketResolution
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


VIRTUAL_SEQUENCE_FIRST = 0x80
VIRTUAL_SEQUENCE_LAST = 0xFF
FOOTER_DELAY_MS = 0xFA
MAX_PEER_LAG_MS = 0xFA
_SEQUENCE_MASK = 0x0FFFFFFF


@dataclass(frozen=True)
class VirtualWorldBatch:
    datagram: TunnelDatagram
    sequences: tuple[int, ...]


class NetGameLinkWorldState:
    """Own the destination-local virtual window and native timing footer."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def reset_race_state(wire: EndpointWireState) -> None:
        """Reset race-local cadence while preserving connection timing."""
        wire.next_server_virtual_sequence = VIRTUAL_SEQUENCE_FIRST
        wire.last_world_state_footer_tick_ms = 0
        wire.world_state_log_not_before = 0.0
        wire.world_state_footer_diagnostic_logged = False

    @staticmethod
    def take_virtual_sequence(wire: EndpointWireState) -> int:
        sequence = int(wire.next_server_virtual_sequence)
        if not VIRTUAL_SEQUENCE_FIRST <= sequence <= VIRTUAL_SEQUENCE_LAST:
            sequence = VIRTUAL_SEQUENCE_FIRST
        wire.next_server_virtual_sequence = (
            VIRTUAL_SEQUENCE_FIRST
            if sequence == VIRTUAL_SEQUENCE_LAST
            else sequence + 1
        )
        return sequence

    @staticmethod
    def observe_footer(
        wire: EndpointWireState,
        client_footer: bytes,
        received_tick_ms: int,
    ) -> None:
        """Mirror NFSC ``FUN_0098b160`` receive-side timing smoothing."""
        raw = bytes(client_footer)
        if len(raw) < 8:
            return

        received_tick = int(received_tick_ms) & 0xFFFFFFFF
        previous_received_tick = (
            int(wire.world_footer_received_tick_ms) & 0xFFFFFFFF
        )
        wire.world_footer_observation_count += 1
        wire.last_client_footer_received_tick_ms = received_tick
        wire.world_footer_remote_ack_tick_ms = int.from_bytes(raw[4:8], "big")
        wire.world_footer_received_tick_ms = received_tick

        peer_send_tick = int.from_bytes(raw[:4], "big")
        sample = (
            (received_tick & 0xFFFF) - (peer_send_tick & 0xFFFF)
        ) & 0xFFFF
        if sample & 0x8000:
            sample -= 0x10000
        if sample < 0 or sample >= 0x09C5:
            return

        # FUN_0098b640 zero-initializes the previous-receive word. At normal
        # uptime the first valid sample intentionally enters the >=1024 ms
        # reset branch instead of a synthetic first-sample special case.
        receive_gap = (received_tick - previous_received_tick) & 0xFFFFFFFF
        if receive_gap & 0x80000000:
            receive_gap = 10
        else:
            receive_gap = max(10, receive_gap)

        if receive_gap < 0x400:
            old_rtt = max(0, int(wire.world_footer_rtt_avg_ms))
            old_jitter = max(0, int(wire.world_footer_jitter_avg_ms))
            jitter_sample = abs(sample - old_rtt)
            wire.world_footer_jitter_avg_ms = (
                old_jitter * (0x400 - receive_gap)
                + jitter_sample * receive_gap
            ) // 0x400
            wire.world_footer_rtt_avg_ms = (
                old_rtt * (0x400 - receive_gap) + sample * receive_gap
            ) // 0x400
        else:
            wire.world_footer_rtt_avg_ms = sample
            wire.world_footer_jitter_avg_ms = 0

    def build_footer(
        self,
        wire: EndpointWireState,
        destination: Address,
        resolution: CarbonTicketResolution,
        *,
        local_now: int,
    ) -> bytes:
        """Serialize the native NetGameLink world-state footer."""
        current = int(local_now) & 0xFFFFFFFF
        if wire.world_footer_received_tick_ms:
            elapsed = (
                current - int(wire.world_footer_received_tick_ms)
            ) & 0xFFFFFFFF
            estimated_remote_tick = (
                int(wire.world_footer_remote_ack_tick_ms) + elapsed
            ) & 0xFFFFFFFF
        else:
            estimated_remote_tick = current

        # FUN_0098aff0 reads these receive anchors without mutating them.
        latest_client_tick = (
            int.from_bytes(wire.last_client_footer[4:8], "big")
            if len(wire.last_client_footer) >= 8
            else 0
        )
        latest_received_tick = (
            int(wire.last_client_footer_received_tick_ms) & 0xFFFFFFFF
        )
        if latest_client_tick and latest_received_tick:
            latest_elapsed = (current - latest_received_tick) & 0xFFFFFFFF
            if latest_elapsed & 0x80000000:
                latest_elapsed = 0
            remote_tick = (latest_client_tick + latest_elapsed) & 0xFFFFFFFF
        else:
            remote_tick = estimated_remote_tick

        peer_lag = (remote_tick - estimated_remote_tick) & 0xFFFFFFFF
        if (
            latest_client_tick
            and MAX_PEER_LAG_MS < peer_lag < 0x80000000
            and not wire.world_footer_lag_repair_logged
        ):
            self.log.info(
                "Carbon GM release V822 raw peer-clock anchor selected: "
                "gid=%s dst=%s:%d pid=%d estimated=%08x latest=%08x "
                "lag_ms=%d threshold_ms=%d "
                "action=use-last-observed-peer-send-plus-elapsed",
                resolution.game.gid,
                destination[0],
                destination[1],
                resolution.participant.player_id,
                estimated_remote_tick,
                remote_tick,
                peer_lag,
                MAX_PEER_LAG_MS,
            )
            wire.world_footer_lag_repair_logged = True

        send_log_gap = (
            current - int(wire.world_footer_send_log_tick_ms)
        ) & 0xFFFFFFFF
        if wire.world_footer_send_log_tick_ms == 0 or send_log_gap >= 2000:
            self.log.info(
                "Carbon GM release V823 world-footer send: "
                "gid=%s dst=%s:%d pid=%d wire_id=%x count=%d "
                "raw=%s raw_received=%08x local_now=%08x "
                "estimated=%08x selected=%08x elapsed=%d "
                "rtt_avg=%d jitter_avg=%d",
                resolution.game.gid,
                destination[0],
                destination[1],
                resolution.participant.player_id,
                id(wire),
                wire.world_footer_observation_count,
                wire.last_client_footer.hex() or "none",
                latest_received_tick,
                current,
                estimated_remote_tick,
                remote_tick,
                (
                    (current - latest_received_tick) & 0xFFFFFFFF
                    if latest_received_tick
                    else 0
                ),
                wire.world_footer_rtt_avg_ms,
                wire.world_footer_jitter_avg_ms,
            )
            wire.world_footer_send_log_tick_ms = current

        latency = (
            max(0, int(wire.world_footer_rtt_avg_ms))
            + max(0, int(wire.world_footer_jitter_avg_ms))
            + 1
        ) // 2
        return (
            remote_tick.to_bytes(4, "big")
            + current.to_bytes(4, "big")
            + min(latency, 0xFFFF).to_bytes(2, "big")
            + b"\x00\x00"
        )

    def build_virtual_datagram(
        self,
        wire: EndpointWireState,
        destination: Address,
        resolution: CarbonTicketResolution,
        bodies: tuple[bytes, ...] | list[bytes],
        *,
        clock_ms: Callable[[], int],
    ) -> VirtualWorldBatch:
        """Build one opaque type-6/type-7 datagram without reliable advance."""
        packets: list[TunnelPacket] = []
        sequences: list[int] = []
        for logical in bodies:
            sequence = self.take_virtual_sequence(wire)
            sequences.append(sequence)
            local_now = int(clock_ms()) & 0xFFFFFFFF
            last_footer_tick = (
                int(wire.last_world_state_footer_tick_ms) & 0xFFFFFFFF
            )
            footer_due = (
                last_footer_tick == 0
                or ((local_now - last_footer_tick) & 0xFFFFFFFF)
                > FOOTER_DELAY_MS
            )
            if footer_due:
                footer = self.build_footer(
                    wire,
                    destination,
                    resolution,
                    local_now=local_now,
                )
                record = bytes(logical) + footer + b"\x45"
                if not wire.world_state_footer_diagnostic_logged:
                    kind = logical_type(logical)
                    body_handle = int(logical[8]) if len(logical) > 8 else -1
                    self.log.info(
                        "Carbon GM release V823 native world-state footer: "
                        "gid=%s dst=%s:%d pid=%d virtual_seq=%02x ack=%07x "
                        "kind=%s body_handle=%s client_footer=%s "
                        "outbound_footer=%s rtt_avg=%d jitter_avg=%d "
                        "suffix=45 cadence=time>250ms "
                        "transform=native-send-clock",
                        resolution.game.gid,
                        destination[0],
                        destination[1],
                        resolution.participant.player_id,
                        sequence,
                        int(wire.last_client_sequence) & _SEQUENCE_MASK,
                        "none" if kind is None else f"0x{int(kind):02x}",
                        "none" if body_handle < 0 else f"{body_handle:02x}",
                        wire.last_client_footer.hex() or "none",
                        footer.hex(),
                        wire.world_footer_rtt_avg_ms,
                        wire.world_footer_jitter_avg_ms,
                    )
                    wire.world_state_footer_diagnostic_logged = True
                wire.last_world_state_footer_tick_ms = local_now
            else:
                record = bytes(logical) + b"\x05"
            packets.append(
                TunnelPacket(
                    1,
                    encode_active(
                        sequence,
                        int(wire.last_client_sequence) & _SEQUENCE_MASK,
                        record,
                    ),
                )
            )
        return VirtualWorldBatch(
            TunnelDatagram(wire.next_offset_words, tuple(packets)),
            tuple(sequences),
        )
