"""Trusted local administration for persistent U2/MW player statistics."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from classic.core.catalog import GameId
from classic.ea.ranking import ClassicPlayerStats, ClassicRankingStore
from classic.ea.sqlite_ranking import SQLiteClassicRankingStore
from common.accounts import SQLiteAccountDatabase
from common.config import (
    load_configuration,
    looks_like_sectioned_ini,
    package_root_for,
    read_flat_config,
    resolve_package_path,
)


DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "server.toml"


GAME_NAMES = {
    "mw": GameId.MOST_WANTED,
    "most_wanted": GameId.MOST_WANTED,
    "u2": GameId.UNDERGROUND2,
    "underground2": GameId.UNDERGROUND2,
}

MW_CATEGORIES = {
    "total": 0,
    "overall": 0,
    "aggregate": 0,
    "circuit": 1,
    "sprint": 2,
    "drag": 3,
    "reserved": 4,
    "unused": 4,
    "drift": 4,
}

MW_CATEGORY_LABELS = {
    0: "total",
    1: "circuit",
    2: "sprint",
    3: "drag",
    4: "reserved",
}

U2_CATEGORY_LABELS = {
    0: "circuit",
    1: "sprint",
    2: "drag",
    3: "drift",
    4: "streetx",
    5: "url",
}

U2_CATEGORIES = {
    **{label: category for category, label in U2_CATEGORY_LABELS.items()},
    "street_x": 4,
    "street-x": 4,
    "streetcross": 4,
    "street_cross": 4,
}


class StatsArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose errors remain easy to test and understand."""


def _game(value: str) -> GameId:
    try:
        return GAME_NAMES[str(value).strip().casefold()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("game must be mw or u2") from exc


def _category(value: str) -> str | int:
    text = str(value).strip().casefold()
    if text in MW_CATEGORIES or text in U2_CATEGORIES:
        return text
    try:
        number = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "category must be total, circuit, sprint, drag, drift, streetx, url, or 0..5"
        ) from exc
    if not 0 <= number <= 5:
        raise argparse.ArgumentTypeError("category index must be between 0 and 5")
    return number


def _resolve_category(game: GameId, value: str | int) -> int:
    if isinstance(value, int):
        maximum = 5 if game is GameId.UNDERGROUND2 else 4
        if 0 <= value <= maximum:
            return value
        raise ValueError(f"category index must be between 0 and {maximum}")
    categories = U2_CATEGORIES if game is GameId.UNDERGROUND2 else MW_CATEGORIES
    try:
        return categories[value]
    except KeyError as exc:
        raise ValueError(f"category {value!r} is not valid for {game.value}") from exc


def _storage_values(config_path: str | Path) -> dict[str, str]:
    """Read only storage settings; do not validate unrelated network/IPC state."""

    path = Path(config_path).expanduser().resolve()
    if not looks_like_sectioned_ini(path):
        return read_flat_config(path)

    configuration = load_configuration(path)
    global_values = configuration["global"]
    root = package_root_for(path)
    return {
        "ACCOUNT_DB": str(resolve_package_path(root, global_values["ACCOUNT_DB"])),
        "ACCOUNT_FILES": str(resolve_package_path(root, global_values["ACCOUNT_FILES"])),
        "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS": global_values["ACCOUNT_SQLITE_BUSY_TIMEOUT_MS"],
        "STATS_DATA": str((root / "data" / "classic" / "stats.json").resolve()),
    }


