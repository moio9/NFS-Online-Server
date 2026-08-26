#!/usr/bin/env python3
"""Unified launcher, service manager and administration console for NFS Online.

Underground 2 and Most Wanted run in one classic service; Carbon runs in a
separate service. Every component reads config/server.toml directly.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from typing import IO, Iterable, Iterator, Sequence

try:  # GNU readline is available on Linux/Termux and improves prompt redraw.
    import readline as _readline
except ImportError:  # pragma: no cover - Windows normally lacks GNU readline
    _readline = None

ROOT = Path(__file__).resolve().parent
CONFIG_ROOT = ROOT / "config"
CONFIG_FILE = CONFIG_ROOT / "server.toml"
SERVER_ROOT = ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from common.config import (  # noqa: E402 - SERVER_ROOT is package-local
    CLASSIC_GAMES,
    CONFIG_SECTIONS,
    VALID_GAMES,
    ConfigurationError,
    RuntimeContext,
    load_configuration as load_deployment_configuration,
    normalize_games as normalize_config_games,
    prepare_runtime_context as build_runtime_context,
    read_configuration_file as read_deployment_config,
)
COMMON_ROOT = SERVER_ROOT / "common"
CLASSIC_ROOT = SERVER_ROOT / "classic"
CARBON_ROOT = SERVER_ROOT / "carbon"
CLIENT_ROOT = ROOT / "clients"
DATA_ROOT = ROOT / "data"
RUN_ROOT = ROOT / "runtime"
LOG_ROOT = ROOT / "logs"
STATE_PATH = DATA_ROOT / "server-state.json"
PID_PATH = RUN_ROOT / "services.json"
ACCOUNT_DB = DATA_ROOT / "accounts.sqlite3"
CONSOLE_PROMPT = "nfs> "
PACKAGE_VERSION = "1.1.3"
PACKAGE_NAME = f"NFS Online Server {PACKAGE_VERSION}"

SERVICES = {
    "classic": {
        "label": "Classic",
        "root": CLASSIC_ROOT,
        "module": "classic",
        "config": CONFIG_FILE,
        "log": LOG_ROOT / "classic.log",
    },
    "carbon": {
        "label": "Carbon",
        "root": CARBON_ROOT,
        "module": "carbon",
        "config": CONFIG_FILE,
        "log": LOG_ROOT / "carbon.log",
    },
}

class LauncherError(RuntimeError):
    """User-facing launcher error."""


ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "gray": "\033[90m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m",
    "bright_cyan": "\033[96m",
}

LABEL_COLORS = {
    "U2": "bright_cyan",
    "MW": "bright_yellow",
    "Carbon": "bright_magenta",
    "Messenger": "bright_blue",
    "Race": "bright_green",
    "Shared": "gray",
}


def read_configuration_file(path: Path | None = None) -> dict[str, dict[str, str]]:
    config_path = CONFIG_FILE if path is None else path
    try:
        return read_deployment_config(config_path)
    except ConfigurationError as exc:
        raise LauncherError(str(exc)) from exc


def load_named_config(name: str) -> dict[str, str]:
    if name not in CONFIG_SECTIONS:
        raise LauncherError(f"unknown configuration: {name}")
    return read_configuration_file()[name]


def load_configuration() -> dict[str, dict[str, str]]:
    try:
        return load_deployment_configuration(CONFIG_FILE)
    except ConfigurationError as exc:
        raise LauncherError(str(exc)) from exc


def update_config_value(path: Path, section: str, key: str, value: str) -> None:
    section_name = section.strip()
    normalized_section = section_name.casefold()
    normalized_key = key.strip().upper()
    if not section_name or not normalized_key:
        raise LauncherError("configuration section and key are required")

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LauncherError(f"cannot read configuration {path}: {exc}") from exc

    result: list[str] = []
    in_target = False
    section_found = False
    key_replaced = False
    key_inserted = False
    assignment = (
        f"{normalized_key} = {json.dumps(str(value), ensure_ascii=False)}"
        if path.suffix.casefold() == ".toml"
        else f"{normalized_key}={value}"
    )

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_target and not key_replaced and not key_inserted:
                result.append(assignment)
                key_inserted = True
            current_section = stripped[1:-1].strip().casefold()
            in_target = current_section == normalized_section
            section_found = section_found or in_target
            result.append(line)
            continue
        if in_target and stripped and not stripped.startswith(("#", ";")) and "=" in line:
            current_key = line.split("=", 1)[0].strip().upper()
            if current_key == normalized_key:
                result.append(assignment)
                key_replaced = True
                continue
        result.append(line)

    if not section_found:
        if result and result[-1].strip():
            result.append("")
        result.extend((f"[{section_name}]", assignment))
    elif in_target and not key_replaced and not key_inserted:
        result.append(assignment)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    os.replace(temporary, path)



def resolve_package_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def atomic_json(path: Path, value: object) -> None:
    """Atomically write private launcher state such as runtime/services.json."""

    path.parent.mkdir(parents=True, exist_ok=True)
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


def ensure_configured_data_layout(
    configuration: dict[str, dict[str, str]],
) -> None:
    """Create directories referenced by the single deployment TOML."""

    global_values = configuration["global"]
    carbon_values = configuration["carbon"]
    resolve_package_path(global_values["ACCOUNT_DB"]).parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    resolve_package_path(global_values["ACCOUNT_FILES"]).mkdir(
        parents=True,
        exist_ok=True,
    )
    for value in (
        carbon_values["MAD_CAMPAIGNS"],
        carbon_values["MAD_IMPRESSION_LOG"],
        carbon_values["DLC_CATALOG"],
        carbon_values["DLC_ASSIGNMENTS"],
    ):
        resolve_package_path(value).parent.mkdir(parents=True, exist_ok=True)



def configured_account_db() -> Path:
    try:
        return resolve_package_path(load_named_config("global")["ACCOUNT_DB"])
    except LauncherError:
        return ACCOUNT_DB


def should_use_colors(mode: str, stream: IO[str]) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    normalized = mode.strip().casefold()
    if normalized == "always":
        return True
    if normalized == "never":
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM", "") != "dumb"


def colored(text: str, color: str, enabled: bool, *, bold: bool = False) -> str:
    if not enabled:
        return text
    prefix = ANSI.get(color, "")
    if bold:
        prefix = ANSI["bold"] + prefix
    return f"{prefix}{text}{ANSI['reset']}"


def fail(message: str, code: int = 2) -> "None":
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def ensure_layout() -> None:
    required = (
        CONFIG_FILE,
        COMMON_ROOT / "__init__.py",
        CLASSIC_ROOT / "__main__.py",
        CARBON_ROOT / "__main__.py",
        CLIENT_ROOT / "underground2" / "net_u2.ini",
        CLIENT_ROOT / "most-wanted" / "net_mw.ini",
        CLIENT_ROOT / "carbon" / "net_carbon.ini",
        ROOT / "source" / "client-zig" / "build.zig",
    )
    missing = [str(item.relative_to(ROOT)) for item in required if not item.is_file()]
    if missing:
        fail("package files are missing: " + ", ".join(missing))
    if sys.version_info < (3, 10):
        fail(f"Python 3.10+ is required; current version is {sys.version.split()[0]}")
    try:
        sqlite3.connect(":memory:").close()
    except Exception as exc:  # pragma: no cover - platform failure
        fail(f"the Python sqlite3 module is unavailable: {exc}")
    for directory in (
        DATA_ROOT,
        DATA_ROOT / "users",
        DATA_ROOT / "backups",
        DATA_ROOT / "classic",
        DATA_ROOT / "carbon",
        RUN_ROOT,
        LOG_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for obsolete in (RUN_ROOT / "classic.cfg", RUN_ROOT / "carbon.cfg"):
        obsolete.unlink(missing_ok=True)


def missing_client_binaries() -> tuple[Path, ...]:
    """Return release-only ASI builds absent from a source checkout."""

    expected = (
        CLIENT_ROOT / "underground2" / "net_u2.asi",
        CLIENT_ROOT / "most-wanted" / "net_mw.asi",
        CLIENT_ROOT / "carbon" / "net_carbon.asi",
    )
    return tuple(path for path in expected if not path.is_file())


def prepare_runtime_context(games: Sequence[str]) -> RuntimeContext:
    ensure_layout()
    selected = normalize_games(games, allow_empty=True)
    configuration = load_configuration()
    ensure_configured_data_layout(configuration)
    environment = dict(os.environ)
    environment["NFS_PACKAGE_ROOT"] = str(ROOT)
    environment["NFS_STATE_PATH"] = str(STATE_PATH)
    try:
        context = build_runtime_context(
            CONFIG_FILE,
            selected,
            state_path=STATE_PATH,
            package_name=PACKAGE_NAME,
            environment=environment,
            persist_state=True,
        )
    except ConfigurationError as exc:
        raise LauncherError(str(exc)) from exc
    if context.warning:
        print(f"Warning: {context.warning}", file=sys.stderr)
    return context


def normalize_games(
    value: str | Iterable[str] | None,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    try:
        return normalize_config_games(value, allow_empty=allow_empty)
    except ConfigurationError as exc:
        raise LauncherError(str(exc)) from exc


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid_state() -> dict[str, object]:
    if not PID_PATH.is_file():
        return {}
    try:
        value = json.loads(PID_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def running_services() -> dict[str, int]:
    state = read_pid_state()
    services = state.get("services", {})
    result: dict[str, int] = {}
    if isinstance(services, dict):
        for name, raw in services.items():
            try:
                pid = int(raw)
            except (TypeError, ValueError):
                continue
            if process_alive(pid):
                result[str(name)] = pid
    return result


def write_pid_state(
    processes: dict[str, subprocess.Popen[object]],
    *,
    mode: str,
    games: Sequence[str],
) -> None:
    alive = {name: process.pid for name, process in processes.items() if process.poll() is None}
    if not alive:
        PID_PATH.unlink(missing_ok=True)
        return
    atomic_json(
        PID_PATH,
        {
            "schema": 2,
            "launcher_pid": os.getpid(),
            "mode": mode,
            "started_at": int(time.time()),
            "games": list(games),
            "services": alive,
        },
    )


def readiness_endpoint(value: str) -> tuple[str, int]:
    host, port_text = value.strip().rsplit(":", 1)
    host = host.strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    elif host.casefold() == "localhost":
        host = "127.0.0.1"
    return host, int(port_text.strip())


def port_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_for_readiness(
    process: subprocess.Popen[object],
    host: str,
    port: int,
    timeout: float,
    *,
    stable_for: float = 0.25,
) -> bool:
    """Require both the port and the newly spawned process to stay alive.

    A stale server can already own the configured readiness port.  Returning on
    the first successful connect would then report a new child as ready during
    the short window before it exits with ``Address already in use``.
    """
    deadline = time.monotonic() + timeout
    ready_since: float | None = None
    while True:
        now = time.monotonic()
        if now >= deadline or process.poll() is not None:
            return False
        if port_ready(host, port):
            if ready_since is None:
                ready_since = now
            elif now - ready_since >= stable_for:
                return process.poll() is None
        else:
            ready_since = None
        time.sleep(0.05)


def child_environment(context: RuntimeContext | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    existing_pythonpath = environment.get("PYTHONPATH", "").strip()
    environment["PYTHONPATH"] = (
        str(SERVER_ROOT)
        if not existing_pythonpath
        else str(SERVER_ROOT) + os.pathsep + existing_pythonpath
    )
    environment["NFS_LOG_LEVEL"] = load_named_config("global")["LOG_LEVEL"].upper()
    environment["NFS_PACKAGE_ROOT"] = str(ROOT)
    environment["NFS_STATE_PATH"] = str(STATE_PATH)
    if context is not None:
        environment.update(context.environment())
    return environment


def command_for(service: str) -> list[str]:
    info = SERVICES[service]
    return [sys.executable, "-u", "-m", str(info["module"]), "--config", str(info["config"])]


def start_child(
    service: str,
    *,
    daemon: bool,
    context: RuntimeContext,
) -> tuple[subprocess.Popen[object], IO[str] | None]:
    info = SERVICES[service]
    log_path = Path(info["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[str] | None = None
    kwargs: dict[str, object] = {
        "cwd": str(info["root"]),
        "env": child_environment(context),
        "stdin": subprocess.DEVNULL,
        "start_new_session": True,
    }
    if daemon:
        handle = log_path.open("a", encoding="utf-8", buffering=1)
        kwargs.update(stdout=handle, stderr=subprocess.STDOUT, text=True)
    else:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    process = subprocess.Popen(command_for(service), **kwargs)
    return process, handle


def terminate_process(process: subprocess.Popen[object], timeout: float = 8.0) -> None:
    """Stop a child process and reap it so it cannot remain a zombie."""
    if process.poll() is not None:
        return
    pid = process.pid
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def terminate_pid(pid: int, timeout: float = 8.0) -> None:
    if not process_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.1)
    if process_alive(pid):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def stop_all(*, quiet: bool = False) -> int:
    services = running_services()
    if not services:
        PID_PATH.unlink(missing_ok=True)
        if not quiet:
            print("The servers are not running.")
        return 0
    for name in ("carbon", "classic"):
        pid = services.get(name)
        if pid:
            if not quiet:
                print(f"Stopping {SERVICES[name]['label']} (PID {pid})...")
            terminate_pid(pid)
    PID_PATH.unlink(missing_ok=True)
    if not quiet:
        print("All services have been stopped.")
    return 0


def output_label(service: str, line: str) -> str:
    if service == "carbon":
        return "Carbon"
    text = line.casefold()
    if "[classic.build]" in text or "classic game services enabled" in text:
        return "Shared"
    if "underground2" in text or "underground 2" in text or ".underground2" in text:
        return "U2"
    if "most_wanted" in text or "most wanted" in text or ".most_wanted" in text:
        return "MW"
    if "carbon_messenger" in text or "ea messenger" in text:
        return "Messenger"
    if "race udp" in text or "race relay" in text:
        return "Race"
    return "Shared"


class ConsolePrinter:
    """Serialize colored log and command output while an input prompt is active."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.logs_enabled = True
        self.prompt_active = False
        self._suspend_count = 0
        self.tty = bool(sys.stdout.isatty())
        self.color_mode = load_named_config("global")["COLOR_MODE"].casefold()
        self.colors_enabled = should_use_colors(self.color_mode, sys.stdout)
        if self.colors_enabled and os.name == "nt":
            os.system("")  # Enable ANSI processing on current Windows terminals.

    def set_color_mode(self, mode: str, *, persist: bool = True) -> None:
        normalized = mode.strip().casefold()
        aliases = {"on": "always", "off": "never"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"auto", "always", "never"}:
            raise LauncherError("colors must be auto, on/always, or off/never")
        self.color_mode = normalized
        self.colors_enabled = should_use_colors(normalized, sys.stdout)
        if persist:
            update_config_value(CONFIG_FILE, CONFIG_SECTIONS["global"], "COLOR_MODE", normalized)

    def _prompt(self) -> str:
        return colored(CONSOLE_PROMPT, "bright_green", self.colors_enabled, bold=True)

    def _style_message(self, message: str, *, error: bool) -> str:
        if not self.colors_enabled or not message:
            return message
        if error or message.startswith("Error"):
            return colored(message, "bright_red", True, bold=True)
        if message.startswith(("Warning",)):
            return colored(message, "bright_yellow", True, bold=True)
        if message.startswith(("Started", "Active", "Session released", "Structure")):
            return colored(message, "bright_green", True)
        if message.startswith(("Stopping", "Restart")):
            return colored(message, "yellow", True)
        return message

    def write(self, message: str = "", *, error: bool = False) -> None:
        stream = sys.stderr if error else sys.stdout
        with self.lock:
            print(self._style_message(message, error=error), file=stream, flush=True)

    def _format_log(self, label: str, line: str) -> str:
        prefix = colored(
            f"[{label}]",
            LABEL_COLORS.get(label, "white"),
            self.colors_enabled,
            bold=True,
        )
        content = line.rstrip("\n")
        if self.colors_enabled:
            if " CRITICAL:" in content or " ERROR:" in content:
                content = colored(content, "bright_red", True)
            elif " WARNING:" in content:
                content = colored(content, "bright_yellow", True)
            elif " DEBUG:" in content:
                content = colored(content, "gray", True)
        return f"{prefix} {content}"

    def emit_log(self, label: str, line: str) -> None:
        if not self.logs_enabled or self._suspend_count:
            return
        rendered = self._format_log(label, line) + "\n"
        with self.lock:
            if self.prompt_active and self.tty:
                buffer = ""
                if _readline is not None:
                    try:
                        buffer = _readline.get_line_buffer()
                    except Exception:
                        buffer = ""
                sys.stdout.write("\r\033[2K")
                sys.stdout.write(rendered)
                sys.stdout.write(self._prompt() + buffer)
                sys.stdout.flush()
                if _readline is not None:
                    try:
                        _readline.redisplay()
                    except Exception:
                        pass
            else:
                sys.stdout.write(rendered)
                sys.stdout.flush()

    def input(self) -> str:
        self.prompt_active = True
        try:
            return input(self._prompt())
        finally:
            self.prompt_active = False

    @contextmanager
    def suspend_logs(self) -> Iterator[None]:
        self._suspend_count += 1
        try:
            yield
        finally:
            self._suspend_count = max(0, self._suspend_count - 1)


