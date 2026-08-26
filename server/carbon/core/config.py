"""Strict configuration for the clean multi-game server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from carbon.core.catalog import GameId
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
class ServerSettings:
    game: GameId
    fesl_listen: Endpoint
    fesl_public: Endpoint
    messenger_public: Endpoint
    theater_public: Endpoint
    race_public: Endpoint
    max_frame_size: int = 65_535
    connection_timeout: float = 60.0
    fesl_activity_timeout: int = 0
    fesl_heartbeat_interval: float = 30.0
    messenger_ipc_endpoint: Endpoint | None = None
    messenger_ipc_secret: str = ""
    messenger_ipc_poll_interval: float = 0.1
    messenger_ipc_heartbeat_interval: float = 1.0
    theater_listen: Endpoint | None = None
    race_listen: Endpoint | None = None
    race_local: Endpoint | None = None
    account_data_path: str | None = None
    auth_mode: str = "password"
    auth_data_path: str | None = None
    auth_auto_enroll: bool = False
    auth_failure_limit: int = 5
    auth_lockout_seconds: float = 300.0
    auth_login_error_probe_code: int | None = None
    account_db_path: str | None = None
    account_files_path: str | None = None
    account_session_lease_seconds: float = 120.0
    account_sqlite_busy_timeout_ms: int = 5_000
    carbon_challenge_quick_join_before_ready: bool = False
    carbon_challenge_quick_join_after_ready: bool = False
    carbon_join_timeout_seconds: float = 45.0
    carbon_race_idle_timeout_seconds: float = 60.0
    carbon_loading_ready_fallback_seconds: float = 8.0
    carbon_dlc_catalog_path: str | None = "../../data/carbon/dlc_catalog.json"
    carbon_dlc_assignments_path: str | None = "../../data/carbon/dlc_assignments.json"
    carbon_dlc_store_enabled: bool = False
    carbon_dlc_store_listen: Endpoint | None = None
    carbon_dlc_store_session_seconds: float = 43_200.0
    carbon_dlc_store_cookie_secure: str = "auto"
    mad_public: Endpoint | None = None
    mad_listen: Endpoint | None = None
    mad_campaigns_path: str | None = None
    mad_rotation_seconds: int = 300
    mad_session_timeout_seconds: float = 900.0
    mad_impression_log_path: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "ServerSettings":
        values = load_service_values(path, "carbon")
        try:
            game = GameId(values.get("GAME", GameId.CARBON.value).strip().lower())
            max_frame_size = int(values.get("MAX_FRAME_SIZE", "65535"))
            connection_timeout = float(values.get("CONNECTION_TIMEOUT", "60"))
            fesl_activity_timeout = int(values.get("FESL_ACTIVITY_TIMEOUT", "0"))
            fesl_heartbeat_interval = float(values.get("FESL_HEARTBEAT_INTERVAL", "30"))
            messenger_ipc_poll_interval = float(values.get("MESSENGER_IPC_POLL_INTERVAL", "0.1"))
            messenger_ipc_heartbeat_interval = float(values.get("MESSENGER_IPC_HEARTBEAT_INTERVAL", "1"))
            auth_mode = values.get("AUTH_MODE", "password").strip().casefold() or "password"
            auth_auto_enroll = _boolean_value(
                values.get("AUTH_AUTO_ENROLL", "0"),
                name="AUTH_AUTO_ENROLL",
            )
            auth_failure_limit = int(values.get("AUTH_FAILURE_LIMIT", "5"))
            auth_lockout_seconds = float(values.get("AUTH_LOCKOUT_SECONDS", "300"))
            auth_login_error_probe_text = values.get(
                "AUTH_LOGIN_ERROR_PROBE_CODE",
                "",
            ).strip()
            auth_login_error_probe_code = (
                int(auth_login_error_probe_text)
                if auth_login_error_probe_text
                else None
            )
            challenge_quick_join_before_ready = _boolean_value(
                values.get("CARBON_CHALLENGE_QUICK_JOIN_BEFORE_READY", "0"),
                name="CARBON_CHALLENGE_QUICK_JOIN_BEFORE_READY",
            )
            challenge_quick_join_after_ready = _boolean_value(
                values.get("CARBON_CHALLENGE_QUICK_JOIN_AFTER_READY", "0"),
                name="CARBON_CHALLENGE_QUICK_JOIN_AFTER_READY",
            )
            dlc_store_enabled = _boolean_value(
                values.get("CARBON_DLC_STORE_ENABLED", "0"),
                name="CARBON_DLC_STORE_ENABLED",
            )
            mad_rotation_seconds = int(values.get("MAD_ROTATION_SECONDS", "300"))
            mad_session_timeout_seconds = float(
                values.get("MAD_SESSION_TIMEOUT_SECONDS", "900")
            )
            settings = cls(
                game=game,
                fesl_listen=Endpoint.parse(values.get("FESL_LISTEN", "0.0.0.0:18210")),
                fesl_public=Endpoint.parse(values.get("FESL_PUBLIC", "127.0.0.1:18210")),
                messenger_public=Endpoint.parse(values.get("MESSENGER_PUBLIC", "127.0.0.1:13505")),
                theater_public=Endpoint.parse(values.get("THEATER_PUBLIC", "127.0.0.1:18215")),
                race_public=Endpoint.parse(values.get("RACE_PUBLIC", "127.0.0.1:19118")),
                max_frame_size=max_frame_size,
                connection_timeout=connection_timeout,
                fesl_activity_timeout=fesl_activity_timeout,
                fesl_heartbeat_interval=fesl_heartbeat_interval,
                messenger_ipc_endpoint=(
                    Endpoint.parse(values.get("MESSENGER_IPC", "127.0.0.1:13506"))
                    if values.get("MESSENGER_IPC", "127.0.0.1:13506").strip()
                    else None
                ),
                messenger_ipc_secret=values.get(
                    "MESSENGER_IPC_SECRET", ""
                ).strip(),
                messenger_ipc_poll_interval=messenger_ipc_poll_interval,
                messenger_ipc_heartbeat_interval=messenger_ipc_heartbeat_interval,
                theater_listen=Endpoint.parse(values.get("THEATER_LISTEN", "0.0.0.0:18215")),
                race_listen=Endpoint.parse(values.get("RACE_LISTEN", "0.0.0.0:19118")),
                race_local=(
                    Endpoint.parse(values["RACE_LOCAL"])
                    if values.get("RACE_LOCAL", "").strip()
                    else None
                ),
                account_data_path=values.get("ACCOUNT_DATA", "../../data/carbon/progression.json") or None,
                auth_mode=auth_mode,
                auth_data_path=values.get("AUTH_DATA", "../../data/carbon/auth.json") or None,
                auth_auto_enroll=auth_auto_enroll,
                auth_failure_limit=auth_failure_limit,
                auth_lockout_seconds=auth_lockout_seconds,
                auth_login_error_probe_code=auth_login_error_probe_code,
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
                carbon_challenge_quick_join_before_ready=challenge_quick_join_before_ready,
                carbon_challenge_quick_join_after_ready=challenge_quick_join_after_ready,
                carbon_join_timeout_seconds=float(
                    values.get("CARBON_JOIN_TIMEOUT_SECONDS", "45")
                ),
                carbon_race_idle_timeout_seconds=float(
                    values.get("CARBON_RACE_IDLE_TIMEOUT_SECONDS", "60")
                ),
                carbon_loading_ready_fallback_seconds=float(
                    values.get("CARBON_LOADING_READY_FALLBACK_SECONDS", "8")
                ),
                carbon_dlc_catalog_path=(
                    values.get(
                        "CARBON_DLC_CATALOG",
                        "../../data/carbon/dlc_catalog.json",
                    )
                    or None
                ),
                carbon_dlc_assignments_path=(
                    values.get(
                        "CARBON_DLC_ASSIGNMENTS",
                        "../../data/carbon/dlc_assignments.json",
                    )
                    or None
                ),
                carbon_dlc_store_enabled=dlc_store_enabled,
                carbon_dlc_store_listen=(
                    Endpoint.parse(
                        values.get("CARBON_DLC_STORE_LISTEN", "127.0.0.1:8081")
                    )
                    if values.get("CARBON_DLC_STORE_LISTEN", "127.0.0.1:8081").strip()
                    else None
                ),
                carbon_dlc_store_session_seconds=float(
                    values.get("CARBON_DLC_STORE_SESSION_SECONDS", "43200")
                ),
                carbon_dlc_store_cookie_secure=values.get(
                    "CARBON_DLC_STORE_COOKIE_SECURE", "auto"
                ).strip().casefold(),
                mad_public=Endpoint.parse(
                    values.get("MAD_PUBLIC", "127.0.0.1:9000")
                ),
                mad_listen=Endpoint.parse(
                    values.get("MAD_LISTEN", "0.0.0.0:9000")
                ),
                mad_campaigns_path=(
                    values.get("MAD_CAMPAIGNS", "../../data/carbon/mad_campaigns.json")
                    or None
                ),
                mad_rotation_seconds=mad_rotation_seconds,
                mad_session_timeout_seconds=mad_session_timeout_seconds,
                mad_impression_log_path=(
                    values.get(
                        "MAD_IMPRESSION_LOG",
                        "../../data/carbon/mad_impressions.jsonl",
                    )
                    or None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid server configuration {path}: {exc}") from exc
        if settings.max_frame_size < 12 or settings.max_frame_size > 16 * 1024 * 1024:
            raise ValueError("MAX_FRAME_SIZE must be between 12 and 16777216")
        if settings.connection_timeout <= 0:
            raise ValueError("CONNECTION_TIMEOUT must be positive")
        if settings.fesl_activity_timeout < 0:
            raise ValueError("FESL_ACTIVITY_TIMEOUT must be zero or positive")
        if settings.fesl_heartbeat_interval <= 0:
            raise ValueError("FESL_HEARTBEAT_INTERVAL must be positive")
        if settings.messenger_ipc_poll_interval <= 0:
            raise ValueError("MESSENGER_IPC_POLL_INTERVAL must be positive")
        if settings.messenger_ipc_heartbeat_interval <= 0:
            raise ValueError("MESSENGER_IPC_HEARTBEAT_INTERVAL must be positive")
        legacy_mode = values.get("MESSENGER_MODE", "external").strip().casefold()
        if legacy_mode not in {"", "external"}:
            raise ValueError(
                "MESSENGER_MODE=internal was removed; Carbon must use the shared EA Messenger"
            )
        if values.get("MESSENGER_LISTEN", "").strip():
            raise ValueError(
                "MESSENGER_LISTEN was removed from Carbon; configure the shared listener"
            )
        if settings.messenger_ipc_endpoint is None:
            raise ValueError("MESSENGER_IPC must not be empty")
        if settings.messenger_ipc_endpoint.host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MESSENGER_IPC must use a loopback host")
        if not settings.messenger_ipc_secret:
            raise ValueError("MESSENGER_IPC_SECRET must not be empty")
        if settings.mad_rotation_seconds < 0:
            raise ValueError("MAD_ROTATION_SECONDS must be zero or positive")
        if settings.mad_session_timeout_seconds <= 0:
            raise ValueError("MAD_SESSION_TIMEOUT_SECONDS must be positive")
        if settings.carbon_join_timeout_seconds <= 0:
            raise ValueError("CARBON_JOIN_TIMEOUT_SECONDS must be positive")
        if settings.carbon_race_idle_timeout_seconds <= 0:
            raise ValueError("CARBON_RACE_IDLE_TIMEOUT_SECONDS must be positive")
        if settings.carbon_loading_ready_fallback_seconds <= 0:
            raise ValueError(
                "CARBON_LOADING_READY_FALLBACK_SECONDS must be positive"
            )
        if settings.carbon_dlc_store_enabled and settings.carbon_dlc_store_listen is None:
            raise ValueError(
                "CARBON_DLC_STORE_LISTEN must not be empty when the store is enabled"
            )
        if settings.carbon_dlc_store_session_seconds <= 0:
            raise ValueError("CARBON_DLC_STORE_SESSION_SECONDS must be positive")
        if settings.carbon_dlc_store_cookie_secure not in {"auto", "always", "never"}:
            raise ValueError(
                "CARBON_DLC_STORE_COOKIE_SECURE must be auto, always or never"
            )
        if settings.auth_mode not in {"open", "password"}:
            raise ValueError("AUTH_MODE must be open or password")
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
        if (
            settings.auth_login_error_probe_code is not None
            and not 0 <= settings.auth_login_error_probe_code <= 65_535
        ):
            raise ValueError(
                "AUTH_LOGIN_ERROR_PROBE_CODE must be empty or between 0 and 65535"
            )
        return settings


def _boolean_value(value: object, *, name: str) -> bool:
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be 0/1, false/true, no/yes or off/on")


def load_key_values(path: str | Path) -> dict[str, str]:
    """Compatibility accessor for Carbon's derived service values."""

    return load_service_values(path, "carbon")
