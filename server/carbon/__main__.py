"""Command-line entry point for the Carbon service."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
from threading import Event

from carbon import BUILD_PROFILE, BUILD_VERSION
from carbon.core.catalog import GameId
from carbon.core.config import ServerSettings
from carbon.app import CarbonApplication


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "server.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Need for Speed Carbon online service")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    settings = ServerSettings.load(args.config)
    if settings.game is not GameId.CARBON:
        parser.error(f"adapter not implemented: {settings.game.value}")

    level_name = os.environ.get("NFS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logging.getLogger("carbon.build").info(
        "Carbon service %s (%s), auth=%s, messenger=shared-ipc",
        BUILD_VERSION,
        BUILD_PROFILE,
        settings.auth_mode,
    )

    app = CarbonApplication(settings)
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
