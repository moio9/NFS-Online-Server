"""Classic lobby message routing and MW race-control messages.

The wire-level ``mesg`` handler remains available through
``ClassicPreloginService`` but no longer lives in the main protocol router.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


class ClassicMessageMixin:
    """Route lobby messages without changing callback semantics."""

    def _dispatch_message(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        fields: dict[str, str],
        mw_callback: bool,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        attr = (
            fields.get("ATTR", "")
            or fields.get("F", "")
            or fields.get("FLAGS", "")
        ).strip()
        text = fields.get("TEXT", fields.get("T", "")).strip()
        if self._is_most_wanted and not attr:
            if text == "42":
                attr = "EGS"
            elif "TIME%3d" in text or "TIME=" in text:
                attr = "EGT"
        ack = (
            self._mw_callback_ack(packet)
            if self._is_most_wanted
            else ClassicEAFrame.from_fields(
                "mesg",
                (("ATTR", attr),) if attr else (),
                separator="\t",
                final_separator=False,
            ).encode()
        )
        if identity is None:
            return ClassicPreloginReply((ack,), "chat_not_authenticated")
        private_name = fields.get("PRIV", "").strip()
        sender_name = context.auth.persona or "Player"
        if private_name:
            target = next(
                (
                    candidate
                    for candidate in self._connections.values()
                    if candidate.auth.persona.casefold() == private_name.casefold()
                ),
                None,
            )
            invite_flag = "EPQ" if attr.upper() == "EPQ" else ""
            if (
                invite_flag
                and target is not None
                and context.lobby_game_id
            ):
                game = self.sessions.get_game(context.lobby_game_id)
                target_id = self._user_id(target)
                if (
                    game is not None
                    and identity.user_id == game.owner_id
                    and target_id
                ):
                    self.sessions.invite_user(game.game_id, target_id)
            own = self._message_frame(
                text,
                f'"To {private_name}"',
                attr="" if invite_flag else attr,
                flag=invite_flag or "PU",
            )
            if target is not None and target.send_wire is not None:
                target.send_wire(
                    self._message_frame(
                        text,
                        sender_name,
                        attr="" if invite_flag else attr,
                        flag=invite_flag or "P",
                    )
                )
            return ClassicPreloginReply(
                (ack, own),
                "game_invite" if invite_flag else "private_message",
            )
        game = (
            self.sessions.get_game(context.lobby_game_id)
            if context.lobby_game_id
            else None
        )
        mw_race_message = self._is_most_wanted and attr.upper() in {"EGS", "EGT"}
        if mw_race_message and game is not None:
            race_attr = attr.upper()
            countdown_reset = race_attr == "EGT" and (
                "TIME%3d-1" in text or "TIME=-1" in text
            )
            control_key = (game.game_id, identity.user_id, attr.upper())
            now = time.monotonic()
            with self._connections_lock:
                previous = self._mw_control_messages.get(control_key)
                duplicate = bool(
                    previous
                    and previous[0] == text
                    and now - previous[1] < 1.0
                )
                self._mw_control_messages[control_key] = (text, now)
            # EGS begins the client's ready/connectivity loop; EGT only
            # carries the host countdown.  Keep the lightweight marker for
            # the subsequent AUX self projection, but do not treat every EGT
            # sender as a newly-ready participant.
            if race_attr == "EGS":
                self.sessions.set_ready(game.game_id, identity.user_id, True)
            if duplicate:
                return ClassicPreloginReply(
                    (ack,),
                    f"race_{attr.lower()}_duplicate",
                )
            peer_message = self._message_frame(
                text,
                sender_name,
                flag=attr.upper(),
                include_user=True,
            )
            own_message = self._message_frame(
                text,
                sender_name,
                flag=f"{attr.upper()}U",
                include_user=True,
            )
            self._send_users(
                set(game.participants),
                (peer_message,),
                exclude=identity.user_id,
            )
            result = (
                "race_egt_reset"
                if countdown_reset
                else f"race_{attr.lower()}"
            )
            if mw_callback:
                actor = self._context_for_user(identity.user_id)
                if actor is not None and actor.send_wire is not None:
                    actor.send_wire(own_message)
                return ClassicPreloginReply((ack,), result)
            return ClassicPreloginReply(
                (ack, own_message),
                result,
            )
        message = self._message_frame(text, sender_name, attr=attr)
        if game is not None:
            self._send_users(
                set(game.participants),
                (message,),
                exclude=identity.user_id,
            )
        return ClassicPreloginReply((ack, message), "room_message")
