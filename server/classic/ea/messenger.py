"""Shared EA Messenger transport, profile catalogue and protocol router.

Underground 2 and Most Wanted use the same TCP port and twelve-byte envelope.
This module owns the common wire transport and selects the correct game adapter
from the first AUTH frame plus the active lobby session.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import socket
import struct
from threading import Event, Lock
import time
from typing import Callable, Protocol, TypeVar

from common.enforcement import (
    AccountPolicyEvent,
    LiveAccountConnection,
    LiveAccountConnectionRegistry,
    PolicyCloseGate,
)

from classic.core.catalog import GameId


log = logging.getLogger(__name__)
HEADER_SIZE = 12
WireSender = Callable[[bytes], bool]
ContextT = TypeVar("ContextT")


@dataclass(frozen=True)
class EAMessengerProfile:
    game: GameId
    title: str
    resource: str
    default_show: str
    default_status: str
    default_product: str = ""
    default_attr: str = ""


MESSENGER_PROFILES: dict[GameId, EAMessengerProfile] = {
    GameId.MOST_WANTED: EAMessengerProfile(
        game=GameId.MOST_WANTED,
        title="Need for Speed Most Wanted [PC]",
        resource="eagames/NFS-2006",
        default_show="PASS",
        default_status="EX%3d0%0aP%3dnfs6%0a",
        default_product="is playing Most Wanted",
        default_attr="D",
    ),
    GameId.UNDERGROUND2: EAMessengerProfile(
        game=GameId.UNDERGROUND2,
        title="Need for Speed Underground 2 [PC]",
        resource="eagames/NFS-2005",
        default_show="PASS",
        default_status="EX%3d0%0aP%3dnfs5%0a",
        default_product="is playing Underground 2",
        default_attr="D",
    ),
}


def messenger_profile(game: GameId) -> EAMessengerProfile:
    return MESSENGER_PROFILES[game]


def decode_messenger_fields(payload: bytes) -> dict[str, str]:
    text = bytes(payload).decode("latin-1", errors="replace").rstrip("\x00")
    result: dict[str, str] = {}
    for raw_line in text.replace("\r", "\n").replace("\t", "\n").split("\n"):
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip().upper()
        if name:
            result[name] = value.strip()
    return result


@dataclass(frozen=True)
class EAMessengerFrame:
    command: str
    word: int
    payload: bytes

    @property
    def transaction(self) -> int:
        """Carbon name for the shared four-byte header word."""
        return int(self.word) & 0xFFFFFFFF

    @property
    def fields(self) -> dict[str, str]:
        return decode_messenger_fields(self.payload)

    @classmethod
    def from_fields(
        cls,
        command: str,
        fields: dict[str, object],
        *,
        transaction: int = 0x80000000,
        trailing_newline: bool = False,
    ) -> "EAMessengerFrame":
        lines = [f"{key}={value}" for key, value in fields.items()]
        terminator = "\n\x00" if trailing_newline else "\x00"
        payload = (("\n".join(lines) + terminator).encode("latin-1") if lines else b"\x00")
        return cls(str(command), int(transaction) & 0xFFFFFFFF, payload)

    def encode(self) -> bytes:
        raw_command = self.command.encode("latin-1", errors="strict")
        if len(raw_command) != 4 or any(value < 0x20 or value > 0x7E for value in raw_command):
            raise ValueError(f"invalid EA Messenger command: {self.command!r}")
        payload = bytes(self.payload)
        return (
            raw_command
            + struct.pack(">I", int(self.word) & 0xFFFFFFFF)
            + struct.pack(">I", HEADER_SIZE + len(payload))
            + payload
        )


class EAMessengerStreamDecoder:
    def __init__(self, *, max_frame_size: int = 65_535) -> None:
        if int(max_frame_size) < HEADER_SIZE:
            raise ValueError("max_frame_size must be at least 12")
        self.max_frame_size = int(max_frame_size)
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EAMessengerFrame]:
        self._buffer.extend(data)
        frames: list[EAMessengerFrame] = []
        while len(self._buffer) >= HEADER_SIZE:
            header = bytes(self._buffer[:HEADER_SIZE])
            command_raw = header[:4]
            if any(value < 0x20 or value > 0x7E for value in command_raw):
                raise ValueError(f"invalid EA Messenger command bytes: {command_raw.hex()}")
            word, total_length = struct.unpack(">II", header[4:12])
            if total_length < HEADER_SIZE or total_length > self.max_frame_size:
                raise ValueError(f"invalid EA Messenger frame length: {total_length}")
            if len(self._buffer) < total_length:
                break
            wire = bytes(self._buffer[:total_length])
            del self._buffer[:total_length]
            frames.append(
                EAMessengerFrame(
                    command_raw.decode("latin-1"),
                    word,
                    wire[HEADER_SIZE:],
                )
            )
        return frames


class EAMessengerAdapter(Protocol[ContextT]):
    name: str

    def matches(self, first_frame: EAMessengerFrame, peer: tuple[str, int]) -> bool: ...

    def open(
        self,
        peer: tuple[str, int],
        sender: WireSender,
        *,
        now: float,
    ) -> ContextT: ...

    def dispatch(
        self,
        frame: EAMessengerFrame,
        context: ContextT,
        *,
        now: float,
    ) -> list[bytes]: ...

    def poll(self, context: ContextT, *, now: float) -> list[bytes]: ...

    def close_requested(self, context: ContextT) -> bool: ...

    def connection_id(self, context: ContextT) -> str: ...

    def account_name(self, context: ContextT) -> str: ...

    def policy_frames(
        self, context: ContextT, event: AccountPolicyEvent
    ) -> tuple[bytes, ...]: ...

    def request_close(self, context: ContextT, reason: str) -> None: ...

    def authenticated(self, context: ContextT) -> bool: ...

    def describe(self, context: ContextT) -> str: ...

    def close(self, context: ContextT) -> None: ...


class EAMessengerHub:
    """One port that sniffs the first AUTH frame and selects a game adapter."""

    def __init__(
        self,
        adapters: list[EAMessengerAdapter[object]],
        *,
        max_frame_size: int = 65_535,
        connection_timeout: float = 60.0,
        poll_interval: float = 0.1,
        live_connections: LiveAccountConnectionRegistry | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("at least one EA Messenger adapter is required")
        self.adapters = tuple(adapters)
        self.max_frame_size = int(max_frame_size)
        self.connection_timeout = float(connection_timeout)
        self.poll_interval = max(0.01, min(float(poll_interval), self.connection_timeout))
        self.live_connections = live_connections

    def _select(
        self,
        frame: EAMessengerFrame,
        peer: tuple[str, int],
    ) -> EAMessengerAdapter[object] | None:
        matched = [adapter for adapter in self.adapters if adapter.matches(frame, peer)]
        if len(matched) > 1:
            raise ValueError(
                "ambiguous EA Messenger dialect: " + ", ".join(adapter.name for adapter in matched)
            )
        return matched[0] if matched else None

    def handle_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        conn.settimeout(self.poll_interval)
        decoder = EAMessengerStreamDecoder(max_frame_size=self.max_frame_size)
        send_lock = Lock()
        dispatch_lock = Lock()
        adapter: EAMessengerAdapter[object] | None = None
        context: object | None = None
        connected_at = time.monotonic()
        last_activity = connected_at
        reason = "server-stop"
        forced_close_reason = ""
        registered_connection_id = ""
        policy_close = PolicyCloseGate()

        def send_wire(wire: bytes) -> bool:
            nonlocal reason
            payload = bytes(wire)
            if not payload:
                return True
            try:
                with send_lock:
                    if policy_close.active:
                        return False
                    if (
                        adapter is not None
                        and context is not None
                        and adapter.close_requested(context)
                    ):
                        return False
                    conn.sendall(payload)
                return True
            except OSError as exc:
                reason = f"send-error:{exc.errno or 'unknown'}"
                return False

        log.info("EA Messenger client connected: peer=%s:%d", addr[0], addr[1])
        try:
            while not stop_event.is_set():
                if policy_close.expired():
                    reason = forced_close_reason or policy_close.reason or "policy-close"
                    break
                if (
                    adapter is not None
                    and context is not None
                    and adapter.close_requested(context)
                ):
                    reason = forced_close_reason or "protocol-close"
                    break
                try:
                    data = conn.recv(8192)
                except socket.timeout:
                    if policy_close.active:
                        if policy_close.expired():
                            reason = (
                                forced_close_reason
                                or policy_close.reason
                                or "policy-close"
                            )
                            break
                        continue
                    now = time.monotonic()
                    if adapter is None:
                        if now - last_activity >= self.connection_timeout:
                            reason = "unauthenticated-timeout"
                            break
                    elif context is not None:
                        with dispatch_lock:
                            if adapter.close_requested(context):
                                pending_wires: tuple[bytes, ...] = ()
                            else:
                                pending_wires = tuple(adapter.poll(context, now=now))
                        for wire in pending_wires:
                            if not send_wire(wire):
                                break
                    continue
                except OSError as exc:
                    if policy_close.active:
                        reason = (
                            forced_close_reason
                            or policy_close.reason
                            or "policy-close"
                        )
                    elif (
                        adapter is not None
                        and context is not None
                        and adapter.close_requested(context)
                    ):
                        reason = forced_close_reason or "protocol-close"
                    else:
                        reason = f"recv-error:{exc.errno or 'unknown'}"
                    break
                if not data:
                    reason = (
                        forced_close_reason or policy_close.reason or "policy-close"
                        if policy_close.active
                        else "peer-eof"
                    )
                    break
                if policy_close.active:
                    # The account has already been removed from shared state.
                    # Keep the transport read-only until the bounded drain
                    # window expires so no command can revive the session.
                    continue
                last_activity = time.monotonic()
                try:
                    frames = decoder.feed(data)
                except ValueError as exc:
                    log.warning(
                        "invalid EA Messenger frame from %s:%d: %s",
                        addr[0],
                        addr[1],
                        exc,
                    )
                    reason = "invalid-frame"
                    break
                for frame in frames:
                    now = time.monotonic()
                    if adapter is None:
                        if frame.command.upper() == "DISC":
                            # Retail U2/MW can open a short-lived socket whose
                            # first and only request is DISC.  The older
                            # compatible listener answers that pre-session
                            # teardown with the generic Messenger AUTH banner
                            # before closing; dialect selection cannot use a
                            # fieldless DISC when U2 and MW share this port.
                            acknowledged = send_wire(
                                EAMessengerFrame.from_fields(
                                    "AUTH",
                                    {"TITL": "EA MESSENGER"},
                                    transaction=0,
                                ).encode()
                            )
                            reason = (
                                "client-disconnect"
                                if acknowledged
                                else reason
                            )
                            break
                        try:
                            adapter = self._select(frame, addr)
                        except ValueError as exc:
                            log.warning(
                                "ambiguous EA Messenger dialect from %s:%d: %s",
                                addr[0],
                                addr[1],
                                exc,
                            )
                            reason = "ambiguous-dialect"
                            break
                        if adapter is None:
                            log.warning(
                                "unknown EA Messenger dialect from %s:%d command=%s fields=%s",
                                addr[0],
                                addr[1],
                                frame.command,
                                ",".join(sorted(frame.fields)) or "<none>",
                            )
                            reason = "unknown-dialect"
                            break
                        context = adapter.open(addr, send_wire, now=now)
                        if self.live_connections is not None:
                            registered_connection_id = adapter.connection_id(context)

                            def account_name(
                                selected_adapter=adapter, selected_context=context
                            ) -> str:
                                return selected_adapter.account_name(selected_context)

                            def begin_policy_close(
                                event: AccountPolicyEvent,
                                selected_adapter=adapter,
                                selected_context=context,
                            ) -> bool:
                                nonlocal forced_close_reason, reason
                                policy_frames = tuple(
                                    selected_adapter.policy_frames(
                                        selected_context, event
                                    )
                                )
                                with dispatch_lock:
                                    with send_lock:
                                        if (
                                            policy_close.active
                                            or selected_adapter.close_requested(
                                                selected_context
                                            )
                                        ):
                                            return False
                                        forced_close_reason = event.disconnect_reason
                                        sent = 0
                                        try:
                                            for policy_frame in policy_frames:
                                                conn.sendall(bytes(policy_frame))
                                                sent += 1
                                        except OSError as exc:
                                            reason = (
                                                f"send-error:{exc.errno or 'unknown'}"
                                            )
                                            policy_close.force(forced_close_reason)
                                            return False
                                        policy_close.request(forced_close_reason)
                                return sent > 0

                            def request_policy_close(
                                close_reason: str,
                            ) -> None:
                                nonlocal forced_close_reason
                                forced_close_reason = forced_close_reason or str(
                                    close_reason or "account-policy"
                                )
                                with dispatch_lock:
                                    with send_lock:
                                        policy_close.request(forced_close_reason)

                            self.live_connections.register(
                                LiveAccountConnection(
                                    connection_id=registered_connection_id,
                                    protocol=f"ea-messenger:{adapter.name}",
                                    account_name=account_name,
                                    begin_close=begin_policy_close,
                                    request_close=request_policy_close,
                                )
                            )
                        log.info(
                            "EA Messenger dialect selected: peer=%s:%d adapter=%s",
                            addr[0],
                            addr[1],
                            adapter.name,
                        )
                    assert adapter is not None and context is not None
                    try:
                        with dispatch_lock:
                            if policy_close.active or adapter.close_requested(context):
                                replies: tuple[bytes, ...] = ()
                            else:
                                replies = tuple(
                                    adapter.dispatch(frame, context, now=now)
                                )
                    except Exception:
                        log.exception(
                            "EA Messenger dispatch failed: peer=%s:%d adapter=%s command=%s",
                            addr[0],
                            addr[1],
                            adapter.name,
                            frame.command,
                        )
                        reason = "dispatch-error"
                        break
                    if policy_close.active:
                        break
                    for wire in replies:
                        if not send_wire(wire):
                            break
                    if policy_close.active:
                        break
                    if reason.startswith("send-error:"):
                        break
                    try:
                        with dispatch_lock:
                            if policy_close.active or adapter.close_requested(context):
                                trailing_wires: tuple[bytes, ...] = ()
                            else:
                                after_send = getattr(adapter, "after_send", None)
                                if callable(after_send):
                                    after_send(context)
                                trailing_wires = tuple(
                                    adapter.poll(context, now=time.monotonic())
                                )
                    except Exception:
                        log.exception(
                            "EA Messenger post-send processing failed: "
                            "peer=%s:%d adapter=%s command=%s",
                            addr[0],
                            addr[1],
                            adapter.name,
                            frame.command,
                        )
                        reason = "dispatch-error"
                        break
                    if policy_close.active:
                        break
                    for wire in trailing_wires:
                        if not send_wire(wire):
                            break
                    if reason.startswith("send-error:") or adapter.close_requested(context):
                        break
                if reason in {
                    "client-disconnect",
                    "unknown-dialect",
                    "ambiguous-dialect",
                    "dispatch-error",
                } or reason.startswith("send-error:"):
                    break
        finally:
            if self.live_connections is not None and registered_connection_id:
                self.live_connections.unregister(registered_connection_id)
            description = "<unselected>"
            authenticated = False
            adapter_name = "<none>"
            if adapter is not None and context is not None:
                adapter_name = adapter.name
                try:
                    description = adapter.describe(context)
                    authenticated = adapter.authenticated(context)
                finally:
                    adapter.close(context)
            log.info(
                "EA Messenger client disconnected: peer=%s:%d adapter=%s identity=%s "
                "authenticated=%d uptime=%.3f reason=%s",
                addr[0],
                addr[1],
                adapter_name,
                description,
                int(authenticated),
                time.monotonic() - connected_at,
                reason,
            )
