"""EA race UDP relay for classic clients using the ASI destination wrapper."""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
from threading import RLock
from typing import Iterator

from classic.ea.directory import GameSession, SessionState


log = logging.getLogger(__name__)
Address = tuple[str, int]
Identity = tuple[int, int]
ChannelReply = tuple[bytes, Address, int]
Route = tuple[Identity, tuple[Identity, ...]]
U2_IDENTITY_MARKER = b"U2I1"

# Carrier-grade NAT space is not routed on the public Internet and gives each
# participant a stable, distinct sockaddr when several clients share one IP.
DEFAULT_VIRTUAL_NETWORK = "100.64.0.0/10"
MW_GAME_PORT = 3658


class ClassicRaceRelay:
    """Forward datagrams between registered game participants.

    Clients prepend ``destination port (network order) + destination IPv4`` to
    each game payload.  The stock MW eight-byte 1/5/2 bootstrap exchange is
    translated so channelized peers see the token expected by their native
    connection state.
    """

    def __init__(self, *, virtual_network: str = DEFAULT_VIRTUAL_NETWORK) -> None:
        network = ipaddress.ip_network(virtual_network, strict=True)
        if network.version != 4 or network.num_addresses < 4:
            raise ValueError("race relay virtual_network must be an IPv4 network")

        self._lock = RLock()
        self._public_host = ""
        self._virtual_hosts: Iterator[ipaddress.IPv4Address] = network.hosts()
        self._next_game_token = 1
        self._game_tokens: dict[int, int] = {}
        self._token_to_game: dict[int, GameSession] = {}
        self._participants: dict[int, tuple[Identity, ...]] = {}
        self._public_tokens: set[int] = set()
        self._channelized_tokens: set[int] = set()
        self._mw_channelized_tokens: set[int] = set()
        # The older working MW relay exposes one public UDP port to clients
        # on different devices. The ASI wrapper already carries the virtual
        # destination, so per-player WAN ports only add a forwarding failure
        # point without adding routing information.
        self._mw_shared_port_tokens: set[int] = set()
        # U2 clients advertising the U2I1 wrapper send and receive through the
        # base relay listener. Track capability per identity so legacy clients
        # can still receive on their original viewer-specific channel.
        self._u2_shared_port_identities: set[Identity] = set()
        # Every MW guest owns a distinct endpoint-discovery token, while the
        # owner exposes its own local token to every accepted spoke.  Keep the
        # guest tokens per identity and translate each directed control packet
        # at delivery time.
        self._mw_bootstrap_tokens: dict[Identity, int] = {}
        # The owner creates one CommUDP connection record per guest, and each
        # record can carry a different local token.  Key by the guest spoke,
        # not by the whole game, so late players cannot inherit OPPO1 state.
        self._mw_owner_bootstrap_tokens: dict[Identity, int] = {}
        # Dedicated-port/legacy MW can expose only one owner socket. Keep a
        # game-wide fallback there, but never use it for shared-port spokes.
        self._mw_owner_fallback_tokens: dict[int, int] = {}
        self._virtual_to_identity: dict[str, Identity] = {}
        self._identity_to_virtual: dict[Identity, str] = {}
        self._wire_alias_to_identities: dict[str, list[Identity]] = {}
        self._identity_to_wire_alias: dict[Identity, str] = {}
        self._identity_to_endpoint: dict[Identity, Address] = {}
        self._endpoint_to_identity: dict[Address, Identity] = {}
        self._pending: dict[Identity, list[tuple[Identity, bytes, int]]] = {}
        # U2 can create one connected UDP socket per opponent.  A participant
        # therefore may have several physical source endpoints on the same
        # viewer channel.  Preserve the directed peer relation so a packet
        # from B to A is returned to A's socket that was opened toward B,
        # instead of whichever A socket happened to send most recently.
        self._directed_endpoints: dict[tuple[Identity, Identity], Address] = {}
        self._directed_pending: dict[
            tuple[Identity, Identity],
            list[tuple[bytes, int]],
        ] = {}
        # MW recreates the lobby game after a race. The host may return before
        # one or more guests have left the old gameplay/post-race transport, so a
        # handoff must temporarily support both generations: old endpoints stay
        # routable until that guest rejoins through GJOI and publishes its fresh
        # command-1 socket. Stable virtual identities are shared by both views.
        self._mw_handoff_candidates: dict[int, tuple[Identity, ...]] = {}
        self._mw_handoff_rebind_order: dict[int, list[Identity]] = {}
        self._mw_handoff_endpoint_hints: dict[
            int, dict[Identity, Address]
        ] = {}
        # Completed owner->guest command-2 deliveries are transport-owned
        # proof that a MW spoke is usable. The app drains these notifications
        # after sending the datagram, so lobby join serialization need not rely
        # exclusively on the client's sometimes-stale LT projection.
        self._mw_settled_links: set[tuple[int, int]] = set()

    def drain_mw_settled_links(self) -> tuple[tuple[int, int], ...]:
        """Return newly completed ``(game_id, guest_user_id)`` MW spokes."""

        with self._lock:
            settled = tuple(sorted(self._mw_settled_links))
            self._mw_settled_links.clear()
            return settled

    def set_public_host(self, host: str) -> None:
        resolved = socket.gethostbyname(str(host))
        socket.inet_aton(resolved)
        with self._lock:
            self._public_host = resolved

    def _allocate_virtual(self) -> str:
        try:
            address = next(self._virtual_hosts)
        except StopIteration as exc:
            raise RuntimeError("race relay virtual address pool exhausted") from exc
        return str(address)

    def register_game(
        self,
        game: GameSession,
        *,
        advertise_public: bool = True,
    ) -> dict[int, str]:
        """Register participants and return their relay-facing addresses."""

        object_key = id(game)
        with self._lock:
            token = self._game_tokens.get(object_key)
            if token is None:
                token = self._next_game_token
                self._next_game_token += 1
                self._game_tokens[object_key] = token

            ordered_users = list(game.ordered_participants())
            current_identities = {
                (token, int(user_id)) for user_id in ordered_users
            }
            handoff_candidates = self._mw_handoff_candidates.get(token, ())
            if not handoff_candidates:
                # GLEA/ULEA keeps the owner's shared MW relay alive, but the
                # departed guest's physical socket must not survive that
                # membership change.  Otherwise a later GJOI reuses the dead
                # endpoint and rejects the returning client's command-1 probe.
                known_identities = set(self._participants.get(token, ()))
                known_identities.update(
                    identity
                    for identity in self._identity_to_virtual
                    if identity[0] == token
                )
                departed = known_identities - current_identities
                for identity in departed:
                    self._purge_identity_transport(
                        identity,
                        remove_virtual=True,
                    )
                if departed:
                    log.info(
                        "EA race UDP retired departed participant transport: "
                        "game=%d users=%s",
                        token,
                        ",".join(
                            str(identity[1])
                            for identity in sorted(departed)
                        ),
                    )

            identities: list[Identity] = []
            advertised: dict[int, str] = {}
            for user_id in ordered_users:
                identity = (token, int(user_id))
                virtual = self._identity_to_virtual.get(identity)
                if virtual is None:
                    virtual = self._allocate_virtual()
                    self._identity_to_virtual[identity] = virtual
                    self._virtual_to_identity[virtual] = identity
                identities.append(identity)
                advertised[int(user_id)] = (
                    self._public_host
                    if advertise_public and self._public_host
                    else virtual
                )

                wire_id = int(
                    getattr(game, "participant_wire_ids", {}).get(
                        int(user_id),
                        0,
                    )
                    or 0
                )
                if 0 < wire_id <= 0xFFFFFFFF:
                    # Stock MW turns OPID 1/2 into the little-endian pseudo
                    # hosts 1.0.0.0/2.0.0.0.  They are carried only inside the
                    # ASI wrapper and must resolve to the same relay identities
                    # as the advertised virtual addresses.
                    alias = socket.inet_ntoa(struct.pack("<I", wire_id))
                    previous_alias = self._identity_to_wire_alias.get(identity)
                    if previous_alias is not None and previous_alias != alias:
                        previous_aliases = self._wire_alias_to_identities.get(
                            previous_alias,
                            [],
                        )
                        previous_aliases = [
                            candidate
                            for candidate in previous_aliases
                            if candidate != identity
                        ]
                        if previous_aliases:
                            self._wire_alias_to_identities[previous_alias] = (
                                previous_aliases
                            )
                        else:
                            self._wire_alias_to_identities.pop(
                                previous_alias,
                                None,
                            )
                    aliases = self._wire_alias_to_identities.setdefault(
                        alias,
                        [],
                    )
                    if identity in aliases:
                        aliases.remove(identity)
                    aliases.append(identity)
                    self._identity_to_wire_alias[identity] = alias

            transport_identities = tuple(identities)
            if handoff_candidates:
                current = set(identities)
                pending = list(handoff_candidates)

                # Once the replacement race actually starts, an old participant
                # which never rejoined cannot legally enter it. Retire only those
                # absent identities; guests already present in the lobby remain
                # eligible to publish their new command-1 socket after GSTA.
                if game.state is not SessionState.OPEN:
                    absent = [identity for identity in pending if identity not in current]
                    for identity in absent:
                        self._drop_mw_handoff_identity(token, identity)
                    pending = [identity for identity in pending if identity in current]
                    if pending:
                        self._mw_handoff_candidates[token] = tuple(pending)
                    else:
                        self._finish_mw_handoff(token)

                if pending:
                    pending_set = set(pending)
                    queue = [
                        identity
                        for identity in self._mw_handoff_rebind_order.get(token, [])
                        if identity in pending_set and identity in current
                    ]
                    # register_game is called after every successful GJOI. Keep
                    # the first appearance of each returning guest, which records
                    # real GJOI order even when participant_order was seeded from
                    # the previous room.
                    for identity in identities:
                        if identity in pending_set and identity not in queue:
                            queue.append(identity)
                    self._mw_handoff_rebind_order[token] = queue

                    # Keep old transport identities visible to the relay until
                    # their replacement socket is confirmed. This is the critical
                    # host-first case: the lobby contains only the owner, while
                    # guests are still sending post-race traffic on old sockets.
                    retained: list[Identity] = []
                    for identity in self._participants.get(token, ()):
                        if (
                            identity in current
                            or identity in pending_set
                            or identity == (token, int(game.owner_id))
                        ) and identity not in retained:
                            retained.append(identity)
                    for identity in identities:
                        if identity not in retained:
                            retained.append(identity)
                    transport_identities = tuple(retained)

            self._participants[token] = transport_identities
            self._token_to_game[token] = game
            if advertise_public:
                self._public_tokens.add(token)
            else:
                self._public_tokens.discard(token)
            return advertised

    def _register_channelized_game(
        self,
        game: GameSession,
        *,
        mw_bootstrap_translation: bool,
        mw_shared_port: bool = False,
    ) -> dict[int, str]:
        advertised = self.register_game(game, advertise_public=False)
        with self._lock:
            token = self._game_tokens.get(id(game))
            if token is not None:
                self._channelized_tokens.add(token)
                if mw_bootstrap_translation:
                    self._mw_channelized_tokens.add(token)
                else:
                    self._mw_channelized_tokens.discard(token)
                if mw_shared_port:
                    self._mw_shared_port_tokens.add(token)
                    # Preserve an already-established first owner/guest spoke
                    # before late guests make the owner's generic endpoint move.
                    participants = self._participants.get(token, ())
                    owner = self._owner(token)
                    owner_endpoint = self._identity_to_endpoint.get(owner) if owner is not None else None
                    if owner is not None and owner_endpoint is not None and len(participants) > 2:
                        for guest in participants:
                            if guest == owner:
                                continue
                            guest_endpoint = self._identity_to_endpoint.get(guest)
                            if guest_endpoint is None:
                                continue
                            self._directed_endpoints.setdefault((owner, guest), owner_endpoint)
                            self._directed_endpoints.setdefault((guest, owner), guest_endpoint)
                else:
                    self._mw_shared_port_tokens.discard(token)
        return advertised

    def register_virtual_game(self, game: GameSession) -> dict[int, str]:
        """Register a channelized MW game with virtual peer addresses."""

        return self._register_channelized_game(
            game,
            mw_bootstrap_translation=True,
        )

    def register_shared_virtual_game(self, game: GameSession) -> dict[int, str]:
        """Register MW peers behind one public relay port.

        Each packet still names its virtual destination in the ASI wrapper;
        only the Internet-facing UDP port is shared.
        """

        return self._register_channelized_game(
            game,
            mw_bootstrap_translation=True,
            mw_shared_port=True,
        )

    def register_u2_virtual_game(self, game: GameSession) -> dict[int, str]:
        """Register a channelized U2 game without MW token translation."""

        return self._register_channelized_game(
            game,
            mw_bootstrap_translation=False,
        )

    def handoff_game(
        self,
        previous: GameSession,
        replacement: GameSession,
    ) -> dict[int, str] | None:
        """Move stable MW virtual identities to a replacement lobby game.

        Retail MW returns from a completed race by creating a new lobby game.
        The virtual OPPO addresses must stay stable. Physical sockets eventually
        change too, but not atomically: when the host returns first, guests can
        keep sending the old post-race transport for several seconds. Re-key the
        relay token while preserving that old generation, then replace each guest
        edge only after its successful GJOI is followed by a fresh command 1.
        """

        previous_key = id(previous)
        replacement_key = id(replacement)
        if int(previous.owner_id) != int(replacement.owner_id):
            return None

        with self._lock:
            token = self._game_tokens.get(previous_key)
            if token is None:
                return None

            replacement_token = self._game_tokens.get(replacement_key)
            if replacement_token is not None and replacement_token != token:
                return None

            previous_order = self._participants.get(token, ())
            if not previous_order:
                previous_order = tuple(
                    (token, int(user_id))
                    for user_id in previous.ordered_participants()
                )
            owner = (token, int(replacement.owner_id))
            if not previous_order or owner not in previous_order:
                return None

            advertised: dict[int, str] = {}
            for identity in previous_order:
                virtual = self._identity_to_virtual.get(identity)
                if virtual is None:
                    return None
                advertised[identity[1]] = (
                    self._public_host
                    if token in self._public_tokens and self._public_host
                    else virtual
                )

            guest_candidates = tuple(
                identity for identity in previous_order if identity != owner
            )
            endpoint_hints = {
                identity: endpoint
                for identity in guest_candidates
                if (endpoint := self._identity_to_endpoint.get(identity))
                is not None
            }

            # Do not remove endpoint or directed-route bindings here. In the
            # host-first flow they are still the only path used by guests which
            # have not returned to the lobby yet. Only queued payloads are unsafe
            # to carry across generations; live routes and bootstrap tokens are
            # retained until the corresponding guest confirms its new socket.
            preserved_endpoint_count = sum(
                1
                for identity in previous_order
                if identity in self._identity_to_endpoint
            )
            preserved_directed_count = sum(
                1
                for edge in self._directed_endpoints
                if edge[0][0] == token or edge[1][0] == token
            )
            for target, pending in tuple(self._pending.items()):
                retained = [item for item in pending if item[0][0] != token]
                if target[0] == token or not retained:
                    self._pending.pop(target, None)
                else:
                    self._pending[target] = retained
            for edge in tuple(self._directed_pending):
                if edge[0][0] == token or edge[1][0] == token:
                    self._directed_pending.pop(edge, None)

            self._game_tokens.pop(previous_key, None)
            self._game_tokens[replacement_key] = token
            self._token_to_game[token] = replacement
            # Transport membership deliberately remains the previous owner/guest
            # set. Lobby projection still comes from replacement.participants, so
            # absent guests are not advertised in +mgm/GJOI until they rejoin.
            self._participants[token] = previous_order
            self._mw_handoff_candidates[token] = guest_candidates
            self._mw_handoff_rebind_order[token] = []
            self._mw_handoff_endpoint_hints[token] = endpoint_hints
            log.info(
                "EA race UDP handed off MW graph: token=%d old_game=%d "
                "new_game=%d stable_virtuals=%d preserved_endpoints=%d "
                "preserved_directed=%d awaiting_guests=%d",
                token,
                previous.game_id,
                replacement.game_id,
                len(advertised),
                preserved_endpoint_count,
                preserved_directed_count,
                len(guest_candidates),
            )
            return advertised

    def _purge_identity_transport(
        self,
        identity: Identity,
        *,
        keep_endpoint: Address | None = None,
        remove_virtual: bool = False,
    ) -> None:
        """Remove stale socket/edge state for one relay identity.

        ``keep_endpoint`` is used when a replacement socket reuses the exact
        old sockaddr. Virtual identity and aliases normally survive a handoff;
        they are removed only when a participant is absent at race start.
        """

        primary = self._identity_to_endpoint.get(identity)
        if primary != keep_endpoint:
            self._identity_to_endpoint.pop(identity, None)
        for endpoint, mapped in tuple(self._endpoint_to_identity.items()):
            if mapped == identity and endpoint != keep_endpoint:
                self._endpoint_to_identity.pop(endpoint, None)

        self._pending.pop(identity, None)
        for target, pending in tuple(self._pending.items()):
            retained = [item for item in pending if item[0] != identity]
            if retained:
                self._pending[target] = retained
            else:
                self._pending.pop(target, None)

        for edge in tuple(self._directed_endpoints):
            if identity in edge:
                self._directed_endpoints.pop(edge, None)
        for edge in tuple(self._directed_pending):
            if identity in edge:
                self._directed_pending.pop(edge, None)

        self._mw_bootstrap_tokens.pop(identity, None)
        self._mw_owner_bootstrap_tokens.pop(identity, None)
        self._u2_shared_port_identities.discard(identity)

        if remove_virtual:
            virtual = self._identity_to_virtual.pop(identity, None)
            if (
                virtual is not None
                and self._virtual_to_identity.get(virtual) == identity
            ):
                self._virtual_to_identity.pop(virtual, None)
            alias = self._identity_to_wire_alias.pop(identity, None)
            if alias is not None:
                aliases = self._wire_alias_to_identities.get(alias, [])
                aliases = [candidate for candidate in aliases if candidate != identity]
                if aliases:
                    self._wire_alias_to_identities[alias] = aliases
                else:
                    self._wire_alias_to_identities.pop(alias, None)

    def _finish_mw_handoff(self, token: int) -> None:
        """Collapse dual-generation transport state to the replacement room."""

        game = self._token_to_game.get(token)
        current = (
            tuple((token, int(user_id)) for user_id in game.ordered_participants())
            if game is not None
            else ()
        )
        current_set = set(current)
        for identity in self._participants.get(token, ()):
            if identity not in current_set:
                self._purge_identity_transport(identity, remove_virtual=True)
        self._participants[token] = current
        self._mw_handoff_candidates.pop(token, None)
        self._mw_handoff_rebind_order.pop(token, None)
        self._mw_handoff_endpoint_hints.pop(token, None)
        log.info(
            "EA race UDP completed MW post-race handoff: token=%d game=%d "
            "participants=%d",
            token,
            game.game_id if game is not None else 0,
            len(current),
        )

    def _drop_mw_handoff_identity(
        self,
        token: int,
        identity: Identity,
    ) -> None:
        """Retire an old guest which did not join the replacement race."""

        self._purge_identity_transport(identity, remove_virtual=True)
        candidates = tuple(
            candidate
            for candidate in self._mw_handoff_candidates.get(token, ())
            if candidate != identity
        )
        if candidates:
            self._mw_handoff_candidates[token] = candidates
        else:
            self._mw_handoff_candidates.pop(token, None)
        queue = self._mw_handoff_rebind_order.get(token, [])
        self._mw_handoff_rebind_order[token] = [
            candidate for candidate in queue if candidate != identity
        ]
        hints = self._mw_handoff_endpoint_hints.get(token)
        if hints is not None:
            hints.pop(identity, None)
        self._participants[token] = tuple(
            candidate
            for candidate in self._participants.get(token, ())
            if candidate != identity
        )
        game = self._token_to_game.get(token)
        log.info(
            "EA race UDP dropped absent MW handoff guest: token=%d game=%d "
            "user=%d remaining=%d",
            token,
            game.game_id if game is not None else 0,
            identity[1],
            len(candidates),
        )

    def _confirm_mw_handoff_guest(
        self,
        identity: Identity,
        source: Address,
        payload: bytes,
    ) -> bool:
        """Replace one old guest edge after GJOI + fresh command 1."""

        token = identity[0]
        candidates = self._mw_handoff_candidates.get(token, ())
        queue = self._mw_handoff_rebind_order.get(token, [])
        if (
            identity not in candidates
            or identity not in queue
            or self._bootstrap_command(payload) != 1
        ):
            return False

        hint = self._mw_handoff_endpoint_hints.get(token, {}).get(identity)
        if hint == source:
            reason = "reused-endpoint"
        elif hint is not None and hint[0] == source[0]:
            reason = "new-port-same-ip"
        else:
            reason = "gjoi-order"

        owner = self._owner(token)
        # Retail keeps the owner's UDP spokes open while returning to the room.
        # Preserve the host side of this guest's old spoke, then replace only
        # the guest side.  The fresh command 1 can therefore reach the owner
        # immediately and provoke command 5 on that same host socket.  Waiting
        # for a brand-new owner socket deadlocks the live client: no command 5
        # is emitted until command 1 has first reached CommUDP.
        owner_spoke = (
            self._directed_endpoints.get((owner, identity))
            if owner is not None
            else None
        )
        self._purge_identity_transport(identity, keep_endpoint=source)
        if owner is not None and owner_spoke is not None:
            self._directed_endpoints[(owner, identity)] = owner_spoke
        remaining = tuple(candidate for candidate in candidates if candidate != identity)
        self._mw_handoff_rebind_order[token] = [
            candidate for candidate in queue if candidate != identity
        ]
        hints = self._mw_handoff_endpoint_hints.get(token)
        if hints is not None:
            hints.pop(identity, None)
        if remaining:
            self._mw_handoff_candidates[token] = remaining
        else:
            self._mw_handoff_candidates.pop(token, None)

        game = self._token_to_game.get(token)
        log.info(
            "EA race UDP rebound MW post-race guest: token=%d game=%d user=%d "
            "peer=%s:%d reason=%s remaining=%d",
            token,
            game.game_id if game is not None else 0,
            identity[1],
            source[0],
            source[1],
            reason,
            len(remaining),
        )
        if not remaining:
            self._finish_mw_handoff(token)
        return True

    def unregister_game(self, game: GameSession) -> bool:
        """Remove every route and queued packet owned by ``game``."""

        object_key = id(game)
        with self._lock:
            token = self._game_tokens.pop(object_key, None)
            if token is None:
                return False

            identities = set(self._participants.pop(token, ()))
            identities.update(
                identity
                for identity in self._identity_to_virtual
                if identity[0] == token
            )
            identities.update(self._mw_handoff_candidates.get(token, ()))
            for identity in identities:
                virtual = self._identity_to_virtual.pop(identity, None)
                if (
                    virtual is not None
                    and self._virtual_to_identity.get(virtual) == identity
                ):
                    self._virtual_to_identity.pop(virtual, None)

                alias = self._identity_to_wire_alias.pop(identity, None)
                if alias is not None:
                    aliases = self._wire_alias_to_identities.get(alias)
                    if aliases is not None:
                        self._wire_alias_to_identities[alias] = [
                            candidate
                            for candidate in aliases
                            if candidate != identity
                        ]
                        if not self._wire_alias_to_identities[alias]:
                            self._wire_alias_to_identities.pop(alias, None)

                endpoint = self._identity_to_endpoint.pop(identity, None)
                if (
                    endpoint is not None
                    and self._endpoint_to_identity.get(endpoint) == identity
                ):
                    self._endpoint_to_identity.pop(endpoint, None)
                self._pending.pop(identity, None)
                self._mw_bootstrap_tokens.pop(identity, None)
                self._mw_owner_bootstrap_tokens.pop(identity, None)

            # Pending records are keyed by their target, but also retain the
            # source identity.  Remove either side so no packet from a closed
            # game can be delivered after an endpoint is reused.
            for target, pending in tuple(self._pending.items()):
                retained = [
                    item for item in pending if item[0][0] != token
                ]
                if retained:
                    self._pending[target] = retained
                else:
                    self._pending.pop(target, None)

            for edge in tuple(self._directed_endpoints):
                if edge[0][0] == token or edge[1][0] == token:
                    self._directed_endpoints.pop(edge, None)
            for edge in tuple(self._directed_pending):
                if edge[0][0] == token or edge[1][0] == token:
                    self._directed_pending.pop(edge, None)

            self._token_to_game.pop(token, None)
            self._public_tokens.discard(token)
            self._channelized_tokens.discard(token)
            self._mw_channelized_tokens.discard(token)
            self._mw_shared_port_tokens.discard(token)
            self._u2_shared_port_identities = {
                identity
                for identity in self._u2_shared_port_identities
                if identity[0] != token
            }
            self._mw_owner_fallback_tokens.pop(token, None)
            self._mw_handoff_candidates.pop(token, None)
            self._mw_handoff_rebind_order.pop(token, None)
            self._mw_handoff_endpoint_hints.pop(token, None)
            return True

    @staticmethod
    def _decode(data: bytes) -> tuple[str, int, bytes, str | None] | None:
        if len(data) <= 6:
            return None
        port = struct.unpack("!H", data[:2])[0]
        try:
            host = socket.inet_ntoa(data[2:6])
        except OSError:
            return None
        if not port or host == "0.0.0.0":
            return None
        if len(data) > 14 and data[6:10] == U2_IDENTITY_MARKER:
            try:
                source_host = socket.inet_ntoa(data[10:14])
            except OSError:
                return None
            if source_host != "0.0.0.0":
                return host, port, data[14:], source_host
        return host, port, data[6:], None

    @staticmethod
    def _wrap(source_host: str, source_port: int, payload: bytes) -> bytes:
        return (
            struct.pack("!H", int(source_port))
            + socket.inet_aton(source_host)
            + bytes(payload)
        )

    @staticmethod
    def _bootstrap_command(payload: bytes) -> int | None:
        """Return the stock endpoint-discovery command, if this is one."""

        if len(payload) < 4:
            return None
        command = struct.unpack_from("<I", payload)[0]
        return command if command in {1, 5} else None

    def _demangle_mw_control_payload(
        self,
        token: int,
        payload: bytes,
        sender: Identity,
        recipient: Identity,
    ) -> bytes:
        """Present each MW spoke with the token stored in its local UDP state."""

        if len(payload) != 8:
            return bytes(payload)
        command, current_token = struct.unpack("<II", payload)
        owner_identity = self._owner(token)
        translated_token: int | None = None
        if (
            recipient == owner_identity
            and sender != owner_identity
            and command == 1
        ):
            translated_token = self._mw_owner_bootstrap_tokens.get(sender)
            if (
                translated_token is None
                and token not in self._mw_shared_port_tokens
            ):
                translated_token = self._mw_owner_fallback_tokens.get(token)
        elif (
            recipient != owner_identity
            and command in {2, 5}
            and recipient in self._mw_bootstrap_tokens
        ):
            translated_token = self._mw_bootstrap_tokens[recipient]
        if translated_token is None or current_token == translated_token:
            return bytes(payload)
        return struct.pack("<II", command, translated_token)

    def _owner(self, token: int) -> Identity | None:
        game = self._token_to_game.get(token)
        if game is None:
            return None
        identity = (token, int(game.owner_id))
        return identity if identity in self._participants.get(token, ()) else None

    def _public_route(
        self,
        source: Address,
        command: int | None,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        """Resolve public U2 traffic without fabricating a participant.

        Bootstrap commands 1/5 can identify a participant before its observed
        UDP endpoint is known.  Every later packet targets the same public
        relay address, so it must be routed from the endpoint binding learned
        during that bootstrap instead of being rejected merely because its
        first word is not another bootstrap command.
        """

        if command is None:
            source_identity = self._endpoint_to_identity.get(source)
            if source_identity is None:
                return None
            token = source_identity[0]
            if token not in self._public_tokens:
                return None
            participants = self._participants.get(token, ())
            if source_identity not in participants:
                return None
            targets = tuple(
                identity
                for identity in participants
                if identity != source_identity
            )
            return (source_identity, targets) if targets else None

        tokens = sorted(self._participants, reverse=True)
        if command == 5:
            # The owner emits command 5. Prefer the newest registered game so a
            # reused UDP socket naturally moves away from a completed session.
            for token in tokens:
                owner = self._owner(token)
                if owner is None:
                    continue
                peers = tuple(
                    identity
                    for identity in self._participants[token]
                    if identity != owner
                )
                if peers:
                    return owner, peers
            return None

        # A non-owner emits command 1 after the owner's endpoint is visible.
        for token in tokens:
            owner = self._owner(token)
            if owner is None:
                continue
            peers = [
                identity
                for identity in self._participants[token]
                if identity != owner
            ]
            # Some U2 builds emit command 1 from both clients instead of the
            # usual owner-command-5/guest-command-1 pair.  The first public
            # probe still identifies one of exactly two participant slots;
            # bind it provisionally to the owner so the next command-1 can
            # bind the only remaining peer and flush both queued probes.
            if owner not in self._identity_to_endpoint:
                if peers:
                    log.info(
                        "EA race UDP inferred U2 first command-1 endpoint: "
                        "game=%d owner=%d peer=%s:%d",
                        token,
                        owner[1],
                        source[0],
                        source[1],
                    )
                    return owner, tuple(peers)
                continue
            existing = self._endpoint_to_identity.get(source)
            if existing in peers:
                return existing, (owner,)
            unbound = [
                identity
                for identity in peers
                if identity not in self._identity_to_endpoint
            ]
            if unbound:
                return unbound[0], (owner,)
        return None

    def _virtual_route(
        self,
        source: Address,
        target: Identity,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        token = target[0]
        participants = self._participants.get(token, ())
        if len(participants) == 2:
            peer = next(
                (identity for identity in participants if identity != target),
                None,
            )
            if peer is not None:
                return peer, (target,)
        existing = self._endpoint_to_identity.get(source)
        if existing in participants and existing != target:
            return existing, (target,)

        candidates = [
            identity
            for identity in participants
            if identity != target
            and identity not in self._identity_to_endpoint
        ]
        if len(candidates) == 1:
            return candidates[0], (target,)

        all_peers = [identity for identity in participants if identity != target]
        if len(all_peers) == 1:
            return all_peers[0], (target,)
        return None

    def _wire_alias_route(
        self,
        source: Address,
        targets: list[Identity],
        payload: bytes,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        command = self._bootstrap_command(payload)
        for target in sorted(targets, reverse=True):
            token = target[0]
            participants = self._participants.get(token, ())
            owner = self._owner(token)
            if owner is None or target not in participants:
                continue
            if command == 5:
                peers = tuple(
                    identity for identity in participants if identity != owner
                )
                if peers:
                    return owner, peers
            if command == 1:
                peers = [
                    identity for identity in participants if identity != owner
                ]
                existing = self._endpoint_to_identity.get(source)
                source_identity = (
                    existing
                    if existing in peers
                    else (peers[0] if len(peers) == 1 else None)
                )
                if source_identity is not None:
                    return source_identity, (owner,)
            route = self._virtual_route(source, target)
            if route is not None:
                return route
        return None

    def _bind(self, identity: Identity, endpoint: Address) -> None:
        # Two local MW instances may expose the same source sockaddr.  Target
        # aliases still identify the sender in a two-player game, so retain an
        # endpoint for both identities instead of evicting the first binding.
        previous = self._identity_to_endpoint.get(identity)
        if (
            previous is not None
            and previous != endpoint
            and self._endpoint_to_identity.get(previous) == identity
        ):
            self._endpoint_to_identity.pop(previous, None)
        self._endpoint_to_identity[endpoint] = identity
        self._identity_to_endpoint[identity] = endpoint

    def _channel_route(
        self,
        source: Address,
        channel: int,
        target: Identity,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        """Route a wrapped U2/MW packet through its viewer-specific port.

        The listener identifies the sender even when several clients share an
        IP or source sockaddr.  Some early MW control packets name the
        sender's own virtual address; in a two-player session those are
        intended for the only peer and are corrected here.  U2 uses the same
        channel identity with one port per participant.
        """

        token = target[0]
        participants = self._participants.get(token, ())
        if token not in self._channelized_tokens:
            return None
        if channel < 0 or channel >= len(participants):
            return None

        channel_identity = participants[channel]
        channel_endpoint = self._identity_to_endpoint.get(channel_identity)
        endpoint_identity = self._endpoint_to_identity.get(source)
        mw_star = bool(
            token in self._mw_channelized_tokens
            and len(participants) > 2
        )
        owner = self._owner(token) if mw_star else None
        # A client can briefly send through the other viewer's relay port when
        # a room is reused.  Once both endpoints are known, the source sockaddr
        # is stronger evidence than that stale channel and must not be allowed
        # to move the other participant's binding back and forth.  Preserve the
        # channel distinction when both local clients genuinely share the same
        # sockaddr: in that case channel_identity is already bound to source.
        if (
            endpoint_identity in participants
            and self._identity_to_endpoint.get(endpoint_identity) == source
        ):
            source_identity = endpoint_identity
        else:
            source_identity = channel_identity
        if source_identity != channel_identity:
            log.info(
                "EA race UDP corrected stale channel endpoint: game=%d "
                "channel=%d channel_user=%d endpoint_user=%d peer=%s:%d",
                token,
                channel,
                channel_identity[1],
                source_identity[1],
                source[0],
                source[1],
            )
        if mw_star:
            if owner is None:
                return None
            # Retail MW builds a separate host/guest UDP spoke for every
            # participant.  Guests do not exchange bootstrap or transport
            # packets with one another.  Respect the wrapper destination for
            # owner packets so one guest's connection state cannot leak into
            # another guest's slot.  Native packets have no destination in
            # their payload; retain owner fan-out only as that raw fallback.
            if source_identity != owner:
                return source_identity, (owner,)
            if target != owner:
                return source_identity, (target,)
            peers = tuple(
                identity for identity in participants if identity != owner
            )
            return (source_identity, peers) if peers else None
        if target != source_identity:
            return source_identity, (target,)

        peers = tuple(
            identity for identity in participants if identity != source_identity
        )
        if peers:
            log.info(
                "EA race UDP corrected channel self-target: game=%d "
                "channel=%d user=%d peer=%s:%d",
                token,
                channel,
                source_identity[1],
                source[0],
                source[1],
            )
            return source_identity, peers
        return None

    def _claim_mw_handoff_guest(
        self,
        token: int,
        participants: tuple[Identity, ...],
        source: Address,
        payload: bytes,
    ) -> Identity | None:
        """Identify an unbound post-race guest on a newly opened UDP socket."""

        candidates = self._mw_handoff_candidates.get(token, ())
        if not candidates:
            return None
        if self._bootstrap_command(payload) != 1:
            return None
        game = self._token_to_game.get(token)
        current = (
            {
                (token, int(user_id))
                for user_id in game.ordered_participants()
            }
            if game is not None
            else set(participants)
        )
        pending = set(candidates)
        queue = [
            identity
            for identity in self._mw_handoff_rebind_order.get(token, [])
            if identity in current and identity in pending
        ]
        self._mw_handoff_rebind_order[token] = queue
        if not queue:
            return None

        hints = self._mw_handoff_endpoint_hints.get(token, {})
        exact = [identity for identity in queue if hints.get(identity) == source]
        if len(exact) == 1:
            chosen = exact[0]
        else:
            same_ip = [
                identity
                for identity in queue
                if (hint := hints.get(identity)) is not None
                and hint[0] == source[0]
            ]
            if len(same_ip) == 1:
                chosen = same_ip[0]
            else:
                # Multiple local/Wine clients commonly share one IP and even the
                # same guest bootstrap token. GJOI order is the only stable
                # discriminator left, and register_game captured it above.
                chosen = queue[0]
        return chosen

    def _shared_mw_route(
        self,
        source: Address,
        target: Identity,
        payload: bytes,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        """Resolve wrapped MW traffic on one public port without touching game state."""
        token = target[0]
        participants = self._participants.get(token, ())
        if token not in self._mw_shared_port_tokens or target not in participants:
            return None
        owner = self._owner(token)
        if owner is None:
            return None
        endpoint_identity = self._endpoint_to_identity.get(source)
        if len(participants) <= 2:
            if target == owner and self._mw_handoff_candidates.get(token):
                if endpoint_identity in participants and endpoint_identity != owner:
                    return endpoint_identity, (owner,)
                handoff_guest = self._claim_mw_handoff_guest(
                    token, participants, source, payload
                )
                if handoff_guest is not None:
                    return handoff_guest, (owner,)
                # Do not let an arbitrary post-handoff packet steal the only
                # guest slot before that client has completed GJOI + command 1.
                return None
            return self._virtual_route(source, target)
        if target != owner:
            if endpoint_identity in participants and endpoint_identity != owner:
                return None
            return owner, (target,)
        if endpoint_identity in participants:
            if endpoint_identity == owner:
                return None
            return endpoint_identity, (owner,)
        handoff_guest = self._claim_mw_handoff_guest(
            token, participants, source, payload
        )
        if handoff_guest is not None:
            return handoff_guest, (owner,)
        unbound_guests = [
            identity for identity in participants
            if identity != owner and identity not in self._identity_to_endpoint
        ]
        if len(unbound_guests) == 1:
            return unbound_guests[0], (owner,)
        if len(unbound_guests) > 1 and self._bootstrap_command(payload) == 1:
            # Several clients can finish GJOI within the same scheduler tick
            # and share both their public IP and bootstrap token.  The newest
            # room member is the one which normally emits the first command-1
            # probe; claim it first, then the remaining probe becomes unique.
            # Non-bootstrap traffic is never allowed to guess an identity.
            chosen = unbound_guests[-1]
            log.info(
                "EA race UDP claimed simultaneous MW guest bootstrap: "
                "game=%d user=%d candidates=%s peer=%s:%d",
                token,
                chosen[1],
                ",".join(str(identity[1]) for identity in unbound_guests),
                source[0],
                source[1],
            )
            return chosen, (owner,)
        return None

    def _channelized_route(
        self,
        source: Address,
        channel: int,
        target: Identity,
        payload: bytes,
    ) -> tuple[Identity, tuple[Identity, ...]] | None:
        # Shared-port MW derives the sender from the source endpoint and the
        # wrapped virtual destination. Dedicated listener channels remain
        # available for U2 and compatibility tests.
        if target[0] in self._mw_shared_port_tokens:
            return self._shared_mw_route(source, target, payload)
        return self._channel_route(source, channel, target)

    def _reply_channel(self, identity: Identity) -> int:
        token = identity[0]
        if (
            token in self._mw_shared_port_tokens
            or identity in self._u2_shared_port_identities
        ):
            return 0
        if token not in self._channelized_tokens:
            return 0
        participants = self._participants.get(token, ())
        try:
            return participants.index(identity)
        except ValueError:
            return 0

    def _channel_target_port_is_valid(
        self,
        channel: int,
        target: Identity,
        target_port: int,
    ) -> bool:
        """Accept the native game port or an exact viewer self-target port."""

        if target_port == MW_GAME_PORT:
            return True
        participants = self._participants.get(target[0], ())
        return (
            0 <= channel < len(participants)
            and target == participants[channel]
        )

    def handle_channel(
        self,
        data: bytes,
        source: Address,
        channel: int,
    ) -> tuple[ChannelReply, ...]:
        """Handle traffic received on a specific relay listener port."""

        wire = bytes(data)
        decoded = self._decode(wire)
        with self._lock:
            wrapped_target_known = bool(
                decoded is not None
                and (
                    decoded[0] in self._virtual_to_identity
                    or decoded[0] in self._wire_alias_to_identities
                    or (
                        bool(self._public_host)
                        and decoded[0] == self._public_host
                    )
                )
            )

        if not wrapped_target_known:
            return self._handle_raw_channel(wire, source, int(channel))

        return tuple(
            (response, target, self._reply_channel(target_identity))
            for response, target, target_identity in self._handle_wrapped(
                wire,
                source,
                source_channel=int(channel),
            )
        )

    def _raw_channel_target(
        self,
        source: Address,
        channel: int,
    ) -> Identity | None:
        """Resolve a native MW datagram using its host/guest listener port."""

        candidates: list[tuple[Identity, Identity]] = []
        for token in sorted(self._channelized_tokens, reverse=True):
            participants = self._participants.get(token, ())
            if len(participants) < 2 or not 0 <= channel < len(participants):
                continue
            source_identity = participants[channel]
            # With two players the listener identifies both the sender and
            # its only target, preserving the early loopback inference used
            # by two local Wine instances. In a larger MW room a native
            # packet has no encoded destination. A guest's self-target is
            # resolved to the owner spoke; an owner self-target is the raw
            # compatibility fallback and fans out to all guests.
            target_identity = (
                next(
                    identity
                    for identity in participants
                    if identity != source_identity
                )
                if len(participants) == 2
                else source_identity
            )
            if self._identity_to_endpoint.get(source_identity) == source:
                return target_identity
            if self._endpoint_to_identity.get(source) == source_identity:
                return target_identity
            candidates.append((source_identity, target_identity))

        # A listener channel identifies the sender before its first endpoint
        # has been learned. Prefer the newest game whose channel identity is
        # still unbound; completed games are removed by unregister_game.
        for source_identity, target_identity in candidates:
            if source_identity not in self._identity_to_endpoint:
                return target_identity
        return candidates[0][1] if candidates else None

    def _handle_raw_channel(
        self,
        payload: bytes,
        source: Address,
        channel: int,
    ) -> tuple[ChannelReply, ...]:
        """Relay native MW UDP when the endpoint hook performs no wrapping.

        The viewer-specific relay listener already identifies host versus
        guest. Synthesize an internal wrapper so route binding, pending queues,
        bootstrap-token normalization, and reply-channel selection remain
        shared with wrapped clients, then remove that wrapper before delivery.
        """

        with self._lock:
            target_identity = self._raw_channel_target(source, channel)
            target_host = (
                self._identity_to_virtual.get(target_identity)
                if target_identity is not None
                else None
            )
            target_participants = (
                self._participants.get(target_identity[0], ())
                if target_identity is not None
                else ()
            )
            mw_multi_player_source = bool(
                target_identity is not None
                and target_identity[0] in self._mw_channelized_tokens
                and len(target_participants) > 2
            )
            if mw_multi_player_source:
                # _raw_channel_target returns the sender itself for native MW
                # rooms with 3+ players. Bind it before _channel_route so the
                # shared guest channel cannot replace it with participants[1].
                self._bind(target_identity, source)
            # Two stock MW instances on the same machine both use the native
            # game port, while the endpoint redirector connects the host to
            # relay port 20000 and the guest to 20001.  The first guest probe
            # therefore arrives before the host has emitted anything, but it
            # can still be delivered to the same loopback sockaddr through
            # the host listener.  The listener's source port selects the
            # correct connected Wine socket.  Keep this inference strictly
            # local; a remote peer's port does not reveal its address.
            target_channel = (
                self._reply_channel(target_identity)
                if target_identity is not None
                else 0
            )
            inferred_endpoint = (
                f"127.0.0.{2 + target_channel}",
                MW_GAME_PORT,
            )
            inferred_loopback_peer = bool(
                target_identity is not None
                and not mw_multi_player_source
                and target_identity not in self._identity_to_endpoint
                and source[1] == MW_GAME_PORT
                and ipaddress.ip_address(source[0]).is_loopback
                and len(payload) == 8
                and self._bootstrap_command(payload) in {1, 5}
            )
            if inferred_loopback_peer:
                self._identity_to_endpoint[target_identity] = inferred_endpoint
        if target_identity is None or target_host is None:
            first_word = (
                struct.unpack_from("<I", payload)[0]
                if len(payload) >= 4
                else 0
            )
            log.info(
                "EA race UDP unresolved raw channel: channel=%d peer=%s:%d "
                "w0=0x%08x payload=%d",
                channel,
                source[0],
                source[1],
                first_word,
                len(payload),
            )
            return ()

        if inferred_loopback_peer:
            log.info(
                "EA race UDP inferred loopback peer endpoint: game=%d "
                "channel=%d target_user=%d endpoint=%s:%d reply_channel=%d",
                target_identity[0],
                channel,
                target_identity[1],
                inferred_endpoint[0],
                inferred_endpoint[1],
                target_channel,
            )

        internal = self._wrap(target_host, MW_GAME_PORT, payload)
        routed = self._handle_wrapped(
            internal,
            source,
            source_channel=channel,
        )
        replies = tuple(
            (
                response[6:] if len(response) >= 6 else response,
                target,
                self._reply_channel(target_identity),
            )
            for response, target, target_identity in routed
        )
        first_word = (
            struct.unpack_from("<I", payload)[0]
            if len(payload) >= 4
            else 0
        )
        log.info(
            "EA race UDP raw channel handled: channel=%d peer=%s:%d "
            "game=%d target_user=%d w0=0x%08x payload=%d relayed=%d",
            channel,
            source[0],
            source[1],
            target_identity[0],
            target_identity[1],
            first_word,
            len(payload),
            len(replies),
        )
        return replies

    def handle(self, data: bytes, source: Address):
        wire = bytes(data)
        return tuple(
            (response, target)
            for response, target, _target_identity in self._handle_wrapped(
                wire,
                source,
            )
        )

    def _resolve_wrapped_route(
        self,
        source: Address,
        source_channel: int | None,
        target_host: str,
        target_port: int,
        payload: bytes,
        source_hint: str | None,
    ) -> Route | None:
        target_identity = self._virtual_to_identity.get(target_host)
        if target_identity is not None:
            if (
                source_hint is not None
                and target_identity[0] in self._channelized_tokens
                and target_identity[0] not in self._mw_channelized_tokens
            ):
                source_identity = self._virtual_to_identity.get(source_hint)
                participants = self._participants.get(target_identity[0], ())
                resolved_target = target_identity
                if source_identity == target_identity:
                    # Live stock U2 uses the viewer-local ADDR0 as the peer of
                    # its initial guest command-1 socket.  The bootstrap is
                    # nevertheless addressed logically to the game owner; the
                    # owner replies with command 5 using the guest's virtual
                    # address.  Restrict this correction to that exact packet
                    # so ordinary self-target traffic remains invalid.
                    owner = self._owner(target_identity[0])
                    if (
                        self._bootstrap_command(payload) == 1
                        and owner is not None
                        and owner != source_identity
                    ):
                        resolved_target = owner
                if (
                    source_identity in participants
                    and source_identity is not None
                    and source_identity != resolved_target
                    and source_identity[0] == resolved_target[0]
                    and self._channel_target_port_is_valid(
                        0,
                        resolved_target,
                        target_port,
                    )
                ):
                    self._u2_shared_port_identities.add(source_identity)
                    return source_identity, (resolved_target,)
                # A packet which explicitly selected the U2I1 format must not
                # fall back to listener-channel inference. Otherwise a forged
                # or stale source identity received on channel zero would be
                # reinterpreted as the owner.
                return None
            if (
                source_channel is not None
                and target_identity[0] in self._channelized_tokens
            ):
                if not self._channel_target_port_is_valid(
                    source_channel,
                    target_identity,
                    target_port,
                ):
                    return None
                return self._channelized_route(
                    source,
                    source_channel,
                    target_identity,
                    payload,
                )
            return self._virtual_route(source, target_identity)

        if target_host in self._wire_alias_to_identities:
            alias_targets = self._wire_alias_to_identities[target_host]
            channel_targets = (
                [
                    identity
                    for identity in reversed(alias_targets)
                    if identity[0] in self._channelized_tokens
                ]
                if source_channel is not None
                else []
            )
            if channel_targets:
                return next(
                    (
                        candidate
                        for target in channel_targets
                        if self._channel_target_port_is_valid(
                            source_channel,
                            target,
                            target_port,
                        )
                        and (
                            candidate := self._channelized_route(
                                source,
                                source_channel,
                                target,
                                payload,
                            )
                        ) is not None
                    ),
                    None,
                )
            return self._wire_alias_route(source, alias_targets, payload)

        if self._public_host and target_host == self._public_host:
            return self._public_route(source, self._bootstrap_command(payload))
        return None

    def _source_reply_address(
        self,
        source_identity: Identity,
        source: Address,
        channelized_reply: bool,
    ) -> Address:
        token = source_identity[0]
        source_virtual = (
            self._public_host
            if token in self._public_tokens and self._public_host
            else (
                self._identity_to_virtual[source_identity]
                if channelized_reply
                else self._identity_to_wire_alias.get(
                    source_identity,
                    self._identity_to_virtual[source_identity],
                )
            )
        )
        source_port = (
            MW_GAME_PORT
            if channelized_reply or token in self._public_tokens
            else source[1]
        )
        return source_virtual, source_port

    def _record_mw_bootstrap_tokens(
        self,
        payload: bytes,
        command: int | None,
        source_identity: Identity,
        targets: tuple[Identity, ...],
        owner_identity: Identity | None,
        mw_bootstrap_translation: bool,
    ) -> None:
        token = source_identity[0]
        if (
            mw_bootstrap_translation
            and len(payload) == 8
            and command == 1
            and owner_identity is not None
            and source_identity != owner_identity
        ):
            guest_token = struct.unpack_from("<I", payload, 4)[0]
            previous = self._mw_bootstrap_tokens.get(source_identity)
            if previous != guest_token:
                self._mw_bootstrap_tokens[source_identity] = guest_token
                log.info(
                    "EA race UDP learned MW guest bootstrap token: "
                    "game=%d guest=%d token=0x%08x",
                    token,
                    source_identity[1],
                    guest_token,
                )
        if (
            mw_bootstrap_translation
            and len(payload) == 8
            and command == 5
            and owner_identity is not None
            and source_identity == owner_identity
        ):
            owner_token = struct.unpack_from("<I", payload, 4)[0]
            if token not in self._mw_shared_port_tokens:
                self._mw_owner_fallback_tokens[token] = owner_token
            for guest_identity in targets:
                if guest_identity == owner_identity:
                    continue
                previous = self._mw_owner_bootstrap_tokens.get(guest_identity)
                if previous == owner_token:
                    continue
                self._mw_owner_bootstrap_tokens[guest_identity] = owner_token
                log.info(
                    "EA race UDP learned MW owner spoke token: "
                    "game=%d owner=%d guest=%d token=0x%08x",
                    token,
                    source_identity[1],
                    guest_identity[1],
                    owner_token,
                )

    def _relay_current_payload(
        self,
        source_identity: Identity,
        targets: tuple[Identity, ...],
        payload: bytes,
        source: Address,
        source_virtual: str,
        source_reply_port: int,
        mw_bootstrap_translation: bool,
        directed_routes: bool,
        command: int | None,
    ) -> tuple[list[tuple[bytes, Address, Identity]], int]:
        token = source_identity[0]
        replies: list[tuple[bytes, Address, Identity]] = []
        queued = 0
        for target in targets:
            if target == source_identity:
                continue
            target_endpoint = (
                self._directed_endpoints.get((target, source_identity))
                if directed_routes
                else self._identity_to_endpoint.get(target)
            )
            if target_endpoint is None:
                if directed_routes:
                    self._directed_pending.setdefault(
                        (target, source_identity),
                        [],
                    ).append((bytes(payload), source[1]))
                else:
                    self._pending.setdefault(target, []).append(
                        (source_identity, bytes(payload), source[1])
                    )
                queued += 1
                continue
            target_payload = (
                self._demangle_mw_control_payload(
                    token, payload, source_identity, target
                )
                if mw_bootstrap_translation
                else payload
            )
            if target_payload != payload:
                translated_command, translated_token = struct.unpack(
                    "<II",
                    target_payload,
                )
                log.info(
                    "EA race UDP demangled MW bootstrap token: "
                    "game=%d recipient=%d command=%d token=0x%08x",
                    token,
                    target[1],
                    translated_command,
                    translated_token,
                )
            if len(payload) == 8 and command in {1, 5}:
                log.info(
                    "EA race UDP bootstrap delivery: game=%d sender=%d "
                    "recipient=%d dst=%s:%d wire_source=%s:%d command=%d",
                    token,
                    source_identity[1],
                    target[1],
                    target_endpoint[0],
                    target_endpoint[1],
                    source_virtual,
                    source_reply_port,
                    command,
                )
            replies.append(
                (
                    self._wrap(source_virtual, source_reply_port, target_payload),
                    target_endpoint,
                    target,
                )
            )
        return replies, queued

    def _flush_pending_payloads(
        self,
        replies: list[tuple[bytes, Address, Identity]],
        source_identity: Identity,
        source: Address,
        channelized_reply: bool,
        mw_bootstrap_translation: bool,
    ) -> None:
        token = source_identity[0]
        for pending_source, pending_payload, pending_port in self._pending.pop(
            source_identity,
            [],
        ):
            pending_virtual = (
                self._public_host
                if token in self._public_tokens and self._public_host
                else (
                    self._identity_to_virtual.get(pending_source)
                    if channelized_reply
                    else self._identity_to_wire_alias.get(
                        pending_source,
                        self._identity_to_virtual.get(pending_source),
                    )
                )
            )
            if pending_virtual is None:
                continue
            pending_reply_port = (
                MW_GAME_PORT
                if channelized_reply or token in self._public_tokens
                else pending_port
            )
            pending_command = self._bootstrap_command(pending_payload)
            if len(pending_payload) == 8 and pending_command in {1, 5}:
                log.info(
                    "EA race UDP bootstrap delivery: game=%d sender=%d "
                    "recipient=%d dst=%s:%d wire_source=%s:%d command=%d pending=1",
                    pending_source[0],
                    pending_source[1],
                    source_identity[1],
                    source[0],
                    source[1],
                    pending_virtual,
                    pending_reply_port,
                    pending_command,
                )
            delivered = (
                self._demangle_mw_control_payload(
                    pending_source[0],
                    pending_payload,
                    pending_source,
                    source_identity,
                )
                if mw_bootstrap_translation
                else pending_payload
            )
            replies.append(
                (
                    self._wrap(pending_virtual, pending_reply_port, delivered),
                    source,
                    source_identity,
                )
            )

    def _bind_directed_routes(
        self,
        source_identity: Identity,
        targets: tuple[Identity, ...],
        source: Address,
    ) -> None:
        for target in targets:
            if target != source_identity:
                self._directed_endpoints[(source_identity, target)] = source

    def _flush_directed_pending_payloads(
        self,
        replies: list[tuple[bytes, Address, Identity]],
        source_identity: Identity,
        targets: tuple[Identity, ...],
        source: Address,
        mw_bootstrap_translation: bool,
    ) -> None:
        """Flush peer-specific packets to the newly observed U2 socket."""

        for peer_identity in targets:
            if peer_identity == source_identity:
                continue
            pending = self._directed_pending.pop(
                (source_identity, peer_identity),
                [],
            )
            if not pending:
                continue
            peer_virtual = self._identity_to_virtual.get(peer_identity)
            if peer_virtual is None:
                continue
            for pending_payload, _pending_port in pending:
                pending_command = self._bootstrap_command(pending_payload)
                if len(pending_payload) == 8 and pending_command in {1, 5}:
                    log.info(
                        "EA race UDP bootstrap delivery: game=%d sender=%d "
                        "recipient=%d dst=%s:%d wire_source=%s:%d command=%d "
                        "pending=1 directed=1",
                        peer_identity[0],
                        peer_identity[1],
                        source_identity[1],
                        source[0],
                        source[1],
                        peer_virtual,
                        MW_GAME_PORT,
                        pending_command,
                    )
                replies.append(
                    (
                        self._wrap(
                            peer_virtual,
                            MW_GAME_PORT,
                            (
                                self._demangle_mw_control_payload(
                                    peer_identity[0],
                                    pending_payload,
                                    peer_identity,
                                    source_identity,
                                )
                                if mw_bootstrap_translation
                                else pending_payload
                            ),
                        ),
                        source,
                        source_identity,
                    )
                )

    def _handle_wrapped(
        self,
        wire: bytes,
        source: Address,
        *,
        source_channel: int | None = None,
    ) -> tuple[tuple[bytes, Address, Identity], ...]:
        """Route one ASI-wrapped packet and retain recipient identities."""
        decoded = self._decode(wire)
        if decoded is None:
            log.info(
                "EA race UDP ignored invalid wrapper: peer=%s:%d len=%d",
                source[0],
                source[1],
                len(wire),
            )
            return ()

        target_host, target_port, payload, source_hint = decoded
        first_word = (
            struct.unpack_from("<I", payload)[0]
            if len(payload) >= 4
            else 0
        )
        with self._lock:
            route = self._resolve_wrapped_route(
                source,
                source_channel,
                target_host,
                target_port,
                payload,
                source_hint,
            )
            if route is None:
                log.info(
                    "EA race UDP unresolved route: peer=%s:%d "
                    "target=%s:%d source_hint=%s w0=0x%08x payload=%d",
                    source[0],
                    source[1],
                    target_host,
                    target_port,
                    source_hint or "-",
                    first_word,
                    len(payload),
                )
                return ()

            source_identity, targets = route
            self._confirm_mw_handoff_guest(
                source_identity,
                source,
                payload,
            )
            self._bind(source_identity, source)
            channelized_reply = (
                source_channel is not None
                and source_identity[0] in self._channelized_tokens
            )
            participants = self._participants.get(source_identity[0], ())
            mw_channelized = (
                source_identity[0] in self._mw_channelized_tokens
            )
            # CommUDP validates command 1 against the token stored in the
            # recipient's local connection record before it emits command 5.
            # The relay therefore has to translate every owner/guest spoke,
            # including late guests in rooms with three or more players.
            mw_bootstrap_translation = mw_channelized
            mw_shared_port = source_identity[0] in self._mw_shared_port_tokens
            directed_routes = (
                channelized_reply
                and len(participants) > 2
                and (not mw_channelized or mw_shared_port)
            )
            if directed_routes:
                self._bind_directed_routes(source_identity, targets, source)
            source_virtual, source_reply_port = self._source_reply_address(
                source_identity,
                source,
                channelized_reply,
            )
            command = self._bootstrap_command(payload)
            owner_identity = self._owner(source_identity[0])
            if mw_bootstrap_translation and len(payload) == 8:
                raw_command, raw_token = struct.unpack_from("<II", payload)
                if raw_command in {1, 2, 5}:
                    spoke_guest = (
                        source_identity
                        if owner_identity is not None and source_identity != owner_identity
                        else next(
                            (target for target in targets if target != owner_identity),
                            None,
                        )
                    )
                    stored_owner = (
                        self._mw_owner_bootstrap_tokens.get(spoke_guest)
                        if spoke_guest is not None
                        else None
                    )
                    if (
                        stored_owner is None
                        and source_identity[0] not in self._mw_shared_port_tokens
                    ):
                        stored_owner = self._mw_owner_fallback_tokens.get(
                            source_identity[0]
                        )
                    stored_guest = (
                        self._mw_bootstrap_tokens.get(spoke_guest)
                        if spoke_guest is not None
                        else None
                    )
                    log.info(
                        "MW CTRL TRACE IN: game=%d channel=%s user=%d "
                        "peer=%s:%d target=%s:%d command=%d token=0x%08x "
                        "stored_owner=%s stored_guest=%s",
                        source_identity[0],
                        source_channel if source_channel is not None else "-",
                        source_identity[1],
                        source[0],
                        source[1],
                        target_host,
                        target_port,
                        raw_command,
                        raw_token,
                        (f"0x{stored_owner:08x}" if stored_owner is not None else "-"),
                        (f"0x{stored_guest:08x}" if stored_guest is not None else "-"),
                    )
            self._record_mw_bootstrap_tokens(
                payload,
                command,
                source_identity,
                targets,
                owner_identity,
                mw_bootstrap_translation,
            )
            replies, queued = self._relay_current_payload(
                source_identity,
                targets,
                bytes(payload),
                source,
                source_virtual,
                source_reply_port,
                mw_bootstrap_translation,
                directed_routes,
                command,
            )
            if (
                mw_bootstrap_translation
                and len(payload) == 8
                and owner_identity is not None
                and source_identity == owner_identity
                and struct.unpack_from("<I", payload)[0] == 2
            ):
                game = self._token_to_game.get(source_identity[0])
                delivered_identities = {
                    target_identity
                    for _wire, _endpoint, target_identity in replies
                }
                if game is not None:
                    for target_identity in targets:
                        if (
                            target_identity != owner_identity
                            and target_identity in delivered_identities
                        ):
                            self._mw_settled_links.add(
                                (game.game_id, target_identity[1])
                            )
            if directed_routes:
                self._flush_directed_pending_payloads(
                    replies,
                    source_identity,
                    targets,
                    source,
                    mw_bootstrap_translation,
                )
            else:
                self._flush_pending_payloads(
                    replies,
                    source_identity,
                    source,
                    channelized_reply,
                    mw_bootstrap_translation,
                )
            if first_word == 0x98361027 and len(payload) == 86:
                log.info(
                    "MW86 TRACE IN: game=%d channel=%s user=%d peer=%s:%d "
                    "source_virtual=%s:%d target=%s:%d hex=%s",
                    source_identity[0],
                    source_channel if source_channel is not None else "-",
                    source_identity[1],
                    source[0],
                    source[1],
                    source_virtual[0],
                    source_reply_port,
                    target_host,
                    target_port,
                    bytes(payload).hex(),
                )
                for wire_payload, wire_endpoint, target_identity in replies:
                    log.info(
                        "MW86 TRACE OUT: game=%d sender=%d recipient=%d "
                        "dst=%s:%d wire_len=%d wire_hex=%s",
                        source_identity[0],
                        source_identity[1],
                        target_identity[1],
                        wire_endpoint[0],
                        wire_endpoint[1],
                        len(wire_payload),
                        bytes(wire_payload).hex(),
                    )

            log.info(
                "EA race UDP routed: game=%d channel=%s user=%d peer=%s:%d "
                "target=%s:%d command=%s w0=0x%08x payload=%d "
                "queued=%d relayed=%d directed=%d reply_channels=%s",
                source_identity[0],
                source_channel if source_channel is not None else "-",
                source_identity[1],
                source[0],
                source[1],
                target_host,
                target_port,
                command,
                first_word,
                len(payload),
                queued,
                len(replies),
                int(directed_routes),
                ",".join(
                    str(self._reply_channel(target_identity))
                    for _wire, _endpoint, target_identity in replies
                )
                or "-",
            )
            return tuple(replies)
