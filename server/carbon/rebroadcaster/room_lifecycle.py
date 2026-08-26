"""Room-scoped lifecycle transitions for the Carbon rebroadcaster."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, MutableSet
import logging

from carbon.gamemanager.protocol import (
    NGL_FOOTER_FLAG,
    PLAIN_TERMINATOR,
)
from carbon.gamemanager.race_session import reopen_host_properties
from carbon.gamemanager.race_state import (
    GameRaceState,
    RacePhase,
    RoomAccess,
)
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.race_results import RaceResultCoordinator
from carbon.rebroadcaster.session_objects import SessionObjectCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
    SourceKey,
)
from carbon.rebroadcaster.world_state import NetGameLinkWorldState
from carbon.theater.directory import (
    CarbonGame,
    CarbonGameDirectory,
    CarbonTicketResolution,
)
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF

Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
FooterFor = Callable[[Address, CarbonTicketResolution], bytes]
ClearRoomCommit = Callable[[str], None]


class RoomLifecycleCoordinator:
    """Own room access, Ready aborts, rematches and room-scoped cleanup."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        games: CarbonGameDirectory,
        session_objects: SessionObjectCoordinator,
        race_results: RaceResultCoordinator,
        world_state: NetGameLinkWorldState,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        ready_epochs: MutableMapping[str, ReadyEpoch],
        ready_generations: MutableMapping[str, int],
        participant_endpoints: MutableMapping[SourceKey, Address],
        published_joins: MutableSet[tuple[str, int]],
        reconnect_pending: MutableSet[SourceKey],
        joiner_state13_windows: MutableSet[SourceKey],
        guest_countdown_transitions: MutableSet[str],
        unhandled_diagnostics: MutableSet[tuple[str, int, str, str]],
        *,
        session_endpoints: SessionEndpoints,
        footer_for: FooterFor,
        clear_room_commit: ClearRoomCommit,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.games = games
        self.session_objects = session_objects
        self.race_results = race_results
        self.world_state = world_state
        self._wire = wires
        self._bindings = bindings
        self._race = races
        self._ready_epochs = ready_epochs
        self._ready_generations = ready_generations
        self._participant_endpoints = participant_endpoints
        self._published_joins = published_joins
        self._reconnect_pending = reconnect_pending
        self._joiner_state13_window_sent = joiner_state13_windows
        self._guest_countdown_transition_sent = guest_countdown_transitions
        self._unhandled_message_diagnostics = unhandled_diagnostics
        self.session_endpoints = session_endpoints
        self._footer_for = footer_for
        self._clear_room_commit = clear_room_commit
        self.log = logger or logging.getLogger(__name__)

    def clear_unbound_transport_state(self, gid: str) -> None:
        """Drop active race state while preserving reconnect intent."""

        game_id = str(gid)
        self.session_objects.clear_room(game_id)
        self._race.pop(game_id, None)
        self.race_results.discard(game_id)
        self._joiner_state13_window_sent.intersection_update(
            key
            for key in self._joiner_state13_window_sent
            if key[0] != game_id
        )
        self._ready_epochs.pop(game_id, None)
        self._ready_generations.pop(game_id, None)
        self._guest_countdown_transition_sent.discard(game_id)

    def retire_transport_state(self, gid: str) -> None:
        """Discard every room-scoped cache after authoritative retirement."""

        game_id = str(gid)
        self.clear_unbound_transport_state(game_id)
        self._clear_room_commit(game_id)
        self._published_joins.intersection_update(
            key for key in self._published_joins if key[0] != game_id
        )
        self._reconnect_pending.intersection_update(
            key for key in self._reconnect_pending if key[0] != game_id
        )
        for key in tuple(self._participant_endpoints):
            if key[0] == game_id:
                self._participant_endpoints.pop(key, None)
        self._unhandled_message_diagnostics.intersection_update(
            item
            for item in self._unhandled_message_diagnostics
            if item[0] != game_id
        )

    def lock_room_access(
        self,
        game: CarbonGame,
        race: GameRaceState,
        *,
        reason: str,
    ) -> bool:
        """Close both the GameManager room and its public PlayNow listing."""

        changed = race.lock_room_access()
        challenge_open_after_ready = (
            str(game.properties.get("B-U-game_type", "")) == "2"
            and game.challenge_ready
            and self.games.challenge_quick_join_after_ready
        )
        self.games.set_quick_join_locked(
            game.gid,
            not challenge_open_after_ready,
            reason=(
                f"{reason}:challenge-after-ready-open"
                if challenge_open_after_ready
                else reason
            ),
        )
        return changed

    def abort_ready_epoch(self, gid: str, *, reason: str) -> None:
        epoch = self._ready_epochs.pop(gid, None)
        previous_stage = epoch.stage if epoch is not None else None
        if epoch is not None:
            epoch.stage = ReadyStage.ABORTED
            for endpoint in self.session_endpoints(gid):
                wire = self._wire.get(endpoint)
                if wire is None:
                    continue
                wire.ready_epoch_generation = 0
                wire.ready_requested = False
                wire.active_game_ready = False
                wire.match_timer_retry = None
                wire.match_timer_sequence = 0
                wire.match_timer_generation_id = 0
                wire.ready_seed_final_sequence = 0
                wire.ready_seed_used_latency_history = False
        race = self._race.get(gid)
        countdown_reset = False
        if race is not None and race.phase <= RacePhase.COUNTDOWN:
            countdown_reset = race.phase == RacePhase.COUNTDOWN
            race.phase = RacePhase.SESSION_SETUP
            race.room_access = RoomAccess.OPEN
            game = self.games.get(gid)
            if (
                game is not None
                and str(game.properties.get("B-U-game_type", "")) == "2"
            ):
                self.games.set_challenge_ready(
                    gid,
                    False,
                    reason=f"ready-abort:{reason}",
                )
            else:
                self.games.set_quick_join_locked(
                    gid,
                    False,
                    reason=f"ready-abort:{reason}",
                )
            race.countdown_deadline = 0.0
            race.latest_match_timer = b""
            race.countdown_wire_deadline = 0.0
            race.countdown_generation_id = 0
            race.countdown_initial_timer = b""
            race.countdown_latest_timer = b""
        self._guest_countdown_transition_sent.discard(gid)
        if epoch is not None:
            self.log.info(
                "Carbon GM ReadyEpoch abort: gid=%s gen=%d stage=%s reason=%s",
                gid,
                epoch.generation,
                previous_stage.name,
                reason,
            )
        elif countdown_reset:
            self.log.info(
                "Carbon GM orphan countdown reset: gid=%s reason=%s "
                "phase=SESSION_SETUP room=open timer=cleared",
                gid,
                reason,
            )

    def reset_finished_race_for_rematch(self, game: CarbonGame) -> bool:
        """Reset race-local state while preserving the live room transport."""

        gid = game.gid
        previous = self._race.get(gid)
        if previous is None or previous.phase != RacePhase.FINISHED:
            return False

        previous_ai_count = len(previous.player_controlled_ai)
        had_results = gid in self.race_results.trackers
        self._race[gid] = GameRaceState()
        self.race_results.discard(gid)
        self._guest_countdown_transition_sent.discard(gid)
        self._clear_room_commit(gid)

        for endpoint in self.session_endpoints(gid):
            wire = self._wire.get(endpoint)
            if wire is None:
                continue
            # Keep tunnel offsets, reliable sequences, the authenticated
            # binding and session-object generations. Only race-owned gates
            # may be reset for another event in the same room.
            wire.ready_requested = False
            wire.start_lock_final_sequence = 0
            wire.latency_info_sent = False
            wire.race_ready_seen = False
            wire.gameplay_ready = False
            wire.active_game_ready = False
            wire.match_timer_retry = None
            wire.match_timer_sequence = 0
            wire.match_timer_generation_id = 0
            wire.ready_seed_final_sequence = 0
            wire.ready_seed_used_latency_history = False
            wire.pending_ai_registration_windows.clear()
            wire.ai_registration_ready_refresh_sent = False
            wire.ready_epoch_generation = 0
            self.world_state.reset_race_state(wire)
            wire.pursuit_tag_log_not_before = 0.0

        self.log.info(
            "Carbon GM release V830 rematch state reset: "
            "gid=%s endpoints=%d previous_phase=FINISHED "
            "ai_cars=%d results=%d transport=preserved",
            gid,
            len(self.session_endpoints(gid)),
            previous_ai_count,
            int(had_results),
        )
        return True

    def reopen_finished_room(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
    ) -> bool:
        """Apply the native post-race join/invite availability transition."""

        game = binding.game
        race = self._race.setdefault(game.gid, GameRaceState())
        if race.phase != RacePhase.FINISHED:
            return False

        self.abort_ready_epoch(
            game.gid,
            reason="post-race-enable-joins",
        )
        if not self.reset_finished_race_for_rematch(game):
            return False
        self._race[game.gid].post_race_reopened = True
        if str(game.properties.get("B-U-game_type", "")) == "2":
            self.games.set_challenge_ready(
                game.gid,
                False,
                reason="post-race-enable-joins",
            )
        else:
            self.games.set_quick_join_locked(
                game.gid,
                False,
                reason="post-race-enable-joins",
            )

        properties = tuple(
            item.encode()
            for item in reopen_host_properties(
                game.session.capacity,
                wire_flag0=False if game.server_hosted else game.is_ranked,
            )
        )
        endpoints = self.session_endpoints(game.gid)
        for destination in endpoints:
            wire = self._wire[destination]
            acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
            base = int(wire.next_server_sequence) & _SEQUENCE_MASK
            if destination == source:
                packets = tuple(
                    TunnelPacket(
                        1,
                        encode_active(
                            (index << 28)
                            | ((base + index) & _SEQUENCE_MASK),
                            acknowledgement,
                            self.publisher.commudp_aggregate_payload(
                                tuple(reversed(properties[: index + 1]))
                            ),
                        ),
                    )
                    for index in range(len(properties))
                )
            else:
                footer = wire.footer or self._footer_for(
                    destination,
                    self._bindings[destination],
                )
                footer_record = footer + NGL_FOOTER_FLAG
                records = tuple(item + PLAIN_TERMINATOR for item in properties)
                bodies = (
                    footer_record,
                    records[0]
                    + footer_record
                    + bytes((len(footer_record),)),
                    records[1]
                    + records[0]
                    + bytes((len(records[0]),))
                    + footer_record
                    + bytes((len(footer_record),)),
                    records[2]
                    + records[1]
                    + bytes((len(records[1]),))
                    + records[0]
                    + bytes((len(records[0]),)),
                    records[3]
                    + records[2]
                    + bytes((len(records[2]),))
                    + records[1]
                    + bytes((len(records[1]),)),
                )
                flags = (0, 1, 2, 2, 2)
                packets = tuple(
                    TunnelPacket(
                        1,
                        encode_active(
                            ((flag & 0x0F) << 28)
                            | ((base + index) & _SEQUENCE_MASK),
                            acknowledgement,
                            body,
                        ),
                    )
                    for index, (flag, body) in enumerate(zip(flags, bodies))
                )
            self.publisher.append_datagram(
                replies,
                TunnelDatagram(wire.next_offset_words, packets),
                destination,
                confirmation="room-reopen",
            )
            wire.next_server_sequence = (base + len(packets)) & _SEQUENCE_MASK

        self.log.info(
            "Carbon GM release V832 post-race room reopened: "
            "gid=%s source=%s:%d endpoints=%d "
            "source_flags=0,1,2,3 peer_flags=0,1,2,2,2 "
            "state=join+presence+invites-open",
            game.gid,
            source[0],
            source[1],
            len(endpoints),
        )
        return True
