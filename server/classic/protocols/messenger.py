"""Classic U2/MW adapter for the shared EA Messenger TCP hub."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from classic.core.catalog import GameId
from classic.ea.messenger import EAMessengerFrame, WireSender, messenger_profile

from .control import (
    ClassicControlContext,
    ClassicControlService,
    encode_control_frame,
)
from .frame import ClassicEAFrame


@dataclass
class ClassicMessengerAdapterContext:
    control: ClassicControlContext
    pending_auth: EAMessengerFrame | None = None


class ClassicMessengerAdapter:
    """Route one classic Messenger dialect without duplicating socket code."""

    def __init__(self, service: ClassicControlService, game: GameId) -> None:
        if game not in {GameId.UNDERGROUND2, GameId.MOST_WANTED}:
            raise ValueError(f"classic Messenger adapter cannot serve {game.value}")
        self.service = service
        self.game = game
        self.profile = messenger_profile(game)
        self.name = f"{game.value}-classic-messenger"

    def matches(
        self,
        first_frame: EAMessengerFrame,
        peer: tuple[str, int],
    ) -> bool:
        command = first_frame.command.upper()
        if command not in {"AUTH", "PSET"}:
            return False
        fields = first_frame.fields

        combined = " ".join(
            (
                fields.get("RSRC", ""),
                fields.get("TITL", ""),
                fields.get("PROD", ""),
                fields.get("STAT", ""),
            )
        ).casefold()
        # Carbon can share this TCP endpoint, but its LKEY AUTH must never be
        # selected from an unrelated same-IP U2/MW lobby identity.
        if "nfs-2007" in combined or "need for speed carbon" in combined:
            return False
        requested = self.service._requested_persona(fields)
        if requested:
            active_identity = self.service.social.resolve_lobby(
                peer[0],
                requested,
            )
            if active_identity is not None:
                # Once the named persona exists, its actual lobby game is a
                # stronger discriminator than the generic stock product year.
                return self.service.can_authenticate(peer[0], fields)

        u2_hints = ("nfs-2005", "nfs-console-2005", "underground 2", "nfs5")
        mw_hints = ("nfs-2006", "nfs-console-2006", "most wanted", "nfs6")
        has_u2_hint = any(hint in combined for hint in u2_hints)
        has_mw_hint = any(hint in combined for hint in mw_hints)
        if has_u2_hint or has_mw_hint:
            return has_u2_hint if self.game is GameId.UNDERGROUND2 else has_mw_hint

        if command == "PSET":
            # Race-transition PSET sockets contain no USER/persona and may be
            # opened after both same-IP personas already own control sockets.
            # Select by the active game's lobby, then dispatch() can send the
            # original server's one-shot PSET acknowledgement.
            return self.service.social.has_lobby(
                peer[0],
                game_id=self.game.value,
            )

        # Some stock clients identify only as NFS-CONSOLE-2005 or omit product
        # fields entirely.  In that case, select the adapter from the active
        # lobby identity registered for this peer/persona and game.
        return self.service.can_authenticate(peer[0], fields)

    def open(
        self,
        peer: tuple[str, int],
        sender: WireSender,
        *,
        now: float,
    ) -> ClassicMessengerAdapterContext:
        del now
        context = ClassicControlContext(
            connection_id=f"{self.game.value}-messenger:{peer[0]}:{peer[1]}:{uuid4().hex}",
            client_ip=peer[0],
        )
        # The service receives this callback during AUTH and stores it in the
        # shared social registry for asynchronous roster/message delivery.
        context.sender = sender
        return ClassicMessengerAdapterContext(context)

    def dispatch(
        self,
        frame: EAMessengerFrame,
        context: ClassicMessengerAdapterContext,
        *,
        now: float,
    ) -> list[bytes]:
        del now

        classic = ClassicEAFrame(frame.command, frame.payload, frame.word)
        if frame.command.upper() == "AUTH" and not self.service.can_authenticate(
            context.control.client_ip,
            classic.fields(),
        ):
            # Stock U2 opens Messenger slightly before its lobby AUTH/PERS
            # finishes. Keep the AUTH pending instead of rejecting a valid
            # connection merely because the lobby identity is not visible yet.
            context.pending_auth = frame
            return []
        if frame.command.upper() == "PSET" and not context.control.authenticated:
            # The game also opens short, PSET-only presence connections while
            # transitioning from lobby to race. The original service ACKs this
            # bootstrap without requiring a preceding AUTH on the same socket.
            return [encode_control_frame("PSET")]

        reply = self.service.dispatch(
            classic,
            context.control,
            self._control_sender(context),
        )
        if reply.close_connection:
            context.control.authenticated = False
            context.control.close_requested = True
        return list(reply.frames)

    @staticmethod
    def _control_sender(
        context: ClassicMessengerAdapterContext,
    ):
        def send(
            verb: str,
            fields: tuple[tuple[str, str], ...],
        ) -> bool:
            sender = context.control.sender
            return bool(sender and sender(encode_control_frame(verb, fields)))

        return send

    def poll(
        self,
        context: ClassicMessengerAdapterContext,
        *,
        now: float,
    ) -> list[bytes]:
        del now
        pending = context.pending_auth
        if pending is None:
            return []
        classic = ClassicEAFrame(pending.command, pending.payload, pending.word)
        fields = classic.fields()
        if not self.service.can_authenticate(context.control.client_ip, fields):
            return []
        context.pending_auth = None
        reply = self.service.dispatch(
            classic,
            context.control,
            self._control_sender(context),
        )
        if reply.close_connection:
            context.control.authenticated = False
            context.control.close_requested = True
        return list(reply.frames)

    @staticmethod
    def close_requested(context: ClassicMessengerAdapterContext) -> bool:
        return context.control.close_requested

    @staticmethod
    def connection_id(context: ClassicMessengerAdapterContext) -> str:
        return context.control.connection_id

    @staticmethod
    def account_name(context: ClassicMessengerAdapterContext) -> str:
        identity = context.control.identity
        return identity.account_name if identity is not None else ""

    @staticmethod
    def policy_frames(context: ClassicMessengerAdapterContext, event) -> tuple[bytes, ...]:
        del context, event
        return ()

    @staticmethod
    def request_close(context: ClassicMessengerAdapterContext, reason: str) -> None:
        del reason
        context.control.authenticated = False
        context.control.close_requested = True

    @staticmethod
    def authenticated(context: ClassicMessengerAdapterContext) -> bool:
        return context.control.authenticated

    @staticmethod
    def describe(context: ClassicMessengerAdapterContext) -> str:
        return context.control.persona or "<unauthenticated>"

    def close(self, context: ClassicMessengerAdapterContext) -> None:
        context.control.sender = None
        self.service.release(context.control)
