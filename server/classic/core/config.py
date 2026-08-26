"""Strict configuration for the combined Underground 2 / Most Wanted server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from classic.core.catalog import GameId
from common.config import load_service_values


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    @classmethod
    def parse(cls, value: str, *, default_host: str = "127.0.0.1") -> "Endpoint":
        text = str(value or "").strip()
        if not text:
            raise ValueError("empty endpoint")
        if ":" not in text:
            host, port_text = default_host, text
        else:
            host, port_text = text.rsplit(":", 1)
            host = host.strip() or default_host
        try:
            port = int(port_text.strip())
        except ValueError as exc:
            raise ValueError(f"invalid endpoint port: {value!r}") from exc
        if not 0 <= port <= 65_535:
            raise ValueError(f"endpoint port out of range: {port}")
        return cls(host, port)


@dataclass(frozen=True)
class ClassicGameSettings:
    game: GameId
    bootstrap_listen: Endpoint
    bootstrap_public: Endpoint
    lobby_listen: Endpoint
    lobby_public: Endpoint
    directory_session: str = ""
    directory_mask: str = ""


@dataclass(frozen=True)
class ServerSettings:
    underground2: ClassicGameSettings
    most_wanted: ClassicGameSettings
    messenger_listen: Endpoint
    messenger_public: Endpoint
    web_listen: Endpoint
    web_public: Endpoint
    enable_u2: bool = True
    enable_mw: bool = True
    local_advertise_host: str = "127.0.0.1"
    mw_lobby_extra_listen: Endpoint | None = None
    auth_mode: str = "password"
    auth_data_path: str = "../../data/classic/auth.json"
    auth_auto_enroll: bool = False
    u2_auth_auto_enroll: bool = False
    mw_auth_auto_enroll: bool = False
    u2_game_size_policy: str = "client"
    u2_game_min_players: int = 2
    u2_game_max_players: int = 4
    auth_failure_limit: int = 5
    auth_lockout_seconds: float = 300.0
    account_db_path: str | None = None
    account_files_path: str | None = None
    account_session_lease_seconds: float = 120.0
    account_sqlite_busy_timeout_ms: int = 5_000
    social_data_path: str = "../../data/classic/social.json"
    stats_data_path: str = "../../data/classic/stats.json"
    carbon_messenger_ipc_listen: Endpoint | None = None
    carbon_messenger_ipc_secret: str = ""
    carbon_messenger_ipc_max_age: float = 5.0
    carbon_messenger_heartbeat_interval: float = 30.0
    carbon_messenger_auth_ipc_wait: float = 3.0
    max_frame_size: int = 65_535
    connection_timeout: float = 60.0
    classic_directory_ttl: float = 120.0
    classic_lobby_idle_timeout: float = 0.0
    classic_lobby_heartbeat_interval: float = 20.0
    race_listen: Endpoint = Endpoint("127.0.0.1", 0)
    race_public: Endpoint = Endpoint("127.0.0.1", 0)
    race_virtual_network: str = "100.64.0.0/10"

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        games: str | Iterable[str] | None = None,
    ) -> "ServerSettings":
        values = load_service_values(path, "classic", games=games)
        try:
            settings = cls(
                underground2=ClassicGameSettings(
                    GameId.UNDERGROUND2,
                    Endpoint.parse(values.get("U2_BOOTSTRAP_LISTEN", "0.0.0.0:20921")),
                    Endpoint.parse(values.get("U2_BOOTSTRAP_PUBLIC", "127.0.0.1:20921")),
                    Endpoint.parse(values.get("U2_LOBBY_LISTEN", "0.0.0.0:20922")),
                    Endpoint.parse(values.get("U2_LOBBY_PUBLIC", "127.0.0.1:20922")),
                    values.get("U2_DIRECTORY_SESSION", "").strip(),
                    values.get("U2_DIRECTORY_MASK", "").strip(),
                ),
                most_wanted=ClassicGameSettings(
                    GameId.MOST_WANTED,
                    Endpoint.parse(values.get("MW_BOOTSTRAP_LISTEN", "0.0.0.0:30921")),
                    Endpoint.parse(values.get("MW_BOOTSTRAP_PUBLIC", "127.0.0.1:30921")),
                    Endpoint.parse(values.get("MW_LOBBY_LISTEN", "0.0.0.0:30920")),
                    Endpoint.parse(values.get("MW_LOBBY_PUBLIC", "127.0.0.1:30920")),
                    values.get("MW_DIRECTORY_SESSION", "").strip(),
                    values.get("MW_DIRECTORY_MASK", "").strip(),
                ),
                messenger_listen=Endpoint.parse(values.get("MESSENGER_LISTEN", "0.0.0.0:13505")),
                enable_u2=_boolean_value(values.get("ENABLE_U2", "1"), name="ENABLE_U2"),
                enable_mw=_boolean_value(values.get("ENABLE_MW", "1"), name="ENABLE_MW"),
                local_advertise_host=values.get("LOCAL_ADVERTISE_HOST", "127.0.0.1").strip() or "127.0.0.1",
                messenger_public=Endpoint.parse(values.get("MESSENGER_PUBLIC", "127.0.0.1:13505")),
                web_listen=Endpoint.parse(values.get("WEB_LISTEN", "0.0.0.0:20923")),
                web_public=Endpoint.parse(values.get("WEB_PUBLIC", "127.0.0.1:20923")),
                mw_lobby_extra_listen=(
                    Endpoint.parse(values["MW_LOBBY_EXTRA_LISTEN"])
                    if values.get("MW_LOBBY_EXTRA_LISTEN", "").strip()
                    else None
                ),
                auth_mode=values.get("AUTH_MODE", "password").strip().casefold() or "password",
                auth_data_path=values.get("AUTH_DATA", "../../data/classic/auth.json").strip(),
                auth_auto_enroll=_boolean_value(values.get("AUTH_AUTO_ENROLL", "0"), name="AUTH_AUTO_ENROLL"),
                u2_auth_auto_enroll=_boolean_value(
                    values.get("U2_AUTH_AUTO_ENROLL", values.get("AUTH_AUTO_ENROLL", "0")),
                    name="U2_AUTH_AUTO_ENROLL",
                ),
                mw_auth_auto_enroll=_boolean_value(
                    values.get("MW_AUTH_AUTO_ENROLL", values.get("AUTH_AUTO_ENROLL", "0")),
                    name="MW_AUTH_AUTO_ENROLL",
                ),
                u2_game_size_policy=values.get(
                    "U2_GAME_SIZE_POLICY", "client"
                ).strip().casefold() or "client",
                u2_game_min_players=int(values.get("U2_GAME_MIN_PLAYERS", "2")),
                u2_game_max_players=int(values.get("U2_GAME_MAX_PLAYERS", "4")),
                auth_failure_limit=int(values.get("AUTH_FAILURE_LIMIT", "5")),
                auth_lockout_seconds=float(values.get("AUTH_LOCKOUT_SECONDS", "300")),
                account_db_path=(
                    values.get(
                        "ACCOUNT_DB",
                        "",
                    ).strip()
                    or None
                ),
                account_files_path=(
                    values.get(
                        "ACCOUNT_FILES",
                        "",
                    ).strip()
                    or None
                ),
                account_session_lease_seconds=float(
                    values.get("ACCOUNT_SESSION_LEASE_SECONDS", "120")
                ),
                account_sqlite_busy_timeout_ms=int(
                    values.get("ACCOUNT_SQLITE_BUSY_TIMEOUT_MS", "5000")
                ),
                social_data_path=values.get("SOCIAL_DATA", "../../data/classic/social.json").strip(),
                stats_data_path=values.get("STATS_DATA", "../../data/classic/stats.json").strip(),
                carbon_messenger_ipc_listen=(
                    Endpoint.parse(values.get("CARBON_MESSENGER_IPC_LISTEN", ""))
                    if values.get("CARBON_MESSENGER_IPC_LISTEN", "").strip()
                    else None
                ),
                carbon_messenger_ipc_secret=values.get(
                    "CARBON_MESSENGER_IPC_SECRET", ""
                ).strip(),
                carbon_messenger_ipc_max_age=float(
                    values.get("CARBON_MESSENGER_IPC_MAX_AGE", "5")
                ),
                carbon_messenger_heartbeat_interval=float(
                    values.get("CARBON_MESSENGER_HEARTBEAT_INTERVAL", "30")
                ),
                carbon_messenger_auth_ipc_wait=float(
                    values.get("CARBON_MESSENGER_AUTH_IPC_WAIT", "3")
                ),
                max_frame_size=int(values.get("MAX_FRAME_SIZE", "65535")),
                connection_timeout=float(values.get("CONNECTION_TIMEOUT", "60")),
                classic_directory_ttl=float(values.get("CLASSIC_DIRECTORY_TTL", "120")),
                classic_lobby_idle_timeout=float(
                    values.get("CLASSIC_LOBBY_IDLE_TIMEOUT", "0")
                ),
                classic_lobby_heartbeat_interval=float(
                    values.get(
                        "CLASSIC_LOBBY_HEARTBEAT_INTERVAL",
                        values.get("MW_LOBBY_HEARTBEAT_INTERVAL", "20"),
                    )
                ),
                race_listen=Endpoint.parse(
                    values.get("RACE_LISTEN", "0.0.0.0:20000")
                ),
                race_public=Endpoint.parse(
                    values.get("RACE_PUBLIC", "127.0.0.1:20000")
                ),
                race_virtual_network=values.get(
                    "RACE_VIRTUAL_NETWORK",
                    "100.64.0.0/10",
                ).strip(),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid server configuration {path}: {exc}") from exc

        if not settings.local_advertise_host or ":" in settings.local_advertise_host or any(ch.isspace() for ch in settings.local_advertise_host):
            raise ValueError("LOCAL_ADVERTISE_HOST must be a hostname or IPv4 without port")
        if settings.auth_mode not in {"open", "password"}:
            raise ValueError("AUTH_MODE must be open or password")
        if settings.u2_game_size_policy not in {"client", "server"}:
            raise ValueError("U2_GAME_SIZE_POLICY must be client or server")
        if not 1 <= settings.u2_game_min_players <= 8:
            raise ValueError("U2_GAME_MIN_PLAYERS must be between 1 and 8")
        if not 1 <= settings.u2_game_max_players <= 8:
            raise ValueError("U2_GAME_MAX_PLAYERS must be between 1 and 8")
        if settings.u2_game_min_players > settings.u2_game_max_players:
            raise ValueError(
                "U2_GAME_MIN_PLAYERS must not exceed U2_GAME_MAX_PLAYERS"
            )
        if not settings.auth_data_path:
            raise ValueError("AUTH_DATA must not be empty")
        if not settings.social_data_path:
            raise ValueError("SOCIAL_DATA must not be empty")
        if not settings.stats_data_path:
            raise ValueError("STATS_DATA must not be empty")
        if settings.carbon_messenger_ipc_max_age <= 0:
            raise ValueError("CARBON_MESSENGER_IPC_MAX_AGE must be positive")
        if settings.carbon_messenger_heartbeat_interval <= 0:
            raise ValueError("CARBON_MESSENGER_HEARTBEAT_INTERVAL must be positive")
        if settings.carbon_messenger_auth_ipc_wait <= 0:
            raise ValueError("CARBON_MESSENGER_AUTH_IPC_WAIT must be positive")
        if settings.carbon_messenger_ipc_listen is not None:
            if settings.carbon_messenger_ipc_listen.host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("CARBON_MESSENGER_IPC_LISTEN must use a loopback host")
            if not settings.carbon_messenger_ipc_secret:
                raise ValueError("CARBON_MESSENGER_IPC_SECRET must not be empty")
        if settings.max_frame_size < 12 or settings.max_frame_size > 16 * 1024 * 1024:
            raise ValueError("MAX_FRAME_SIZE must be between 12 and 16777216")
        if settings.connection_timeout <= 0:
            raise ValueError("CONNECTION_TIMEOUT must be positive")
        if settings.auth_failure_limit < 0:
            raise ValueError("AUTH_FAILURE_LIMIT must be zero or positive")
        if settings.auth_lockout_seconds < 0:
            raise ValueError("AUTH_LOCKOUT_SECONDS must be zero or positive")
        if (settings.account_db_path is None) != (settings.account_files_path is None):
            raise ValueError("ACCOUNT_DB and ACCOUNT_FILES must either both be set or both be empty")
        if settings.account_session_lease_seconds <= 0:
            raise ValueError("ACCOUNT_SESSION_LEASE_SECONDS must be positive")
        if settings.account_sqlite_busy_timeout_ms < 0:
            raise ValueError("ACCOUNT_SQLITE_BUSY_TIMEOUT_MS must be zero or positive")
        if settings.classic_directory_ttl <= 0:
            raise ValueError("CLASSIC_DIRECTORY_TTL must be positive")
        if settings.classic_lobby_idle_timeout < 0:
            raise ValueError("CLASSIC_LOBBY_IDLE_TIMEOUT must be zero or positive")
        if settings.classic_lobby_heartbeat_interval <= 0:
            raise ValueError("CLASSIC_LOBBY_HEARTBEAT_INTERVAL must be positive")
        if not settings.race_virtual_network:
            raise ValueError("RACE_VIRTUAL_NETWORK must not be empty")

        endpoints = {
            "U2_BOOTSTRAP_LISTEN": settings.underground2.bootstrap_listen,
            "U2_LOBBY_LISTEN": settings.underground2.lobby_listen,
            "MW_BOOTSTRAP_LISTEN": settings.most_wanted.bootstrap_listen,
            "MW_LOBBY_LISTEN": settings.most_wanted.lobby_listen,
            "MESSENGER_LISTEN": settings.messenger_listen,
            "WEB_LISTEN": settings.web_listen,
        }
        if settings.mw_lobby_extra_listen is not None:
            endpoints["MW_LOBBY_EXTRA_LISTEN"] = settings.mw_lobby_extra_listen
        if settings.carbon_messenger_ipc_listen is not None:
            endpoints["CARBON_MESSENGER_IPC_LISTEN"] = settings.carbon_messenger_ipc_listen
        occupied: dict[tuple[str, int], str] = {}
        for name, endpoint in endpoints.items():
            key = (endpoint.host, endpoint.port)
            previous = occupied.get(key)
            if previous is not None and endpoint.port != 0:
                raise ValueError(f"{name} conflicts with {previous} on {endpoint.host}:{endpoint.port}")
            occupied[key] = name
        return settings


def _boolean_value(value: object, *, name: str) -> bool:
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be 0/1, false/true, no/yes or off/on")


def load_key_values(path: str | Path) -> dict[str, str]:
    """Compatibility accessor for Classic's derived service values."""

    return load_service_values(path, "classic")
