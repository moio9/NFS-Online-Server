"""Shared U2/MW directory bootstrap and SESS/MASK challenge registry."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import md5
from threading import RLock
import time
from typing import Callable

from classic.core.config import Endpoint

from .frame import ClassicEAFrame


@dataclass(frozen=True)
class ClassicDirectoryChallenge:
    session: str
    mask: str
    issued_at: float


class ClassicDirectoryRegistry:
    """Transfer the directory challenge from bootstrap TCP to lobby TCP."""

    def __init__(
        self,
        *,
        session_base: int = 1_773_180_069,
        fixed_session: str = "",
        fixed_mask: str = "",
        ttl_seconds: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if float(ttl_seconds) <= 0:
            raise ValueError("directory challenge TTL must be positive")
        self.session_base = int(session_base)
        self.fixed_session = str(fixed_session or "").strip()
        self.fixed_mask = str(fixed_mask or "").strip()
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._counter = 0
        self._by_ip: dict[str, ClassicDirectoryChallenge] = {}

    @staticmethod
    def _ip_key(client_ip: object) -> str:
        return str(client_ip or "").strip()

    def issue(self, client_ip: str) -> ClassicDirectoryChallenge:
        now = self._clock()
        with self._lock:
            if self.fixed_session:
                session = self.fixed_session
            else:
                session = str(self.session_base + self._counter)
                self._counter += 1
            mask = self.fixed_mask or md5(
                f"{session}:lan-dir-mask".encode("ascii")
            ).hexdigest()
            challenge = ClassicDirectoryChallenge(session, mask, now)
            key = self._ip_key(client_ip)
            if key:
                self._by_ip[key] = challenge
            self._expire_locked(now)
            return challenge

    def recent(self, client_ip: str) -> ClassicDirectoryChallenge | None:
        now = self._clock()
        key = self._ip_key(client_ip)
        with self._lock:
            self._expire_locked(now)
            challenge = self._by_ip.get(key)
            if challenge is None:
                return None
            if now - challenge.issued_at > self.ttl_seconds:
                self._by_ip.pop(key, None)
                return None
            return challenge

    def _expire_locked(self, now: float) -> None:
        stale = [
            key
            for key, challenge in self._by_ip.items()
            if now - challenge.issued_at > self.ttl_seconds
        ]
        for key in stale:
            self._by_ip.pop(key, None)


@dataclass(frozen=True)
class ClassicBootstrapReply:
    frames: tuple[bytes, ...]
    reason: str = "ok"
    close_connection: bool = False


class ClassicBootstrapService:
    """Handle the plaintext directory stage shared by U2 and MW clients."""

    def __init__(
        self,
        registry: ClassicDirectoryRegistry,
        advertised_lobby: Endpoint,
    ) -> None:
        self.registry = registry
        self._advertised_lobby = advertised_lobby
        self._endpoint_resolver: Callable[[Endpoint, str], Endpoint] | None = None
        self._lock = RLock()

    @property
    def advertised_lobby(self) -> Endpoint:
        with self._lock:
            return self._advertised_lobby

    def set_advertised_lobby(self, endpoint: Endpoint) -> None:
        with self._lock:
            self._advertised_lobby = endpoint

    def set_endpoint_resolver(
        self, resolver: Callable[[Endpoint, str], Endpoint] | None
    ) -> None:
        with self._lock:
            self._endpoint_resolver = resolver

    def endpoint_for_client(self, client_ip: str) -> Endpoint:
        endpoint = self.advertised_lobby
        with self._lock:
            resolver = self._endpoint_resolver
        return resolver(endpoint, client_ip) if resolver is not None else endpoint

    def dispatch(self, frame: ClassicEAFrame, *, client_ip: str) -> ClassicBootstrapReply:
        command = frame.command.casefold()
        if command == "@tic":
            # The proven plaintext path does not require an application reply.
            return ClassicBootstrapReply((), "tick")
        if command not in {"@dir", "?dir"}:
            return ClassicBootstrapReply((), "unsupported_command")

        challenge = self.registry.issue(client_ip)
        endpoint = self.endpoint_for_client(client_ip)
        reply = ClassicEAFrame.from_fields(
            "@dir",
            (
                ("ADDR", endpoint.host),
                ("PORT", endpoint.port),
                ("SESS", challenge.session),
                ("MASK", challenge.mask),
            ),
            separator="\t",
            final_separator=False,
        ).encode()
        return ClassicBootstrapReply((reply,), "directory")
