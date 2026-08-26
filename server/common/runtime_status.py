"""Small read-only runtime status publisher for external tooling.

Game services keep authoritative room and race state in memory. This helper
periodically writes a sanitized JSON snapshot into ``runtime/`` so a separate
website can read that state without importing or controlling the live process.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, Mapping


log = logging.getLogger(__name__)
SnapshotFactory = Callable[[], Mapping[str, Any]]


class RuntimeStatusPublisher:
    """Publish one process-local status snapshot as an atomic JSON file."""

    def __init__(
        self,
        path: str | Path,
        snapshot_factory: SnapshotFactory,
        *,
        name: str,
        interval: float = 2.0,
    ) -> None:
        if float(interval) <= 0:
            raise ValueError("runtime status interval must be positive")
        self.path = Path(path).expanduser().resolve()
        self.snapshot_factory = snapshot_factory
        self.name = str(name)
        self.interval = float(interval)
        self._stop = Event()
        self._thread: Thread | None = None
        self._lifecycle_lock = Lock()
        self._last_error = ""

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._publish_once()
            self._thread = Thread(
                target=self._run,
                name=f"{self.name}-runtime-status",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval * 2.0))
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
        self._remove_own_snapshot()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._publish_once()

    def _publish_once(self) -> None:
        try:
            payload = dict(self.snapshot_factory())
            payload.update(
                {
                    "schema": 1,
                    "pid": os.getpid(),
                    "updated_at": time.time(),
                }
            )
            self._write_atomic(payload)
        except Exception as exc:  # status output must never take the game down
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_error:
                log.warning("%s runtime status publish failed: %s", self.name, message)
                self._last_error = message
        else:
            if self._last_error:
                log.info("%s runtime status publisher recovered", self.name)
                self._last_error = ""

    def _write_atomic(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)

    def _remove_own_snapshot(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            owner_pid = int(value.get("pid", 0)) if isinstance(value, dict) else 0
        except (TypeError, ValueError):
            owner_pid = 0
        if owner_pid != os.getpid():
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("%s runtime status cleanup failed: %s", self.name, exc)
