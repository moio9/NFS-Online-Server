"""Trusted local administration for Carbon login credentials."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from carbon.accounts.credentials import CredentialStore
from common.accounts import SQLiteAccountDatabase
from carbon.accounts.sqlite_backend import SQLiteCredentialStore
from carbon.core.config import ServerSettings


def _password(value: str | None, *, confirmation: bool) -> str:
    if value is not None:
        secret = str(value)
    else:
        secret = getpass("Password: ")
        if confirmation and secret != getpass("Repeat password: "):
            raise ValueError("passwords do not match")
    if not secret:
        raise ValueError("password must not be empty")
    return secret


DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "server.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Carbon FESL login credentials")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a password account")
    create.add_argument("account")
    create.add_argument("--persona")
    create.add_argument("--password", help="avoid on shared shells; omit to prompt securely")

    password = sub.add_parser("password", help="replace an account password")
    password.add_argument("account")
    password.add_argument("--password", help="avoid on shared shells; omit to prompt securely")

    enabled = sub.add_parser("enabled", help="enable or disable an account")
    enabled.add_argument("account")
    enabled.add_argument("state", choices=("on", "off"))

    sub.add_parser("list", help="list configured accounts")

    args = parser.parse_args()
    settings = ServerSettings.load(args.config)
    if settings.account_db_path is not None and settings.account_files_path is not None:
        store = SQLiteCredentialStore(
            SQLiteAccountDatabase(
                settings.account_db_path,
                settings.account_files_path,
                failure_limit=settings.auth_failure_limit,
                lockout_seconds=settings.auth_lockout_seconds,
                busy_timeout_ms=settings.account_sqlite_busy_timeout_ms,
            )
        )
    else:
        if not settings.auth_data_path:
            parser.error("AUTH_DATA must be configured for password authentication")
        store = CredentialStore(
            settings.auth_data_path,
            failure_limit=settings.auth_failure_limit,
            lockout_seconds=settings.auth_lockout_seconds,
        )

    try:
        if args.command == "create":
            account = store.create_account(
                args.account,
                _password(args.password, confirmation=args.password is None),
                persona=args.persona,
            )
            print(f"created: account={account.account_name} persona={account.persona}")
            return 0
        if args.command == "password":
            account = store.set_password(
                args.account,
                _password(args.password, confirmation=args.password is None),
            )
            print(f"password updated: account={account.account_name}")
            return 0
        if args.command == "enabled":
            account = store.set_enabled(args.account, args.state == "on")
            print(f"account={account.account_name} enabled={int(account.enabled)}")
            return 0
        if args.command == "list":
            for account in store.accounts():
                print(
                    f"account={account.account_name} persona={account.persona} "
                    f"enabled={int(account.enabled)}"
                )
            return 0
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
