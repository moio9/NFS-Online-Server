"""Combined Underground 2 and Most Wanted application."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import logging
import ipaddress
import socket
from dataclasses import replace
from pathlib import Path

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from common.accounts import SQLiteAccountDatabase, SQLiteSessionRegistry
from common.enforcement import (
    AccountPolicyEvent,
    AccountPolicyMonitor,
    LiveAccountConnectionRegistry,
    SQLiteAccountPolicyEventStore,
)
from common.runtime_status import RuntimeStatusPublisher
from classic.accounts.sqlite_backend import SQLiteCredentialStore, SQLiteIdentityStore
from classic.core.catalog import GameId
from classic.core.config import Endpoint, ServerSettings
from classic.core.tcp import TCPListener
from classic.core.udp import UDPListener
from classic.ea.messenger import EAMessengerHub
from classic.ea.multiplex import ClassicEndpointMultiplexer
from classic.ea.ranking import ClassicRankingStore
from classic.ea.sqlite_ranking import SQLiteClassicRankingStore
from classic.ea.social import SocialService
from classic.ea.web import ClassicWebGateway, MW_NEWS_PATH, U2_NEWS_PATH
from classic.games.most_wanted.auth import create_auth_service as create_mw_auth
from classic.games.underground2.auth import create_auth_service as create_u2_auth
from classic.protocols.carbon_messenger import CarbonMessengerAdapter
from classic.protocols.carbon_messenger_ipc import (
    CarbonIPCIdentity,
    CarbonMessengerIPCReceiver,
    CarbonMessengerIPCState,
)
from classic.protocols.control import ClassicControlProfile, ClassicControlService
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.messenger import ClassicMessengerAdapter
from classic.protocols.prelogin import ClassicPreloginProfile
from classic.protocols.race import ClassicRaceRelay
from classic.protocols.runtime import ClassicGameRuntime


log = logging.getLogger(__name__)

CLASSIC_LOBBY_HEARTBEAT = ClassicEAFrame.signed("@cnt", b"\x00", 9).encode()


def _mw_lobby_heartbeat() -> bytes:
    """Build the stock MW ``~png`` keepalive with a fresh REF timestamp."""

    now = datetime.now()
    reference = f"{now.year}.{now.month}.{now.day}-{now:%H:%M:%S}"
    return ClassicEAFrame.from_fields(
        "~png",
        (("REF", reference),),
        final_separator=False,
    ).encode()


MW_MAX_RELAY_PLAYERS = 4

U2_PRELOGIN = ClassicPreloginProfile(
    game_id=GameId.UNDERGROUND2.value,
    news_path=U2_NEWS_PATH,
    lobby_heartbeat_wire=CLASSIC_LOBBY_HEARTBEAT,
)
MW_PRELOGIN = ClassicPreloginProfile(
    game_id=GameId.MOST_WANTED.value,
    tos_url_keys=("TOSA_URL", "TOSAC_URL"),
    news_url_key="NEWS_URL",
    news_path=MW_NEWS_PATH,
    lobby_heartbeat_wire=_mw_lobby_heartbeat,
)


class ClassicOnlineApplication:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.enable_u2 = bool(settings.enable_u2)
        self.enable_mw = bool(settings.enable_mw)
        self.live_connections = LiveAccountConnectionRegistry(
            name="classic-live-connections"
        )
        self.account_database: SQLiteAccountDatabase | None = None
        self.account_policy_monitor: AccountPolicyMonitor | None = None
        self.session_registries: tuple[object, ...] = ()
        if settings.account_db_path is not None and settings.account_files_path is not None:
            self.account_database = SQLiteAccountDatabase(
                settings.account_db_path,
                settings.account_files_path,
                auto_enroll=settings.auth_auto_enroll,
                failure_limit=settings.auth_failure_limit,
                lockout_seconds=settings.auth_lockout_seconds,
                busy_timeout_ms=settings.account_sqlite_busy_timeout_ms,
            )
            self.identities = SQLiteIdentityStore(self.account_database)
            self.credentials = SQLiteCredentialStore(self.account_database)
            u2_sessions = SQLiteSessionRegistry(
                self.account_database,
                game=GameId.UNDERGROUND2.value,
                lease_seconds=settings.account_session_lease_seconds,
            )
            mw_sessions = SQLiteSessionRegistry(
                self.account_database,
                game=GameId.MOST_WANTED.value,
                lease_seconds=settings.account_session_lease_seconds,
            )
            self.session_registries = (u2_sessions, mw_sessions)
            log.info(
                "Shared account SQLite enabled: db=%s files=%s lease=%.1fs",
                self.account_database.path,
                self.account_database.user_root,
                settings.account_session_lease_seconds,
            )
        else:
            log.warning(
                "Shared ACCOUNT_DB/ACCOUNT_FILES are disabled; Classic is using "
                "JSON-local identities/social data and will not share accounts "
                "safely with a separate Carbon process"
            )
            self.identities = IdentityStore()
            self.credentials = CredentialStore(
                settings.auth_data_path,
                auto_enroll=settings.auth_auto_enroll,
                failure_limit=settings.auth_failure_limit,
                lockout_seconds=settings.auth_lockout_seconds,
            )
            u2_sessions = None
            mw_sessions = None
        if self.account_database is not None:
            legacy_stats = Path(settings.stats_data_path) if settings.stats_data_path else None
            self.ranking = SQLiteClassicRankingStore(
                self.account_database,
                legacy_path=(
                    legacy_stats
                    if legacy_stats is not None and legacy_stats.exists()
                    else None
                ),
            )
        else:
            # Compatibility mode for explicitly shared-account-free setups.
            # The normal launcher always uses the account database above.
            self.ranking = ClassicRankingStore(
                settings.stats_data_path or None,
                persona_visible=self._ranking_persona_visible,
            )
        self.social = SocialService(
            settings.social_data_path,
            database=self.account_database,
            persona_provider=self._known_personas,
        )
        from common.web_social import WebSocialEventPump
        self.web_social_events = (
            WebSocialEventPump(self.account_database.path, self.social)
            if self.account_database is not None else None
        )
        verify_passwords = settings.auth_mode == "password"
        self.u2 = ClassicGameRuntime(
            settings.underground2,
            credentials=self.credentials,
            identities=self.identities,
            social=self.social,
            ranking=self.ranking,
            auth_factory=create_u2_auth,
            prelogin_profile=replace(
                U2_PRELOGIN,
                u2_game_size_policy=settings.u2_game_size_policy,
                u2_game_min_players=settings.u2_game_min_players,
                u2_game_max_players=settings.u2_game_max_players,
            ),
            messenger_public=settings.messenger_public,
            web_public=settings.web_public,
            max_frame_size=settings.max_frame_size,
            connection_timeout=settings.connection_timeout,
            directory_ttl=settings.classic_directory_ttl,
            lobby_idle_timeout=settings.classic_lobby_idle_timeout,
            lobby_heartbeat_interval=settings.classic_lobby_heartbeat_interval,
            verify_passwords=verify_passwords,
            auto_enroll=settings.u2_auth_auto_enroll,
            active_sessions=u2_sessions,
            live_connections=self.live_connections,
        )
        self.mw = ClassicGameRuntime(
            settings.most_wanted,
            credentials=self.credentials,
            identities=self.identities,
            social=self.social,
            ranking=self.ranking,
            auth_factory=create_mw_auth,
            prelogin_profile=MW_PRELOGIN,
            messenger_public=settings.messenger_public,
            web_public=settings.web_public,
            max_frame_size=settings.max_frame_size,
            connection_timeout=settings.connection_timeout,
            directory_ttl=settings.classic_directory_ttl,
            lobby_idle_timeout=settings.classic_lobby_idle_timeout,
            lobby_heartbeat_interval=settings.classic_lobby_heartbeat_interval,
            verify_passwords=verify_passwords,
            auto_enroll=settings.mw_auth_auto_enroll,
            active_sessions=mw_sessions,
            live_connections=self.live_connections,
            extra_lobby_listens=(
                (settings.mw_lobby_extra_listen,)
                if settings.mw_lobby_extra_listen is not None
                else ()
            ),
        )
        self.u2.bootstrap.set_endpoint_resolver(self._endpoint_for_client)
        self.mw.bootstrap.set_endpoint_resolver(self._endpoint_for_client)
        self.u2.prelogin.set_endpoint_resolver(self._endpoint_for_client)
        self.mw.prelogin.set_endpoint_resolver(self._endpoint_for_client)

        self.u2_control = ClassicControlService(
            self.social,
            profile=ClassicControlProfile.for_game(GameId.UNDERGROUND2),
        )
        self.mw_control = ClassicControlService(
            self.social,
            profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
        )
        messenger_adapters = []
        if self.enable_u2:
            messenger_adapters.append(
                ClassicMessengerAdapter(self.u2_control, GameId.UNDERGROUND2)
            )
        if self.enable_mw:
            messenger_adapters.append(
                ClassicMessengerAdapter(self.mw_control, GameId.MOST_WANTED)
            )
        self.carbon_messenger_state = (
            CarbonMessengerIPCState(max_age_seconds=settings.carbon_messenger_ipc_max_age)
            if settings.carbon_messenger_ipc_listen is not None
            else None
        )
        self.carbon_messenger_ipc_receiver = (
            CarbonMessengerIPCReceiver(
                settings.carbon_messenger_ipc_listen,
                secret=settings.carbon_messenger_ipc_secret,
                state=self.carbon_messenger_state,
            )
            if settings.carbon_messenger_ipc_listen is not None
            and self.carbon_messenger_state is not None
            else None
        )
        if self.carbon_messenger_state is not None:
            messenger_adapters.append(
                CarbonMessengerAdapter(
                    self.carbon_messenger_state,
                    social=self.social,
                    identity_resolver=self._resolve_carbon_identity,
                    heartbeat_interval=settings.carbon_messenger_heartbeat_interval,
                    auth_ipc_wait=settings.carbon_messenger_auth_ipc_wait,
                )
            )
        self.messenger_hub = EAMessengerHub(
            messenger_adapters,
            max_frame_size=settings.max_frame_size,
            connection_timeout=settings.connection_timeout,
            live_connections=self.live_connections,
        )
        self.web_gateway = ClassicWebGateway(settings.messenger_public)
        self.endpoint_multiplexer = ClassicEndpointMultiplexer(
            self.messenger_hub.handle_connection,
            self.web_gateway.handle_connection,
        )
        self.messenger_listener = TCPListener(
            settings.messenger_listen,
            self.endpoint_multiplexer.handle_connection,
            name="ea-messenger-shared",
        )
        self.web_listener = TCPListener(
            settings.web_listen,
            self.endpoint_multiplexer.handle_connection,
            name="classic-web-prel",
        )
        self.race_relay = ClassicRaceRelay(
            virtual_network=settings.race_virtual_network,
        )
        self.race_channel_count = max(
            settings.u2_game_max_players if self.enable_u2 else 0,
            MW_MAX_RELAY_PLAYERS if self.enable_mw else 0,
            1,
        )
        for label, configured in (
            ("RACE_LISTEN", settings.race_listen),
            ("RACE_PUBLIC", settings.race_public),
        ):
            if (
                configured.port > 0
                and configured.port + self.race_channel_count - 1 > 65_535
            ):
                raise ValueError(
                    f"{label} port must leave room for "
                    f"{self.race_channel_count} relay channels"
                )

        def listener_endpoint(channel: int) -> Endpoint:
            base_port = settings.race_listen.port
            return Endpoint(
                settings.race_listen.host,
                base_port + channel if base_port else 0,
            )

        self.race_listeners = tuple(
            UDPListener(
                listener_endpoint(channel),
                lambda data, source, channel=channel: self._handle_race_datagram(
                    channel,
                    data,
                    source,
                ),
                name=f"classic-race-relay-channel-{channel}",
            )
            for channel in range(self.race_channel_count)
        )
        # Compatibility aliases retained for callers and tests that inspect
        # the original two-listener runtime directly.
        self.race_listener = self.race_listeners[0]
        self.race_guest_listener = self.race_listeners[
            min(1, len(self.race_listeners) - 1)
        ]
        if self.account_database is not None:
            self.account_policy_monitor = AccountPolicyMonitor(
                SQLiteAccountPolicyEventStore(self.account_database),
                self._handle_account_policy,
                name="classic-account-policy",
            )
        self.runtime_status = RuntimeStatusPublisher(
            Path(__file__).resolve().parents[2] / "runtime" / "classic-status.json",
            self._runtime_status_snapshot,
            name="classic",
        )

    def _runtime_status_snapshot(self) -> dict[str, object]:
        return {
            "component": "classic",
            "games": {
                "underground2": {
                    "enabled": bool(self.enable_u2),
                    "rooms": (
                        self.u2.sessions.status_snapshot() if self.enable_u2 else []
                    ),
                },
                "most_wanted": {
                    "enabled": bool(self.enable_mw),
                    "rooms": (
                        self.mw.sessions.status_snapshot() if self.enable_mw else []
                    ),
                },
            },
        }

    def _handle_account_policy(self, event: AccountPolicyEvent) -> None:
        if not event.restrictive or self.account_database is None:
            return
        identities = self.account_database.identities_for_account(event.account_id)
        user_ids = tuple(identity.user_id for identity in identities)
        u2_result = None
        mw_result = None

        def cleanup_game_state() -> None:
            nonlocal u2_result, mw_result
            u2_result = self.u2.prelogin.enforce_account_policy(
                user_ids,
                reason=event.disconnect_reason,
            )
            mw_result = self.mw.prelogin.enforce_account_policy(
                user_ids,
                reason=event.disconnect_reason,
            )

        transport = self.live_connections.enforce(
            event,
            before_close=cleanup_game_state,
        )
        assert u2_result is not None and mw_result is not None
        log.warning(
            "Classic account policy applied: account=%s action=%s "
            "transports=%d notified=%d closing=%d protocols=%s "
            "u2_games=%d mw_games=%d mw_usersets=%d",
            event.account_name,
            event.action,
            transport.matched,
            transport.notified,
            transport.closing,
            ",".join(transport.protocols) or "none",
            u2_result.games_closed,
            mw_result.games_closed,
            mw_result.usersets_deleted,
        )

    def _endpoint_for_client(self, endpoint: Endpoint, client_ip: str) -> Endpoint:
        """Advertise an endpoint that is reachable from this TCP viewer.

        A LAN viewer can reach ``PUBLIC_HOST`` through NAT loopback and then
        appear to the TCP listener with the server's own public address. The
        race UDP path can still arrive directly from the viewer's LAN address,
        so advertising the public relay in that case makes a connected UDP
        socket reject replies sourced from the server's LAN address. Treat a
        TCP peer equal to the advertised endpoint's resolved public address as
        a hairpin viewer and keep its endpoints on ``LOCAL_ADVERTISE_HOST``.
        """
        try:
            address = ipaddress.ip_address(str(client_ip or "").strip())
        except ValueError:
            return endpoint
        if address.is_loopback:
            return Endpoint("127.0.0.1", endpoint.port)
        if address.is_private or address.is_link_local:
            return Endpoint(self.settings.local_advertise_host, endpoint.port)

        try:
            endpoint_address = ipaddress.ip_address(socket.gethostbyname(endpoint.host))
        except (OSError, ValueError):
            endpoint_address = None
        if endpoint_address is not None and address == endpoint_address:
            local_host = str(self.settings.local_advertise_host or "").strip()
            try:
                local_address = ipaddress.ip_address(local_host)
            except ValueError:
                local_address = None
            if local_address is not None and (
                local_address.is_private
                or local_address.is_link_local
                or local_address.is_loopback
            ):
                log.info(
                    "Classic endpoint hairpin projection: peer=%s public=%s local=%s port=%d",
                    address, endpoint_address, local_host, endpoint.port,
                )
                return Endpoint(local_host, endpoint.port)
        return endpoint

    def _handle_race_datagram(
        self,
        channel: int,
        data: bytes,
        source: tuple[str, int],
    ) -> tuple[()]:
        replies = self.race_relay.handle_channel(
            data,
            source,
            channel,
        )
        for response, target, reply_channel in replies:
            if not 0 <= reply_channel < len(self.race_listeners):
                log.warning(
                    "Classic race relay dropped invalid reply channel: "
                    "source_channel=%d reply_channel=%d channels=%d",
                    channel,
                    reply_channel,
                    len(self.race_listeners),
                )
                continue
            self.race_listeners[reply_channel].send_datagram(response, target)
        if self.enable_mw:
            for game_id, user_id in self.race_relay.drain_mw_settled_links():
                self.mw.prelogin.notify_mw_transport_settled(game_id, user_id)
        return ()

    def _known_personas(self) -> tuple[str, ...]:
        return tuple(
            persona
            for account in self.credentials.accounts()
            for persona in account.all_personas
        )

    def _ranking_persona_visible(self, persona: str) -> bool:
        key = str(persona or "").strip().casefold()
        owners = [
            account
            for account in self.credentials.accounts()
            if key in {value.casefold() for value in account.all_personas}
        ]
        if not owners:
            return True
        return any(
            account.enabled
            and not account.banned
            and not self.credentials.is_email_blocked(account.email)
            for account in owners
        )

    def _resolve_carbon_identity(self, persona: str) -> CarbonIPCIdentity | None:
        if self.account_database is not None:
            record = self.account_database.identity_for_persona(
                persona,
                require_carbon_wire_id=True,
            )
            if record is not None:
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )
        if self.carbon_messenger_state is not None:
            return self.carbon_messenger_state.identity_for_persona(persona)
        return None

    @staticmethod
    def _advertised(configured: Endpoint, bound: Endpoint) -> Endpoint:
        return Endpoint(configured.host, bound.port if configured.port == 0 else configured.port)

    @staticmethod
    def _advertised_race_channel(
        configured: Endpoint,
        bound: Endpoint,
        channel: int,
    ) -> Endpoint:
        return Endpoint(
            configured.host,
            bound.port if configured.port == 0 else configured.port + channel,
        )

    def start(self) -> None:
        with ExitStack() as rollback:
            if self.account_policy_monitor is not None:
                self.account_policy_monitor.start()
                rollback.callback(self.account_policy_monitor.stop)
            ipc_bound = (
                self.carbon_messenger_ipc_receiver.start()
                if self.carbon_messenger_ipc_receiver is not None
                else None
            )
            if self.carbon_messenger_ipc_receiver is not None:
                rollback.callback(self.carbon_messenger_ipc_receiver.stop)
            race_bounds: list[Endpoint] = []
            for listener in self.race_listeners:
                race_bounds.append(listener.start())
                rollback.callback(listener.stop)
            race_public_endpoints = tuple(
                self._advertised_race_channel(
                    self.settings.race_public,
                    bound,
                    channel,
                )
                for channel, bound in enumerate(race_bounds)
            )
            race_public = race_public_endpoints[0]
            self.race_relay.set_public_host(race_public.host)
            if self.enable_u2:
                self.u2.prelogin.set_race_relay(
                    race_public,
                    self.race_relay.register_u2_virtual_game,
                    *race_public_endpoints[1:],
                    unregistrar=self.race_relay.unregister_game,
                )
            if self.enable_mw:
                self.mw.prelogin.set_race_relay(
                    race_public,
                    self.race_relay.register_shared_virtual_game,
                    *race_public_endpoints[1:],
                    unregistrar=self.race_relay.unregister_game,
                    handoff=self.race_relay.handoff_game,
                )
            web_bound = self.web_listener.start()
            rollback.callback(self.web_listener.stop)
            messenger_bound = self.messenger_listener.start()
            rollback.callback(self.messenger_listener.stop)
            messenger_public = self._advertised(self.settings.messenger_public, messenger_bound)
            web_public = self._advertised(self.settings.web_public, web_bound)
            self.web_gateway.set_messenger_public(messenger_public)
            if self.enable_u2:
                self.u2.set_shared_endpoints(messenger_public, web_public)
            if self.enable_mw:
                self.mw.set_shared_endpoints(messenger_public, web_public)
            if self.enable_u2:
                self.u2.start()
                rollback.callback(self.u2.stop)
            if self.enable_mw:
                self.mw.start()
                rollback.callback(self.mw.stop)
            self.runtime_status.start()
            rollback.callback(self.runtime_status.stop)
            if self.web_social_events is not None:
                self.web_social_events.start()
                rollback.callback(self.web_social_events.stop)
            rollback.pop_all()
        log.info(
            "Classic game services enabled: underground2=%d most_wanted=%d",
            int(self.enable_u2),
            int(self.enable_mw),
        )
        log.info(
            "Shared EA Messenger endpoint (HTTP multiplexed) listening on %s:%d, advertised as %s:%d",
            messenger_bound.host,
            messenger_bound.port,
            messenger_public.host,
            messenger_public.port,
        )
        if ipc_bound is not None:
            log.info(
                "Carbon Messenger IPC receiver listening on %s:%d max_age=%.3fs social=sqlite",
                ipc_bound.host,
                ipc_bound.port,
                self.settings.carbon_messenger_ipc_max_age,
            )
        log.info(
            "Shared news/TOS/PREL endpoint (Messenger multiplexed) listening on %s:%d, advertised as %s:%d",
            web_bound.host,
            web_bound.port,
            web_public.host,
            web_public.port,
        )
        log.info(
            "Shared race relay listening on UDP %s ports=%s, advertised as "
            "%s ports=%s channels=%d",
            race_bounds[0].host,
            ",".join(str(endpoint.port) for endpoint in race_bounds),
            race_public.host,
            ",".join(str(endpoint.port) for endpoint in race_public_endpoints),
            len(race_public_endpoints),
        )

    def stop(self) -> None:
        if self.web_social_events is not None:
            self.web_social_events.stop()
        self.runtime_status.stop()
        if self.account_policy_monitor is not None:
            self.account_policy_monitor.stop()
        if self.enable_mw:
            self.mw.stop()
        if self.enable_u2:
            self.u2.stop()
        self.messenger_listener.stop()
        self.web_listener.stop()
        for listener in reversed(self.race_listeners):
            listener.stop()
        if self.carbon_messenger_ipc_receiver is not None:
            self.carbon_messenger_ipc_receiver.stop()
        for registry in self.session_registries:
            try:
                registry.release_all()
            except Exception:
                log.exception("failed to clear shared account sessions during shutdown")
