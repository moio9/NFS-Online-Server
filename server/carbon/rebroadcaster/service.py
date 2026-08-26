"""UDP ingress facade and coordinator wiring for Carbon GameManager.

The service owns transport decoding, endpoint proof and dispatch ordering.
Room, Ready, bootstrap, race and gameplay policy live in focused coordinators;
Theater remains the authority for game membership.
"""

from __future__ import annotations

import logging
import struct
from threading import RLock
import time

from carbon.gamemanager.protocol import (
    GMMessageType,
    OLMessageType,
    ObservedTimerId,
)
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.results import (
    FINAL_GAME_RESULTS,
    GAME_RESULTS,
    LEADER_FINISHED,
    RACER_FINISHED,
)
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.gamemanager.session import (
    BoundParticipant,
)
from carbon.gamemanager.session_object import (
    is_session_object_complete,
    iter_session_object_blocks,
)
from carbon.rebroadcaster.active_router import ActiveMessageRouter
from carbon.rebroadcaster.ai_registration import AIRegistrationCoordinator
from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.endpoint_lifecycle import EndpointLifecycleCoordinator
from carbon.rebroadcaster.gameplay_relay import GameplayRelayCoordinator
from carbon.rebroadcaster.handshake import EndpointHandshake, hash_sar_decimal
from carbon.rebroadcaster.invite_session import InviteSessionBarrierCoordinator
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.race_results import (
    RaceResultCoordinator,
)
from carbon.rebroadcaster.race_start import (
    RaceEndpoint,
    RaceStartCoordinator,
)
from carbon.rebroadcaster.ready_seed import ReadySeedCoordinator
from carbon.rebroadcaster.ready_state import ReadyStateCoordinator
from carbon.rebroadcaster.room_commit import RoomCommitCoordinator
from carbon.rebroadcaster.room_lifecycle import RoomLifecycleCoordinator
from carbon.rebroadcaster.session_bootstrap import SessionBootstrapCoordinator
from carbon.rebroadcaster.session_objects import SessionObjectCoordinator
from carbon.rebroadcaster.state import (
    Address,
    EndpointWireState,
    ReadyEpoch,
    ReadyStage,
    RebroadcasterStats,
    SourceKey,
)
from carbon.rebroadcaster.world_state import NetGameLinkWorldState
from carbon.progression import CarbonProgressionStore, RaceAwards
from carbon.theater.directory import (
    CarbonGame,
    CarbonGameDirectory,
    CarbonTicketResolution,
)
from carbon.transport.commudp import (
    CommUDPActive,
    CommUDPControl,
    CommUDPType,
    game_manager_body,
    parse_channel_one,
    parse_session_ticket,
)
from carbon.transport.prototunnel import (
    ProtoTunnelError,
    TunnelDatagram,
    TunnelPacket,
    decode_datagram,
)


log = logging.getLogger(__name__)
_SEQUENCE_MASK = 0x0FFFFFFF
_SETUP_REPLY_SPACING_SECONDS = 0.012

__all__ = (
    "CarbonRebroadcasterService",
    "EndpointWireState",
    "ReadyEpoch",
    "ReadyStage",
)


