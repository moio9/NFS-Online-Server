"""Shared deployment configuration for the NFS Online processes.

``config/server.toml`` is the editable server configuration. Classic, Carbon,
the launcher and administration commands derive their process-specific
settings in memory and never create secondary ``runtime/*.cfg`` files. Legacy
sectioned INI files remain readable only to make upgrades non-destructive;
client-side ``net_*.ini`` files are unaffected.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
from typing import Iterable, Mapping

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - exercised only on Python 3.10
    _tomllib = None


DEFAULT_PUBLIC_HOST = "127.1.1.0"
VALID_GAMES = ("u2", "mw", "carbon")
CLASSIC_GAMES = frozenset(("u2", "mw"))

CONFIG_SECTIONS = {
    "global": "server",
    "u2": "underground2",
    "mw": "most_wanted",
    "carbon": "carbon",
}

CONFIG_DEFAULTS: dict[str, dict[str, str]] = {
    "global": {
        "DEFAULT_GAMES": "u2,mw,carbon",
        "PUBLIC_HOST": DEFAULT_PUBLIC_HOST,
        "LOCAL_ADVERTISE_HOST": "auto",
        "COLOR_MODE": "auto",
        "LOG_LEVEL": "INFO",
        "ACCOUNT_DB": "data/accounts.sqlite3",
        "ACCOUNT_FILES": "data/users",
        "ACCOUNT_SESSION_LEASE_SECONDS": "120",
        "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS": "5000",
        "AUTH_MODE": "password",
        "AUTH_AUTO_ENROLL": "0",
        "AUTH_FAILURE_LIMIT": "5",
        "AUTH_LOCKOUT_SECONDS": "300",
        "MAX_FRAME_SIZE": "65535",
        "CONNECTION_TIMEOUT": "60",
        "DIRECTORY_TTL": "120",
        "CLASSIC_LOBBY_IDLE_TIMEOUT": "0",
        "CLASSIC_LOBBY_HEARTBEAT_INTERVAL": "20",
        "MESSENGER_LISTEN": "0.0.0.0:13505",
        "MESSENGER_PUBLIC_PORT": "13505",
        "MESSENGER_IPC_LISTEN": "127.0.0.1:13506",
        "IPC_SECRET": "AUTO",
        "MESSENGER_IPC_MAX_AGE": "5",
        "CARBON_SESSION_HEARTBEAT_INTERVAL": "30",
        "MESSENGER_AUTH_IPC_WAIT": "3",
        "WEB_LISTEN": "0.0.0.0:20923",
        "WEB_PUBLIC_PORT": "20923",
        "RACE_RELAY_LISTEN": "0.0.0.0:20000",
        "RACE_RELAY_PUBLIC_PORT": "20000",
        "RACE_VIRTUAL_NETWORK": "100.64.0.0/10",
    },
    "u2": {
        "BOOTSTRAP_LISTEN": "0.0.0.0:20921",
        "BOOTSTRAP_PUBLIC_PORT": "20921",
        "LOBBY_LISTEN": "0.0.0.0:20922",
        "LOBBY_PUBLIC_PORT": "20922",
        "DIRECTORY_SESSION": "",
        "DIRECTORY_MASK": "",
        "GAME_SIZE_POLICY": "client",
        "GAME_MIN_PLAYERS": "2",
        "GAME_MAX_PLAYERS": "4",
    },
    "mw": {
        "CREATE_ACCOUNT_ON_FIRST_LOGIN": "1",
        "BOOTSTRAP_LISTEN": "0.0.0.0:30921",
        "BOOTSTRAP_PUBLIC_PORT": "30921",
        "LOBBY_LISTEN": "0.0.0.0:30920",
        "LOBBY_PUBLIC_PORT": "30920",
        "DIRECTORY_SESSION": "",
        "DIRECTORY_MASK": "",
    },
    "carbon": {
        "FESL_LISTEN": "0.0.0.0:18210",
        "FESL_PUBLIC_PORT": "18210",
        "THEATER_LISTEN": "0.0.0.0:18215",
        "THEATER_PUBLIC_PORT": "18215",
        "RACE_LISTEN": "0.0.0.0:19118",
        "RACE_PUBLIC_PORT": "19118",
        "MAD_LISTEN": "0.0.0.0:9000",
        "MAD_PUBLIC_PORT": "9000",
        "MAD_CAMPAIGNS": "data/carbon/mad_campaigns.json",
        "MAD_ROTATION_SECONDS": "300",
        "MAD_SESSION_TIMEOUT_SECONDS": "900",
        "MAD_IMPRESSION_LOG": "data/carbon/mad_impressions.jsonl",
        "FESL_ACTIVITY_TIMEOUT": "0",
        "FESL_HEARTBEAT_INTERVAL": "30",
        "MESSENGER_IPC_POLL_INTERVAL": "0.1",
        "MESSENGER_IPC_HEARTBEAT_INTERVAL": "1",
        "AUTH_LOGIN_ERROR_PROBE_CODE": "",
        "CHALLENGE_QUICK_JOIN_BEFORE_READY": "0",
        "CHALLENGE_QUICK_JOIN_AFTER_READY": "0",
        "JOIN_TIMEOUT_SECONDS": "45",
        "RACE_IDLE_TIMEOUT_SECONDS": "60",
        "LOADING_READY_FALLBACK_SECONDS": "8",
        "DLC_CATALOG": "data/carbon/dlc_catalog.json",
        "DLC_ASSIGNMENTS": "data/carbon/dlc_assignments.json",
        "DLC_STORE_ENABLED": "0",
        "DLC_STORE_LISTEN": "127.0.0.1:8081",
        "DLC_STORE_SESSION_SECONDS": "43200",
        "DLC_STORE_COOKIE_SECURE": "auto",
    },
}

_AUTO_SECRET_VALUES = {"", "auto", "generate"}
_AUTO_LOCAL_VALUES = {"", "auto", "detect"}


class ConfigurationError(ValueError):
    """Invalid deployment configuration or unresolved runtime state."""


@dataclass(frozen=True)
class RuntimeContext:
    """Runtime-only values shared by the launcher and both child processes."""

    config_path: Path
    package_root: Path
    games: tuple[str, ...]
    public_host: str
    public_ipv4: str
    local_ipv4: str
    ipc_secret: str
    warning: str | None = None

    def environment(self) -> dict[str, str]:
        """Environment overrides that make child derivation deterministic."""

        return {
            "NFS_GAMES": ",".join(self.games),
            "NFS_PUBLIC_HOST": self.public_host,
            "NFS_PUBLIC_IPV4": self.public_ipv4,
            "NFS_LOCAL_IPV4": self.local_ipv4,
            "NFS_IPC_SECRET": self.ipc_secret,
        }


def _strip_toml_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#":
            return value[:index]
    return value


def _split_toml_array(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_toml_value(value: str, *, path: Path, line_number: int) -> object:
    text = value.strip()
    if not text:
        raise ConfigurationError(f"{path}:{line_number}: missing TOML value")
    if text.startswith('"'):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"{path}:{line_number}: invalid TOML string: {exc}"
            ) from exc
        if not isinstance(parsed, str):
            raise ConfigurationError(f"{path}:{line_number}: invalid TOML string")
        return parsed
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise ConfigurationError(f"{path}:{line_number}: invalid TOML string")
        return text[1:-1]
    lowered = text.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if text.startswith("["):
        if not text.endswith("]"):
            raise ConfigurationError(f"{path}:{line_number}: invalid TOML list")
        body = text[1:-1].strip()
        if not body:
            return []
        return [
            _parse_toml_value(item, path=path, line_number=line_number)
            for item in _split_toml_array(body)
        ]
    compact = text.replace("_", "")
    if re.fullmatch(r"[+-]?\d+", compact):
        return int(compact)
    if re.fullmatch(
        r"[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?",
        compact,
    ):
        return float(compact)
    raise ConfigurationError(
        f"{path}:{line_number}: unsupported TOML value {text!r}"
    )


def _parse_toml_compat(text: str, *, path: Path) -> dict[str, object]:
    """Small Python 3.10 fallback for this package's flat TOML tables."""

    document: dict[str, object] = {}
    current: dict[str, object] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise ConfigurationError(
                    f"{path}:{line_number}: invalid TOML section"
                )
            section = line[1:-1].strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", section):
                raise ConfigurationError(
                    f"{path}:{line_number}: invalid TOML section {section!r}"
                )
            if section in document:
                raise ConfigurationError(
                    f"{path}:{line_number}: section [{section}] is duplicated"
                )
            current = {}
            document[section] = current
            continue
        if current is None:
            raise ConfigurationError(
                f"{path}:{line_number}: keys must be inside a TOML section"
            )
        if "=" not in line:
            raise ConfigurationError(f"{path}:{line_number}: missing '='")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise ConfigurationError(
                f"{path}:{line_number}: invalid TOML key {key!r}"
            )
        if key in current:
            raise ConfigurationError(
                f"{path}:{line_number}: key {key} is duplicated"
            )
        current[key] = _parse_toml_value(
            raw_value,
            path=path,
            line_number=line_number,
        )
    return document


