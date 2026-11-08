"""Xbox metadata projections around the shared Classic room lifecycle."""
from __future__ import annotations

import logging
import time

from classic.protocols.frame import ClassicEAFrame, ClassicOpaqueValue
from classic.protocols.prelogin import ClassicPreloginReply, ClassicPreloginService

log = logging.getLogger(__name__)


def _opaque(value):
    return ClassicOpaqueValue(str(value).encode("latin-1"))


class XboxPrelogin(ClassicPreloginService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Xbox's lobby flow uses participant IDs from 2. In a two-client
        # local test, starting at 1 left the host waiting despite UDP traffic.
        self._next_wire_user_id = 2

    def _dispatch_selection(self, context, fields):
        reply = super()._dispatch_selection(context, fields)
        if not reply.frames or context.auth.identity is None:
            return reply
        game = self.sessions.get_game(context.lobby_game_id) if context.lobby_game_id else None
        who = ClassicEAFrame.from_fields(
            "+who", self._mw_presence_fields(context, game=game),
        ).encode()
        return ClassicPreloginReply((reply.frames[0], who, *reply.frames[1:]),
                                    reply.reason, reply.close_connection,
                                    after_send=reply.after_send)

    def _dispatch_address(self, context, fields):
        # The Xbox LAN implementation advertises the address observed by the
        # lobby socket; the client's addr request can contain a different local
        # address. MADDR carries the Xbox peer address separately.
        peer_address = context.auth.client_ip or context.client_address
        reply = super()._dispatch_address(context, fields)
        context.client_address = peer_address
        return reply

    def _mw_userset_frame(self, command, userset):
        return ClassicEAFrame.from_fields(command, (
            ("I", userset.userset_id), ("T", userset.type_value),
            ("SF", userset.sysflags), ("CF", userset.custflags),
            ("O", userset.owner_persona), ("S", userset.capacity),
            ("C", len(userset.members or ())), ("N", userset.name),
            ("D", userset.description), ("P", userset.params),
        )).encode()

    def _dispatch_mw_userset_create(self, packet, context, fields):
        reply = super()._dispatch_mw_userset_create(packet, context, fields)
        if reply.reason != "userset_created" or not reply.frames:
            return reply
        # The Xbox creator immediately receives its user and room-member
        # records. PC's +who/+ust sequence is a different projection.
        return ClassicPreloginReply(
            (reply.frames[0], self._xbox_user_frame(context), self._mw_usm_frame(context)),
            reply.reason, reply.close_connection, after_send=reply.after_send,
        )

    def _dispatch_mw_userset_update(self, packet, context, fields):
        # Room flags and game flags are distinct on Xbox. The PC adapter
        # intentionally mirrors the room's CUSTFLAGS into the game object;
        # sending those letters in Xbox's numeric game field breaks parity.
        identity = context.auth.identity
        with self._usersets_lock:
            room = self._usersets.get(context.userset_id)
            if identity is None or room is None or room.owner_id != identity.user_id:
                return ClassicPreloginReply((), "userset_admin_rejected")
            if fields.get("NAME", "").strip():
                room.name = fields["NAME"].strip()
            if "SIZE" in fields:
                try:
                    room.capacity = max(1, min(8, int(fields["SIZE"])))
                except (TypeError, ValueError):
                    pass
            for attr, keys in (("type_value", ("TYPE",)), ("sysflags", ("SYSFLAGS", "SF")),
                               ("custflags", ("CUSTFLAGS", "CF")),
                               ("params", ("PARAMS",)), ("description", ("DESC",))):
                for key in keys:
                    if key in fields:
                        setattr(room, attr, fields[key])
                        break
        command = "+ust" if packet.command.isupper() or packet.reserved else "uadm"
        return ClassicPreloginReply((self._mw_userset_frame(command, room),), "userset_updated")

    def _dispatch_auxiliary(self, context, fields):
        # Xbox peers consume both lobby presence (+usr) and room membership
        # (+usm) when the client's connection state changes. The PC path has
        # different timing and does not publish the Xbox +usr update.
        text = fields.get("TEXT", "")
        ack = ClassicEAFrame.from_fields("auxi", (("TEXT", text),)).encode()
        identity = context.auth.identity
        if identity is None:
            return ClassicPreloginReply((ack,), "auxiliary")
        self._participant_aux[identity.user_id] = text
        game = self.sessions.get_game(context.lobby_game_id) if context.lobby_game_id else None
        if game is not None:
            game.participant_aux[identity.user_id] = text
        with self._usersets_lock:
            userset = self._usersets.get(context.userset_id)
            members = set(userset.members or ()) if userset else set()
        if identity.user_id not in members:
            return ClassicPreloginReply((ack,), "auxiliary")
        presence = self._xbox_user_frame(context, game)
        member = self._mw_usm_frame(context, game=game)
        self._send_users(members - {identity.user_id}, (presence, member))
        return ClassicPreloginReply((ack, presence, member), "auxiliary")

    def _dispatch_message(self, packet, context, fields, mw_callback):
        attr = fields.get("ATTR", "").upper()
        if attr not in {"EGS", "EGT"}:
            return super()._dispatch_message(packet, context, fields, mw_callback)
        ack = ClassicEAFrame.from_fields("mesg", ()).encode()
        identity = context.auth.identity
        if identity is not None:
            game = self.sessions.get_game(context.lobby_game_id) if context.lobby_game_id else None
            if game is not None:
                msg = self._message_frame(fields.get("TEXT", ""),
                                          context.auth.persona or "Player", flag=attr)
                self._send_users(set(game.participants), (msg,), exclude=identity.user_id)
        return ClassicPreloginReply((ack,), "xbox_message")

    def _xbox_user_frame(self, context, game=None):
        return ClassicEAFrame.from_fields(
            "+usr",
            self._mw_presence_fields(context, game=game, display_game_id=context.lobby_game_id),
        ).encode()

    def _dispatch_mw_userset_join(self, packet, context, fields):
        reply = super()._dispatch_mw_userset_join(packet, context, fields)
        if reply.reason != "userset_joined" or context.auth.identity is None:
            return reply
        with self._usersets_lock:
            userset = self._usersets.get(context.userset_id)
            members = set(userset.members or ()) if userset is not None else set()
        game = self.sessions.get_game(userset.game_id) if userset and userset.game_id else None
        guest_id = context.auth.identity.user_id
        peers = [self._context_for_user(uid) for uid in members if uid != guest_id]
        peers = [peer for peer in peers if peer is not None]
        guest_record = self._xbox_user_frame(context, game)
        self._send_users(members - {guest_id}, (guest_record, self._mw_usm_frame(context, game=game)))
        frames = list(reply.frames)
        for peer in peers:
            frames.append(self._xbox_user_frame(peer, game))
        frames.append(guest_record)
        frames.append(self._mw_usm_frame(context, game=game))
        return ClassicPreloginReply(tuple(frames), reply.reason, reply.close_connection,
                                    after_send=reply.after_send)

    def _dispatch_game_join(self, context, fields):
        # Xbox first joins a userset. Its subsequent game NAME can use the
        # local gamertag while the authenticated account has another persona.
        # Resolve against the previously accepted userset, not an arbitrary
        # client-provided game. This also handles room/game IDs diverging.
        staged = context.mw_userset_staged_game_id
        identity = context.auth.identity
        with self._usersets_lock:
            userset = self._usersets.get(context.userset_id)
            member = bool(
                identity is not None and userset is not None
                and userset.game_id == staged
                and identity.user_id in (userset.members or set())
            )
        game = self.sessions.get_game(staged) if staged and member else None
        empty = ClassicEAFrame.from_fields("gjoi", (), final_separator=False).encode()
        if identity is None or game is None or not context.auth.persona:
            return ClassicPreloginReply((empty,), "game_join_rejected")
        if not self.sessions.join_game(
            game.game_id, identity.user_id, fields.get("PASS", ""),
            persona=context.auth.persona, address=context.client_address,
        ):
            return ClassicPreloginReply((empty,), "game_join_rejected")
        previous = context.lobby_game_id
        if previous and previous != game.game_id:
            self.sessions.leave_game(previous, identity.user_id)
        context.lobby_game_id = game.game_id
        context.mw_userset_staged_game_id = 0
        aux = self._participant_aux.get(identity.user_id, "")
        if aux:
            game.participant_aux[identity.user_id] = aux
        for uid in game.ordered_participants():
            if uid == identity.user_id:
                continue
            peer = self._context_for_user(uid)
            if peer is not None and peer.send_wire is not None:
                peer.send_wire(ClassicEAFrame.from_fields(
                    "+mgm", self._mw_game_fields(game, viewer_id=uid),
                    separator="\n", final_separator=True,
                ).encode())
                # The original LAN DLL also publishes the updated userset and
                # member after GJOI. Without this room update, the Xbox host
                # can connect to the guest and start a race while its room UI
                # still shows only the host.
                if userset is not None:
                    peer.send_wire(self._mw_userset_frame("+ust", userset))
                    peer.send_wire(self._mw_usm_frame(context, game=game))
                    peer.send_wire(ClassicEAFrame.from_fields(
                        "+mgm", self._mw_game_fields(game, viewer_id=uid),
                    ).encode())
        game_fields = self._mw_game_fields(game, viewer_id=identity.user_id)
        joined = ClassicEAFrame.from_fields(
            "gjoi", game_fields,
            separator="\n", final_separator=True,
        ).encode()
        who = ClassicEAFrame.from_fields(
            "+who", self._mw_presence_fields(context, game=game),
            separator="\n", final_separator=True,
        ).encode()
        log.info("Xbox game join: game=%s participants=%s session_bytes=%s",
                 game.game_id, len(game.participants), len(getattr(game, "xbox_session", "")))
        return ClassicPreloginReply((joined, who), "game_joined")

    def dispatch(self, packet, context):
        command = getattr(packet, "command", "").casefold()
        if command in {"auth", "pers"}:
            fields = packet.fields()
            if fields.get("MADDR"):
                context.auth.xbox_maddr = fields["MADDR"]
        if command == "gset":
            fields = packet.fields()
            identity = context.auth.identity
            game = self.sessions.get_game(context.lobby_game_id) if context.lobby_game_id else None
            if identity is not None and game is not None and identity.user_id == game.owner_id:
                if "SESS" in fields:
                    game.xbox_session = fields["SESS"]
                if "PLATPARAMS" in fields:
                    game.xbox_platform_params = fields["PLATPARAMS"]
        return super().dispatch(packet, context)

    def _mw_presence_fields(self, context, **kwargs):
        # Xbox's presence record uses a different one-letter profile from PC.
        # Its G value must reflect the actual joined game after GJOI, including
        # in the local +who record that the PC adapter deliberately stages at 0.
        identity = context.auth.identity
        uid = identity.user_id if identity is not None else 0
        game = kwargs.get("game")
        address = self._mw_projected_address(context, game)
        aux = self._participant_aux.get(uid, "")
        if game is not None:
            aux = game.participant_aux.get(uid, aux)
        return (
            ("I", self._mw_wire_user_id(uid)), ("M", context.auth.persona or "Player"),
            ("N", context.auth.persona or "Player"),
            ("MA", _opaque(getattr(context.auth, "xbox_maddr", ""))),
            ("A", address), ("LA", address), ("G", context.lobby_game_id),
            ("P", 1), ("F", "U"), ("LO", "enUS"), ("PRES", 1), ("X", aux),
        )

    def _mw_usm_frame(self, context, *, game=None, display_game_id=0, flags=""):
        identity = context.auth.identity
        uid = identity.user_id if identity else 0
        aux = self._participant_aux.get(uid, "")
        if game is not None:
            aux = game.participant_aux.get(uid, aux)
        return ClassicEAFrame.from_fields("+usm", (
            ("S", 0), ("I", self._mw_wire_user_id(uid)),
            ("N", context.auth.persona or "Player"), ("F", "U"),
            ("G", context.lobby_game_id), ("X", aux),
        )).encode()

    def _mw_game_fields(self, game, **kwargs):
        # Xbox expects its own game-object field sequence and the host's
        # original SYSFLAGS, including its opaque XSession data. PC's stock
        # projection uses different defaults and ordering.
        viewer_id = int(kwargs.get("viewer_id", 0) or game.owner_id)
        viewer = self._context_for_user(viewer_id)
        userset = self._mw_userset_for_game(game.game_id)
        members = game.ordered_participants()
        session = getattr(game, "xbox_session", "")
        fields = [
            ("IDENT", game.game_id), ("NAME", game.name),
            ("SELF", viewer.auth.persona if viewer else game.host_persona),
            ("HOST", game.host_persona), ("PARAMS", game.params),
            ("PLATPARAMS", _opaque(getattr(game, "xbox_platform_params", ""))),
            ("ROOM", userset.userset_id if userset else game.room_id),
            ("CUSTFLAGS", game.custflags), ("SYSFLAGS", game.sysflags),
            ("COUNT", len(members)), ("PRIV", 0), ("MINSIZE", game.min_players),
            ("MAXSIZE", game.capacity), ("NUMPART", 1), ("PARTSIZE0", game.capacity),
            ("PARTPARAMS0", ""), ("SEED", game.game_id),
            ("WHEN", time.strftime("%Y.%m.%d-%H:%M:%S", time.localtime(game.created_at))),
            ("AUTH", ""), ("SESS", _opaque(session)),
        ]
        for index, user_id in enumerate(members):
            participant = self._context_for_user(user_id)
            maddr = getattr(participant.auth, "xbox_maddr", "") if participant else ""
            address = game.participant_addresses.get(user_id, "127.0.0.1")
            fields.extend((
                (f"OPPO{index}", game.participant_personas.get(user_id, "Player")),
                (f"OPID{index}", self._mw_wire_user_id(user_id)),
                (f"ADDR{index}", address), (f"LADDR{index}", address),
                (f"MADDR{index}", _opaque(maddr)), (f"OPPART{index}", 0),
                (f"OPPARAM{index}", ""), (f"OPFLAG{index}", 0), (f"PRES{index}", 0),
            ))
        return tuple(fields)
