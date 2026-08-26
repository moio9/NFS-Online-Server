"""Binding and ordered session bootstrap for Carbon GameManager endpoints."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, MutableSet
import logging
import time

from carbon.gamemanager.player_codec import encode_join, encode_roster
from carbon.gamemanager.race_session import (
    descriptor,
    descriptor_bundle,
    open_host_properties,
    session_attributes,
)
from carbon.gamemanager.race_state import GameRaceState
from carbon.gamemanager.session import (
    BoundParticipant,
    local_first,
    player_wire_data,
)
from carbon.gamemanager.session_codec import (
    encode_active,
    encode_empty_active_ack,
    encode_host_hello,
)
from carbon.gamemanager.session_object import first_block_identity
from carbon.rebroadcaster.handshake import EndpointHandshake
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.race_results import RaceResultCoordinator
from carbon.rebroadcaster.retry import ReliableWindow, RetryPolicy
from carbon.rebroadcaster.session_objects import SessionObjectCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    SourceKey,
)
from carbon.theater.directory import CarbonGame, CarbonTicketResolution
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF
_SESSION_BOOTSTRAP_RETRY_POLICY = RetryPolicy(0.5, 2.0, 8, 20.0)

Replies = list[tuple[bytes, Address]]
SessionEndpoints = Callable[[str], tuple[Address, ...]]
SourceKeyFor = Callable[[CarbonTicketResolution], SourceKey]
IsHost = Callable[[CarbonTicketResolution], bool]
FooterFor = Callable[[Address, CarbonTicketResolution], bytes]
BoundParticipants = Callable[[CarbonGame], tuple[BoundParticipant, ...]]
ClockOrigin = Callable[[], float]


class SessionBootstrapCoordinator:
    """Own authenticated binding, roster publication and session bootstrap."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        session_objects: SessionObjectCoordinator,
        race_results: RaceResultCoordinator,
        endpoints: MutableMapping[Address, EndpointHandshake],
        wires: MutableMapping[Address, EndpointWireState],
        bindings: MutableMapping[Address, CarbonTicketResolution],
        participant_endpoints: MutableMapping[SourceKey, Address],
        published_joins: MutableSet[tuple[str, int]],
        reconnect_pending: MutableSet[SourceKey],
        races: MutableMapping[str, GameRaceState],
        *,
        session_endpoints: SessionEndpoints,
        source_key: SourceKeyFor,
        is_host: IsHost,
        footer_for: FooterFor,
        bound_participants: BoundParticipants,
        clock_origin: ClockOrigin,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.session_objects = session_objects
        self.race_results = race_results
        self._endpoints = endpoints
        self._wire = wires
        self._bindings = bindings
        self._participant_endpoints = participant_endpoints
        self._published_joins = published_joins
        self._reconnect_pending = reconnect_pending
        self._race = races
        self.session_endpoints = session_endpoints
        self._source_key = source_key
        self._is_host = is_host
        self._footer_for = footer_for
        self._bound_participants = bound_participants
        self._clock_origin = clock_origin
        self.log = logger or logging.getLogger(__name__)

    def bind(self, addr: Address, resolution: CarbonTicketResolution) -> bool:
        current = self._bindings.get(addr)
        if current is not None and (
            current.game.gid != resolution.game.gid
            or current.participant.identity.user_id
            != resolution.participant.identity.user_id
        ):
            return False

        participant_key = self._source_key(resolution)
        previous_addr = self._participant_endpoints.get(participant_key)
        if previous_addr is not None and previous_addr != addr:
            self._bindings.pop(previous_addr, None)
            self._wire.pop(previous_addr, None)
            self._endpoints.pop(previous_addr, None)
            # A fresh UDP endpoint must receive its own 0x0185/session
            # bootstrap without republishing a duplicate self player.
            self._published_joins.discard(
                (resolution.game.gid, int(resolution.participant.player_id))
            )
            for peer_wire in self._wire.values():
                peer_wire.pending_session_releases.discard(previous_addr)
                peer_wire.published_remote_objects.pop(participant_key, None)
                peer_wire.published_session_offsets.pop(participant_key, None)
        self._bindings[addr] = resolution
        self._participant_endpoints[participant_key] = addr
        wire = self._wire.setdefault(addr, EndpointWireState())
        bound_at = time.monotonic()
        if wire.bound_at <= 0.0:
            wire.bound_at = bound_at
        wire.last_activity_at = max(wire.last_activity_at, bound_at)
        if participant_key in self._reconnect_pending:
            wire.suppress_self_join_publication = True
            self._reconnect_pending.discard(participant_key)
        self._race.setdefault(resolution.game.gid, GameRaceState())
        self.race_results.tracker(resolution.game)
        return True

    def append_bootstrap(
        self,
        replies: Replies,
        addr: Address,
        resolution: CarbonTicketResolution,
    ) -> None:
        wire = self._wire[addr]
        participants = self._bound_participants(resolution.game)
        # Retail two-player Quick Join is receiver-local. Invite, ranked and
        # larger rooms retain authoritative insertion order for stable slots.
        two_player_quick_join = (
            resolution.game.server_hosted
            and str(resolution.game.properties.get("B-U-game_type", "")) == "1"
            and not bool(resolution.participant.invite_remote_player_id)
            and len(participants) == 2
        )
        ordered = (
            local_first(
                participants,
                local_player_id=resolution.participant.player_id,
            )
            if two_player_quick_join or not resolution.game.server_hosted
            else participants
        )
        if not ordered:
            return

        expected = min(len(ordered), 8)
        capacity = max(expected, min(int(resolution.game.session.capacity), 8))
        footer = wire.footer or self._footer_for(addr, resolution)
        wire.footer = footer
        ack = int(wire.bootstrap_acknowledgement) & _SEQUENCE_MASK

        packets: list[TunnelPacket] = []
        hello_sequence = 0x10000000 | EndpointPublisher.take_server_sequence(wire)
        packets.append(
            TunnelPacket(
                1,
                encode_active(
                    hello_sequence,
                    ack,
                    encode_host_hello(
                        expected,
                        capacity=capacity,
                        footer=footer,
                        server_hosted=(
                            resolution.game.server_hosted
                            and self._is_host(resolution)
                        ),
                    ),
                ),
            )
        )
        for bound in ordered:
            player = player_wire_data(
                bound,
                local_player_id=resolution.participant.player_id,
            )
            packets.append(
                TunnelPacket(
                    1,
                    encode_active(
                        EndpointPublisher.take_server_sequence(wire),
                        ack,
                        encode_roster(player),
                    ),
                )
            )

        self.publisher.append_datagram(
            replies,
            TunnelDatagram(wire.next_offset_words, tuple(packets)),
            addr,
            confirmation="session-host-bootstrap",
            application_confirmation=True,
        )
        wire.bootstrap_sent = True
        footer_client_tick = int.from_bytes(footer[:4], "big")
        footer_server_tick = int.from_bytes(footer[4:8], "big")
        self.log.info(
            "Carbon GM release bootstrap sent: gid=%s dst=%s:%d local_pid=%d "
            "expected=%d capacity=%d roster=%s footer_client_tick=%08x "
            "footer_server_tick=%08x clock_delta_ms=%d",
            resolution.game.gid,
            addr[0],
            addr[1],
            resolution.participant.player_id,
            expected,
            capacity,
            ",".join(str(item.participant.player_id) for item in ordered),
            footer_client_tick,
            footer_server_tick,
            (footer_server_tick - footer_client_tick) & 0xFFFFFFFF,
        )

    def append_ticket_ack(
        self,
        replies: Replies,
        addr: Address,
        resolution: CarbonTicketResolution,
    ) -> None:
        wire = self._wire[addr]
        footer = wire.footer or self._footer_for(addr, resolution)
        wire.footer = footer
        acknowledgement = int(wire.bootstrap_acknowledgement) & _SEQUENCE_MASK
        self.publisher.append_datagram(
            replies,
            TunnelDatagram(
                wire.next_offset_words,
                (
                    TunnelPacket(
                        1,
                        encode_empty_active_ack(
                            0x100,
                            acknowledgement,
                            footer=footer,
                        ),
                    ),
                ),
            ),
            addr,
        )

    def append_join_publication(
        self,
        replies: Replies,
        source: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        joining_pid = int(binding.participant.player_id)
        publication_key = (binding.game.gid, joining_pid)
        source_wire = self._wire[source]
        if source_wire.suppress_self_join_publication:
            source_wire.suppress_self_join_publication = False
            self.append_session_bootstrap(replies, source, binding)
            self.log.info(
                "Carbon GM release reconnect bootstrap completed without "
                "self join publication: gid=%s pid=%d src=%s:%d",
                binding.game.gid,
                joining_pid,
                source[0],
                source[1],
            )
            return
        if publication_key in self._published_joins:
            return

        joining = BoundParticipant(binding.participant, source)
        invite_remote_pid = int(binding.participant.invite_remote_player_id)
        destinations = [source]
        destinations.extend(
            peer
            for peer in self.session_endpoints(binding.game.gid)
            if peer != source
        )
        sent = 0
        for destination in destinations:
            destination_binding = self._bindings.get(destination)
            destination_wire = self._wire.get(destination)
            if (
                destination_binding is None
                or destination_wire is None
                or not destination_wire.bootstrap_sent
            ):
                continue
            player = player_wire_data(
                joining,
                local_player_id=destination_binding.participant.player_id,
                force_state=6,
            )
            join_body = encode_join(player)
            reply_start = len(replies)
            self.publisher.append_active_body(
                replies,
                destination,
                join_body,
                confirmation=(
                    "session-self-join"
                    if destination == source
                    else "session-player-join"
                ),
            )
            if destination == source:
                destination_wire.session_self_join_body = join_body
                destination_wire.session_self_join_record = next(
                    (
                        bytes(raw)
                        for raw, target in replies[reply_start:]
                        if target == destination
                    ),
                    b"",
                )
            destination_wire.published_player_ids.add(joining_pid)
            sent += 1

        if not sent:
            return
        self._published_joins.add(publication_key)
        self.append_session_bootstrap(replies, source, binding)
        for peer in self.session_endpoints(binding.game.gid):
            if peer != source:
                self.session_objects.append_remote_parts(
                    replies,
                    peer,
                    source,
                    offsets={0},
                )
        self.log.info(
            "Carbon GM release join published: gid=%s joining_pid=%d "
            "source=%s:%d destinations=%d invite_remote_pid=%d "
            "source_publication_pid=%d",
            binding.game.gid,
            joining_pid,
            source[0],
            source[1],
            sent,
            invite_remote_pid,
            joining_pid,
        )

    def append_session_bootstrap(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        wire = self._wire.get(addr)
        if wire is None or wire.session_bootstrap_sent or not wire.bootstrap_sent:
            return
        participants = tuple(binding.game.participants.values())
        local_slot = next(
            (
                index
                for index, participant in enumerate(participants)
                if participant.identity.user_id
                == binding.participant.identity.user_id
            ),
            0,
        )
        handle_base = int(binding.game.descriptor_handle_base)
        if handle_base <= 0:
            raise ValueError(
                f"Carbon room {binding.game.gid} has no descriptor handle allocation"
            )
        local_handle = handle_base + local_slot * 10
        race = self._race.setdefault(binding.game.gid, GameRaceState())
        attributes = race.attributes or session_attributes(binding.game.properties)
        descriptor_clock = max(0.0, time.monotonic() - self._clock_origin())
        room_tick_ms = int(binding.game.created_tick_ms) & 0xFFFFFFFF
        if room_tick_ms == 0:
            room_tick_ms = int(time.monotonic() * 1000.0) & 0xFFFFFFFF
            if room_tick_ms == 0:
                room_tick_ms = 1
            binding.game.created_tick_ms = room_tick_ms
        packet_specs: list[tuple[int, bytes]] = [
            (
                0,
                descriptor(
                    local_handle,
                    descriptor_clock,
                    room_tick_ms=room_tick_ms,
                ),
            ),
            (
                1,
                descriptor_bundle(
                    local_handle,
                    descriptor_clock,
                    room_tick_ms=room_tick_ms,
                ),
            ),
            (0, attributes),
        ]
        bundled_remote_parts: list[tuple[Address, tuple[bytes, ...]]] = []
        if binding.game.server_hosted:
            for peer in self.session_endpoints(binding.game.gid):
                if peer == addr:
                    continue
                selected = self.session_objects.select_remote_parts(
                    peer,
                    addr,
                    offsets={0},
                    allow_pending_bootstrap=True,
                )
                if not selected:
                    continue
                bundled_remote_parts.append((peer, selected))
                packet_specs.extend((0, block) for block in selected)
        wire.session_bootstrap_specs = tuple(
            (int(redundancy), bytes(body))
            for redundancy, body in packet_specs
        )
        packets: list[TunnelPacket] = []
        for redundancy, body in wire.session_bootstrap_specs:
            sequence = EndpointPublisher.take_server_sequence(wire)
            sequence |= (redundancy & 0x0F) << 28
            packets.append(
                TunnelPacket(
                    1,
                    encode_active(
                        sequence,
                        int(wire.last_client_sequence) & _SEQUENCE_MASK,
                        body + b"\x04",
                    ),
                )
            )
        reply_start = len(replies)
        bootstrap_datagrams = self.publisher.append_packet_batches(
            replies,
            packets,
            addr,
        )
        wire.session_bootstrap_sent = True
        bootstrap_payloads = tuple(
            bytes(raw)
            for raw, target in replies[reply_start:]
            if target == addr
        )
        stage_payloads = (
            (
                (wire.session_self_join_record,)
                if wire.session_self_join_record
                else ()
            )
            + bootstrap_payloads
        )
        bootstrap_final_sequence = (
            int(wire.next_server_sequence) - 1
        ) & _SEQUENCE_MASK
        wire.session_bootstrap_window = ReliableWindow(
            records=stage_payloads,
            base_sequence=(
                bootstrap_final_sequence - len(packets)
                + (0 if wire.session_self_join_record else 1)
            )
            & _SEQUENCE_MASK,
            final_sequence=bootstrap_final_sequence,
            retry=_SESSION_BOOTSTRAP_RETRY_POLICY.begin(time.monotonic()),
        )
        self.log.info(
            "Carbon GM release session bootstrap sent: gid=%s dst=%s:%d "
            "pid=%d bodies=0022,0104(0022),1d15 "
            "descriptor_clock=%.6f room_tick=%08x remote_offset0=%d "
            "packets=%d datagrams=%d game_type=%s matchmaking_state=%s",
            binding.game.gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
            descriptor_clock,
            room_tick_ms,
            sum(len(blocks) for _peer, blocks in bundled_remote_parts),
            len(packets),
            bootstrap_datagrams,
            binding.game.properties.get("B-U-game_type", "?"),
            binding.game.properties.get("B-U-matchmaking_state", "?"),
        )
        for peer, selected in bundled_remote_parts:
            source_binding = self._bindings[peer]
            source_key = self._source_key(source_binding)
            cached_remote = wire.published_remote_objects.get(source_key, selected)
            remote_object_id, remote_pid, remote_name = first_block_identity(
                cached_remote
            )
            self.log.info(
                "Carbon GM release V622 remote host session object bundled "
                "with session bootstrap: gid=%s source=%s:%d "
                "destination=%s:%d remote_object=%d offsets=0x0 pid=%d "
                "name=%s blocks=%d ack=%07x",
                binding.game.gid,
                peer[0],
                peer[1],
                addr[0],
                addr[1],
                remote_object_id,
                remote_pid,
                remote_name or "-",
                len(selected),
                int(wire.last_client_sequence) & _SEQUENCE_MASK,
            )

    def append_initial_hostprops(
        self,
        replies: Replies,
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        wire = self._wire.get(addr)
        if wire is None or wire.initial_hostprops_sent:
            return
        self.publisher.append_active_body(
            replies,
            addr,
            open_host_properties(
                binding.game.session.capacity,
                wire_flag0=binding.game.is_ranked,
            ).encode()
            + (wire.footer or self._footer_for(addr, binding))
            + b"\x44",
            confirmation="session-initial-hostprops",
        )
        wire.initial_hostprops_sent = True
        self.log.info(
            "Carbon GM release initial HostProps sent: "
            "gid=%s dst=%s:%d pid=%d",
            binding.game.gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
        )