def _toml_document(path: Path) -> dict[str, object]:
    try:
        if _tomllib is not None:
            with path.open("rb") as handle:
                value = _tomllib.load(handle)
        else:  # pragma: no cover - Python 3.10 compatibility
            value = _parse_toml_compat(
                path.read_text(encoding="utf-8-sig"),
                path=path,
            )
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigurationError(f"invalid TOML configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: the TOML root must be a table")
    return value


def _toml_config_value(value: object, *, path: Path, section: str, key: str) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            if not isinstance(item, (str, int, float, bool)):
                raise ConfigurationError(
                    f"{path}:[{section}]: {key} contains a complex value"
                )
            rendered.append(_toml_config_value(item, path=path, section=section, key=key))
        return ",".join(rendered)
    raise ConfigurationError(
        f"{path}:[{section}]: {key} must be text, a number, a boolean, or a list"
    )


def _raw_toml_sections(path: Path) -> dict[str, dict[str, str]]:
    document = _toml_document(path)
    sections: dict[str, dict[str, str]] = {}
    for raw_section, raw_values in document.items():
        if not isinstance(raw_values, dict):
            raise ConfigurationError(
                f"{path}: key {raw_section!r} must be a TOML section"
            )
        section = str(raw_section).strip()
        rendered: dict[str, str] = {}
        for raw_key, raw_value in raw_values.items():
            key = str(raw_key).strip()
            rendered[key] = _toml_config_value(
                raw_value,
                path=path,
                section=section,
                key=key,
            )
        sections[section] = rendered
    return sections


