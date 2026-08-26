"""Live gameplay publication for Carbon rebroadcaster endpoints.

``GameplayRelayCoordinator`` owns destination-local publication of live race
records after the room/session bootstrap has completed.  Endpoint discovery,
room membership and the invite pre-confirm barrier remain owned by the UDP
service and are supplied through narrow callbacks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
import logging
import time

from carbon.gamemanager.protocol import OLMessageType
from carbon.gamemanager.race_session import logical_type
from carbon.gamemanager.race_state import GameRaceState, RacePhase
from carbon.rebroadcaster.ai_registration import AIRegistrationCoordinator
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.state import Address, EndpointWireState
from carbon.rebroadcaster.world_state import NetGameLinkWorldState
from carbon.theater.directory import CarbonTicketResolution


SessionEndpoints = Callable[[str], tuple[Address, ...]]
HoldPreconfirm = Callable[[Address, bytes], bool]
FooterFor = Callable[[Address, CarbonTicketResolution], bytes]
ClockMilliseconds = Callable[[], int]


class GameplayRelayCoordinator:
    """Relay live race records without owning room or endpoint lifecycle."""

    def __init__(
        self,
        publisher: EndpointPublisher,
        ai_registration: AIRegistrationCoordinator,
        world_state: NetGameLinkWorldState,
        wires: Mapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        races: MutableMapping[str, GameRaceState],
        *,
        session_endpoints: SessionEndpoints,
        hold_preconfirm: HoldPreconfirm,
        footer_for: FooterFor,
        clock_ms: ClockMilliseconds,
        logger: logging.Logger | None = None,
    ) -> None:
        self.publisher = publisher
        self.ai_registration = ai_registration
        self.world_state = world_state
        self._wires = wires
        self._bindings = bindings
        self._races = races
        self._session_endpoints = session_endpoints
        self._hold_preconfirm = hold_preconfirm
        self._footer_for = footer_for
        self._clock_ms = clock_ms
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def current_player_controlled_ai_body(logical: bytes) -> bytes | None:
        return AIRegistrationCoordinator.current_body(logical)

    def relay_world_states(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        logicals: Sequence[bytes],
    ) -> None:
        source_wire = self._wires[source]
        bodies = tuple(bytes(item) for item in logicals)
        if not source_wire.gameplay_ready or not bodies:
            return

        sent = 0
        for destination in self._session_endpoints(binding.game.gid):
            if not self._wires[destination].gameplay_ready:
                continue
            self.append_virtual_world_bodies(replies, destination, bodies)
            sent += 1
        now = time.monotonic()
        if sent and now >= source_wire.world_state_log_not_before:
            source_wire.world_state_log_not_before = now + 5.0
            self.log.info(
                "Carbon GM release V823 kind5 world-state relay: "
                "gid=%s src=%s:%d endpoints=%d states=%d type6=%d type7=%d "
                "sequence=destination-local-80-ff footer_cadence=time>250ms "
                "reliable_unchanged=1",
                binding.game.gid,
                source[0],
                source[1],
                sent,
                len(bodies),
                sum(logical_type(body) == OLMessageType.CAR_STATE for body in bodies),
                sum(
                    logical_type(body) == OLMessageType.CAR_STATE_BLOCK
                    for body in bodies
                ),
            )

    def append_virtual_world_bodies(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        bodies: Sequence[bytes],
    ) -> tuple[int, ...]:
        """Serialize opaque NetGameLink state without reliable advancement."""
        wire = self._wires[destination]
        binding = self._bindings[destination]
        batch = self.world_state.build_virtual_datagram(
            wire,
            destination,
            binding,
            tuple(bytes(item) for item in bodies),
            clock_ms=self._clock_ms,
        )
        if batch.datagram.packets:
            self.publisher.append_datagram(
                replies,
                batch.datagram,
                destination,
            )
        return batch.sequences

    def relay_pursuit_tag_sync(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        logical: bytes,
    ) -> None:
        """Reflect the current Pursuit Tag ownership/bust state to the room."""
        race = self._races.setdefault(binding.game.gid, GameRaceState())
        if race.phase != RacePhase.RACING:
            return
        if len(logical) < 17:
            self.log.warning(
                "Carbon GM rejected truncated PursuitTagSync: "
                "gid=%s src=%s:%d bytes=%d",
                binding.game.gid,
                source[0],
                source[1],
                len(logical),
            )
            return

        current = bytes(logical[:17])
        if logical_type(current) != OLMessageType.PURSUIT_TAG_SYNC:
            return

        sent = 0
        for destination in self._session_endpoints(binding.game.gid):
            if not self._wires[destination].gameplay_ready:
                continue
            self.publisher.append_active_body(replies, destination, current + b"\x04")
            sent += 1

        source_wire = self._wires[source]
        now = time.monotonic()
        transition = current[7:9] != b"\xff\xf8"
        if sent and (transition or now >= source_wire.pursuit_tag_log_not_before):
            if not transition:
                source_wire.pursuit_tag_log_not_before = now + 5.0
            self.log.info(
                "Carbon GM release V831 PursuitTagSync relayed: "
                "gid=%s src=%s:%d endpoints=%d car=%04x state=%04x "
                "payload=%s transition=%d cadence=client-native",
                binding.game.gid,
                source[0],
                source[1],
                sent,
                int.from_bytes(current[5:7], "big"),
                int.from_bytes(current[7:9], "big"),
                current[9:17].hex(),
                int(transition),
            )

    def relay_player_controlled_ai(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        logicals: Sequence[bytes],
    ) -> None:
        destinations = tuple(
            (destination, self._wires[destination])
            for destination in self._session_endpoints(binding.game.gid)
        )
        self.ai_registration.relay(
            replies,
            source,
            gid=binding.game.gid,
            race=self._races.setdefault(binding.game.gid, GameRaceState()),
            destinations=destinations,
            logicals=logicals,
        )

    def update_ai_registration_delivery(
        self,
        replies: list[tuple[bytes, Address]],
        destination: Address,
        binding: CarbonTicketResolution,
        *,
        force_retry: bool = False,
        reason: str = "ack-gap",
        now: float | None = None,
    ) -> bool:
        return self.ai_registration.update_delivery(
            replies,
            destination,
            self._wires[destination],
            self._races.get(binding.game.gid),
            gid=binding.game.gid,
            player_id=binding.participant.player_id,
            force_retry=force_retry,
            reason=reason,
            now=now,
        )

    def reflect_logical_to_room(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        logical: bytes,
        *,
        confirmation: str | None = None,
    ) -> int:
        """Reflect one client OLMSG compound to every room endpoint."""
        sent = 0
        for destination in self._session_endpoints(binding.game.gid):
            if self._hold_preconfirm(destination, logical):
                continue
            destination_binding = self._bindings[destination]
            destination_wire = self._wires[destination]
            body = bytes(logical)
            if destination == source:
                body += b"\x04"
            else:
                body += destination_wire.footer or self._footer_for(
                    destination,
                    destination_binding,
                )
                body += b"\x44"
            self.publisher.append_active_body(
                replies,
                destination,
                body,
                confirmation=confirmation,
            )
            sent += 1
        return sent

    def relay_logical_to_peers(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        binding: CarbonTicketResolution,
        logical: bytes,
        *,
        footer: bool,
        confirmation: str | None = None,
    ) -> int:
        sent = 0
        for destination in self._session_endpoints(binding.game.gid):
            if destination == source:
                continue
            if self._hold_preconfirm(destination, logical):
                continue
            destination_binding = self._bindings[destination]
            destination_wire = self._wires[destination]
            body = bytes(logical)
            if footer:
                body += destination_wire.footer or self._footer_for(
                    destination,
                    destination_binding,
                )
                body += b"\x44"
            else:
                body += b"\x04"
            self.publisher.append_active_body(
                replies,
                destination,
                body,
                confirmation=confirmation,
            )
            sent += 1
        return sent
