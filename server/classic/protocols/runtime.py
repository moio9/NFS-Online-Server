"""Live bootstrap and pre-login runtime shared by Underground 2 and MW."""

from __future__ import annotations

import logging
import socket
import time
from threading import Event, Lock
from typing import Callable
from uuid import uuid4

from common.enforcement import (
    AccountPolicyEvent,
    LiveAccountConnection,
    LiveAccountConnectionRegistry,
    PolicyCloseGate,
)

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.catalog import GameId
from classic.core.config import Endpoint, ClassicGameSettings
from classic.core.tcp import TCPListener
from classic.ea.social import SocialService
from classic.ea.ranking import ClassicRankingStore
from classic.ea.directory import SessionDirectory

from .auth import (
    ClassicActiveSessionRegistry,
    ClassicAuthContext,
    ClassicAuthService,
)
from .bootstrap import ClassicBootstrapService, ClassicDirectoryRegistry
from .prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)
from .stream import ClassicEAFrameError, ClassicEAShortFrame, ClassicEAStreamDecoder
from .u2_stock_transport import U2StockBootstrapTransport


log = logging.getLogger(__name__)
AuthFactory = Callable[..., ClassicAuthService]


class ClassicGameRuntime:
    """One game's bootstrap/lobby listeners using shared account/social stores."""

    def __init__(
        self,
        settings: ClassicGameSettings,
        *,
        credentials: CredentialStore,
        identities: IdentityStore,
        social: SocialService,
        ranking: ClassicRankingStore,
        auth_factory: AuthFactory,
        prelogin_profile: ClassicPreloginProfile,
        messenger_public: Endpoint,
        web_public: Endpoint,
        max_frame_size: int,
        connection_timeout: float,
        directory_ttl: float,
        lobby_idle_timeout: float,
        lobby_heartbeat_interval: float,
        verify_passwords: bool,
        auto_enroll: bool,
        active_sessions: ClassicActiveSessionRegistry | None = None,
        live_connections: LiveAccountConnectionRegistry | None = None,
        extra_lobby_listens: tuple[Endpoint, ...] = (),
    ) -> None:
        self.settings = settings
        self.game = settings.game
        self.credentials = credentials
        self.identities = identities
        self.social = social
        self.max_frame_size = int(max_frame_size)
        self.connection_timeout = float(connection_timeout)
        self.lobby_idle_timeout = float(lobby_idle_timeout)
        self.lobby_heartbeat_interval = float(lobby_heartbeat_interval)
        self.lobby_heartbeat_wire = prelogin_profile.lobby_heartbeat_wire
        self.active_sessions = active_sessions or ClassicActiveSessionRegistry()
        self.live_connections = live_connections
        self.sessions = SessionDirectory()
        self.directory_registry = ClassicDirectoryRegistry(
            fixed_session=settings.directory_session,
            fixed_mask=settings.directory_mask,
            ttl_seconds=directory_ttl,
        )
        self.bootstrap = ClassicBootstrapService(
            self.directory_registry,
            settings.lobby_public,
        )
        self.auth = auth_factory(
            credentials,
            identities,
            active_sessions=self.active_sessions,
            verify_passwords=verify_passwords,
            auto_enroll=auto_enroll,
        )
        self.prelogin = ClassicPreloginService(
            self.auth,
            profile=prelogin_profile,
            control_endpoint=messenger_public,
            web_endpoint=web_public,
            sessions=self.sessions,
            ranking=ranking,
            social=social,
        )
        self.u2_stock_transport = (
            U2StockBootstrapTransport(
                bootstrap=self.bootstrap,
                directory_registry=self.directory_registry,
                prelogin=self.prelogin,
                social=self.social,
                max_frame_size=self.max_frame_size,
                connection_timeout=self.connection_timeout,
            )
            if self.game is GameId.UNDERGROUND2
            else None
        )
        prefix = "u2" if self.game is GameId.UNDERGROUND2 else "mw"
        self.lobby_listener = TCPListener(
            settings.lobby_listen,
            self._handle_lobby_connection,
            name=f"{prefix}-lobby",
        )
        self.extra_lobby_listeners = tuple(
            TCPListener(
                endpoint,
                self._handle_lobby_connection,
                name=f"{prefix}-lobby-extra-{index}",
            )
            for index, endpoint in enumerate(extra_lobby_listens, 1)
        )
        self.bootstrap_listener = TCPListener(
            settings.bootstrap_listen,
            self._handle_bootstrap_connection,
            name=f"{prefix}-bootstrap",
        )

    @property
    def display_name(self) -> str:
        return (
            "Need for Speed Underground 2"
            if self.game is GameId.UNDERGROUND2
            else "Need for Speed Most Wanted"
        )

    @staticmethod
    def _advertised(configured: Endpoint, bound: Endpoint) -> Endpoint:
        return Endpoint(
            configured.host,
            bound.port if configured.port == 0 else configured.port,
        )

    def set_shared_endpoints(self, messenger: Endpoint, web: Endpoint) -> None:
        self.prelogin.set_control_endpoint(messenger)
        self.prelogin.set_web_endpoint(web)

    def start(self) -> tuple[Endpoint, Endpoint]:
        lobby_endpoint = self.lobby_listener.start()
        extra_lobby_endpoints: list[Endpoint] = []
        try:
            for listener in self.extra_lobby_listeners:
                extra_lobby_endpoints.append(listener.start())
        except Exception:
            for listener in reversed(self.extra_lobby_listeners):
                listener.stop()
            self.lobby_listener.stop()
            raise
        self.bootstrap.set_advertised_lobby(
            self._advertised(self.settings.lobby_public, lobby_endpoint)
        )
        try:
            bootstrap_endpoint = self.bootstrap_listener.start()
        except Exception:
            for listener in reversed(self.extra_lobby_listeners):
                listener.stop()
            self.lobby_listener.stop()
            raise
        advertised_lobby = self.bootstrap.advertised_lobby
        log.info(
            "%s bootstrap listening on %s:%d",
            self.display_name,
            bootstrap_endpoint.host,
            bootstrap_endpoint.port,
        )
        log.info(
            "%s lobby/auth listening on %s:%d, advertised as %s:%d",
            self.display_name,
            lobby_endpoint.host,
            lobby_endpoint.port,
            advertised_lobby.host,
            advertised_lobby.port,
        )
        for endpoint in extra_lobby_endpoints:
            log.info(
                "%s additional lobby/callback listening on %s:%d",
                self.display_name,
                endpoint.host,
                endpoint.port,
            )
        return bootstrap_endpoint, lobby_endpoint

    def stop(self) -> None:
        self.bootstrap_listener.stop()
        for listener in reversed(self.extra_lobby_listeners):
            listener.stop()
        self.lobby_listener.stop()

    def _recv_loop(
        self,
        conn: socket.socket,
        stop_event: Event,
        *,
        service_name: str,
        on_packet,
        send_wire=None,
        idle_timeout: float | None,
        heartbeat_interval: float | None = None,
        heartbeat_ready: Callable[[], bool] | None = None,
        heartbeat_wire: bytes | Callable[[], bytes] = b"",
        close_requested: Callable[[], bool] | None = None,
        close_reason: Callable[[], str] | None = None,
        quiesced: Callable[[], bool] | None = None,
        log_first_data: bool = False,
    ) -> str:
        poll = min(0.5, self.connection_timeout)
        conn.settimeout(poll)
        decoder = ClassicEAStreamDecoder(max_frame_size=self.max_frame_size)
        last_activity = time.monotonic()
        next_heartbeat = (
            last_activity + heartbeat_interval
            if heartbeat_interval is not None
            else None
        )
        heartbeat_count = 0
        first_data_logged = False

        def requested_reason() -> str | None:
            if close_requested is None or not close_requested():
                return None
            reason = close_reason() if close_reason is not None else ""
            return str(reason or "policy-close")

        while not stop_event.is_set():
            policy_reason = requested_reason()
            if policy_reason is not None:
                return policy_reason
            try:
                data = conn.recv(8192)
            except socket.timeout:
                policy_reason = requested_reason()
                if policy_reason is not None:
                    return policy_reason
                if quiesced is not None and quiesced():
                    continue
                now = time.monotonic()
                if (
                    idle_timeout is not None
                    and now - last_activity >= idle_timeout
                ):
                    return "idle-timeout"
                if (
                    next_heartbeat is not None
                    and now >= next_heartbeat
                    and heartbeat_wire
                    and heartbeat_ready is not None
                    and heartbeat_ready()
                ):
                    try:
                        heartbeat_payload = bytes(
                            heartbeat_wire()
                            if callable(heartbeat_wire)
                            else heartbeat_wire
                        )
                        if not heartbeat_payload:
                            next_heartbeat = now + float(heartbeat_interval)
                            continue
                        if send_wire is None:
                            conn.sendall(heartbeat_payload)
                        elif not send_wire(heartbeat_payload):
                            if quiesced is not None and quiesced():
                                continue
                            return "heartbeat-send-error"
                    except OSError as exc:
                        return f"heartbeat-send-error:{exc.errno or 'unknown'}"
                    heartbeat_count += 1
                    next_heartbeat = now + float(heartbeat_interval)
                    if heartbeat_count == 1 or heartbeat_count % 10 == 0:
                        log.info(
                            "%s heartbeat sent: count=%d",
                            service_name,
                            heartbeat_count,
                        )
                continue
            except OSError as exc:
                policy_reason = requested_reason()
                if policy_reason is not None:
                    return policy_reason
                return f"recv-error:{exc.errno or 'unknown'}"
            policy_reason = requested_reason()
            if policy_reason is not None:
                return policy_reason
            if not data:
                return "peer-eof"
            if quiesced is not None and quiesced():
                continue
            if log_first_data and not first_data_logged:
                first_data_logged = True
                log.info(
                    "%s raw first recv len=%d hex=%s",
                    service_name,
                    len(data),
                    data[:96].hex(),
                )
            last_activity = time.monotonic()
            if heartbeat_interval is not None:
                next_heartbeat = last_activity + heartbeat_interval
            try:
                packets = decoder.feed(data)
            except ClassicEAFrameError as exc:
                log.warning("invalid %s frame: %s", service_name, exc)
                return "invalid-frame"
            quiesced_during_dispatch = False
            for packet in packets:
                try:
                    packet_result = on_packet(packet)
                    if len(packet_result) == 2:
                        frames, close_connection = packet_result
                        after_send = None
                    else:
                        frames, close_connection, after_send = packet_result
                except OSError as exc:
                    return f"dispatch-error:{exc.errno or 'unknown'}"
                except Exception:
                    log.exception("%s dispatch failed for packet=%r", service_name, packet)
                    return "dispatch-error"
                for frame in frames:
                    try:
                        if send_wire is None:
                            conn.sendall(frame)
                        elif not send_wire(frame):
                            if quiesced is not None and quiesced():
                                quiesced_during_dispatch = True
                                break
                            return "send-error"
                    except OSError as exc:
                        return f"send-error:{exc.errno or 'unknown'}"
                if quiesced_during_dispatch:
                    break
                if quiesced is not None and quiesced():
                    quiesced_during_dispatch = True
                    break
                if after_send is not None:
                    try:
                        after_send()
                    except Exception:
                        log.exception(
                            "%s post-send action failed for packet=%r",
                            service_name,
                            packet,
                        )
                        return "post-send-error"
                if close_connection:
                    return "protocol-close"
            if quiesced_during_dispatch:
                continue
        return "server-stop"

    def _handle_bootstrap_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        connected_at = time.monotonic()
        label = self.game.value
        log.info("%s bootstrap client connected: peer=%s:%d", label, addr[0], addr[1])

        if self.u2_stock_transport is not None:
            reason = self.u2_stock_transport.run(conn, addr, stop_event)
        else:
            def on_packet(packet):
                if isinstance(packet, ClassicEAShortFrame):
                    return (), False
                reply = self.bootstrap.dispatch(packet, client_ip=addr[0])
                if packet.command.casefold() in {"@dir", "?dir"}:
                    advertised = self.bootstrap.endpoint_for_client(addr[0])
                    log.info(
                        "%s directory reply lobby=%s:%d",
                        label,
                        advertised.host,
                        advertised.port,
                    )
                return reply.frames, reply.close_connection

            reason = self._recv_loop(
                conn,
                stop_event,
                service_name=f"{label} bootstrap",
                on_packet=on_packet,
                idle_timeout=self.connection_timeout,
                log_first_data=True,
            )
        log.info(
            "%s bootstrap client disconnected: peer=%s:%d uptime=%.3f reason=%s",
            label,
            addr[0],
            addr[1],
            time.monotonic() - connected_at,
            reason,
        )

    def _handle_lobby_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        label = self.game.value
        connection_id = f"{label}:{addr[0]}:{addr[1]}:{uuid4().hex}"
        challenge = self.directory_registry.recent(addr[0])
        auth_context = ClassicAuthContext(
            connection_id=connection_id,
            client_ip=addr[0],
            session_challenge=challenge.session if challenge else "",
            mask=challenge.mask if challenge else "",
        )
        context = ClassicPreloginContext(
            auth=auth_context,
            client_address=addr[0],
            client_port=addr[1],
        )
        send_lock = Lock()
        dispatch_lock = Lock()
        policy_close = PolicyCloseGate()

        def send_wire(frame: bytes) -> bool:
            try:
                with send_lock:
                    if context.close_requested or policy_close.active:
                        return False
                    conn.sendall(frame)
                return True
            except OSError:
                return False

        context.send_wire = send_wire

        def account_name() -> str:
            if context.auth.account is not None:
                return context.auth.account.account_name
            if context.auth.identity is not None:
                return context.auth.identity.account_name
            return ""

        def begin_policy_close(event: AccountPolicyEvent) -> bool:
            frame = self.auth.account_policy_frame(event.action)
            with dispatch_lock:
                with send_lock:
                    if context.close_requested or policy_close.active:
                        return False
                    context.close_reason = event.disconnect_reason
                    try:
                        conn.sendall(frame)
                    except OSError:
                        policy_close.force(event.disconnect_reason)
                        return False
                    policy_close.request(event.disconnect_reason)
            return True

        def request_policy_close(reason: str) -> None:
            with dispatch_lock:
                with send_lock:
                    context.close_reason = context.close_reason or str(
                        reason or "account-policy"
                    )
                    policy_close.request(context.close_reason)

        if self.live_connections is not None:
            self.live_connections.register(
                LiveAccountConnection(
                    connection_id=connection_id,
                    protocol=f"classic-{label}-lobby",
                    account_name=account_name,
                    begin_close=begin_policy_close,
                    request_close=request_policy_close,
                )
            )
        connected_at = time.monotonic()
        registered_persona = ""
        registered_game_session = ""
        log.info(
            "%s lobby client connected: peer=%s:%d challenge=%s",
            label,
            addr[0],
            addr[1],
            "yes" if challenge else "no",
        )

        def dispatch_packet(packet):
            nonlocal registered_persona, registered_game_session
            reply = self.prelogin.dispatch(packet, context)
            if (
                context.auth.identity is not None
                and context.auth.persona
                and context.auth.persona.casefold() != registered_persona.casefold()
            ):
                account_name = (
                    context.auth.account.account_name
                    if context.auth.account is not None
                    else context.auth.identity.account_name
                )
                self.social.register_lobby(
                    connection_id,
                    account_name,
                    context.auth.persona,
                    addr[0],
                    game_id=self.game.value,
                    session_token=context.auth.session_token,
                )
                registered_persona = context.auth.persona
            session_id = str(int(context.lobby_game_id or 0)) if registered_persona else ""
            if session_id != registered_game_session:
                self.social.set_game_session(
                    connection_id,
                    registered_persona,
                    self.game.value,
                    session_id if session_id != "0" else "",
                )
                registered_game_session = session_id if session_id != "0" else ""
            command = packet.tag if isinstance(packet, ClassicEAShortFrame) else packet.command
            log.info(
                "%s lobby command=%s result=%s close=%s",
                label,
                command,
                reply.reason,
                reply.close_connection,
            )
            if (
                label == GameId.MOST_WANTED.value
                and not isinstance(packet, ClassicEAShortFrame)
                and (
                    reply.reason.startswith("race_")
                    or reply.reason == "auxiliary_ready_refresh"
                )
            ):
                values = packet.fields()
                attr = (
                    values.get("ATTR", "")
                    or values.get("FLAGS", "")
                    or values.get("F", "")
                )
                text = values.get("TEXT", values.get("T", ""))
                log.info(
                    "%s lobby ready payload: command=%s attr=%s text=%s",
                    label,
                    command,
                    attr or "-",
                    text[:320],
                )
            if reply.reason == "unsupported_command":
                log.info(
                    "%s lobby command deferred: peer=%s:%d command=%s",
                    label,
                    addr[0],
                    addr[1],
                    command,
                )
            return reply.frames, reply.close_connection, reply.after_send

        def on_packet(packet):
            with dispatch_lock:
                if context.close_requested:
                    return (), True, None
                if policy_close.active:
                    return (), False, None
                return dispatch_packet(packet)

        def heartbeat_ready() -> bool:
            # Keep the lobby transport alive in every protocol phase.  Stock
            # clients can remain between ``sele`` and ``auth`` while the user
            # is entering credentials, so heartbeat delivery must not depend
            # on an account session already existing.  Only the SQLite/session
            # lease refresh remains authentication-gated.
            if policy_close.active:
                return False
            if context.authenticated and hasattr(self.active_sessions, "touch"):
                if not self.active_sessions.touch(connection_id):
                    context.authenticated = False
            return True

        reason = "connection-handler-error"
        try:
            reason = self._recv_loop(
                conn,
                stop_event,
                service_name=f"{label} lobby",
                on_packet=on_packet,
                send_wire=send_wire,
                idle_timeout=(
                    self.lobby_idle_timeout
                    if self.lobby_idle_timeout > 0
                    else None
                ),
                heartbeat_interval=(
                    self.lobby_heartbeat_interval
                    if self.lobby_heartbeat_wire
                    and self.lobby_heartbeat_interval > 0
                    else None
                ),
                heartbeat_ready=heartbeat_ready,
                heartbeat_wire=self.lobby_heartbeat_wire,
                close_requested=lambda: (
                    context.close_requested or policy_close.expired()
                ),
                close_reason=lambda: (
                    context.close_reason or policy_close.reason
                ),
                quiesced=lambda: policy_close.active,
            )
        finally:
            if self.live_connections is not None:
                self.live_connections.unregister(connection_id)
            persona = context.auth.persona or "<unauthenticated>"
            if registered_persona:
                self.social.unregister_lobby(connection_id)
            self.prelogin.release(context)
        log.info(
            "%s lobby client disconnected: peer=%s:%d persona=%s uptime=%.3f reason=%s",
            label,
            addr[0],
            addr[1],
            persona,
            time.monotonic() - connected_at,
            reason,
        )