def tee_output(
    service: str,
    process: subprocess.Popen[object],
    stop_event: threading.Event,
    printer: ConsolePrinter,
) -> None:
    log_path = Path(SERVICES[service]["log"])
    stream = process.stdout
    if stream is None:
        return
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        for line in iter(stream.readline, ""):
            log.write(line)
            printer.emit_log(output_label(service, line), line)
            if stop_event.is_set():
                break


class RuntimeManager:
    """Own the child processes used by the foreground interactive console."""

    def __init__(self, printer: ConsolePrinter, *, daemon: bool = False) -> None:
        self.printer = printer
        self.daemon = daemon
        self.games: tuple[str, ...] = ()
        self.context: RuntimeContext | None = None
        self.processes: dict[str, subprocess.Popen[object]] = {}
        self.handles: list[IO[str]] = []
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self._lock = threading.RLock()

    def _spawn(self, service: str) -> subprocess.Popen[object]:
        if self.context is None:
            raise LauncherError("the runtime context has not been prepared")
        process, handle = start_child(
            service,
            daemon=self.daemon,
            context=self.context,
        )
        self.processes[service] = process
        if handle is not None:
            self.handles.append(handle)
        if not self.daemon:
            thread = threading.Thread(
                target=tee_output,
                args=(service, process, self.stop_event, self.printer),
                daemon=True,
                name=f"{service}-log",
            )
            thread.start()
            self.threads.append(thread)
        return process

    def _stop_service(self, service: str, *, announce: bool = True) -> None:
        process = self.processes.pop(service, None)
        if process is None or process.poll() is not None:
            return
        if announce:
            self.printer.write(f"Stopping {SERVICES[service]['label']} (PID {process.pid})...")
        terminate_process(process)

    def _start_classic(self) -> None:
        process = self._spawn("classic")
        host, port = readiness_endpoint(
            load_named_config("global")["MESSENGER_IPC_LISTEN"]
        )
        if not wait_for_readiness(process, host, port, 12.0):
            self._stop_service("classic", announce=False)
            raise LauncherError(
                f"Classic did not open IPC/Messenger {host}:{port}; "
                f"check {SERVICES['classic']['log']}"
            )

    def _start_carbon(self) -> None:
        process = self._spawn("carbon")
        host, port = readiness_endpoint(load_named_config("carbon")["FESL_LISTEN"])
        if not wait_for_readiness(process, host, port, 12.0):
            self._stop_service("carbon", announce=False)
            raise LauncherError(
                f"Carbon did not open FESL {host}:{port}; "
                f"check {SERVICES['carbon']['log']}"
            )

    def _persist(self) -> None:
        write_pid_state(
            self.processes,
            mode="daemon" if self.daemon else "console",
            games=self.games,
        )

    def start_initial(self, games: Sequence[str]) -> None:
        selected = normalize_games(games)
        self.context = prepare_runtime_context(selected)
        self.printer.write(f"Public host: {self.context.public_host}")
        self.printer.write(f"Effective public IPv4: {self.context.public_ipv4}")
        self.printer.write(f"Detected local IPv4: {self.context.local_ipv4}")
        self._start_classic()
        if "carbon" in selected:
            self._start_carbon()
        self.games = selected
        self._persist()
        self.printer.write("Started: " + " + ".join(game.upper() for game in selected) + " + shared EA Messenger.")
        self.printer.write(f"Logs: {LOG_ROOT}")

    def apply_games(self, games: Sequence[str]) -> None:
        target = normalize_games(games, allow_empty=True)
        with self._lock:
            old = self.games
            if target == old:
                self.printer.write("The selection is already active: " + (", ".join(target) or "nothing"))
                return
            if not target:
                self._stop_service("carbon")
                self._stop_service("classic")
                self.games = ()
                self.context = None
                self._persist()
                self.printer.write("All games are stopped; the console remains open.")
                return

            removing_carbon = "carbon" in old and "carbon" not in target
            adding_carbon = "carbon" not in old and "carbon" in target
            classic_changed = (set(old) & CLASSIC_GAMES) != (set(target) & CLASSIC_GAMES)

            if removing_carbon:
                self._stop_service("carbon")

            self.context = prepare_runtime_context(target)

            if "classic" not in self.processes or self.processes["classic"].poll() is not None:
                self._start_classic()
            elif classic_changed:
                if "carbon" in target and "carbon" in self.processes:
                    self.printer.write(
                        "Warning: changing U2/MW restarts Classic and temporarily interrupts EA Messenger."
                    )
                self._stop_service("classic")
                self._start_classic()

            if adding_carbon or ("carbon" in target and "carbon" not in self.processes):
                self._start_carbon()

            self.games = target
            self._persist()
            self.printer.write("Active: " + " + ".join(game.upper() for game in target))

    def restart(self, targets: Sequence[str]) -> None:
        requested = normalize_games(targets, allow_empty=True)
        if not requested:
            raise LauncherError("specify u2, mw, carbon, or all")
        if not self.games:
            raise LauncherError("no game is running; use start")
        inactive = [game for game in requested if game not in self.games]
        if inactive:
            raise LauncherError("not running: " + ", ".join(inactive) + "; use start")
        restart_classic = bool(set(requested) & CLASSIC_GAMES)
        restart_carbon = "carbon" in requested
        self.context = prepare_runtime_context(self.games)
        if restart_carbon:
            self._stop_service("carbon")
        if restart_classic:
            if "carbon" in self.games and "carbon" in self.processes:
                self.printer.write(
                    "Warning: restarting U2/MW restarts the Classic process and the shared EA Messenger."
                )
            self._stop_service("classic")
            self._start_classic()
        if restart_carbon and "carbon" in self.games:
            self._start_carbon()
        self._persist()
        self.printer.write("Restart completed: " + ", ".join(requested))

    def status_lines(self) -> list[str]:
        games = ", ".join(self.games) if self.games else "no games"
        lines = [f"Selected games: {games}"]
        for name in ("classic", "carbon"):
            process = self.processes.get(name)
            if process is not None and process.poll() is None:
                lines.append(f"{SERVICES[name]['label']}: running (PID {process.pid})")
            else:
                lines.append(f"{SERVICES[name]['label']}: stopped")
        return lines

    def failures(self) -> list[tuple[str, int]]:
        failures: list[tuple[str, int]] = []
        for name, process in list(self.processes.items()):
            code = process.poll()
            if code is not None:
                failures.append((name, int(code)))
                self.processes.pop(name, None)
        if failures:
            self._persist()
        return failures

    def shutdown(self) -> None:
        self.stop_event.set()
        self._stop_service("carbon")
        self._stop_service("classic")
        self.games = ()
        self.context = None
        self._persist()
        for handle in self.handles:
            try:
                handle.close()
            except OSError:
                pass
        self.handles.clear()


