"""Trusted local administration for Carbon DLC assignments."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from carbon.core.config import ServerSettings
from carbon.dlc import CarbonDLCConfigError, CarbonDLCInventory
from common.accounts import SQLiteAccountDatabase


DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "server.toml"


def _database(settings: ServerSettings) -> SQLiteAccountDatabase:
    if settings.account_db_path is None or settings.account_files_path is None:
        raise ValueError("ACCOUNT_DB and ACCOUNT_FILES are required for per-account DLC")
    return SQLiteAccountDatabase(
        settings.account_db_path,
        settings.account_files_path,
        failure_limit=settings.auth_failure_limit,
        lockout_seconds=settings.auth_lockout_seconds,
        busy_timeout_ms=settings.account_sqlite_busy_timeout_ms,
    )


def _inventory(settings: ServerSettings) -> CarbonDLCInventory:
    if not settings.carbon_dlc_catalog_path or not settings.carbon_dlc_assignments_path:
        raise ValueError("CARBON_DLC_CATALOG and CARBON_DLC_ASSIGNMENTS are required")
    inventory = CarbonDLCInventory.from_paths(
        settings.carbon_dlc_catalog_path,
        settings.carbon_dlc_assignments_path,
    )
    if inventory.assignment_store is None:
        raise ValueError("Carbon DLC assignment store is unavailable")
    return inventory


def _canonical_account(database: SQLiteAccountDatabase, identifier: str) -> str:
    record = database.resolve_account(identifier)
    if record is None:
        raise KeyError(f"account not found: {identifier}")
    return record.account_name


def _unlock(current: Sequence[str], additions: Sequence[str]) -> tuple[str, ...]:
    result = [] if tuple(current) == ("none",) else list(current)
    for raw in additions:
        selector = str(raw).strip().casefold()
        if selector == "all":
            return ("all",)
        if selector == "none":
            continue
        negative = f"-{selector}"
        result = [item for item in result if item not in {negative, "none"}]
        if selector not in result:
            result.append(selector)
    return tuple(result) or ("none",)


def _lock(current: Sequence[str], removals: Sequence[str]) -> tuple[str, ...]:
    result = list(current)
    for raw in removals:
        selector = str(raw).strip().casefold()
        if selector in {"all", "none"}:
            return ("none",)
        negative = f"-{selector}"
        result = [item for item in result if item not in {selector, negative, "none"}]
        result.append(negative)
    return tuple(result) or ("none",)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage free Carbon DLC assignments per account"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list available DLC groups")
    listing.add_argument("--category", default="")

    show = sub.add_parser("show", help="show one account's effective DLC")
    show.add_argument("account")

    set_command = sub.add_parser("set", help="replace one account's DLC selection")
    set_command.add_argument("account")
    set_command.add_argument("selectors", nargs="+")

    unlock = sub.add_parser("unlock", aliases=["add"], help="unlock DLC selectors")
    unlock.add_argument("account")
    unlock.add_argument("selectors", nargs="+")

    lock = sub.add_parser("lock", aliases=["remove"], help="remove DLC selectors")
    lock.add_argument("account")
    lock.add_argument("selectors", nargs="+")

    all_command = sub.add_parser("all", help="unlock every catalog DLC")
    all_command.add_argument("account")

    none_command = sub.add_parser("none", help="disable every DLC for an account")
    none_command.add_argument("account")

    reset = sub.add_parser("reset", help="return an account to the default selection")
    reset.add_argument("account")

    args = parser.parse_args(argv)
    try:
        settings = ServerSettings.load(args.config)
        inventory = _inventory(settings)
        store = inventory.assignment_store
        assert store is not None

        if args.command == "list":
            category = str(args.category or "").strip().casefold()
            groups = sorted(
                inventory.catalog.groups.values(),
                key=lambda group: (group.category, group.label.casefold(), group.key),
            )
            for group in groups:
                if category and group.category != category:
                    continue
                print(
                    f"{group.key}\tcategory={group.category}\t"
                    f"tokens={len(group.tokens)}\tFREE\t{group.label}"
                )
            return 0

        database = _database(settings)
        account = _canonical_account(database, args.account)
        key = account.casefold()

        if args.command == "show":
            assignments = store.current()
            selectors = store.selectors_for_account(account)
            groups = store.effective_group_keys(account)
            tokens = inventory.catalog.expand(selectors)
            source = "account" if key in assignments.accounts else "default"
            print(f"account={account}")
            print(f"source={source}")
            print("selectors=" + ",".join(selectors))
            print(f"groups={len(groups)}")
            print(f"tokens={len(tokens)}")
            for group_key in groups:
                print(f"group={group_key}")
            return 0

        if args.command == "reset":
            store.reset_account(account)
        elif args.command == "all":
            store.set_account(account, ("all",))
        elif args.command == "none":
            store.set_account(account, ("none",))
        elif args.command == "set":
            store.set_account(account, tuple(args.selectors))
        elif args.command in {"unlock", "add"}:
            store.set_account(
                account,
                _unlock(store.selectors_for_account(account), args.selectors),
            )
        elif args.command in {"lock", "remove"}:
            store.set_account(
                account,
                _lock(store.selectors_for_account(account), args.selectors),
            )
        else:  # pragma: no cover - argparse guarantees the command
            return 2

        selectors = store.selectors_for_account(account)
        groups = store.effective_group_keys(account)
        tokens = inventory.catalog.expand(selectors)
        print(
            f"DLC updated: account={account} selectors={','.join(selectors)} "
            f"groups={len(groups)} tokens={len(tokens)}"
        )
        return 0
    except (CarbonDLCConfigError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