class CarbonRebroadcasterService:
    def __init__(
        self,
        games: CarbonGameDirectory,
        progression: CarbonProgressionStore | None = None,
        *,
        join_timeout_seconds: float = 45.0,
        race_idle_timeout_seconds: float = 60.0,
        loading_ready_fallback_seconds: float = 8.0,
    ) -> None:
        self.games = games
        self.progression = progression or CarbonProgressionStore()
        self.key = str(games.ekey).encode("ascii", errors="strict")
        if not self.key:
            raise ValueError("Carbon rebroadcaster EKEY cannot be empty")
        if float(join_timeout_seconds) <= 0:
            raise ValueError("Carbon join timeout must be positive")
        if float(race_idle_timeout_seconds) <= 0:
            raise ValueError("Carbon race idle timeout must be positive")
        if float(loading_ready_fallback_seconds) <= 0:
            raise ValueError("Carbon loading READY fallback must be positive")
        self.join_timeout_seconds = float(join_timeout_seconds)
        self.race_idle_timeout_seconds = float(race_idle_timeout_seconds)
        self.loading_ready_fallback_seconds = float(
            loading_ready_fallback_seconds
        )
        self._lock = RLock()
        self._endpoints: dict[Address, EndpointHandshake] = {}
        self._wire: dict[Address, EndpointWireState] = {}
        self._bindings: dict[Address, CarbonTicketResolution] = {}
        self.confirmations = ConfirmationManager(self._wire, logger=log)
        self.outbound = EndpointPublisher(
            self.key,
            self._wire,
            self._bindings,
            confirmations=self.confirmations,
            logger=log,
        )
        self.session_objects = SessionObjectCoordinator(
            self.outbound,
            self._wire,
            self._bindings,
            is_host=self._is_host,
            logger=log,
        )
        self.ai_registration = AIRegistrationCoordinator(
            self.outbound,
            logger=log,
        )
        self._participant_endpoints: dict[SourceKey, Address] = {}
        self._published_joins: set[tuple[str, int]] = set()
        self._reconnect_pending: set[SourceKey] = set()
        self._joiner_state13_window_sent: set[SourceKey] = set()
        self._ready_epochs: dict[str, ReadyEpoch] = {}
        self._ready_generations: dict[str, int] = {}
        self._guest_countdown_transition_sent: set[str] = set()
        self._race: dict[str, GameRaceState] = {}
        self.race_results = RaceResultCoordinator(
            self.progression,
            logger=log,
        )
        self.world_state = NetGameLinkWorldState(logger=log)
        self._clock_origin = time.monotonic()
        self.session_bootstrap = SessionBootstrapCoordinator(
            self.outbound,
            self.session_objects,
            self.race_results,
            self._endpoints,
            self._wire,
            self._bindings,
            self._participant_endpoints,
            self._published_joins,
            self._reconnect_pending,
            self._race,
            session_endpoints=self.session_endpoints,
            source_key=self._source_key,
            is_host=self._is_host,
            footer_for=self._footer_for,
            bound_participants=self._bound_participants,
            clock_origin=lambda: self._clock_origin,
            logger=log,
        )
        self.ready_seed = ReadySeedCoordinator(
            self.outbound,
            self._wire,
            self._bindings,
            current_timer_body=self._current_timer_body,
            current_active_game_body=self._current_active_game_body,
            state_value=self._state_value,
            is_host=self._is_host,
            logger=log,
        )
        self.ready_state = ReadyStateCoordinator(
            self.outbound,
            self._wire,
            self._bindings,
            self._race,
            self._ready_epochs,
            self._joiner_state13_window_sent,
            self.ready_seed,
            self.games,
            self._ready_generations,
            session_endpoints=self.session_endpoints,
            is_host=self._is_host,
            active_game_bodies=self._active_game_bodies,
            current_active_game_body=self._current_active_game_body,
            state_name=self._state_name,
            state_value=self._state_value,
            footer_for=self._footer_for,
            current_timer_body=self._current_timer_body,
            timer_logical_deadline=self._timer_logical_deadline,
            record_countdown_wire_timer=self._record_countdown_wire_timer,
            lock_room_access=lambda game, race, reason: (
                self.room_lifecycle.lock_room_access(
                    game,
                    race,
                    reason=reason,
                )
            ),
            abort_ready_epoch=lambda gid, reason: (
                self.room_lifecycle.abort_ready_epoch(gid, reason=reason)
            ),
            reset_finished_race=lambda game: (
                self.room_lifecycle.reset_finished_race_for_rematch(game)
            ),
            logger=log,
        )
        self.room_commit = RoomCommitCoordinator(
            self.outbound,
            self.session_objects,
            self._wire,
            self._bindings,
            self._race,
            session_endpoints=self.session_endpoints,
            is_host=self._is_host,
            source_key=self._source_key,
            lock_room_access=lambda game, race, reason: (
                self.room_lifecycle.lock_room_access(
                    game,
                    race,
                    reason=reason,
                )
            ),
            seed_countdown=lambda replies, game: self._seed_shared_countdown(
                replies,
                game,
            ),
            current_active_game_body=self._current_active_game_body,
            state_value=self._state_value,
            clock_origin=lambda: self._clock_origin,
            logger=log,
        )
        self.invite_session = InviteSessionBarrierCoordinator(
            self.outbound,
            self.session_objects,
            self._wire,
            self._bindings,
            self._race,
            session_endpoints=self.session_endpoints,
            host_endpoint=self._host_endpoint,
            is_host=self._is_host,
            source_key=self._source_key,
            finalize_room=self.room_commit.maybe_finalize_room_session,
            hold_helper_allocation=(
                self.room_commit.hold_helper_allocation_generation
            ),
            clock_origin=lambda: self._clock_origin,
            logger=log,
        )
        self.gameplay_relay = GameplayRelayCoordinator(
            self.outbound,
            self.ai_registration,
            self.world_state,
            self._wire,
            self._bindings,
            self._race,
            session_endpoints=self.session_endpoints,
            hold_preconfirm=self.invite_session.hold_preconfirm,
            footer_for=self._footer_for,
            clock_ms=lambda: self._server_tick_ms(),
            logger=log,
        )
        self.race_start = RaceStartCoordinator(
            self.outbound,
            self.ai_registration,
            clock_origin=lambda: self._clock_origin,
            footer_for=lambda addr: self._footer_for(
                addr,
                self._bindings[addr],
            ),
            hold_preconfirm=self.invite_session.hold_preconfirm,
            finalize_room=self.room_commit.maybe_finalize_room_session,
            lock_room=lambda game, race, reason: self.room_lifecycle.lock_room_access(
                game,
                race,
                reason=reason,
            ),
            logger=log,
        )
        self._received = 0
        self._rejected = 0
        self._started = 0
        self._tickets_rejected = 0
        # Unknown lifecycle messages are useful once per participant/phase,
        # while logging every repeated body can itself starve the UDP loop.
        self._unhandled_message_diagnostics: set[
            tuple[str, int, str, str]
        ] = set()
        self.room_lifecycle = RoomLifecycleCoordinator(
            self.outbound,
            self.games,
            self.session_objects,
            self.race_results,
            self.world_state,
            self._wire,
            self._bindings,
            self._race,
            self._ready_epochs,
            self._ready_generations,
            self._participant_endpoints,
            self._published_joins,
            self._reconnect_pending,
            self._joiner_state13_window_sent,
            self._guest_countdown_transition_sent,
            self._unhandled_message_diagnostics,
            session_endpoints=self.session_endpoints,
            footer_for=self._footer_for,
            clear_room_commit=self.room_commit.clear_room,
            logger=log,
        )
        self.active_router = ActiveMessageRouter(
            lambda replies, destination, body: self._append_active_body(
                replies,
                destination,
                body,
            ),
            self.games,
            self.session_bootstrap,
            self.invite_session,
            self.gameplay_relay,
            self.race_results,
            self.room_commit,
            self.room_lifecycle,
            self.race_start,
            self._wire,
            self._bindings,
            self._race,
            self._ready_epochs,
            self._unhandled_message_diagnostics,
            session_endpoints=self.session_endpoints,
            is_host=self._is_host,
            footer_for=self._footer_for,
            logger=log,
        )
        self.endpoint_lifecycle = EndpointLifecycleCoordinator(
            self.games,
            self.gameplay_relay,
            self.room_lifecycle,
            self.confirmations,
            self._endpoints,
            self._wire,
            self._bindings,
            self._participant_endpoints,
            self._published_joins,
            self._race,
            self._ready_epochs,
            self._lock,
            join_timeout_seconds=self.join_timeout_seconds,
            race_idle_timeout_seconds=self.race_idle_timeout_seconds,
            session_endpoints=self.session_endpoints,
            source_key=self._source_key,
            is_host=self._is_host,
            logger=log,
        )

    def status_snapshot(self) -> dict[str, dict[str, object]]:
        """Return sanitized per-room race state for external status tools."""
        with self._lock:
            return {
                str(gid): {
                    "phase": race.phase.name,
                    "room_access": race.room_access.name,
                }
                for gid, race in self._race.items()
            }


    def record_race_result(
        self,
        gid: str,
        *,
        event_type: int,
        winner_profile_ids: set[int] | tuple[int, ...] | list[int],
    ) -> RaceAwards:
        return self.active_router.record_race_result(
            gid,
            event_type=event_type,
            winner_profile_ids=winner_profile_ids,
        )

    def _dedicated_handshake_hint(
        self,
        addr: Address,
    ) -> CarbonTicketResolution | None:
        """Identify the unbound dedicated participant behind an initial UDP Type1.

        ProtoTunnel setup happens before the ticket is carried in GM 0x00, so
        the rebroadcaster cannot resolve the participant cryptographically yet.
        Theater EGAM has already recorded the participant's internal UDP port,
        which is preserved by Carbon in the official captures and on localhost.
        """
        pending: list[CarbonTicketResolution] = []
        endpoint_matches: list[CarbonTicketResolution] = []
        port_matches: list[CarbonTicketResolution] = []
        for game in self.games.list():
            if not game.server_hosted:
                continue
            for participant in game.participants.values():
                key = (game.gid, participant.identity.user_id)
                if key in self._participant_endpoints:
                    continue
                resolution = CarbonTicketResolution(game, participant)
                pending.append(resolution)
                if int(participant.internal_port) == int(addr[1]):
                    port_matches.append(resolution)
                    if str(participant.internal_ip).strip() == str(addr[0]).strip():
                        endpoint_matches.append(resolution)

        # Several LAN clients normally use Carbon's stock UDP/1042 at the
        # same time.  Port-only matching then becomes ambiguous even though
        # Theater already recorded each participant's exact internal address.
        if endpoint_matches:
            if len(endpoint_matches) == 1:
                return endpoint_matches[0]

            # UDP/1042 is reused immediately when the retail client leaves one
            # room and accepts another invite.  The old Theater membership can
            # remain in the directory until ECNL or the stale-join sweep runs,
            # so two otherwise valid dedicated participants may temporarily
            # advertise the exact same internal endpoint.  The most recent
            # EGAM owns that socket; treating this handover as ambiguous makes
            # us answer with the client-hosted HUID and the client never sends
            # its GameManager ticket.
            selected = max(
                endpoint_matches,
                key=lambda item: float(item.participant.entered_at),
            )
            log.info(
                "Carbon UDP dedicated hint endpoint handover: src=%s:%d "
                "candidates=%s selected_gid=%s selected_pid=%d reason=latest-egam",
                addr[0],
                addr[1],
                ",".join(
                    f"{item.game.gid}:{item.participant.player_id}"
                    for item in sorted(
                        endpoint_matches,
                        key=lambda item: float(item.participant.entered_at),
                    )
                ),
                selected.game.gid,
                selected.participant.player_id,
            )
            return selected
        if len(port_matches) == 1:
            return port_matches[0]
        if addr[0] in {"127.0.0.1", "::1"} and len(pending) == 1:
            return pending[0]
        return None

    def _decode_client_datagram(
        self,
        payload: bytes,
        addr: Address,
        key: bytes,
    ) -> tuple[TunnelDatagram, int]:
        """Decode a client stream, restoring the high RC4-offset bits.

        A new server can meet a client already in a race, so before a first
        successful packet we also try the next two 16-bit epochs.  Once one is
        accepted, ordinary packets need only their expected epoch.
        """
        if len(payload) < 2:
            raise ProtoTunnelError("truncated ProtoTunnel datagram")
        low_offset = int.from_bytes(payload[:2], "big")
        previous = self._wire.get(addr, EndpointWireState()).client_stream_offset_words
        if previous is None:
            candidates = (low_offset, low_offset + 0x10000, low_offset + 0x20000)
        else:
            candidate = (int(previous) & ~0xFFFF) | low_offset
            if candidate + 0x8000 < int(previous):
                candidate += 0x10000
            elif candidate > int(previous) + 0x8000 and candidate >= 0x10000:
                candidate -= 0x10000
            candidates = (candidate, candidate + 0x10000, max(0, candidate - 0x10000))

        last_error: ProtoTunnelError | None = None
        for candidate in dict.fromkeys(candidates):
            try:
                return (
                    decode_datagram(
                        payload,
                        key,
                        stream_offset_words=candidate,
                    ),
                    candidate,
                )
            except ProtoTunnelError as exc:
                last_error = exc
        raise last_error or ProtoTunnelError("invalid ProtoTunnel subpacket table")

    def _decode_ingress_datagram(
        self,
        payload: bytes,
        addr: Address,
    ) -> TunnelDatagram | None:
        """Decode one client datagram and record its receive-side wire state."""
        with self._lock:
            self._received += 1
        selected_key = self._wire.get(addr, EndpointWireState()).tunnel_key or self.key
        try:
            datagram, client_stream_offset = self._decode_client_datagram(
                payload,
                addr,
                selected_key,
            )
        except ProtoTunnelError as exc:
            alternate_key = bytes(item ^ 0xFF for item in selected_key)
            try:
                datagram, client_stream_offset = self._decode_client_datagram(
                    payload,
                    addr,
                    alternate_key,
                )
            except ProtoTunnelError:
                with self._lock:
                    self._rejected += 1
                log.warning(
                    "Carbon UDP datagram rejected: src=%s:%d bytes=%d error=%s prefix=%s",
                    addr[0],
                    addr[1],
                    len(payload),
                    exc,
                    bytes(payload[:24]).hex(),
                )
                return None
            with self._lock:
                self._wire.setdefault(addr, EndpointWireState()).tunnel_key = alternate_key
            log.info(
                "Carbon UDP accepted alternate EKEY after retry: src=%s:%d bytes=%d",
                addr[0],
                addr[1],
                len(payload),
            )

        with self._lock:
            wire = self._wire.setdefault(addr, EndpointWireState())
            wire.client_stream_offset_words = client_stream_offset
            wire.last_activity_at = time.monotonic()
        return datagram

    @staticmethod
    def _control_packets(datagram: TunnelDatagram) -> list[CommUDPControl]:
        controls: list[CommUDPControl] = []
        for packet in datagram.packets:
            parsed = parse_channel_one(packet)
            if isinstance(parsed, CommUDPControl):
                controls.append(parsed)
        return controls

    def _handle_transport_controls(
        self,
        addr: Address,
        controls: list[CommUDPControl],
    ) -> bool:
        """Apply CONNECT/DISCONNECT lifecycle and report a terminal disconnect."""
        if any(item.kind is CommUDPType.DISCONNECT for item in controls):
            binding = self._bindings.get(addr)
            log.info(
                "Carbon UDP disconnect received: src=%s:%d gid=%s pid=%s",
                addr[0],
                addr[1],
                binding.game.gid if binding is not None else "<unbound>",
                binding.participant.player_id if binding is not None else "<unbound>",
            )
            self._drop_endpoint(addr, notify_peers=True)
            if binding is not None:
                self.games.leave(
                    binding.game.gid,
                    binding.participant.identity.user_id,
                    reason="udp-disconnect",
                )
            return True
        if (
            any(item.kind is CommUDPType.CONNECT for item in controls)
            and addr in self._bindings
        ):
            # A retail reconnect commonly reuses UDP/1042. Treat its new Type1
            # as a fresh transport even when ECNL/Type3 from the old room was
            # lost, otherwise bootstrap_sent suppresses the next HostHello.
            self._reconnect_pending.add(self._source_key(self._bindings[addr]))
            self._drop_endpoint(addr)
        return False

    def _accept_endpoint_handshake(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        datagram: TunnelDatagram,
        controls: list[CommUDPControl],
    ) -> tuple[EndpointWireState, TunnelDatagram | None]:
        """Create/update an endpoint handshake and append its transport reply."""
        endpoint = self._endpoints.get(addr)
        dedicated_hint = self._dedicated_handshake_hint(addr)
        if endpoint is None:
            server_huid = (
                dedicated_hint.game.host.user_id
                if dedicated_hint is not None
                else 51
            )
            server_tunnel_id = hash_sar_decimal(server_huid)
            endpoint = EndpointHandshake(
                server_tunnel_id=server_tunnel_id,
                dedicated=dedicated_hint is not None,
            )
            self._endpoints[addr] = endpoint
            self._started += 1
            channel7 = next(
                (packet.payload for packet in datagram.packets if packet.channel == 7),
                b"",
            )
            log.info(
                "Carbon UDP endpoint started: src=%s:%d packets=%d controls=%s "
                "profile=%s ch7=%s controls_detail=%s egeg_huid=%s tunnel_id=%08x "
                "hinted_gid=%s hinted_pid=%s",
                addr[0],
                addr[1],
                len(datagram.packets),
                ",".join(item.kind.name for item in controls) or "none",
                "dedicated" if dedicated_hint is not None else "client-hosted",
                channel7.hex() or "none",
                ",".join(
                    f"{item.kind.name}:{item.connection_id:08x}"
                    for item in controls
                ) or "none",
                server_huid,
                server_tunnel_id,
                dedicated_hint.game.gid if dedicated_hint is not None else "none",
                dedicated_hint.participant.player_id if dedicated_hint is not None else "none",
            )
        elif not endpoint.dedicated and dedicated_hint is not None:
            # A client can keep retransmitting an old CONNECT while the server
            # restarts.  That creates an unbound client-hosted handshake before
            # Theater has any EGAM membership to identify it.  Once EGAM is
            # present, promote the existing endpoint before answering the next
            # CONNECT; otherwise its cached HUID 51 survives forever and the
            # dedicated client never advances to the GameManager ticket.
            previous_tunnel_id = endpoint.server_tunnel_id
            endpoint.server_tunnel_id = hash_sar_decimal(
                dedicated_hint.game.host.user_id
            )
            endpoint.dedicated = True
            log.info(
                "Carbon UDP endpoint promoted to dedicated: src=%s:%d "
                "gid=%s pid=%d old_tunnel_id=%08x tunnel_id=%08x reason=egam-after-connect",
                addr[0],
                addr[1],
                dedicated_hint.game.gid,
                dedicated_hint.participant.player_id,
                previous_tunnel_id,
                endpoint.server_tunnel_id,
            )

        wire = self._wire.setdefault(addr, EndpointWireState())
        if wire.fallback_client_tick_ms == 0:
            # Record the first CONNECT/ticket receive epoch. The clean-room
            # transport does not expose the earlier client clock sample.
            first_tick = self._server_tick_ms()
            wire.fallback_client_tick_ms = first_tick or 0xFFFFFFFF

        response = endpoint.accept(datagram)
        if response is not None:
            response_hex = response.encode(wire.tunnel_key or self.key).hex()
            response_ch7 = next(
                (packet.payload for packet in response.packets if packet.channel == 7),
                b"",
            )
            response_controls = self._control_packets(response)
            log.info(
                "Carbon UDP handshake reply: dst=%s:%d profile=%s offset=%d ch7=%s "
                "controls=%s raw=%s",
                addr[0],
                addr[1],
                "dedicated" if endpoint.dedicated else "client-hosted",
                response.offset_words,
                response_ch7.hex() or "none",
                ",".join(
                    f"{item.kind.name}:{item.connection_id:08x}"
                    for item in response_controls
                ) or "none",
                response_hex,
            )
            self._append_datagram(replies, response, addr)
        return wire, response

    def _record_active_wire_state(
        self,
        addr: Address,
        wire: EndpointWireState,
        active: CommUDPActive,
        logical: bytes,
    ) -> bool:
        """Record reliability and NetGameLink timing state from one active packet."""
        kind = logical_type(logical)
        # Retail CarState records use a separate virtual sequence and must not
        # replace the ACK anchor for server control traffic.
        track_sequence = kind not in (
            OLMessageType.CAR_STATE,
            OLMessageType.CAR_STATE_BLOCK,
        )
        duplicate = self.confirmations.observe_inbound(
            addr,
            sequence=active.sequence,
            acknowledgement=active.acknowledgement,
            payload=logical,
            track_sequence=track_sequence,
        )
        if len(active.payload) < 21 or not active.payload[-1] & 0x40:
            return duplicate

        client_footer = bytes(active.payload[-13:-1])
        wire.last_client_footer = client_footer
        received_footer_tick = self._server_tick_ms()
        self.world_state.observe_footer(
            wire,
            client_footer,
            received_footer_tick,
        )
        log_gap = (
            received_footer_tick
            - int(wire.world_footer_observation_log_tick_ms)
        ) & 0xFFFFFFFF
        binding = self._bindings.get(addr)
        race = self._race.get(binding.game.gid) if binding is not None else None
        if (
            binding is not None
            and race is not None
            and race.phase is RacePhase.RACING
            and (
                wire.world_footer_observation_log_tick_ms == 0
                or log_gap >= 2000
            )
        ):
            peer_send_tick = int.from_bytes(client_footer[4:8], "big")
            log.info(
                "Carbon GM release V823 world-footer observe: "
                "gid=%s src=%s:%d pid=%d wire_id=%x count=%d "
                "seq=%08x kind=%s raw=%s peer_send=%08x "
                "received=%08x estimator_peer=%08x "
                "estimator_received=%08x rtt_avg=%d jitter_avg=%d",
                binding.game.gid,
                addr[0],
                addr[1],
                binding.participant.player_id,
                id(wire),
                wire.world_footer_observation_count,
                int(active.sequence),
                "none" if kind is None else f"0x{int(kind):02x}",
                client_footer.hex(),
                peer_send_tick,
                received_footer_tick,
                wire.world_footer_remote_ack_tick_ms,
                wire.world_footer_received_tick_ms,
                wire.world_footer_rtt_avg_ms,
                wire.world_footer_jitter_avg_ms,
            )
            wire.world_footer_observation_log_tick_ms = received_footer_tick
        wire.footer = self._server_footer_from_client(client_footer)
        return duplicate

    def _ingest_session_blocks(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        wire: EndpointWireState,
        logical: bytes,
    ) -> None:
        """Assemble session-object fragments and retire stale generations."""
        for session_block in iter_session_object_blocks(logical):
            binding = self._bindings.get(addr)
            fragment_key = (session_block.object_id, session_block.offset)
            if (
                binding is not None
                and bool(binding.participant.invite_remote_player_id)
                and not wire.session_confirmed
                and fragment_key not in wire.preconfirm_session_fragments_logged
            ):
                wire.preconfirm_session_fragments_logged.add(fragment_key)
                local_handle = (
                    int.from_bytes(session_block.raw[23:27], "big")
                    if session_block.offset == 0 and len(session_block.raw) >= 43
                    else -1
                )
                player_id = (
                    int.from_bytes(session_block.raw[31:35], "big")
                    if session_block.offset == 0 and len(session_block.raw) >= 43
                    else -1
                )
                slot_a = (
                    int.from_bytes(session_block.raw[35:39], "big")
                    if session_block.offset == 0 and len(session_block.raw) >= 43
                    else -1
                )
                slot_b = (
                    int.from_bytes(session_block.raw[39:43], "big")
                    if session_block.offset == 0 and len(session_block.raw) >= 43
                    else -1
                )
                log.info(
                    "Carbon GM release invite preconfirm guest object diagnostic: "
                    "gid=%s src=%s:%d pid=%d object=%d total=%d offset=%#x "
                    "local_handle=%s object_pid=%s slots=%s,%s raw=%s",
                    binding.game.gid,
                    addr[0],
                    addr[1],
                    binding.participant.player_id,
                    session_block.object_id,
                    session_block.total_size,
                    session_block.offset,
                    hex(local_handle) if local_handle >= 0 else "-",
                    player_id if player_id >= 0 else "-",
                    slot_a if slot_a >= 0 else "-",
                    slot_b if slot_b >= 0 else "-",
                    session_block.raw.hex(),
                )
            if (
                session_block.offset == 0
                and wire.session_object_id
                and session_block.object_id != wire.session_object_id
            ):
                previous_object_id = wire.session_object_id
                binding = self._bindings.get(addr)
                if binding is not None:
                    self.room_commit.hold_helper_allocation_generation(
                        addr,
                        binding,
                    )
                wire.session_blocks.clear()
                wire.local_reflected_object_id = 0
                wire.session_generation = max(2, wire.session_generation + 1)
                if binding is not None:
                    source_key = self._source_key(binding)
                    wire.published_session_offsets.pop(source_key, None)
                    for peer_wire in self._wire.values():
                        if peer_wire is wire:
                            continue
                        peer_wire.published_remote_objects.pop(source_key, None)
                        peer_wire.published_session_offsets.pop(source_key, None)
                log.info(
                    "Carbon GM release session object generation advanced: "
                    "src=%s:%d old_object=%d new_object=%d action=clear-old-offsets",
                    addr[0],
                    addr[1],
                    previous_object_id,
                    session_block.object_id,
                )
            if session_block.offset == 0:
                wire.session_object_id = session_block.object_id
                if wire.session_generation == 0:
                    wire.session_generation = 1
            elif (
                wire.session_object_id
                and session_block.object_id != wire.session_object_id
            ):
                log.info(
                    "Carbon GM ignored stale session object continuation: "
                    "src=%s:%d current_object=%d stale_object=%d offset=%#x",
                    addr[0],
                    addr[1],
                    wire.session_object_id,
                    session_block.object_id,
                    session_block.offset,
                )
                continue
            wire.session_blocks[session_block.offset] = session_block.raw
            if binding is not None:
                self.room_commit.release_pending_helper_allocation(
                    replies,
                    addr,
                    binding,
                )

    def _bind_active_ticket(
        self,
        addr: Address,
        active: CommUDPActive,
    ) -> CarbonTicketResolution | None:
        ticket = parse_session_ticket(active.game_manager)
        if ticket is None:
            return None
        resolution = self.games.resolve_ticket(ticket)
        if resolution is None:
            self._tickets_rejected += 1
            log.warning(
                "Carbon UDP ticket rejected: src=%s:%d ticket=%s reason=UNKNOWN_TICKET",
                addr[0],
                addr[1],
                ticket,
            )
            return None
        if not self._bind(addr, resolution):
            self._tickets_rejected += 1
            log.warning(
                "Carbon UDP ticket rejected: src=%s:%d ticket=%s gid=%s pid=%d reason=BIND_FAILED",
                addr[0],
                addr[1],
                ticket,
                resolution.game.gid,
                resolution.participant.player_id,
            )
            return None
        log.info(
            "Carbon UDP ticket bound: src=%s:%d ticket=%s gid=%s pid=%d persona=%s server_hosted=%s",
            addr[0],
            addr[1],
            ticket,
            resolution.game.gid,
            resolution.participant.player_id,
            resolution.participant.identity.persona,
            int(resolution.game.server_hosted),
        )
        return resolution

    def _ingest_active_packets(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        datagram: TunnelDatagram,
        wire: EndpointWireState,
    ) -> tuple[list[CommUDPActive], CarbonTicketResolution | None]:
        active_packets: list[CommUDPActive] = []
        newly_bound: CarbonTicketResolution | None = None
        duplicate = False
        for packet in datagram.packets:
            active = parse_channel_one(packet)
            if not isinstance(active, CommUDPActive):
                continue
            active_packets.append(active)
            logical = game_manager_body(active.payload)
            duplicate = (
                self._record_active_wire_state(addr, wire, active, logical)
                or duplicate
            )
            self._ingest_session_blocks(replies, addr, wire, logical)
            resolution = self._bind_active_ticket(addr, active)
            if resolution is not None:
                newly_bound = resolution
        if duplicate:
            replies.extend(
                self.confirmations.replay_pending(
                    addr,
                    reason="duplicate-client-datagram",
                )
            )
        return active_packets, newly_bound

    def _trace_bound_datagram(
        self,
        addr: Address,
        binding: CarbonTicketResolution,
        datagram: TunnelDatagram,
        active_packets: list[CommUDPActive],
    ) -> None:
        """Emit the narrow capture substitute used around lifecycle/result traffic."""
        interesting = False
        result_trace = False
        race = self._race.get(binding.game.gid)
        for item in active_packets:
            logical = game_manager_body(item.payload)
            kind = logical_type(logical)
            if kind in (
                OLMessageType.START_TIMER,
                OLMessageType.GAME_ATTRIBUTES,
                OLMessageType.ACTIVE_GAME_MESSAGE,
                OLMessageType.MATCHMAKING_OFF_REQUEST,
                OLMessageType.DISABLE_JOINS_REQUEST,
            ):
                interesting = True
            if kind in (
                LEADER_FINISHED,
                RACER_FINISHED,
                GAME_RESULTS,
                FINAL_GAME_RESULTS,
                OLMessageType.POST_RACE_SYNC,
            ):
                result_trace = True
            elif kind == OLMessageType.START_TIMER and len(logical) >= 9:
                timer_id = int.from_bytes(logical[5:9], "big")
                if timer_id == int(ObservedTimerId.POST_RACE_WINDOW):
                    result_trace = True
            elif (
                race is not None
                and race.phase == RacePhase.RACING
                and kind is None
                and logical
            ):
                result_trace = True
            if kind == OLMessageType.START_TIMER and len(logical) >= 17:
                timer_id = int.from_bytes(logical[5:9], "big")
                if timer_id == int(ObservedTimerId.RACE_COUNTDOWN):
                    interesting = True

        if not (
            (interesting or result_trace)
            and str(binding.game.properties.get("B-U-game_type", "")) == "2"
            and len(self.session_endpoints(binding.game.gid)) >= 2
        ):
            return

        log.info(
            "Carbon GM WIRETRACE inbound: gid=%s src=%s:%d pid=%d "
            "host=%d phase=%s result_trace=%d tunnel_packets=%d active_packets=%d",
            binding.game.gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
            int(self._is_host(binding)),
            self._race.get(binding.game.gid, GameRaceState()).phase.name,
            int(result_trace),
            len(datagram.packets),
            len(active_packets),
        )
        for index, item in enumerate(active_packets):
            logical = game_manager_body(item.payload)
            kind = logical_type(logical)
            kind_name = (
                kind.name
                if isinstance(kind, OLMessageType)
                else (f"0x{int(kind):02x}" if kind is not None else "none")
            )
            timer_detail = ""
            if kind == OLMessageType.START_TIMER and len(logical) >= 17:
                timer_id, sender_clock, duration = struct.unpack(">Iff", logical[5:17])
                timer_detail = (
                    f" timer_id={timer_id} sender_clock={sender_clock:.6f}"
                    f" duration={duration:.6f}"
                )
            log.info(
                "Carbon GM WIRETRACE packet: gid=%s index=%d "
                "seq=%08x low=%07x flags=%x ack=%08x kind=%s "
                "payload_len=%d logical_len=%d%s logical=%s",
                binding.game.gid,
                index,
                int(item.sequence),
                int(item.sequence) & _SEQUENCE_MASK,
                (int(item.sequence) >> 28) & 0xF,
                int(item.acknowledgement),
                kind_name,
                len(item.payload),
                len(logical),
                timer_detail,
                logical.hex(),
            )

    def _dispatch_bound_packets(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        active_packets: list[CommUDPActive],
        newly_bound: CarbonTicketResolution | None,
    ) -> int:
        """Run ordered Ready/session dispatch and return the reply-count marker."""
        replies_before = len(replies)
        consumed_ids: set[int] = set()
        for relay in (
            self.ready_state.relay_joiner_state13_window,
            self.ready_state.relay_retail_ready_seed,
            self.ready_state.relay_clean_state13_ack,
            self.ready_state.relay_native_ready_bundle,
            self.ready_state.relay_native_ready_snapshot,
        ):
            consumed_ids.update(relay(replies, addr, binding, active_packets))

        if (
            wire.bootstrap_pending
            and newly_bound is None
            and active_packets
            and not wire.bootstrap_sent
        ):
            self._append_bootstrap(replies, addr, binding)
            wire.bootstrap_pending = False

        for active in active_packets:
            if id(active) in consumed_ids:
                continue
            message = active.game_manager
            if (
                message is not None
                and message.message_type == int(GMMessageType.PLAYER_PUBLISH)
            ):
                self.confirmations.confirm_application(
                    addr,
                    label="session-host-bootstrap",
                )
                wire.invite_join_sequence = int(active.sequence) & _SEQUENCE_MASK
                self._append_join_publication(replies, addr, binding)

        world_state_bodies: list[bytes] = []
        player_controlled_ai_bodies: list[bytes] = []
        for active in active_packets:
            if id(active) in consumed_ids:
                continue
            logical = game_manager_body(active.payload)
            kind = logical_type(logical)
            if kind == OLMessageType.PLAYER_CONTROLLED_AI_CAR:
                current = self.gameplay_relay.current_player_controlled_ai_body(
                    logical
                )
                if current is not None:
                    player_controlled_ai_bodies.append(current)
                continue
            if kind in (
                OLMessageType.CAR_STATE,
                OLMessageType.CAR_STATE_BLOCK,
            ):
                world_state_bodies.append(logical)
                continue
            self._handle_bound_active(replies, addr, binding, active)

        if player_controlled_ai_bodies:
            self.gameplay_relay.relay_player_controlled_ai(
                replies,
                addr,
                binding,
                player_controlled_ai_bodies,
            )
        if world_state_bodies:
            self.gameplay_relay.relay_world_states(
                replies,
                addr,
                binding,
                world_state_bodies,
            )
        return replies_before

    def _finish_bound_datagram(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
        wire: EndpointWireState,
        active_packets: list[CommUDPActive],
        newly_bound: CarbonTicketResolution | None,
        replies_before: int,
    ) -> None:
        """Publish deferred session work and the fallback transport ACK."""
        if active_packets:
            self.endpoint_lifecycle.publish_pending_player_leaves(
                replies,
                addr,
                binding,
                wire,
            )

        if wire.session_confirmed:
            self.room_commit.release_pending_helper_allocation(
                replies,
                addr,
                binding,
            )

        helper_generation_blocked = (
            self.room_commit.advance_helper_generation_barrier(
                replies,
                binding.game,
                current_address=addr,
            )
        )
        if (
            is_session_object_complete(wire.session_blocks.values())
            and not helper_generation_blocked
        ):
            self.invite_session.handle_complete_session_object(
                replies,
                addr,
                binding,
            )
        if wire.session_confirmation_pending and not wire.session_confirmed:
            self.invite_session.append_session_confirmation(
                replies,
                addr,
                binding,
            )
            if wire.session_confirmed:
                self.room_commit.release_pending_helper_allocation(
                    replies,
                    addr,
                    binding,
                )

        self._append_post_start_latency_if_acked(replies, addr, binding)
        self.gameplay_relay.update_ai_registration_delivery(
            replies,
            addr,
            binding,
        )
        has_session_fragment = any(
            next(
                iter_session_object_blocks(game_manager_body(item.payload)),
                None,
            ) is not None
            for item in active_packets
        )
        hold_preconfirm_ack = (
            bool(binding.participant.invite_remote_player_id)
            and not wire.session_confirmed
            and wire.session_bootstrap_sent
        )
        source_has_reply = any(
            target == addr
            for _payload, target in replies[replies_before:]
        )
        if not active_packets or newly_bound is not None or source_has_reply:
            return
        if not hold_preconfirm_ack:
            self._append_transport_ack(replies, addr)
            return
        if (
            has_session_fragment
            and not is_session_object_complete(wire.session_blocks.values())
        ):
            log.info(
                "Carbon GM release invite partial session ACK held: "
                "gid=%s dst=%s:%d pid=%d object=%d offsets=%s "
                "action=wait-for-complete-object",
                binding.game.gid,
                addr[0],
                addr[1],
                binding.participant.player_id,
                wire.session_object_id,
                ",".join(hex(offset) for offset in sorted(wire.session_blocks)),
            )
            return
        log.info(
            "Carbon GM release invite preconfirm transport ACK held: "
            "gid=%s dst=%s:%d pid=%d client_seq=%07x "
            "client_ack=%07x next_server=%07x action=wait-for-0x02",
            binding.game.gid,
            addr[0],
            addr[1],
            binding.participant.player_id,
            wire.last_client_sequence,
            wire.last_client_acknowledgement,
            wire.next_server_sequence,
        )

    def handle_datagram(self, payload: bytes, addr: Address) -> list[tuple[bytes, Address]]:
        datagram = self._decode_ingress_datagram(payload, addr)
        if datagram is None:
            return []
        replies: list[tuple[bytes, Address]] = []
        with self._lock:
            controls = self._control_packets(datagram)
            if self._handle_transport_controls(addr, controls):
                return replies
            wire, response = self._accept_endpoint_handshake(
                replies,
                addr,
                datagram,
                controls,
            )

            active_packets, newly_bound = self._ingest_active_packets(
                replies,
                addr,
                datagram,
                wire,
            )

            if newly_bound is not None and not wire.bootstrap_sent:
                # The ticket datagram is answered only by the transport ACK.
                # HostHello/roster are released after the client's following
                # empty active ACK (official frames 437-440).
                wire.bootstrap_pending = True
                wire.bootstrap_acknowledgement = int(wire.last_client_sequence) & _SEQUENCE_MASK
                if response is None:
                    self._append_ticket_ack(replies, addr, newly_bound)

            binding = self._bindings.get(addr)
            if binding is not None:
                self._trace_bound_datagram(
                    addr,
                    binding,
                    datagram,
                    active_packets,
                )
                replies_before = self._dispatch_bound_packets(
                    replies,
                    addr,
                    binding,
                    wire,
                    active_packets,
                    newly_bound,
                )
                self._finish_bound_datagram(
                    replies,
                    addr,
                    binding,
                    wire,
                    active_packets,
                    newly_bound,
                    replies_before,
                )

        return replies

    def drop_participant(self, gid: str, user_id: int) -> bool:
        return self.endpoint_lifecycle.drop_participant(gid, user_id)

    def force_disconnect_user(
        self,
        user_id: int,
        *,
        reason: str,
    ) -> int:
        return self.endpoint_lifecycle.force_disconnect_user(
            user_id,
            reason=reason,
        )

    def _drop_endpoint(self, addr: Address, *, notify_peers: bool = False) -> None:
        self.endpoint_lifecycle.drop_endpoint(
            addr,
            notify_peers=notify_peers,
        )

    def _append_transport_ack(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
    ) -> None:
        self.outbound.append_transport_ack(replies, addr)

    def _bind(self, addr: Address, resolution: CarbonTicketResolution) -> bool:
        return self.session_bootstrap.bind(addr, resolution)

    def _append_bootstrap(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        resolution: CarbonTicketResolution,
    ) -> None:
        self.session_bootstrap.append_bootstrap(replies, addr, resolution)

    def _append_ticket_ack(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        resolution: CarbonTicketResolution,
    ) -> None:
        self.session_bootstrap.append_ticket_ack(replies, addr, resolution)

    def _append_join_publication(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.session_bootstrap.append_join_publication(
            replies,
            source,
            binding,
        )

    def _append_session_bootstrap(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.session_bootstrap.append_session_bootstrap(
            replies,
            addr,
            binding,
        )

    def poll_retries(
        self,
        *,
        now: float | None = None,
    ) -> list[tuple[bytes, Address]]:
        # Keep the historical mutable service knobs authoritative for callers
        # and tests which tune expiry thresholds after construction.
        self.endpoint_lifecycle.join_timeout_seconds = self.join_timeout_seconds
        self.endpoint_lifecycle.race_idle_timeout_seconds = (
            self.race_idle_timeout_seconds
        )
        current = time.monotonic() if now is None else float(now)
        replies = self.endpoint_lifecycle.poll_retries(now=current)
        with self._lock:
            for game in tuple(self.games.list()):
                race = self._race.get(game.gid)
                if race is None:
                    continue
                self.race_start.poll_loading_ready_fallback(
                    replies,
                    game,
                    race,
                    self._race_start_endpoint_snapshots(game.gid),
                    now=current,
                    fallback_seconds=self.loading_ready_fallback_seconds,
                )
        return replies

    def _append_initial_hostprops(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.session_bootstrap.append_initial_hostprops(
            replies,
            addr,
            binding,
        )

    @staticmethod
    def _current_active_game_body(logical: bytes) -> bytes | None:
        """Return only the leading 0x1C message, without redundancy tails.

        Carbon CommUDP appends older OLMSG bodies after a 0x04 separator.
        ``game_manager_body`` intentionally preserves that history, which is
        useful for diagnostics but unsafe to reflect as new application data.
        """
        if logical_type(logical) != OLMessageType.ACTIVE_GAME_MESSAGE or len(logical) < 11:
            return None
        name_length = int.from_bytes(logical[5:7], "big")
        end = 7 + name_length + 4
        if end > len(logical):
            return None
        return bytes(logical[:end])

    @staticmethod
    def _active_game_bodies(logical: bytes) -> tuple[bytes, ...]:
        """Recover every complete 0x1C body from a cumulative OLMSG chain."""
        marker = bytes.fromhex("000000001c")
        bodies: list[bytes] = []
        cursor = 0
        while True:
            offset = logical.find(marker, cursor)
            if offset < 0:
                break
            if offset + 7 > len(logical):
                break
            name_length = int.from_bytes(logical[offset + 5:offset + 7], "big")
            end = offset + 7 + name_length + 4
            if end <= len(logical):
                body = bytes(logical[offset:end])
                if body not in bodies:
                    bodies.append(body)
            cursor = offset + len(marker)
        return tuple(bodies)

    @staticmethod
    def _state_name(logical: bytes) -> str | None:
        if logical_type(logical) != OLMessageType.ACTIVE_GAME_MESSAGE or len(logical) < 11:
            return None
        name_length = int.from_bytes(logical[5:7], "big")
        end = 7 + name_length
        if end + 4 > len(logical):
            return None
        return logical[7:end].decode("utf-8", errors="replace")

    @staticmethod
    def _current_timer_body(logical: bytes) -> bytes | None:
        return RaceStartCoordinator.current_timer_body(logical)

    @staticmethod
    def _timer_logical_deadline(timer: bytes) -> float:
        return RaceStartCoordinator.timer_logical_deadline(timer)

    def _record_countdown_wire_timer(
        self,
        race: GameRaceState,
        timer: bytes,
    ) -> tuple[int, float, float]:
        return self.race_start.record_countdown_wire_timer(race, timer)

    def _handle_bound_active(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
        active: CommUDPActive,
    ) -> None:
        self.active_router.handle(replies, addr, binding, active)

    def _race_start_endpoint_snapshot(
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

    def _race_start_endpoint_snapshots(
        self,
        gid: str,
    ) -> tuple[RaceEndpoint, ...]:
        return tuple(
            self._race_start_endpoint_snapshot(address)
            for address in self.session_endpoints(gid)
        )

    def _seed_shared_countdown(
        self,
        replies: list[tuple[bytes, Address]],
        game: CarbonGame,
    ) -> None:
        self.race_start.seed_countdown(
            replies,
            game,
            self._race.setdefault(game.gid, GameRaceState()),
            self._race_start_endpoint_snapshots(game.gid),
        )

    def _broadcast_room_timer(
        self,
        replies: list[tuple[bytes, Address]],
        game: CarbonGame,
        snapshot: bytes,
        *,
        source: Address | None = None,
    ) -> None:
        self.active_router.broadcast_room_timer(
            replies,
            game,
            snapshot,
            source=source,
        )

    @staticmethod
    def _state_value(logical: bytes) -> int | None:
        if logical_type(logical) != OLMessageType.ACTIVE_GAME_MESSAGE or len(logical) < 11:
            return None
        name_length = int.from_bytes(logical[5:7], "big")
        state_offset = 7 + name_length
        if state_offset + 4 > len(logical):
            return None
        return int.from_bytes(logical[state_offset:state_offset + 4], "big")

    def _retry_match_timer_for_endpoint(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.active_router.retry_match_timer(
            replies,
            addr,
            binding,
        )

    def _maybe_broadcast_ready_lock(
        self,
        replies: list[tuple[bytes, Address]],
        game: CarbonGame,
        source: Address,
    ) -> None:
        self.active_router.broadcast_ready_lock(
            replies,
            game,
            source,
        )

    def _broadcast_start_lock(
        self,
        replies: list[tuple[bytes, Address]],
        game: CarbonGame,
    ) -> None:
        self.active_router.broadcast_start_lock(
            replies,
            game,
        )

    def _append_start_lock_bundle(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        *,
        max_hosted_players: int | None = None,
        wire_flag0: bool = False,
        track_start_lock: bool = True,
    ) -> None:
        self.race_start.append_start_lock_bundle(
            replies,
            destination,
            self._wire[destination],
            max_hosted_players=max_hosted_players,
            wire_flag0=wire_flag0,
            track_start_lock=track_start_lock,
        )

    def _append_post_start_latency_if_acked(
        self,
        replies: list[tuple[bytes, Address]],
        addr: Address,
        binding: CarbonTicketResolution,
    ) -> None:
        self.race_start.append_post_start_latency_if_acked(
            replies,
            binding.game.gid,
            self._race[binding.game.gid],
            self._race_start_endpoint_snapshot(addr, binding),
        )

    def _broadcast_startloading(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        body: bytes,
    ) -> None:
        self.active_router.broadcast_startloading(
            replies,
            source,
            binding,
            body,
        )

    def _maybe_broadcast_startsync(
        self,
        replies: list[tuple[bytes, Address]],
        game: CarbonGame,
        source: Address,
    ) -> None:
        self.active_router.maybe_broadcast_startsync(
            replies,
            game,
            source,
        )

    def _append_active_bodies(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        bodies: tuple[bytes, ...] | list[bytes],
        *,
        footer: bool,
    ) -> None:
        binding = self._bindings[destination]
        wire = self._wire[destination]
        decorated: list[bytes] = []
        for logical in bodies:
            body = bytes(logical)
            if footer:
                body += wire.footer or self._footer_for(destination, binding)
                body += b"\x44"
            else:
                body += b"\x04"
            decorated.append(body)
        self.outbound.append_active_bodies(
            replies,
            destination,
            decorated,
        )

    @staticmethod
    def _commudp_aggregate_payload(
        records_newest_to_oldest: tuple[bytes, ...] | list[bytes],
    ) -> bytes:
        return EndpointPublisher.commudp_aggregate_payload(
            records_newest_to_oldest
        )

    def _append_active_record_batch(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        records_oldest_to_newest: tuple[bytes, ...] | list[bytes],
    ) -> int:
        return self.outbound.append_active_record_batch(
            replies,
            destination,
            records_oldest_to_newest,
        )

    def _append_active_body(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        body: bytes,
    ) -> int:
        return self.outbound.append_active_body(
            replies,
            destination,
            body,
        )

    def _append_datagram(
        self,
        replies: list[tuple[bytes, Address]],
        datagram: TunnelDatagram,
        addr: Address,
    ) -> None:
        self.outbound.append_datagram(replies, datagram, addr)

    def _append_packet_batches(
        self,
        replies: list[tuple[bytes, Address]],
        packets: list[TunnelPacket] | tuple[TunnelPacket, ...],
        addr: Address,
    ) -> int:
        return self.outbound.append_packet_batches(replies, packets, addr)

    @staticmethod
    def _take_server_sequence(wire: EndpointWireState) -> int:
        return EndpointPublisher.take_server_sequence(wire)

    @staticmethod
    def _sequence_acked(acknowledgement: int, target: int) -> bool:
        return EndpointLifecycleCoordinator._sequence_acked(
            acknowledgement,
            target,
        )

    @staticmethod
    def _source_key(resolution: CarbonTicketResolution) -> SourceKey:
        return (resolution.game.gid, resolution.participant.identity.user_id)

    @staticmethod
    def _is_host(resolution: CarbonTicketResolution) -> bool:
        game = resolution.game
        if not game.server_hosted:
            return resolution.participant.identity.user_id == game.host.user_id

        # Dedicated rooms are allocated on behalf of the first requester.
        # That allocator remains the player-side session coordinator for the
        # lifetime of the room. Never infer this role from player_id order:
        # Messenger-compatible PIDs are stable persona IDs, so a later guest
        # may legitimately have a numerically smaller PID (for example 3305
        # versus host 17954). min(player_id) therefore flips host/helper.
        coordinator_user_id = game.allocator_user_id
        if coordinator_user_id is None:
            first = next(iter(game.participants.values()), None)
            coordinator_user_id = first.identity.user_id if first is not None else None
        if coordinator_user_id is None:
            return False
        return resolution.participant.identity.user_id == int(coordinator_user_id)

    def _host_endpoint(self, game: CarbonGame) -> Address | None:
        if not game.server_hosted:
            return self._participant_endpoints.get((game.gid, game.host.user_id))
        coordinator_user_id = game.allocator_user_id
        if coordinator_user_id is None:
            first = next(iter(game.participants.values()), None)
            coordinator_user_id = first.identity.user_id if first is not None else None
        if coordinator_user_id is None:
            return None
        return self._participant_endpoints.get((game.gid, int(coordinator_user_id)))

    @staticmethod
    def _server_tick_ms() -> int:
        return int(time.monotonic() * 1000.0) & 0xFFFFFFFF

    @staticmethod
    def _tick_before(server_tick: int) -> int:
        """Return a non-zero tick distinct from *server_tick* modulo uint32."""
        tick = (int(server_tick) - 1) & 0xFFFFFFFF
        return tick or 0xFFFFFFFF

    @staticmethod
    def _tick_after(client_tick: int) -> int:
        """Return a non-zero tick strictly different from *client_tick*."""
        tick = (int(client_tick) + 1) & 0xFFFFFFFF
        return tick or 1

    @classmethod
    def _server_footer_from_client(cls, client_footer: bytes) -> bytes:
        raw = bytes(client_footer)
        server_tick = cls._server_tick_ms()
        client_tick = int.from_bytes(raw[:4], "big") if len(raw) >= 4 else 0
        if client_tick == 0:
            client_tick = cls._tick_before(server_tick)
        elif client_tick == server_tick:
            # Fast local clients can provide their transport timestamp in the
            # same millisecond in which HostHello is serialized.  The retail
            # working trace keeps the client sample and advances the server
            # sample by one tick (client=N, server=N+1).  V791 only repaired
            # the fallback path, so a real client footer could still leave a
            # zero delta and the invited helper would never emit OLMSG 0x02.
            server_tick = cls._tick_after(client_tick)
        return (
            client_tick.to_bytes(4, "big")
            + server_tick.to_bytes(4, "big")
            + b"\x00" * 4
        )

    def _footer_for(self, addr: Address, resolution: CarbonTicketResolution) -> bytes:
        del resolution
        server_tick = self._server_tick_ms()
        wire = self._wire.get(addr)
        client_tick = (
            int(wire.fallback_client_tick_ms) & 0xFFFFFFFF
            if wire is not None
            else 0
        )
        if client_tick == 0 or client_tick == server_tick:
            client_tick = self._tick_before(server_tick)
        return (
            client_tick.to_bytes(4, "big")
            + server_tick.to_bytes(4, "big")
            + b"\x00" * 4
        )

    def _bound_participants(self, game: CarbonGame) -> tuple[BoundParticipant, ...]:
        result: list[BoundParticipant] = []
        # Carbon's player slot is the directory insertion index. Keep this
        # exact order here because descriptor handles and rewritten 0x1E
        # session objects use ``game.participants.values()`` for that index.
        # Numeric PID sorting happens to be equivalent for synthetic 1/2 test
        # identities, but not for real persona-derived PIDs such as
        # 31094, 17954 and 3305.
        for participant in game.participants.values():
            endpoint = self._participant_endpoints.get((game.gid, participant.identity.user_id))
            if endpoint is not None:
                result.append(BoundParticipant(participant, endpoint))
        return tuple(result)

    def binding(self, addr: Address) -> CarbonTicketResolution | None:
        with self._lock:
            return self._bindings.get(addr)

    def reply_spacing_seconds_for(self, addr: Address) -> float:
        """Keep the retail Join gap without throttling live race relay.

        The 12 ms listener gap exists for the setup-time Join/session
        descriptor pair. Once a destination enters RACING, applying it to
        every world-state reply only adds queueing between otherwise
        independent endpoints.
        """
        with self._lock:
            binding = self._bindings.get(addr)
            if binding is None:
                return _SETUP_REPLY_SPACING_SECONDS
            race = self._race.get(binding.game.gid)
            if race is not None and race.phase >= RacePhase.RACING:
                return 0.0
            return _SETUP_REPLY_SPACING_SECONDS

    def session_endpoints(self, gid: str) -> tuple[Address, ...]:
        """Return bound endpoints with the room coordinator first."""
        with self._lock:
            endpoints: list[tuple[int, int, Address]] = []
            for addr, resolution in self._bindings.items():
                if resolution.game.gid != str(gid):
                    continue
                game = resolution.game
                coordinator_user_id = game.allocator_user_id
                if game.server_hosted and coordinator_user_id is None:
                    first = next(iter(game.participants.values()), None)
                    coordinator_user_id = first.identity.user_id if first is not None else None
                is_coordinator = (
                    game.server_hosted
                    and coordinator_user_id is not None
                    and resolution.participant.identity.user_id == int(coordinator_user_id)
                )
                endpoints.append(
                    (
                        0 if is_coordinator else 1,
                        int(resolution.participant.player_id),
                        addr,
                    )
                )
            return tuple(addr for _, _, addr in sorted(endpoints))

    def peers(self, addr: Address) -> tuple[Address, ...]:
        with self._lock:
            binding = self._bindings.get(addr)
            if binding is None:
                return ()
            return tuple(peer for peer in self.session_endpoints(binding.game.gid) if peer != addr)

    def stats(self) -> RebroadcasterStats:
        with self._lock:
            return RebroadcasterStats(
                self._received,
                self._rejected,
                self._started,
                len(self._bindings),
                self._tickets_rejected,
            )
