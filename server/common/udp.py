"""Shared UDP reply dispatch; listeners retain protocol-specific behavior."""
from __future__ import annotations

import time
from typing import Iterable

DatagramReply = tuple[bytes, tuple[str, int]]


class UDPReplyDispatcher:
    """Reusable reply processing for listeners with their own send queues."""

    def _dispatch_replies(self, replies: Iterable[DatagramReply]) -> None:
        for index, (response, target) in enumerate(replies):
            if not response:
                continue
            delay = self._reply_spacing_for(target) if index else 0.0
            if self.isolate_reply_targets:
                self._enqueue_send(response, target, delay)
                continue
            if delay:
                time.sleep(delay)
            self._send_response(response, target)

    def _reply_spacing_for(self, target: tuple[str, int]) -> float:
        configured = self.reply_spacing_seconds
        if callable(configured):
            try:
                return max(0.0, float(configured(target)))
            except Exception:
                self._udp_log.exception(
                    "%s reply-spacing resolver failed for %s:%d",
                    self.name,
                    target[0],
                    target[1],
                )
                return 0.0
        return max(0.0, float(configured))

    def _send_response(self, response: bytes, target: tuple[str, int]) -> None:
        sock = self._socket
        if sock is None or self.stop_event.is_set():
            return
        sock.sendto(response, target)
