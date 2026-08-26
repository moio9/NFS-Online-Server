"""Trusted local administration for shared EA login credentials."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from classic.accounts.credentials import CredentialStore
from common.accounts import SQLiteAccountDatabase
from classic.accounts.sqlite_backend import SQLiteCredentialStore
from classic.core.config import ServerSettings


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
    parser = argparse.ArgumentParser(description="Manage shared EA login credentials")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a password account")
    create.add_argument("account")
    create.add_argument("--persona")
    create.add_argument("--email", default="")
    create.add_argument(
        "--alias",
        action="append",
        default=[],
        help="additional login identifier; repeat for multiple aliases",
    )
    create.add_argument(
        "--extra-persona",
        action="append",
        default=[],
        help="additional persona; repeat for multiple personas",
    )
    create.add_argument("--password", help="avoid on shared shells; omit to prompt securely")

    password = sub.add_parser("password", help="replace an account password")
    password.add_argument("account")
    password.add_argument("--password", help="avoid on shared shells; omit to prompt securely")

    enabled = sub.add_parser("enabled", help="enable or disable an account")
    enabled.add_argument("account")
    enabled.add_argument("state", choices=("on", "off"))

    ban = sub.add_parser("ban", help="ban an account")
    ban.add_argument("account")

    unban = sub.add_parser("unban", help="remove an account ban")
    unban.add_argument("account")

    kick = sub.add_parser("kick", help="disconnect an account without banning it")
    kick.add_argument("account")

    ban_email = sub.add_parser("ban-email", help="block an email address")
    ban_email.add_argument("email")

    unban_email = sub.add_parser("unban-email", help="unblock an email address")
    unban_email.add_argument("email")

    sub.add_parser("list", help="list configured accounts")

    args = parser.parse_args()
    settings = ServerSettings.load(args.config)
    database: SQLiteAccountDatabase | None = None
    if settings.account_db_path is not None and settings.account_files_path is not None:
        database = SQLiteAccountDatabase(
            settings.account_db_path,
            settings.account_files_path,
            failure_limit=settings.auth_failure_limit,
            lockout_seconds=settings.auth_lockout_seconds,
            busy_timeout_ms=settings.account_sqlite_busy_timeout_ms,
        )
        store = SQLiteCredentialStore(
            database
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
                email=args.email,
                aliases=tuple(args.alias),
                personas=tuple(args.extra_persona),
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
        if args.command in {"ban", "unban"}:
            account = store.set_banned(args.account, args.command == "ban")
            print(f"account={account.account_name} banned={int(account.banned)}")
            return 0
        if args.command == "kick":
            if database is None:
                parser.error("kick requires shared ACCOUNT_DB/ACCOUNT_FILES")
            account = database.kick(args.account)
            print(f"kicked: account={account.account_name}")
            return 0
        if args.command in {"ban-email", "unban-email"}:
            email = store.set_email_blocked(
                args.email,
                args.command == "ban-email",
            )
            print(
                f"email={email} blocked={int(args.command == 'ban-email')}"
            )
            return 0
        if args.command == "list":
            for account in store.accounts():
                print(
                    f"account={account.account_name} persona={account.persona} "
                    f"enabled={int(account.enabled)} banned={int(account.banned)} "
                    f"email={account.email or '-'}"
                )
            for email in store.blocked_emails():
                print(f"blocked_email={email}")
            return 0
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
