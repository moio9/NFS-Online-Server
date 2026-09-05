"""Carbon adapter for the shared EA Messenger TCP hub."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from uuid import uuid4

from classic.ea.messenger import EAMessengerFrame, WireSender
from classic.ea.social import SocialService
from classic.protocols.carbon_messenger_ipc import CarbonIPCIdentity, CarbonMessengerIPCState
from classic.protocols.carbon_messenger_service import (
    CarbonMessengerService,
    MessengerConnection,
)


log = logging.getLogger(__name__)
_FORCED_LOGOFF_CLOSE_GRACE = 2.0
# Theater GLST completes before Carbon's frontend finishes its initial FESL
# ranking/metrics burst.  Delivering ADMN during that burst makes the SDK tear
# down its sockets before FeOnlineDisconnect owns the native error dialog.
_FORCED_LOGOFF_UI_READY_GRACE = 1.0


@dataclass
class CarbonMessengerAdapterContext:
    connection: MessengerConnection
    peer: tuple[str, int]
    next_ping: float
    pending_auth: EAMessengerFrame | None = None
    pending_auth_deadline: float = 0.0
    forced_logoff_endpoint_ready: bool = False
    forced_logoff_pset_ready: bool = False
    forced_logoff_ui_ready_deadline: float = 0.0
    forced_logoff_close_deadline: float = 0.0


class CarbonMessengerAdapter:
    """Serve Carbon on the U2/MW shared Messenger port.

    Authentication is resolved from the authenticated loopback IPC state published by
    COnline.  A short pending window absorbs the normal FESL-login-to-Messenger
    race without accepting unknown LKEY values.
    """

    name = "carbon-shared-messenger"

    def __init__(
        self,
        state: CarbonMessengerIPCState,
        *,
        social: SocialService | None = None,
        identity_resolver=None,
        heartbeat_interval: float = 30.0,
        auth_ipc_wait: float = 3.0,
    ) -> None:
        self.state = state
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.auth_ipc_wait = max(0.25, float(auth_ipc_wait))
        self.service = CarbonMessengerService(
            state,
            is_inviteable=state.is_inviteable,
            invite_details=state.invite_details,
            known_identities=state.known_identities,
            social=social,
            identity_resolver=identity_resolver,
        )

    def matches(
        self,
        first_frame: EAMessengerFrame,
        peer: tuple[str, int],
    ) -> bool:
        del peer
        if first_frame.command.upper() != "AUTH":
            return False
        fields = first_frame.fields
        resource = " ".join(
            (
                fields.get("RSRC", ""),
                fields.get("PRES", ""),
                fields.get("TITL", ""),
                fields.get("PROD", ""),
            )
        ).casefold()
        token = fields.get("LKEY", "")
        return bool(token) and (
            "nfs-2007" in resource
            or "carbon" in resource
            or self.state.resolve_session(token) is not None
            or self.state.forced_logoff(token) is not None
        )

    def open(
        self,
        peer: tuple[str, int],
        sender: WireSender,
        *,
        now: float,
    ) -> CarbonMessengerAdapterContext:
        connection = MessengerConnection(
            connection_id=f"carbon-messenger:{peer[0]}:{peer[1]}:{uuid4().hex}",
            client_ip=peer[0],
        )

        def send_frame(frame: EAMessengerFrame) -> bool:
            return bool(sender(frame.encode()))

        connection.sender = send_frame
        return CarbonMessengerAdapterContext(
            connection=connection,
            peer=peer,
            next_ping=now + self.heartbeat_interval,
        )

    def _dispatch_authenticated(
        self,
        frame: EAMessengerFrame,
        context: CarbonMessengerAdapterContext,
        *,
        now: float,
    ) -> list[bytes]:
        replies = self.service.dispatch(frame, context.connection)
        context.next_ping = now + self.heartbeat_interval
        output = [reply.encode() for reply in replies]
        output.extend(item.encode() for item in context.connection.drain())
        return output

    def dispatch(
        self,
        frame: EAMessengerFrame,
        context: CarbonMessengerAdapterContext,
        *,
        now: float,
    ) -> list[bytes]:
        if context.connection.forced_logoff_reason:
            if context.connection.forced_logoff_notice_sent:
                return []
            command = frame.command.upper()
            # Retail Carbon does not declare Communicator connected at AUTH.
            # It first completes both RGET transactions and EPGT.  Let that
            # bootstrap run normally, then wait for PSET.  PSET proves the
            # parallel Theater USER handshake has also completed.
            if not context.forced_logoff_endpoint_ready:
                output = self._dispatch_authenticated(frame, context, now=now)
                if command == "EPGT" and not context.forced_logoff_endpoint_ready:
                    context.forced_logoff_endpoint_ready = True
                    log.warning(
                        "Carbon Messenger duplicate bootstrap completed: "
                        "persona=%s action=wait-for-pset",
                        context.connection.identity.persona
                        if context.connection.identity is not None
                        else "<unknown>",
                    )
                return output

            if command != "PSET":
                return self._dispatch_authenticated(frame, context, now=now)

            # Acknowledge the rejected client's PSET without replacing the
            # established client's shared presence.  Theater GLST normally
            # finishes a few milliseconds later; poll waits for its IPC write
            # barrier before sending the native notice.
            output = [
                EAMessengerFrame.from_fields(
                    "PSET",
                    {"ID": frame.fields.get("ID", "")},
                    transaction=0,
                    trailing_newline=True,
                ).encode()
            ]
            context.forced_logoff_pset_ready = True
            log.warning(
                "Carbon Messenger duplicate PSET acknowledged: persona=%s "
                "action=wait-for-theater-glst",
                context.connection.identity.persona
                if context.connection.identity is not None
                else "<unknown>",
            )
            return output
        if frame.command.upper() == "AUTH" and not context.connection.authenticated:
            token = frame.fields.get("LKEY", "")
            forced_logoff = self.state.forced_logoff(token)
            if forced_logoff is not None:
                context.pending_auth = None
                return [
                    self.service.begin_forced_logoff(
                        context.connection,
                        token,
                        forced_logoff,
                        frame.fields.get("ID", ""),
                    ).encode()
                ]
            if self.state.resolve_session(token) is None:
                context.pending_auth = frame
                context.pending_auth_deadline = now + self.auth_ipc_wait
                log.info(
                    "Carbon Messenger AUTH waiting for IPC session: peer=%s:%d",
                    context.peer[0],
                    context.peer[1],
                )
                return []
        return self._dispatch_authenticated(frame, context, now=now)

    def poll(
        self,
        context: CarbonMessengerAdapterContext,
        *,
        now: float,
    ) -> list[bytes]:
        output: list[bytes] = []
        pending = context.pending_auth
        if pending is not None:
            token = pending.fields.get("LKEY", "")
            forced_logoff = self.state.forced_logoff(token)
            if forced_logoff is not None:
                context.pending_auth = None
                output.append(
                    self.service.begin_forced_logoff(
                        context.connection,
                        token,
                        forced_logoff,
                        pending.fields.get("ID", ""),
                    ).encode()
                )
            elif self.state.resolve_session(token) is not None:
                context.pending_auth = None
                output.extend(self._dispatch_authenticated(pending, context, now=now))
            elif now >= context.pending_auth_deadline:
                context.pending_auth = None
                # Preserve Carbon's normal explicit INVALID_SESSION response
                # after the IPC grace period expires.
                output.extend(self._dispatch_authenticated(pending, context, now=now))
            return output

        if (
            context.connection.forced_logoff_reason
            and context.forced_logoff_pset_ready
            and not context.connection.forced_logoff_notice_sent
        ):
            forced_logoff = self.state.forced_logoff(
                context.connection.session_token
            )
            if forced_logoff is not None and forced_logoff.theater_ready:
                if context.forced_logoff_ui_ready_deadline <= 0.0:
                    context.forced_logoff_ui_ready_deadline = (
                        now + _FORCED_LOGOFF_UI_READY_GRACE
                    )
                    log.warning(
                        "Carbon Messenger duplicate transport bootstrap ready: "
                        "persona=%s action=wait-for-frontend-ui delay=%.3fs",
                        context.connection.identity.persona
                        if context.connection.identity is not None
                        else "<unknown>",
                        _FORCED_LOGOFF_UI_READY_GRACE,
                    )
                    return output
                if now < context.forced_logoff_ui_ready_deadline:
                    return output
                output.append(
                    self.service.finish_forced_logoff(context.connection).encode()
                )
                context.forced_logoff_close_deadline = (
                    now + _FORCED_LOGOFF_CLOSE_GRACE
                )
            return output

        if (
            context.connection.forced_logoff_notice_sent
            and context.forced_logoff_close_deadline > 0.0
            and now >= context.forced_logoff_close_deadline
        ):
            # Normally stock Carbon closes the three service sockets itself
            # after ADMN/DUPL.  Keep the server side open long enough for that
            # native shutdown/UI path; this is only a bounded fallback.
            context.connection.authenticated = False
            context.connection.close_requested = True
            return output

        if context.connection.authenticated:
            self.service.sync_session(context.connection)
        output.extend(item.encode() for item in context.connection.drain())
        if (
            context.connection.forced_logoff_notice_sent
            and context.forced_logoff_close_deadline <= 0.0
        ):
            context.forced_logoff_close_deadline = now + _FORCED_LOGOFF_CLOSE_GRACE
        if context.connection.authenticated and now >= context.next_ping:
            output.append(self.service.ping_frame().encode())
            context.next_ping = now + self.heartbeat_interval
        return output

    @staticmethod
    def after_send(context: CarbonMessengerAdapterContext) -> None:
        context.connection.run_after_send()

    @staticmethod
    def close_requested(context: CarbonMessengerAdapterContext) -> bool:
        return context.connection.close_requested

    @staticmethod
    def connection_id(context: CarbonMessengerAdapterContext) -> str:
        return context.connection.connection_id

    @staticmethod
    def account_name(context: CarbonMessengerAdapterContext) -> str:
        identity = context.connection.identity
        return identity.account_name if identity is not None else ""

    @staticmethod
    def policy_frames(context: CarbonMessengerAdapterContext, event) -> tuple[bytes, ...]:
        del context, event
        return ()

    @staticmethod
    def request_close(context: CarbonMessengerAdapterContext, reason: str) -> None:
        del reason
        context.connection.authenticated = False
        context.connection.close_requested = True

    @staticmethod
    def authenticated(context: CarbonMessengerAdapterContext) -> bool:
        return context.connection.authenticated

    @staticmethod
    def describe(context: CarbonMessengerAdapterContext) -> str:
        identity = context.connection.identity
        return identity.persona if identity is not None else "<unauthenticated>"

    def close(self, context: CarbonMessengerAdapterContext) -> None:
        context.connection.sender = None
        self.service.disconnect(context.connection)
