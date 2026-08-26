"""Classic U2/MW game-session command handlers.

This mixin owns game creation, join, leave/delete, kick, settings and start.
The methods are extracted intact from the historical pre-login service so the
wire protocol, callback ordering and race lifecycle remain unchanged.
"""

from __future__ import annotations

from hashlib import md5
import logging
import time
from typing import TYPE_CHECKING

from classic.ea.directory import GameSession, SessionState, Visibility
from classic.lobby.constants import (
    MW_GJOI_UNAVAILABLE_RESERVED,
    U2_READY_FLAG,
    U2_ROOMS,
)
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


# Keep existing log filters and operational dashboards stable.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicGameCommandMixin:
    """Handle Classic lobby game commands and race-start transitions."""

    def _dispatch_game_create(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        if identity is None or not context.auth.persona:
            return ClassicPreloginReply(
                (ClassicEAFrame.short("userbadc"),),
                "not_authenticated",
                True,
            )
        # Stock MW can create the next room immediately after the native UDP
        # command 3 transition, without first sending gdel/glea for the
        # completed race.  The owner replacement must inherit the established
        # CommUDP graph; otherwise every returning guest loses its endpoint as
        # soon as GCRE allocates the new lobby game.  A non-owner leaving early
        # must likewise not unregister the route still used by the owner.
        deferred_postrace_guests: list[ClassicPreloginContext] = []
        previous_game_for_handoff: GameSession | None = None
        previous_participant_order: tuple[int, ...] = ()
        if context.lobby_game_id:
            previous_game = self.sessions.get_game(context.lobby_game_id)
            if previous_game is not None:
                current_previous_order = previous_game.ordered_participants()
                previous_participants = set(current_previous_order)
                preserve_shared_race_transport = (
                    self._preserve_transport_for_guest_exit(
                        previous_game,
                        identity.user_id,
                    )
                )
                handoff_shared_race_transport = bool(
                    self._is_most_wanted
                    and previous_game.owner_id == identity.user_id
                    and previous_game.state
                    in {SessionState.ACTIVE, SessionState.FINISHED}
                    and len(previous_participants) > 1
                    and previous_game.participant_race_addresses
                    and self.race_handoff is not None
                )
                if handoff_shared_race_transport:
                    previous_game_for_handoff = previous_game
                    previous_participant_order = current_previous_order
                elif not preserve_shared_race_transport:
                    self._retire_game_transport(previous_game)
                if previous_game.owner_id == identity.user_id:
                    if self._is_most_wanted:
                        self._finalize_mw_missing_rank_reports(previous_game)
                    self.sessions.close_game(previous_game.game_id)
                    for user_id in current_previous_order:
                        peer = self._context_for_user(user_id)
                        if (
                            peer is not None
                            and peer.lobby_game_id == previous_game.game_id
                        ):
                            peer.lobby_game_id = 0
                            peer.mw_join_pending_game_id = 0
                            if (
                                user_id != identity.user_id
                                and (
                                    peer.mw_deferred_usea_game_id
                                    == previous_game.game_id
                                    or peer.mw_deferred_gjoi_game_id
                                    == previous_game.game_id
                                )
                            ):
                                deferred_postrace_guests.append(peer)
                            else:
                                peer.mw_postrace_return_pending = False
                                peer.mw_postrace_snapshot_game_id = 0
                                peer.mw_postrace_room_view_game_id = 0
                                peer.mw_deferred_usea_game_id = 0
                                peer.mw_deferred_gjoi_game_id = 0
                else:
                    if self._is_most_wanted:
                        self._mw_release_join_serial_slot(
                            previous_game.game_id,
                            identity.user_id,
                        )
                    detached = self.sessions.leave_game(
                        previous_game.game_id,
                        identity.user_id,
                    )
                    if detached and preserve_shared_race_transport:
                        self._sync_preserved_game_transport(previous_game)
                log.info(
                    "%s detached previous game before gcre: game=%d "
                    "actor=%d participants=%d transport_retired=%d "
                    "transport_handoff_pending=%d",
                    self.profile.game_id,
                    previous_game.game_id,
                    identity.user_id,
                    len(previous_participants),
                    0
                    if (
                        preserve_shared_race_transport
                        or handoff_shared_race_transport
                    )
                    else 1,
                    1 if handoff_shared_race_transport else 0,
                )
            context.lobby_game_id = 0
            context.mw_join_pending_game_id = 0
            context.mw_postrace_return_pending = False
            context.mw_postrace_snapshot_game_id = 0
            context.mw_postrace_room_view_game_id = 0
            context.mw_deferred_usea_game_id = 0
            context.mw_deferred_gjoi_game_id = 0
        if self._is_underground2:
            min_players, capacity = self._u2_game_sizes(fields)
        else:
            try:
                capacity = max(2, min(8, int(fields.get("MAXSIZE", "4") or 4)))
            except (TypeError, ValueError):
                capacity = 4
            min_players = 2
        name = fields.get("NAME", "").strip() or f"007.{context.auth.persona}"
        params = fields.get("PARAMS", "").strip()
        password = fields.get("PASS", "").strip()
        custflags = fields.get("CUSTFLAGS", "0")
        try:
            custflags_value = int(str(custflags or "0"), 0) & 0xFFFFFFFF
        except (TypeError, ValueError):
            custflags_value = 0
        private = bool(custflags_value & 0x100)
        room_id = 0
        if self._is_underground2:
            requested_room = str(fields.get("ROOM", "") or "").strip()
            room = self._u2_room(name=requested_room)
            if room is None and requested_room:
                room = self._u2_room(
                    room_id=self._lobby_int(requested_room, 0)
                )
            if room is None:
                room = self._u2_room(
                    room_id=context.u2_room_id,
                    name=context.u2_room_name,
                )
            if room is None:
                # The stock client flow used by the all-in-one build does
                # not issue MOVE before GCRE.  nfsuserver consequently has
                # no FromRoom either, and the client omits its post-race
                # RANK report when +ses keeps ROOM=0.  Treat that classic
                # roomless flow as the default ranked circuit room; RESU's
                # race_type remains authoritative for the category.
                room = U2_ROOMS[0]
                log.info(
                    "%s assigned roomless U2 game to default ranked room: "
                    "user=%d room=%s",
                    self.profile.game_id,
                    identity.user_id,
                    room[1],
                )
            if room is not None:
                context.u2_room_id, context.u2_room_name = room
                room_id = context.u2_room_id
            self._u2_pending_games.pop(identity.user_id, None)
            log.info(
                "%s U2 game size negotiated: user=%d policy=%s "
                "requested_min=%s requested_max=%s applied_min=%d applied_max=%d",
                self.profile.game_id,
                identity.user_id,
                self.profile.u2_game_size_policy,
                fields.get("MINSIZE", "-"),
                fields.get("MAXSIZE", "-"),
                min_players,
                capacity,
            )
        game = self.sessions.create_game(
            room_id,
            identity.user_id,
            capacity=capacity,
            min_players=min_players,
            visibility=(
                Visibility.PRIVATE if private else Visibility.PUBLIC
            ),
            password=password,
            name=name,
            params=params,
            custflags=custflags,
            sysflags=fields.get("SYSFLAGS", "0"),
            host_persona=context.auth.persona,
            host_address=context.client_address,
        )
        if previous_game_for_handoff is not None:
            # Seed the replacement with the old OPPO/channel order before the
            # staged GJOI requests. SessionDirectory filters absent users, while
            # the relay separately records the order in which guests return.
            game.participant_order = list(previous_participant_order)
            if not self._handoff_game_transport(
                previous_game_for_handoff,
                game,
            ):
                self._retire_game_transport(previous_game_for_handoff)
                log.warning(
                    "%s could not hand off MW race UDP transport; rebuilt "
                    "fresh graph: old_game=%d new_game=%d",
                    self.profile.game_id,
                    previous_game_for_handoff.game_id,
                    game.game_id,
                )
            else:
                with self._connections_lock:
                    self._mw_postrace_handoff_returners[game.game_id] = {
                        int(user_id)
                        for user_id in previous_participant_order
                        if int(user_id) != int(identity.user_id)
                    }
        context.lobby_game_id = game.game_id
        if self._is_most_wanted and context.userset_id:
            with self._usersets_lock:
                userset = self._usersets.get(context.userset_id)
                if (
                    userset is not None
                    and userset.owner_id == identity.user_id
                ):
                    userset.game_id = game.game_id
        aux_text = self._participant_aux.get(identity.user_id, "")
        if self._is_most_wanted:
            # GCRE allocates a fresh lobby game even when the established UDP
            # graph is handed over. Do not copy the previous race's client-owned
            # CE vector into the replacement room.
            aux_text = self._mw_auxiliary_for_new_game(aux_text)
            self._participant_aux[identity.user_id] = aux_text
            # Keep an explicit per-game value even when CE was the only record,
            # otherwise presence projection falls back to the stale global AUX.
            game.participant_aux[identity.user_id] = aux_text
        elif aux_text:
            game.participant_aux[identity.user_id] = aux_text
        if self._is_most_wanted:
            userset = (
                self._usersets.get(context.userset_id)
                if context.userset_id
                else None
            )
            if userset is not None:
                # Capture fullrace&room, post-race transition: the guest
                # remains in the host userset, receives +ust C=2 and the
                # host at G=<new game id>, then performs usea + gjoi
                # without another ujoi.
                bridge = (
                    self._mw_userset_frame("+ust", userset),
                    self._mw_usm_frame(
                        context,
                        game=game,
                        display_game_id=game.game_id,
                    ),
                )
                deferred_user_ids = {
                    peer.auth.identity.user_id
                    for peer in deferred_postrace_guests
                    if peer.auth.identity is not None
                }
                self._send_users(
                    set(userset.members or set()) - deferred_user_ids,
                    bridge,
                    exclude=identity.user_id,
                )
            game_fields = self._mw_game_fields(
                game,
                viewer_id=identity.user_id,
            )
            created = ClassicEAFrame.from_fields(
                "gcre",
                game_fields,
                separator="\t",
                final_separator=False,
            ).encode()
            def rearm_deferred_postrace_joins() -> None:
                for peer in deferred_postrace_guests:
                    peer_identity = peer.auth.identity
                    sender = peer.send_wire
                    if (
                        peer_identity is None
                        or sender is None
                    ):
                        continue
                    old_game_id = (
                        peer.mw_deferred_gjoi_game_id
                        or peer.mw_deferred_usea_game_id
                    )
                    peer.lobby_game_id = 0
                    peer.mw_join_pending_game_id = 0
                    peer.mw_postrace_return_pending = False
                    peer.mw_postrace_snapshot_game_id = 0
                    peer.mw_postrace_room_view_game_id = 0
                    peer.mw_deferred_usea_game_id = 0
                    peer.mw_deferred_gjoi_game_id = 0
                    # Stock does not auto-join a guest whose early gjoi
                    # received `ugam`.  The owner's G=<new id> presence,
                    # followed by the updated userset, makes the client
                    # issue gjoi again against the replacement game.
                    pending_frames: list[bytes] = [
                        self._mw_usm_frame(
                            context,
                            game=game,
                            display_game_id=game.game_id,
                        ),
                    ]
                    if userset is not None:
                        pending_frames.append(
                            self._mw_userset_frame("+ust", userset)
                        )
                    sent = all(sender(frame) for frame in pending_frames)
                    log.info(
                        "%s rearmed post-race gjoi after owner "
                        "replacement: user=%d old_game=%d new_game=%d "
                        "sent=%d",
                        self.profile.game_id,
                        peer_identity.user_id,
                        old_game_id,
                        game.game_id,
                        1 if sent else 0,
                    )

            return ClassicPreloginReply(
                (
                    self._mw_who_frame(context, game=game),
                    self._mw_usm_frame(context, game=game),
                    created,
                ),
                "game_created",
                after_send=(
                    rearm_deferred_postrace_joins
                    if deferred_postrace_guests
                    else None
                ),
            )
        game_fields = self._game_fields(game)
        created = ClassicEAFrame.from_fields(
            "gcre",
            game_fields,
            separator="\t",
            final_separator=False,
        ).encode()
        managed = ClassicEAFrame.from_fields(
            "+mgm",
            game_fields,
            separator="\t",
            final_separator=False,
        ).encode()
        return ClassicPreloginReply(
            (created, self._who_frame(context, game.game_id), managed),
            "game_created",
        )


    def _dispatch_game_join(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        if identity is None or not context.auth.persona:
            return ClassicPreloginReply(
                (ClassicEAFrame.short("userbadc"),),
                "not_authenticated",
                True,
            )
        game: GameSession | None = None
        try:
            game_id = int(fields.get("IDENT", fields.get("GAME", "0")) or 0)
        except (TypeError, ValueError):
            game_id = 0
        if game_id:
            game = self.sessions.get_game(game_id)
        if game is None:
            name = fields.get("NAME", "").strip()
            game = next(
                (
                    candidate
                    for candidate in self.sessions.list_games()
                    if candidate.name.casefold() == name.casefold()
                ),
                None,
            )
        supplied_password = fields.get("PASS", "")
        if (
            game is not None
            and game.password
            and supplied_password != game.password
        ):
            return ClassicPreloginReply(
                (ClassicEAFrame.short("gjoipass"),),
                "game_join_bad_password",
            )
        postrace_same_game_reentry = bool(
            self._is_most_wanted
            and game is not None
            and identity.user_id in game.participants
            and context.lobby_game_id == game.game_id
            and context.mw_postrace_return_pending
            and game.state in {SessionState.ACTIVE, SessionState.FINISHED}
        )
        if postrace_same_game_reentry:
            # Stock 3playerroom&race answers this exact early re-entry
            # with `gjoi` + reserved `ugam` + one NUL payload byte.  It
            # keeps the guest in the userset, then +usm/+ust from the
            # owner's replacement makes the client retry normal gjoi.
            # Silence here leaves the client transaction open until its
            # lobby socket times out.
            context.mw_join_pending_game_id = 0
            context.mw_deferred_gjoi_game_id = game.game_id
            log.info(
                "%s rejected early post-race gjoi with stock ugam "
                "marker: user=%d old_game=%d participants=%d",
                self.profile.game_id,
                identity.user_id,
                game.game_id,
                len(game.participants),
            )
            unavailable = ClassicEAFrame(
                "gjoi",
                b"\x00",
                reserved=MW_GJOI_UNAVAILABLE_RESERVED,
            ).encode()
            return ClassicPreloginReply(
                (unavailable,),
                "game_join_unavailable_postrace",
            )
        joining_new_mw_transport = bool(
            self._is_most_wanted
            and game is not None
            and context.lobby_game_id != game.game_id
        )
        with self._connections_lock:
            postrace_handoff_return = bool(
                joining_new_mw_transport
                and game is not None
                and identity.user_id
                in self._mw_postrace_handoff_returners.get(
                    game.game_id,
                    set(),
                )
            )
        serial_slot_reserved = False
        if (
            joining_new_mw_transport
            and not postrace_handoff_return
            and game is not None
        ):
            serial_slot_reserved = self._mw_reserve_join_serial_slot(
                game.game_id,
                identity.user_id,
            )
            if not serial_slot_reserved:
                log.info(
                    "MW serialized gjoi asked client to retry: "
                    "game=%d user=%d pending=%s",
                    game.game_id,
                    identity.user_id,
                    ",".join(
                        str(user_id)
                        for user_id in sorted(
                            self._mw_join_serial_unstable.get(
                                game.game_id,
                                set(),
                            )
                        )
                    ) or "-",
                )
                unavailable = ClassicEAFrame(
                    "gjoi",
                    b"\x00",
                    reserved=MW_GJOI_UNAVAILABLE_RESERVED,
                ).encode()
                return ClassicPreloginReply(
                    (unavailable,),
                    "game_join_unavailable_serialized",
                )
        elif postrace_handoff_return and game is not None:
            # The retail three-player post-race capture accepts both returning
            # GJOI transactions before either guest publishes LT=0.  The host
            # opens the replacement CommUDP spokes only after that full room
            # projection exists, so applying the fresh-room barrier here would
            # deadlock: guest one waits for the host edge while guest two is
            # kept outside the game.  participant_order is pre-seeded from the
            # completed race only by the UDP handoff path, making this distinct
            # from a genuinely new simultaneous join.
            log.info(
                "MW post-race gjoi bypassed fresh-room serialization: "
                "game=%d user=%d expected_returns=%s",
                game.game_id,
                identity.user_id,
                ",".join(
                    str(user_id)
                    for user_id in sorted(
                        self._mw_postrace_handoff_returners.get(
                            game.game_id,
                            set(),
                        )
                    )
                ) or "-",
            )
        joined_session = bool(
            game is not None
            and self.sessions.join_game(
                game.game_id,
                identity.user_id,
                supplied_password,
                persona=context.auth.persona,
                address=context.client_address,
            )
        )
        if not joined_session:
            if serial_slot_reserved and game is not None:
                self._mw_release_join_serial_slot(
                    game.game_id,
                    identity.user_id,
                )
        if game is None or not joined_session:
            empty = ClassicEAFrame.from_fields(
                "gjoi", (), separator="\t", final_separator=False
            ).encode()
            return ClassicPreloginReply((empty,), "game_join_rejected")
        if context.lobby_game_id and context.lobby_game_id != game.game_id:
            previous_game = self.sessions.get_game(context.lobby_game_id)
            preserve_previous_transport = bool(
                previous_game is not None
                and self._preserve_transport_for_guest_exit(
                    previous_game,
                    identity.user_id,
                )
            )
            if (
                previous_game is not None
                and not preserve_previous_transport
            ):
                self._retire_game_transport(previous_game)
            elif self._is_most_wanted and previous_game is not None:
                self._mw_release_join_serial_slot(
                    previous_game.game_id,
                    identity.user_id,
                )
            detached = self.sessions.leave_game(
                context.lobby_game_id,
                identity.user_id,
            )
            if (
                detached
                and preserve_previous_transport
                and previous_game is not None
            ):
                self._sync_preserved_game_transport(previous_game)
        context.lobby_game_id = game.game_id
        if postrace_handoff_return:
            with self._connections_lock:
                returners = self._mw_postrace_handoff_returners.get(
                    game.game_id
                )
                if returners is not None:
                    returners.discard(identity.user_id)
                    if not returners:
                        self._mw_postrace_handoff_returners.pop(
                            game.game_id,
                            None,
                        )
        aux_text = self._participant_aux.get(identity.user_id, "")
        if joining_new_mw_transport:
            # A guest entering a different game also gets a fresh CommUDP graph.
            # Remove only the previous graph's CE vector; a repeated GJOI for
            # this same game must retain the CE values the client just measured.
            aux_text = self._mw_auxiliary_for_new_game(aux_text)
            self._participant_aux[identity.user_id] = aux_text
            game.participant_aux[identity.user_id] = aux_text
        elif aux_text:
            game.participant_aux[identity.user_id] = aux_text
        if self._is_most_wanted:
            userset = self._mw_userset_for_game(game.game_id)
            joined = ClassicEAFrame.from_fields(
                "gjoi",
                self._mw_game_fields(
                    game,
                    viewer_id=identity.user_id,
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            if joining_new_mw_transport:
                context.mw_join_pending_game_id = game.game_id
                # Retail sends the joiner's local G=0 projection in the GJOI
                # transaction itself.  AUXI must not be used as a delayed
                # substitute, otherwise the third OPPO slot exists before the
                # client has a matching local userset record.
                context.mw_join_snapshot_pending_game_id = 0
                context.mw_join_pending_viewer_ids.clear()
                context.mw_postrace_return_pending = False
                context.mw_postrace_snapshot_game_id = 0
                context.mw_postrace_room_view_game_id = 0
                context.mw_deferred_usea_game_id = 0
                context.mw_deferred_gjoi_game_id = 0
                context.mw_userset_staged_game_id = 0

                peer_frames: list[bytes] = []
                peer_ids = set(game.participants) - {identity.user_id}
                for peer_id in peer_ids:
                    peer = self._context_for_user(peer_id)
                    if peer is None:
                        continue
                    peer.mw_staged_onln_target_ids.setdefault(
                        game.game_id,
                        set(),
                    ).add(identity.user_id)
                if userset is not None:
                    peer_frames.append(
                        self._mw_userset_frame("+ust", userset)
                    )
                peer_frames.append(self._mw_usm_frame(context, game=game))
                self._send_users(peer_ids, tuple(peer_frames))

                # Stock 3playerroom&race primes the joining client's existing
                # userset identities before it returns the GJOI game object.
                # For a third join the observed order is the first guest and
                # then the host (reverse established join order), both with the
                # real game id.  Omitting these rows leaves retail free to
                # reuse cached I/name objects from a departed member, so the
                # otherwise-correct OPPO records can be rendered under stale
                # names.
                local_frames: list[bytes] = []
                existing_ids = [
                    user_id
                    for user_id in game.ordered_participants()
                    if user_id != identity.user_id
                ]
                for existing_id in reversed(existing_ids):
                    existing = self._context_for_user(existing_id)
                    if existing is None:
                        continue
                    local_frames.append(
                        self._mw_usm_frame(
                            existing,
                            game=game,
                            display_game_id=game.game_id,
                        )
                    )
                local_frames.extend(
                    (
                        joined,
                        self._mw_who_frame(context, game=game),
                    )
                )
                if userset is not None:
                    local_frames.append(
                        self._mw_userset_frame("+ust", userset)
                    )
                local_frames.append(self._mw_usm_frame(context, game=game))
                log.info(
                    "MW gjoi published retail local G=0 snapshot: "
                    "game=%d member=%s local_frames=%d recipients=%d userset=%s",
                    game.game_id,
                    context.auth.persona or "Player",
                    len(local_frames),
                    len(peer_ids),
                    userset.userset_id if userset is not None else 0,
                )
                return ClassicPreloginReply(
                    tuple(local_frames),
                    "game_joined",
                )

            # A repeated GJOI for the same room is acknowledged without
            # replaying +ust/+usm or restarting the established transport.
            return ClassicPreloginReply((joined,), "game_joined_repeat")
        game_fields = self._game_fields(game)
        joined = ClassicEAFrame.from_fields(
            "gjoi", game_fields, separator="\t", final_separator=False
        ).encode()
        who = self._who_frame(context, game.game_id)
        managed = ClassicEAFrame.from_fields(
            "+mgm", game_fields, separator="\t", final_separator=False
        ).encode()
        self._send_users(
            set(game.participants),
            (who, managed),
            exclude=identity.user_id,
        )
        return ClassicPreloginReply((joined, who, managed), "game_joined")


    def _dispatch_game_leave(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        command: str,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        ack = ClassicEAFrame.from_fields(
            command, (), separator="\t", final_separator=False
        ).encode()
        if identity is None or game is None:
            context.lobby_game_id = 0
            return ClassicPreloginReply((ack,), "game_leave_noop")
        if self._is_most_wanted:
            # A client may abandon a staged GJOI before publishing LT=0.
            # Its serialization reservation must not block the next guest.
            self._mw_release_join_serial_slot(
                game.game_id,
                identity.user_id,
            )
        participants = set(game.participants)
        if command == "gdel" and identity.user_id == game.owner_id:
            if self._is_most_wanted:
                self._mw_remember_departed_room_personas(
                    game,
                    viewer_ids=participants,
                    departed_ids=participants,
                )
            self._retire_game_transport(game)
            self.sessions.close_game(game.game_id)
            if self._is_most_wanted:
                userset = self._mw_userset_for_game(game.game_id)
                display_game_id = game.game_id
                deleted_game = ClassicEAFrame.from_fields(
                    "+mgm",
                    (("IDENT", display_game_id),),
                    separator="\t",
                    final_separator=False,
                ).encode()
                reset_members: list[bytes] = []
                for user_id in sorted(
                    participants,
                    key=lambda candidate: (
                        candidate != game.owner_id,
                        candidate,
                    ),
                ):
                    peer = self._context_for_user(user_id)
                    if peer is None:
                        continue
                    peer.lobby_game_id = 0
                    peer.mw_join_pending_game_id = 0
                    peer.mw_postrace_return_pending = False
                    peer.mw_postrace_snapshot_game_id = 0
                    peer.mw_postrace_room_view_game_id = 0
                    peer.mw_deferred_usea_game_id = 0
                    peer.mw_deferred_gjoi_game_id = 0
                    reset_members.append(self._mw_usm_frame(peer))
                for user_id in participants:
                    peer = self._context_for_user(user_id)
                    if (
                        peer is not None
                        and user_id != identity.user_id
                        and peer.send_wire is not None
                    ):
                        peer.send_wire(self._mw_who_frame(peer))
                        for member in reset_members:
                            peer.send_wire(member)
                        peer.send_wire(deleted_game)
                log.info(
                    "%s cascaded owner game deletion: game=%d "
                    "participants=%d",
                    self.profile.game_id,
                    display_game_id,
                    len(participants),
                )
                context.lobby_game_id = 0
                return ClassicPreloginReply((ack,), "game_deleted")
            for user_id in participants:
                peer = self._context_for_user(user_id)
                if peer is not None:
                    peer.lobby_game_id = 0
                    if (
                        user_id != identity.user_id
                        and peer.send_wire is not None
                    ):
                        for frame in self._closed_game_reset_frames(
                            peer,
                            game,
                        ):
                            if not peer.send_wire(frame):
                                break
            context.lobby_game_id = 0
            return ClassicPreloginReply((ack,), "game_deleted")
        preserve_shared_race_transport = (
            self._preserve_transport_for_guest_exit(
                game,
                identity.user_id,
            )
        )
        if preserve_shared_race_transport:
            log.info(
                "%s preserved owner race transport after guest leave: "
                "game=%d user=%d owner=%d",
                self.profile.game_id,
                game.game_id,
                identity.user_id,
                game.owner_id,
            )
        else:
            self._retire_game_transport(game)
        if self._is_most_wanted:
            self._mw_remember_departed_room_personas(
                game,
                viewer_ids=participants - {identity.user_id},
                departed_ids={identity.user_id},
            )
        detached = self.sessions.leave_game(game.game_id, identity.user_id)
        context.lobby_game_id = 0
        context.mw_join_pending_game_id = 0
        context.mw_postrace_return_pending = False
        context.mw_postrace_snapshot_game_id = 0
        context.mw_postrace_room_view_game_id = 0
        context.mw_deferred_usea_game_id = 0
        context.mw_deferred_gjoi_game_id = 0
        remaining = self.sessions.get_game(game.game_id)
        if remaining is not None:
            if detached and preserve_shared_race_transport:
                self._sync_preserved_game_transport(remaining)
            if self._is_most_wanted:
                userset = self._mw_userset_for_game(game.game_id)
                display_game_id = remaining.game_id
                managed = ClassicEAFrame.from_fields(
                    "+mgm",
                    self._mw_game_fields(remaining),
                    separator="\t",
                    final_separator=False,
                ).encode()
                departed = self._mw_usm_frame(context)
                self._send_users(
                    set(remaining.participants),
                    (departed, managed),
                )
                left = ClassicEAFrame.from_fields(
                    command,
                    self._mw_game_fields(remaining),
                    reserved=packet.reserved,
                    separator="\t",
                    final_separator=False,
                ).encode()
                return ClassicPreloginReply((left,), "game_left")
            managed = ClassicEAFrame.from_fields(
                "+mgm",
                self._game_fields(remaining),
                separator="\t",
                final_separator=False,
            ).encode()
            self._send_users(set(remaining.participants), (managed,))
        return ClassicPreloginReply((ack,), "game_left")


    def _dispatch_u2_kick(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
        command: str,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        log.info(
            "[kick] received gset KICK=%s actor=%s",
            fields.get("KICK", fields.get("NAME", fields.get("PERS", ""))),
            context.auth.persona or "Player",
        )
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        ack = ClassicEAFrame.from_fields(
            packet.command, (), separator="\t", final_separator=False
        ).encode()
        if identity is None or game is None or identity.user_id != game.owner_id:
            return ClassicPreloginReply((ack,), "kick_denied")
        try:
            target_id = int(fields.get("UID", fields.get("CALLUSER", "0")) or 0)
        except (TypeError, ValueError):
            target_id = 0
        target_name = fields.get(
            "KICK", fields.get("NAME", fields.get("PERS", ""))
        ).strip()
        if not target_id and target_name:
            target_id = next(
                (
                    user_id
                    for user_id, persona in game.participant_personas.items()
                    if persona.casefold() == target_name.casefold()
                ),
                0,
            )
        if not target_id or target_id == identity.user_id:
            return ClassicPreloginReply((ack,), "kick_no_target")
        target = self._context_for_user(target_id)
        userset = (
            self._mw_userset_for_game(game.game_id)
            if self._is_most_wanted
            else None
        )
        if not self.sessions.leave_game(game.game_id, target_id, kicked=True):
            return ClassicPreloginReply((ack,), "kick_no_target")
        if self._is_most_wanted:
            self._mw_release_join_serial_slot(game.game_id, target_id)
        if userset is not None:
            with self._usersets_lock:
                if userset.members is not None:
                    userset.members.discard(target_id)
        preserve_shared_race_transport = self._preserve_transport_for_guest_exit(
            game,
            target_id,
        )
        if not preserve_shared_race_transport:
            self._retire_game_transport(game)
        else:
            self._sync_preserved_game_transport(game)
        if target is not None:
            target.lobby_game_id = 0
            if userset is not None and target.userset_id == userset.userset_id:
                target.userset_id = 0
            target.mw_join_pending_game_id = 0
            if target.send_wire is not None:
                remaining = self.sessions.get_game(game.game_id)
                if remaining is not None:
                    for frame in self._kicked_reset_frames(target, remaining):
                        if not target.send_wire(frame):
                            break
        remaining = self.sessions.get_game(game.game_id)
        if remaining is not None:
            host_ack = ClassicEAFrame.from_fields(
                packet.command,
                (
                    self._mw_game_fields(
                        remaining,
                        viewer_id=identity.user_id,
                    )
                    if self._is_most_wanted
                    else self._game_fields(remaining)
                )
                if command == "gset"
                else (),
                separator="\t",
                final_separator=False,
            ).encode()
            managed = ClassicEAFrame.from_fields(
                "+mgm",
                (
                    self._mw_game_fields(
                        remaining,
                        viewer_id=identity.user_id,
                    )
                    if self._is_most_wanted
                    else self._game_fields(remaining)
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            roster = (
                self._mw_userset_roster_frames(userset)
                if userset is not None
                else ()
            )
            peer_frames = (*roster, managed)
            self._send_users(
                set(remaining.participants),
                peer_frames,
                exclude=identity.user_id,
            )
            if userset is not None:
                self._mw_log_userset_roster(userset)
            log.info(
                "[game] sent +mgm game_id=%d participants=%d after_kick=%s",
                remaining.game_id,
                len(remaining.participants),
                target_name or target_id,
            )
            return ClassicPreloginReply(
                (host_ack, *roster, managed),
                "player_kicked",
            )
        return ClassicPreloginReply((ack,), "player_kicked")


    def _dispatch_game_settings(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
        command: str,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        if identity is None or game is None:
            empty = ClassicEAFrame.from_fields(
                packet.command, (), separator="\t", final_separator=False
            ).encode()
            return ClassicPreloginReply((empty,), "game_settings_noop")
        if self._is_most_wanted and command == "gset":
            if identity.user_id == game.owner_id:
                if fields.get("PARAMS"):
                    game.params = fields["PARAMS"]
                if fields.get("NAME", "").strip():
                    game.name = fields["NAME"].strip()
                if "MINSIZE" in fields:
                    try:
                        game.min_players = max(
                            1,
                            min(
                                game.capacity,
                                int(fields["MINSIZE"] or game.min_players),
                            ),
                        )
                    except (TypeError, ValueError):
                        pass
            # In the retail post-race flow both returning guests can complete
            # GJOI before either one has resolved the other through ONLN.  The
            # owner's lowercase GSET is the room-commit point: the server
            # answers with every staged member promoted from G=0 and one full
            # game object.  Waiting for every current participant to issue an
            # ONLN lookup creates a circular dependency between simultaneous
            # returners and leaves the host rendering both rows disconnected.
            #
            # Do not consume the promotion from the uppercase Messenger
            # callback.  Its response travels on the callback socket; the
            # lowercase request is the copy whose roster frames reach the
            # host's main lobby stream.
            pending_members: list[ClassicPreloginContext] = []
            if (
                identity.user_id == game.owner_id
                and packet.command == packet.command.casefold()
                and not packet.reserved
            ):
                for user_id in game.ordered_participants():
                    if user_id == identity.user_id:
                        continue
                    member_context = self._context_for_user(user_id)
                    if (
                        member_context is not None
                        and member_context.mw_join_pending_game_id
                        == game.game_id
                    ):
                        pending_members.append(member_context)
            ack = ClassicEAFrame.from_fields(
                packet.command,
                self._mw_game_fields(
                    game,
                    viewer_id=identity.user_id,
                ),
                reserved=packet.reserved,
                separator="\t",
                final_separator=False,
            ).encode()
            if not pending_members:
                return ClassicPreloginReply((ack,), "game_settings")

            promoted_members = tuple(
                self._mw_usm_frame(
                    member_context,
                    game=game,
                    display_game_id=game.game_id,
                )
                for member_context in pending_members
            )
            managed = ClassicEAFrame.from_fields(
                "+mgm",
                self._mw_game_fields(
                    game,
                    viewer_id=identity.user_id,
                ),
                separator="\t",
                final_separator=False,
            ).encode()

            def publish_owner_room_commit() -> None:
                promoted: list[tuple[int, ClassicPreloginContext, bytes]] = []
                for member_context, member in zip(
                    pending_members,
                    promoted_members,
                ):
                    member_id = self._user_id(member_context)
                    if (
                        member_context.mw_join_pending_game_id
                        != game.game_id
                        or member_id not in game.participants
                    ):
                        continue
                    member_context.mw_join_pending_game_id = 0
                    member_context.mw_join_pending_viewer_ids.clear()
                    promoted.append((member_id, member_context, member))

                if not promoted:
                    return

                for peer_id in set(game.participants):
                    peer = self._context_for_user(peer_id)
                    if peer is None:
                        continue
                    staged_ids = peer.mw_staged_onln_target_ids.get(
                        game.game_id
                    )
                    if staged_ids is not None:
                        staged_ids.difference_update(
                            member_id for member_id, _, _ in promoted
                        )
                        if not staged_ids:
                            peer.mw_staged_onln_target_ids.pop(
                                game.game_id,
                                None,
                            )

                # The owner already received these rows in the GSET reply.
                # Every guest receives its own promoted +who/+usm, the other
                # newly committed members, and one final complete room object.
                for viewer_id in set(game.participants) - {identity.user_id}:
                    viewer = self._context_for_user(viewer_id)
                    if viewer is None or viewer.send_wire is None:
                        continue
                    for member_id, member_context, member in promoted:
                        if member_id == viewer_id:
                            viewer.send_wire(
                                self._mw_who_frame(
                                    member_context,
                                    game=game,
                                    display_game_id=game.game_id,
                                )
                            )
                        viewer.send_wire(member)
                    viewer.send_wire(
                        ClassicEAFrame.from_fields(
                            "+mgm",
                            self._mw_game_fields(
                                game,
                                viewer_id=viewer_id,
                            ),
                            separator="\t",
                            final_separator=False,
                        ).encode()
                    )

                log.info(
                    "MW owner gset committed staged room members: "
                    "game=%d owner=%s promoted=%s participants=%d",
                    game.game_id,
                    context.auth.persona or identity.user_id,
                    ",".join(
                        member_context.auth.persona or str(member_id)
                        for member_id, member_context, _ in promoted
                    ),
                    len(game.participants),
                )

            return ClassicPreloginReply(
                (ack, *promoted_members, managed),
                "game_settings_room_commit",
                after_send=publish_owner_room_commit,
            )
        try:
            user_flags = int(fields.get("USERFLAGS", "0") or 0)
        except (TypeError, ValueError):
            user_flags = 0
        ready = command == "term" or bool(user_flags & U2_READY_FLAG)
        self.sessions.set_ready(game.game_id, identity.user_id, ready)
        if identity.user_id == game.owner_id:
            if fields.get("PARAMS"):
                game.params = fields["PARAMS"]
            if command == "gset" and (
                self.profile.u2_game_size_policy == "server"
                or "MINSIZE" in fields
                or "MAXSIZE" in fields
            ):
                previous_min, previous_max = game.min_players, game.capacity
                game.min_players, game.capacity = self._u2_game_sizes(
                    fields,
                    current=game,
                )
                log.info(
                    "%s U2 game size updated: game=%d user=%d policy=%s "
                    "min=%d->%d max=%d->%d",
                    self.profile.game_id,
                    game.game_id,
                    identity.user_id,
                    self.profile.u2_game_size_policy,
                    previous_min,
                    game.min_players,
                    previous_max,
                    game.capacity,
                )
        game_fields = self._game_fields(game)
        ack = ClassicEAFrame.from_fields(
            packet.command,
            game_fields if command == "gset" else (),
            separator="\t",
            final_separator=False,
        ).encode()
        managed = ClassicEAFrame.from_fields(
            "+mgm", game_fields, separator="\t", final_separator=False
        ).encode()
        self._send_users(
            set(game.participants),
            (managed,),
            exclude=identity.user_id,
        )
        return ClassicPreloginReply((ack, managed), "game_settings")


    def _dispatch_game_start(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        mw_callback: bool,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        # U2 and MW use different start paths. MW's stock ASI issues an
        # uppercase callback request and expects its opaque token back.
        ack = (
            self._mw_callback_ack(packet)
            if self._is_most_wanted
            else ClassicEAFrame("gsta", b"\x00" * 9).encode()
        )
        if identity is None or game is None or identity.user_id != game.owner_id:
            return ClassicPreloginReply((ack,), "game_start_denied")
        if len(game.participants) < game.min_players:
            return ClassicPreloginReply((ack,), "game_start_waiting_for_players")
        self.sessions.set_state(game.game_id, SessionState.ACTIVE)
        seed = int(time.time()) & 0x7FFFFFFF
        self._mw_session_seeds[game.game_id] = seed
        if self._is_underground2:
            for user_id in game.participants:
                self._u2_pending_games[user_id] = game.game_id
            room = self._u2_room(game.room_id)
            log.info(
                "%s preserved rank correlation at game start: "
                "game=%d room=%s ranked=%d participants=%s",
                self.profile.game_id,
                game.game_id,
                room[1] if room is not None else str(game.room_id),
                1 if room is not None and room[0] <= 4 else 0,
                ",".join(str(user_id) for user_id in sorted(game.participants)),
            )
        with self._connections_lock:
            self._mw_postrace_room_view_games.discard(game.game_id)
            self._mw_postrace_handoff_returners.pop(game.game_id, None)
        if self.race_registrar is not None:
            game.participant_race_addresses = self.race_registrar(game)
        if self._is_most_wanted:
            actor_frames = self._mw_start_frames(context, game, seed)
            for user_id in set(game.participants):
                if user_id == identity.user_id:
                    continue
                peer = self._context_for_user(user_id)
                if peer is None or peer.send_wire is None:
                    continue
                for frame in self._mw_start_frames(peer, game, seed):
                    if not peer.send_wire(frame):
                        break
            if mw_callback:
                actor = self._context_for_user(identity.user_id)
                if actor is not None and actor.send_wire is not None:
                    for frame in actor_frames:
                        if not actor.send_wire(frame):
                            break
                callback_frames = [ack]
                for user_id in sorted(
                    game.participants,
                    key=lambda candidate: (candidate != game.owner_id, candidate),
                ):
                    participant = self._context_for_user(user_id)
                    if participant is not None:
                        callback_frames.append(self._mw_usr_frame(participant, game))
                callback_frames.append(self._mw_gam_frame(game))
                return ClassicPreloginReply(
                    tuple(callback_frames),
                    "game_started_callback",
                )
            return ClassicPreloginReply(
                (ack, *actor_frames),
                "game_started",
            )
        host_start_fields = self._game_fields(
            game, viewer_id=identity.user_id, start=True
        )
        # Stock U2 copies +ses.AUTH into its pending game-report state.
        # At race finish it discards RESU before building the `rank`
        # command when AUTH is empty, which surfaces as the native
        # "server refused the results" message without any rank frame on
        # the wire.  Keep one stable authority ticket for every viewer of
        # this race; the client echoes it in the eventual report.
        session_auth = md5(
            f"u2:{game.game_id}:{game.owner_id}:{seed}".encode("ascii")
        ).hexdigest()
        managed = ClassicEAFrame.from_fields(
            "+mgm", host_start_fields, separator="\t", final_separator=False
        ).encode()
        session = ClassicEAFrame.from_fields(
            "+ses",
            (
                *host_start_fields,
                ("AUTH", session_auth),
                ("SEED", seed),
                ("SELF", context.auth.persona),
            ),
            separator="\t",
            final_separator=False,
        ).encode()
        for user_id in set(game.participants):
            if user_id == identity.user_id:
                continue
            peer = self._context_for_user(user_id)
            if peer is None or peer.send_wire is None:
                continue
            peer_start_fields = self._game_fields(
                game, viewer_id=user_id, start=True
            )
            peer_managed = ClassicEAFrame.from_fields(
                "+mgm",
                peer_start_fields,
                separator="\t",
                final_separator=False,
            ).encode()
            peer_session = ClassicEAFrame.from_fields(
                "+ses",
                (
                    *peer_start_fields,
                    ("AUTH", session_auth),
                    ("SEED", seed),
                    ("SELF", peer.auth.persona),
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            peer.send_wire(ack)
            peer.send_wire(peer_managed)
            peer.send_wire(peer_session)
        return ClassicPreloginReply((ack, managed, session), "game_started")
