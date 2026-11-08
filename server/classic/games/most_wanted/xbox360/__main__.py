"""Experimental password-backed MW Xbox lobby on loopback only.

The Xbox client needs a local authentication bridge. For remote use, forward
these ports through an authenticated encrypted tunnel. This separate runtime
does not yet provide Internet race relay or PC/Xbox cross-platform rooms.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import ipaddress
import logging
from pathlib import Path
import signal
from threading import Event

from classic.app import ClassicOnlineApplication, MW_PRELOGIN
from classic.core.catalog import GameId
from classic.core.config import ClassicGameSettings, Endpoint, ServerSettings
from classic.ea.ranking import ClassicRankingStore
from classic.games.most_wanted.xbox360.auth import create_auth_service
from classic.games.most_wanted.xbox360.clock import xbox_lobby_heartbeat
from classic.games.most_wanted.xbox360.lobby import XboxPrelogin
from classic.protocols.runtime import ClassicGameRuntime


def create_runtime(settings, *, bind_ip="127.0.0.1", directory_port=30960, lobby_port=10901):
    if not ipaddress.ip_address(bind_ip).is_loopback:
        raise ValueError("Xbox service must bind to loopback; use an encrypted tunnel")
    if settings.auth_mode != "password" or not settings.account_db_path or not settings.account_files_path:
        raise ValueError("Xbox service requires password authentication and the shared account database")
    if not (0 <= int(directory_port) <= 65535 and 0 <= int(lobby_port) <= 65535):
        raise ValueError("invalid Xbox service port")
    app = ClassicOnlineApplication(settings)
    # Xbox stat fields are not yet reconciled with the PC schema. Keep these
    # experiments in memory until the wire mapping and results are verified.
    xbox_ranking = ClassicRankingStore()
    bootstrap = Endpoint(bind_ip, int(directory_port))
    lobby = Endpoint(bind_ip, int(lobby_port))
    xbox_prelogin_profile = replace(MW_PRELOGIN, lobby_heartbeat_wire=xbox_lobby_heartbeat)
    runtime = ClassicGameRuntime(
        ClassicGameSettings(GameId.MOST_WANTED, bootstrap, bootstrap, lobby, lobby),
        credentials=app.credentials, identities=app.identities, social=app.social,
        ranking=xbox_ranking, auth_factory=create_auth_service, prelogin_profile=xbox_prelogin_profile,
        messenger_public=settings.messenger_public, web_public=settings.web_public,
        max_frame_size=settings.max_frame_size, connection_timeout=settings.connection_timeout,
        directory_ttl=settings.classic_directory_ttl,
        lobby_idle_timeout=settings.classic_lobby_idle_timeout,
        lobby_heartbeat_interval=settings.classic_lobby_heartbeat_interval,
        verify_passwords=True, auto_enroll=False,
        active_sessions=app.mw.active_sessions, live_connections=app.live_connections,
    )
    runtime.prelogin = XboxPrelogin(
        runtime.auth, profile=xbox_prelogin_profile, control_endpoint=settings.messenger_public,
        web_endpoint=settings.web_public, sessions=runtime.sessions,
        ranking=xbox_ranking, social=app.social,
    )
    return app, runtime


def main():
    root = Path(__file__).resolve().parents[5]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(root / "config" / "server.toml"))
    parser.add_argument("--bind-ip", default="127.0.0.1")
    parser.add_argument("--directory-port", type=int, default=30960)
    parser.add_argument("--lobby-port", type=int, default=10901)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    settings = ServerSettings.load(args.config, games="mw")
    _app, runtime = create_runtime(settings, bind_ip=args.bind_ip,
                                   directory_port=args.directory_port, lobby_port=args.lobby_port)
    done = Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    runtime.start()
    try:
        done.wait()
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
