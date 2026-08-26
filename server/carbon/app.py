"""Runnable Carbon FESL application composed from clean services."""

from __future__ import annotations

from contextlib import ExitStack
import logging
from pathlib import Path
import socket
from threading import Event

from carbon.accounts.credentials import CredentialStore
from carbon.accounts.identity import Identity, IdentityStore
from common.accounts import SQLiteAccountDatabase, SQLiteSessionRegistry
from common.enforcement import (
    AccountPolicyEvent,
    AccountPolicyMonitor,
    LiveAccountConnectionRegistry,
    SQLiteAccountPolicyEventStore,
)
from common.runtime_status import RuntimeStatusPublisher
from carbon.accounts.sqlite_backend import SQLiteCredentialStore, SQLiteIdentityStore
from carbon.core.catalog import GameId
from carbon.core.config import Endpoint, ServerSettings
from carbon.core.tcp import TCPListener
from carbon.core.udp import UDPListener
from carbon.dlc import CarbonDLCInventory
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService
from carbon.fesl.sqlite_blob import SQLiteCarbonBlobStore
from carbon.theater.service import CarbonTheaterService
from carbon.messenger_ipc import CarbonMessengerIPCPublisher
from carbon.theater.directory import CarbonGameDirectory
from carbon.rebroadcaster.service import CarbonRebroadcasterService
from carbon.runtime.fesl import handle_fesl_connection
from carbon.runtime.theater import handle_theater_connection
from carbon.progression import CarbonProgressionStore
from carbon.mad.service import CarbonMADService
from carbon.web.dlc_store import CarbonDLCStoreServer


log = logging.getLogger(__name__)