def update_client_hosts(host: str) -> int:
    pattern = re.compile(r"^(\s*(?:[A-Za-z0-9_]*host)\s*=\s*).*$", re.IGNORECASE)
    updated = 0
    for config_path in sorted(CLIENT_ROOT.glob("*/*.ini")):
        try:
            lines = config_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise LauncherError(f"cannot read client configuration {config_path}: {exc}") from exc
        changed = False
        rendered: list[str] = []
        for line in lines:
            match = pattern.match(line)
            if match:
                replacement = f"{match.group(1)}{host}"
                changed = changed or replacement != line
                rendered.append(replacement)
            else:
                rendered.append(line)
        if changed:
            temporary = config_path.with_suffix(config_path.suffix + ".tmp")
            temporary.write_text("\n".join(rendered) + "\n", encoding="utf-8")
            os.replace(temporary, config_path)
            updated += 1
    return updated


def configure_host(host: str) -> int:
    value = host.strip()
    if not value or any(char.isspace() for char in value) or ":" in value:
        fail("invalid hostname; do not include a port")
    update_config_value(CONFIG_FILE, CONFIG_SECTIONS["global"], "PUBLIC_HOST", value)
    client_count = update_client_hosts(value)
    prepare_runtime_context(VALID_GAMES)
    print(f"Public host set in {CONFIG_FILE.relative_to(ROOT)}: {value}")
    print(f"Client configurations updated: {client_count}")
    return 0