def _raw_ini_sections(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    return {
        section: {
            key: value.strip()
            for key, value in parser.items(section, raw=True)
        }
        for section in parser.sections()
    }


def read_configuration_file(path: str | Path) -> dict[str, dict[str, str]]:
    """Read strict TOML (or a legacy INI) and merge documented defaults."""

    config_path = Path(path)
    raw_sections = (
        _raw_toml_sections(config_path)
        if config_path.suffix.casefold() == ".toml"
        else _raw_ini_sections(config_path)
    )

    expected_sections = {value.casefold(): key for key, value in CONFIG_SECTIONS.items()}
    actual_sections: dict[str, str] = {}
    for section in raw_sections:
        normalized = section.strip().casefold()
        if normalized in actual_sections:
            raise ConfigurationError(f"{config_path}: section [{section}] is duplicated")
        actual_sections[normalized] = section

    unknown_sections = sorted(set(actual_sections) - set(expected_sections))
    if unknown_sections:
        rendered = ", ".join(f"[{actual_sections[name]}]" for name in unknown_sections)
        raise ConfigurationError(
            f"{config_path} contains unknown sections: {rendered}"
        )

    result: dict[str, dict[str, str]] = {}
    for scope, expected_name in CONFIG_SECTIONS.items():
        values = dict(CONFIG_DEFAULTS[scope])
        actual_name = actual_sections.get(expected_name.casefold())
        if actual_name is not None:
            raw: dict[str, str] = {}
            for key, value in raw_sections[actual_name].items():
                normalized = key.strip().upper()
                if not normalized or not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized):
                    raise ConfigurationError(
                        f"{config_path}:[{actual_name}]: invalid key {key!r}"
                    )
                if normalized in raw:
                    raise ConfigurationError(
                        f"{config_path}:[{actual_name}]: key {normalized} is duplicated"
                    )
                raw[normalized] = value.strip()
            unknown = sorted(set(raw) - set(CONFIG_DEFAULTS[scope]))
            if unknown:
                raise ConfigurationError(
                    f"{config_path}:[{actual_name}] contains unknown keys: "
                    + ", ".join(unknown)
                )
            values.update(raw)
        result[scope] = values
    return result


def normalize_games(
    value: str | Iterable[str] | None,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return VALID_GAMES
    raw = value.replace("+", ",").split(",") if isinstance(value, str) else list(value)
    selected: list[str] = []
    aliases = {
        "all": VALID_GAMES,
        "none": (),
        "off": (),
        "underground2": ("u2",),
        "underground_2": ("u2",),
        "mostwanted": ("mw",),
        "most_wanted": ("mw",),
        "nfsc": ("carbon",),
        "carbon": ("carbon",),
    }
    for item in raw:
        name = str(item or "").strip().casefold().replace("-", "_")
        if not name:
            continue
        for game in aliases.get(name, (name,)):
            if game not in VALID_GAMES:
                raise ConfigurationError(
                    f"unknown game: {item!r}; use u2, mw, carbon, or all"
                )
            if game not in selected:
                selected.append(game)
    if not selected and not allow_empty:
        raise ConfigurationError("select at least one game")
    return tuple(game for game in VALID_GAMES if game in selected)


def _int(values: Mapping[str, str], key: str) -> int:
    try:
        return int(values[key].strip())
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc


def _float(values: Mapping[str, str], key: str) -> float:
    try:
        return float(values[key].strip())
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a number") from exc


def _bool(values: Mapping[str, str], key: str) -> bool:
    value = values[key].strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{key} must be 0/1, true/false, yes/no, or on/off"
    )