def _resolve_race_public_endpoint(endpoint: Endpoint) -> Endpoint:
    """Return the numeric IPv4 endpoint expected by Carbon's EGEG fields."""
    try:
        socket.inet_aton(endpoint.host)
    except OSError:
        try:
            addresses = socket.getaddrinfo(
                endpoint.host,
                endpoint.port,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
        except socket.gaierror as exc:
            raise ValueError(
                f"cannot resolve public race endpoint {endpoint.host!r} to IPv4"
            ) from exc
        if not addresses:
            raise ValueError(
                f"cannot resolve public race endpoint {endpoint.host!r} to IPv4"
            )
        resolved = Endpoint(str(addresses[0][4][0]), endpoint.port)
        log.info(
            "Carbon public race endpoint resolved: host=%s ipv4=%s port=%d",
            endpoint.host,
            resolved.host,
            resolved.port,
        )
        return resolved
    return endpoint


class CarbonApplication:
    def __init__(self, settings: ServerSettings) -> None:
        if settings.game is not GameId.CARBON:
            raise ValueError(f"CarbonApplication cannot serve {settings.game.value}")
        self.settings = settings
        self.live_connections = LiveAccountConnectionRegistry(
            name="carbon-live-connections"
        )
        self.account_database: SQLiteAccountDatabase | None = None
        self.account_sessions: SQLiteSessionRegistry | None = None
        self.account_policy_monitor: AccountPolicyMonitor | None = None
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
            self.account_sessions = SQLiteSessionRegistry(
                self.account_database,
                game=GameId.CARBON.value,
                lease_seconds=settings.account_session_lease_seconds,
            )
            log.info(
                "Shared account SQLite enabled: db=%s files=%s lease=%.1fs",
                self.account_database.path,
                self.account_database.user_root,
                settings.account_session_lease_seconds,
            )
        else:
            log.warning(
                "Shared ACCOUNT_DB/ACCOUNT_FILES are disabled; Carbon is using "
                "JSON-local accounts/Blob metadata and may diverge from Classic"
            )
            self.identities = IdentityStore()
            self.credentials = CredentialStore(
                settings.auth_data_path,
                auto_enroll=settings.auth_auto_enroll,
                failure_limit=settings.auth_failure_limit,
                lockout_seconds=settings.auth_lockout_seconds,
            )
        self.progression = CarbonProgressionStore(settings.account_data_path)
        if settings.carbon_dlc_catalog_path is None:
            raise ValueError("CARBON_DLC_CATALOG cannot be empty")
        if settings.carbon_dlc_assignments_path is None:
            raise ValueError("CARBON_DLC_ASSIGNMENTS cannot be empty")
        self.dlc_inventory = CarbonDLCInventory.from_paths(
            settings.carbon_dlc_catalog_path,
            settings.carbon_dlc_assignments_path,
        )
        self.dlc_store: CarbonDLCStoreServer | None = None
        if settings.carbon_dlc_store_enabled:
            if self.account_database is None:
                raise ValueError(
                    "Carbon DLC Store requires shared ACCOUNT_DB/ACCOUNT_FILES"
                )
            if settings.carbon_dlc_store_listen is None:
                raise ValueError("Carbon DLC Store requires DLC_STORE_LISTEN")
            self.dlc_store = CarbonDLCStoreServer(
                settings.carbon_dlc_store_listen,
                self.account_database,
                self.dlc_inventory,
                session_seconds=settings.carbon_dlc_store_session_seconds,
                cookie_secure=settings.carbon_dlc_store_cookie_secure,
            )
        race_public = _resolve_race_public_endpoint(settings.race_public)
        self.games = CarbonGameDirectory(
            race_public,
            local_race_endpoint=settings.race_local,
            player_id_resolver=self.identities.wire_player_id,
            challenge_quick_join_before_ready=(
                settings.carbon_challenge_quick_join_before_ready
            ),
            challenge_quick_join_after_ready=(
                settings.carbon_challenge_quick_join_after_ready
            ),
        )
        self.fesl = CarbonFESLService(
            CarbonEndpoints(
                settings.messenger_public.host,
                settings.messenger_public.port,
                settings.theater_public.host,
                settings.theater_public.port,
            ),
            self.identities,
            self.games,
            self.progression,
            blobs=(
                SQLiteCarbonBlobStore(self.account_database)
                if self.account_database is not None
                else None
            ),
            credentials=self.credentials,
            authentication_mode=settings.auth_mode,
            activity_timeout_seconds=settings.fesl_activity_timeout,
            login_error_probe_code=settings.auth_login_error_probe_code,
            dlc_inventory=self.dlc_inventory,
            active_sessions=self.account_sessions,
        )
        self.messenger_ipc = (
            CarbonMessengerIPCPublisher(
                settings.messenger_ipc_endpoint,
                secret=settings.messenger_ipc_secret,
                identities=self.identities,
                games=self.games,
                # With shared SQLite, Classic resolves offline buddy identities
                # directly from the database.  Avoid scanning every account at
                # the IPC poll rate; only active sessions travel over IPC.
                known_identities=(
                    self._known_messenger_identities
                    if self.account_database is None
                    else (lambda: ())
                ),
                poll_interval=settings.messenger_ipc_poll_interval,
                heartbeat_interval=settings.messenger_ipc_heartbeat_interval,
            )
            if settings.messenger_ipc_endpoint is not None
            else None
        )
        self.rebroadcaster = CarbonRebroadcasterService(
            self.games,
            self.progression,
            join_timeout_seconds=settings.carbon_join_timeout_seconds,
            race_idle_timeout_seconds=settings.carbon_race_idle_timeout_seconds,
            loading_ready_fallback_seconds=(
                settings.carbon_loading_ready_fallback_seconds
            ),
        )
        self.theater = CarbonTheaterService(
            self.identities,
            self.games,
            leave_handler=self.rebroadcaster.drop_participant,
        )
        self.mad = (
            CarbonMADService(
                settings.mad_public,
                campaign_path=settings.mad_campaigns_path,
                rotation_seconds=settings.mad_rotation_seconds,
                session_timeout_seconds=settings.mad_session_timeout_seconds,
                impression_log_path=settings.mad_impression_log_path,
            )
            if settings.mad_public is not None
            else None
        )
        self.listener = TCPListener(
            settings.fesl_listen,
            self._handle_fesl_connection,
            name="carbon-fesl",
        )
        theater_listen = settings.theater_listen or settings.theater_public
        self.theater_listener = TCPListener(
            theater_listen,
            self._handle_theater_connection,
            name="carbon-theater",
        )
        race_listen = settings.race_listen or settings.race_public
        # The retail GameManager processes Join before accepting the following
        # session descriptor bundle.  The official create capture spaces those
        # two UDP replies by roughly 12 ms; sending them back-to-back in one
        # listener tick leaves Carbon in its empty-ACK retry loop.
        self.race_listener = UDPListener(
            race_listen,
            self.rebroadcaster.handle_datagram,
            name="carbon-race",
            reply_spacing_seconds=self.rebroadcaster.reply_spacing_seconds_for,
            isolate_reply_targets=True,
            poll_handler=self.rebroadcaster.poll_retries,
        )
        self.mad_listener = (
            TCPListener(
                settings.mad_listen,
                self.mad.handle_connection,
                name="carbon-mad",
            )
            if settings.mad_listen is not None and self.mad is not None
            else None
        )
        if self.account_database is not None:
            self.account_policy_monitor = AccountPolicyMonitor(
                SQLiteAccountPolicyEventStore(self.account_database),
                self._handle_account_policy,
                name="carbon-account-policy",
            )
        self.runtime_status = RuntimeStatusPublisher(
            Path(__file__).resolve().parents[2] / "runtime" / "carbon-status.json",
            self._runtime_status_snapshot,
            name="carbon",
        )

    def _runtime_status_snapshot(self) -> dict[str, object]:
        race_state = self.rebroadcaster.status_snapshot()
        rooms = self.games.status_snapshot()
        for room in rooms:
            state = race_state.get(str(room.get("id", "")), {})
            room["race_phase"] = str(state.get("phase", "SESSION_SETUP"))
            room["room_access"] = str(state.get("room_access", "OPEN"))
        return {
            "component": "carbon",
            "games": {
                "carbon": {
                    "enabled": True,
                    "rooms": rooms,
                }
            },
        }

    def _handle_account_policy(self, event: AccountPolicyEvent) -> None:
        if not event.restrictive or self.account_database is None:
            return
        identities = self.account_database.identities_for_account(event.account_id)
        removed_rooms = 0

        def cleanup_game_state() -> None:
            nonlocal removed_rooms
            removed_rooms = sum(
                self.rebroadcaster.force_disconnect_user(
                    identity.user_id,
                    reason=event.disconnect_reason,
                )
                for identity in identities
            )

        transport = self.live_connections.enforce(
            event,
            before_close=cleanup_game_state,
        )
        log.warning(
            "Carbon account policy applied: account=%s action=%s "
            "transports=%d notified=%d closing=%d protocols=%s rooms=%d",
            event.account_name,
            event.action,
            transport.matched,
            transport.notified,
            transport.closing,
            ",".join(transport.protocols) or "none",
            removed_rooms,
        )

    def _known_messenger_identities(self):
        if self.account_database is None:
            return self.progression.known_identities()
        return tuple(
            Identity(
                record.account_name,
                record.persona,
                record.profile_id,
                record.user_id,
            )
            for record in self.account_database.personas()
        )

    def start(self) -> Endpoint:
        with ExitStack() as rollback:
            if self.account_policy_monitor is not None:
                self.account_policy_monitor.start()
                rollback.callback(self.account_policy_monitor.stop)
            if self.settings.auth_login_error_probe_code is not None:
                log.warning(
                    "Carbon AUTH_LOGIN_ERROR_PROBE_CODE=%d rejects every Login "
                    "request for client UI testing",
                    self.settings.auth_login_error_probe_code,
                )
            if self.settings.auth_mode == "open":
                log.warning(
                    "Carbon AUTH_MODE=open permits persona impersonation; "
                    "use AUTH_MODE=password before exposing account or Blob "
                    "services to untrusted clients"
                )
            if self.messenger_ipc is not None:
                self.messenger_ipc.start()
                rollback.callback(self.messenger_ipc.stop)
            fesl_endpoint = self.listener.start()
            rollback.callback(self.listener.stop)
            theater_endpoint = self.theater_listener.start()
            rollback.callback(self.theater_listener.stop)
            race_endpoint = self.race_listener.start()
            rollback.callback(self.race_listener.stop)
            mad_endpoint: Endpoint | None = None
            if self.mad_listener is not None and self.mad is not None:
                self.mad.start()
                rollback.callback(self.mad.stop)
                mad_endpoint = self.mad_listener.start()
                rollback.callback(self.mad_listener.stop)
            dlc_store_endpoint: Endpoint | None = None
            if self.dlc_store is not None:
                dlc_store_endpoint = self.dlc_store.start()
                rollback.callback(self.dlc_store.stop)
            self.runtime_status.start()
            rollback.callback(self.runtime_status.stop)
            rollback.pop_all()
        log.info("Carbon FESL listening on %s:%d", fesl_endpoint.host, fesl_endpoint.port)
        log.info(
            "Carbon Messenger delegated to shared endpoint %s:%d ipc=%s",
            self.settings.messenger_public.host,
            self.settings.messenger_public.port,
            self.settings.messenger_ipc_endpoint,
        )
        log.info("Carbon Theater listening on %s:%d", theater_endpoint.host, theater_endpoint.port)
        log.info(
            "Carbon race/rebroadcaster listening on %s:%d",
            race_endpoint.host,
            race_endpoint.port,
        )
        if mad_endpoint is not None:
            log.info(
                "Carbon Massive Ads listening on %s:%d",
                mad_endpoint.host,
                mad_endpoint.port,
            )
        if dlc_store_endpoint is not None:
            log.info(
                "Carbon DLC Store listening on http://%s:%d/dlc",
                dlc_store_endpoint.host,
                dlc_store_endpoint.port,
            )
        assignments = self.dlc_inventory.current_assignments()
        log.info(
            "Carbon DLC catalog loaded: groups=%d unique_tokens=%d accounts=%d personas=%d",
            len(self.dlc_inventory.catalog.groups),
            len(self.dlc_inventory.catalog.all_tokens()),
            len(assignments.accounts),
            len(assignments.personas),
        )
        log.info(
            "Carbon Challenge Quick Join policy: before_ready=%s after_ready=%s",
            (
                "enabled"
                if self.settings.carbon_challenge_quick_join_before_ready
                else "invite-only"
            ),
            (
                "enabled"
                if self.settings.carbon_challenge_quick_join_after_ready
                else "invite-only"
            ),
        )
        return fesl_endpoint

    def stop(self) -> None:
        self.runtime_status.stop()
        if self.dlc_store is not None:
            self.dlc_store.stop()
        if self.account_policy_monitor is not None:
            self.account_policy_monitor.stop()
        if self.mad_listener is not None:
            self.mad_listener.stop()
        if self.mad is not None:
            self.mad.stop()
        self.race_listener.stop()
        self.theater_listener.stop()
        self.listener.stop()
        if self.messenger_ipc is not None:
            self.messenger_ipc.stop()
        if self.account_sessions is not None:
            try:
                self.account_sessions.release_all()
            except Exception:
                log.exception("failed to clear shared Carbon account sessions during shutdown")

    def _handle_fesl_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        handle_fesl_connection(
            conn,
            addr,
            stop_event,
            settings=self.settings,
            service=self.fesl,
            live_connections=self.live_connections,
        )

    def _handle_theater_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        handle_theater_connection(
            conn,
            addr,
            stop_event,
            settings=self.settings,
            service=self.theater,
            live_connections=self.live_connections,
        )
