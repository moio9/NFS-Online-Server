"""Race countdown, room locking and start synchronization for Carbon.

``RaceStartCoordinator`` and ``RaceEndpoint`` are local server abstractions,
not names recovered from the EA implementation.  The coordinator operates on
explicit room and endpoint snapshots supplied by the UDP service.  It owns
neither the room directory nor ``GameRaceState``; destination-local
serialization is delegated to ``EndpointPublisher`` and AI registration
reliability remains owned by ``AIRegistrationCoordinator``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import math
import struct
import time

from carbon.gamemanager.protocol import (
    NGL_FOOTER_FLAG,
    NGL_FOOTER_WITH_TRAILER,
    OLMessageType,
    ObservedTimerId,
    REDUNDANT_BODY_SEPARATOR,
    logical_message,
    with_plain_terminator,
)
from carbon.gamemanager.race_session import (
    anonymous_state,
    latency_info,
    locked_host_properties,
    logical_type,
    named_state,
    session_attributes,
    start_lock_host_properties,
    start_race_sync,
    start_timer,
)
from carbon.gamemanager.race_state import (
    GameRaceState,
    RacePhase,
    RoomAccess,
)
from carbon.gamemanager.session_codec import encode_active
from carbon.gamemanager.session_object import is_session_object_complete
from carbon.rebroadcaster.ai_registration import AIRegistrationCoordinator
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.retry import RetryPolicy
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
)
from carbon.theater.directory import CarbonGame
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000
_READY_TIMER_DEADLINE_TOLERANCE_SECONDS = 0.050
_ACTIVE_GAME_TIMER_RETRY_POLICY = RetryPolicy(0.5, 0.5, 1, 5.0)

Replies = list[tuple[bytes, Address]]
FooterFor = Callable[[Address], bytes]
HoldPreconfirm = Callable[[Address, bytes], bool]
FinalizeRoom = Callable[[Replies, CarbonGame], None]
LockRoom = Callable[[CarbonGame, GameRaceState, str], bool]
ClockOrigin = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RaceEndpoint:
    """One live endpoint snapshot used by the race-start state machine."""

    address: Address
    player_id: int
    persona: str
    is_host: bool
    wire: EndpointWireState


class RaceStartCoordinator:
    """Publish ordered countdown and race-start transitions."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        ai_registration: AIRegistrationCoordinator,
        *,
        clock_origin: ClockOrigin,
        footer_for: FooterFor,
        hold_preconfirm: HoldPreconfirm,
        finalize_room: FinalizeRoom,
        lock_room: LockRoom,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.ai_registration = ai_registration
        self.clock_origin = clock_origin
        self.footer_for = footer_for
        self.hold_preconfirm = hold_preconfirm
        self.finalize_room = finalize_room
        self.lock_room = lock_room
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def current_timer_body(logical: bytes) -> bytes | None:
        if logical_type(logical) != OLMessageType.START_TIMER or len(logical) < 17:
            return None
        return bytes(logical[:17])

    @staticmethod
    def timer_logical_deadline(timer: bytes) -> float:
        if len(timer) < 17:
            raise ValueError("truncated StartTimer body")
        _timer_id, sender_clock, duration = struct.unpack(">Iff", timer[5:17])
        return float(sender_clock) + float(duration)

    def record_countdown_wire_timer(
        self,
        race: GameRaceState,
        timer: bytes,
    ) -> tuple[int, float, float]:
        """Store one timer-5 snapshot without changing its wire clock."""
        current = bytes(timer[:17])
        deadline = self.timer_logical_deadline(current)
        previous_deadline = float(race.countdown_wire_deadline)
        drift = deadline - previous_deadline if race.countdown_generation_id else 0.0
        if (
            race.countdown_generation_id == 0
            or abs(drift) > _READY_TIMER_DEADLINE_TOLERANCE_SECONDS
        ):
            race.countdown_generation_id += 1
            race.countdown_initial_timer = current
            drift = 0.0
        race.countdown_wire_deadline = deadline
        race.countdown_latest_timer = current
        race.latest_match_timer = current
        return race.countdown_generation_id, deadline, drift

    def seed_countdown(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
    ) -> None:
        endpoints = tuple(endpoints)
        if race.phase >= RacePhase.COUNTDOWN:
            return
        if len(endpoints) < 2:
            return
        race.begin_countdown(time.monotonic())
        attributes = race.attributes or session_attributes(game.properties)
        for endpoint in endpoints:
            bodies = (
                named_state(endpoint.persona, 14),
                anonymous_state(9),
                attributes,
            )
            decorated: list[bytes] = []
            for logical in bodies:
                body = bytes(logical)
                body += endpoint.wire.footer or self.footer_for(endpoint.address)
                body += b"\x44"
                decorated.append(body)
            self.publisher.append_active_bodies(
                replies,
                endpoint.address,
                decorated,
                confirmation="race-countdown-context",
            )
        self.log.info(
            "Carbon GM release countdown context seeded: gid=%s endpoints=%d duration=%.3f deadline=%.3f attributes=%s timer=wait-for-host",
            game.gid,
            len(endpoints),
            race.countdown_duration,
            race.countdown_deadline,
            "captured" if race.attributes else "capabilities-fallback",
        )

    def broadcast_timer(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
        snapshot: bytes,
        *,
        source: Address | None = None,
        ready_epoch: ReadyEpoch | None = None,
    ) -> None:
        """Rebroadcast one Carbon 0x1B timer on the shared server clock."""
        endpoints = tuple(endpoints)
        now = time.monotonic()
        timer_id, sender_clock, duration = struct.unpack(">Iff", snapshot[5:17])
        if (
            timer_id == int(ObservedTimerId.RACE_COUNTDOWN)
            and ready_epoch is not None
            and ready_epoch.stage
            in (
                ReadyStage.SEED_SENT_WAIT_GUEST_13_15,
                ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE,
                ReadyStage.COUNTDOWN_ACTIVE,
            )
        ):
            self.log.info(
                "Carbon GM ReadyEpoch generic timer excluded: gid=%s gen=%d "
                "stage=%s id=%d",
                game.gid,
                ready_epoch.generation,
                ready_epoch.stage.name,
                timer_id,
            )
            return
        source_endpoint = next(
            (endpoint for endpoint in endpoints if endpoint.address == source),
            None,
        )
        if (
            source is not None
            and timer_id == int(ObservedTimerId.ROOM_WAIT_WINDOW)
            and game.server_hosted
            and str(game.properties.get("B-U-game_type", "")) == "2"
            and source_endpoint is not None
            and not source_endpoint.is_host
        ):
            self.log.info(
                "Carbon GM ignored helper-authored room-wait timer: "
                "gid=%s src=%s:%d pid=%d id=%d action=wait-for-host",
                game.gid,
                source[0],
                source[1],
                source_endpoint.player_id,
                timer_id,
            )
            return
        if not math.isfinite(sender_clock) or not math.isfinite(duration) or duration <= 0.0:
            self.log.warning(
                "Carbon GM ignored invalid timer: gid=%s id=%d sender_clock=%r duration=%r",
                game.gid,
                timer_id,
                sender_clock,
                duration,
            )
            return

        timer_role = "generic"
        maximum = 300.0
        if timer_id == int(ObservedTimerId.ROOM_WAIT_WINDOW):
            timer_role = "room-wait"
            maximum = 900.0
        elif timer_id in (
            int(ObservedTimerId.RETAIL_RACE_COUNTDOWN),
            int(ObservedTimerId.RACE_COUNTDOWN),
        ):
            timer_role = "race-countdown"
            maximum = 300.0
        elif timer_id == int(ObservedTimerId.POST_RACE_WINDOW):
            timer_role = "post-race"
            maximum = 300.0

        if duration > maximum:
            self.log.warning(
                "Carbon GM ignored out-of-range %s timer: gid=%s id=%d duration=%r max=%r",
                timer_role,
                game.gid,
                timer_id,
                duration,
                maximum,
            )
            return

        preserve_wire_timer = timer_id == int(ObservedTimerId.RACE_COUNTDOWN)
        timer = (
            bytes(snapshot[:17])
            if preserve_wire_timer
            else start_timer(
                current_seconds=max(0.0, now - self.clock_origin()),
                duration_seconds=duration,
                timer_id=timer_id,
            )
        )
        delivery_endpoints = tuple(
            endpoint
            for endpoint in endpoints
            if not self.hold_preconfirm(endpoint.address, timer)
        )
        generation = 0
        wire_deadline = 0.0
        drift = 0.0
        if preserve_wire_timer:
            generation, wire_deadline, drift = self.record_countdown_wire_timer(
                race,
                timer,
            )
        sent_sequences: dict[Address, int] = {}
        for endpoint in delivery_endpoints:
            sent_sequences[endpoint.address] = self.publisher.append_active_body(
                replies,
                endpoint.address,
                with_plain_terminator(timer),
            )

        if timer_role == "room-wait":
            race.latest_room_timer = timer
            race.room_wait_deadline = now + duration
            if str(game.properties.get("B-U-game_type", "")) == "2":
                self.finalize_room(replies, game)
        elif timer_role == "post-race":
            race.latest_post_race_timer = timer
            race.post_race_deadline = now + duration
        elif timer_role == "race-countdown":
            if race.phase == RacePhase.SESSION_SETUP:
                race.begin_countdown(now)
            if race.phase != RacePhase.COUNTDOWN:
                self.log.info(
                    "Carbon GM ignored race-countdown lifecycle update: gid=%s phase=%s id=%d",
                    game.gid,
                    race.phase.name,
                    timer_id,
                )
                return
            race.countdown_deadline = now + duration
            race.latest_match_timer = timer
            for endpoint in delivery_endpoints:
                if preserve_wire_timer:
                    endpoint.wire.match_timer_sequence = sent_sequences[
                        endpoint.address
                    ]
                    endpoint.wire.match_timer_generation_id = generation
                if not endpoint.is_host and not endpoint.wire.active_game_ready:
                    retry = _ACTIVE_GAME_TIMER_RETRY_POLICY.begin(now)
                    retry.retry_not_before = now
                    endpoint.wire.match_timer_retry = retry
            if (
                str(game.properties.get("B-U-game_type", "")) == "2"
                and len(endpoints) >= 2
            ):
                self.log.info(
                    "Carbon GM release safe co-op countdown timer relayed: "
                    "gid=%s endpoints=%d timer=%s synthetic_bundle=0",
                    game.gid,
                    len(endpoints),
                    timer.hex(),
                )

        self.log.info(
            "Carbon GM release timer %s: gid=%s role=%s id=%d endpoints=%d "
            "sender_clock=%.3f duration=%.3f generation=%d wire_deadline=%.6f "
            "drift=%.6f timer=%s",
            "relayed-exact" if preserve_wire_timer else "rebased",
            game.gid,
            timer_role,
            timer_id,
            len(delivery_endpoints),
            sender_clock,
            duration,
            generation,
            wire_deadline,
            drift,
            timer.hex(),
        )

    def retry_match_timer(
        self,
        replies: Replies,
        gid: str,
        race: GameRaceState,
        endpoint: RaceEndpoint,
        *,
        ready_epoch: ReadyEpoch | None = None,
    ) -> None:
        wire = endpoint.wire
        timer = race.latest_match_timer
        if ready_epoch is not None and ready_epoch.stage != ReadyStage.ABORTED:
            wire.match_timer_retry = None
            return
        retry = wire.match_timer_retry
        if retry is None or not timer:
            return
        now = time.monotonic()
        if not retry.due(now):
            if retry.exhausted(now):
                wire.match_timer_retry = None
            return
        timer_id = int.from_bytes(timer[5:9], "big")
        remaining = float(race.countdown_deadline) - now
        if not math.isfinite(remaining) or remaining < 0.5:
            wire.match_timer_retry = None
            self.log.info(
                "Carbon GM Match Begins timer retry skipped after deadline: "
                "gid=%s dst=%s:%d pid=%d remaining=%.3f",
                gid,
                endpoint.address[0],
                endpoint.address[1],
                endpoint.player_id,
                remaining,
            )
            return
        if timer_id == int(ObservedTimerId.RACE_COUNTDOWN):
            if (
                wire.match_timer_generation_id != race.countdown_generation_id
                or wire.match_timer_sequence == 0
            ):
                wire.match_timer_retry = None
                self.log.info(
                    "Carbon GM Match Begins timer retry skipped for stale generation: "
                    "gid=%s dst=%s:%d endpoint_generation=%d current_generation=%d",
                    gid,
                    endpoint.address[0],
                    endpoint.address[1],
                    wire.match_timer_generation_id,
                    race.countdown_generation_id,
                )
                return
            if self._sequence_acked(
                wire.last_client_acknowledgement,
                wire.match_timer_sequence,
            ):
                wire.match_timer_retry = None
                self.log.info(
                    "Carbon GM Match Begins timer retry skipped after ACK: "
                    "gid=%s dst=%s:%d generation=%d sequence=%07x ack=%07x",
                    gid,
                    endpoint.address[0],
                    endpoint.address[1],
                    race.countdown_generation_id,
                    wire.match_timer_sequence,
                    wire.last_client_acknowledgement,
                )
                return
            refreshed = bytes(timer[:17])
        else:
            refreshed = start_timer(
                current_seconds=max(0.0, now - self.clock_origin()),
                duration_seconds=remaining,
                timer_id=timer_id,
            )
        retry_sequence = self.publisher.append_active_body(
            replies,
            endpoint.address,
            with_plain_terminator(refreshed),
        )
        wire.match_timer_sequence = retry_sequence
        retry.record_retry(now)
        wire.match_timer_retry = None
        self.log.info(
            "Carbon GM release Match Begins timer retried after ActiveGame transition: "
            "gid=%s dst=%s:%d pid=%d remaining=%.3f generation=%d "
            "sequence=%07x wire_deadline=%.6f exact=%d timer=%s",
            gid,
            endpoint.address[0],
            endpoint.address[1],
            endpoint.player_id,
            remaining,
            race.countdown_generation_id,
            retry_sequence,
            race.countdown_wire_deadline,
            int(timer_id == int(ObservedTimerId.RACE_COUNTDOWN)),
            refreshed.hex(),
        )

    def broadcast_ready_lock(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
        source: Address,
    ) -> None:
        endpoints = tuple(endpoints)
        if race.room_access == RoomAccess.LOCKED:
            return
        if len(endpoints) < 2 or any(
            not endpoint.wire.ready_requested for endpoint in endpoints
        ):
            return
        incomplete = [
            endpoint
            for endpoint in endpoints
            if (
                not endpoint.wire.session_confirmed
                or not is_session_object_complete(
                    endpoint.wire.session_blocks.values()
                )
            )
        ]
        if incomplete:
            self.log.info(
                "Carbon GM release ready-lock deferred: gid=%s "
                "reason=session-incomplete pending=%s",
                game.gid,
                ",".join(str(endpoint.player_id) for endpoint in incomplete),
            )
            return
        for endpoint in endpoints:
            locked = locked_host_properties(
                game.session.capacity,
                wire_flag0=False if game.server_hosted else game.is_ranked,
            ).encode()
            body = (
                locked + b"\x04"
                if endpoint.address == source
                else locked
                + (endpoint.wire.footer or self.footer_for(endpoint.address))
                + b"\x44"
            )
            self.publisher.append_active_body(
                replies,
                endpoint.address,
                body,
                confirmation="race-ready-lock",
            )
        self.lock_room(game, race, "ready-hostprops-lock")
        self.log.info(
            "Carbon GM release ready-lock HostProps sent: gid=%s endpoints=%d",
            game.gid,
            len(endpoints),
        )

    def broadcast_start_lock(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
    ) -> None:
        endpoints = tuple(endpoints)
        if race.phase >= RacePhase.START_LOCKED:
            return
        if race.phase != RacePhase.COUNTDOWN_EXPIRED:
            self.log.info(
                "Carbon GM release start-lock deferred: gid=%s phase=%s",
                game.gid,
                race.phase.name,
            )
            return
        if len(endpoints) < 2:
            return
        for endpoint in endpoints:
            self.append_start_lock_bundle(
                replies,
                endpoint.address,
                endpoint.wire,
                max_hosted_players=game.session.capacity,
                wire_flag0=False if game.server_hosted else game.is_ranked,
            )
        race.mark_start_locked()
        self.log.info(
            "Carbon GM release final start-lock bundle sent: gid=%s endpoints=%d",
            game.gid,
            len(endpoints),
        )

    def append_start_lock_bundle(
        self,
        replies: Replies,
        destination: Address,
        wire: EndpointWireState,
        *,
        max_hosted_players: int | None = None,
        wire_flag0: bool = False,
        track_start_lock: bool = True,
    ) -> None:
        footer = wire.footer
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        first_ack = int(wire.last_client_acknowledgement) & _SEQUENCE_MASK
        range_ack = int(wire.last_client_sequence) & _SEQUENCE_MASK
        capacity = 8 if max_hosted_players is None else int(max_hosted_players)
        elements = [
            with_plain_terminator(properties.encode())
            for properties in start_lock_host_properties(
                capacity,
                wire_flag0=wire_flag0,
            )
        ]
        bodies = (
            footer + NGL_FOOTER_FLAG,
            elements[0] + footer + NGL_FOOTER_WITH_TRAILER,
            elements[1]
            + elements[0]
            + REDUNDANT_BODY_SEPARATOR
            + footer
            + NGL_FOOTER_WITH_TRAILER,
            elements[2]
            + elements[1]
            + REDUNDANT_BODY_SEPARATOR
            + elements[0]
            + REDUNDANT_BODY_SEPARATOR,
            (
                elements[3]
                + elements[2]
                + REDUNDANT_BODY_SEPARATOR
                + elements[1]
                + REDUNDANT_BODY_SEPARATOR
                + elements[0]
                + REDUNDANT_BODY_SEPARATOR
                + footer
                + NGL_FOOTER_WITH_TRAILER
            ),
        )
        flags = (0, 1, 2, 2, 4)
        packets: list[TunnelPacket] = []
        for offset, (flag, body) in enumerate(zip(flags, bodies)):
            header = ((flag & 0x0F) << 28) | (
                (base + offset) & _SEQUENCE_MASK
            )
            acknowledgement = first_ack if offset == 0 else range_ack
            packets.append(
                TunnelPacket(1, encode_active(header, acknowledgement, body))
            )
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, tuple(packets)),
            destination,
            confirmation="race-start-lock",
        )
        wire.next_server_sequence = (base + len(packets)) & _SEQUENCE_MASK
        if track_start_lock:
            wire.start_lock_final_sequence = (
                base + len(packets) - 1
            ) & _SEQUENCE_MASK

    def append_post_start_latency_if_acked(
        self,
        replies: Replies,
        gid: str,
        race: GameRaceState,
        endpoint: RaceEndpoint,
    ) -> None:
        wire = endpoint.wire
        target = int(wire.start_lock_final_sequence) & _SEQUENCE_MASK
        if not target or wire.latency_info_sent:
            return
        if not self._sequence_acked(wire.last_client_acknowledgement, target):
            return
        self.publisher.append_active_body(
            replies,
            endpoint.address,
            latency_info(
                endpoint.player_id,
                race.fallback_latency_to_host,
            )
            + (wire.footer or self.footer_for(endpoint.address))
            + b"\x44",
            confirmation="race-post-start-latency",
        )
        wire.latency_info_sent = True
        self.log.info(
            "Carbon GM release latency info sent: gid=%s dst=%s:%d pid=%d ack=%08x target=%08x",
            gid,
            endpoint.address[0],
            endpoint.address[1],
            endpoint.player_id,
            wire.last_client_acknowledgement,
            target,
        )

    def broadcast_startloading(
        self,
        replies: Replies,
        source: Address,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
        body: bytes,
    ) -> None:
        endpoints = tuple(endpoints)
        if race.phase >= RacePhase.LOADING:
            return
        incomplete = [
            endpoint
            for endpoint in endpoints
            if (
                not endpoint.wire.session_confirmed
                or not is_session_object_complete(
                    endpoint.wire.session_blocks.values()
                )
            )
        ]
        if incomplete:
            self.log.info(
                "Carbon GM release StartLoading deferred: gid=%s "
                "reason=session-incomplete pending=%s",
                game.gid,
                ",".join(str(endpoint.player_id) for endpoint in incomplete),
            )
            return
        if race.phase == RacePhase.COUNTDOWN:
            now = time.monotonic()
            remaining = float(race.countdown_deadline) - now
            ready_barrier = (
                len(endpoints) >= 2
                and race.room_access == RoomAccess.LOCKED
                and all(
                    endpoint.wire.ready_requested
                    and endpoint.wire.session_confirmed
                    for endpoint in endpoints
                )
            )
            deadline_valid = (
                math.isfinite(race.countdown_deadline)
                and race.countdown_deadline > 0.0
            )
            if (
                ready_barrier
                and deadline_valid
                and remaining <= max(0.0, float(race.start_delay_seconds))
            ):
                race.mark_countdown_expired()
                self.log.info(
                    "Carbon GM release countdown expiry inferred from host "
                    "StartLoading: gid=%s remaining=%.3f endpoints=%d",
                    game.gid,
                    remaining,
                    len(endpoints),
                )
        if race.phase not in (RacePhase.COUNTDOWN_EXPIRED, RacePhase.START_LOCKED):
            self.log.info(
                "Carbon GM release StartLoading deferred: gid=%s phase=%s "
                "remaining=%.3f room_locked=%d",
                game.gid,
                race.phase.name,
                float(race.countdown_deadline) - time.monotonic(),
                int(race.room_access == RoomAccess.LOCKED),
            )
            return
        source_endpoint = next(
            (endpoint for endpoint in endpoints if endpoint.address == source),
            None,
        )
        if source_endpoint is None:
            return
        for endpoint in endpoints:
            encoded = bytes(body)
            if endpoint.address == source:
                encoded += b"\x04"
            else:
                encoded += (
                    endpoint.wire.footer or self.footer_for(endpoint.address)
                ) + b"\x44"
            self.publisher.append_active_body(
                replies,
                endpoint.address,
                encoded,
                confirmation="race-start-loading",
            )
        loading_started_at = time.monotonic()
        race.mark_loading(
            now=loading_started_at,
            source_player_id=source_endpoint.player_id,
        )
        self.log.info(
            "Carbon GM release StartLoading broadcast: gid=%s host=%s:%d endpoints=%d player=%d",
            game.gid,
            source[0],
            source[1],
            len(endpoints),
            int.from_bytes(body[5:9], "big"),
        )

    def observe_startloading_signal(
        self,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
        source: Address,
        body: bytes,
    ) -> None:
        """Record a non-host loading echo without rebroadcasting it."""

        endpoints = tuple(endpoints)
        source_endpoint = next(
            (endpoint for endpoint in endpoints if endpoint.address == source),
            None,
        )
        if source_endpoint is None or race.phase != RacePhase.LOADING:
            self.log.info(
                "Carbon GM StartLoading signal deferred: gid=%s src=%s:%d "
                "phase=%s endpoint=%d",
                game.gid,
                source[0],
                source[1],
                race.phase.name,
                int(source_endpoint is not None),
            )
            return
        added = race.observe_loading_player(source_endpoint.player_id)
        self.log.info(
            "Carbon GM StartLoading signal observed: gid=%s src=%s:%d "
            "pid=%d wire_player=%d loading=%d/%d duplicate=%d",
            game.gid,
            source[0],
            source[1],
            source_endpoint.player_id,
            int.from_bytes(body[5:9], "big"),
            len(race.loading_player_ids),
            len(endpoints),
            int(not added),
        )

    def poll_loading_ready_fallback(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
        *,
        now: float,
        fallback_seconds: float,
    ) -> bool:
        """Finish a stalled loading barrier after every live peer entered it."""

        endpoints = tuple(endpoints)
        if race.phase != RacePhase.LOADING or race.loading_started_at <= 0.0:
            return False
        elapsed = float(now) - float(race.loading_started_at)
        if elapsed < float(fallback_seconds):
            return False
        current_player_ids = {endpoint.player_id for endpoint in endpoints}
        if (
            len(endpoints) < 2
            or not current_player_ids.issubset(race.loading_player_ids)
        ):
            return False
        incomplete = [
            endpoint.player_id
            for endpoint in endpoints
            if (
                not endpoint.wire.session_confirmed
                or not is_session_object_complete(
                    endpoint.wire.session_blocks.values()
                )
            )
        ]
        if incomplete:
            return False
        missing_ready = [
            endpoint
            for endpoint in endpoints
            if not endpoint.wire.race_ready_seen
        ]
        if not missing_ready:
            return False
        for endpoint in missing_ready:
            endpoint.wire.race_ready_seen = True
        source_endpoint = next(
            (endpoint for endpoint in endpoints if endpoint.is_host),
            endpoints[0],
        )
        self.log.warning(
            "Carbon GM loading READY fallback released: gid=%s elapsed=%.3f "
            "endpoints=%d loading=%s missing_ready=%s",
            game.gid,
            elapsed,
            len(endpoints),
            ",".join(str(item) for item in sorted(current_player_ids)),
            ",".join(str(endpoint.player_id) for endpoint in missing_ready),
        )
        self.maybe_broadcast_startsync(
            replies,
            source_endpoint.address,
            game,
            race,
            endpoints,
        )
        return race.phase == RacePhase.RACING

    def maybe_broadcast_startsync(
        self,
        replies: Replies,
        source: Address,
        game: CarbonGame,
        race: GameRaceState,
        endpoints: Sequence[RaceEndpoint],
    ) -> None:
        endpoints = tuple(endpoints)
        if race.phase >= RacePhase.RACING or race.phase != RacePhase.LOADING:
            return
        if len(endpoints) < 2 or any(
            not endpoint.wire.race_ready_seen for endpoint in endpoints
        ):
            return
        for endpoint in endpoints:
            self.ai_registration.update_delivery(
                replies,
                endpoint.address,
                endpoint.wire,
                race,
                gid=game.gid,
                player_id=endpoint.player_id,
                force_retry=True,
                reason="pre-start",
            )
        self.ai_registration.refresh_ready_guests(
            replies,
            gid=game.gid,
            race=race,
            guests=tuple(
                (endpoint.address, endpoint.player_id, endpoint.wire)
                for endpoint in endpoints
                if not endpoint.is_host
            ),
        )
        clock_ms = int(time.monotonic() * 1000.0) & 0xFFFFFFFF
        body = start_race_sync(
            clock_ms,
            start_delay_seconds=race.start_delay_seconds,
            ping=race.start_sync_ping,
        )
        ready = logical_message(OLMessageType.READY)
        for endpoint in endpoints:
            reflected_ready = ready
            if endpoint.address == source:
                reflected_ready += b"\x04"
            else:
                reflected_ready += (
                    endpoint.wire.footer or self.footer_for(endpoint.address)
                ) + b"\x44"
            self.publisher.append_active_body(
                replies,
                endpoint.address,
                reflected_ready,
                confirmation="race-ready-reflection",
            )

            start_bundle = body + b"\x04" + ready
            if endpoint.address == source:
                start_bundle += b"\x04"
            else:
                start_bundle += (
                    endpoint.wire.footer or self.footer_for(endpoint.address)
                ) + b"\x44"
            self.publisher.append_active_body(
                replies,
                endpoint.address,
                start_bundle,
                confirmation="race-start-sync",
            )
            endpoint.wire.gameplay_ready = True
        race.mark_racing()
        self.log.info(
            "Carbon GM release StartRaceSync broadcast: gid=%s endpoints=%d clock=%08x",
            game.gid,
            len(endpoints),
            clock_ms,
        )

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF
