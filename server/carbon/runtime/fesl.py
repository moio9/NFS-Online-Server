"""Policy-aware live Carbon FESL connection runtime."""

from __future__ import annotations

import logging
import socket
import time
from threading import Event, Lock
from uuid import uuid4

from common.enforcement import (
    AccountPolicyEvent,
    LiveAccountConnection,
    LiveAccountConnectionRegistry,
    PolicyCloseGate,
)

from carbon.core.config import ServerSettings
from carbon.fesl.frame import (
    FESLFrame,
    FESLFrameError,
    FESLStreamDecoder,
    packetize_frame,
)
from carbon.fesl.service import CarbonFESLService, FESLConnection


log = logging.getLogger("carbon.app")

_PLAY_NOW_STATUS_DELAY_SECONDS = 0.080


def _reply_delay_seconds(request: FESLFrame, reply: FESLFrame) -> float:
    """Return capture-backed pacing for asynchronous FESL replies."""

    if request.command != "pnow" or reply.command != "pnow":
        return 0.0
    if reply.fields.get("TXN", "").casefold() != "status":
        return 0.0
    return _PLAY_NOW_STATUS_DELAY_SECONDS


def handle_fesl_connection(
    conn: socket.socket,
    addr: tuple[str, int],
    stop_event: Event,
    *,
    settings: ServerSettings,
    service: CarbonFESLService,
    live_connections: LiveAccountConnectionRegistry,
) -> None:
    # Retail Carbon receives an asynchronous fsys/Ping every 30 seconds.
    # Merely leaving this socket open is insufficient: after roughly three
    # minutes without those frames the client tears down FESL and cascades
    # the close through Theater, Messenger and the race transport.
    poll_interval = min(settings.connection_timeout, 0.1)
    conn.settimeout(poll_interval)
    decoder = FESLStreamDecoder(max_frame_size=settings.max_frame_size)
    context = FESLConnection(
        connection_id=f"carbon:{addr[0]}:{addr[1]}:{uuid4().hex}"
    )
    send_lock = Lock()
    dispatch_lock = Lock()
    connected_at = time.monotonic()
    next_ping = connected_at + settings.fesl_heartbeat_interval
    ping_requests = 0
    disconnect_reason = "server-stop"
    policy_close = PolicyCloseGate()

    def encode_packets(frame: FESLFrame) -> tuple[bytes, ...]:
        return tuple(packet.encode() for packet in packetize_frame(frame))

    def send_encoded(packets: tuple[bytes, ...]) -> bool:
        nonlocal disconnect_reason
        try:
            with send_lock:
                if context.close_requested or policy_close.active:
                    return False
                for packet in packets:
                    conn.sendall(packet)
            return True
        except OSError as exc:
            disconnect_reason = f"send-error:{exc.errno or 'unknown'}"
            return False

    def send_frame(frame: FESLFrame) -> bool:
        return send_encoded(encode_packets(frame))

    def account_name() -> str:
        return context.identity.account_name if context.identity is not None else ""

    def begin_policy_close(event: AccountPolicyEvent) -> bool:
        nonlocal disconnect_reason
        packets = encode_packets(service.account_policy_frame(event.action))
        with dispatch_lock:
            with send_lock:
                if context.close_requested or policy_close.active:
                    return False
                context.close_reason = event.disconnect_reason
                try:
                    for packet in packets:
                        conn.sendall(packet)
                except OSError as exc:
                    disconnect_reason = f"send-error:{exc.errno or 'unknown'}"
                    policy_close.force(event.disconnect_reason)
                    return False
                policy_close.request(event.disconnect_reason)
        return bool(packets)

    def request_policy_close(reason: str) -> None:
        with dispatch_lock:
            with send_lock:
                context.close_reason = context.close_reason or str(
                    reason or "account-policy"
                )
                policy_close.request(context.close_reason)

    live_connections.register(
        LiveAccountConnection(
            connection_id=context.connection_id,
            protocol="carbon-fesl",
            account_name=account_name,
            begin_close=begin_policy_close,
            request_close=request_policy_close,
        )
    )
    log.info("Carbon FESL client connected: peer=%s:%d", addr[0], addr[1])
    try:
        while not stop_event.is_set() and not context.close_requested:
            if policy_close.expired():
                disconnect_reason = (
                    context.close_reason or policy_close.reason or "policy-close"
                )
                break
            try:
                data = conn.recv(8192)
            except socket.timeout:
                if context.close_requested:
                    disconnect_reason = context.close_reason or "policy-close"
                    break
                if policy_close.active:
                    if policy_close.expired():
                        disconnect_reason = (
                            context.close_reason
                            or policy_close.reason
                            or "policy-close"
                        )
                        break
                    continue
                now = time.monotonic()
                if now >= next_ping:
                    # Heartbeats keep the FESL transport alive in every phase,
                    # including while the client is still on the login screen.
                    # Persistent account/session activity is refreshed only
                    # after authentication established an identity.
                    with dispatch_lock:
                        if context.close_requested:
                            disconnect_reason = (
                                context.close_reason or "policy-close"
                            )
                            break
                        if context.identity is not None:
                            service.touch(context)
                    if not send_frame(service.ping_frame()):
                        if policy_close.active:
                            # Enforcement won the send-lock race.  The socket
                            # is intentionally quiescent, not broken; let the
                            # bounded drain window own the final close.
                            continue
                        if context.close_requested:
                            disconnect_reason = (
                                context.close_reason or "policy-close"
                            )
                        elif not disconnect_reason.startswith("send-error:"):
                            disconnect_reason = "heartbeat-send-error"
                        break
                    ping_requests += 1
                    next_ping = now + settings.fesl_heartbeat_interval
                    if ping_requests == 1 or ping_requests % 10 == 0:
                        log.info(
                            "Carbon FESL heartbeat sent: peer=%s:%d persona=%s "
                            "requests=%d acknowledgements=%d",
                            addr[0],
                            addr[1],
                            context.identity.persona
                            if context.identity is not None
                            else "<unauthenticated>",
                            ping_requests,
                            context.ping_responses,
                        )
                continue
            except OSError as exc:
                if context.close_requested or policy_close.active:
                    disconnect_reason = (
                        context.close_reason or policy_close.reason or "policy-close"
                    )
                else:
                    disconnect_reason = f"recv-error:{exc.errno or 'unknown'}"
                break
            if context.close_requested:
                disconnect_reason = context.close_reason or "policy-close"
                break
            if not data:
                disconnect_reason = "peer-eof"
                break
            if policy_close.active:
                continue
            try:
                frames = decoder.feed(data)
            except FESLFrameError as exc:
                log.warning(
                    "invalid Carbon FESL frame from %s:%d: %s",
                    addr[0],
                    addr[1],
                    exc,
                )
                disconnect_reason = "invalid-frame"
                break
            for frame in frames:
                operation = frame.fields.get("TXN", "<missing>")
                trace_frame = not (
                    frame.command == "fsys"
                    and str(operation).casefold() in {"ping", "memcheck"}
                )
                if trace_frame:
                    log.info(
                        "Carbon FESL request trace: peer=%s:%d persona=%s "
                        "command=%s operation=%s txn=0x%08x fields=%s",
                        addr[0],
                        addr[1],
                        context.identity.persona
                        if context.identity is not None
                        else "<unauthenticated>",
                        frame.command,
                        operation,
                        frame.transaction,
                        ",".join(sorted(frame.fields)) or "<none>",
                    )
                try:
                    with dispatch_lock:
                        if context.close_requested or policy_close.active:
                            disconnect_reason = (
                                context.close_reason
                                or policy_close.reason
                                or "policy-close"
                            )
                            replies: tuple[FESLFrame, ...] = ()
                        else:
                            previous_ping_responses = context.ping_responses
                            replies = tuple(service.dispatch(frame, context))
                except Exception:
                    log.exception(
                        "Carbon FESL dispatch failed: peer=%s:%d "
                        "persona=%s command=%s txn=0x%08x",
                        addr[0],
                        addr[1],
                        context.identity.persona
                        if context.identity is not None
                        else "<unauthenticated>",
                        frame.command,
                        frame.transaction,
                    )
                    disconnect_reason = "dispatch-error"
                    break
                if context.close_requested or policy_close.active:
                    break
                if context.ping_responses != previous_ping_responses and (
                    context.ping_responses == 1
                    or context.ping_responses % 10 == 0
                ):
                    log.info(
                        "Carbon FESL heartbeat acknowledged: peer=%s:%d persona=%s "
                        "requests=%d acknowledgements=%d transaction=%08x",
                        addr[0],
                        addr[1],
                        context.identity.persona
                        if context.identity is not None
                        else "<unauthenticated>",
                        ping_requests,
                        context.ping_responses,
                        frame.transaction,
                    )
                for reply in replies:
                    if trace_frame:
                        log.info(
                            "Carbon FESL reply trace: peer=%s:%d persona=%s "
                            "command=%s operation=%s txn=0x%08x fields=%s",
                            addr[0],
                            addr[1],
                            context.identity.persona
                            if context.identity is not None
                            else "<unauthenticated>",
                            reply.command,
                            reply.fields.get("TXN", "<missing>"),
                            reply.transaction,
                            ",".join(sorted(reply.fields)) or "<none>",
                        )
                    reply_delay = _reply_delay_seconds(frame, reply)
                    if reply_delay > 0:
                        time.sleep(reply_delay)
                    encoded_replies = encode_packets(reply)
                    if len(encoded_replies) > 1:
                        log.info(
                            "Carbon FESL fragmented reply: peer=%s:%d persona=%s "
                            "command=%s txn=0x%08x decoded=%d fragments=%d",
                            addr[0],
                            addr[1],
                            context.identity.persona
                            if context.identity is not None
                            else "<unauthenticated>",
                            reply.command,
                            reply.transaction,
                            max(
                                0,
                                len(reply.payload)
                                - int(reply.payload.endswith(b"\x00")),
                            ),
                            len(encoded_replies),
                        )
                    if not send_encoded(encoded_replies):
                        if context.close_requested or policy_close.active:
                            disconnect_reason = (
                                context.close_reason
                                or policy_close.reason
                                or "policy-close"
                            )
                        break
                    if frame.command == "pnow":
                        log.info(
                            "Carbon FESL PlayNow reply sent: peer=%s:%d persona=%s "
                            "stage=%s txn=0x%08x delay_ms=%d",
                            addr[0],
                            addr[1],
                            context.identity.persona
                            if context.identity is not None
                            else "<unauthenticated>",
                            reply.fields.get("TXN", "<missing>"),
                            reply.transaction,
                            round(reply_delay * 1000),
                        )
                if (
                    context.close_requested
                    or policy_close.active
                    or disconnect_reason.startswith("send-error:")
                    or disconnect_reason == "dispatch-error"
                ):
                    break
    finally:
        live_connections.unregister(context.connection_id)
        persona = (
            context.identity.persona
            if context.identity is not None
            else "<unauthenticated>"
        )
        service.disconnect(context)
        log.info(
            "Carbon FESL client disconnected: peer=%s:%d persona=%s "
            "uptime=%.3f reason=%s heartbeat_requests=%d heartbeat_acks=%d",
            addr[0],
            addr[1],
            persona,
            time.monotonic() - connected_at,
            context.close_reason or disconnect_reason,
            ping_requests,
            context.ping_responses,
        )


__all__ = ["handle_fesl_connection"]
