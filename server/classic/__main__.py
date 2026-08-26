"""Command-line entry point for the Underground 2 / Most Wanted service."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
from threading import Event

from classic import BUILD_PROFILE, BUILD_VERSION
from classic.app import ClassicOnlineApplication
from classic.core.config import ServerSettings


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "server.toml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Need for Speed Underground 2 + Most Wanted online service"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--games",
        default=os.environ.get("NFS_GAMES"),
        help="u2,mw; normally supplied by nfs_online.py",
    )
    args = parser.parse_args()

    settings = ServerSettings.load(args.config, games=args.games)
    level_name = os.environ.get("NFS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logging.getLogger("classic.build").info(
        "Classic service %s (%s), U2=%s, MW=%s",
        BUILD_VERSION,
        BUILD_PROFILE,
        "enabled" if settings.enable_u2 else "disabled",
        "enabled" if settings.enable_mw else "disabled",
    )

    app = ClassicOnlineApplication(settings)
    done = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        done.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    app.start()
    try:
        done.wait()
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
