"""ReadyEpoch creation and native countdown relay for Carbon."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging
import struct
import time
import zlib

from carbon.gamemanager.protocol import OLMessageType, ObservedTimerId
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.ready_seed import ReadySeedCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
)
from carbon.theater.directory import (
    CarbonGame,
    CarbonGameDirectory,
    CarbonTicketResolution,
)
from carbon.transport.commudp import CommUDPActive, game_manager_body
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_READY_TIMER_DEADLINE_TOLERANCE_SECONDS = 0.050

Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
IsHost = Callable[[CarbonTicketResolution], bool]
CurrentActiveGameBody = Callable[[bytes], bytes | None]
StateValue = Callable[[bytes], int | None]
CurrentTimerBody = Callable[[bytes], bytes | None]
TimerLogicalDeadline = Callable[[bytes], float]
RecordCountdownWireTimer = Callable[
    [GameRaceState, bytes],
    tuple[int, float, float],
]
LockRoomAccess = Callable[..., bool]
AbortReadyEpoch = Callable[..., None]
ResetFinishedRace = Callable[[CarbonGame], bool]


class ReadyEpochCoordinator:
    """Own Ready seed generations and native countdown snapshots."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        ready_epochs: MutableMapping[str, ReadyEpoch],
        ready_seed: ReadySeedCoordinator,
        games: CarbonGameDirectory,
        ready_generations: MutableMapping[str, int],
        *,
        session_endpoints: SessionEndpoints,
        is_host: IsHost,
        current_active_game_body: CurrentActiveGameBody,
        state_value: StateValue,
        current_timer_body: CurrentTimerBody,
        timer_logical_deadline: TimerLogicalDeadline,
        record_countdown_wire_timer: RecordCountdownWireTimer,
        lock_room_access: LockRoomAccess,
        abort_ready_epoch: AbortReadyEpoch,
        reset_finished_race: ResetFinishedRace,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self._wire = wires
        self._bindings = bindings
        self._race = races
        self._ready_epochs = ready_epochs
        self.ready_seed = ready_seed
        self.games = games
        self._ready_generations = ready_generations
        self.session_endpoints = session_endpoints
        self._is_host = is_host
        self._current_active_game_body = current_active_game_body
        self._state_value = state_value
        self._current_timer_body = current_timer_body
        self._timer_logical_deadline = timer_logical_deadline
        self._record_countdown_wire_timer = record_countdown_wire_timer
        self._lock_room_access = lock_room_access
        self._abort_ready_epoch = abort_ready_epoch
        self._reset_finished_race_for_rematch = reset_finished_race
        self.log = logger or logging.getLogger(__name__)

    def _append_datagram(
        self,
        replies: Replies,
        datagram: TunnelDatagram,
        destination: Address,
    ) -> None:
        self.publisher.append_datagram(
            replies,
            datagram,
            destination,
            confirmation="ready-epoch-window",
        )

    def relay_native_ready_bundle(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        """Relay the coordinator's exact eight-packet native countdown bundle."""

        game = binding.game
        if not self._is_host(binding):
            return set()
        epoch = self._ready_epochs.get(game.gid)
        if (
            epoch is None
            or epoch.stage
            != ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE
            or int(binding.participant.player_id) != epoch.host_pid
        ):
            return set()
        if len(active_packets) != 8:
            return set()

        logicals = [game_manager_body(item.payload) for item in active_packets]
        kinds = [logical_type(item) for item in logicals]
        expected = [
            OLMessageType.GAME_ATTRIBUTES,
            OLMessageType.START_TIMER,
            OLMessageType.GAME_ATTRIBUTES,
            OLMessageType.ACTIVE_GAME_MESSAGE,
            OLMessageType.ACTIVE_GAME_MESSAGE,
            OLMessageType.START_TIMER,
            OLMessageType.GAME_ATTRIBUTES,
            OLMessageType.ACTIVE_GAME_MESSAGE,
        ]
        if kinds != expected:
            return set()
        flags = [((int(item.sequence) >> 28) & 0xF) for item in active_packets]
        if flags != [0, 0, 0, 0, 1, 2, 0, 0]:
            return set()
        if self._state_value(
            self._current_active_game_body(logicals[3]) or b""
        ) != 7:
            return set()
        if self._state_value(
            self._current_active_game_body(logicals[4]) or b""
        ) != 14:
            return set()
        if self._state_value(
            self._current_active_game_body(logicals[7]) or b""
        ) != 7:
            return set()
        timer = self._current_timer_body(logicals[1])
        if timer is None:
            return set()
        timer_id, _clock, duration = struct.unpack(">Iff", timer[5:17])
        if timer_id != int(ObservedTimerId.RACE_COUNTDOWN):
            return set()
        deadline = self._timer_logical_deadline(timer)
        if (
            abs(deadline - epoch.wire_deadline)
            > _READY_TIMER_DEADLINE_TOLERANCE_SECONDS
        ):
            self.log.info(
                "Carbon GM ReadyEpoch native bundle rejected: gid=%s gen=%d "
                "reason=deadline-drift drift_ms=%.3f",
                game.gid,
                epoch.generation,
                (deadline - epoch.wire_deadline) * 1000.0,
            )
            return set()

        endpoints = self.session_endpoints(game.gid)
        invited_dedicated_flow = game.server_hosted and any(
            not self._is_host(self._bindings[destination])
            and bool(
                self._bindings[destination].participant.invite_remote_player_id
            )
            for destination in endpoints
        )
        for destination in endpoints:
            wire = self._wire[destination]
            destination_binding = self._bindings[destination]
            acknowledgement = (
                int(epoch.guest_state_final_sequence) & _SEQUENCE_MASK
                if (
                    invited_dedicated_flow
                    and not self._is_host(destination_binding)
                    and epoch.guest_state_final_sequence
                )
                else int(wire.last_client_sequence) & _SEQUENCE_MASK
            )
            base = int(wire.next_server_sequence) & _SEQUENCE_MASK
            packets: list[TunnelPacket] = []
            for index, (active, logical) in enumerate(
                zip(active_packets, logicals)
            ):
                flag = (int(active.sequence) >> 28) & 0xF
                sequence = (flag << 28) | (
                    (base + index) & _SEQUENCE_MASK
                )
                packets.append(
                    TunnelPacket(
                        1,
                        encode_active(
                            sequence,
                            acknowledgement,
                            logical + b"\x04",
                        ),
                    )
                )
            self._append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, tuple(packets)),
                destination,
            )
            wire.next_server_sequence = (
                base + len(packets)
            ) & _SEQUENCE_MASK

        race = self._race.setdefault(game.gid, GameRaceState())
        race.attributes = bytes(logicals[0])
        race.countdown_duration = float(duration)
        generation, wire_deadline, drift = self._record_countdown_wire_timer(
            race,
            timer,
        )
        race.countdown_deadline = time.monotonic() + float(duration)
        for destination in endpoints:
            wire = self._wire[destination]
            wire.match_timer_sequence = (
                (int(wire.next_server_sequence) - 3) & _SEQUENCE_MASK
            )
            wire.match_timer_generation_id = generation
            wire.ready_requested = True
            wire.match_timer_retry = None
        epoch.native_bundle_hash = zlib.crc32(b"".join(logicals))
        epoch.wire_deadline = wire_deadline
        epoch.stage = ReadyStage.COUNTDOWN_ACTIVE
        race.begin_countdown(time.monotonic())
        self._lock_room_access(game, race, reason="ready-native-bundle")
        self.log.info(
            "Carbon GM ReadyEpoch native-bundle: gid=%s gen=%d "
            "source_hash=%08x relayed=%d packets_per_endpoint=8 "
            "flags=0,0,0,0,1,2,0,0 timer_id=%d duration=%.3f "
            "timer_generation=%d wire_deadline=%.6f drift=%.6f "
            "room_locked=1 countdown_started=1",
            game.gid,
            epoch.generation,
            epoch.native_bundle_hash,
            len(endpoints),
            timer_id,
            duration,
            generation,
            wire_deadline,
            drift,
        )
        return {id(active) for active in active_packets}

    def relay_native_ready_snapshot(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        """Relay the Ready-owned timer/state7/named-state14 snapshot exactly."""

        game = binding.game
        epoch = self._ready_epochs.get(game.gid)
        if (
            epoch is None
            or epoch.stage != ReadyStage.COUNTDOWN_ACTIVE
            or not self._is_host(binding)
            or int(binding.participant.player_id) != epoch.host_pid
            or len(active_packets) != 3
        ):
            return set()
        logicals = [game_manager_body(item.payload) for item in active_packets]
        flags = [(int(item.sequence) >> 28) & 0xF for item in active_packets]
        if flags != [0, 0, 1]:
            return set()
        timer = self._current_timer_body(logicals[0])
        state7 = self._current_active_game_body(logicals[1])
        state14 = self._current_active_game_body(logicals[2])
        if (
            timer is None
            or int.from_bytes(timer[5:9], "big")
            != int(ObservedTimerId.RACE_COUNTDOWN)
            or state7 is None
            or self._state_value(state7) != 7
            or state14 is None
            or self._state_value(state14) != 14
        ):
            return set()
        deadline = self._timer_logical_deadline(timer)
        drift = deadline - epoch.wire_deadline
        if abs(drift) > _READY_TIMER_DEADLINE_TOLERANCE_SECONDS:
            self.log.info(
                "Carbon GM ReadyEpoch snapshot rejected: gid=%s gen=%d "
                "reason=deadline-drift drift_ms=%.3f",
                game.gid,
                epoch.generation,
                drift * 1000.0,
            )
            return set()
        endpoints = self.session_endpoints(game.gid)
        for destination in endpoints:
            wire = self._wire[destination]
            acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
            base = int(wire.next_server_sequence) & _SEQUENCE_MASK
            packets = tuple(
                TunnelPacket(
                    1,
                    encode_active(
                        (flag << 28) | ((base + index) & _SEQUENCE_MASK),
                        acknowledgement,
                        logical + b"\x04",
                    ),
                )
                for index, (flag, logical) in enumerate(zip(flags, logicals))
            )
            self._append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, packets),
                destination,
            )
            wire.next_server_sequence = (
                base + len(packets)
            ) & _SEQUENCE_MASK
        race = self._race.setdefault(game.gid, GameRaceState())
        _timer_generation, wire_deadline, recorded_drift = (
            self._record_countdown_wire_timer(race, timer)
        )
        epoch.wire_deadline = wire_deadline
        self.log.info(
            "Carbon GM ReadyEpoch snapshot: gid=%s gen=%d deadline=%.6f "
            "drift_ms=%.3f flags=0,0,1 relayed=%d",
            game.gid,
            epoch.generation,
            wire_deadline,
            recorded_drift * 1000.0,
            len(endpoints),
        )
        return {id(active) for active in active_packets}

    def relay_retail_ready_seed(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        """Recognize a retail Challenge Ready seed and publish role windows."""

        game = binding.game
        if not self._is_host(binding):
            return set()
        if str(game.properties.get("B-U-game_type", "")) != "2":
            return set()
        endpoints = self.session_endpoints(game.gid)
        if len(endpoints) < 2:
            return set()
        race = self._race.setdefault(game.gid, GameRaceState())
        if (
            race.phase not in (RacePhase.SESSION_SETUP, RacePhase.FINISHED)
            and game.gid not in self._ready_epochs
        ):
            return set()

        parts = self.ready_seed.parts(game, active_packets)
        if parts is None:
            return set()
        timer, attributes, state1 = parts
        self.games.set_challenge_ready(
            game.gid,
            True,
            reason="retail-ready-seed",
        )

        source_first_sequence = int(active_packets[0].sequence) & _SEQUENCE_MASK
        source_final_sequence = int(active_packets[-1].sequence) & _SEQUENCE_MASK
        source_payload_hash = zlib.crc32(
            b"".join(
                game_manager_body(item.payload) for item in active_packets
            )
        )
        existing_epoch = self._ready_epochs.get(game.gid)
        if (
            existing_epoch is not None
            and existing_epoch.stage != ReadyStage.ABORTED
            and existing_epoch.source_first_sequence == source_first_sequence
            and existing_epoch.source_payload_hash == source_payload_hash
        ):
            self.log.info(
                "Carbon GM ReadyEpoch seed duplicate: gid=%s gen=%d "
                "source_seq=%07x payload_hash=%08x",
                game.gid,
                existing_epoch.generation,
                source_first_sequence,
                source_payload_hash,
            )
            return {id(active) for active in active_packets}
        if existing_epoch is not None:
            self._abort_ready_epoch(game.gid, reason="new-ready-request")
        if race.phase == RacePhase.FINISHED:
            self._reset_finished_race_for_rematch(game)
            race = self._race[game.gid]

        host_binding = next(
            self._bindings[endpoint]
            for endpoint in endpoints
            if self._is_host(self._bindings[endpoint])
        )
        guest_binding = next(
            self._bindings[endpoint]
            for endpoint in endpoints
            if not self._is_host(self._bindings[endpoint])
        )
        guest_endpoint = next(
            endpoint
            for endpoint in endpoints
            if not self._is_host(self._bindings[endpoint])
        )
        generation = self._ready_generations.get(game.gid, 0) + 1
        self._ready_generations[game.gid] = generation
        epoch = ReadyEpoch(
            generation=generation,
            stage=ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
            host_pid=int(host_binding.participant.player_id),
            guest_pid=int(guest_binding.participant.player_id),
            source_first_sequence=source_first_sequence,
            source_final_sequence=source_final_sequence,
            source_payload_hash=source_payload_hash,
            attributes=bytes(attributes),
            wire_deadline=self._timer_logical_deadline(timer),
            guest_pre_state_sequence=(
                int(self._wire[guest_endpoint].last_client_sequence)
                & _SEQUENCE_MASK
            ),
        )
        self._ready_epochs[game.gid] = epoch
        self.log.info(
            "Carbon GM ReadyEpoch create: gid=%s gen=%d host=%d guest=%d "
            "deadline=%.6f source_seq=%07x-%07x payload_hash=%08x",
            game.gid,
            generation,
            epoch.host_pid,
            epoch.guest_pid,
            epoch.wire_deadline,
            source_first_sequence,
            source_final_sequence,
            source_payload_hash,
        )

        host_count = 0
        guest_count = 0
        guest_prelude_flags = "none"
        for destination in endpoints:
            destination_binding = self._bindings[destination]
            if self._is_host(destination_binding):
                self.ready_seed.append_host(
                    replies,
                    destination,
                    game,
                    generation,
                    timer,
                    attributes,
                    state1,
                )
                host_count += 1
            else:
                guest_prelude_flags, _has_latency_pair = (
                    self.ready_seed.append_guest(
                        replies,
                        destination,
                        endpoints,
                        game,
                        generation,
                        timer,
                        attributes,
                        state1,
                    )
                )
                guest_count += 1

        timer_id, _sender_clock, duration = struct.unpack(">Iff", timer[5:17])
        race.attributes = attributes
        race.countdown_duration = float(duration)
        timer_generation, wire_deadline, _drift = (
            self._record_countdown_wire_timer(race, timer)
        )
        for destination in endpoints:
            self._wire[destination].match_timer_generation_id = timer_generation
        guest_used_latency_history = any(
            not self._is_host(self._bindings[endpoint])
            and self._wire[endpoint].ready_seed_used_latency_history
            for endpoint in endpoints
        )
        self.log.info(
            "Carbon GM role-split Ready seed relayed: gid=%s endpoints=%d "
            "host_endpoints=%d guest_endpoints=%d timer_id=%d "
            "host_duration=%.3f guest_duration=%.3f generation=%d "
            "wire_deadline=%.6f host_packets=5 host_flags=0,1,2,0,0 "
            "guest_latency_history=%d guest_prelude=%s "
            "guest_seed_packets=%d guest_seed_flags=2,0,0 "
            "wait_native_state13_state15=1 room_locked=0 "
            "countdown_started=0",
            game.gid,
            len(endpoints),
            host_count,
            guest_count,
            timer_id,
            duration,
            duration,
            timer_generation,
            wire_deadline,
            int(guest_used_latency_history),
            guest_prelude_flags,
            3,
        )
        return {id(active) for active in active_packets}
