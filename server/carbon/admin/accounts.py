"""Local command-line administration for Carbon account progression."""

from __future__ import annotations

import argparse
from pathlib import Path

from carbon.core.config import ServerSettings
from carbon.progression import (
    BEAT_MODERATOR_STAT,
    CarbonProgressionStore,
    EA_MODERATOR_ROLE,
    VIRUS_STATS,
)


DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "server.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Carbon moderator and viral vinyl flags")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    moderator = sub.add_parser("moderator", help="enable or disable EA moderator status")
    moderator.add_argument("account")
    moderator.add_argument("state", choices=("on", "off"))

    virus = sub.add_parser("virus", help="grant or revoke a viral vinyl flag")
    virus.add_argument("account")
    virus.add_argument("name", choices=VIRUS_STATS)
    virus.add_argument("state", choices=("on", "off"), nargs="?", default="on")

    beat = sub.add_parser("beat-moderator", help="grant or revoke Beat_Moderator")
    beat.add_argument("account")
    beat.add_argument("state", choices=("on", "off"), nargs="?", default="on")

    args = parser.parse_args()
    settings = ServerSettings.load(args.config)
    if not settings.account_data_path:
        parser.error("ACCOUNT_DATA must be configured for persistent account administration")
    store = CarbonProgressionStore(settings.account_data_path)

    if args.command == "moderator":
        store.set_role(args.account, EA_MODERATOR_ROLE, args.state == "on")
        print(f"{args.account}: Moderator={'1.0' if args.state == 'on' else '0.0'}")
        return 0
    if args.command == "virus":
        store.set_stat(args.account, args.name, 1.0 if args.state == "on" else 0.0)
        print(f"{args.account}: {args.name}={'1.0' if args.state == 'on' else '0.0'}")
        return 0
    if args.command == "beat-moderator":
        store.set_stat(args.account, BEAT_MODERATOR_STAT, 1.0 if args.state == "on" else 0.0)
        print(f"{args.account}: {BEAT_MODERATOR_STAT}={'1.0' if args.state == 'on' else '0.0'}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