def _configured_path(value: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _stats_path(config_path: str | Path) -> Path:
    configured = _storage_values(config_path).get(
        "STATS_DATA",
        "../../data/classic/stats.json",
    ).strip()
    if not configured:
        raise ValueError("STATS_DATA must not be empty")
    return _configured_path(configured)


def _ranking_store(
    config_path: str | Path,
) -> ClassicRankingStore | SQLiteClassicRankingStore:
    """Open the same authoritative backend selected by the Classic runtime."""

    values = _storage_values(config_path)
    database_value = values.get("ACCOUNT_DB", "").strip()
    files_value = values.get("ACCOUNT_FILES", "").strip()
    if bool(database_value) != bool(files_value):
        raise ValueError("ACCOUNT_DB and ACCOUNT_FILES must both be set")
    if not database_value:
        return ClassicRankingStore(_stats_path(config_path))

    try:
        busy_timeout_ms = int(values.get("ACCOUNT_SQLITE_BUSY_TIMEOUT_MS", "5000"))
    except ValueError as exc:
        raise ValueError("ACCOUNT_SQLITE_BUSY_TIMEOUT_MS must be an integer") from exc
    database = SQLiteAccountDatabase(
        _configured_path(database_value),
        _configured_path(files_value),
        busy_timeout_ms=busy_timeout_ms,
    )
    legacy_path = _stats_path(config_path)
    return SQLiteClassicRankingStore(
        database,
        legacy_path=legacy_path if legacy_path.exists() else None,
    )


def _updates(args: argparse.Namespace) -> dict[str, int]:
    result: dict[str, int] = {}
    for argument, field in (
        ("wins", "wins"),
        ("losses", "losses"),
        ("disconnects", "disconnects"),
        ("skill", "rep"),
        ("opponents_rep", "opponents_rep"),
        ("opponents_rating", "opponents_rating"),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            result[field] = int(value)
    return result


def _print_summary(
    game: GameId,
    category: int,
    row: dict[str, int | float | str],
) -> None:
    print(
        f"game={game.value} persona={row['persona']} "
        f"category={(_category_labels(game)).get(category, str(category))} "
        f"rank={row['rank']} wins={row['wins']} losses={row['losses']} "
        f"disconnects={row['disconnects']} skill={row['rep']} "
        f"nos_used={float(row['nos_used']):.3f} "
        f"opponents_rep={row['opponents_rep']} "
        f"opponents_rating={row['opponents_rating']}"
    )


def _category_labels(game: GameId) -> dict[int, str]:
    return U2_CATEGORY_LABELS if game is GameId.UNDERGROUND2 else MW_CATEGORY_LABELS


def _player_summary(
    stats: ClassicPlayerStats,
    category: int,
) -> dict[str, int | float | str]:
    values = stats.category(category)
    return {
        "persona": stats.persona,
        "rank": values[0],
        "wins": values[1],
        "losses": values[2],
        "disconnects": values[3],
        "rep": values[4],
        "opponents_rep": values[5],
        "opponents_rating": values[6],
        "nos_used": stats.get_mw_nos_used(category),
    }


def _add_edit_arguments(parser: argparse.ArgumentParser, *, signed: bool) -> None:
    value_help = "signed delta" if signed else "new non-negative value"
    parser.add_argument("--wins", type=int, help=value_help)
    parser.add_argument("--losses", type=int, help=value_help)
    parser.add_argument("--disconnects", type=int, help=value_help)
    parser.add_argument("--skill", type=int, help=value_help)
    parser.add_argument("--opponents-rep", dest="opponents_rep", type=int, help=value_help)
    parser.add_argument(
        "--opponents-rating",
        dest="opponents_rating",
        type=int,
        help=value_help,
    )


def build_parser(*, default_config: str = str(DEFAULT_CONFIG)) -> argparse.ArgumentParser:
    parser = StatsArgumentParser(description="Manage persistent U2/MW player statistics")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--game", type=_game, default="mw")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="show one player's stored statistics")
    show.add_argument("persona")
    show.add_argument("category", nargs="?", type=_category)

    set_parser = sub.add_parser("set", help="set fields in one category")
    set_parser.add_argument("persona")
    set_parser.add_argument("category", type=_category)
    _add_edit_arguments(set_parser, signed=False)

    add = sub.add_parser("add", help="increment or decrement fields in one category")
    add.add_argument("persona")
    add.add_argument("category", type=_category)
    _add_edit_arguments(add, signed=True)

    reset = sub.add_parser("reset", help="reset one category or the whole player")
    reset.add_argument("persona")
    reset.add_argument("category", nargs="?", type=_category)

    delete = sub.add_parser("delete", help="delete one player's statistics")
    delete.add_argument("persona")

    board = sub.add_parser("list", help="show the leaderboard for one category")
    board.add_argument("category", nargs="?", type=_category, default=0)
    board.add_argument("--limit", type=int, default=100)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    default_config: str = str(DEFAULT_CONFIG),
) -> int:
    parser = build_parser(default_config=default_config)
    args = parser.parse_args(argv)
    try:
        store = _ranking_store(args.config)
        game: GameId = args.game
        category = (
            None
            if not hasattr(args, "category") or args.category is None
            else _resolve_category(game, args.category)
        )
        category_count = 6 if game is GameId.UNDERGROUND2 else 5

        if args.command == "show":
            categories = range(category_count) if category is None else (category,)
            for current_category in categories:
                _print_summary(
                    game,
                    current_category,
                    store.summary(game, args.persona, current_category),
                )
            return 0

        if args.command in {"set", "add"}:
            updates = _updates(args)
            if not updates:
                parser.error("provide at least one field such as --wins or --skill")
            row = store.update_fields(
                game,
                args.persona,
                category,
                updates,
                relative=args.command == "add",
            )
            _print_summary(game, category, row)
            return 0

        if args.command == "reset":
            store.reset(game, args.persona, category)
            categories = range(category_count) if category is None else (category,)
            for current_category in categories:
                _print_summary(
                    game,
                    current_category,
                    store.summary(game, args.persona, current_category),
                )
            return 0

        if args.command == "delete":
            removed = store.delete(game, args.persona)
            print(f"game={game.value} persona={args.persona} deleted={int(removed)}")
            return 0

        if args.command == "list":
            if args.limit <= 0:
                parser.error("--limit must be positive")
            rows = store.leaderboard(game, category, limit=args.limit)
            for row in rows:
                _print_summary(
                    game,
                    category,
                    _player_summary(row, category),
                )
            return 0
    except (KeyError, OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
