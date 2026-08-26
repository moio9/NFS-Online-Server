"""Cumulative-ACK confirmation windows for Carbon CommUDP publications."""

from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
import logging
import time
import zlib

from carbon.rebroadcaster.retry import RetryPolicy, RetrySchedule
from carbon.rebroadcaster.state import Address, EndpointWireState


_SEQUENCE_MASK = 0x0FFFFFFF
_SEQUENCE_HALF = 0x08000000
_CONFIRMATION_RETRY_POLICY = RetryPolicy(0.25, 2.0, 16, 45.0)


@dataclass
class PendingConfirmation:
    """Exact encrypted records retained until their final sequence is ACKed."""

    label: str
    records: tuple[bytes, ...]
    base_sequence: int
    final_sequence: int
    retry: RetrySchedule
    application_confirmation: bool = False
    transport_acknowledged: bool = False
    exhausted_logged: bool = False


class ConfirmationManager:
    """Own generic reliable delivery independently of client frame rate."""

    def __init__(
        self,
        wires: MutableMapping[Address, EndpointWireState],
        *,
        retry_policy: RetryPolicy = _CONFIRMATION_RETRY_POLICY,
        logger: logging.Logger | None = None,
    ) -> None:
        self._wires = wires
        self.retry_policy = retry_policy
        self._pending: dict[Address, list[PendingConfirmation]] = {}
        # The gameplay state machines need the ACK carried by the latest
        # client packet, even when Carbon has moved to a phase-local sequence
        # space.  Retain a separate monotonic transport observation for
        # diagnostics; pending windows themselves are irreversible and are
        # retired only by an ACK observed after they were registered.
        self._acknowledgement_high_water: dict[Address, int] = {}
        self._recent_inbound: dict[
            Address,
            tuple[deque[tuple[int, int]], set[tuple[int, int]]],
        ] = {}
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def sequence_acked(acknowledgement: int, target: int) -> bool:
        delta = (int(acknowledgement) - int(target)) & _SEQUENCE_MASK
        return delta < _SEQUENCE_HALF

    @staticmethod
    def sequence_newer(candidate: int, current: int) -> bool:
        delta = (int(candidate) - int(current)) & _SEQUENCE_MASK
        return 0 < delta < _SEQUENCE_HALF

    def observe_inbound(
        self,
        address: Address,
        *,
        sequence: int,
        acknowledgement: int,
        payload: bytes,
        track_sequence: bool,
    ) -> bool:
        """Record phase-local anchors and return whether this is a duplicate."""

        wire = self._wires[address]
        ack = int(acknowledgement) & _SEQUENCE_MASK
        # Preserve the exact latest ACK for the existing phase-specific Carbon
        # gates.  A cleared confirmation window is never reopened by a later,
        # lower ACK.
        wire.last_client_acknowledgement = ack
        self.acknowledge(address, ack)

        if not track_sequence:
            return False
        received = int(sequence) & _SEQUENCE_MASK
        # Carbon restarts its client reliable sequence between some bootstrap
        # phases (for example 0x101 -> session-object 0x0a). Sequence ordering
        # therefore cannot be global. Detect only an exact wire duplicate and
        # keep the newest phase-local value as the server ACK anchor.
        wire.last_client_sequence = received
        # ACK and footer timing may legitimately change when the client
        # retries one reliable request.  The phase-local sequence plus logical
        # GameManager payload identifies the request that needs a replay.
        fingerprint = (received, zlib.crc32(bytes(payload)))
        recent = self._recent_inbound.get(address)
        if recent is None:
            recent = (deque(), set())
            self._recent_inbound[address] = recent
        order, members = recent
        if fingerprint in members:
            return True
        order.append(fingerprint)
        members.add(fingerprint)
        while len(order) > 256:
            members.discard(order.popleft())
        return False

    def register(
        self,
        address: Address,
        records: Sequence[bytes],
        *,
        base_sequence: int,
        final_sequence: int,
        label: str,
        application_confirmation: bool = False,
        now: float | None = None,
    ) -> PendingConfirmation | None:
        """Retain one exact wire window for a later inbound confirmation."""

        base = int(base_sequence) & _SEQUENCE_MASK
        final = int(final_sequence) & _SEQUENCE_MASK
        payloads = tuple(bytes(record) for record in records if record)
        if not payloads:
            return None
        pending = self._pending.setdefault(address, [])
        for existing in pending:
            if (
                existing.final_sequence == final
                and existing.records == payloads
            ):
                return existing
        opened_at = time.monotonic() if now is None else float(now)
        window = PendingConfirmation(
            label=str(label),
            records=payloads,
            base_sequence=base,
            final_sequence=final,
            retry=self.retry_policy.begin(opened_at),
            application_confirmation=bool(application_confirmation),
        )
        pending.append(window)
        self.log.debug(
            "Carbon GM confirmation opened: dst=%s:%d label=%s "
            "seq=%07x-%07x records=%d",
            address[0],
            address[1],
            window.label,
            base,
            final,
            len(payloads),
        )
        return window

    def _advance_acknowledgement_high_water(
        self,
        address: Address,
        acknowledgement: int,
    ) -> int:
        ack = int(acknowledgement) & _SEQUENCE_MASK
        current = self._acknowledgement_high_water.get(address)
        if current is None or self.sequence_newer(ack, current):
            self._acknowledgement_high_water[address] = ack
            return ack
        return current

    def acknowledge(self, address: Address, acknowledgement: int) -> int:
        ack = int(acknowledgement) & _SEQUENCE_MASK
        self._advance_acknowledgement_high_water(
            address,
            ack,
        )
        pending = self._pending.get(address)
        if not pending:
            return 0
        retained: list[PendingConfirmation] = []
        for window in pending:
            if not self.sequence_acked(ack, window.final_sequence):
                retained.append(window)
                continue
            if not window.application_confirmation:
                continue
            if not window.transport_acknowledged:
                window.transport_acknowledged = True
                self.log.info(
                    "Carbon GM transport ACK observed; awaiting application "
                    "confirmation: dst=%s:%d label=%s ack=%07x target=%07x",
                    address[0],
                    address[1],
                    window.label,
                    ack,
                    window.final_sequence,
                )
            retained.append(window)
        cleared = len(pending) - len(retained)
        if retained:
            self._pending[address] = retained
        else:
            self._pending.pop(address, None)
        if cleared:
            self.log.debug(
                "Carbon GM confirmations acknowledged: dst=%s:%d "
                "ack=%07x cleared=%d remaining=%d",
                address[0],
                address[1],
                ack,
                cleared,
                len(retained),
            )
        return cleared

    def confirm_application(self, address: Address, *, label: str) -> int:
        """Retire application-owned windows after the expected next stage."""

        pending = self._pending.get(address)
        if not pending:
            return 0
        retained = [
            window
            for window in pending
            if not (
                window.application_confirmation
                and window.label == str(label)
            )
        ]
        cleared = len(pending) - len(retained)
        if retained:
            self._pending[address] = retained
        else:
            self._pending.pop(address, None)
        if cleared:
            self.log.info(
                "Carbon GM application confirmation observed: "
                "dst=%s:%d label=%s cleared=%d",
                address[0],
                address[1],
                label,
                cleared,
            )
        return cleared

    def poll(
        self,
        *,
        now: float | None = None,
    ) -> list[tuple[bytes, Address]]:
        current = time.monotonic() if now is None else float(now)
        replies: list[tuple[bytes, Address]] = []
        for address, pending in tuple(self._pending.items()):
            wire = self._wires.get(address)
            if wire is None:
                self._pending.pop(address, None)
                continue
            for window in tuple(self._pending.get(address, ())):
                if window.transport_acknowledged:
                    if (
                        current >= window.retry.deadline
                        and not window.exhausted_logged
                    ):
                        window.exhausted_logged = True
                        self.log.warning(
                            "Carbon GM application confirmation wait dormant: "
                            "dst=%s:%d label=%s seq=%07x-%07x elapsed=%.3f "
                            "action=retain-without-wire-replay",
                            address[0],
                            address[1],
                            window.label,
                            window.base_sequence,
                            window.final_sequence,
                            max(0.0, current - window.retry.opened_at),
                        )
                    continue
                reason = window.retry.exhaustion_reason(current)
                if reason is not None:
                    if not window.exhausted_logged:
                        window.exhausted_logged = True
                        self.log.warning(
                            "Carbon GM confirmation retry dormant: "
                            "dst=%s:%d label=%s seq=%07x-%07x attempts=%d "
                            "reason=%s action=retain-for-client-replay",
                            address[0],
                            address[1],
                            window.label,
                            window.base_sequence,
                            window.final_sequence,
                            window.retry.retries_sent,
                            reason,
                        )
                    continue
                if not window.retry.due(current):
                    continue
                replies.extend((record, address) for record in window.records)
                delay = window.retry.record_retry(current)
                self.log.info(
                    "Carbon GM confirmation retried: dst=%s:%d label=%s "
                    "seq=%07x-%07x attempt=%d records=%d next_retry=%.3f",
                    address[0],
                    address[1],
                    window.label,
                    window.base_sequence,
                    window.final_sequence,
                    window.retry.retries_sent,
                    len(window.records),
                    delay,
                )
        return replies

    def replay_pending(
        self,
        address: Address,
        *,
        now: float | None = None,
        reason: str,
    ) -> list[tuple[bytes, Address]]:
        """Immediately replay unacked windows after a duplicate client packet."""

        current = time.monotonic() if now is None else float(now)
        wire = self._wires.get(address)
        if wire is None:
            return []
        pending = tuple(
            window
            for window in self._pending.get(address, ())
            if not window.transport_acknowledged
        )
        replies = [
            (record, address)
            for window in pending
            for record in window.records
        ]
        if not replies:
            return []
        for window in pending:
            if window.retry.exhausted(current):
                window.retry = self.retry_policy.begin(current)
                window.exhausted_logged = False
            else:
                window.retry.defer_from(current)
        self.log.info(
            "Carbon GM confirmations replayed on client retry: "
            "dst=%s:%d windows=%d records=%d reason=%s",
            address[0],
            address[1],
            len(pending),
            len(replies),
            reason,
        )
        return replies

    def pending(self, address: Address) -> tuple[PendingConfirmation, ...]:
        return tuple(self._pending.get(address, ()))

    def clear_endpoint(self, address: Address) -> None:
        self._pending.pop(address, None)
        self._acknowledgement_high_water.pop(address, None)
        self._recent_inbound.pop(address, None)