def account_command(arguments: list[str]) -> int:
    context = prepare_runtime_context(VALID_GAMES)
    command = [
        sys.executable,
        "-m",
        "classic.admin.auth",
        "--config",
        str(CONFIG_FILE),
        *arguments,
    ]
    return subprocess.call(
        command,
        cwd=str(CLASSIC_ROOT),
        env=child_environment(context),
    )


def carbon_admin_command(arguments: list[str]) -> int:
    context = prepare_runtime_context(VALID_GAMES)
    command = [
        sys.executable,
        "-m",
        "carbon.admin.accounts",
        "--config",
        str(CONFIG_FILE),
        *arguments,
    ]
    return subprocess.call(
        command,
        cwd=str(CARBON_ROOT),
        env=child_environment(context),
    )


def dlc_admin_command(arguments: list[str]) -> int:
    context = prepare_runtime_context(VALID_GAMES)
    command = [
        sys.executable,
        "-m",
        "carbon.admin.dlc",
        "--config",
        str(CONFIG_FILE),
        *arguments,
    ]
    return subprocess.call(
        command,
        cwd=str(CARBON_ROOT),
        env=child_environment(context),
    )


def stats_command(arguments: list[str]) -> int:
    context = prepare_runtime_context(VALID_GAMES)
    command = [
        sys.executable,
        "-m",
        "classic.admin.stats",
        "--config",
        str(CONFIG_FILE),
        *arguments,
    ]
    return subprocess.call(
        command,
        cwd=str(CLASSIC_ROOT),
        env=child_environment(context),
    )


