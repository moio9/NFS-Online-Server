"""Receiver-local Carbon session-object publication.

``SessionObjectCoordinator`` owns object-id allocation, receiver rewrites and
per-room publication caches.  The rebroadcaster service decides *when* a
session fragment belongs in a flow; this component decides *which* fragments
are still missing and serializes them through the endpoint publisher.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import logging

from carbon.gamemanager.session_codec import encode_active
from carbon.gamemanager.session_object import (
    SESSION_OBJECT_CHUNK_OFFSETS,
    first_block_identity,
    is_session_object_complete,
    parse_session_object_block,
    rewrite_for_receiver,
    unique_blocks,
)
from carbon.rebroadcaster.outbound import EndpointPublisher
from carbon.rebroadcaster.state import Address, EndpointWireState, SourceKey
from carbon.theater.directory import CarbonTicketResolution
from carbon.transport.prototunnel import TunnelDatagram, TunnelPacket


_SEQUENCE_MASK = 0x0FFFFFFF


class SessionObjectCoordinator:
    """Allocate and publish receiver-local views of session objects."""

    def __init__(
        self,
        outbound: EndpointPublisher,
        wires: MutableMapping[Address, EndpointWireState],
        bindings: Mapping[Address, CarbonTicketResolution],
        *,
        is_host: Callable[[CarbonTicketResolution], bool],
        logger: logging.Logger | None = None,
    ) -> None:
        self.outbound = outbound
        self._wires = wires
        self._bindings = bindings
        self._is_host = is_host
        self.log = logger or logging.getLogger(__name__)

        # Challenge invite captures assign the remote object namespace
        # centrally (host -> helper: 0x11, helper -> host: 0x12).  The source
        # object's generation is part of the identity because clients can
        # publish several complete generations during one room lifetime.
        self._dedicated_object_ids: dict[
            tuple[str, SourceKey, int], int
        ] = {}
        self._dedicated_object_next: dict[str, int] = {}

    def clear_room(self, gid: str) -> None:
        """Discard allocator state after the final endpoint leaves a room."""
        room_id = str(gid)
        self._dedicated_object_ids = {
            key: value
            for key, value in self._dedicated_object_ids.items()
            if key[0] != room_id
        }
        self._dedicated_object_next.pop(room_id, None)

    def append_local_parts(
        self,
        replies: list[tuple[bytes, Address]],
        address: Address,
        binding: CarbonTicketResolution,
        *,
        offsets: set[int] | None = None,
    ) -> int:
        selected, reflected_object_id, local_slot = self.select_local_parts(
            address,
            binding,
            offsets=offsets,
        )
        for block in selected:
            self.outbound.append_active_body(
                replies,
                address,
                block + b"\x04",
                confirmation="session-local-object",
            )
        if selected:
            self.log.info(
                "Carbon GM release local session object reflected: gid=%s "
                "dst=%s:%d object=%d slot=%d offsets=%s",
                binding.game.gid,
                address[0],
                address[1],
                reflected_object_id,
                local_slot,
                ",".join(
                    hex(int.from_bytes(block[13:17], "big"))
                    for block in selected
                ),
            )
        return len(selected)

    def select_local_parts(
        self,
        address: Address,
        binding: CarbonTicketResolution,
        *,
        offsets: set[int] | None = None,
    ) -> tuple[tuple[bytes, ...], int, int]:
        wire = self._wires[address]
        source_key = self._source_key(binding)
        already = wire.published_session_offsets.setdefault(source_key, set())
        requested = (
            set(SESSION_OBJECT_CHUNK_OFFSETS)
            if offsets is None
            else set(offsets)
        )
        missing = requested - already
        if not missing:
            return (), int(wire.local_reflected_object_id), 0

        parsed = unique_blocks(wire.session_blocks.values())
        first = next((item for item in parsed if item.offset == 0), None)
        if first is None:
            return (), int(wire.local_reflected_object_id), 0
        participants = tuple(binding.game.participants.values())
        local_slot = next(
            (
                index
                for index, participant in enumerate(participants)
                if (
                    participant.identity.user_id
                    == binding.participant.identity.user_id
                )
            ),
            max(0, int(binding.participant.player_id) - 1),
        )
        reflected_object_id = int(wire.local_reflected_object_id)
        if reflected_object_id <= 0:
            used_ids = set(wire.remote_object_ids)
            for raw in wire.session_blocks.values():
                parsed_block = parse_session_object_block(raw)
                if parsed_block is not None and parsed_block.object_id:
                    used_ids.add(parsed_block.object_id)
            reflected_object_id = max(used_ids, default=1) + 1
            wire.local_reflected_object_id = reflected_object_id
            wire.remote_object_ids.add(reflected_object_id)
        rewritten = rewrite_for_receiver(
            wire.session_blocks.values(),
            remote_object_id=reflected_object_id,
            remote_slot=local_slot,
        )
        selected = tuple(
            block
            for block in rewritten
            if int.from_bytes(block[13:17], "big") in missing
        )
        for block in selected:
            already.add(int.from_bytes(block[13:17], "big"))
        return selected, reflected_object_id, local_slot

    def append_remote_parts(
        self,
        replies: list[tuple[bytes, Address]],
        source: Address,
        destination: Address,
        *,
        offsets: set[int],
        bundle: bool = False,
        prefer_invite_sequence: bool = True,
    ) -> int:
        selected = self.select_remote_parts(
            source,
            destination,
            offsets=offsets,
        )
        if bundle and len(selected) > 1:
            destination_wire = self._wires[destination]
            acknowledgement = (
                int(destination_wire.invite_join_sequence) & _SEQUENCE_MASK
                if (
                    prefer_invite_sequence
                    and destination_wire.invite_join_sequence
                )
                else int(destination_wire.last_client_sequence) & _SEQUENCE_MASK
            )
            packets = tuple(
                TunnelPacket(
                    1,
                    encode_active(
                        self.outbound.take_server_sequence(destination_wire),
                        acknowledgement,
                        block + b"\x04",
                    ),
                )
                for block in selected
            )
            self.outbound.append_datagram(
                replies,
                TunnelDatagram(destination_wire.next_offset_words, packets),
                destination,
                confirmation="session-remote-object",
            )
        else:
            for block in selected:
                self.outbound.append_active_body(
                    replies,
                    destination,
                    block + b"\x04",
                    confirmation="session-remote-object",
                )
        if not selected:
            return 0

        source_binding = self._bindings[source]
        destination_binding = self._bindings[destination]
        destination_wire = self._wires[destination]
        selected_offsets = {
            int.from_bytes(block[13:17], "big")
            for block in selected
        }
        if (
            self._is_host(source_binding)
            and bool(destination_binding.participant.invite_remote_player_id)
            and not destination_wire.session_confirmed
            and 0x3C8 in selected_offsets
        ):
            destination_wire.invite_host_continuation_final_sequence = (
                int(destination_wire.next_server_sequence) - 1
            ) & _SEQUENCE_MASK

        # Continuation-only sends have no offset-zero identity block.  Report
        # the cached complete object instead of the placeholder fields found
        # in an isolated continuation.
        source_key = self._source_key(source_binding)
        cached_remote = destination_wire.published_remote_objects.get(
            source_key,
            selected,
        )
        source_object_id, source_pid, source_name = first_block_identity(
            cached_remote
        )
        direction = (
            "V681 reciprocal joiner session object"
            if not self._is_host(source_binding)
            else "V622 remote host session object"
        )
        self.log.info(
            "Carbon GM release %s sent: gid=%s source=%s:%d "
            "destination=%s:%d remote_object=%d offsets=%s pid=%d "
            "name=%s blocks=%d ack=%07x",
            direction,
            source_binding.game.gid,
            source[0],
            source[1],
            destination[0],
            destination[1],
            source_object_id,
            ",".join(
                hex(int.from_bytes(block[13:17], "big"))
                for block in selected
            ),
            source_pid,
            source_name or "-",
            len(selected),
            (
                int(destination_wire.invite_join_sequence) & _SEQUENCE_MASK
                if (
                    bundle
                    and prefer_invite_sequence
                    and destination_wire.invite_join_sequence
                )
                else int(destination_wire.last_client_sequence) & _SEQUENCE_MASK
            ),
        )
        return len(selected)

    def select_remote_parts(
        self,
        source: Address,
        destination: Address,
        *,
        offsets: set[int],
        allow_pending_bootstrap: bool = False,
    ) -> tuple[bytes, ...]:
        prepared = self._remote_blocks(
            source,
            destination,
            allow_pending_bootstrap=allow_pending_bootstrap,
        )
        if prepared is None:
            return ()
        source_key, rewritten = prepared
        destination_wire = self._wires[destination]
        already = destination_wire.published_session_offsets.setdefault(
            source_key,
            set(),
        )
        selected = tuple(
            block
            for block in rewritten
            if int.from_bytes(block[13:17], "big") in offsets
            and int.from_bytes(block[13:17], "big") not in already
        )
        for block in selected:
            already.add(int.from_bytes(block[13:17], "big"))
        return selected

    def _remote_blocks(
        self,
        source: Address,
        destination: Address,
        *,
        allow_pending_bootstrap: bool = False,
    ) -> tuple[SourceKey, tuple[bytes, ...]] | None:
        source_binding = self._bindings.get(source)
        destination_binding = self._bindings.get(destination)
        source_wire = self._wires.get(source)
        destination_wire = self._wires.get(destination)
        if (
            source_binding is None
            or destination_binding is None
            or source_wire is None
            or destination_wire is None
            or source_binding.game.gid != destination_binding.game.gid
            or (
                not allow_pending_bootstrap
                and not destination_wire.session_bootstrap_sent
            )
            or not is_session_object_complete(source_wire.session_blocks.values())
        ):
            return None
        source_key = self._source_key(source_binding)
        cached = destination_wire.published_remote_objects.get(source_key)
        if cached is not None:
            return source_key, cached

        game = source_binding.game
        if (
            game.server_hosted
            and str(game.properties.get("B-U-game_type", "")) == "2"
        ):
            generation_key = (
                game.gid,
                source_key,
                int(source_wire.session_object_id),
            )
            remote_object_id = self._dedicated_object_ids.get(generation_key)
            if remote_object_id is None:
                remote_object_id = self._dedicated_object_next.get(
                    game.gid,
                    0x11,
                )
                self._dedicated_object_ids[generation_key] = remote_object_id
                self._dedicated_object_next[game.gid] = remote_object_id + 1
        else:
            used_ids = set(destination_wire.remote_object_ids)
            for raw in destination_wire.session_blocks.values():
                parsed = parse_session_object_block(raw)
                if parsed is not None and parsed.object_id:
                    used_ids.add(parsed.object_id)
            remote_object_id = max(used_ids, default=1) + 1
        participants = tuple(game.participants.values())
        remote_slot = next(
            (
                index
                for index, participant in enumerate(participants)
                if (
                    participant.identity.user_id
                    == source_binding.participant.identity.user_id
                )
            ),
            max(0, int(source_binding.participant.player_id) - 1),
        )
        rewritten = rewrite_for_receiver(
            source_wire.session_blocks.values(),
            remote_object_id=remote_object_id,
            remote_slot=remote_slot,
        )
        destination_wire.remote_object_ids.add(remote_object_id)
        destination_wire.published_remote_objects[source_key] = rewritten
        destination_wire.published_session_offsets.setdefault(source_key, set())
        return source_key, rewritten

    @staticmethod
    def _source_key(resolution: CarbonTicketResolution) -> SourceKey:
        return (resolution.game.gid, resolution.participant.identity.user_id)
