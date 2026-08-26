"""Central command router for the shared Classic lobby service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame
    from classic.protocols.stream import ClassicEAShortFrame


def _wire_types():
    """Load wire types lazily so lobby modules remain independently importable."""
    from classic.protocols.frame import ClassicEAFrame
    from classic.protocols.stream import ClassicEAShortFrame

    return ClassicEAFrame, ClassicEAShortFrame


class ClassicRouterMixin:
    """Route decoded EA frames to title- and domain-specific handlers."""

    def dispatch(
        self,
        packet: ClassicEAFrame | ClassicEAShortFrame,
        context: ClassicPreloginContext,
    ) -> ClassicPreloginReply:
        ClassicEAFrame, ClassicEAShortFrame = _wire_types()
        if isinstance(packet, ClassicEAShortFrame):
            if packet.tag.casefold() in {"newsbadc", "userbadc"}:
                return ClassicPreloginReply((), "short_ack")
            return ClassicPreloginReply((), "unsupported_short_frame")

        command = packet.command.casefold()
        fields = self._fields(packet)
        request_context = context
        mw_callback = False
        if self._is_most_wanted and (packet.command.isupper() or packet.reserved):
            callback_context = self._mw_context_for_callback(fields)
            if callback_context is not None:
                context = callback_context
                mw_callback = context is not request_context

        if command == "addr":
            return self._dispatch_address(context, fields)

        if command == "skey":
            return ClassicPreloginReply((self._signed_ack("skey"),), "server_key")

        if command == "news":
            # NEWS advertises transport endpoints, so classify the viewer from
            # the server-captured socket peer in the auth context.  ADDR is
            # client-declared game state and must not switch a public peer to
            # LOCAL_ADVERTISE_HOST.
            return ClassicPreloginReply(
                (self._news_frame(client_ip=context.auth.client_ip),), "news"
            )

        if command == "sele":
            return self._dispatch_selection(context, fields)
        if command in {"auth", "acct", "pers", "cper", "dper"}:
            return self._dispatch_authentication(packet, context, command)

        if self._is_underground2 and command == "move":
            return self._dispatch_u2_move(context, fields)

        if command == "user":
            return self._dispatch_user(context)

        if command == "auxi":
            reply = self._dispatch_auxiliary(context, fields)
            if self._is_most_wanted and mw_callback:
                callback_frames: list[bytes] = [self._mw_callback_ack(packet)]
                game = (
                    self.sessions.get_game(context.lobby_game_id)
                    if context.lobby_game_id
                    else None
                )
                if game is not None:
                    callback_frames.append(self._mw_usr_frame(context, game))

                # ``reply`` was built for the authenticated main connection.
                # Its leading AUXI ACK belongs to neither socket here: the
                # callback gets the reserved-token ACK above. Forward only the
                # remaining room snapshot to the user's main lobby stream.
                main_frames = list(reply.frames)
                if main_frames:
                    try:
                        first, remainder = ClassicEAFrame.decode_one(main_frames[0])
                    except (TypeError, ValueError):
                        first = None
                        remainder = b""
                    if (
                        first is not None
                        and not remainder
                        and first.command.casefold() == "auxi"
                    ):
                        main_frames = main_frames[1:]

                original_after_send = reply.after_send

                def finish_auxiliary_callback() -> None:
                    if context.send_wire is not None:
                        for wire in main_frames:
                            if not context.send_wire(wire):
                                break
                    if original_after_send is not None:
                        original_after_send()

                return ClassicPreloginReply(
                    tuple(callback_frames),
                    "auxiliary_callback",
                    reply.close_connection,
                    after_send=(
                        finish_auxiliary_callback
                        if main_frames or original_after_send is not None
                        else None
                    ),
                )
            return reply
        if command == "snap":
            return ClassicPreloginReply(
                self._snap_frames(fields, context),
                "stats_snapshot",
            )

        if self._is_most_wanted and command == "ucre":
            return self._dispatch_mw_userset_create(packet, context, fields)
        if self._is_most_wanted and command == "uadm":
            return self._dispatch_mw_userset_update(packet, context, fields)
        if self._is_most_wanted and command == "usea":
            return self._dispatch_mw_userset_search(packet, context, fields)
        if self._is_most_wanted and command == "ujoi":
            return self._dispatch_mw_userset_join(packet, context, fields)
        if command == "gsea":
            return self._dispatch_game_search(context, fields)

        if command == "gcre":
            return self._dispatch_game_create(context, fields)

        if command == "gjoi":
            reply = self._dispatch_game_join(context, fields)
            if self._is_most_wanted and mw_callback:
                game = (
                    self.sessions.get_game(context.lobby_game_id)
                    if context.lobby_game_id
                    else None
                )
                if game is None:
                    return reply

                # Retail's uppercase callback does not return a plain GJOI
                # acknowledgement.  It reuses the callback token as the first
                # four bytes of a complete game-object frame, then sends the
                # caller's +usr projection and the compact +gam projection.
                joined_wire = (
                    reply.frames[0]
                    if reply.frames
                    else ClassicEAFrame.from_fields(
                        "gjoi",
                        self._mw_game_fields(
                            game,
                            viewer_id=self._user_id(context),
                        ),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
                callback_command = (
                    int(packet.reserved).to_bytes(4, "big")
                    if packet.reserved
                    else packet.command.encode("latin-1")
                )
                callback_frames = (
                    callback_command + joined_wire[4:],
                    self._mw_usr_frame(context, game),
                    self._mw_gam_frame(game),
                )

                # Normally the lowercase main-stream GJOI arrives first, so
                # this callback is a repeat and nothing is forwarded.  If the
                # callback wins the race, preserve the same local G=0 frames
                # by delivering the main reply after the callback write.
                main_frames = (
                    list(reply.frames)
                    if reply.reason == "game_joined"
                    else []
                )
                original_after_send = reply.after_send

                def finish_gjoi_callback() -> None:
                    if context.send_wire is not None:
                        for wire in main_frames:
                            if not context.send_wire(wire):
                                break
                    if original_after_send is not None:
                        original_after_send()

                return ClassicPreloginReply(
                    callback_frames,
                    "game_joined_callback",
                    reply.close_connection,
                    after_send=(
                        finish_gjoi_callback
                        if main_frames or original_after_send is not None
                        else None
                    ),
                )
            return reply

        if command == "mesg":
            return self._dispatch_message(packet, context, fields, mw_callback)

        if command in {"glea", "gdel"}:
            return self._dispatch_game_leave(packet, context, command)

        if self._is_most_wanted and command in {"ulea", "udel"}:
            return self._dispatch_mw_userset_leave(
                packet, context, fields, command
            )

        # Stock MW has no confirmed host-side Kick command/button.  Keep the
        # classic server extension out of the MW protocol path; U2 retains its
        # previously implemented room-owner kick behavior.
        if (not self._is_most_wanted) and (
            command == "kick"
            or (command == "gset" and fields.get("KICK", "").strip())
        ):
            return self._dispatch_u2_kick(
                packet, context, fields, command
            )

        if command in {"gset", "term"}:
            return self._dispatch_game_settings(packet, context, fields, command)

        if command == "onln":
            return self._dispatch_online(context, fields)

        if self._is_underground2 and command == "rept":
            return self._dispatch_u2_feedback(context, fields)
        if self._is_most_wanted and command == "rept":
            return self._dispatch_mw_feedback(context, fields)

        if self._is_underground2 and command == "rank":
            return self._dispatch_u2_rank(packet, context, fields)
        if self._is_most_wanted and command == "rank":
            return self._dispatch_mw_rank(packet, context, fields)
        if command == "gsta":
            return self._dispatch_game_start(packet, context, mw_callback)

        if command == "*con":
            return ClassicPreloginReply(
                (
                    ClassicEAFrame.from_fields(
                        "*con",
                        (),
                        separator="\t",
                        final_separator=False,
                    ).encode(),
                ),
                "connection_ack",
            )

        if command in {"@alv", "@cnt", "~png"}:
            return ClassicPreloginReply((), "heartbeat")

        return ClassicPreloginReply((), "unsupported_command")
