"""Policy-aware live Carbon Theater connection runtime."""

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
from carbon.fesl.frame import FESLFrame, FESLFrameError, FESLStreamDecoder
from carbon.theater.service import CarbonTheaterService, TheaterConnection


log = logging.getLogger("carbon.app")

_THEATER_EGAM_GDET_DELAY_SECONDS = 0.028
_THEATER_EGAM_COMPLETION_DELAY_SECONDS = 0.080


def _reply_schedule(
    request: FESLFrame,
    replies: tuple[FESLFrame, ...],
) -> tuple[tuple[float, tuple[FESLFrame, ...]], ...]:
    """Return capture-backed Theater reply batches and relative delays."""

    if request.command == "EGAM" and [reply.command for reply in replies] == [
        "GDET",
        "EGAM",
        "EGEG",
    ]:
        return (
            (_THEATER_EGAM_GDET_DELAY_SECONDS, (replies[0],)),
            (_THEATER_EGAM_COMPLETION_DELAY_SECONDS, (replies[1], replies[2])),
        )
    return tuple((0.0, (reply,)) for reply in replies)


def handle_theater_connection(
    conn: socket.socket,
    addr: tuple[str, int],
    stop_event: Event,
    *,
    settings: ServerSettings,
    service: CarbonTheaterService,
    live_connections: LiveAccountConnectionRegistry,
) -> None:
    # Socket timeout is only a shutdown/push polling interval.  The EGAM
    # reply window below follows the ordering and timing in retail traces.
    conn.settimeout(min(settings.connection_timeout, 0.1))
    decoder = FESLStreamDecoder(max_frame_size=settings.max_frame_size)
    context = TheaterConnection(
        connection_id=f"carbon-theater:{addr[0]}:{addr[1]}:{uuid4().hex}",
        peer_ip=addr[0],
        peer_port=addr[1],
    )
    send_lock = Lock()
    dispatch_lock = Lock()
    disconnect_reason = "server-stop"
    policy_close = PolicyCloseGate()

    def encode_frames(frames) -> bytes:
        return b"".join(frame.encode() for frame in frames)

    def send_encoded(payload: bytes) -> bool:
        nonlocal disconnect_reason
        if not payload:
            return True
        try:
            with send_lock:
                if context.close_requested or policy_close.active:
                    return False
                conn.sendall(payload)
            return True
        except OSError as exc:
            context.close_requested = True
            context.close_reason = (
                context.close_reason
                or f"send-error:{exc.errno or 'unknown'}"
            )
            disconnect_reason = context.close_reason
            return False

    def send_frames(frames) -> bool:
        return send_encoded(encode_frames(frames))

    def send_frame(frame) -> bool:
        return send_frames((frame,))

    def account_name() -> str:
        return context.identity.account_name if context.identity is not None else ""

    def begin_policy_close(event: AccountPolicyEvent) -> bool:
        with dispatch_lock:
            with send_lock:
                if context.close_requested or policy_close.active:
                    return False
                context.close_reason = event.disconnect_reason
                policy_close.request(event.disconnect_reason)
        # Theater has no proven unsolicited stock ban frame.  FESL owns the
        # user-visible notification; Theater becomes read-only immediately and
        # shares the same bounded drain window before physical socket closure.
        return False

    def request_policy_close(reason: str) -> None:
        with dispatch_lock:
            with send_lock:
                context.close_reason = context.close_reason or str(
                    reason or "account-policy"
                )
                policy_close.request(context.close_reason)

    context.sender = send_frame
    live_connections.register(
        LiveAccountConnection(
            connection_id=context.connection_id,
            protocol="carbon-theater",
            account_name=account_name,
            begin_close=begin_policy_close,
            request_close=request_policy_close,
        )
    )
    log.info("Carbon Theater client connected: peer=%s:%d", addr[0], addr[1])
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
                if policy_close.active:
                    if policy_close.expired():
                        disconnect_reason = (
                            context.close_reason
                            or policy_close.reason
                            or "policy-close"
                        )
                        break
                    continue
                with dispatch_lock:
                    if context.close_requested:
                        pending: tuple[FESLFrame, ...] = ()
                    else:
                        pending = tuple(context.drain())
                for frame in pending:
                    if not send_frame(frame):
                        break
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
                    "invalid Carbon Theater frame from %s:%d: %s",
                    addr[0],
                    addr[1],
                    exc,
                )
                disconnect_reason = "invalid-frame"
                break
            for frame in frames:
                try:
                    with dispatch_lock:
                        if context.close_requested or policy_close.active:
                            replies: tuple[FESLFrame, ...] = ()
                        else:
                            replies = tuple(service.dispatch(frame, context))
                except Exception:
                    log.exception(
                        "Carbon Theater dispatch failed: peer=%s:%d "
                        "persona=%s command=%s",
                        addr[0],
                        addr[1],
                        context.identity.persona
                        if context.identity is not None
                        else "<unauthenticated>",
                        frame.command,
                    )
                    disconnect_reason = "dispatch-error"
                    break
                if context.close_requested or policy_close.active:
                    disconnect_reason = (
                        context.close_reason or policy_close.reason or "policy-close"
                    )
                    break
                for reply_delay, reply_batch in _reply_schedule(
                    frame, replies
                ):
                    if reply_delay > 0:
                        time.sleep(reply_delay)
                    if not send_frames(reply_batch):
                        break
                    if frame.command == "EGAM":
                        log.info(
                            "Carbon Theater EGAM reply batch sent: peer=%s:%d persona=%s "
                            "commands=%s delay_ms=%d bytes=%d",
                            addr[0],
                            addr[1],
                            context.identity.persona
                            if context.identity is not None
                            else "<unauthenticated>",
                            "+".join(reply.command for reply in reply_batch),
                            round(reply_delay * 1000),
                            sum(len(reply.encode()) for reply in reply_batch),
                        )
                        if any(reply.command == "EGEG" for reply in reply_batch):
                            service.complete_invite_entry(context)
                    if context.close_requested:
                        break
                if (
                    frame.command == "GLST"
                    and not context.close_requested
                    and not disconnect_reason.startswith("send-error:")
                ):
                    # Messenger must not deliver ADMN/DUPL until the Theater
                    # list bootstrap is on the wire.  Otherwise Carbon closes
                    # all services before its frontend can present -204.
                    service.complete_forced_logoff_bootstrap(context)
                if (
                    context.close_requested
                    or disconnect_reason.startswith("send-error:")
                    or disconnect_reason == "dispatch-error"
                ):
                    break
            if (
                context.close_requested
                or disconnect_reason == "dispatch-error"
                or disconnect_reason.startswith("send-error:")
            ):
                break
            with dispatch_lock:
                if context.close_requested:
                    pending = ()
                else:
                    pending = tuple(context.drain())
            for frame in pending:
                if not send_frame(frame):
                    break
    finally:
        live_connections.unregister(context.connection_id)
        context.sender = None
        service.disconnect(context)
        log.info(
            "Carbon Theater client disconnected: peer=%s:%d persona=%s reason=%s",
            addr[0],
            addr[1],
            context.identity.persona
            if context.identity is not None
            else "<unauthenticated>",
            context.close_reason or disconnect_reason,
        )


__all__ = ["handle_theater_connection"]
