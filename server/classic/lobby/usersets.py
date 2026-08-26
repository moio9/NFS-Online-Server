"""Most Wanted userset rooms for the shared Classic lobby service.

The mixin preserves the historical ``ClassicPreloginService`` API while
separating userset wire projection, discovery, membership and lifecycle from
the protocol router. Cross-title game creation and join orchestration lives
separately in ``classic.lobby.game_commands``.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import TYPE_CHECKING

from classic.ea.directory import GameSession, SessionState
from classic.lobby.models import (
    ClassicPreloginContext,
    ClassicPreloginReply,
    ClassicUserset,
)
if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


# Keep the existing logger name so operational log filters do not change.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicUsersetMixin:
    """Most Wanted userset discovery, roster projection and membership flow."""

    def _init_mw_usersets(self) -> None:
        """Initialize userset state owned by this behavior mixin."""
        self._usersets_lock = RLock()
        self._next_userset_id = 1
        self._usersets: dict[int, ClassicUserset] = {}
        self._next_wire_user_id = 1
        self._wire_user_ids: dict[int, int] = {}

    @staticmethod
    def _mw_userset_fields(
        userset: ClassicUserset,
        *,
        include_ident: bool = False,
        include_name: bool = False,
    ) -> tuple[tuple[str, object], ...]:
        members = userset.members or set()
        fields: list[tuple[str, object]] = [
            ("I", userset.userset_id),
            ("T", userset.type_value),
            ("SF", userset.sysflags),
            ("CF", userset.custflags),
            ("O", userset.owner_persona),
            ("S", userset.capacity),
            ("N", userset.name),
            ("D", userset.description),
            ("P", userset.params),
            ("C", len(members)),
        ]
        # Retail uses the short I/N projection for userset listings and
        # notifications.  IDENT is appended only to transaction replies;
        # NAME is additionally present on the original ucre reply.  Adding
        # both aliases to +ust changes the room-membership notification that
        # makes existing MW clients allocate a new peer transport edge.
        if include_ident:
            fields.append(("IDENT", userset.userset_id))
        if include_name:
            fields.append(("NAME", userset.name))
        return tuple(fields)

    def _mw_userset_frame(
        self,
        command: str,
        userset: ClassicUserset,
    ) -> bytes:
        is_create_reply = command == "ucre"
        is_join_reply = command == "ujoi"
        return _ea_frame().from_fields(
            command,
            self._mw_userset_fields(
                userset,
                include_ident=is_create_reply or is_join_reply,
                include_name=is_create_reply,
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_find_userset(self, fields: dict[str, str]) -> ClassicUserset | None:
        try:
            userset_id = int(fields.get("IDENT", fields.get("I", "0")) or 0)
        except (TypeError, ValueError):
            userset_id = 0
        name = fields.get("NAME", fields.get("N", "")).strip().casefold()
        with self._usersets_lock:
            if userset_id:
                userset = self._usersets.get(userset_id)
                if userset is not None:
                    return userset
            return next(
                (
                    userset
                    for userset in self._usersets.values()
                    if name and userset.name.casefold() == name
                ),
                None,
            )

    def _mw_wire_user_id(self, user_id: int) -> int:
        uid = int(user_id)
        with self._usersets_lock:
            wire_id = self._wire_user_ids.get(uid)
            if wire_id is None:
                wire_id = self._next_wire_user_id
                self._next_wire_user_id += 1
                self._wire_user_ids[uid] = wire_id
            return wire_id

    def _mw_remember_departed_room_personas(
        self,
        game: GameSession,
        *,
        viewer_ids: set[int],
        departed_ids: set[int],
    ) -> None:
        """Remember identities which a stock client may retain after teardown.

        MW can issue one final ``onln PERS=<old member>`` after it has already
        accepted the old game/userset deletion.  Keep this small, per-connection
        tombstone so that lookup can be redirected only when a fresh room has
        exactly one staged replacement member.  It must not affect ordinary
        global presence lookups or ambiguous multi-join rooms.
        """

        departed_personas = {
            int(user_id): str(
                game.participant_personas.get(int(user_id), "") or ""
            ).strip().casefold()
            for user_id in departed_ids
        }
        for viewer_id in viewer_ids:
            viewer = self._context_for_user(viewer_id)
            if viewer is None:
                continue
            viewer.mw_departed_room_personas.update(
                persona
                for user_id, persona in departed_personas.items()
                if user_id != int(viewer_id) and persona
            )

    def _mw_userset_for_game(self, game_id: int) -> ClassicUserset | None:
        with self._usersets_lock:
            return next(
                (
                    userset
                    for userset in self._usersets.values()
                    if userset.game_id == int(game_id)
                ),
                None,
            )

    @staticmethod
    def _mw_projected_address(
        context: ClassicPreloginContext,
        game: GameSession | None = None,
    ) -> str:
        """Return the same per-participant address used by the MW game object.

        Stock MW associates callback ``+usr``/presence rows with OPPO slots by
        address as well as by wire user id.  The relay advertises unique virtual
        race addresses, therefore every presence projection for a game member
        must use that same address.  Falling back to the real client address is
        valid only before a race address has been assigned.
        """
        address = str(context.client_address or "")
        identity = context.auth.identity
        if game is None or identity is None:
            return address
        projected = str(
            game.participant_race_addresses.get(identity.user_id, "") or ""
        ).strip()
        if not projected:
            return address
        # Be tolerant of a persisted host:port form, although current entries
        # are plain IPv4 strings.
        if projected.count(":") == 1:
            host, port = projected.rsplit(":", 1)
            if host and port.isdigit():
                projected = host
        return projected

    def _mw_presence_fields(
        self,
        context: ClassicPreloginContext,
        *,
        game: GameSession | None = None,
        display_game_id: int = 0,
    ) -> tuple[tuple[str, object], ...]:
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        persona = context.auth.persona or "Player"
        userset = (
            self._mw_userset_for_game(game.game_id)
            if game is not None
            else self._usersets.get(context.userset_id)
        )
        aux_text = self._participant_aux.get(user_id, "")
        if game is not None:
            aux_text = game.participant_aux.get(user_id, aux_text)
        projected_address = self._mw_projected_address(context, game)
        return (
            ("I", self._mw_wire_user_id(user_id)),
            ("N", persona),
            ("M", persona),
            ("F", "U"),
            ("A", projected_address),
            ("P", 425),
            ("S", ""),
            ("G", display_game_id),
            ("AT", ""),
            ("CL", 511),
            ("LV", 0),
            ("MD", 0),
            ("LA", projected_address),
            ("HW", 0),
            ("RP", 0),
            ("MA", ""),
            ("LO", "enUS"),
            ("X", aux_text),
            ("US", userset.name if userset is not None else ""),
            ("C", ""),
        )

    def _mw_who_frame(
        self,
        context: ClassicPreloginContext,
        *,
        game: GameSession | None = None,
        display_game_id: int = 0,
        flags: str | None = None,
    ) -> bytes:
        fields = list(
            self._mw_presence_fields(
                context,
                game=game,
                display_game_id=display_game_id,
            )
        )
        if flags is not None:
            fields = [
                (key, flags if key == "F" else value)
                for key, value in fields
            ]
        return _ea_frame().from_fields(
            "+who",
            fields,
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_usm_frame(
        self,
        context: ClassicPreloginContext,
        *,
        game: GameSession | None = None,
        display_game_id: int = 0,
        flags: str = "",
    ) -> bytes:
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        persona = context.auth.persona or "Player"
        aux_text = self._participant_aux.get(user_id, "")
        if game is not None:
            aux_text = game.participant_aux.get(user_id, aux_text)
        return _ea_frame().from_fields(
            "+usm",
            (
                ("I", self._mw_wire_user_id(user_id)),
                ("N", persona),
                ("F", flags),
                ("G", display_game_id),
                ("X", aux_text),
                ("S", 0),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_removed_usm_frame(self, user_id: int) -> bytes:
        """Build stock MW's short userset-member deletion marker."""
        return _ea_frame().from_fields(
            "+usm",
            (
                ("I", self._mw_wire_user_id(user_id)),
                ("S", 0),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_userset_roster_frames(
        self,
        userset: ClassicUserset,
        *,
        initial_user_id: int = 0,
    ) -> tuple[bytes, ...]:
        """Return a complete owner-first userset snapshot for stock MW."""
        game = (
            self.sessions.get_game(userset.game_id)
            if userset.game_id
            else None
        )
        frames: list[bytes] = [self._mw_userset_frame("+ust", userset)]
        members = (
            list(game.ordered_participants())
            if game is not None
            else sorted(
                userset.members or set(),
                key=lambda user_id: (user_id != userset.owner_id, user_id),
            )
        )
        for user_id in members:
            member = self._context_for_user(user_id)
            if member is None:
                continue
            display_game_id = 0
            if (
                user_id != int(initial_user_id)
                and game is not None
                and user_id in game.participants
            ):
                display_game_id = game.game_id
            frames.append(
                self._mw_usm_frame(
                    member,
                    game=(
                        game
                        if game is not None and user_id in game.participants
                        else None
                    ),
                    display_game_id=display_game_id,
                )
            )
        return tuple(frames)

    def _mw_log_userset_roster(
        self,
        userset: ClassicUserset,
        *,
        initial_user_id: int = 0,
    ) -> None:
        game = (
            self.sessions.get_game(userset.game_id)
            if userset.game_id
            else None
        )
        log.info(
            "[userset] sent +ust userset_id=%d name=%s owner=%s C=%d",
            userset.userset_id,
            userset.name,
            userset.owner_persona,
            len(userset.members or set()),
        )
        for user_id in sorted(
            userset.members or set(),
            key=lambda candidate: (candidate != userset.owner_id, candidate),
        ):
            member = self._context_for_user(user_id)
            if member is None:
                continue
            display_game_id = 0
            if (
                user_id != int(initial_user_id)
                and game is not None
                and user_id in game.participants
            ):
                display_game_id = game.game_id
            log.info(
                "[userset] sent +usm member=%s wire_id=%d G=%d",
                member.auth.persona or "Player",
                self._mw_wire_user_id(user_id),
                display_game_id,
            )

    def _dispatch_mw_userset_create(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        identity = context.auth.identity
        if identity is None or not context.auth.persona:
            return ClassicPreloginReply(
                (_ea_frame().short("userbadc"),),
                "not_authenticated",
                True,
            )
        try:
            capacity = max(1, min(8, int(fields.get("SIZE", "4") or 4)))
        except (TypeError, ValueError):
            capacity = 4
        with self._usersets_lock:
            previous = self._usersets.get(context.userset_id)
            if previous is not None and previous.owner_id == identity.user_id:
                self._usersets.pop(previous.userset_id, None)
            userset = ClassicUserset(
                userset_id=self._next_userset_id,
                owner_id=identity.user_id,
                owner_persona=context.auth.persona,
                name=(
                    fields.get("NAME", "").strip()
                    or f"014.{context.auth.persona}"
                ),
                capacity=capacity,
                type_value=fields.get("TYPE", "0") or "0",
                sysflags=fields.get("SYSFLAGS", "KV") or "KV",
                custflags=fields.get("CUSTFLAGS", "JKM-") or "JKM-",
                params=fields.get("PARAMS", ""),
                description=fields.get("DESC", ""),
            )
            self._usersets[userset.userset_id] = userset
            self._next_userset_id += 1
            context.userset_id = userset.userset_id
        response_command = (
            "+ust" if packet.command.isupper() or packet.reserved else "ucre"
        )
        created = self._mw_userset_frame(response_command, userset)
        if response_command == "+ust":
            frames = (created,)
        else:
            frames = (
                created,
                self._mw_who_frame(context),
                self._mw_userset_frame("+ust", userset),
                self._mw_usm_frame(context),
            )
        return ClassicPreloginReply(
            frames,
            "userset_created",
        )

    def _dispatch_mw_userset_update(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        identity = context.auth.identity
        with self._usersets_lock:
            userset = self._usersets.get(context.userset_id)
            if (
                identity is None
                or userset is None
                or userset.owner_id != identity.user_id
            ):
                return ClassicPreloginReply((), "userset_admin_rejected")
            if fields.get("NAME", "").strip():
                userset.name = fields["NAME"].strip()
            if "SIZE" in fields:
                try:
                    userset.capacity = max(
                        1,
                        min(8, int(fields["SIZE"] or userset.capacity)),
                    )
                except (TypeError, ValueError):
                    pass
            if "TYPE" in fields:
                userset.type_value = fields["TYPE"] or userset.type_value
            if "SYSFLAGS" in fields or "SF" in fields:
                userset.sysflags = (
                    fields.get("SYSFLAGS", fields.get("SF", userset.sysflags))
                    or userset.sysflags
                )
            if "CUSTFLAGS" in fields or "CF" in fields:
                userset.custflags = (
                    fields.get(
                        "CUSTFLAGS",
                        fields.get("CF", userset.custflags),
                    )
                    or userset.custflags
                )
            if "PARAMS" in fields:
                userset.params = fields["PARAMS"]
            if "DESC" in fields:
                userset.description = fields["DESC"]
        response_command = (
            "+ust" if packet.command.isupper() or packet.reserved else "uadm"
        )
        updated = self._mw_userset_frame(response_command, userset)
        game = (
            self.sessions.get_game(userset.game_id)
            if userset.game_id
            else None
        )
        if response_command == "+ust" or game is None:
            frames = (updated,)
        else:
            self._send_users(
                set(game.participants),
                (self._mw_userset_frame("+ust", userset),),
                exclude=identity.user_id,
            )
            waiting_members: list[bytes] = []
            for user_id in sorted(userset.members or set()):
                if user_id in game.participants:
                    continue
                member = self._context_for_user(user_id)
                if member is not None:
                    waiting_members.append(
                        self._mw_usm_frame(member, game=game)
                    )
            frame_list: list[bytes] = [
                updated,
                self._mw_who_frame(
                    context,
                    game=game,
                    display_game_id=game.game_id,
                ),
                self._mw_userset_frame("+ust", userset),
                *waiting_members,
                self._mw_usm_frame(
                    context,
                    game=game,
                    display_game_id=game.game_id,
                ),
                _ea_frame().from_fields(
                    "+mgm",
                    self._mw_game_fields(game),
                    separator="\t",
                    final_separator=False,
                ).encode(),
            ]
            if waiting_members:
                frame_list.append(
                    _ea_frame().from_fields(
                        "+sst",
                        (
                            ("UIL", len(waiting_members)),
                            ("UIR", 0),
                            ("UIG", len(game.participants)),
                            ("GIP", 0),
                            ("GCR", 0),
                            ("GCM", 2),
                        ),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
            frames = tuple(frame_list)
        return ClassicPreloginReply(
            frames,
            "userset_updated",
        )

    def _dispatch_mw_userset_search(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        identity = context.auth.identity
        if identity is None:
            return ClassicPreloginReply((), "not_authenticated")
        current_game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        with self._connections_lock:
            postrace_room_view = bool(
                current_game is not None
                and context.mw_postrace_return_pending
                and current_game.game_id
                in self._mw_postrace_room_view_games
            )
        postrace_userset: ClassicUserset | None = None
        if postrace_room_view and current_game is not None:
            postrace_userset = self._mw_find_userset(fields)
            if postrace_userset is None and not any(
                fields.get(key, "").strip()
                for key in ("NAME", "N", "IDENT", "I")
            ):
                with self._usersets_lock:
                    postrace_userset = self._usersets.get(
                        context.userset_id
                    )
            if (
                postrace_userset is None
                or postrace_userset.game_id != current_game.game_id
                or identity.user_id
                not in (postrace_userset.members or set())
            ):
                postrace_userset = None
            context.mw_deferred_usea_game_id = 0
        if postrace_userset is not None:
            # In stock 3playerroom&race the first guest returns at
            # 357.947s while the owner does not recreate until 382.440s.
            # The active userset search at 358.162s is still answered
            # immediately with COUNT=1/+uss C=3.  The following premature
            # gjoi is rejected separately with the short "ugam" marker.
            usersets = [postrace_userset]
            log.info(
                "%s served active post-race usea before owner "
                "replacement: user=%d old_game=%d userset=%d",
                self.profile.game_id,
                identity.user_id,
                current_game.game_id,
                postrace_userset.userset_id,
            )
        else:
            with self._usersets_lock:
                usersets = [
                    userset
                    for userset in self._usersets.values()
                    if userset.game_id
                    and (
                        (game := self.sessions.get_game(userset.game_id))
                        is not None
                    )
                    and game.state is SessionState.OPEN
                    and game.visible_to(identity.user_id)
                ]
        search = _ea_frame().from_fields(
            "usea",
            (("COUNT", len(usersets)),),
            separator="\t",
            final_separator=False,
        ).encode()
        listings = tuple(
            self._mw_userset_frame("+uss", userset)
            for userset in usersets
        )
        log.info(
            "[userset] usea response user=%s count=%d usersets=%s",
            context.auth.persona or "Player",
            len(usersets),
            ",".join(str(userset.userset_id) for userset in usersets),
        )
        return ClassicPreloginReply(
            (search, *listings),
            (
                "userset_search_postrace_active"
                if postrace_userset is not None
                else "userset_search"
            ),
        )

    def _dispatch_mw_userset_join(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        identity = context.auth.identity
        userset = self._mw_find_userset(fields)
        log.info(
            "[userset] ujoi request user=%s requested_id=%s requested_name=%s",
            context.auth.persona or "Player",
            fields.get("IDENT", fields.get("I", "")),
            fields.get("NAME", fields.get("N", "")),
        )
        if identity is None or userset is None:
            rejected = _ea_frame().from_fields(
                "ujoi",
                (
                    ("IDENT", 0),
                    ("NAME", fields.get("NAME", "")),
                    ("C", 0),
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            return ClassicPreloginReply((rejected,), "userset_join_rejected")
        with self._usersets_lock:
            members = userset.members
            if members is None:
                members = set()
                userset.members = members
            if (
                identity.user_id not in members
                and len(members) >= userset.capacity
            ):
                return ClassicPreloginReply((), "userset_full")
            members.add(identity.user_id)
            context.userset_id = userset.userset_id
            context.mw_userset_staged_game_id = userset.game_id
        log.info(
            "[userset] active userset assigned user=%s userset_id=%d owner=%s",
            context.auth.persona or "Player",
            userset.userset_id,
            userset.owner_persona,
        )
        # Retail only acknowledges the userset transaction here. Existing
        # peers receive +ust/+usm after the member completes gjoi; publishing
        # the member during ujoi updates the UI but does not make MW create
        # the new peer-to-peer transport edge.
        return ClassicPreloginReply(
            (self._mw_userset_frame("ujoi", userset),),
            "userset_joined",
        )

    def _dispatch_mw_userset_leave(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
        command: str,
    ) -> ClassicPreloginReply:
        identity = context.auth.identity
        userset = self._mw_find_userset(fields)
        if userset is None and context.userset_id:
            with self._usersets_lock:
                userset = self._usersets.get(context.userset_id)
        userset_id = (
            userset.userset_id
            if userset is not None
            else context.userset_id
        )
        userset_game_id = (
            userset.game_id
            if userset is not None and userset.game_id
            else userset_id
        )
        userset_members = set(userset.members or set()) if userset else set()
        owner_deleting_userset = bool(
            command == "udel"
            and userset is not None
            and identity is not None
            and userset.owner_id == identity.user_id
        )
        current_game = (
            self.sessions.get_game(userset.game_id)
            if userset is not None and userset.game_id
            else None
        )
        detached_active_guest = False
        if (
            command == "ulea"
            and identity is not None
            and current_game is not None
            and current_game.owner_id != identity.user_id
            and identity.user_id in current_game.participants
            and context.lobby_game_id == current_game.game_id
            and self._preserve_transport_for_guest_exit(
                current_game,
                identity.user_id,
            )
        ):
            # A guest can leave the post-race room while its owner is
            # still driving.  ULEA is then the only explicit leave sent
            # by the client: detach just this participant, while keeping
            # the owner's shared race transport registered.
            self._mw_remember_departed_room_personas(
                current_game,
                viewer_ids=set(current_game.participants)
                - {identity.user_id},
                departed_ids={identity.user_id},
            )
            detached_active_guest = self.sessions.leave_game(
                current_game.game_id,
                identity.user_id,
            )
            if detached_active_guest:
                self._sync_preserved_game_transport(current_game)
                context.lobby_game_id = 0
                log.info(
                    "%s detached active guest before ulea: game=%d "
                    "actor=%d owner=%d remaining=%d transport_retired=0",
                    self.profile.game_id,
                    current_game.game_id,
                    identity.user_id,
                    current_game.owner_id,
                    len(current_game.participants),
                )
        remaining = (
            self.sessions.get_game(userset.game_id)
            if userset is not None and userset.game_id
            else None
        )
        who_before = self._mw_who_frame(context)
        member_before = self._mw_usm_frame(context)
        removed_member = (
            self._mw_removed_usm_frame(identity.user_id)
            if identity is not None
            else member_before
        )
        managed = (
            _ea_frame().from_fields(
                "+mgm",
                self._mw_game_fields(remaining),
                separator="\t",
                final_separator=False,
            ).encode()
            if remaining is not None
            else None
        )
        updated_userset: bytes | None = None
        deleted_userset: bytes | None = None
        with self._usersets_lock:
            if userset is not None and identity is not None:
                if owner_deleting_userset:
                    self._usersets.pop(userset.userset_id, None)
                    deleted_userset = _ea_frame().from_fields(
                        "+ust",
                        (("I", userset.userset_id),),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                else:
                    if userset.members is not None:
                        userset.members.discard(identity.user_id)
                    updated_userset = self._mw_userset_frame(
                        "+ust",
                        userset,
                    )
            context.userset_id = 0
            context.mw_join_pending_game_id = 0
            context.mw_postrace_return_pending = False
            context.mw_postrace_snapshot_game_id = 0
            context.mw_postrace_room_view_game_id = 0
            context.mw_deferred_usea_game_id = 0
            context.mw_deferred_gjoi_game_id = 0
        deleted_game = _ea_frame().from_fields(
            "+mgm",
            (("IDENT", userset_game_id),),
            separator="\t",
            final_separator=False,
        ).encode()
        if owner_deleting_userset:
            if remaining is not None:
                self._mw_remember_departed_room_personas(
                    remaining,
                    viewer_ids=set(remaining.participants),
                    departed_ids=set(remaining.participants),
                )
                self._retire_game_transport(remaining)
                self.sessions.close_game(remaining.game_id)
            for user_id in userset_members:
                peer = self._context_for_user(user_id)
                if peer is None:
                    continue
                if peer.userset_id == userset_id:
                    peer.userset_id = 0
                if peer.lobby_game_id == userset_game_id:
                    peer.lobby_game_id = 0
                peer.mw_join_pending_game_id = 0
                peer.mw_postrace_return_pending = False
                peer.mw_postrace_snapshot_game_id = 0
                peer.mw_postrace_room_view_game_id = 0
                peer.mw_deferred_usea_game_id = 0
                peer.mw_deferred_gjoi_game_id = 0
        ack = _ea_frame().from_fields(
            command,
            (),
            reserved=packet.reserved,
            separator="\t",
            final_separator=False,
        ).encode()
        who_after = self._mw_who_frame(context)
        if identity is not None and userset is not None:
            if owner_deleting_userset:
                reset_status = _ea_frame().from_fields(
                    "+sst",
                    (
                        ("UIL", len(userset_members)),
                        ("UIR", 0),
                        ("UIG", 0),
                        ("GIP", 0),
                        ("GCR", 0),
                        ("GCM", 1),
                    ),
                    separator="\t",
                    final_separator=False,
                ).encode()
                for user_id in userset_members:
                    if user_id == identity.user_id:
                        continue
                    peer = self._context_for_user(user_id)
                    if peer is None or peer.send_wire is None:
                        continue
                    peer.send_wire(self._mw_who_frame(peer))
                    if deleted_userset is not None:
                        peer.send_wire(deleted_userset)
                    if remaining is not None:
                        peer.send_wire(deleted_game)
                    peer.send_wire(reset_status)
                log.info(
                    "%s cascaded owner userset deletion: userset=%d "
                    "game=%d members=%d",
                    self.profile.game_id,
                    userset_id,
                    userset_game_id,
                    len(userset_members),
                )
            else:
                # GLEA already projected the departed member once with G=0.
                # Stock ULEA then publishes the updated userset count, a short
                # `I=<wire id>, S=0` deletion marker (no stale N field), and
                # finally the compacted game record.  Repeating the full member
                # row here leaves its old persona cached in the retail roster.
                peer_frames = tuple(
                    frame
                    for frame in (updated_userset, removed_member, managed)
                    if frame is not None
                )
                if peer_frames:
                    self._send_users(
                        set(userset.members or set()),
                        peer_frames,
                        exclude=identity.user_id,
                    )
        if command == "udel":
            frames = tuple(
                frame
                for frame in (
                    ack,
                    who_after,
                    deleted_userset,
                    deleted_game,
                )
                if frame is not None
            )
            return ClassicPreloginReply(frames, "userset_deleted")
        # Official MW ULEA sends +who/+mgm, the 12-byte ULEA ACK, then a
        # final +who with an empty userset.  +usm is a peer notification;
        # echoing it to the leaving client creates a contradictory local
        # member transition (and, in the active-race path, used to pair a
        # userset C=1 with a stale game COUNT=2).
        frames = [who_before]
        if managed is not None:
            frames.append(managed)
        frames.extend((ack, who_after))
        return ClassicPreloginReply(
            tuple(frames),
            "userset_left",
        )


__all__ = ["ClassicUsersetMixin"]
