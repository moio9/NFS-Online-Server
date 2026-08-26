"""Ready state13/state15 delivery gates for Carbon endpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging

from carbon.gamemanager.protocol import (
    DESTINATION_FOOTER_FLAG,
    NGL_FOOTER_FLAG,
    NGL_FOOTER_WITH_TRAILER,
    OLMessageType,
    PLAIN_TERMINATOR,
    REDUNDANT_BODY_SEPARATOR,
)
from carbon.gamemanager.race_session import logical_type, named_state
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.session_codec import encode_active
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.ready_epoch import ReadyEpochCoordinator
from carbon.rebroadcaster.ready_seed import ReadySeedCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
    SourceKey,
)
from carbon.theater.directory import (
    CarbonGame,
    CarbonGameDirectory,
    CarbonTicketResolution,
)
from carbon.transport.commudp import CommUDPActive, game_manager_body
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000

SessionEndpoints = Callable[[str], tuple[Address, ...]]
IsHost = Callable[[CarbonTicketResolution], bool]
ActiveGameBodies = Callable[[bytes], tuple[bytes, ...]]
CurrentActiveGameBody = Callable[[bytes], bytes | None]
StateName = Callable[[bytes], str | None]
StateValue = Callable[[bytes], int | None]
FooterFor = Callable[[Address, CarbonTicketResolution], bytes]
CurrentTimerBody = Callable[[bytes], bytes | None]
TimerLogicalDeadline = Callable[[bytes], float]
RecordCountdownWireTimer = Callable[
    [GameRaceState, bytes],
    tuple[int, float, float],
]
LockRoomAccess = Callable[..., bool]
AbortReadyEpoch = Callable[..., None]
ResetFinishedRace = Callable[[CarbonGame], bool]


class ReadyStateCoordinator:
    """Own helper state13/state15 publication and Ready-seed ACK gating."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        ready_epochs: MutableMapping[str, ReadyEpoch],
        joiner_state13_windows: set[SourceKey],
        ready_seed: ReadySeedCoordinator,
        games: CarbonGameDirectory,
        ready_generations: MutableMapping[str, int],
        *,
        session_endpoints: SessionEndpoints,
        is_host: IsHost,
        active_game_bodies: ActiveGameBodies,
        current_active_game_body: CurrentActiveGameBody,
        state_name: StateName,
        state_value: StateValue,
        footer_for: FooterFor,
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
        self._joiner_state13_window_sent = joiner_state13_windows
        self.session_endpoints = session_endpoints
        self._is_host = is_host
        self._active_game_bodies = active_game_bodies
        self._current_active_game_body = current_active_game_body
        self._state_name = state_name
        self._state_value = state_value
        self._footer_for = footer_for
        self.log = logger or logging.getLogger(__name__)
        self.epochs = ReadyEpochCoordinator(
            publisher,
            wires,
            bindings,
            races,
            ready_epochs,
            ready_seed,
            games,
            ready_generations,
            session_endpoints=session_endpoints,
            is_host=is_host,
            current_active_game_body=current_active_game_body,
            state_value=state_value,
            current_timer_body=current_timer_body,
            timer_logical_deadline=timer_logical_deadline,
            record_countdown_wire_timer=record_countdown_wire_timer,
            lock_room_access=lock_room_access,
            abort_ready_epoch=abort_ready_epoch,
            reset_finished_race=reset_finished_race,
            logger=self.log,
        )

    def _append_datagram(
        self,
        replies: list[tuple[bytes, Address]],
        datagram: TunnelDatagram,
        destination: Address,
    ) -> None:
        self.publisher.append_datagram(
            replies,
            datagram,
            destination,
            confirmation="ready-state-window",
        )

    @staticmethod
    def _take_server_sequence(wire: EndpointWireState) -> int:
        return EndpointPublisher.take_server_sequence(wire)

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF

    def relay_joiner_state13_window(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        """Complete the helper's initial ActiveGame allocation handshake.

        Retail normally sends a clean named state 13 followed by named state 5
        with state 13 in reliable history. Live clients do not always keep both
        records in the same ProtoTunnel datagram: retransmission/coalescing can
        leave only the clean state 13, or only the state5+state13 companion, in
        the current receive call. The server can construct state 5 from the
        authenticated persona, so requiring both packets atomically is unsafe.

        The reply still keeps the retail wire shape: guest flags 0,1 and host
        flags 2,2 with real one-byte history lengths. Any companion/retransmit
        observed after the transaction is consumed and transport-ACKed instead
        of being relayed as an extra application sequence.
        """
        game = binding.game
        if self._is_host(binding):
            return set()
        if str(game.properties.get("B-U-game_type", "")) != "2":
            return set()
        if game.gid in self._ready_epochs:
            return set()
        race = self._race.setdefault(game.gid, GameRaceState())
        if race.phase != RacePhase.SESSION_SETUP or not race.room_commit_sent:
            return set()
        endpoints = self.session_endpoints(game.gid)
        if len(endpoints) < 2:
            return set()

        persona = binding.participant.identity.persona
        source_key = (game.gid, binding.participant.identity.user_id)
        state13: bytes | None = None
        client_state5: bytes | None = None
        matched: set[int] = set()

        for active in active_packets:
            logical = game_manager_body(active.payload)
            current = self._current_active_game_body(logical)
            if current is None:
                continue

            current_state = self._state_value(current)
            current_name = self._state_name(current)
            histories = self._active_game_bodies(logical)[1:]
            historical_state13 = next(
                (
                    body
                    for body in histories
                    if self._state_value(body) == 13
                    and self._state_name(body) == persona
                ),
                None,
            )

            if current_state == 13 and current_name == persona:
                state13 = state13 or current
                matched.add(id(active))
                continue

            if (
                current_state == 5
                and current_name == persona
                and historical_state13 is not None
            ):
                client_state5 = current
                state13 = state13 or historical_state13
                matched.add(id(active))

        # Once the retail transaction has already been sent, consume only its
        # late companion/retransmit records. This prevents an extra generic
        # state5/state13 relay while still allowing the normal transport ACK.
        if source_key in self._joiner_state13_window_sent:
            return matched

        if state13 is None or self._state_name(state13) != persona:
            return set()

        # A split receive may contain only state 13. State 5 is deterministic
        # and is tied to the authenticated participant identity.
        state5 = client_state5 or named_state(persona, 5)

        guest_wire = self._wire[source]

        # Validate the complete coordinator side before publishing anything.
        # This keeps the guest and host parts of the transaction atomic.
        host_windows: list[
            tuple[Address, EndpointWireState, int, int, bytes, bytes, bytes]
        ] = []
        for destination in endpoints:
            if destination == source:
                continue
            destination_binding = self._bindings[destination]
            if not self._is_host(destination_binding):
                continue
            wire = self._wire[destination]
            helper_latency = bytes(guest_wire.latest_latency_info)
            host_latency = bytes(wire.latest_latency_info)
            if not helper_latency or not host_latency:
                self.log.warning(
                    "Carbon GM joiner state13 window deferred: gid=%s "
                    "helper_latency=%d host_latency=%d",
                    game.gid,
                    int(bool(helper_latency)),
                    int(bool(host_latency)),
                )
                return set()
            host_windows.append(
                (
                    destination,
                    wire,
                    int(wire.next_server_sequence) & _SEQUENCE_MASK,
                    int(wire.last_client_sequence) & _SEQUENCE_MASK,
                    wire.footer or self._footer_for(destination, destination_binding),
                    helper_latency,
                    host_latency,
                )
            )
        if not host_windows:
            return set()

        guest_ack = int(guest_wire.last_client_sequence) & _SEQUENCE_MASK
        guest_base = int(guest_wire.next_server_sequence) & _SEQUENCE_MASK
        guest_footer = guest_wire.footer or self._footer_for(source, binding)
        guest_state13_record = state13 + guest_footer + DESTINATION_FOOTER_FLAG
        if len(guest_state13_record) > 0xFF:
            return set()

        guest_packets = (
            TunnelPacket(
                1,
                encode_active(guest_base, guest_ack, guest_state13_record),
            ),
            TunnelPacket(
                1,
                encode_active(
                    0x10000000 | ((guest_base + 1) & _SEQUENCE_MASK),
                    guest_ack,
                    state5
                    + PLAIN_TERMINATOR
                    + guest_state13_record
                    + bytes((len(guest_state13_record),)),
                ),
            ),
        )

        # Prebuild host packets so a length failure cannot leave the guest half
        # of the transaction published by itself.
        host_datagrams: list[
            tuple[Address, EndpointWireState, int, TunnelDatagram]
        ] = []
        for (
            destination,
            wire,
            base,
            acknowledgement,
            footer,
            helper_latency,
            host_latency,
        ) in host_windows:
            helper_record = helper_latency + PLAIN_TERMINATOR
            host_record = host_latency + PLAIN_TERMINATOR
            host_state13_record = state13 + footer + DESTINATION_FOOTER_FLAG
            if any(
                len(item) > 0xFF
                for item in (helper_record, host_record, host_state13_record)
            ):
                return set()

            host_packets = (
                TunnelPacket(
                    1,
                    encode_active(
                        0x20000000 | base,
                        acknowledgement,
                        host_state13_record
                        + helper_record
                        + bytes((len(helper_record),))
                        + host_record
                        + bytes((len(host_record),)),
                    ),
                ),
                TunnelPacket(
                    1,
                    encode_active(
                        0x20000000 | ((base + 1) & _SEQUENCE_MASK),
                        acknowledgement,
                        state5
                        + PLAIN_TERMINATOR
                        + host_state13_record
                        + bytes((len(host_state13_record),))
                        + helper_record
                        + bytes((len(helper_record),)),
                    ),
                ),
            )
            host_datagrams.append(
                (
                    destination,
                    wire,
                    base,
                    TunnelDatagram(wire.next_offset_words, host_packets),
                )
            )

        self._append_datagram(
            replies,
            TunnelDatagram(guest_wire.next_offset_words, guest_packets),
            source,
        )
        guest_wire.next_server_sequence = (guest_base + 2) & _SEQUENCE_MASK

        for destination, wire, base, datagram in host_datagrams:
            self._append_datagram(replies, datagram, destination)
            wire.next_server_sequence = (base + 2) & _SEQUENCE_MASK

        self._joiner_state13_window_sent.add(source_key)
        self.log.info(
            "Carbon GM initial guest ActiveGame allocation acknowledged: gid=%s "
            "guest=%s:%d states=13,5+13 guest_flags=0,1 host_flags=2,2 "
            "host_endpoints=%d host_packets=%d input_packets=%d split_safe=1",
            game.gid,
            source[0],
            source[1],
            len(host_datagrams),
            len(host_datagrams) * 2,
            len(matched),
        )
        return matched

    def relay_clean_state13_ack(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        """Relay native state13/state15 after every Ready seed is acknowledged."""
        game = binding.game
        epoch = self._ready_epochs.get(game.gid)
        if epoch is None:
            return set()

        if epoch.stage != ReadyStage.SEED_SENT_WAIT_GUEST_13_15:
            return set()

        state13: bytes | None = None
        state15: bytes | None = None
        matched: set[int] = set()
        is_guest_source = (
            not self._is_host(binding)
            and int(binding.participant.player_id) == epoch.guest_pid
        )
        if is_guest_source:
            guest_persona = binding.participant.identity.persona
            starts_native_state_window = False
            for active in active_packets:
                logical = game_manager_body(active.payload)
                kind = logical_type(logical)
                if kind in (
                    OLMessageType.ENABLE_JOINS_REQUEST,
                    OLMessageType.MATCHMAKING_ON_REQUEST,
                ):
                    starts_native_state_window = True
                for current in self._active_game_bodies(logical):
                    state = self._state_value(current)
                    state_name = self._state_name(current)
                    if state == 13 and state_name == guest_persona:
                        state13 = current
                        starts_native_state_window = True
                        matched.add(id(active))
                    elif state == 15 and state_name == "":
                        state15 = current
                        starts_native_state_window = True
                        matched.add(id(active))
            if not epoch.guest_state_window_started:
                if starts_native_state_window:
                    epoch.guest_state_window_started = True
                else:
                    epoch.guest_pre_state_sequence = (
                        int(self._wire[source].last_client_sequence)
                        & _SEQUENCE_MASK
                    )

        # V804 targeted completion: V803 proved that omitting the malformed
        # destination-history Ready prelude keeps the invited PC helper alive.
        # In that safe flow the helper publishes the native named state 13 but
        # consistently omits only the anonymous state 15, leaving ReadyEpoch in
        # SEED_SENT_WAIT_GUEST_13_15 and preventing the retail Match Starting
        # countdown.  Do not reintroduce the crashing prelude and do not invent
        # both client states.  Cache the authentic helper state 13, then fill
        # only the missing fixed-format anonymous state 15.  The normal retail
        # 0x8281 control, state pair and host-native eight-packet bundle remain
        # mandatory after this point.
        if state13 is not None:
            epoch.state13 = state13
        if state15 is not None:
            epoch.state15 = state15
        if is_guest_source and (state13 is not None or state15 is not None):
            epoch.guest_state_final_sequence = (
                int(self._wire[source].last_client_sequence) & _SEQUENCE_MASK
            )
        if not epoch.state13 or not epoch.state15:
            return matched

        endpoints = self.session_endpoints(game.gid)
        if len(endpoints) < 2:
            return set()

        # In both official Ready captures, each destination has cumulatively
        # ACKed its complete Ready seed before the server opens the state-pair
        # window.  A fast local helper can publish native state 13 while the
        # host still acknowledges only the pre-seed sequence.  Sending flags
        # 0,1,2 into that outstanding five-packet host window makes both PC
        # clients close Theater and CommUDP before the coordinator can emit
        # its native eight-packet countdown bundle.  Cache the authentic
        # helper states and release the pair from the first later transport
        # packet that proves every destination accepted its seed.  This is a
        # delivery gate only; the official control remains asynchronous.
        pending_seed_acks: list[str] = []
        for destination in endpoints:
            destination_wire = self._wire[destination]
            target = int(destination_wire.ready_seed_final_sequence) & _SEQUENCE_MASK
            if target and not self._sequence_acked(
                destination_wire.last_client_acknowledgement,
                target,
            ):
                destination_binding = self._bindings[destination]
                pending_seed_acks.append(
                    "%s:%d/%s=%07x<%07x"
                    % (
                        destination[0],
                        destination[1],
                        "host" if self._is_host(destination_binding) else "guest",
                        int(destination_wire.last_client_acknowledgement)
                        & _SEQUENCE_MASK,
                        target,
                    )
                )
        if pending_seed_acks:
            if not epoch.seed_ack_wait_logged:
                self.log.info(
                    "Carbon GM ReadyEpoch state-pair deferred: gid=%s gen=%d "
                    "pending_seed_acks=%s action=wait-for-complete-ready-seed",
                    game.gid,
                    epoch.generation,
                    ",".join(pending_seed_acks),
                )
                epoch.seed_ack_wait_logged = True
            return matched

        state13 = epoch.state13
        state15 = epoch.state15
        native_source = next(
            (
                destination
                for destination in endpoints
                if int(self._bindings[destination].participant.player_id)
                == epoch.guest_pid
            ),
            source,
        )
        self.log.info(
            "Carbon GM ReadyEpoch guest-states: gid=%s gen=%d "
            "native_source=%s:%d release_trigger=%s:%d "
            "state13=native state15=native",
            game.gid,
            epoch.generation,
            native_source[0],
            native_source[1],
            source[0],
            source[1],
        )

        control = bytes.fromhex("018c000000828100000002")
        invited_dedicated_flow = game.server_hosted and any(
            not self._is_host(self._bindings[destination])
            and bool(
                self._bindings[
                    destination
                ].participant.invite_remote_player_id
            )
            for destination in endpoints
        )
        for destination in endpoints:
            wire = self._wire[destination]
            if wire.ready_epoch_generation != epoch.generation:
                continue
            destination_binding = self._bindings[destination]
            if invited_dedicated_flow and self._is_host(destination_binding):
                self._append_invite_host_ready_state_window(
                    replies,
                    destination,
                    destination_binding,
                    epoch,
                )
                continue
            if (
                invited_dedicated_flow
                and not self._is_host(destination_binding)
                and len(wire.latest_latency_info) == 13
                and logical_type(wire.latest_latency_info)
                == OLMessageType.LATENCY_INFO
            ):
                self._append_invite_guest_ready_state_window(
                    replies,
                    destination,
                    destination_binding,
                    epoch,
                )
                continue

            # readychalange frame 1120 ends the coordinator's five-packet
            # Ready request at 0x177. The coordinator subsequently uses 0x178
            # for empty transport ACKs, but official server frames 1156/1160
            # deliberately retain ack=0x177. A fast localhost path receives
            # those empty ACKs before helper state 13; acknowledging 0x178 here
            # moves the coordinator's application ring past Ready and it closes
            # before emitting frame 1161's native countdown bundle.
            acknowledgement = (
                int(epoch.source_final_sequence) & _SEQUENCE_MASK
                if self._is_host(destination_binding)
                else int(wire.last_client_sequence) & _SEQUENCE_MASK
            )
            control_sequence = self._take_server_sequence(wire)
            self._append_datagram(
                replies,
                TunnelDatagram(
                    wire.next_offset_words,
                    (
                        TunnelPacket(
                            1,
                            encode_active(
                                control_sequence,
                                acknowledgement,
                                control,
                            ),
                        ),
                    ),
                ),
                destination,
            )
            self._append_ready_state_pair(
                replies,
                destination,
                destination_binding,
                epoch,
                acknowledgement,
                (
                    "ready-request-final"
                    if self._is_host(destination_binding)
                    else "guest-native-current"
                ),
                control_sequence,
            )
        epoch.stage = ReadyStage.STATE_PAIR_SENT_WAIT_HOST_NATIVE_BUNDLE
        return matched

    def _append_ready_state_pair(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        destination_binding: CarbonTicketResolution,
        epoch: ReadyEpoch,
        acknowledgement: int,
        acknowledgement_source: str,
        control_sequence: int,
    ) -> None:
        wire = self._wire[destination]
        control = bytes.fromhex("018c000000828100000002")
        footer = wire.footer or self._footer_for(
            destination,
            destination_binding,
        )
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        first_body = epoch.state13 + b"\x04" + control + footer + b"\x44"
        second_body = (
            epoch.state15
            + b"\x04"
            + epoch.state13
            + b"\x04\x12"
            + control
            + footer
            + b"\x44"
        )
        self._append_datagram(
            replies,
            TunnelDatagram(
                wire.next_offset_words,
                (
                    TunnelPacket(
                        1,
                        encode_active(
                            0x10000000 | base,
                            acknowledgement,
                            first_body,
                        ),
                    ),
                    TunnelPacket(
                        1,
                        encode_active(
                            0x20000000 | ((base + 1) & _SEQUENCE_MASK),
                            acknowledgement,
                            second_body,
                        ),
                    ),
                ),
            ),
            destination,
        )
        wire.next_server_sequence = (base + 2) & _SEQUENCE_MASK
        self.log.info(
            "Carbon GM ReadyEpoch state-pair: gid=%s gen=%d dst=%s:%d "
            "flags=1,2 control_seq=%07x ack=%07x ack_source=%s "
            "seed_delivery_gate=complete",
            destination_binding.game.gid,
            epoch.generation,
            destination[0],
            destination[1],
            int(control_sequence) & _SEQUENCE_MASK,
            acknowledgement,
            acknowledgement_source,
        )

    def _append_invite_host_ready_state_window(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        destination_binding: CarbonTicketResolution,
        epoch: ReadyEpoch,
    ) -> None:
        """Emit the invited Challenge coordinator window captured in frame 1438.

        ``invitechallenge&ready.pcapng`` frames 1438/1441 use five increasing
        sequence numbers with flags 0,1,2,2,2.  All five acknowledge the
        coordinator's current sequence (the empty transport packet immediately
        after its Ready request), and the final three packets carry the exact
        three-record CommUDP rolling history that makes the coordinator publish
        its native countdown bundle.
        """
        wire = self._wire[destination]
        footer = wire.footer or self._footer_for(
            destination,
            destination_binding,
        )
        control = bytes.fromhex("018c000000828100000002")
        state13_record = epoch.state13 + PLAIN_TERMINATOR
        state15_record = epoch.state15 + PLAIN_TERMINATOR
        control_record = control + PLAIN_TERMINATOR
        footer_record = footer + NGL_FOOTER_FLAG
        bodies = (
            footer_record,
            control_record + footer + NGL_FOOTER_WITH_TRAILER,
            (
                state13_record
                + control_record
                + REDUNDANT_BODY_SEPARATOR
                + footer
                + NGL_FOOTER_WITH_TRAILER
            ),
            (
                state15_record
                + state13_record
                + bytes((len(state13_record),))
                + control_record
                + REDUNDANT_BODY_SEPARATOR
            ),
            (
                state13_record
                + state15_record
                + bytes((len(state15_record),))
                + state13_record
                + bytes((len(state13_record),))
            ),
        )
        flags = (0, 1, 2, 2, 2)
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
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
        self._append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, packets),
            destination,
        )
        wire.next_server_sequence = (base + len(packets)) & _SEQUENCE_MASK
        self.log.info(
            "Carbon GM ReadyEpoch invite-host state-window: "
            "gid=%s gen=%d dst=%s:%d flags=0,1,2,2,2 "
            "ack=%07x ack_source=host-current-after-ready "
            "seed_delivery_gate=complete",
            destination_binding.game.gid,
            epoch.generation,
            destination[0],
            destination[1],
            acknowledgement,
        )

    def _append_invite_guest_ready_state_window(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        destination_binding: CarbonTicketResolution,
        epoch: ReadyEpoch,
    ) -> None:
        """Emit the invited helper window captured in frames 1436 and 1439."""
        wire = self._wire[destination]
        footer = wire.footer or self._footer_for(
            destination,
            destination_binding,
        )
        control = bytes.fromhex("018c000000828100000002")
        latency_record = bytes(wire.latest_latency_info) + PLAIN_TERMINATOR
        state13_record = epoch.state13 + PLAIN_TERMINATOR
        state15_record = epoch.state15 + PLAIN_TERMINATOR
        control_record = control + PLAIN_TERMINATOR
        footer_record = footer + NGL_FOOTER_FLAG
        bodies = (
            (
                footer_record
                + latency_record
                + bytes((len(latency_record),))
            ),
            (
                control_record
                + footer_record
                + bytes((len(footer_record),))
            ),
            (
                state13_record
                + control_record
                + bytes((len(control_record),))
                + footer_record
                + bytes((len(footer_record),))
            ),
            (
                state15_record
                + state13_record
                + bytes((len(state13_record),))
                + control_record
                + bytes((len(control_record),))
                + footer_record
                + bytes((len(footer_record),))
            ),
            (
                state13_record
                + state15_record
                + bytes((len(state15_record),))
                + state13_record
                + bytes((len(state13_record),))
                + control_record
                + bytes((len(control_record),))
            ),
        )
        flags = (1, 1, 2, 3, 3)
        base = int(wire.next_server_sequence) & _SEQUENCE_MASK
        first_acknowledgement = (
            int(epoch.guest_pre_state_sequence) & _SEQUENCE_MASK
        )
        acknowledgement = (
            int(epoch.guest_state_final_sequence) & _SEQUENCE_MASK
        ) or (int(wire.last_client_sequence) & _SEQUENCE_MASK)
        packets = tuple(
            TunnelPacket(
                1,
                encode_active(
                    ((flag & 0x0F) << 28)
                    | ((base + index) & _SEQUENCE_MASK),
                    (
                        first_acknowledgement
                        if index == 0 and first_acknowledgement
                        else acknowledgement
                    ),
                    body,
                ),
            )
            for index, (flag, body) in enumerate(zip(flags, bodies))
        )
        self._append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, packets),
            destination,
        )
        wire.next_server_sequence = (base + len(packets)) & _SEQUENCE_MASK
        self.log.info(
            "Carbon GM ReadyEpoch invite-guest state-window: "
            "gid=%s gen=%d dst=%s:%d flags=1,1,2,3,3 "
            "first_ack=%07x ack=%07x "
            "ack_source=guest-pre-state+guest-native-final "
            "seed_delivery_gate=complete",
            destination_binding.game.gid,
            epoch.generation,
            destination[0],
            destination[1],
            first_acknowledgement,
            acknowledgement,
        )

    def relay_native_ready_bundle(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        return self.epochs.relay_native_ready_bundle(
            replies,
            source,
            binding,
            active_packets,
        )

    def relay_native_ready_snapshot(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        return self.epochs.relay_native_ready_snapshot(
            replies,
            source,
            binding,
            active_packets,
        )

    def relay_retail_ready_seed(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        active_packets: list[CommUDPActive],
    ) -> set[int]:
        return self.epochs.relay_retail_ready_seed(
            replies,
            source,
            binding,
            active_packets,
        )
