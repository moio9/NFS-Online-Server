"""Classic lobby address, authentication and user-bootstrap handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.auth import ClassicAuthReply
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


class ClassicHandshakeMixin:
    """Handle the pre-lobby identity and endpoint bootstrap commands."""

    def _dispatch_address(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        context.client_address = str(
            fields.get("ADDR", context.client_address) or ""
        ).strip()
        try:
            context.client_port = int(
                fields.get("PORT", context.client_port) or 0
            )
        except (TypeError, ValueError):
            context.client_port = 0
        return ClassicPreloginReply((self._signed_ack("addr"),), "address")

    def _dispatch_authentication(
        self,
        packet: ClassicEAFrame,
        context: ClassicPreloginContext,
        command: str,
    ) -> ClassicPreloginReply:
        auth_reply: ClassicAuthReply = self.auth.dispatch(packet, context.auth)
        if auth_reply.accepted and command == "auth":
            context.authenticated = True
        if auth_reply.accepted and command in {"pers", "cper"}:
            context.persona_selected = True
            self._register(context)
        frames = auth_reply.frames
        if (
            auth_reply.accepted
            and command == "auth"
            and self._is_underground2
            and context.u2_rooms_requested
        ):
            frames = (*frames, *self._u2_room_frames())
        return ClassicPreloginReply(
            frames,
            auth_reply.reason,
            auth_reply.close_connection,
        )

    def _dispatch_user(
        self,
        context: ClassicPreloginContext,
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        if not context.authenticated or context.auth.identity is None:
            return ClassicPreloginReply(
                (ClassicEAFrame.short("userbadc"),),
                "not_authenticated",
                True,
            )
        return ClassicPreloginReply(
            (self._user_frame(context), self._auxiliary_frame()),
            "user",
        )