def _validate_endpoint(value: str, key: str, *, loopback_only: bool = False) -> None:
    text = value.strip()
    if ":" not in text:
        raise ConfigurationError(f"{key} must be host:port")
    host, port_text = text.rsplit(":", 1)
    host = host.strip()
    if not host or any(char.isspace() for char in host):
        raise ConfigurationError(f"{key} has an invalid host")
    try:
        port = int(port_text.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{key} has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{key} has a port outside the 1..65535 range")
    if loopback_only and host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError(f"{key} must use loopback")


def validate_configuration(
    values: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    global_values = values["global"]
    color_mode = global_values["COLOR_MODE"].casefold()
    if color_mode not in {"auto", "always", "never"}:
        raise ConfigurationError("COLOR_MODE must be auto, always, or never")
    log_level = global_values["LOG_LEVEL"].upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(
            "LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    host = global_values["PUBLIC_HOST"].strip()
    if not host or any(char.isspace() for char in host) or ":" in host:
        raise ConfigurationError("PUBLIC_HOST must be a hostname or IPv4 address without a port")
    secret = global_values["IPC_SECRET"].strip()
    if secret.casefold() not in _AUTO_SECRET_VALUES and len(secret) < 32:
        raise ConfigurationError(
            "an explicit IPC_SECRET must contain at least 32 characters"
        )
    normalize_games(global_values["DEFAULT_GAMES"])
    if global_values["AUTH_MODE"].casefold() not in {"password", "open"}:
        raise ConfigurationError("AUTH_MODE must be password or open")
    _bool(global_values, "AUTH_AUTO_ENROLL")
    _bool(values["mw"], "CREATE_ACCOUNT_ON_FIRST_LOGIN")
    _bool(values["carbon"], "CHALLENGE_QUICK_JOIN_BEFORE_READY")
    _bool(values["carbon"], "CHALLENGE_QUICK_JOIN_AFTER_READY")
    _bool(values["carbon"], "DLC_STORE_ENABLED")
    if values["u2"]["GAME_SIZE_POLICY"].strip().casefold() not in {"client", "server"}:
        raise ConfigurationError("u2.GAME_SIZE_POLICY must be client or server")

    for scope, keys in {
        "global": ("MESSENGER_LISTEN", "WEB_LISTEN", "RACE_RELAY_LISTEN"),
        "u2": ("BOOTSTRAP_LISTEN", "LOBBY_LISTEN"),
        "mw": ("BOOTSTRAP_LISTEN", "LOBBY_LISTEN"),
        "carbon": (
            "FESL_LISTEN",
            "THEATER_LISTEN",
            "RACE_LISTEN",
            "MAD_LISTEN",
            "DLC_STORE_LISTEN",
        ),
    }.items():
        for key in keys:
            _validate_endpoint(values[scope][key], f"{scope}.{key}")
    _validate_endpoint(
        global_values["MESSENGER_IPC_LISTEN"],
        "global.MESSENGER_IPC_LISTEN",
        loopback_only=True,
    )

    for scope, keys in {
        "global": (
            "MESSENGER_PUBLIC_PORT",
            "WEB_PUBLIC_PORT",
            "RACE_RELAY_PUBLIC_PORT",
            "AUTH_FAILURE_LIMIT",
            "MAX_FRAME_SIZE",
            "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS",
        ),
        "u2": (
            "BOOTSTRAP_PUBLIC_PORT",
            "LOBBY_PUBLIC_PORT",
            "GAME_MIN_PLAYERS",
            "GAME_MAX_PLAYERS",
        ),
        "mw": ("BOOTSTRAP_PUBLIC_PORT", "LOBBY_PUBLIC_PORT"),
        "carbon": (
            "FESL_PUBLIC_PORT",
            "THEATER_PUBLIC_PORT",
            "RACE_PUBLIC_PORT",
            "MAD_PUBLIC_PORT",
        ),
    }.items():
        for key in keys:
            number = _int(values[scope], key)
            if key.endswith("_PORT") and not 1 <= number <= 65535:
                raise ConfigurationError(
                    f"{scope}.{key} must be between 1 and 65535"
                )

    if _int(global_values, "AUTH_FAILURE_LIMIT") < 1:
        raise ConfigurationError("AUTH_FAILURE_LIMIT must be at least 1")
    if not 12 <= _int(global_values, "MAX_FRAME_SIZE") <= 16 * 1024 * 1024:
        raise ConfigurationError("MAX_FRAME_SIZE must be between 12 and 16777216")
    if _int(global_values, "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS") < 0:
        raise ConfigurationError("ACCOUNT_SQLITE_BUSY_TIMEOUT_MS cannot be negative")

    u2_min = _int(values["u2"], "GAME_MIN_PLAYERS")
    u2_max = _int(values["u2"], "GAME_MAX_PLAYERS")
    if not 1 <= u2_min <= 8:
        raise ConfigurationError("u2.GAME_MIN_PLAYERS must be between 1 and 8")
    if not 1 <= u2_max <= 8:
        raise ConfigurationError("u2.GAME_MAX_PLAYERS must be between 1 and 8")
    if u2_min > u2_max:
        raise ConfigurationError(
            "u2.GAME_MIN_PLAYERS cannot be greater than GAME_MAX_PLAYERS"
        )

    for key in (
        "ACCOUNT_SESSION_LEASE_SECONDS",
        "CONNECTION_TIMEOUT",
        "DIRECTORY_TTL",
        "CLASSIC_LOBBY_HEARTBEAT_INTERVAL",
        "MESSENGER_IPC_MAX_AGE",
        "CARBON_SESSION_HEARTBEAT_INTERVAL",
        "MESSENGER_AUTH_IPC_WAIT",
    ):
        if _float(global_values, key) <= 0:
            raise ConfigurationError(f"{key} must be positive")
    if _float(global_values, "AUTH_LOCKOUT_SECONDS") < 0:
        raise ConfigurationError("AUTH_LOCKOUT_SECONDS cannot be negative")
    if _float(global_values, "CLASSIC_LOBBY_IDLE_TIMEOUT") < 0:
        raise ConfigurationError("CLASSIC_LOBBY_IDLE_TIMEOUT cannot be negative")

    carbon = values["carbon"]
    for key in (
        "FESL_HEARTBEAT_INTERVAL",
        "MESSENGER_IPC_POLL_INTERVAL",
        "MESSENGER_IPC_HEARTBEAT_INTERVAL",
        "MAD_SESSION_TIMEOUT_SECONDS",
        "JOIN_TIMEOUT_SECONDS",
        "RACE_IDLE_TIMEOUT_SECONDS",
        "LOADING_READY_FALLBACK_SECONDS",
    ):
        if _float(carbon, key) <= 0:
            raise ConfigurationError(f"carbon.{key} must be positive")
    if _float(carbon, "FESL_ACTIVITY_TIMEOUT") < 0:
        raise ConfigurationError("carbon.FESL_ACTIVITY_TIMEOUT cannot be negative")
    if _int(carbon, "MAD_ROTATION_SECONDS") < 0:
        raise ConfigurationError("carbon.MAD_ROTATION_SECONDS cannot be negative")
    if _float(carbon, "DLC_STORE_SESSION_SECONDS") <= 0:
        raise ConfigurationError("carbon.DLC_STORE_SESSION_SECONDS must be positive")
    if carbon["DLC_STORE_COOKIE_SECURE"].strip().casefold() not in {
        "auto",
        "always",
        "never",
    }:
        raise ConfigurationError(
            "carbon.DLC_STORE_COOKIE_SECURE must be auto, always, or never"
        )
    return values


def load_configuration(path: str | Path) -> dict[str, dict[str, str]]:
    return validate_configuration(read_configuration_file(path))


def looks_like_sectioned_ini(path: str | Path) -> bool:
    config_path = Path(path)
    try:
        lines = config_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {config_path}: {exc}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        return line.startswith("[")
    return False


def read_flat_config(path: str | Path) -> dict[str, str]:
    """Read the retired flat format for tests and migration tools only."""

    config_path = Path(path)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        config_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise ConfigurationError(
                f"{config_path}:{line_number}: expected KEY=value"
            )
        key, value = line.split("=", 1)
        name = key.strip().upper()
        if not name:
            raise ConfigurationError(f"{config_path}:{line_number}: empty key")
        values[name] = value.strip()
    return values


def package_root_for(config_path: str | Path) -> Path:
    path = Path(config_path).expanduser().resolve()
    return path.parent.parent if path.parent.name.casefold() == "config" else path.parent


def resolve_package_path(root: Path, value: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Classic and Carbon can start almost at the same time.  A per-process
    # temporary name avoids both processes writing the same .tmp file.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{path.name} is invalid: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _resolve_ipv4(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _is_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def _detect_local_ipv4(public_host: str) -> str:
    for target in ((public_host, 53), ("1.1.1.1", 53)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            address = str(sock.getsockname()[0]).strip()
            if _is_ipv4(address) and address != "0.0.0.0":
                return address
        except OSError:
            pass
        finally:
            sock.close()
    try:
        address = socket.gethostbyname(socket.gethostname())
        if _is_ipv4(address) and address != "0.0.0.0":
            return address
    except OSError:
        pass
    return "127.0.0.1"


def prepare_runtime_context(
    config_path: str | Path,
    games: str | Iterable[str] | None,
    *,
    state_path: str | Path | None = None,
    package_name: str = "NFS Online Server",
    environment: Mapping[str, str] | None = None,
    persist_state: bool = True,
) -> RuntimeContext:
    """Validate deployment config, resolve transient values and persist durable state."""

    path = Path(config_path).expanduser().resolve()
    configuration = load_configuration(path)
    global_values = configuration["global"]
    selected = normalize_games(
        global_values["DEFAULT_GAMES"] if games is None else games,
        allow_empty=True,
    )
    root = package_root_for(path)
    state_file = Path(state_path).expanduser().resolve() if state_path else root / "data" / "server-state.json"
    state = _read_state(state_file)
    env = os.environ if environment is None else environment

    public_host = str(env.get("NFS_PUBLIC_HOST") or global_values["PUBLIC_HOST"]).strip()
    if not public_host or any(ch.isspace() for ch in public_host) or ":" in public_host:
        raise ConfigurationError(
            "NFS_PUBLIC_HOST/PUBLIC_HOST must be a hostname or IPv4 address without a port"
        )

    local_setting = str(
        env.get("NFS_LOCAL_IPV4") or global_values["LOCAL_ADVERTISE_HOST"]
    ).strip()
    local_ipv4 = (
        _detect_local_ipv4(public_host)
        if local_setting.casefold() in _AUTO_LOCAL_VALUES
        else local_setting
    )
    if not _is_ipv4(local_ipv4):
        raise ConfigurationError(
            "NFS_LOCAL_IPV4/LOCAL_ADVERTISE_HOST must be auto or an IPv4 address without a port"
        )

    public_ipv4 = str(env.get("NFS_PUBLIC_IPV4") or "").strip()
    warning: str | None = None
    resolved_from_dns = False
    if not public_ipv4:
        public_ipv4 = _resolve_ipv4(public_host) or ""
        resolved_from_dns = bool(public_ipv4)
    if not public_ipv4:
        cached = str(state.get("last_public_ipv4") or "").strip()
        public_ipv4 = cached if _is_ipv4(cached) else local_ipv4
        warning = (
            f"{public_host} did not resolve through DNS; the relay temporarily uses "
            f"{public_ipv4}."
        )
    if not _is_ipv4(public_ipv4):
        raise ConfigurationError("the resolved public address is not a valid IPv4 address")

    configured_secret = global_values["IPC_SECRET"].strip()
    ipc_secret = str(env.get("NFS_IPC_SECRET") or "").strip()
    if not ipc_secret:
        if configured_secret.casefold() in _AUTO_SECRET_VALUES:
            ipc_secret = str(state.get("ipc_secret") or "").strip()
            if len(ipc_secret) < 32:
                ipc_secret = secrets.token_hex(32)
        else:
            ipc_secret = configured_secret
    if len(ipc_secret) < 32:
        raise ConfigurationError("the IPC secret must contain at least 32 characters")

    normalized_state: dict[str, object] = {
        "schema": 3,
        "ipc_secret": ipc_secret,
        "created_by": package_name,
    }
    if resolved_from_dns:
        normalized_state["last_public_ipv4"] = public_ipv4
    else:
        cached = str(state.get("last_public_ipv4") or "").strip()
        if _is_ipv4(cached):
            normalized_state["last_public_ipv4"] = cached
    if persist_state:
        _atomic_json(state_file, normalized_state)

    return RuntimeContext(
        config_path=path,
        package_root=root,
        games=selected,
        public_host=public_host,
        public_ipv4=public_ipv4,
        local_ipv4=local_ipv4,
        ipc_secret=ipc_secret,
        warning=warning,
    )


def _service_context(
    config_path: str | Path,
    games: str | Iterable[str] | None,
    environment: Mapping[str, str] | None,
) -> tuple[dict[str, dict[str, str]], RuntimeContext]:
    path = Path(config_path).expanduser().resolve()
    configuration = load_configuration(path)
    root = package_root_for(path)
    env = os.environ if environment is None else environment
    context = prepare_runtime_context(
        path,
        games,
        state_path=root / "data" / "server-state.json",
        environment=env,
        # The launcher already persisted the state and gives both services the
        # exact same secret/IP values.  Child readers therefore stay read-only.
        persist_state=not bool(str(env.get("NFS_IPC_SECRET") or "").strip()),
    )
    return configuration, context


def _endpoint(host: str, port: int) -> str:
    return f"{host}:{port}"


def classic_service_values(
    config_path: str | Path,
    *,
    games: str | Iterable[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    configuration, runtime = _service_context(config_path, games, environment)
    global_values = configuration["global"]
    u2 = configuration["u2"]
    mw = configuration["mw"]
    root = runtime.package_root
    selected = set(runtime.games)

    return {
        "ENABLE_U2": "1" if "u2" in selected else "0",
        "ENABLE_MW": "1" if "mw" in selected else "0",
        "LOCAL_ADVERTISE_HOST": runtime.local_ipv4,
        "U2_BOOTSTRAP_LISTEN": u2["BOOTSTRAP_LISTEN"],
        "U2_BOOTSTRAP_PUBLIC": _endpoint(runtime.public_ipv4, _int(u2, "BOOTSTRAP_PUBLIC_PORT")),
        "U2_LOBBY_LISTEN": u2["LOBBY_LISTEN"],
        "U2_LOBBY_PUBLIC": _endpoint(runtime.public_ipv4, _int(u2, "LOBBY_PUBLIC_PORT")),
        "U2_DIRECTORY_SESSION": u2["DIRECTORY_SESSION"],
        "U2_DIRECTORY_MASK": u2["DIRECTORY_MASK"],
        "U2_GAME_SIZE_POLICY": u2["GAME_SIZE_POLICY"].strip().casefold(),
        "U2_GAME_MIN_PLAYERS": str(_int(u2, "GAME_MIN_PLAYERS")),
        "U2_GAME_MAX_PLAYERS": str(_int(u2, "GAME_MAX_PLAYERS")),
        "MW_BOOTSTRAP_LISTEN": mw["BOOTSTRAP_LISTEN"],
        "MW_BOOTSTRAP_PUBLIC": _endpoint(runtime.public_ipv4, _int(mw, "BOOTSTRAP_PUBLIC_PORT")),
        "MW_LOBBY_LISTEN": mw["LOBBY_LISTEN"],
        "MW_LOBBY_PUBLIC": _endpoint(runtime.public_ipv4, _int(mw, "LOBBY_PUBLIC_PORT")),
        "MW_DIRECTORY_SESSION": mw["DIRECTORY_SESSION"],
        "MW_DIRECTORY_MASK": mw["DIRECTORY_MASK"],
        "MESSENGER_LISTEN": global_values["MESSENGER_LISTEN"],
        "MESSENGER_PUBLIC": _endpoint(runtime.public_ipv4, _int(global_values, "MESSENGER_PUBLIC_PORT")),
        "CARBON_MESSENGER_IPC_LISTEN": global_values["MESSENGER_IPC_LISTEN"],
        "CARBON_MESSENGER_IPC_SECRET": runtime.ipc_secret,
        "CARBON_MESSENGER_IPC_MAX_AGE": global_values["MESSENGER_IPC_MAX_AGE"],
        "CARBON_MESSENGER_HEARTBEAT_INTERVAL": global_values["CARBON_SESSION_HEARTBEAT_INTERVAL"],
        "CARBON_MESSENGER_AUTH_IPC_WAIT": global_values["MESSENGER_AUTH_IPC_WAIT"],
        "WEB_LISTEN": global_values["WEB_LISTEN"],
        "WEB_PUBLIC": _endpoint(runtime.public_ipv4, _int(global_values, "WEB_PUBLIC_PORT")),
        "RACE_LISTEN": global_values["RACE_RELAY_LISTEN"],
        "RACE_PUBLIC": _endpoint(runtime.public_ipv4, _int(global_values, "RACE_RELAY_PUBLIC_PORT")),
        "RACE_VIRTUAL_NETWORK": global_values["RACE_VIRTUAL_NETWORK"],
        "AUTH_MODE": global_values["AUTH_MODE"],
        "AUTH_DATA": str((root / "data" / "classic" / "auth.json").resolve()),
        "SOCIAL_DATA": str((root / "data" / "classic" / "social.json").resolve()),
        "STATS_DATA": str((root / "data" / "classic" / "stats.json").resolve()),
        "AUTH_AUTO_ENROLL": global_values["AUTH_AUTO_ENROLL"],
        "U2_AUTH_AUTO_ENROLL": global_values["AUTH_AUTO_ENROLL"],
        "MW_AUTH_AUTO_ENROLL": mw["CREATE_ACCOUNT_ON_FIRST_LOGIN"],
        "AUTH_FAILURE_LIMIT": global_values["AUTH_FAILURE_LIMIT"],
        "AUTH_LOCKOUT_SECONDS": global_values["AUTH_LOCKOUT_SECONDS"],
        "MAX_FRAME_SIZE": global_values["MAX_FRAME_SIZE"],
        "CONNECTION_TIMEOUT": global_values["CONNECTION_TIMEOUT"],
        "CLASSIC_DIRECTORY_TTL": global_values["DIRECTORY_TTL"],
        "CLASSIC_LOBBY_IDLE_TIMEOUT": global_values["CLASSIC_LOBBY_IDLE_TIMEOUT"],
        "CLASSIC_LOBBY_HEARTBEAT_INTERVAL": global_values["CLASSIC_LOBBY_HEARTBEAT_INTERVAL"],
        "ACCOUNT_DB": str(resolve_package_path(root, global_values["ACCOUNT_DB"])),
        "ACCOUNT_FILES": str(resolve_package_path(root, global_values["ACCOUNT_FILES"])),
        "ACCOUNT_SESSION_LEASE_SECONDS": global_values["ACCOUNT_SESSION_LEASE_SECONDS"],
        "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS": global_values["ACCOUNT_SQLITE_BUSY_TIMEOUT_MS"],
    }


def carbon_service_values(
    config_path: str | Path,
    *,
    games: str | Iterable[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    del games  # Carbon is launched only when selected; it has no sub-game toggle.
    configuration, runtime = _service_context(config_path, ("carbon",), environment)
    global_values = configuration["global"]
    carbon = configuration["carbon"]
    root = runtime.package_root

    return {
        "GAME": "carbon",
        "FESL_LISTEN": carbon["FESL_LISTEN"],
        "FESL_PUBLIC": _endpoint(runtime.public_ipv4, _int(carbon, "FESL_PUBLIC_PORT")),
        "MESSENGER_PUBLIC": _endpoint(runtime.public_ipv4, _int(global_values, "MESSENGER_PUBLIC_PORT")),
        "MESSENGER_IPC": global_values["MESSENGER_IPC_LISTEN"],
        "MESSENGER_IPC_SECRET": runtime.ipc_secret,
        "MESSENGER_IPC_POLL_INTERVAL": carbon["MESSENGER_IPC_POLL_INTERVAL"],
        "MESSENGER_IPC_HEARTBEAT_INTERVAL": carbon["MESSENGER_IPC_HEARTBEAT_INTERVAL"],
        "FESL_ACTIVITY_TIMEOUT": carbon["FESL_ACTIVITY_TIMEOUT"],
        "FESL_HEARTBEAT_INTERVAL": carbon["FESL_HEARTBEAT_INTERVAL"],
        "THEATER_LISTEN": carbon["THEATER_LISTEN"],
        "THEATER_PUBLIC": _endpoint(runtime.public_ipv4, _int(carbon, "THEATER_PUBLIC_PORT")),
        "RACE_LISTEN": carbon["RACE_LISTEN"],
        "RACE_PUBLIC": _endpoint(runtime.public_ipv4, _int(carbon, "RACE_PUBLIC_PORT")),
        "RACE_LOCAL": _endpoint(runtime.local_ipv4, _int(carbon, "RACE_PUBLIC_PORT")),
        "MAD_LISTEN": carbon["MAD_LISTEN"],
        "MAD_PUBLIC": _endpoint(runtime.public_ipv4, _int(carbon, "MAD_PUBLIC_PORT")),
        "MAD_CAMPAIGNS": str(resolve_package_path(root, carbon["MAD_CAMPAIGNS"])),
        "MAD_ROTATION_SECONDS": carbon["MAD_ROTATION_SECONDS"],
        "MAD_SESSION_TIMEOUT_SECONDS": carbon["MAD_SESSION_TIMEOUT_SECONDS"],
        "MAD_IMPRESSION_LOG": str(resolve_package_path(root, carbon["MAD_IMPRESSION_LOG"])),
        "MAX_FRAME_SIZE": global_values["MAX_FRAME_SIZE"],
        "CONNECTION_TIMEOUT": global_values["CONNECTION_TIMEOUT"],
        "ACCOUNT_DATA": str((root / "data" / "carbon" / "progression.json").resolve()),
        "ACCOUNT_DB": str(resolve_package_path(root, global_values["ACCOUNT_DB"])),
        "ACCOUNT_FILES": str(resolve_package_path(root, global_values["ACCOUNT_FILES"])),
        "ACCOUNT_SESSION_LEASE_SECONDS": global_values["ACCOUNT_SESSION_LEASE_SECONDS"],
        "ACCOUNT_SQLITE_BUSY_TIMEOUT_MS": global_values["ACCOUNT_SQLITE_BUSY_TIMEOUT_MS"],
        "AUTH_MODE": global_values["AUTH_MODE"],
        "AUTH_DATA": str((root / "data" / "carbon" / "auth.json").resolve()),
        "AUTH_AUTO_ENROLL": global_values["AUTH_AUTO_ENROLL"],
        "AUTH_FAILURE_LIMIT": global_values["AUTH_FAILURE_LIMIT"],
        "AUTH_LOCKOUT_SECONDS": global_values["AUTH_LOCKOUT_SECONDS"],
        "AUTH_LOGIN_ERROR_PROBE_CODE": carbon["AUTH_LOGIN_ERROR_PROBE_CODE"],
        "CARBON_CHALLENGE_QUICK_JOIN_BEFORE_READY": carbon["CHALLENGE_QUICK_JOIN_BEFORE_READY"],
        "CARBON_CHALLENGE_QUICK_JOIN_AFTER_READY": carbon["CHALLENGE_QUICK_JOIN_AFTER_READY"],
        "CARBON_JOIN_TIMEOUT_SECONDS": carbon["JOIN_TIMEOUT_SECONDS"],
        "CARBON_RACE_IDLE_TIMEOUT_SECONDS": carbon["RACE_IDLE_TIMEOUT_SECONDS"],
        "CARBON_LOADING_READY_FALLBACK_SECONDS": carbon[
            "LOADING_READY_FALLBACK_SECONDS"
        ],
        "CARBON_DLC_CATALOG": str(resolve_package_path(root, carbon["DLC_CATALOG"])),
        "CARBON_DLC_ASSIGNMENTS": str(resolve_package_path(root, carbon["DLC_ASSIGNMENTS"])),
        "CARBON_DLC_STORE_ENABLED": carbon["DLC_STORE_ENABLED"],
        "CARBON_DLC_STORE_LISTEN": carbon["DLC_STORE_LISTEN"],
        "CARBON_DLC_STORE_SESSION_SECONDS": carbon["DLC_STORE_SESSION_SECONDS"],
        "CARBON_DLC_STORE_COOKIE_SECURE": carbon["DLC_STORE_COOKIE_SECURE"],
    }


def load_service_values(
    config_path: str | Path,
    service: str,
    *,
    games: str | Iterable[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    normalized = service.strip().casefold()
    if normalized not in {"classic", "carbon"}:
        raise ConfigurationError(f"unknown service configuration: {service}")
    if not looks_like_sectioned_ini(config_path):
        return read_flat_config(config_path)
    if normalized == "classic":
        return classic_service_values(
            config_path,
            games=games,
            environment=environment,
        )
    return carbon_service_values(
        config_path,
        games=games,
        environment=environment,
    )
