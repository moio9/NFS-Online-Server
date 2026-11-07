"""Standalone Carbon Messenger host for deployments without Classic games.

Mixed-game deployments continue to host the shared listener with Classic,
which owns the live U2/MW lobby state. The Carbon-only process receives its
session and room state through the same authenticated loopback IPC channel.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
from threading import Event

from carbon.core.config import Endpoint
from carbon.messenger.adapter import CarbonMessengerAdapter
from carbon.messenger.ipc import CarbonIPCIdentity, CarbonMessengerIPCReceiver, CarbonMessengerIPCState
from classic.ea.multiplex import ClassicEndpointMultiplexer
from classic.ea.web import ClassicWebGateway
from common.accounts import SQLiteAccountDatabase
from common.config import load_service_values
from common.enforcement import AccountPolicyEvent, AccountPolicyMonitor, LiveAccountConnectionRegistry, SQLiteAccountPolicyEventStore
from common.messenger import EAMessengerHub
from common.social import SocialService
from common.tcp import TCPListener
from common.web_social import WebSocialEventPump

log = logging.getLogger(__name__)
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / 'config' / 'server.toml'


@dataclass(frozen=True)
class StandaloneSettings:
    messenger_listen: Endpoint
    messenger_public: Endpoint
    web_listen: Endpoint
    ipc_listen: Endpoint
    ipc_secret: str
    account_db: str | None = None
    account_files: str | None = None
    social_data: str | None = None
    max_age: float = 5.0
    heartbeat_interval: float = 30.0
    auth_wait: float = 3.0
    max_frame_size: int = 65535
    connection_timeout: float = 60.0
    busy_timeout_ms: int = 5000

    @classmethod
    def load(cls, path: str | Path) -> 'StandaloneSettings':
        values = load_service_values(path, 'classic', games=('carbon',))
        ipc = values.get('CARBON_MESSENGER_IPC_LISTEN', '').strip()
        secret = values.get('CARBON_MESSENGER_IPC_SECRET', '').strip()
        if not ipc or not secret:
            raise ValueError('Carbon Messenger requires its loopback IPC endpoint and secret')
        db = values.get('ACCOUNT_DB', '').strip() or None
        files = values.get('ACCOUNT_FILES', '').strip() or None
        if bool(db) != bool(files):
            raise ValueError('ACCOUNT_DB and ACCOUNT_FILES must both be set or both be empty')
        settings = cls(
            messenger_listen=Endpoint.parse(values.get('MESSENGER_LISTEN', '0.0.0.0:13505')),
            messenger_public=Endpoint.parse(values.get('MESSENGER_PUBLIC', '127.0.0.1:13505')),
            web_listen=Endpoint.parse(values.get('WEB_LISTEN', '0.0.0.0:20923')),
            ipc_listen=Endpoint.parse(ipc), ipc_secret=secret, account_db=db, account_files=files,
            social_data=values.get('SOCIAL_DATA', '').strip() or None,
            max_age=float(values.get('CARBON_MESSENGER_IPC_MAX_AGE', '5')),
            heartbeat_interval=float(values.get('CARBON_MESSENGER_HEARTBEAT_INTERVAL', '30')),
            auth_wait=float(values.get('CARBON_MESSENGER_AUTH_IPC_WAIT', '3')),
            max_frame_size=int(values.get('MAX_FRAME_SIZE', '65535')),
            connection_timeout=float(values.get('CONNECTION_TIMEOUT', '60')),
            busy_timeout_ms=int(values.get('ACCOUNT_SQLITE_BUSY_TIMEOUT_MS', '5000')),
        )
        if (settings.max_age <= 0 or settings.heartbeat_interval <= 0 or settings.auth_wait <= 0
                or settings.connection_timeout <= 0 or settings.busy_timeout_ms < 0
                or not 12 <= settings.max_frame_size <= 16 * 1024 * 1024):
            raise ValueError('invalid Messenger timeout, frame size or database timeout')
        return settings


class StandaloneMessenger:
    """Own the Messenger, web gateway and Carbon IPC for a Carbon-only run."""

    def __init__(self, settings: StandaloneSettings) -> None:
        self.settings = settings
        self.database = (SQLiteAccountDatabase(settings.account_db, settings.account_files,
                                                busy_timeout_ms=settings.busy_timeout_ms)
                         if settings.account_db and settings.account_files else None)
        self.social = SocialService(settings.social_data, database=self.database,
                                    persona_provider=self._known_personas)
        self.connections = LiveAccountConnectionRegistry(name='standalone-messenger-connections')
        self.state = CarbonMessengerIPCState(max_age_seconds=settings.max_age)
        self.receiver = CarbonMessengerIPCReceiver(settings.ipc_listen, secret=settings.ipc_secret,
                                                    state=self.state)
        self.adapter = CarbonMessengerAdapter(self.state, social=self.social,
                                              identity_resolver=self._resolve_identity,
                                              heartbeat_interval=settings.heartbeat_interval,
                                              auth_ipc_wait=settings.auth_wait)
        self.hub = EAMessengerHub([self.adapter], max_frame_size=settings.max_frame_size,
                                  connection_timeout=settings.connection_timeout,
                                  live_connections=self.connections)
        self.web = ClassicWebGateway(settings.messenger_public)
        self.multiplexer = ClassicEndpointMultiplexer(self.hub.handle_connection, self.web.handle_connection)
        self.messenger_listener = TCPListener(settings.messenger_listen, self.multiplexer.handle_connection,
                                               name='standalone-ea-messenger')
        self.web_listener = TCPListener(settings.web_listen, self.multiplexer.handle_connection,
                                        name='standalone-web-prel')
        self.social_events = (WebSocialEventPump(self.database.path, self.social,
                              visibility_notifier=self.adapter.service._notify_social_presence,
                              invite_sender=self.adapter.send_website_invite)
                              if self.database is not None else None)
        self.policy_monitor = (AccountPolicyMonitor(SQLiteAccountPolicyEventStore(self.database),
                               self._handle_policy, name='messenger-account-policy')
                               if self.database is not None else None)

    def _known_personas(self) -> tuple[str, ...]:
        if self.database is None:
            return tuple(identity.persona for identity in self.state.known_identities())
        return tuple(persona for account in self.database.accounts() for persona in account.personas)

    def _resolve_identity(self, persona: str) -> CarbonIPCIdentity | None:
        if self.database is not None:
            record = self.database.identity_for_persona(persona, require_carbon_wire_id=True)
            if record is not None:
                return CarbonIPCIdentity(record.account_name, record.persona, record.profile_id,
                                         record.user_id, int(record.carbon_wire_player_id or 0))
        return self.state.identity_for_persona(persona)

    def _handle_policy(self, event: AccountPolicyEvent) -> None:
        if event.restrictive:
            self.connections.enforce(event)

    def start(self) -> None:
        with ExitStack() as rollback:
            if self.policy_monitor is not None:
                self.policy_monitor.start()
                rollback.callback(self.policy_monitor.stop)
            self.receiver.start()
            rollback.callback(self.receiver.stop)
            self.web_listener.start()
            rollback.callback(self.web_listener.stop)
            bound = self.messenger_listener.start()
            rollback.callback(self.messenger_listener.stop)
            public = self.settings.messenger_public
            self.web.set_messenger_public(Endpoint(public.host, bound.port if public.port == 0 else public.port))
            if self.social_events is not None:
                self.social_events.start()
                rollback.callback(self.social_events.stop)
            rollback.pop_all()
        log.info('Standalone Carbon Messenger listening on %s:%d', bound.host, bound.port)

    def stop(self) -> None:
        if self.social_events is not None:
            self.social_events.stop()
        if self.policy_monitor is not None:
            self.policy_monitor.stop()
        self.messenger_listener.stop()
        self.web_listener.stop()
        self.receiver.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Run the standalone Carbon Messenger')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    args = parser.parse_args(argv)
    level = getattr(logging, os.environ.get('NFS_LOG_LEVEL', 'INFO').strip().upper(), logging.INFO)
    logging.basicConfig(level=level, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    app = StandaloneMessenger(StandaloneSettings.load(args.config))
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    app.start()
    try:
        stop.wait()
    finally:
        app.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
