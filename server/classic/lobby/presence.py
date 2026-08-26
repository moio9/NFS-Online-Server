"""Classic lobby presence and auxiliary-state commands.

This mixin owns the ``auxi`` and ``onln`` flows shared by Underground 2 and
Most Wanted.  It keeps the public ``ClassicPreloginService`` API and wire
ordering unchanged while removing presence synchronization from the router.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from classic.ea.directory import SessionState
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply
from .mw_control_bridge import update_mw_control_projection

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


# Preserve the existing operational logger category.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicPresenceMixin:
    """Handle Classic auxiliary state and online-presence projection."""

    def _dispatch_auxiliary(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        aux_text = str(fields.get("TEXT", "") or "")
        if (
            self._is_most_wanted
            and identity is not None
            and aux_text
            and ("SCF%3d" in aux_text or "CE%3d" in aux_text)
        ):
            log.info(
                "MW AUX TRACE: game=%d user=%d persona=%s text=%s",
                context.lobby_game_id,
                identity.user_id,
                context.auth.persona or "Player",
                aux_text,
            )
        # Mirror the exact client-owned AUX state to the Messenger callback
        # bridge only after identity and game lookup have completed.
        if self._is_most_wanted and identity is not None and aux_text:
            try:
                bridge_address = (
                    game.participant_race_addresses.get(
                        user_id,
                        context.client_address,
                    )
                    if game is not None
                    else context.client_address
                )
                update_mw_control_projection(
                    wire_id=self._mw_wire_user_id(user_id),
                    game_id=game.game_id if game is not None else 0,
                    persona=context.auth.persona or "Player",
                    aux=aux_text,
                    address=bridge_address,
                    client_ip=(context.auth.client_ip or context.client_address),
                )
            except Exception:
                log.exception("MW control projection update failed")

        if (
            self._is_most_wanted
            and identity is not None
            and game is not None
            and aux_text
            and self._mw_auxiliary_link_settled(aux_text)
            and self._mw_release_join_serial_slot(
                game.game_id,
                identity.user_id,
            )
        ):
            log.info(
                "MW serialized gjoi released after settled LT: "
                "game=%d user=%d persona=%s",
                game.game_id,
                identity.user_id,
                context.auth.persona or "Player",
            )

        if identity is not None and aux_text:
            self._participant_aux[identity.user_id] = aux_text
            if context.lobby_game_id:
                game = self.sessions.get_game(context.lobby_game_id)
                if game is not None:
                    game.participant_aux[identity.user_id] = aux_text
                    if self._is_most_wanted:
                        with self._connections_lock:
                            postrace_game_in_room_view = (
                                game.game_id
                                in self._mw_postrace_room_view_games
                            )
                        if (
                            context.mw_postrace_return_pending
                            and postrace_game_in_room_view
                            and identity.user_id in game.participants
                            and game.state
                            in {SessionState.ACTIVE, SessionState.FINISHED}
                        ):
                            self._mw_record_auxiliary(
                                game,
                                identity.user_id,
                                aux_text,
                            )
                            ack = ClassicEAFrame.from_fields(
                                "auxi",
                                (("TEXT", aux_text),),
                                separator="\t",
                                final_separator=False,
                            ).encode()
                            return ClassicPreloginReply(
                                (ack,),
                                "auxiliary_postrace_room_view",
                            )
                        postrace_guest_waiting = bool(
                            context.mw_postrace_return_pending
                            and identity.user_id != game.owner_id
                            and identity.user_id in game.participants
                            and game.state
                            in {SessionState.ACTIVE, SessionState.FINISHED}
                        )
                        if postrace_guest_waiting:
                            # The guest-first capture keeps auxiliary
                            # presence in lobby view after the bare old
                            # game +mgm.  A normal ready refresh would
                            # republish every participant at G=<old game>
                            # and resurrect the finished race object.
                            self._mw_record_auxiliary(
                                game,
                                identity.user_id,
                                aux_text,
                            )
                            ack = ClassicEAFrame.from_fields(
                                "auxi",
                                (("TEXT", aux_text),),
                                separator="\t",
                                final_separator=False,
                            ).encode()
                            room_member = self._mw_usm_frame(
                                context,
                                game=game,
                            )
                            self._send_users(
                                set(game.participants),
                                (room_member,),
                                exclude=identity.user_id,
                            )
                            return ClassicPreloginReply(
                                (
                                    ack,
                                    self._mw_who_frame(
                                        context,
                                        game=game,
                                    ),
                                ),
                                "auxiliary_postrace_guest_waiting",
                            )
                        self._mw_record_auxiliary(
                            game,
                            identity.user_id,
                            aux_text,
                        )
                        # CE is client-owned transport state.  In retail a
                        # healthy two-player room remains at CE=3,1 and grows
                        # to CE=3,1,1 when a third member joins.  Rewriting the
                        # two-player value to 3,3 pre-completes the future
                        # guest slot, so the host never creates that CommUDP
                        # edge and ignores the third client's cmd1 bootstrap.
                        display_game_id = game.game_id
                        ack = ClassicEAFrame.from_fields(
                            "auxi",
                            (("TEXT", aux_text),),
                            separator="\t",
                            final_separator=False,
                        ).encode()
                        member = self._mw_usm_frame(
                            context,
                            game=game,
                            display_game_id=display_game_id,
                        )
                        if (
                            context.mw_join_snapshot_pending_game_id
                            == game.game_id
                        ):
                            context.mw_join_snapshot_pending_game_id = 0
                            userset = self._mw_userset_for_game(game.game_id)
                            snapshot = (
                                self._mw_userset_roster_frames(
                                    userset,
                                    initial_user_id=identity.user_id,
                                )
                                if userset is not None
                                else ()
                            )
                            initial_frames = (
                                ack,
                                self._mw_who_frame(context, game=game),
                                *snapshot,
                            )
                            if len(game.participants) > 2:
                                # Retail 3playerroom&race sends the first 3+
                                # join AUXI snapshot without an AUXI ack:
                                # +who, +ust, then one +usm per participant.
                                # Promotion follows separately as
                                # +who/+usm/+mgm after the staged ONLN barrier.
                                initial_frames = initial_frames[1:]

                            def publish_joiner_membership() -> None:
                                if (
                                    context.mw_join_pending_game_id
                                    == game.game_id
                                    or context.send_wire is None
                                ):
                                    return
                                # Existing peers already received the retail
                                # G=0 -> G=<game> promotion.  The joiner still
                                # needs the same active membership locally,
                                # but republishing it to the peers here would
                                # restart a transport edge they just opened.
                                context.send_wire(
                                    self._mw_who_frame(
                                        context,
                                        game=game,
                                        display_game_id=game.game_id,
                                    )
                                )
                                context.send_wire(member)
                                context.send_wire(
                                    ClassicEAFrame.from_fields(
                                        "+mgm",
                                        self._mw_game_fields(
                                            game,
                                            viewer_id=identity.user_id,
                                        ),
                                        separator="\t",
                                        final_separator=False,
                                    ).encode()
                                )

                            log.info(
                                "MW auxi published deferred join snapshot: "
                                "game=%d member=%s frames=%d promoted=%d",
                                game.game_id,
                                context.auth.persona or "Player",
                                len(initial_frames),
                                int(
                                    context.mw_join_pending_game_id
                                    != game.game_id
                                ),
                            )
                            return ClassicPreloginReply(
                                initial_frames,
                                "auxiliary_join_snapshot",
                                after_send=publish_joiner_membership,
                            )
                        if (
                            context.mw_join_pending_game_id
                            == game.game_id
                        ):
                            # Retail keeps a freshly joined MW member staged
                            # at G=0 until an existing peer resolves it with
                            # `onln PERS=<joiner>`.  AUXI may deliver the G=0
                            # roster snapshot above, but a repeated AUXI must
                            # not create/promote the CommUDP edge early.
                            # `_dispatch_online()` is the sole G=0 -> G=game
                            # promotion point and publishes +usm/+mgm only
                            # after the ONLN response has been written.
                            log.info(
                                "MW auxi kept staged join pending for onln: "
                                "game=%d user=%d participants=%d",
                                game.game_id,
                                identity.user_id,
                                len(game.participants),
                            )
                            return ClassicPreloginReply(
                                (ack,),
                                "auxiliary_join_waiting",
                            )
                        self._send_users(
                            set(game.participants),
                            (member,),
                            exclude=identity.user_id,
                        )
                        if game.ready_participants:
                            # Retail projects one AUX author at a time: peers
                            # receive that author's +usm, while the author
                            # receives its own +who/+usm pair.  Replaying the
                            # complete roster here races simultaneous guest
                            # AUX updates and can make a client rebind its
                            # ready/connectivity UI to another OPPO slot.
                            own_frames = [
                                ack,
                                self._mw_who_frame(
                                    context,
                                    game=game,
                                    display_game_id=display_game_id,
                                ),
                                member,
                            ]
                            return ClassicPreloginReply(
                                tuple(own_frames),
                                "auxiliary_ready_refresh",
                            )
                        return ClassicPreloginReply(
                            (ack,),
                            "auxiliary",
                        )
                    self._send_users(
                        set(game.participants),
                        (self._who_frame(context, game.game_id),),
                        exclude=identity.user_id,
                    )
        return ClassicPreloginReply((self._auxiliary_frame(),), "auxiliary")


    def _dispatch_online(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        requested = fields.get("PERS", fields.get("NAME", "")).strip()
        with self._connections_lock:
            target: ClassicPreloginContext | None = None
            if requested:
                target = next(
                    (
                        candidate
                        for candidate in self._connections.values()
                        if candidate.auth.persona.casefold()
                        == requested.casefold()
                        or (
                            candidate.auth.account is not None
                            and candidate.auth.account.account_name.casefold()
                            == requested.casefold()
                        )
                    ),
                    None,
                )
            else:
                target = context
        if self._is_most_wanted:
            requester_id = self._user_id(context)
            requester_game = (
                self.sessions.get_game(context.lobby_game_id)
                if context.lobby_game_id
                else None
            )
            resolved_target_id = self._user_id(target) if target is not None else 0
            requested_key = requested.casefold()
            staged_replacements: list[ClassicPreloginContext] = []
            if (
                requester_game is not None
                and requester_id in requester_game.participants
                # A guest which is still completing its own GJOI must resolve
                # its requested established peers first.  When two guests
                # join in the same scheduler window, the second GJOI stages
                # that member for the first guest as well; redirecting the
                # first guest's ONLN(host) to the second guest crosses their
                # local peer graphs.  Keep the staged row queued and consume
                # it once this viewer's own membership has been promoted.
                and context.mw_join_pending_game_id
                != requester_game.game_id
            ):
                staged_ids = context.mw_staged_onln_target_ids.get(
                    requester_game.game_id,
                    set(),
                )
                for staged_id in tuple(staged_ids):
                    candidate = self._context_for_user(staged_id)
                    if (
                        candidate is None
                        or staged_id not in requester_game.participants
                        or candidate.mw_join_pending_game_id
                        != requester_game.game_id
                    ):
                        staged_ids.discard(staged_id)
                        continue
                    staged_replacements.append(candidate)
                if not staged_ids:
                    context.mw_staged_onln_target_ids.pop(
                        requester_game.game_id,
                        None,
                    )
            if len(staged_replacements) == 1:
                replacement = staged_replacements[0]
                replacement_id = self._user_id(replacement)
                requester_userset = (
                    self._mw_userset_for_game(requester_game.game_id)
                    if requester_game is not None
                    else None
                )
                resolved_waiting_userset_member = bool(
                    target is not None
                    and requester_userset is not None
                    and resolved_target_id
                    in set(requester_userset.members or set())
                    and resolved_target_id not in requester_game.participants
                )
                if (
                    resolved_target_id != replacement_id
                    and not resolved_waiting_userset_member
                ):
                    log.info(
                        "MW onln bound viewer lookup to staged member: "
                        "requester=%s requested=%s resolved=%s replacement=%s "
                        "game=%d",
                        context.auth.persona or "Player",
                        requested or "-",
                        (
                            target.auth.persona
                            if target is not None and target.auth.persona
                            else "-"
                        ),
                        replacement.auth.persona or replacement_id,
                        requester_game.game_id,
                    )
                    target = replacement
                    resolved_target_id = replacement_id
                    context.mw_departed_room_personas.discard(requested_key)
                elif resolved_waiting_userset_member:
                    # Host-first post-race re-entry retains every old guest in
                    # the userset while they GJOI the replacement game one by
                    # one.  A lookup for a later returnee is therefore real;
                    # consuming it as the sole currently staged guest makes
                    # the client cache the wrong persona and omit the later
                    # ONLN promotion when that member actually rejoins.
                    log.info(
                        "MW onln preserved waiting post-race userset member: "
                        "requester=%s requested=%s staged=%s game=%d",
                        context.auth.persona or "Player",
                        target.auth.persona or requested,
                        replacement.auth.persona or replacement_id,
                        requester_game.game_id,
                    )
            if (
                requested_key
                and requested_key in context.mw_departed_room_personas
                and requester_game is not None
                and requester_id in requester_game.participants
                and resolved_target_id not in requester_game.participants
            ):
                pending_replacements = []
                for user_id in requester_game.ordered_participants():
                    if user_id == requester_id:
                        continue
                    candidate = self._context_for_user(user_id)
                    if (
                        candidate is not None
                        and candidate.mw_join_pending_game_id
                        == requester_game.game_id
                    ):
                        pending_replacements.append(candidate)
                if len(pending_replacements) == 1:
                    replacement = pending_replacements[0]
                    replacement_id = self._user_id(replacement)
                    log.info(
                        "MW onln redirected departed persona to sole pending "
                        "member: requester=%s requested=%s resolved=%s "
                        "replacement=%s game=%d",
                        context.auth.persona or "Player",
                        requested,
                        (
                            target.auth.persona
                            if target is not None and target.auth.persona
                            else "-"
                        ),
                        replacement.auth.persona or replacement_id,
                        requester_game.game_id,
                    )
                    target = replacement
                    context.mw_departed_room_personas.discard(requested_key)
            target_id = self._user_id(target) if target is not None else 0
            target_persona = (
                target.auth.persona
                if target is not None and target.auth.persona
                else requested
            ) or "Player"
            target_address = (
                target.client_address if target is not None else ""
            )
            target_game_id = (
                target.lobby_game_id if target is not None else 0
            )
            target_game = (
                self.sessions.get_game(target_game_id)
                if target_game_id
                else None
            )
            userset = (
                self._mw_userset_for_game(target_game.game_id)
                if target_game is not None
                else None
            )
            pending_target_game = (
                target_game
                if target is not None
                and target_game is not None
                and target.mw_join_pending_game_id == target_game.game_id
                else None
            )
            promotion_ready = False
            if pending_target_game is not None:
                requester_id = self._user_id(context)
                expected_viewers = (
                    set(pending_target_game.participants) - {target_id}
                )
                if requester_id in expected_viewers:
                    target.mw_join_pending_viewer_ids.add(requester_id)
                promotion_ready = bool(
                    expected_viewers
                ) and expected_viewers.issubset(
                    target.mw_join_pending_viewer_ids
                )
            viewer_game = (
                self.sessions.get_game(context.lobby_game_id)
                if context.lobby_game_id
                else None
            )
            postrace_room_view = bool(
                context.mw_postrace_return_pending
                and viewer_game is not None
                and target_game is not None
                and viewer_game.game_id == target_game.game_id
                and target_id in viewer_game.participants
                and viewer_game.state
                in {SessionState.ACTIVE, SessionState.FINISHED}
            )
            display_game_id = (
                target_game.game_id
                if (
                    target_game is not None
                    and not postrace_room_view
                    and (
                        pending_target_game is None
                        or promotion_ready
                    )
                )
                else 0
            )
            aux_text = self._participant_aux.get(target_id, "")
            if target_game is not None:
                aux_text = target_game.participant_aux.get(
                    target_id,
                    aux_text,
                )
            online = ClassicEAFrame.from_fields(
                "onln",
                (
                    ("I", self._mw_wire_user_id(target_id) if target_id else 0),
                    ("N", target_persona),
                    ("M", target_persona),
                    ("F", ""),
                    ("A", target_address),
                    ("P", 425),
                    ("S", ""),
                    ("G", display_game_id),
                    ("AT", ""),
                    ("CL", 511),
                    ("LV", 0),
                    ("MD", 0),
                    ("LA", target_address),
                    ("HW", 0),
                    ("RP", 0),
                    ("MA", ""),
                    ("LO", "enUS"),
                    ("X", aux_text),
                    ("US", userset.name if userset is not None else ""),
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            promotion_game = (
                pending_target_game
                if pending_target_game is not None and promotion_ready
                else None
            )

            def publish_pending_game_membership() -> None:
                if (
                    target is None
                    or promotion_game is None
                    or target.mw_join_pending_game_id
                    != promotion_game.game_id
                ):
                    return
                target.mw_join_pending_game_id = 0
                target.mw_join_pending_viewer_ids.clear()
                for peer_id in set(promotion_game.participants):
                    peer = self._context_for_user(peer_id)
                    if peer is None:
                        continue
                    staged_ids = peer.mw_staged_onln_target_ids.get(
                        promotion_game.game_id,
                    )
                    if staged_ids is None:
                        continue
                    staged_ids.discard(target_id)
                    if not staged_ids:
                        peer.mw_staged_onln_target_ids.pop(
                            promotion_game.game_id,
                            None,
                        )
                member = self._mw_usm_frame(
                    target,
                    game=promotion_game,
                    display_game_id=promotion_game.game_id,
                )
                for peer_id in set(promotion_game.participants):
                    if peer_id == target_id:
                        continue
                    peer = self._context_for_user(peer_id)
                    if peer is None or peer.send_wire is None:
                        continue
                    peer.send_wire(member)
                    # +usm promotes the userset member, while the complete
                    # game object supplies the new player's stable slot and
                    # relay address.  The MW client shim consumes 2 -> 3 (and
                    # 3 -> 4) snapshots append-only so established CommUDP
                    # records are not torn down during this update.
                    peer.send_wire(
                        ClassicEAFrame.from_fields(
                            "+mgm",
                            self._mw_game_fields(
                                promotion_game,
                                viewer_id=peer_id,
                            ),
                            separator="\t",
                            final_separator=False,
                        ).encode()
                    )
                if (
                    target.mw_join_snapshot_pending_game_id == 0
                    and target.send_wire is not None
                ):
                    target.send_wire(
                        self._mw_who_frame(
                            target,
                            game=promotion_game,
                            display_game_id=promotion_game.game_id,
                        )
                    )
                    target.send_wire(member)
                    target.send_wire(
                        ClassicEAFrame.from_fields(
                            "+mgm",
                            self._mw_game_fields(
                                promotion_game,
                                viewer_id=target_id,
                            ),
                            separator="\t",
                            final_separator=False,
                        ).encode()
                    )
                log.info(
                    "[userset] sent +usm member=%s wire_id=%d G=%d",
                    target.auth.persona or "Player",
                    self._mw_wire_user_id(target_id),
                    promotion_game.game_id,
                )
                log.info(
                    "[game] sent +mgm game_id=%d promoted=%s participants=%d",
                    promotion_game.game_id,
                    target.auth.persona or "Player",
                    len(promotion_game.participants),
                )
                if self._mw_release_join_serial_slot(
                    promotion_game.game_id,
                    target_id,
                ):
                    log.info(
                        "MW serialized gjoi released after ONLN room "
                        "promotion: game=%d user=%d",
                        promotion_game.game_id,
                        target_id,
                    )

            return ClassicPreloginReply(
                (online,),
                "online_status",
                after_send=(
                    publish_pending_game_membership
                    if promotion_game is not None
                    else None
                ),
            )
        if target is None:
            target = context
        target_id = self._user_id(target)
        target_game_id = target.lobby_game_id
        target_game = (
            self.sessions.get_game(target_game_id) if target_game_id else None
        )
        aux_text = self._participant_aux.get(target_id, "")
        if target_game is not None:
            aux_text = target_game.participant_aux.get(
                target_id,
                aux_text,
            )
        stats = ",".join(["270f", "0", "0", "0", "64", "65", "65"] * 5)
        online = ClassicEAFrame.from_fields(
            "onln",
            (
                ("I", target_id),
                (
                    "M",
                    target.auth.account.account_name
                    if target.auth.account is not None
                    else target.auth.persona,
                ),
                ("N", target.auth.persona or "Player"),
                ("F", "G" if target_game_id else "U"),
                ("A", target.client_address),
                ("P", 1 if target_game_id else 2),
                ("S", stats),
                ("X", aux_text),
                ("G", target_game_id),
                ("AT", ""),
                ("CL", 0),
                ("LV", 0),
                ("MD", 0),
                ("LA", target.client_address),
                ("HW", 0),
                ("RP", 0),
                ("MA", target.client_address),
                ("US", ""),
            ),
            separator="\t",
            final_separator=False,
        ).encode()
        frames: list[bytes] = [online]
        if target_game is not None:
            frames.append(self._who_frame(target, target_game_id))
            frames.append(
                ClassicEAFrame.from_fields(
                    "+mgm",
                    self._game_fields(target_game),
                    separator="\t",
                    final_separator=False,
                ).encode()
            )
        return ClassicPreloginReply(tuple(frames), "online_status")