def interactive_account() -> int:
    account = input("Account name: ").strip()
    if not account:
        fail("the account name cannot be empty")
    persona = input(f"Persona [{account}]: ").strip() or account
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if password != confirmation:
        fail("passwords do not match")
    if not password:
        fail("the password cannot be empty")
    return account_command(["create", account, "--persona", persona, "--password", password])


def open_account_db() -> sqlite3.Connection | None:
    database = configured_account_db()
    if not database.is_file():
        return None
    connection = sqlite3.connect(database, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def session_lines() -> list[str]:
    connection = open_account_db()
    if connection is None:
        return ["The account database does not exist yet."]
    try:
        now = time.time()
        connection.execute("DELETE FROM active_sessions WHERE expires_at <= ?", (now,))
        connection.commit()
        rows = connection.execute(
            """
            SELECT a.account_name, p.display_name, s.game, s.server_id,
                   s.connected_at, s.heartbeat_at, s.expires_at
            FROM active_sessions AS s
            JOIN accounts AS a ON a.account_id=s.account_id
            JOIN personas AS p ON p.persona_id=s.persona_id
            ORDER BY s.connected_at
            """
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return ["No active sessions."]
    result = ["ACCOUNT          PERSONA          GAME          AGE   LEASE   SERVER"]
    now = time.time()
    for row in rows:
        age = max(0, int(now - float(row["connected_at"])))
        lease = max(0, int(float(row["expires_at"]) - now))
        result.append(
            f"{str(row['account_name'])[:16]:16} "
            f"{str(row['display_name'])[:16]:16} "
            f"{str(row['game'])[:12]:12} "
            f"{age:4}s  {lease:4}s  {row['server_id']}"
        )
    return result


def release_session(identifier: str) -> bool:
    connection = open_account_db()
    if connection is None:
        return False
    key = identifier.strip().casefold()
    try:
        cursor = connection.execute(
            """
            DELETE FROM active_sessions
            WHERE account_id IN (
                SELECT account_id FROM accounts WHERE account_name_key=?
                UNION
                SELECT account_id FROM personas WHERE display_name_key=?
            )
            """,
            (key, key),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def tail_lines(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-max(1, count):]]


def filtered_log_lines(source: str, count: int) -> list[str]:
    key = source.casefold()
    if key == "carbon":
        return tail_lines(Path(SERVICES["carbon"]["log"]), count)
    if key in {"classic", "all"}:
        classic = tail_lines(Path(SERVICES["classic"]["log"]), count)
        if key == "classic":
            return classic
        carbon = tail_lines(Path(SERVICES["carbon"]["log"]), count)
        return classic + carbon
    label_map = {
        "u2": "U2",
        "mw": "MW",
        "messenger": "Messenger",
        "race": "Race",
        "shared": "Shared",
    }
    wanted = label_map.get(key)
    if wanted is None:
        raise LauncherError("log source must be all, classic, u2, mw, carbon, messenger, race, or shared")
    path = Path(SERVICES["classic"]["log"])
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        matches = [line.rstrip("\n") for line in handle if output_label("classic", line) == wanted]
    return matches[-max(1, count):]


def configuration_lines(scope: str = "all") -> list[str]:
    aliases = {
        "server": "global",
        "global": "global",
        "underground2": "u2",
        "underground_2": "u2",
        "u2": "u2",
        "mostwanted": "mw",
        "most_wanted": "mw",
        "mw": "mw",
        "carbon": "carbon",
        "all": "all",
    }
    requested = scope.strip().casefold().replace("-", "_") or "all"
    normalized = aliases.get(requested)
    if normalized is None:
        raise LauncherError(
            "config show: use server, underground2, most_wanted, carbon, or all"
        )
    names = list(CONFIG_SECTIONS) if normalized == "all" else [normalized]
    lines: list[str] = []
    for name in names:
        values = load_named_config(name)
        section = CONFIG_SECTIONS[name]
        lines.append(f"[{section}] {CONFIG_FILE.relative_to(ROOT)}")
        for key, value in values.items():
            shown = value
            if key == "IPC_SECRET" and value.casefold() in {"", "auto", "generate"}:
                shown = "<AUTO/generated>"
            elif key == "IPC_SECRET":
                shown = "<explicit, hidden>"
            lines.append(f"  {key}={shown}")
    return lines


def configuration_paths() -> list[str]:
    return [
        "Editable configuration:",
        f"  {CONFIG_FILE.relative_to(ROOT)}",
        "Persistent internal state:",
        f"  {STATE_PATH.relative_to(ROOT)}",
        "Process state (exists only while the server is running):",
        f"  {PID_PATH.relative_to(ROOT)}",
    ]


def handle_config_console(arguments: list[str], manager: RuntimeManager, printer: ConsolePrinter) -> None:
    action = arguments[0].casefold() if arguments else "paths"
    if action == "paths":
        for line in configuration_paths():
            printer.write(line)
        return
    if action == "show":
        scope = arguments[1] if len(arguments) >= 2 else "all"
        for line in configuration_lines(scope):
            printer.write(line)
        return
    if action == "reload":
        selected = manager.games or normalize_games(load_named_config("global")["DEFAULT_GAMES"])
        prepare_runtime_context(selected)
        printer.write("Configuration validated. Use restart for active services.")
        return
    raise LauncherError("config paths | config show [server|u2|mw|carbon|all] | config reload")


HELP_TEXT = """
Console commands:
  help                              show this list
  status                            show active processes and games
  start <u2|mw|carbon|all> [...]    start or add games
  stop <u2|mw|carbon|all> [...]     stop selected games
  restart <u2|mw|carbon|all> [...]  restart the required services

  sessions / players                show active SQLite sessions
  session release <account>         manually release an account lease

  account list                      list accounts
  account create <name> [persona]   create an account; password is prompted securely
  account password <name>           change the password
  account enable|disable <name>      enable/disable the account
  account ban|unban <name>           apply/remove a global ban
  account kick <name> / kick <name>  disconnect the player without banning

  dlc list [--category cars]         list free Carbon DLC packages
  dlc show <account>                 show an account selection
  dlc unlock|lock <account> <dlc>    add/remove DLC per account
  dlc all|none|reset <account>       all, none, or the default selection

  stats [--game u2|mw] <command>     administer persistent statistics

  virus <account> <1|2|3> [on|off]  admin Carbon: viral vinyl
  moderator <account> <on|off>      admin Carbon: EA Moderator role
  beat-moderator <account> [on|off] admin Carbon: achievement/stat

  logs on|off                       show live logs in the terminal
  logs show <source> [lines]        show the latest log lines
  colors [auto|on|off]              set ANSI colors and save to server.toml
  config paths                      show configuration and state files
  config show [scope]               show the effective configuration
  config reload                     validate config/server.toml
  clear                             clear the terminal
  check                             validate Python, SQLite, and configuration
  quit / exit                       stop everything and close the console
""".strip()


def parse_game_arguments(arguments: Sequence[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    if not arguments:
        raise LauncherError("specify u2, mw, carbon, or all")
    return normalize_games(arguments, allow_empty=allow_empty)


def handle_account_console(arguments: list[str], printer: ConsolePrinter) -> None:
    if not arguments:
        raise LauncherError("use account list/create/password/enable/disable/ban/unban/kick")
    action = arguments[0].casefold()
    with printer.suspend_logs():
        if action == "list":
            account_command(["list"])
            return
        if action == "create":
            if len(arguments) < 2:
                raise LauncherError("account create <name> [persona]")
            command = ["create", arguments[1]]
            if len(arguments) >= 3:
                command += ["--persona", arguments[2]]
            account_command(command)
            return
        if action == "password":
            if len(arguments) != 2:
                raise LauncherError("account password <name>")
            account_command(["password", arguments[1]])
            return
        if action in {"enable", "disable"}:
            if len(arguments) != 2:
                raise LauncherError(f"account {action} <name>")
            account_command(["enabled", arguments[1], "on" if action == "enable" else "off"])
            return
        if action in {"ban", "unban", "kick"}:
            if len(arguments) != 2:
                raise LauncherError(f"account {action} <name>")
            account_command([action, arguments[1]])
            return
    raise LauncherError(f"unknown account action: {action}")


def handle_carbon_admin(command: str, arguments: list[str], printer: ConsolePrinter) -> None:
    with printer.suspend_logs():
        if command == "virus":
            if len(arguments) not in {2, 3}:
                raise LauncherError("virus <account> <1|2|3> [on|off]")
            aliases = {
                "1": "Virus1",
                "virus1": "Virus1",
                "knockout": "Virus1",
                "2": "Virus2",
                "virus2": "Virus2",
                "canyon": "Virus2",
                "3": "Virus3",
                "virus3": "Virus3",
                "pursuit": "Virus3",
            }
            virus = aliases.get(arguments[1].casefold())
            if virus is None:
                raise LauncherError("virus must be 1, 2, 3, knockout, canyon, or pursuit")
            state = arguments[2].casefold() if len(arguments) == 3 else "on"
            if state not in {"on", "off"}:
                raise LauncherError("state must be on or off")
            carbon_admin_command(["virus", arguments[0], virus, state])
            return
        if command == "moderator":
            if len(arguments) != 2 or arguments[1].casefold() not in {"on", "off"}:
                raise LauncherError("moderator <account> <on|off>")
            carbon_admin_command(["moderator", arguments[0], arguments[1].casefold()])
            return
        if command == "beat-moderator":
            if len(arguments) not in {1, 2}:
                raise LauncherError("beat-moderator <account> [on|off]")
            state = arguments[1].casefold() if len(arguments) == 2 else "on"
            if state not in {"on", "off"}:
                raise LauncherError("state must be on or off")
            carbon_admin_command(["beat-moderator", arguments[0], state])
            return


def interactive_console(manager: RuntimeManager, printer: ConsolePrinter) -> int:
    printer.write("Interactive console active. Type 'help'.")
    while True:
        for name, code in manager.failures():
            printer.write(f"{SERVICES[name]['label']} stopped with exit code {code}.", error=True)
        try:
            raw = printer.input()
        except EOFError:
            printer.write("")
            raw = "quit"
        except KeyboardInterrupt:
            printer.write("\nUse 'quit' to stop, or continue with a command.")
            continue
        try:
            parts = shlex.split(raw, posix=os.name != "nt")
        except ValueError as exc:
            printer.write(f"Invalid command: {exc}", error=True)
            continue
        if not parts:
            continue
        command = parts[0].casefold()
        arguments = parts[1:]
        try:
            if command in {"help", "?"}:
                printer.write(HELP_TEXT)
            elif command == "status":
                for line in manager.status_lines():
                    printer.write(line)
                for line in session_lines():
                    if not line.startswith("ACCOUNT") and "session" in line.casefold():
                        printer.write(line)
            elif command == "start":
                requested = parse_game_arguments(arguments)
                manager.apply_games(tuple(set(manager.games) | set(requested)))
            elif command == "stop":
                requested = parse_game_arguments(arguments)
                if set(requested) == set(VALID_GAMES):
                    manager.apply_games(())
                else:
                    manager.apply_games(tuple(game for game in manager.games if game not in requested))
            elif command == "restart":
                manager.restart(parse_game_arguments(arguments))
            elif command in {"sessions", "players"}:
                for line in session_lines():
                    printer.write(line)
            elif command == "session":
                if len(arguments) != 2 or arguments[0].casefold() != "release":
                    raise LauncherError("session release <account>")
                released = release_session(arguments[1])
                printer.write("Session released." if released else "There is no active session for that account.")
            elif command == "account":
                handle_account_console(arguments, printer)
            elif command == "kick":
                if len(arguments) != 1:
                    raise LauncherError("kick <account>")
                with printer.suspend_logs():
                    account_command(["kick", arguments[0]])
            elif command == "dlc":
                if not arguments:
                    raise LauncherError("dlc list|show|unlock|lock|all|none|reset ...")
                with printer.suspend_logs():
                    dlc_admin_command(arguments)
            elif command == "stats":
                with printer.suspend_logs():
                    stats_command(arguments)
            elif command in {"virus", "moderator", "beat-moderator"}:
                handle_carbon_admin(command, arguments, printer)
            elif command == "logs":
                if not arguments:
                    printer.write("logs: " + ("on" if printer.logs_enabled else "off"))
                elif arguments[0].casefold() in {"on", "off"}:
                    printer.logs_enabled = arguments[0].casefold() == "on"
                    printer.write("Live logs: " + ("enabled" if printer.logs_enabled else "disabled"))
                elif arguments[0].casefold() == "show":
                    source = arguments[1] if len(arguments) >= 2 else "all"
                    try:
                        count = int(arguments[2]) if len(arguments) >= 3 else 30
                    except ValueError as exc:
                        raise LauncherError("the number of lines must be an integer") from exc
                    for line in filtered_log_lines(source, max(1, min(count, 1000))):
                        printer.write(line)
                else:
                    raise LauncherError("logs on|off or logs show <source> [lines]")
            elif command == "colors":
                if not arguments:
                    printer.write(f"Colors: {printer.color_mode} (active={int(printer.colors_enabled)})")
                elif len(arguments) == 1:
                    printer.set_color_mode(arguments[0])
                    printer.write(f"Colors: {printer.color_mode}")
                else:
                    raise LauncherError("colors auto|on|off")
            elif command == "config":
                handle_config_console(arguments, manager, printer)
            elif command == "clear":
                os.system("cls" if os.name == "nt" else "clear")
            elif command == "check":
                run_check()
            elif command in {"quit", "exit", "shutdown"}:
                printer.write("Graceful shutdown...")
                return 0
            else:
                raise LauncherError(f"unknown command: {command}; type help")
        except LauncherError as exc:
            printer.write(f"Error: {exc}", error=True)
        except KeyboardInterrupt:
            printer.write("Operation cancelled.", error=True)


def start_all(*, daemon: bool, games: tuple[str, ...]) -> int:
    existing = running_services()
    if existing:
        names = ", ".join(f"{name}={pid}" for name, pid in existing.items())
        fail(f"the servers are already running ({names}); use 'python nfs_online.py stop'")
    PID_PATH.unlink(missing_ok=True)
    printer = ConsolePrinter()
    manager = RuntimeManager(printer, daemon=daemon)
    completed_start = False
    try:
        manager.start_initial(games)
        completed_start = True
        if daemon:
            printer.write("Running in the background. Stop with: python nfs_online.py stop")
            return 0
        return interactive_console(manager, printer)
    except LauncherError as exc:
        printer.write(f"Error: {exc}", error=True)
        return 2
    finally:
        if daemon and completed_start:
            # Child processes own duplicated log descriptors and remain alive.
            for handle in manager.handles:
                try:
                    handle.close()
                except OSError:
                    pass
            manager.handles.clear()
        else:
            manager.shutdown()


def show_status() -> int:
    services = running_services()
    if not services:
        print("Stopped")
        return 1
    state = read_pid_state()
    raw_games = state.get("games", [])
    try:
        games = normalize_games(raw_games, allow_empty=True)
    except LauncherError:
        games = ()
    print("Games: " + (", ".join(games) if games else "unknown"))
    for name in ("classic", "carbon"):
        pid = services.get(name)
        print(f"{SERVICES[name]['label']}: " + (f"running (PID {pid})" if pid else "stopped"))
    expected = 2 if "carbon" in games else 1
    return 0 if len(services) == expected else 1


def run_check() -> int:
    configuration = load_configuration()
    default_games = normalize_games(configuration["global"]["DEFAULT_GAMES"])
    context = prepare_runtime_context(default_games)
    print(f"Package: {PACKAGE_NAME}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"SQLite: {sqlite3.sqlite_version}")
    print(f"Public host: {context.public_host}")
    print(f"Public IPv4: {context.public_ipv4}")
    print(f"Local IPv4: {context.local_ipv4}")
    print("Default games: " + ", ".join(default_games))
    for line in configuration_paths():
        print(line)
    missing_builds = missing_client_binaries()
    if missing_builds:
        print(
            "Client builds: source checkout (build with "
            "source/client-zig/tools/build_linux.sh or download a release)"
        )
    else:
        print("Client builds: present")
    print("Structure and configuration: OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NFS Online Server — U2, Most Wanted, and Carbon launcher"
    )
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="start the console and selected games")
    start.add_argument("--daemon", action="store_true", help="run in the background without a console")
    start.add_argument(
        "--games",
        default=None,
        help="u2,mw,carbon; defaults to DEFAULT_GAMES from config/server.toml",
    )
    sub.add_parser("stop", help="stop all services")
    sub.add_parser("status", help="show service status")
    sub.add_parser("check", help="validate the package without starting services")
    configure = sub.add_parser("configure", help="change the public hostname and client configurations")
    configure.add_argument("--public-host", required=True)
    config = sub.add_parser("config", help="show or validate configuration")
    config.add_argument("args", nargs=argparse.REMAINDER)
    account = sub.add_parser("account", help="administer shared accounts")
    account.add_argument("args", nargs=argparse.REMAINDER)
    kick = sub.add_parser("kick", help="disconnect an account immediately without banning")
    kick.add_argument("account")
    dlc = sub.add_parser("dlc", help="administer per-account Carbon DLC")
    dlc.add_argument("args", nargs=argparse.REMAINDER)
    stats = sub.add_parser("stats", help="administer U2/MW statistics")
    stats.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("create-account", help="create the first account interactively")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    # Admin command groups are pass-through commands. argparse would
    # otherwise reject child options such as ``stats --game mw ...`` before the
    # dedicated admin parser can see them.
    if raw_arguments and raw_arguments[0] in {"account", "stats", "dlc"}:
        args = argparse.Namespace(command=raw_arguments[0], args=raw_arguments[1:])
    else:
        args = parser.parse_args(raw_arguments)
    command = args.command or "start"
    try:
        if command == "start":
            requested = getattr(args, "games", None)
            if requested is None:
                requested = load_named_config("global")["DEFAULT_GAMES"]
            return start_all(
                daemon=bool(getattr(args, "daemon", False)),
                games=normalize_games(requested),
            )
        if command == "stop":
            return stop_all()
        if command == "status":
            return show_status()
        if command == "check":
            return run_check()
        if command == "configure":
            return configure_host(args.public_host)
        if command == "config":
            action = args.args[0].casefold() if args.args else "paths"
            if action == "paths":
                for line in configuration_paths():
                    print(line)
                return 0
            if action == "show":
                scope = args.args[1] if len(args.args) >= 2 else "all"
                for line in configuration_lines(scope):
                    print(line)
                return 0
            if action in {"check", "reload"}:
                return run_check()
            parser.error("config paths | config show [scope] | config check")
        if command == "account":
            if not args.args:
                parser.error("example: account list or account create Driver --persona Driver")
            return account_command(args.args)
        if command == "kick":
            return account_command(["kick", args.account])
        if command == "dlc":
            if not args.args:
                parser.error("example: dlc show Driver or dlc all Driver")
            return dlc_admin_command(args.args)
        if command == "stats":
            if not args.args:
                parser.error("example: stats --game mw show Driver")
            return stats_command(args.args)
        if command == "create-account":
            return interactive_account()
    except LauncherError as exc:
        fail(str(exc))
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
