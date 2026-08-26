"""Application-message router for bound Carbon GameManager endpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, MutableSet
import logging
import time

from carbon.gamemanager.protocol import (
    GMMessageType,
    OLMessageType,
    ObservedActiveGameState,
    with_plain_terminator,
)
from carbon.gamemanager.race_session import (
    contains_logical_type,
    decode_session_attributes,
    logical_type,
    session_attributes,
)
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.results import (
    FINAL_GAME_RESULTS,
    GAME_RESULTS,
    LEADER_FINISHED,
    RACER_FINISHED,
)
from carbon.gamemanager.session_object import is_session_object_complete
from carbon.progression import RaceAwards
from carbon.rebroadcaster.gameplay_relay import GameplayRelayCoordinator
from carbon.rebroadcaster.invite_session import InviteSessionBarrierCoordinator
from carbon.rebroadcaster.race_results import (
    RaceResultCoordinator,
    ResultOutcome,
)
from carbon.rebroadcaster.race_start import RaceEndpoint, RaceStartCoordinator
from carbon.rebroadcaster.room_commit import RoomCommitCoordinator
from carbon.rebroadcaster.room_lifecycle import RoomLifecycleCoordinator
from carbon.rebroadcaster.session_bootstrap import SessionBootstrapCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
)
from carbon.theater.directory import (
    CarbonGame,
    CarbonGameDirectory,
    CarbonTicketResolution,
)
from carbon.theater.matchmaking import (
    CHALLENGE_ROOM_IDENTITY,
    RACE_PROPERTY_GAME_MODE,
    selected_challenge_event,
    selected_race_property,
)
from carbon.transport.commudp import CommUDPActive, game_manager_body


Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
IsHost = Callable[[CarbonTicketResolution], bool]
FooterFor = Callable[[Address, CarbonTicketResolution], bytes]
AppendActiveBody = Callable[[Replies, Address, bytes], int]


CHALLENGE_EVENT_IDENTITY_FIELDS = (
    "game_mode",
    "car_tier",
    "length",
    "track",
    "n2o",
    "collision_detection",
    "location",
    "race_type_circuit",
    "race_type_sprint",
    "race_type_canyon_due",
    "race_type_speedtrap",
    "race_type_knockout",
    "race_type_pursuit_tag",
)

RACE_SELECTION_IDENTITY_FIELDS = frozenset(
    ("game_mode", "track", *RACE_PROPERTY_GAME_MODE)
)

class ActiveMessageRouter:
    """Route bound OL messages to race, room and gameplay coordinators."""

    def __init__(
        self,
        append_active_body: AppendActiveBody,
        games: CarbonGameDirectory,
        session_bootstrap: SessionBootstrapCoordinator,
        invite_session: InviteSessionBarrierCoordinator,
        gameplay_relay: GameplayRelayCoordinator,
        race_results: RaceResultCoordinator,
        room_commit: RoomCommitCoordinator,
        room_lifecycle: RoomLifecycleCoordinator,
        race_start: RaceStartCoordinator,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        ready_epochs: Mapping[str, ReadyEpoch],
        unhandled_diagnostics: MutableSet[tuple[str, int, str, str]],
        *,
        session_endpoints: SessionEndpoints,
        is_host: IsHost,
        footer_for: FooterFor,
        logger: logging.Logger | None = None,
    ) -> None:
        self._append_active_body = append_active_body
        self.games = games
        self.session_bootstrap = session_bootstrap
        self.invite_session = invite_session
        self.gameplay_relay = gameplay_relay
        self.race_results = race_results
        self.room_commit = room_commit
        self.room_lifecycle = room_lifecycle
        self.race_start = race_start
        self._wire = wires
        self._bindings = bindings
        self._race = races
        self._ready_epochs = ready_epochs
        self._unhandled_message_diagnostics = unhandled_diagnostics
        self.session_endpoints = session_endpoints
        self._is_host = is_host
        self._footer_for = footer_for
        self.log = logger or logging.getLogger(__name__)

    def record_race_result(
        self,
        gid: str,
        *,
        event_type: int,
        winner_profile_ids: set[int] | tuple[int, ...] | list[int],
    ) -> RaceAwards:
        game = self.games.get(str(gid))
        if game is None:
            raise KeyError(f"unknown Carbon game {gid}")
        return self.race_results.award_race(
            game,
            event_type=int(event_type),
            winner_profile_ids=winner_profile_ids,
        )

    def _endpoint_snapshot(
        self,
        address: Address,
        binding: CarbonTicketResolution | None = None,
    ) -> RaceEndpoint:
        resolution = self._bindings[address] if binding is None else binding
        return RaceEndpoint(
            address=address,
            player_id=resolution.participant.player_id,
            persona=resolution.participant.identity.persona,
            is_host=self._is_host(resolution),
            wire=self._wire[address],
        )

    def _endpoint_snapshots(self, gid: str) -> tuple[RaceEndpoint, ...]:
        return tuple(
            self._endpoint_snapshot(address)
            for address in self.session_endpoints(gid)
        )

    def broadcast_room_timer(
        self,
        replies: Replies,
        game: CarbonGame,
        snapshot: bytes,
        *,
        source: Address | None = None,
    ) -> None:
        self.race_start.broadcast_timer(
            replies,
            game,
            self._race.setdefault(game.gid, GameRaceState()),
            self._endpoint_snapshots(game.gid),
            snapshot,
            source=source,
            ready_epoch=self._ready_epochs.get(game.gid),
        )

    def retry_match_timer(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.race_start.retry_match_timer(
            replies,
            binding.game.gid,
            self._race.setdefault(binding.game.gid, GameRaceState()),
            self._endpoint_snapshot(addr, binding),
            ready_epoch=self._ready_epochs.get(binding.game.gid),
        )

    def broadcast_ready_lock(
        self,
        replies: Replies,
        game: CarbonGame,
        source: Address,
    ) -> None:
        self.race_start.broadcast_ready_lock(
            replies,
            game,
            self._race.setdefault(game.gid, GameRaceState()),
            self._endpoint_snapshots(game.gid),
            source,
        )

    def broadcast_start_lock(self, replies: Replies, game: CarbonGame) -> None:
        self.race_start.broadcast_start_lock(
            replies,
            game,
            self._race.setdefault(game.gid, GameRaceState()),
            self._endpoint_snapshots(game.gid),
        )

    def broadcast_startloading(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
        body: bytes,
    ) -> None:
        self.race_start.broadcast_startloading(
            replies,
            source,
            binding.game,
            self._race.setdefault(binding.game.gid, GameRaceState()),
            self._endpoint_snapshots(binding.game.gid),
            body,
        )

    def observe_startloading_signal(
        self,
        source: Address,
        binding: CarbonTicketResolution,
        body: bytes,
    ) -> None:
        self.race_start.observe_startloading_signal(
            binding.game,
            self._race.setdefault(binding.game.gid, GameRaceState()),
            self._endpoint_snapshots(binding.game.gid),
            source,
            body,
        )

    def maybe_broadcast_startsync(
        self,
        replies: Replies,
        game: CarbonGame,
        source: Address,
    ) -> None:
        self.race_start.maybe_broadcast_startsync(
            replies,
            source,
            game,
            self._race.setdefault(game.gid, GameRaceState()),
            self._endpoint_snapshots(game.gid),
        )

    @staticmethod
    def _state_value(logical: bytes) -> int | None:
        if (
            logical_type(logical) != OLMessageType.ACTIVE_GAME_MESSAGE
            or len(logical) < 11
        ):
            return None
        name_length = int.from_bytes(logical[5:7], "big")
        state_offset = 7 + name_length
        if state_offset + 4 > len(logical):
            return None
        return int.from_bytes(logical[state_offset : state_offset + 4], "big")

    @staticmethod
    def _current_active_game_body(logical: bytes) -> bytes | None:
        if (
            logical_type(logical) != OLMessageType.ACTIVE_GAME_MESSAGE
            or len(logical) < 11
        ):
            return None
        name_length = int.from_bytes(logical[5:7], "big")
        end = 7 + name_length + 4
        if end > len(logical):
            return None
        return bytes(logical[:end])

    def _publish_result_outcome(
        self,
        replies: Replies,
        game: CarbonGame,
        race: GameRaceState,
        outcome: ResultOutcome,
    ) -> None:
        for logical in outcome.publications:
            for destination in self.session_endpoints(game.gid):
                destination_binding = self._bindings[destination]
                destination_wire = self._wire[destination]
                self._append_active_body(
                    replies,
                    destination,
                    logical
                    + (
                        destination_wire.footer
                        or self._footer_for(destination, destination_binding)
                    )
                    + b"\x44",
                )
        self.race_results.commit(outcome, race)

    def _handle_sync_or_result(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        race: GameRaceState,
        logical: bytes,
        kind: int | OLMessageType | None,
    ) -> bool:
        if kind == OLMessageType.CLOCK_SYNC_START and len(logical) >= 9:
            self.invite_session.handle_session_token(
                replies,
                addr,
                binding,
                logical[5:9],
            )
            return True
        if kind == LEADER_FINISHED:
            outcome = self.race_results.handle_leader_finished(
                addr,
                binding,
                race,
                logical,
            )
            self._publish_result_outcome(replies, binding.game, race, outcome)
            if outcome.accepted_leader is not None:
                self._publish_result_outcome(
                    replies,
                    binding.game,
                    race,
                    self.race_results.finalize_if_complete(binding.game),
                )
            return True
        if kind in (GAME_RESULTS, FINAL_GAME_RESULTS):
            self._publish_result_outcome(
                replies,
                binding.game,
                race,
                self.race_results.handle_result_report(addr, binding, logical),
            )
            return True
        if kind == RACER_FINISHED:
            endpoints = self.session_endpoints(binding.game.gid)
            self._publish_result_outcome(
                replies,
                binding.game,
                race,
                self.race_results.handle_client_racer_finished(
                    addr,
                    binding,
                    race,
                    logical,
                    endpoint_count=len(endpoints),
                ),
            )
            return True
        if kind == OLMessageType.PURSUIT_TAG_SYNC:
            self.gameplay_relay.relay_pursuit_tag_sync(
                replies,
                addr,
                binding,
                logical,
            )
            return True
        if kind == OLMessageType.LATENCY_INFO and len(logical) >= 13:
            wire.latest_latency_info = logical[:13]
            if (
                bool(binding.participant.invite_remote_player_id)
                and str(binding.game.properties.get("B-U-game_type", "")) == "2"
                and wire.session_bootstrap_sent
                and not wire.session_confirmed
            ):
                self.log.info(
                    "Carbon GM release invite preconfirm LatencyInfo consumed: "
                    "gid=%s src=%s:%d pid=%d "
                    "action=preserve-host-continuation-ack",
                    binding.game.gid,
                    addr[0],
                    addr[1],
                    binding.participant.player_id,
                )
                return True
            self._append_active_body(
                replies,
                addr,
                with_plain_terminator(logical[:13]),
            )
            self.gameplay_relay.relay_logical_to_peers(
                replies,
                addr,
                binding,
                logical[:13],
                footer=False,
                confirmation="session-latency-info",
            )
            return True
        if kind == OLMessageType.START_TIMER and len(logical) >= 17:
            self.broadcast_room_timer(
                replies,
                binding.game,
                logical[:17],
                source=addr,
            )
            return True
        return False

    def _handle_active_game(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        race: GameRaceState,
        logical: bytes,
        kind: int | OLMessageType | None,
    ) -> bool:
        if kind != OLMessageType.ACTIVE_GAME_MESSAGE:
            return False

        state = self._state_value(logical)
        if (
            state == ObservedActiveGameState.COUNTDOWN_EXPIRED
            and self._is_host(binding)
        ):
            if race.phase == RacePhase.COUNTDOWN:
                self.gameplay_relay.reflect_logical_to_room(
                    replies,
                    addr,
                    binding,
                    logical[:11],
                    confirmation="active-game-countdown-expired",
                )
                race.mark_countdown_expired()
                self.log.info(
                    "Carbon GM release captured countdown state2 reflected: "
                    "gid=%s endpoints=%d",
                    binding.game.gid,
                    len(self.session_endpoints(binding.game.gid)),
                )
            return True
        if state == ObservedActiveGameState.ACTIVE_GAME_ALLOCATING:
            wire.active_game_ready = True
            if wire.match_timer_retry is not None:
                wire.match_timer_retry.defer_from(time.monotonic())
        elif wire.active_game_ready:
            self.retry_match_timer(replies, addr, binding)

        defer_challenge_state7 = (
            state == 7
            and self._is_host(binding)
            and str(binding.game.properties.get("B-U-game_type", "")) == "2"
            and not race.room_commit_sent
        )
        delivery = "reflected"
        if defer_challenge_state7:
            current_state7 = self._current_active_game_body(logical)
            if current_state7 is None or self._state_value(current_state7) != 7:
                self.log.warning(
                    "Carbon GM rejected malformed Challenge state7: "
                    "gid=%s src=%s:%d bytes=%d",
                    binding.game.gid,
                    addr[0],
                    addr[1],
                    len(logical),
                )
                return True
            race.pending_coop_host_state7 = current_state7
            race.coop_host_state7_seen = True
            self.room_commit.maybe_finalize_room_session(replies, binding.game)
            endpoints = self.session_endpoints(binding.game.gid)
            helper_wires = [
                self._wire[endpoint]
                for endpoint in endpoints
                if not self._is_host(self._bindings[endpoint])
            ]
            delivery = (
                "commit-published"
                if race.room_commit_sent
                else "deferred-for-coop-commit"
            )
            if not race.room_commit_sent:
                self.log.info(
                    "Carbon GM release Challenge state7 gate: gid=%s "
                    "guest_session=%d host_token=%d helper_allocation=%d "
                    "attributes=%d timer=%d pending_releases=%d action=defer",
                    binding.game.gid,
                    int(
                        bool(helper_wires)
                        and all(item.session_confirmed for item in helper_wires)
                    ),
                    int(len(race.coop_barrier_token) == 4),
                    int(
                        bool(helper_wires)
                        and all(
                            item.allocation_lock_triggered
                            for item in helper_wires
                        )
                    ),
                    int(bool(race.attributes)),
                    int(bool(race.latest_room_timer)),
                    sum(
                        len(self._wire[endpoint].pending_session_releases)
                        for endpoint in endpoints
                    ),
                )
        else:
            self.gameplay_relay.reflect_logical_to_room(
                replies,
                addr,
                binding,
                logical,
                confirmation="active-game-state",
            )
        if state in (6, 7, ObservedActiveGameState.PLAYER_COUNTDOWN_CONTEXT):
            self.log.info(
                "Carbon GM release ActiveGame countdown context handled: "
                "gid=%s state=%s endpoints=%d bytes=%d action=%s",
                binding.game.gid,
                state,
                len(self.session_endpoints(binding.game.gid)),
                len(logical),
                delivery,
            )
        return True

    def _handle_attributes(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        race: GameRaceState,
        logical: bytes,
        kind: int | OLMessageType | None,
    ) -> bool:
        if kind != OLMessageType.GAME_ATTRIBUTES:
            return False
        if self._is_host(binding):
            try:
                decoded = decode_session_attributes(logical)
            except Exception as exc:
                self.log.warning(
                    "Carbon GM rejected malformed host room attributes: "
                    "gid=%s src=%s:%d error=%s",
                    binding.game.gid,
                    addr[0],
                    addr[1],
                    exc,
                )
                return True
            allocated_challenge = (
                str(binding.game.properties.get("B-U-game_type", "")) == "2"
            )
            normalized_fields: list[str] = []
            challenge_event_action = "not-applicable"

            def normalize_field(name: str, required: object) -> None:
                value = str(required)
                if decoded.get(name) != value:
                    decoded[name] = value
                    normalized_fields.append(name)

            required_fields: dict[str, str] = {}
            if allocated_challenge:
                # game_mode=0 is only the initial Sprint allocation default.
                # The host's concrete race_type_* event chooses the final mode;
                # forcing zero beside a Speedtrap/Circuit event crashes NFSC's
                # event resolver and renders the invite as Sprint.
                required_fields = {
                    name: value
                    for name, value in CHALLENGE_ROOM_IDENTITY.items()
                    if name not in {"game_mode", "max_online_player"}
                }
            elif decoded.get("game_type") == "2":
                required_fields = {"help_type": "0"}

            for name, required in required_fields.items():
                normalize_field(name, required)

            if allocated_challenge:
                challenge_event = selected_challenge_event(decoded)
                selected_event = challenge_event
                if race.post_race_reopened:
                    race_property = selected_race_property(decoded)
                    if race_property is not None:
                        event = str(decoded.get(race_property, "")).strip()
                        selected_event = (race_property, event)

                # The helper's allocation window can make the host republish
                # an older local Challenge snapshot.  Keep the pre-join
                # settings only while such a helper is still waiting for its
                # room commit.  Once every current helper is committed (or no
                # helper is present), later host attributes are real settings
                # edits and must resize/update both the directory and guests.
                pending_helper_commit = any(
                    endpoint not in race.coop_committed_helpers
                    for endpoint in self.session_endpoints(binding.game.gid)
                    if not self._is_host(self._bindings[endpoint])
                )
                settings_update_allowed = not pending_helper_commit

                def apply_host_capacity(reason: str) -> int:
                    try:
                        requested_capacity = int(
                            str(decoded.get("max_online_player", "2"))
                        )
                    except ValueError:
                        requested_capacity = int(binding.game.session.capacity)
                    applied_capacity = self.games.set_authoritative_capacity(
                        binding.game.gid,
                        requested_capacity,
                        reason=reason,
                    )
                    if applied_capacity is None:
                        applied_capacity = int(binding.game.session.capacity)
                    race.challenge_capacity = applied_capacity
                    normalize_field(
                        "max_online_player",
                        str(applied_capacity),
                    )
                    return applied_capacity

                if (
                    selected_event is not None
                    and (
                        not race.challenge_event_identity
                        or settings_update_allowed
                    )
                ):
                    race_property, event = selected_event
                    first_event = not race.challenge_event_identity
                    apply_host_capacity(
                        "first-host-challenge-event"
                        if first_event
                        else "host-settings-update"
                    )
                    for candidate in RACE_PROPERTY_GAME_MODE:
                        normalize_field(
                            candidate,
                            event if candidate == race_property else "ABSTAIN",
                        )
                    normalize_field(
                        "game_mode",
                        RACE_PROPERTY_GAME_MODE[race_property],
                    )
                    race.challenge_event_identity = {
                        name: str(decoded.get(name, ""))
                        for name in CHALLENGE_EVENT_IDENTITY_FIELDS
                    }
                    if first_event:
                        action = "captured"
                    elif race.post_race_reopened:
                        action = "rematch-updated"
                    else:
                        action = "settings-updated"
                    challenge_event_action = f"{action}:{race_property}:{event}"
                elif (
                    settings_update_allowed
                    and race.challenge_event_identity
                ):
                    # Settings controls can briefly publish every race slot as
                    # ABSTAIN between two concrete selections. Preserve only
                    # the last event/mode through that transient snapshot;
                    # length, N2O, collision and the other mutable settings
                    # remain authoritative and are relayed immediately.
                    apply_host_capacity("host-settings-transient-selection")
                    changed: list[str] = []
                    for name in RACE_SELECTION_IDENTITY_FIELDS:
                        required = race.challenge_event_identity.get(name)
                        if required is None or decoded.get(name) == required:
                            continue
                        decoded[name] = required
                        normalized_fields.append(name)
                        changed.append(name)
                    race.challenge_event_identity = {
                        name: str(decoded.get(name, ""))
                        for name in CHALLENGE_EVENT_IDENTITY_FIELDS
                    }
                    challenge_event_action = (
                        "settings-selection-preserved:"
                        + (",".join(changed) if changed else "stable")
                    )
                elif race.challenge_event_identity:
                    # Only the uncommitted helper join window freezes the full
                    # settings snapshot.  This is the narrow interval where
                    # stock Carbon republishes stale local Bronze/Silver/Gold
                    # data while allocating the helper session object.
                    normalize_field(
                        "max_online_player",
                        str(
                            race.challenge_capacity
                            or binding.game.session.capacity
                        ),
                    )
                    changed: list[str] = []
                    for name, required in race.challenge_event_identity.items():
                        if decoded.get(name) != required:
                            decoded[name] = required
                            normalized_fields.append(name)
                            changed.append(name)
                    challenge_event_action = (
                        "join-window-normalized:" + ",".join(changed)
                        if changed
                        else "join-window-stable"
                    )
                else:
                    # No trustworthy Challenge event exists yet. Replace the
                    # transient local normal-race snapshot with the neutral
                    # allocation identity rather than publishing/locking it.
                    stale_events = [
                        f"{name}:{str(decoded.get(name, '')).strip()}"
                        for name in RACE_PROPERTY_GAME_MODE
                        if str(decoded.get(name, "")).strip().upper()
                        not in {"", "ABSTAIN"}
                    ]
                    for name in CHALLENGE_EVENT_IDENTITY_FIELDS:
                        if name == "game_mode":
                            safe_value = CHALLENGE_ROOM_IDENTITY["game_mode"]
                        elif name == "track":
                            safe_value = ""
                        elif name in RACE_PROPERTY_GAME_MODE:
                            safe_value = "ABSTAIN"
                        else:
                            safe_value = binding.game.properties.get(
                                f"B-U-{name}",
                                decoded.get(name, ""),
                            )
                        normalize_field(name, safe_value)
                    normalize_field(
                        "max_online_player",
                        str(binding.game.session.capacity),
                    )
                    challenge_event_action = (
                        "deferred:no-valid-cs-event"
                        + (":" + ",".join(stale_events) if stale_events else "")
                    )

            # Keep the host's concrete event identity identical in Theater and
            # GameManager.  Retail GNOT does not carry a second room snapshot,
            # so changing the slot only on this transport leaves the invitee
            # with two different event identities for the same room.
            wire_decoded = dict(decoded)
            wire_identity_normalized: list[str] = []
            if allocated_challenge:
                challenge_event = selected_challenge_event(decoded)
                concrete_event_property = (
                    challenge_event[0] if challenge_event is not None else None
                )
            else:
                concrete_event_property = selected_race_property(decoded)

            if normalized_fields or wire_identity_normalized:
                normalized = dict(binding.game.properties)
                for name, value in wire_decoded.items():
                    if name == "version":
                        normalized["B-version"] = value
                        normalized["B-U-version"] = value
                    else:
                        normalized[f"B-U-{name}"] = value
                logical = session_attributes(normalized)
            race.attributes = logical
            for name, value in decoded.items():
                if name == "version":
                    binding.game.properties["B-version"] = value
                    binding.game.properties["B-U-version"] = value
                else:
                    binding.game.properties[f"B-U-{name}"] = value
            wire_event_property = selected_race_property(wire_decoded)
            wire_event_identity = (
                f"{wire_event_property}:"
                f"{wire_decoded.get(wire_event_property, '')}"
                if wire_event_property is not None
                else "none"
            )
            invite_event_identity = (
                f"{concrete_event_property}:"
                f"{decoded.get(concrete_event_property, '')}"
                if concrete_event_property is not None
                else "none"
            )
            self.log.info(
                "Carbon GM authoritative room attributes captured: "
                "gid=%s game_type=%s matchmaking_state=%s help_type=%s "
                "game_mode=%s invite_game_mode=%s max_players=%s car_tier=%s "
                "sprint=%s circuit=%s event=%s invite_event=%s "
                "challenge_identity_normalized=%s wire_identity_normalized=%s "
                "challenge_event=%s",
                binding.game.gid,
                wire_decoded.get("game_type", "?"),
                wire_decoded.get("matchmaking_state", "?"),
                wire_decoded.get("help_type", "?"),
                wire_decoded.get("game_mode", "?"),
                decoded.get("game_mode", "?"),
                wire_decoded.get("max_online_player", "?"),
                wire_decoded.get("car_tier", "?"),
                wire_decoded.get("race_type_sprint", "?"),
                wire_decoded.get("race_type_circuit", "?"),
                wire_event_identity,
                invite_event_identity,
                ",".join(normalized_fields) if normalized_fields else "none",
                (
                    ",".join(wire_identity_normalized)
                    if wire_identity_normalized
                    else "none"
                ),
                challenge_event_action,
            )
            if wire.session_confirmed:
                self.session_bootstrap.append_initial_hostprops(
                    replies,
                    addr,
                    binding,
                )
                self._append_active_body(
                    replies,
                    addr,
                    bytes(logical)
                    + (wire.footer or self._footer_for(addr, binding))
                    + b"\x44",
                )
            self.gameplay_relay.relay_logical_to_peers(
                replies,
                addr,
                binding,
                logical,
                footer=True,
                confirmation="room-attributes",
            )
            if str(binding.game.properties.get("B-U-game_type", "")) == "2":
                self.room_commit.maybe_finalize_room_session(
                    replies,
                    binding.game,
                )
            return True

        authoritative = race.attributes or session_attributes(
            binding.game.properties
        )
        if self.invite_session.hold_preconfirm(addr, authoritative):
            return True
        self._append_active_body(
            replies,
            addr,
            bytes(authoritative)
            + (wire.footer or self._footer_for(addr, binding))
            + b"\x44",
        )
        self.log.info(
            "Carbon GM helper room attributes replaced with authoritative "
            "snapshot: gid=%s pid=%d",
            binding.game.gid,
            binding.participant.player_id,
        )
        return True

    def _handle_ready_controls(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        race: GameRaceState,
        logical: bytes,
        kind: int | OLMessageType | None,
    ) -> bool:
        if kind == OLMessageType.CAR_STATE:
            return True
        if kind == OLMessageType.START_LOADING and len(logical) >= 9:
            if self._is_host(binding):
                self.broadcast_startloading(replies, addr, binding, logical[:9])
            else:
                self.observe_startloading_signal(addr, binding, logical[:9])
            return True
        if kind == OLMessageType.READY:
            wire.race_ready_seen = True
            self.maybe_broadcast_startsync(replies, binding.game, addr)
            return True
        if kind in (
            OLMessageType.MATCHMAKING_ON_REQUEST,
            OLMessageType.INVITES_ON_REQUEST,
            OLMessageType.ENABLE_JOINS_REQUEST,
        ):
            if (
                kind == OLMessageType.ENABLE_JOINS_REQUEST
                and race.phase == RacePhase.FINISHED
            ):
                self.room_lifecycle.reopen_finished_room(
                    replies,
                    addr,
                    binding,
                )
            return True

        has_matchmaking_off = contains_logical_type(
            logical,
            OLMessageType.MATCHMAKING_OFF_REQUEST,
        )
        has_disable_joins = contains_logical_type(
            logical,
            OLMessageType.DISABLE_JOINS_REQUEST,
        )
        challenge_pre_ready_control = (
            str(binding.game.properties.get("B-U-game_type", "")) == "2"
            and race.phase == RacePhase.SESSION_SETUP
            and binding.game.gid not in self._ready_epochs
        )
        ready_request = has_matchmaking_off or (
            has_disable_joins and not challenge_pre_ready_control
        )
        final_start_request = contains_logical_type(
            logical,
            OLMessageType.INVITES_OFF_REQUEST,
        )
        if challenge_pre_ready_control and (
            (has_disable_joins and not has_matchmaking_off)
            or final_start_request
        ):
            self.log.info(
                "Carbon GM Challenge setup availability control ignored: "
                "gid=%s src=%s:%d disable_joins=%d invites_off=%d "
                "action=wait-for-retail-ready-seed",
                binding.game.gid,
                addr[0],
                addr[1],
                int(has_disable_joins),
                int(final_start_request),
            )
        if ready_request and not final_start_request:
            if str(binding.game.properties.get("B-U-game_type", "")) == "2":
                self.games.set_challenge_ready(
                    binding.game.gid,
                    True,
                    reason="native-ready-control",
                )
            wire.ready_requested = True
            if has_matchmaking_off and has_disable_joins:
                endpoints = self.session_endpoints(binding.game.gid)
                incomplete = [
                    endpoint
                    for endpoint in endpoints
                    if (
                        not self._wire[endpoint].session_confirmed
                        or not is_session_object_complete(
                            self._wire[endpoint].session_blocks.values()
                        )
                    )
                ]
                if incomplete:
                    self.log.info(
                        "Carbon GM release compound ready deferred: gid=%s "
                        "source=%s:%d reason=session-incomplete pending=%s",
                        binding.game.gid,
                        addr[0],
                        addr[1],
                        ",".join(
                            str(
                                self._bindings[endpoint].participant.player_id
                            )
                            for endpoint in incomplete
                        ),
                    )
                else:
                    for endpoint in endpoints:
                        self._wire[endpoint].ready_requested = True
                    self.log.info(
                        "Carbon GM release compound ready accepted as room "
                        "lock: gid=%s source=%s:%d",
                        binding.game.gid,
                        addr[0],
                        addr[1],
                    )
            self.broadcast_ready_lock(replies, binding.game, addr)
            if not self._is_host(binding):
                self.retry_match_timer(replies, addr, binding)
        if final_start_request and not challenge_pre_ready_control:
            self.broadcast_start_lock(replies, binding.game)
        return bool(
            has_matchmaking_off
            or has_disable_joins
            or final_start_request
            or kind == OLMessageType.BIG_MESSAGE
        )

    def _log_unhandled(
        self,
        addr: Address,
        binding: CarbonTicketResolution,
        race: GameRaceState,
        active: CommUDPActive,
        logical: bytes,
        kind: int | OLMessageType | None,
    ) -> None:
        outer_type = (
            int(active.game_manager.message_type)
            if active.game_manager is not None
            else None
        )
        if outer_type in (
            int(GMMessageType.SESSION_TICKET),
            int(GMMessageType.PLAYER_PUBLISH),
        ) or not logical:
            return
        if kind is None:
            kind_name = "none"
        elif int(kind) in OLMessageType._value2member_map_:
            kind_name = OLMessageType(int(kind)).name
        else:
            kind_name = f"0x{int(kind):02x}"
        outer_name = (
            GMMessageType(outer_type).name
            if outer_type in GMMessageType._value2member_map_
            else (f"0x{outer_type:02x}" if outer_type is not None else "none")
        )
        diagnostic_key = (
            binding.game.gid,
            binding.participant.player_id,
            race.phase.name,
            f"{outer_name}/{kind_name}",
        )
        if diagnostic_key in self._unhandled_message_diagnostics:
            return
        self._unhandled_message_diagnostics.add(diagnostic_key)
        self.log.warning(
            "Carbon GM unhandled inbound message: gid=%s src=%s:%d pid=%d "
            "phase=%s outer=%s kind=%s logical_len=%d logical=%s",
            binding.game.gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
            race.phase.name,
            outer_name,
            kind_name,
            len(logical),
            bytes(logical[:128]).hex(),
        )

    def handle(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
        active: CommUDPActive,
    ) -> None:
        logical = game_manager_body(active.payload)
        kind = logical_type(logical)
        wire = self._wire[addr]
        race = self._race.setdefault(binding.game.gid, GameRaceState())

        for handler in (
            self._handle_sync_or_result,
            self._handle_active_game,
            self._handle_attributes,
            self._handle_ready_controls,
        ):
            if handler(
                replies,
                addr,
                binding,
                wire,
                race,
                logical,
                kind,
            ):
                return
        self._log_unhandled(addr, binding, race, active, logical, kind)
